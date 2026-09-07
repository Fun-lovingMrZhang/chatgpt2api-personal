from __future__ import annotations

import asyncio
import base64
import tempfile
import threading
import time
import unittest
from pathlib import Path

from services.image_task_service import ImageTaskService
from services.scheduler.errors import QueueFullError
from services.scheduler.models import AccountRequirement, Lane
from services.scheduler.state_store import SchedulerStateStore


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}


class FakeScheduler:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.requirements: list[AccountRequirement] = []

    def classify_image_generation(self, payload):
        requirement = AccountRequirement(Lane.IMAGE_GENERATION, str(payload.get("model")), cost=1)
        self.requirements.append(requirement)
        return requirement

    async def admit_background(self, requirement, _work):
        if self.reject:
            raise QueueFullError("scheduler queue is full")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.set_result(None)
        return future


class SchedulerAsyncTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_submit_is_admitted_without_creating_a_task_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            scheduler = FakeScheduler()
            service = ImageTaskService(
                Path(tmp_dir) / "tasks.json",
                generation_handler=lambda _payload: {"data": []},
                scheduler_service_instance=scheduler,
            )
            before = {thread.ident for thread in threading.enumerate()}
            result = await service.submit_generation_async(
                OWNER,
                client_task_id="queued-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
            )
            after = {thread.ident for thread in threading.enumerate()}

            self.assertEqual(result["status"], "queued")
            self.assertEqual(after - before, set())
            self.assertEqual(len(scheduler.requirements), 1)

    async def test_scheduler_rejection_removes_unadmitted_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = ImageTaskService(
                Path(tmp_dir) / "tasks.json",
                scheduler_service_instance=FakeScheduler(reject=True),
            )
            with self.assertRaises(QueueFullError):
                await service.submit_generation_async(
                    OWNER,
                    client_task_id="rejected-task",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                )
            self.assertEqual(service.list_tasks(OWNER, ["rejected-task"])["items"], [])

    def test_sqlite_task_metadata_excludes_image_result_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = SchedulerStateStore(Path(tmp_dir) / "scheduler.db")
            encoded = base64.b64encode(b"generated-image").decode("ascii")
            service = ImageTaskService(
                Path(tmp_dir) / "legacy-image-tasks.json",
                generation_handler=lambda _payload: {"data": [{"b64_json": encoded}]},
                state_store=store,
            )
            service.submit_generation(
                OWNER,
                client_task_id="sqlite-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
            )
            for _ in range(100):
                task = service.list_tasks(OWNER, ["sqlite-task"])["items"][0]
                if task.get("status") == "success":
                    break
                time.sleep(0.01)
            record = store.get_task("owner-1:sqlite-task")
            self.assertIsNotNone(record)
            self.assertIsNone(record["result"])
            self.assertNotIn("b64_json", str(record["metadata"]))


if __name__ == "__main__":
    unittest.main()
