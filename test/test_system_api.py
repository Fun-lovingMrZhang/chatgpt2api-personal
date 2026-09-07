from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.system as system_module


AUTH_HEADERS = {"Authorization": "Bearer test-admin"}


class FakeLogService:
    def __init__(self) -> None:
        self.query_calls: list[tuple[tuple, dict]] = []
        self.delete_calls: list[list[str]] = []

    def list(self, **_kwargs):
        return []

    def query(self, *args, **kwargs):
        self.query_calls.append((args, kwargs))
        return {
            "items": [{"id": "log-1", "type": "call", "summary": "ok", "detail": {}, "time": "2026-09-07 10:00:00"}],
            "total": 12,
            "page": 2,
            "page_size": 5,
            "has_more": True,
        }

    def delete(self, ids: list[str]) -> int:
        self.delete_calls.append(ids)
        return len(ids)


class SystemLogApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log_service = FakeLogService()
        self.threadpool_calls: list[tuple[object, tuple, dict]] = []

        async def fake_run_in_threadpool(func, *args, **kwargs):
            self.threadpool_calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        self.patchers = [
            mock.patch.object(system_module, "log_service", self.log_service),
            mock.patch.object(system_module, "require_admin", lambda _authorization: {"role": "admin"}),
            mock.patch.object(system_module, "run_in_threadpool", side_effect=fake_run_in_threadpool),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        app = FastAPI()
        app.include_router(system_module.create_router("test-version"))
        self.client = TestClient(app)

    def test_get_logs_uses_server_pagination_and_threadpool(self) -> None:
        response = self.client.get(
            "/api/logs?type=call&start_date=2026-09-01&end_date=2026-09-07&page=2&page_size=5",
            headers=AUTH_HEADERS,
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 12)
        self.assertEqual(
            self.log_service.query_calls,
            [((), {
                "type_filter": "call",
                "start_date": "2026-09-01",
                "end_date": "2026-09-07",
                "page": 2,
                "page_size": 5,
            })],
        )
        self.assertEqual(self.threadpool_calls[0][0], self.log_service.query)

    def test_get_logs_rejects_invalid_pagination(self) -> None:
        for query in ("page=0", "page_size=0", "page_size=101"):
            response = self.client.get(f"/api/logs?{query}", headers=AUTH_HEADERS)
            self.assertEqual(response.status_code, 400, response.text)

    def test_delete_logs_uses_threadpool_and_returns_count(self) -> None:
        response = self.client.post(
            "/api/logs/delete",
            headers=AUTH_HEADERS,
            json={"ids": ["log-1", "log-2"]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"deleted": 2})
        self.assertEqual(self.log_service.delete_calls, [["log-1", "log-2"]])
        self.assertEqual(self.threadpool_calls[0][0], self.log_service.delete)


if __name__ == "__main__":
    unittest.main()
