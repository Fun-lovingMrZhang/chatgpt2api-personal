from __future__ import annotations

import asyncio
import unittest
import tempfile
from threading import Event
from pathlib import Path
from unittest import mock

from services.log_service import LogService, LoggedCall
from services.scheduler.errors import QueueFullError


class LoggedCallTests(unittest.TestCase):
    def test_dict_response_strips_scheduler_internal_fields(self):
        logged_call = LoggedCall(
            identity={"id": "owner-1"},
            endpoint="/v1/images/generations",
            model="gpt-image-2",
            summary="文生图",
        )

        with mock.patch("services.log_service.log_service.add"):
            result = asyncio.run(
                logged_call.run(
                    lambda: {
                        "data": [{"url": "https://files.example.test/image.png"}],
                        "_account_email": "account@example.test",
                        "_conversation_id": "conversation-1",
                    }
                )
            )

        self.assertNotIn("_account_email", result)
        self.assertNotIn("_conversation_id", result)

    def test_stream_scheduler_error_keeps_status_and_retry_after(self):
        logged_call = LoggedCall(
            identity={"id": "owner-1"},
            endpoint="/v1/chat/completions",
            model="test-model",
            summary="文本生成",
        )

        async def runner(_handler, *_args, streaming=False):
            self.assertTrue(streaming)

            async def stream():
                raise QueueFullError("scheduler queue is full", retry_after_seconds=7)
                yield None

            return stream()

        with mock.patch("services.log_service.log_service.add"):
            response = asyncio.run(
                logged_call.run(lambda: None, scheduler_runner=runner, streaming=True)
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "7")

    @mock.patch("services.log_service.log_service.add")
    def test_inline_data_urls_are_not_written_to_call_logs(self, add_log):
        logged_call = LoggedCall(
            identity={"id": "owner-1"},
            endpoint="/v1/images/generations",
            model="gpt-image-2",
            summary="文生图",
        )

        logged_call.log(
            "调用完成",
            {
                "data": [
                    {"url": "data:image/png;base64,c2VjcmV0"},
                    {"url": "https://files.example.test/image.png"},
                ]
            },
        )

        detail = add_log.call_args.args[2]
        self.assertEqual(detail["urls"], ["https://files.example.test/image.png"])

    @mock.patch("services.log_service.log_service.add")
    def test_logged_call_omits_request_text_and_response_payload(self, add_log):
        logged_call = LoggedCall(
            identity={"id": "owner-1"},
            endpoint="/v1/chat/completions",
            model="test-model",
            summary="文本生成",
            request_text="secret prompt that must not be stored",
            request_shape={"input_image_parts": 1},
        )

        logged_call.log(
            "调用完成",
            {
                "status": 200,
                "request_text": "secret",
                "data": "large response payload",
                "choices": [{"message": {"content": "generated text"}}],
            },
        )

        detail = add_log.call_args.args[2]
        self.assertNotIn("request_text", detail)
        self.assertNotIn("data", detail)
        self.assertNotIn("choices", detail)
        self.assertEqual(detail["request_shape"], {"input_image_parts": 1})


class LogServiceTests(unittest.TestCase):
    def test_sqlite_facade_supports_query_list_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service = LogService(root / "logs.db", root / "logs.jsonl")
            service.initialize()

            item = service.add("call", "stored", {"status": 200})
            result = service.query(page=1, page_size=20)

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["id"], item["id"])
            self.assertEqual(result["items"][0]["detail"], {"status": 200})
            self.assertEqual(service.list(type="call", limit=1)[0]["id"], item["id"])
            self.assertEqual(service.delete([item["id"]]), 1)
            self.assertEqual(service.query(page=1, page_size=20)["total"], 0)

    def test_add_drops_request_and_response_payload_fields_before_storage(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service = LogService(root / "logs.db", root / "logs.jsonl")

            item = service.add(
                "call",
                "payload policy",
                {
                    "request_text": "secret prompt",
                    "data": "large response",
                    "status": "success",
                },
            )

            self.assertIsNotNone(item)
            stored = service.query(page=1, page_size=20)["items"][0]["detail"]
            self.assertEqual(stored, {"status": "success"})

    def test_retention_scheduler_runs_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            service = LogService(root / "logs.db", root / "logs.jsonl")
            stopped = Event()
            ran = Event()

            def cleanup(_retention_days: int) -> int:
                ran.set()
                return 0

            with mock.patch.object(service, "cleanup_expired", side_effect=cleanup):
                thread = service.start_retention_scheduler(
                    stopped,
                    lambda: 30,
                    interval_seconds=0.01,
                )
                self.assertTrue(ran.wait(1))
                stopped.set()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
