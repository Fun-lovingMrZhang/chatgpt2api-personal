from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from services.config import ConfigStore
from services.scheduler.account_pool import AccountPool
from services.scheduler.errors import QueueFullError, SchedulerDeadlineError
from services.scheduler.lane import LaneScheduler
from services.scheduler.models import AccountRequirement, Lane


def make_settings(*, queue_max_items: int = 1, queue_max_cost: int = 8, global_concurrency: int = 2):
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "config.json"
        path.write_text('{"auth-key":"test-auth"}', encoding="utf-8")
        settings = ConfigStore(path).get_scheduler_settings()
    lanes = dict(settings.lanes)
    lanes[Lane.TEXT] = replace(
        lanes[Lane.TEXT],
        worker_count=1,
        global_concurrency=global_concurrency,
        per_account_concurrency=global_concurrency,
        queue_max_items=queue_max_items,
        queue_max_cost=queue_max_cost,
    )
    return replace(settings, lanes=lanes)


def make_pool(settings):
    return AccountPool(
        [
            {
                "access_token": "token-1",
                "type": "Free",
                "status": "正常",
                "source_type": "web",
                "quota": 5,
            },
            {
                "access_token": "token-2",
                "type": "Free",
                "status": "正常",
                "source_type": "web",
                "quota": 5,
            },
        ],
        settings,
    )


class LaneSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_full_does_not_start_a_thread_for_rejected_item(self) -> None:
        settings = make_settings(queue_max_items=1)
        pool = make_pool(settings)
        lane = LaneScheduler(Lane.TEXT, settings, pool)
        requirement = AccountRequirement(Lane.TEXT, "auto")
        started = threading.Event()
        release = threading.Event()

        def blocking_job(lease, cancel_event):
            started.set()
            release.wait(2)
            return lease.access_token

        first = asyncio.create_task(lane.submit(blocking_job, requirement))
        await asyncio.to_thread(started.wait, 1)
        second = asyncio.create_task(lane.submit(blocking_job, requirement))
        await asyncio.sleep(0)

        with self.assertRaises(QueueFullError):
            await lane.submit(blocking_job, requirement)

        self.assertEqual(lane.metrics_snapshot()["rejected"], 1)
        release.set()
        self.assertTrue(await first)
        self.assertTrue(await second)
        await lane.wait_until_idle()
        await lane.stop()

    async def test_cost_four_consumes_four_global_units(self) -> None:
        settings = make_settings(global_concurrency=4, queue_max_cost=4)
        pool = make_pool(settings)
        lane = LaneScheduler(Lane.TEXT, settings, pool)
        requirement = AccountRequirement(Lane.TEXT, "auto")
        started = threading.Event()
        release = threading.Event()

        def blocking_job(lease, cancel_event):
            started.set()
            release.wait(2)
            return lease.access_token

        task = asyncio.create_task(lane.submit(blocking_job, requirement, cost=4))
        await asyncio.to_thread(started.wait, 1)
        with self.assertRaises(QueueFullError):
            await lane.submit(blocking_job, requirement, cost=1)
        self.assertEqual(lane.metrics_snapshot()["active_cost"], 4)
        release.set()
        await task
        await lane.stop()

    async def test_stream_close_releases_lease(self) -> None:
        settings = make_settings(queue_max_items=2)
        pool = make_pool(settings)
        lane = LaneScheduler(Lane.TEXT, settings, pool, stream_queue_size=1)
        requirement = AccountRequirement(Lane.TEXT, "auto")
        first_item = threading.Event()

        def streaming_job(lease, cancel_event):
            for index in range(100):
                first_item.set()
                yield index
                if cancel_event.is_set():
                    return

        stream = await lane.submit_stream(streaming_job, requirement)
        self.assertEqual(await anext(stream), 0)
        await stream.aclose()
        await lane.wait_until_idle()
        self.assertEqual(pool.inflight("token-1", Lane.TEXT), 0)
        self.assertEqual(pool.inflight("token-2", Lane.TEXT), 0)
        await lane.stop()

    async def test_deadline_releases_lease_after_worker_finishes(self) -> None:
        settings = make_settings(queue_max_items=2)
        pool = make_pool(settings)
        lane = LaneScheduler(Lane.TEXT, settings, pool)
        requirement = AccountRequirement(Lane.TEXT, "auto")
        release = threading.Event()

        def slow_job(lease, cancel_event):
            release.wait(2)
            return lease.access_token

        task = asyncio.create_task(
            lane.submit(
                slow_job,
                requirement,
                deadline=lane._clock() + 0.02,
            )
        )
        with self.assertRaises(SchedulerDeadlineError):
            await task
        self.assertEqual(
            sum(pool.inflight(token, Lane.TEXT) for token in ("token-1", "token-2")),
            1,
        )
        release.set()
        await lane.wait_until_idle()
        self.assertEqual(
            sum(pool.inflight(token, Lane.TEXT) for token in ("token-1", "token-2")),
            0,
        )
        await lane.stop()


if __name__ == "__main__":
    unittest.main()
