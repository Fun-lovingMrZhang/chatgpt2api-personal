from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.log_store import SQLiteLogStore


class SQLiteLogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "logs.db"
        self.legacy_path = self.root / "logs.jsonl"
        self.store = SQLiteLogStore(self.db_path, self.legacy_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _raw_value(self, sql: str, parameter: object = None):
        connection = sqlite3.connect(self.db_path)
        try:
            if parameter is None:
                row = connection.execute(sql).fetchone()
            else:
                row = connection.execute(sql, (parameter,)).fetchone()
            return None if row is None else row[0]
        finally:
            connection.close()

    def test_initialize_creates_schema_and_pragmas(self):
        self.store.initialize()

        table_name = self._raw_value(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'logs'"
        )
        self.assertEqual(table_name, "logs")
        self.assertEqual(str(self._raw_value("PRAGMA journal_mode")).lower(), "wal")
        foreign_keys = self.store._connection().execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(foreign_keys, 1)

    def test_add_round_trips_public_shape_and_detail(self):
        self.store.initialize()

        item = self.store.add("call", "调用完成", {"status": 200})

        self.assertTrue(item["id"])
        self.assertRegex(item["time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(item["type"], "call")
        self.assertEqual(item["summary"], "调用完成")
        self.assertEqual(item["detail"], {"status": 200})

    def test_query_returns_newest_first_and_has_total_and_has_more(self):
        self.store.initialize()
        for index in range(3):
            self.store.add("call", f"call-{index}", {"index": index})

        result = self.store.query(page=1, page_size=2)

        self.assertEqual(result["total"], 3)
        self.assertTrue(result["has_more"])
        self.assertEqual(
            [item["summary"] for item in result["items"]],
            ["call-2", "call-1"],
        )

    def test_type_and_date_filters_are_applied_by_sql(self):
        self.store.initialize()
        self.store.add("call", "call", {})
        self.store.add("account", "account", {})

        result = self.store.query(
            type_filter="account",
            start_date="2099-01-01",
            end_date="2099-01-31",
        )

        self.assertEqual(result["total"], 0)

    def test_delete_by_ids_is_transactional_and_idempotent(self):
        self.store.initialize()
        item = self.store.add("call", "delete me", {})

        self.assertEqual(self.store.delete([item["id"], "missing-id"]), 1)
        self.assertEqual(self.store.delete([item["id"]]), 0)


if __name__ == "__main__":
    unittest.main()
