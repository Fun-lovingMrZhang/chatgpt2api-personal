from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


class SQLiteLogStore:
    _DELETE_CHUNK_SIZE = 900

    def __init__(self, db_path: Path, legacy_path: Path):
        self.db_path = Path(db_path)
        self.legacy_path = Path(legacy_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._initialize_lock = threading.RLock()
        self._initialized = False

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self.db_path), timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            connection = self._connection()
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS logs (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        time TEXT NOT NULL,
                        created_ts REAL NOT NULL,
                        type TEXT NOT NULL,
                        summary TEXT NOT NULL DEFAULT '',
                        detail_json TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE INDEX IF NOT EXISTS idx_logs_time
                        ON logs(created_ts DESC, sequence DESC);

                    CREATE INDEX IF NOT EXISTS idx_logs_type_time
                        ON logs(type, created_ts DESC, sequence DESC);

                    CREATE TABLE IF NOT EXISTS log_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
            self._initialized = True

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    @staticmethod
    def _record_time(recorded_at: datetime | None, created_ts: float | None) -> tuple[str, float]:
        value = recorded_at or datetime.now()
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value.strftime("%Y-%m-%d %H:%M:%S"), float(created_ts if created_ts is not None else value.timestamp())

    @staticmethod
    def _parse_date(value: str, *, field_name: str) -> datetime:
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{field_name} must use YYYY-MM-DD format") from exc

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            detail = json.loads(row["detail_json"])
        except (TypeError, json.JSONDecodeError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        return {
            "id": row["id"],
            "time": row["time"],
            "type": row["type"],
            "summary": row["summary"],
            "detail": detail,
        }

    def add(
        self,
        log_type: str,
        summary: str = "",
        detail: dict[str, Any] | None = None,
        *,
        record_id: str | None = None,
        recorded_at: datetime | None = None,
        created_ts: float | None = None,
    ) -> dict[str, Any]:
        self._ensure_initialized()
        time_text, timestamp = self._record_time(recorded_at, created_ts)
        item_id = str(record_id or uuid4().hex)
        payload = detail if isinstance(detail, dict) else {}
        detail_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        connection = self._connection()
        with connection:
            connection.execute(
                """
                INSERT INTO logs (id, time, created_ts, type, summary, detail_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item_id, time_text, timestamp, str(log_type), str(summary), detail_json),
            )
        return {
            "id": item_id,
            "time": time_text,
            "type": str(log_type),
            "summary": str(summary),
            "detail": payload,
        }

    def query(
        self,
        *,
        type_filter: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        self._ensure_initialized()
        if page < 1:
            raise ValueError("page must be at least 1")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")

        conditions: list[str] = []
        parameters: list[Any] = []
        if type_filter:
            conditions.append("type = ?")
            parameters.append(type_filter)
        if start_date:
            start = self._parse_date(start_date, field_name="start_date")
            conditions.append("created_ts >= ?")
            parameters.append(start.timestamp())
        if end_date:
            end = self._parse_date(end_date, field_name="end_date") + timedelta(days=1)
            conditions.append("created_ts < ?")
            parameters.append(end.timestamp())

        where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * page_size
        connection = self._connection()
        connection.execute("BEGIN")
        try:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM logs{where_sql}",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT id, time, type, summary, detail_json
                FROM logs
                {where_sql}
                ORDER BY created_ts DESC, sequence DESC
                LIMIT ? OFFSET ?
                """,
                [*parameters, page_size, offset],
            ).fetchall()
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        items = [self._decode_row(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < total,
        }

    def delete(self, ids: list[str]) -> int:
        target_ids = [str(item or "").strip() for item in ids if str(item or "").strip()]
        if not target_ids:
            return 0
        self._ensure_initialized()
        connection = self._connection()
        removed = 0
        connection.execute("BEGIN")
        try:
            for offset in range(0, len(target_ids), self._DELETE_CHUNK_SIZE):
                chunk = target_ids[offset : offset + self._DELETE_CHUNK_SIZE]
                placeholders = ", ".join("?" for _ in chunk)
                cursor = connection.execute(
                    f"DELETE FROM logs WHERE id IN ({placeholders})",
                    chunk,
                )
                removed += max(cursor.rowcount, 0)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return removed

    def cleanup_expired(self, retention_days: int) -> int:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        self._ensure_initialized()
        cutoff = time.time() - retention_days * 86400
        connection = self._connection()
        with connection:
            cursor = connection.execute("DELETE FROM logs WHERE created_ts < ?", (cutoff,))
            return max(cursor.rowcount, 0)
