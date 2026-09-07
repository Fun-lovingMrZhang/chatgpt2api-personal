from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

from services.log_service import LogService, LoggedCall


class LoggedCallTests(unittest.TestCase):
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
            self.assertEqual(service.list(type="call", limit=1)[0]["id"], item["id"])
            self.assertEqual(service.delete([item["id"]]), 1)
            self.assertEqual(service.query(page=1, page_size=20)["total"], 0)


if __name__ == "__main__":
    unittest.main()
