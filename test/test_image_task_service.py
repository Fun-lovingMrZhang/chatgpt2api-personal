from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services.image_task_service import ImageTaskService, _collect_image_urls


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None) -> ImageTaskService:
        return ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
        )

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertNotIn("data", result["items"][0])

    def test_success_task_keeps_result_in_memory_without_persisting_image_data(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            encoded = base64.b64encode(b"generated-image").decode("ascii")

            def handler(_payload):
                return {"data": [{"b64_json": encoded, "revised_prompt": "cat"}]}

            service = self.make_service(path, handler)
            service.submit_generation(
                OWNER,
                client_task_id="memory-only-result",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            current = wait_for_task(service, OWNER, "memory-only-result", "success")
            self.assertEqual(current["data"][0]["b64_json"], encoded)

            persisted = json.loads(path.read_text(encoding="utf-8"))
            persisted_task = persisted["tasks"][0]
            self.assertNotIn("data", persisted_task)

            reloaded = self.make_service(path, handler)
            reloaded_task = reloaded.list_tasks(OWNER, ["memory-only-result"])["items"][0]
            self.assertEqual(reloaded_task["status"], "success")
            self.assertNotIn("data", reloaded_task)

    def test_legacy_task_data_survives_metadata_save(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            legacy_data = [{"url": "http://local.test/images/legacy.png"}]
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "legacy-task",
                                "owner_id": OWNER["id"],
                                "status": "success",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": now,
                                "updated_at": now,
                                "data": legacy_data,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            service._update_task("owner-1:legacy-task", progress="metadata-only-update")
            service._update_task(
                "owner-1:legacy-task",
                data=[{"url": "data:image/png;base64,new-result"}],
            )

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["tasks"][0]["data"], legacy_data)

    def test_collect_image_urls_skips_inline_data_urls(self):
        self.assertEqual(
            _collect_image_urls(
                [
                    {"url": "data:image/png;base64,c2VjcmV0"},
                    {"url": "https://files.example.test/image.png"},
                ]
            ),
            ["https://files.example.test/image.png"],
        )

    def test_resume_poll_passes_created_as_named_formatter_argument(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            key = "owner-1:resume-task"
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            service._tasks[key] = {
                "id": "resume-task",
                "owner_id": OWNER["id"],
                "status": "running",
                "mode": "generate",
                "model": "gpt-image-2",
                "quality": "auto",
                "size": "",
                "created_at": now,
                "updated_at": now,
            }
            backend = mock.Mock()
            backend._poll_image_results.return_value = (["file-1"], [])
            backend.resolve_conversation_image_urls.return_value = ["https://files.example.test/image.png"]
            backend.download_image_bytes.return_value = [b"image-bytes"]

            with (
                mock.patch("services.image_task_service.config.proxy_url", "", create=True),
                mock.patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend),
                mock.patch(
                    "services.protocol.conversation.format_image_result",
                    return_value={"data": [{"b64_json": "aW1hZ2UtYnl0ZXM="}]},
                ) as format_result,
            ):
                service._run_resume_poll(
                    key,
                    "conversation-1",
                    1.0,
                    OWNER,
                    "generate",
                    "gpt-image-2",
                )

            self.assertEqual(service._tasks[key].get("status"), "success", service._tasks[key])
            self.assertIn("created", format_result.call_args.kwargs)
            self.assertIsInstance(format_result.call_args.kwargs["created"], int)
            self.assertNotIn("message", format_result.call_args.kwargs)

    def test_startup_marks_unfinished_tasks_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))


if __name__ == "__main__":
    unittest.main()
