from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.sub2api_service import (
    Sub2APIConfig,
    _fetch_access_tokens_for_accounts,
)


def make_server(server_id: str = "server-1") -> dict[str, str]:
    return {
        "id": server_id,
        "name": "test",
        "base_url": "https://sub2api.example.test",
        "email": "admin@example.test",
        "password": "secret",
        "api_key": "",
        "group_id": "",
    }


class _FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, account_ids: list[str]):
        self._account_ids = account_ids

    def json(self) -> dict:
        return {
            "code": 0,
            "data": {
                "accounts": [
                    {
                        "id": account_id,
                        "credentials": {"access_token": f"token-{account_id}"},
                    }
                    for account_id in self._account_ids
                ],
            },
        }


class _FakeSession:
    def __init__(self, calls: list[list[str]]):
        self.calls = calls

    def get(self, _url: str, *, params: dict, **_kwargs):
        account_ids = [item for item in params["ids"].split(",") if item]
        self.calls.append(account_ids)
        return _FakeResponse(account_ids)

    def close(self) -> None:
        pass


class Sub2APIConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_old_config_defaults_to_disabled_auto_sync(self):
        path = self.temp_path / "sub2api.json"
        path.write_text(json.dumps([make_server()]), encoding="utf-8")

        config = Sub2APIConfig(path)
        server = config.get_server("server-1")

        self.assertEqual(server["auto_sync_interval_minutes"], 0)
        self.assertEqual(server["auto_sync"]["status"], "disabled")
        self.assertIsNone(server["auto_sync"]["next_run_at"])

    def test_add_and_update_round_trip_auto_sync_interval(self):
        config = Sub2APIConfig(self.temp_path / "sub2api.json")

        added = config.add_server(
            name="test",
            base_url="https://sub2api.example.test",
            email="admin@example.test",
            password="secret",
            api_key="",
            group_id="",
            auto_sync_interval_minutes=5,
        )
        updated = config.update_server(added["id"], {"auto_sync_interval_minutes": 10})

        self.assertEqual(added["auto_sync_interval_minutes"], 5)
        self.assertEqual(updated["auto_sync_interval_minutes"], 10)

    def test_set_auto_sync_state_persists_only_existing_server(self):
        config = Sub2APIConfig(self.temp_path / "sub2api.json")
        added = config.add_server(
            name="test",
            base_url="https://sub2api.example.test",
            email="admin@example.test",
            password="secret",
            api_key="",
            group_id="",
            auto_sync_interval_minutes=5,
        )

        result = config.set_auto_sync_state(added["id"], status="running", last_error="")

        self.assertEqual(result["auto_sync"]["status"], "running")
        self.assertIsNone(config.set_auto_sync_state("missing", status="running"))

    def test_invalid_auto_sync_intervals_normalize_to_zero(self):
        path = self.temp_path / "sub2api.json"
        path.write_text(
            json.dumps([
                {**make_server(str(index)), "auto_sync_interval_minutes": value}
                for index, value in enumerate([-1, 0, 1.5, "invalid", True], start=1)
            ]),
            encoding="utf-8",
        )

        servers = Sub2APIConfig(path).list_servers()

        self.assertEqual([server["auto_sync_interval_minutes"] for server in servers], [0, 0, 0, 0, 0])


class Sub2APITokenExportTests(unittest.TestCase):
    def test_export_batches_ids_to_avoid_unbounded_query_length(self):
        ids = [f"remote-{index}" for index in range(125)]
        calls: list[list[str]] = []
        session = _FakeSession(calls)
        server = {**make_server(), "api_key": "test-api-key"}

        with mock.patch("services.sub2api_service.Session", return_value=session):
            tokens, errors = _fetch_access_tokens_for_accounts(server, ids)

        self.assertEqual(len(calls), 2)
        self.assertEqual([len(batch) for batch in calls], [100, 25])
        self.assertEqual(tokens, [f"token-{account_id}" for account_id in ids])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
