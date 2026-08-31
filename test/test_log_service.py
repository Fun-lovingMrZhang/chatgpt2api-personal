from __future__ import annotations

import unittest
from unittest import mock

from services.log_service import LoggedCall


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


if __name__ == "__main__":
    unittest.main()
