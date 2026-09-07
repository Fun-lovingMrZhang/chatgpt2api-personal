from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from services.config import ConfigStore
from services.scheduler.account_pool import AccountPool
from services.scheduler.models import AccountRequirement, Lane, Outcome


def fake_router(model: str) -> set[str]:
    if model == "pro-only":
        return {"pro"}
    return {"free", "pro", "team"}


def make_settings():
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "config.json"
        path.write_text('{"auth-key":"test-auth"}', encoding="utf-8")
        settings = ConfigStore(path).get_scheduler_settings()
    lanes = dict(settings.lanes)
    lanes[Lane.TEXT] = replace(lanes[Lane.TEXT], per_account_concurrency=2)
    lanes[Lane.IMAGE_GENERATION] = replace(lanes[Lane.IMAGE_GENERATION], per_account_concurrency=2)
    lanes[Lane.IMAGE_EDIT] = replace(lanes[Lane.IMAGE_EDIT], per_account_concurrency=2)
    return replace(settings, lanes=lanes, image_total_per_account_concurrency=3)


def make_accounts(*names: str) -> list[dict[str, object]]:
    return [
        {
            "access_token": name,
            "type": "Pro" if name.startswith("pro") else "Free",
            "status": "正常",
            "source_type": "web",
            "quota": 5,
        }
        for name in names
    ]


class AccountPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.text_requirement = AccountRequirement(Lane.TEXT, "auto", cost=1)
        self.pro_requirement = AccountRequirement(Lane.TEXT, "pro-only", cost=1)
        self.image_requirement = AccountRequirement(
            Lane.IMAGE_GENERATION,
            "gpt-image-2",
            source_type="web",
            cost=1,
        )

    def test_selects_only_accounts_matching_text_model(self) -> None:
        pool = AccountPool(make_accounts("free", "pro-token"), self.settings, model_router=fake_router)

        lease = pool.try_acquire(self.pro_requirement, deadline=pool.clock() + 5)

        self.assertIsNotNone(lease)
        self.assertEqual(lease.account_key, "pro-token")

    def test_selection_does_not_scan_all_accounts(self) -> None:
        pool = AccountPool(make_accounts(*(f"free-{index}" for index in range(2000))), self.settings, model_router=fake_router)
        pool.reset_selection_probe_count()

        for _ in range(100):
            lease = pool.try_acquire(self.text_requirement, deadline=pool.clock() + 5)
            self.assertIsNotNone(lease)
            pool.release(lease, outcome=Outcome.SUCCESS, latency_ms=10)

        self.assertLessEqual(pool.selection_probe_count, 500)

    def test_account_capacity_is_enforced(self) -> None:
        pool = AccountPool(make_accounts("one"), self.settings, model_router=fake_router)
        leases = [
            pool.try_acquire(self.image_requirement, deadline=pool.clock() + 5)
            for _ in range(2)
        ]

        self.assertTrue(all(leases))
        self.assertIsNone(pool.try_acquire(self.image_requirement, deadline=pool.clock() + 5))
        pool.release(leases[0], outcome=Outcome.SUCCESS, latency_ms=20)
        self.assertIsNotNone(pool.try_acquire(self.image_requirement, deadline=pool.clock() + 5))

    def test_image_cost_consumes_total_account_capacity(self) -> None:
        settings = replace(
            self.settings,
            lanes={
                **self.settings.lanes,
                Lane.IMAGE_GENERATION: replace(
                    self.settings.lanes[Lane.IMAGE_GENERATION],
                    per_account_concurrency=4,
                ),
            },
            image_total_per_account_concurrency=4,
        )
        pool = AccountPool(make_accounts("one"), settings, model_router=fake_router)
        requirement = replace(self.image_requirement, cost=4)

        lease = pool.try_acquire(requirement, deadline=pool.clock() + 5)

        self.assertIsNotNone(lease)
        self.assertEqual(pool.inflight("one", Lane.IMAGE_GENERATION), 4)
        self.assertIsNone(pool.try_acquire(self.image_requirement, deadline=pool.clock() + 5))

    def test_image_breaker_does_not_remove_text_capability(self) -> None:
        pool = AccountPool(make_accounts("shared"), self.settings, model_router=fake_router)
        image_lease = pool.try_acquire(self.image_requirement, deadline=pool.clock() + 5)
        self.assertIsNotNone(image_lease)
        pool.release(image_lease, outcome=Outcome.SERVER_ERROR, latency_ms=1000)
        pool.open_breaker("shared", Lane.IMAGE_GENERATION)

        self.assertIsNotNone(pool.try_acquire(self.text_requirement, deadline=pool.clock() + 5))
        self.assertIsNone(pool.try_acquire(self.image_requirement, deadline=pool.clock() + 5))

    def test_excluded_account_is_not_reused(self) -> None:
        pool = AccountPool(make_accounts("one", "two"), self.settings, model_router=fake_router)
        lease = pool.try_acquire(
            self.text_requirement,
            deadline=pool.clock() + 5,
            excluded={"one"},
        )

        self.assertIsNotNone(lease)
        self.assertEqual(lease.account_key, "two")

    def test_duplicate_release_is_idempotent(self) -> None:
        pool = AccountPool(make_accounts("one"), self.settings, model_router=fake_router)
        lease = pool.try_acquire(self.text_requirement, deadline=pool.clock() + 5)
        self.assertIsNotNone(lease)

        pool.release(lease, outcome=Outcome.SUCCESS, latency_ms=10)
        pool.release(lease, outcome=Outcome.SUCCESS, latency_ms=10)

        self.assertEqual(pool.inflight("one", Lane.TEXT), 0)


if __name__ == "__main__":
    unittest.main()
