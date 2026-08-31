from __future__ import annotations

import base64
import unittest
from unittest import mock

from services.protocol.conversation import format_image_result


IMAGE_B64 = base64.b64encode(b"image-bytes").decode("ascii")


class ImageResultFormatTests(unittest.TestCase):
    @mock.patch("services.image_storage_service.ImageStorageService.save")
    def test_b64_json_returns_image_data_without_storage_url(self, save_image):
        result = format_image_result(
            [{"b64_json": IMAGE_B64, "revised_prompt": "draw a poster"}],
            "poster",
            "b64_json",
            created=123,
        )

        self.assertEqual(result, {
            "created": 123,
            "data": [{
                "b64_json": IMAGE_B64,
                "revised_prompt": "draw a poster",
            }],
        })
        save_image.assert_not_called()

    @mock.patch("services.image_storage_service.ImageStorageService.save")
    def test_url_returns_data_url_without_storage_file(self, save_image):
        result = format_image_result(
            [{"b64_json": IMAGE_B64}],
            "poster",
            "url",
            created=456,
        )

        self.assertEqual(result, {
            "created": 456,
            "data": [{
                "url": f"data:image/png;base64,{IMAGE_B64}",
                "revised_prompt": "poster",
            }],
        })
        save_image.assert_not_called()


if __name__ == "__main__":
    unittest.main()
