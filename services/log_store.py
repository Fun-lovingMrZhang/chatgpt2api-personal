from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


class SQLiteLogStore:
    _DELETE_CHUNK_SIZE = 900
    _MIGRATION_BATCH_SIZE = 500
    _MIGRATION_METADATA_KEY = "legacy_jsonl_migration"

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
            self._migrate_legacy(connection)
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

    @staticmethod
    def _legacy_id(raw_line: str, line_number: int) -> str:
        payload = f"{line_number}:{raw_line}".encode("utf-8", errors="ignore")
        return hashlib.sha256(payload).hexdigest()[:24]

    @staticmethod
    def _parse_legacy_time(value: str, fallback_ts: float) -> tuple[str, float]:
        normalized = value.strip()
        parsed: datetime | None = None
        if normalized:
            try:
                parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
        if parsed is None:
            parsed = datetime.fromtimestamp(fallback_ts)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S"), parsed.timestamp()

    def _metadata(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT value FROM log_metadata WHERE key = ?",
            (self._MIGRATION_METADATA_KEY,),
        ).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def migration_metadata(self) -> dict[str, Any]:
        self._ensure_initialized()
        return self._metadata(self._connection())

    def _migration_backup_path(self) -> Path:
        candidate = self.legacy_path.with_name(self.legacy_path.name + ".migrated")
        if not candidate.exists():
            return candidate
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        candidate = self.legacy_path.with_name(f"{self.legacy_path.name}.migrated.{timestamp}")
        suffix = 1
        while candidate.exists():
            candidate = self.legacy_path.with_name(
                f"{self.legacy_path.name}.migrated.{timestamp}.{suffix}"
            )
            suffix += 1
        return candidate

    def _insert_legacy_batch(
        self,
        connection: sqlite3.Connection,
        records: list[tuple[str, str, float, str, str, str]],
    ) -> tuple[int, int]:
        imported = 0
        duplicates = 0
        with connection:
            for record in records:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO logs
                        (id, time, created_ts, type, summary, detail_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    record,
                )
                if cursor.rowcount == 1:
                    imported += 1
                else:
                    duplicates += 1
        return imported, duplicates

    def _migrate_legacy(self, connection: sqlite3.Connection) -> None:
        if not self.legacy_path.exists():
            return
        existing_metadata = self._metadata(connection)
        if existing_metadata.get("completed"):
            return

        source_mtime = self.legacy_path.stat().st_mtime
        backup_path = self._migration_backup_path()
        imported = 0
        duplicates = 0
        invalid_lines = 0
        batch: list[tuple[str, str, float, str, str, str]] = []

        with self.legacy_path.open("r", encoding="utf-8") as file:
            for line_number, raw_line in enumerate(file, start=1):
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    invalid_lines += 1
                    continue
                if not isinstance(payload, dict):
                    invalid_lines += 1
                    continue

                record_id = str(payload.get("id") or self._legacy_id(raw_line, line_number))
                time_text, created_ts = self._parse_legacy_time(
                    str(payload.get("time") or ""),
                    source_mtime,
                )
                detail = payload.get("detail")
                if not isinstance(detail, dict):
                    detail = {}
                batch.append(
                    (
                        record_id,
                        time_text,
                        created_ts,
                        str(payload.get("type") or ""),
                        str(payload.get("summary") or ""),
                        json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                    )
                )
                if len(batch) >= self._MIGRATION_BATCH_SIZE:
                    batch_imported, batch_duplicates = self._insert_legacy_batch(connection, batch)
                    imported += batch_imported
                    duplicates += batch_duplicates
                    batch.clear()

        if batch:
            batch_imported, batch_duplicates = self._insert_legacy_batch(connection, batch)
            imported += batch_imported
            duplicates += batch_duplicates

        metadata = {
            "completed": True,
            "source": str(self.legacy_path),
            "backup_path": str(backup_path),
            "imported": imported,
            "duplicates": duplicates,
            "invalid_lines": invalid_lines,
            "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO log_metadata (key, value) VALUES (?, ?)",
                (self._MIGRATION_METADATA_KEY, json.dumps(metadata, ensure_ascii=False)),
            )
        self.legacy_path.rename(backup_path)

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
