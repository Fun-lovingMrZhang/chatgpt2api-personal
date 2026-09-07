from __future__ import annotations

import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.quota_service import (
    AuthQuotaStore,
    QuotaExceededError,
    QuotaLimitError,
    QuotaService,
)


def user_identity(key_id: str = "user-1") -> dict[str, object]:
    return {"id": key_id, "name": "普通用户", "role": "user"}


def admin_identity() -> dict[str, object]:
    return {"id": "admin", "name": "管理员", "role": "admin"}


def make_service(root: Path) -> QuotaService:
    service = QuotaService(AuthQuotaStore(root / "auth_quota.db"))
    service.initialize()
    return service


def reserve_text(service: QuotaService, index: int):
    try:
        return service.reserve(
            user_identity(),
            "text",
            1,
            request_id=f"request-{index}",
            endpoint="/v1/chat/completions",
        )
    except QuotaExceededError:
        return None


class QuotaServiceTests(unittest.TestCase):
    def test_new_user_has_zero_quota_and_admin_bypasses(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = make_service(Path(tmp_dir))
            service.ensure_key("user-1")

            quota = service.get_quota("user-1")
            self.assertEqual(quota["text_remaining"], 0)
            with self.assertRaises(QuotaExceededError):
                service.reserve(
                    user_identity(),
                    "text",
                    1,
                    request_id="r-1",
                    endpoint="/v1/chat/completions",
                )

            reservation = service.reserve(
                admin_identity(),
                "image",
                4,
                request_id="admin-1",
                endpoint="/v1/images/generations",
            )
            self.assertTrue(reservation.admin_bypass)
            self.assertIsNone(service.get_quota("admin"))

    def test_reserve_commit_and_release_keep_remaining_consistent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = make_service(Path(tmp_dir))
            service.set_limits("user-1", text_limit=2, image_limit=4)

            text = service.reserve(
                user_identity(),
                "text",
                1,
                request_id="t-1",
                endpoint="/v1/chat/completions",
            )
            image = service.reserve(
                user_identity(),
                "image",
                4,
                request_id="i-1",
                endpoint="/v1/images/generations",
            )
            quota = service.get_quota("user-1")
            self.assertEqual(
                (quota["text_used"], quota["text_reserved"], quota["text_remaining"]),
                (0, 1, 1),
            )
            self.assertEqual(
                (quota["image_used"], quota["image_reserved"], quota["image_remaining"]),
                (0, 4, 0),
            )

            self.assertTrue(service.commit(text.reservation_id, 1))
            self.assertTrue(service.commit(image.reservation_id, 3))
            self.assertFalse(service.release(image.reservation_id))
            self.assertFalse(service.commit(image.reservation_id, 1))
            quota = service.get_quota("user-1")
            self.assertEqual(
                (quota["text_used"], quota["text_reserved"], quota["text_remaining"]),
                (1, 0, 1),
            )
            self.assertEqual(
                (quota["image_used"], quota["image_reserved"], quota["image_remaining"]),
                (3, 0, 1),
            )

    def test_limit_update_cannot_drop_below_used_or_reserved(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = make_service(Path(tmp_dir))
            service.set_limits("user-1", text_limit=3, image_limit=3)
            reservation = service.reserve(
                user_identity(),
                "text",
                2,
                request_id="r-1",
                endpoint="/v1/chat/completions",
            )
            service.commit(reservation.reservation_id, 1)
            with self.assertRaises(QuotaLimitError):
                service.set_limits("user-1", text_limit=0, image_limit=3)

            reserved = service.reserve(
                user_identity(),
                "text",
                1,
                request_id="r-2",
                endpoint="/v1/chat/completions",
            )
            self.assertIsNotNone(reserved)
            with self.assertRaises(QuotaLimitError):
                service.set_limits("user-1", text_limit=1, image_limit=3)

    def test_expired_reservation_is_released_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = make_service(Path(tmp_dir))
            service.set_limits("user-1", text_limit=1, image_limit=1)
            reservation = service.reserve(
                user_identity(),
                "text",
                1,
                request_id="r-1",
                endpoint="/v1/chat/completions",
                expires_at=time.time() - 1,
            )
            self.assertEqual(service.recover_expired(now=time.time()), 1)
            self.assertEqual(service.recover_expired(now=time.time()), 0)
            self.assertEqual(service.get_quota("user-1")["text_reserved"], 0)
            self.assertEqual(service.get_reservation(reservation.reservation_id)["status"], "expired")

    def test_active_reservation_can_be_renewed_but_expired_one_cannot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = make_service(Path(tmp_dir))
            service.set_limits("user-1", text_limit=2, image_limit=0)
            active = service.reserve(
                user_identity(),
                "text",
                1,
                request_id="active",
                endpoint="/v1/chat/completions",
                expires_at=time.time() + 30,
            )
            old_expiry = service.get_reservation(active.reservation_id)["expires_at"]
            self.assertTrue(service.renew(active.reservation_id, expires_at=time.time() + 120))
            self.assertGreater(service.get_reservation(active.reservation_id)["expires_at"], old_expiry)

            expired = service.reserve(
                user_identity(),
                "text",
                1,
                request_id="expired",
                endpoint="/v1/chat/completions",
                expires_at=time.time() - 1,
            )
            self.assertFalse(service.renew(expired.reservation_id, expires_at=time.time() + 120))

    def test_concurrent_reservations_never_exceed_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = make_service(Path(tmp_dir))
            service.set_limits("user-1", text_limit=8, image_limit=0)
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(lambda index: reserve_text(service, index), range(16)))
            self.assertEqual(sum(result is not None for result in results), 8)
            self.assertLessEqual(service.get_quota("user-1")["text_reserved"], 8)


if __name__ == "__main__":
    unittest.main()
