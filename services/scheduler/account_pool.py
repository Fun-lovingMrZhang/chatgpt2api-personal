from __future__ import annotations

import heapq
import time
import uuid
from dataclasses import dataclass, field
from threading import RLock
from typing import Callable, Iterable

from services.scheduler.models import (
    AccountRequirement,
    ErrorClass,
    Lane,
    Lease,
    Outcome,
)


@dataclass
class _Breaker:
    state: str = "closed"
    cooldown_until: float = 0.0
    probe_inflight: int = 0


@dataclass
class _AccountRuntime:
    account_key: str
    raw: dict[str, object]
    account_type: str
    plan_type: str
    source_type: str
    quota: int
    lane_inflight: dict[Lane, int] = field(default_factory=dict)
    image_total_inflight: int = 0
    ewma_latency_ms: dict[Lane, float] = field(default_factory=dict)
    successes: dict[Lane, int] = field(default_factory=dict)
    failures: dict[Lane, int] = field(default_factory=dict)
    consecutive_failures: dict[Lane, int] = field(default_factory=dict)
    breakers: dict[Lane, _Breaker] = field(default_factory=dict)
    revision: int = 0

    def breaker(self, lane: Lane) -> _Breaker:
        return self.breakers.setdefault(lane, _Breaker())


class AccountPool:
    """Process-local account capability index and lease manager.

    The pool never waits for capacity. Lane schedulers decide whether to queue
    or reject while this object performs a short, lock-protected reservation.
    """

    def __init__(
        self,
        accounts: Iterable[dict[str, object]],
        settings,
        *,
        model_router: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self._model_router = model_router or (lambda _model: set())
        self._clock = clock
        self._lock = RLock()
        self._accounts: dict[str, _AccountRuntime] = {}
        self._heaps: dict[tuple[object, ...], list[tuple[float, int, str, int]]] = {}
        self._leases: dict[str, Lease] = {}
        self._lease_accounts: dict[str, str] = {}
        self._sequence = 0
        self._selection_probe_count = 0
        self.reconcile(accounts)

    def clock(self) -> float:
        return self._clock()

    @property
    def selection_probe_count(self) -> int:
        return self._selection_probe_count

    def reset_selection_probe_count(self) -> None:
        with self._lock:
            self._selection_probe_count = 0

    def account_keys(self) -> set[str]:
        with self._lock:
            return set(self._accounts)

    def inflight(self, account_key: str, lane: Lane) -> int:
        with self._lock:
            account = self._accounts.get(account_key)
            return int(account.lane_inflight.get(lane, 0)) if account else 0

    def reconcile(self, accounts: Iterable[dict[str, object]]) -> None:
        incoming = {
            str(item.get("access_token") or "").strip(): dict(item)
            for item in accounts
            if str(item.get("access_token") or "").strip()
        }
        with self._lock:
            previous = self._accounts
            next_accounts: dict[str, _AccountRuntime] = {}
            for account_key, raw in incoming.items():
                current = previous.get(account_key)
                runtime = self._new_runtime(account_key, raw, current)
                runtime.revision = (current.revision + 1) if current else 1
                next_accounts[account_key] = runtime
            for account_key, current in previous.items():
                if account_key in next_accounts or not self._has_active_lease(account_key):
                    continue
                current.raw = {**current.raw, "status": "禁用"}
                current.revision += 1
                next_accounts[account_key] = current
            self._accounts = next_accounts
            self._heaps.clear()
            for account in self._accounts.values():
                self._publish_locked(account)

    def try_acquire(
        self,
        requirement: AccountRequirement,
        *,
        deadline: float,
        excluded: set[str] | None = None,
    ) -> Lease | None:
        cost = max(1, int(requirement.cost))
        excluded = set(excluded or set())
        with self._lock:
            candidate = self._best_candidate_locked(requirement, excluded, cost)
            if candidate is None:
                return None
            account = self._accounts[candidate]
            breaker = account.breaker(requirement.lane)
            if breaker.state == "half_open":
                breaker.probe_inflight += 1
            account.lane_inflight[requirement.lane] = (
                int(account.lane_inflight.get(requirement.lane, 0)) + cost
            )
            if requirement.lane in {Lane.IMAGE_GENERATION, Lane.IMAGE_EDIT}:
                account.image_total_inflight += cost
            account.revision += 1
            self._sequence += 1
            lease = Lease(
                lease_id=uuid.uuid4().hex,
                access_token=candidate,
                account_key=candidate,
                lane=requirement.lane,
                cost=cost,
                deadline=deadline,
                token_generation=int(account.raw.get("token_generation") or 0),
            )
            self._leases[lease.lease_id] = lease
            self._lease_accounts[lease.lease_id] = candidate
            self._publish_locked(account)
            return lease

    def release(self, lease: Lease | None, *, outcome: Outcome, latency_ms: float) -> None:
        if lease is None:
            return
        with self._lock:
            if lease.lease_id not in self._leases:
                return
            self._leases.pop(lease.lease_id, None)
            account_key = self._lease_accounts.pop(lease.lease_id, lease.account_key)
            account = self._accounts.get(account_key)
            if account is None:
                return
            lane_count = int(account.lane_inflight.get(lease.lane, 0))
            account.lane_inflight[lease.lane] = max(0, lane_count - lease.cost)
            if lease.lane in {Lane.IMAGE_GENERATION, Lane.IMAGE_EDIT}:
                account.image_total_inflight = max(0, account.image_total_inflight - lease.cost)
            breaker = account.breaker(lease.lane)
            if breaker.state == "half_open":
                breaker.probe_inflight = max(0, breaker.probe_inflight - 1)
            self._record_outcome_locked(account, lease.lane, outcome, latency_ms)
            account.revision += 1
            self._publish_locked(account)

    def record_failure(self, account_key: str, lane: Lane, error_class: ErrorClass) -> None:
        with self._lock:
            account = self._accounts.get(account_key)
            if account is None:
                return
            account.consecutive_failures[lane] = int(account.consecutive_failures.get(lane, 0)) + 1
            if error_class == ErrorClass.RATE_LIMIT:
                self._open_breaker_locked(account, lane, 2.0)
            elif account.consecutive_failures[lane] >= 3:
                self._open_breaker_locked(account, lane, 5.0)
            account.revision += 1
            self._publish_locked(account)

    def record_success(self, account_key: str, lane: Lane, latency_ms: float) -> None:
        with self._lock:
            account = self._accounts.get(account_key)
            if account is None:
                return
            self._record_outcome_locked(account, lane, Outcome.SUCCESS, latency_ms)
            account.revision += 1
            self._publish_locked(account)

    def open_breaker(self, account_key: str, lane: Lane, cooldown_seconds: float = 30.0) -> None:
        with self._lock:
            account = self._accounts.get(account_key)
            if account is None:
                return
            self._open_breaker_locked(account, lane, cooldown_seconds)
            account.revision += 1
            self._publish_locked(account)

    def promote_due_cooldowns(self) -> int:
        now = self._clock()
        promoted = 0
        with self._lock:
            for account in self._accounts.values():
                for lane in Lane:
                    breaker = account.breaker(lane)
                    if breaker.state == "open" and breaker.cooldown_until <= now:
                        breaker.state = "half_open"
                        breaker.probe_inflight = 0
                        account.revision += 1
                        self._publish_locked(account)
                        promoted += 1
        return promoted

    def reclaim_expired(self, now: float | None = None) -> list[Lease]:
        current_time = self._clock() if now is None else now
        with self._lock:
            expired = [lease for lease in self._leases.values() if lease.deadline <= current_time]
        for lease in expired:
            self.release(lease, outcome=Outcome.CANCELLED, latency_ms=0)
        return expired

    def snapshot_health(self) -> list[dict[str, object]]:
        with self._lock:
            items = []
            for account in self._accounts.values():
                for lane in Lane:
                    breaker = account.breaker(lane)
                    items.append({
                        "account_key": account.account_key,
                        "lane": lane.value,
                        "breaker_state": breaker.state,
                        "cooldown_until": breaker.cooldown_until,
                        "quota_snapshot": account.quota,
                        "ewma_latency_ms": account.ewma_latency_ms.get(lane, 0.0),
                        "recent_success_count": account.successes.get(lane, 0),
                        "recent_failure_count": account.failures.get(lane, 0),
                        "last_error_class": "",
                        "inflight": account.lane_inflight.get(lane, 0),
                    })
            return items

    def _new_runtime(
        self,
        account_key: str,
        raw: dict[str, object],
        previous: _AccountRuntime | None,
    ) -> _AccountRuntime:
        runtime = _AccountRuntime(
            account_key=account_key,
            raw=raw,
            account_type=self._normalize(raw.get("type")),
            plan_type=self._normalize(raw.get("plan_type") or raw.get("type")),
            source_type=self._normalize(raw.get("source_type") or raw.get("source")),
            quota=self._quota(raw.get("quota")),
        )
        if previous is not None:
            runtime.lane_inflight = dict(previous.lane_inflight)
            runtime.image_total_inflight = previous.image_total_inflight
            runtime.ewma_latency_ms = dict(previous.ewma_latency_ms)
            runtime.successes = dict(previous.successes)
            runtime.failures = dict(previous.failures)
            runtime.consecutive_failures = dict(previous.consecutive_failures)
            runtime.breakers = {
                lane: _Breaker(item.state, item.cooldown_until, item.probe_inflight)
                for lane, item in previous.breakers.items()
            }
        return runtime

    def _best_candidate_locked(
        self,
        requirement: AccountRequirement,
        excluded: set[str],
        cost: int,
    ) -> str | None:
        best: tuple[float, str] | None = None
        for heap_key in self._heap_keys_for_requirement(requirement):
            heap = self._heaps.get(heap_key)
            if not heap:
                continue
            entry = self._valid_top_locked(heap, requirement, excluded, cost)
            if entry is None:
                continue
            score, _sequence, account_key, _revision = entry
            if best is None or (score, account_key) < best:
                best = (score, account_key)
        return best[1] if best else None

    def _valid_top_locked(
        self,
        heap: list[tuple[float, int, str, int]],
        requirement: AccountRequirement,
        excluded: set[str],
        cost: int,
    ) -> tuple[float, int, str, int] | None:
        skipped: list[tuple[float, int, str, int]] = []
        selected = None
        while heap:
            entry = heapq.heappop(heap)
            self._selection_probe_count += 1
            _score, _sequence, account_key, revision = entry
            account = self._accounts.get(account_key)
            if account is None or account.revision != revision:
                continue
            if account_key in excluded:
                skipped.append(entry)
                continue
            if not self._eligible_locked(account, requirement, cost):
                continue
            selected = entry
            break
        for entry in skipped:
            heapq.heappush(heap, entry)
        if selected is not None:
            heapq.heappush(heap, selected)
        return selected

    def _eligible_locked(self, account: _AccountRuntime, requirement: AccountRequirement, cost: int) -> bool:
        status = self._normalize(account.raw.get("status"))
        if status in {"禁用", "异常", "限流", "disabled", "error", "rate_limited"}:
            return False
        if requirement.lane in {Lane.IMAGE_GENERATION, Lane.IMAGE_EDIT} and account.quota <= 0:
            return False
        if requirement.plan_type and account.plan_type != self._normalize(requirement.plan_type):
            return False
        if requirement.source_type and account.source_type != self._normalize(requirement.source_type):
            return False
        breaker = account.breaker(requirement.lane)
        if breaker.state == "open":
            return False
        if breaker.state == "half_open" and breaker.probe_inflight > 0:
            return False
        lane_settings = self.settings.lanes[requirement.lane]
        if account.lane_inflight.get(requirement.lane, 0) + cost > lane_settings.per_account_concurrency:
            return False
        if requirement.lane in {Lane.IMAGE_GENERATION, Lane.IMAGE_EDIT}:
            if account.image_total_inflight + cost > self.settings.image_total_per_account_concurrency:
                return False
        return True

    def _publish_locked(self, account: _AccountRuntime) -> None:
        if not self._base_eligible_locked(account):
            return
        for key in self._bucket_keys_for_account(account):
            self._sequence += 1
            heap = self._heaps.setdefault(key, [])
            heapq.heappush(
                heap,
                (
                    self._score_locked(account, self._lane_for_bucket(key)),
                    self._sequence,
                    account.account_key,
                    account.revision,
                ),
            )

    def _base_eligible_locked(self, account: _AccountRuntime) -> bool:
        status = self._normalize(account.raw.get("status"))
        return status not in {"禁用", "异常", "限流", "disabled", "error", "rate_limited"}

    def _bucket_keys_for_account(self, account: _AccountRuntime) -> list[tuple[object, ...]]:
        keys: list[tuple[object, ...]] = [(Lane.TEXT.value, account.account_type)]
        for lane in (Lane.IMAGE_GENERATION, Lane.IMAGE_EDIT):
            for plan_type in (account.plan_type, None):
                for source_type in (account.source_type, None):
                    keys.append((lane.value, plan_type, source_type))
        return keys

    def _heap_keys_for_requirement(self, requirement: AccountRequirement) -> list[tuple[object, ...]]:
        if requirement.lane == Lane.TEXT:
            route_types = self._route_account_types(requirement.model)
            return [(Lane.TEXT.value, item) for item in route_types]
        plan_type = self._normalize(requirement.plan_type) if requirement.plan_type else None
        source_type = self._normalize(requirement.source_type) if requirement.source_type else None
        return [
            (requirement.lane.value, plan_type, source_type),
            (requirement.lane.value, plan_type, None),
            (requirement.lane.value, None, source_type),
            (requirement.lane.value, None, None),
        ]

    def _route_account_types(self, model: str) -> set[str]:
        if not model or model == "auto":
            return {account.account_type for account in self._accounts.values()}
        try:
            route = self._model_router(model)
            account_types = getattr(route, "account_types", route)
            return {self._normalize(item) for item in account_types if self._normalize(item)}
        except Exception:
            return {account.account_type for account in self._accounts.values()}

    def _lane_for_bucket(self, key: tuple[object, ...]) -> Lane:
        return Lane.TEXT if key[0] == Lane.TEXT.value else Lane(str(key[0]))

    def _score_locked(self, account: _AccountRuntime, lane: Lane) -> float:
        lane_settings = self.settings.lanes[lane]
        lane_load = account.lane_inflight.get(lane, 0) / max(1, lane_settings.per_account_concurrency)
        total_load = account.image_total_inflight / max(
            1,
            self.settings.image_total_per_account_concurrency,
        ) if lane in {Lane.IMAGE_GENERATION, Lane.IMAGE_EDIT} else lane_load
        latency = min(1.0, account.ewma_latency_ms.get(lane, 0.0) / 10000.0)
        failures = min(1.0, account.failures.get(lane, 0) / 20.0)
        jitter = (hash(account.account_key) & 1023) / 1023.0 * 1e-6
        return 0.45 * lane_load + 0.25 * total_load + 0.20 * latency + 0.10 * failures + jitter

    def _record_outcome_locked(
        self,
        account: _AccountRuntime,
        lane: Lane,
        outcome: Outcome,
        latency_ms: float,
    ) -> None:
        if outcome == Outcome.SUCCESS:
            previous = account.ewma_latency_ms.get(lane, 0.0)
            account.ewma_latency_ms[lane] = float(latency_ms) if previous <= 0 else previous * 0.8 + float(latency_ms) * 0.2
            account.successes[lane] = account.successes.get(lane, 0) + 1
            account.consecutive_failures[lane] = 0
            breaker = account.breaker(lane)
            if breaker.state == "half_open":
                breaker.state = "closed"
                breaker.cooldown_until = 0.0
            return
        account.failures[lane] = account.failures.get(lane, 0) + 1
        account.consecutive_failures[lane] = account.consecutive_failures.get(lane, 0) + 1
        if outcome == Outcome.RATE_LIMIT:
            self._open_breaker_locked(account, lane, 2.0)
        elif outcome in {Outcome.AUTH_ERROR, Outcome.SERVER_ERROR, Outcome.NETWORK_ERROR} and account.consecutive_failures[lane] >= 3:
            self._open_breaker_locked(account, lane, 5.0)

    def _open_breaker_locked(self, account: _AccountRuntime, lane: Lane, cooldown_seconds: float) -> None:
        breaker = account.breaker(lane)
        breaker.state = "open"
        breaker.cooldown_until = max(breaker.cooldown_until, self._clock() + cooldown_seconds)
        breaker.probe_inflight = 0

    def _has_active_lease(self, account_key: str) -> bool:
        return any(lease.account_key == account_key for lease in self._leases.values())

    @staticmethod
    def _normalize(value: object) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _quota(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
