from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

from services.account_service import AccountService, account_service
from services.config import DATA_DIR, config
from services.scheduler.account_pool import AccountPool
from services.scheduler.context import ExecutionContext
from services.scheduler.lane import BoundedStream, LaneScheduler
from services.scheduler.models import AccountRequirement, Lane, Lease, Outcome, SchedulerSettings
from services.scheduler.reconciler import AccountReconciler
from services.scheduler.state_store import SchedulerStateStore, SnapshotWriter
from utils.helper import anonymize_token, split_image_model


class SchedulerService:
    """Own the process-local account scheduler and its lifecycle."""

    def __init__(
        self,
        account_service_instance: AccountService | Any = account_service,
        *,
        settings: SchedulerSettings | None = None,
        state_store: SchedulerStateStore | None = None,
        model_router: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.account_service = account_service_instance
        self.settings = settings or config.get_scheduler_settings()
        self._clock = clock
        self._stop_event = Event()
        self.state_store = state_store or SchedulerStateStore(DATA_DIR / "scheduler.db")
        self.account_pool = AccountPool(
            self.account_service.scheduler_snapshot(),
            self.settings,
            model_router=model_router or self._default_model_router,
            clock=clock,
        )
        self.reconciler = AccountReconciler(
            self.account_service,
            self.account_pool,
            interval_seconds=self.settings.account_reconcile_interval_ms / 1000.0,
            stop_event=self._stop_event,
        )
        self.snapshot_writer = SnapshotWriter(
            self.state_store,
            interval_seconds=self.settings.snapshot_flush_interval_ms / 1000.0,
        )
        self._lanes = {
            lane: LaneScheduler(
                lane,
                self.settings,
                self.account_pool,
                clock=clock,
                lease_replacer=self._replace_execution_lease,
            )
            for lane in Lane
        }
        self._cooldown_thread: Thread | None = None
        self._watchdog_thread: Thread | None = None
        self._started = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._started

    def lane(self, lane: Lane) -> LaneScheduler:
        return self._lanes[lane]

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if not self.settings.enabled:
                return
            self._stop_event.clear()
            self.state_store.initialize()
            self.reconciler.reconcile_now()
            for lane in self._lanes.values():
                await lane.start()
            self.snapshot_writer.start()
            self._cooldown_thread = self._start_thread(
                "scheduler-cooldown",
                self._cooldown_loop,
            )
            self._watchdog_thread = self._start_thread(
                "scheduler-lease-watchdog",
                self._watchdog_loop,
            )
            self.reconciler.start()
            self._started = True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                return
            self._stop_event.set()
            self.reconciler.stop(timeout=1)
            for lane in self._lanes.values():
                await lane.stop()
            self.snapshot_writer.stop(timeout=1)
            for thread in (self._cooldown_thread, self._watchdog_thread):
                if thread is not None and thread.is_alive():
                    thread.join(timeout=1)
            self._cooldown_thread = None
            self._watchdog_thread = None
            self.state_store.close()
            self._started = False

    def reconcile_now(self) -> None:
        self.reconciler.reconcile_now()

    async def run_text(
        self,
        requirement: AccountRequirement,
        work: Callable[..., Any],
        *,
        priority: int = 0,
        deadline: float | None = None,
    ) -> Any:
        return await self.lane(Lane.TEXT).submit(
            work,
            requirement,
            cost=1,
            priority=priority,
            deadline=deadline,
        )

    async def run_image(
        self,
        requirement: AccountRequirement,
        work: Callable[..., Any],
        *,
        priority: int = 0,
        deadline: float | None = None,
    ) -> Any:
        if requirement.lane not in {Lane.IMAGE_GENERATION, Lane.IMAGE_EDIT}:
            raise ValueError("run_image requires an image lane")
        return await self.lane(requirement.lane).submit(
            work,
            requirement,
            cost=max(1, requirement.cost),
            priority=priority,
            deadline=deadline,
        )

    async def run_stream(
        self,
        lane: Lane,
        requirement: AccountRequirement,
        work: Callable[..., Any],
        *,
        cost: int = 1,
        priority: int = 0,
        deadline: float | None = None,
    ) -> BoundedStream:
        if lane != requirement.lane:
            raise ValueError("stream lane does not match requirement")
        return await self.lane(lane).submit_stream(
            work,
            requirement,
            cost=max(1, int(cost)),
            priority=priority,
            deadline=deadline,
        )

    def classify_text(self, model: str | None = None) -> AccountRequirement:
        return AccountRequirement(Lane.TEXT, str(model or "auto").strip() or "auto")

    def classify_image_generation(self, payload: dict[str, object]) -> AccountRequirement:
        return self._classify_image(payload, Lane.IMAGE_GENERATION)

    def classify_image_edit(self, payload: dict[str, object]) -> AccountRequirement:
        return self._classify_image(payload, Lane.IMAGE_EDIT)

    def metrics_snapshot(self) -> dict[str, object]:
        health = []
        for item in self.account_pool.snapshot_health():
            redacted = dict(item)
            token = str(redacted.pop("account_key", ""))
            redacted["account_ref"] = anonymize_token(token)
            health.append(redacted)
        return {
            "enabled": bool(self.settings.enabled),
            "running": self._started,
            "lanes": {
                lane.value: self._lanes[lane].metrics_snapshot()
                for lane in Lane
            },
            "account_health": health,
        }

    def _classify_image(self, payload: dict[str, object], lane: Lane) -> AccountRequirement:
        requested_model = str(payload.get("model") or "gpt-image-2").strip() or "gpt-image-2"
        plan_type, model = split_image_model(requested_model)
        try:
            cost = max(1, int(payload.get("n") or 1))
        except (TypeError, ValueError):
            cost = 1
        return AccountRequirement(
            lane,
            model or requested_model,
            plan_type=plan_type,
            source_type=str(payload.get("source_type") or "web"),
            cost=cost,
        )

    @staticmethod
    def _default_model_router(model: str) -> object:
        # Model catalog refresh performs network I/O. The scheduler starts with
        # a local all-capability fallback and can be given a warmed route map.
        return set()

    def _replace_execution_lease(
        self,
        context: ExecutionContext,
        excluded_tokens: set[str],
        outcome: Outcome,
    ) -> Lease | None:
        current = context.lease
        if current is not None:
            self.account_pool.release(current, outcome=outcome, latency_ms=0.0)
            context.lease = None
            context.lease_released = True
        replacement = self.account_pool.try_acquire(
            context.requirement,
            deadline=current.deadline if current is not None else self._clock() + 1,
            excluded=excluded_tokens,
        )
        if replacement is None:
            return None
        context.lease = replacement
        context.lease_released = False
        return replacement

    def _start_thread(self, name: str, target: Callable[[], None]) -> Thread:
        thread = Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread

    def _cooldown_loop(self) -> None:
        interval = max(0.05, self.settings.lease_watchdog_interval_ms / 1000.0)
        while not self._stop_event.wait(interval):
            self.account_pool.promote_due_cooldowns()

    def _watchdog_loop(self) -> None:
        interval = max(0.05, self.settings.lease_watchdog_interval_ms / 1000.0)
        while not self._stop_event.wait(interval):
            self.account_pool.reclaim_expired()
            for snapshot in self.account_pool.snapshot_health():
                self.snapshot_writer.submit(snapshot)


scheduler_service = SchedulerService()


__all__ = ["SchedulerService", "scheduler_service"]
