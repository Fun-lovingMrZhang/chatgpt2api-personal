from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


class SchedulerStateStore:
    """SQLite persistence for scheduler health snapshots and task metadata."""

    _MIGRATION_BATCH_SIZE = 500
    _SAFE_METADATA_KEYS = {
        "size",
        "quality",
        "n",
        "priority",
        "created_at",
        "conversation_id",
        "remote_task_id",
        "idempotency_key",
        "source",
    }
    _BLOCKED_METADATA_KEYS = {
        "body",
        "data",
        "image",
        "images",
        "mask",
        "prompt",
        "request",
        "request_body",
        "base64",
        "b64_json",
    }

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                    CREATE TABLE IF NOT EXISTS scheduler_account_health (
                        account_key TEXT NOT NULL,
                        lane TEXT NOT NULL,
                        breaker_state TEXT NOT NULL,
                        cooldown_until REAL,
                        quota_snapshot INTEGER,
                        ewma_latency_ms REAL NOT NULL DEFAULT 0,
                        recent_success_count INTEGER NOT NULL DEFAULT 0,
                        recent_failure_count INTEGER NOT NULL DEFAULT 0,
                        last_error_class TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (account_key, lane)
                    );

                    CREATE TABLE IF NOT EXISTS scheduler_tasks (
                        task_key TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        model TEXT NOT NULL,
                        remote_task_id TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT,
                        error TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_scheduler_tasks_status
                        ON scheduler_tasks(status, updated_at DESC);
                    """
                )
            self._initialized = True

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def upsert_health(self, snapshot: dict[str, object]) -> None:
        self.upsert_health_batch([snapshot])

    def upsert_health_batch(self, snapshots: Iterable[dict[str, object]]) -> None:
        rows = [self._health_row(snapshot) for snapshot in snapshots]
        if not rows:
            return
        self._ensure_initialized()
        connection = self._connection()
        with connection:
            connection.executemany(
                """
                INSERT INTO scheduler_account_health (
                    account_key, lane, breaker_state, cooldown_until,
                    quota_snapshot, ewma_latency_ms, recent_success_count,
                    recent_failure_count, last_error_class, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key, lane) DO UPDATE SET
                    breaker_state = excluded.breaker_state,
                    cooldown_until = excluded.cooldown_until,
                    quota_snapshot = excluded.quota_snapshot,
                    ewma_latency_ms = excluded.ewma_latency_ms,
                    recent_success_count = excluded.recent_success_count,
                    recent_failure_count = excluded.recent_failure_count,
                    last_error_class = excluded.last_error_class,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def list_health(self) -> list[dict[str, object]]:
        self._ensure_initialized()
        rows = self._connection().execute(
            """
            SELECT account_key, lane, breaker_state, cooldown_until,
                   quota_snapshot, ewma_latency_ms, recent_success_count,
                   recent_failure_count, last_error_class, updated_at
            FROM scheduler_account_health
            ORDER BY account_key, lane
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def save_task(self, task: dict[str, object]) -> dict[str, object]:
        record = self._task_record(task)
        self._ensure_initialized()
        connection = self._connection()
        with connection:
            connection.execute(
                """
                INSERT INTO scheduler_tasks (
                    task_key, owner_id, task_id, mode, status, model,
                    remote_task_id, metadata_json, result_json, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_key) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    task_id = excluded.task_id,
                    mode = excluded.mode,
                    status = excluded.status,
                    model = excluded.model,
                    remote_task_id = excluded.remote_task_id,
                    metadata_json = excluded.metadata_json,
                    result_json = excluded.result_json,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                record,
            )
        return self.get_task(str(record[0]))

    def update_task(self, task_key: str, updates: dict[str, object]) -> dict[str, object] | None:
        self._ensure_initialized()
        current = self.get_task(task_key)
        if current is None:
            return None
        merged = dict(current)
        merged.update(updates)
        merged["task_key"] = task_key
        if "metadata" in updates:
            merged["metadata"] = self._safe_metadata(updates.get("metadata"))
        if "result" in updates:
            merged["result"] = self._safe_json_value(updates.get("result"))
        return self.save_task(merged)

    def get_task(self, task_key: str) -> dict[str, object] | None:
        self._ensure_initialized()
        row = self._connection().execute(
            "SELECT * FROM scheduler_tasks WHERE task_key = ?",
            (str(task_key),),
        ).fetchone()
        return self._decode_task(row) if row is not None else None

    def load_recoverable_tasks(self) -> list[dict[str, object]]:
        self._ensure_initialized()
        rows = self._connection().execute(
            """
            SELECT * FROM scheduler_tasks
            WHERE status IN ('queued', 'running', 'unknown')
            ORDER BY updated_at ASC
            """
        ).fetchall()
        items = []
        unknown_keys: list[str] = []
        for row in rows:
            item = self._decode_task(row)
            if (
                item["status"] in {"queued", "running"}
                and not str(item.get("remote_task_id") or "").strip()
            ):
                item["status"] = "unknown"
                unknown_keys.append(str(item["task_key"]))
            item["retryable"] = bool(
                str(item.get("remote_task_id") or "").strip()
            )
            items.append(item)
        if unknown_keys:
            connection = self._connection()
            with connection:
                connection.executemany(
                    "UPDATE scheduler_tasks SET status = 'unknown' WHERE task_key = ?",
                    [(task_key,) for task_key in unknown_keys],
                )
        return items

    def import_legacy_tasks(self, path: Path) -> dict[str, object]:
        source = Path(path)
        if not source.exists():
            return {"imported": 0, "skipped": 0, "path": None}
        raw = json.loads(source.read_text(encoding="utf-8"))
        raw_items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            raise ValueError("legacy image task file must contain a task list")

        self._ensure_initialized()
        imported = 0
        skipped = 0
        batch: list[tuple[object, ...]] = []
        connection = self._connection()
        for item in raw_items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            try:
                record = self._legacy_task_record(item)
            except ValueError:
                skipped += 1
                continue
            batch.append(record)
            if len(batch) >= self._MIGRATION_BATCH_SIZE:
                imported_batch, skipped_batch = self._insert_task_batch(connection, batch)
                imported += imported_batch
                skipped += skipped_batch
                batch.clear()
        if batch:
            imported_batch, skipped_batch = self._insert_task_batch(connection, batch)
            imported += imported_batch
            skipped += skipped_batch

        migrated = self._migration_path(source)
        source.replace(migrated)
        return {"imported": imported, "skipped": skipped, "path": str(migrated)}

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    @staticmethod
    def _health_row(snapshot: dict[str, object]) -> tuple[object, ...]:
        account_key = str(snapshot.get("account_key") or "").strip()
        lane = str(snapshot.get("lane") or "").strip()
        if not account_key or not lane:
            raise ValueError("health snapshot requires account_key and lane")
        return (
            account_key,
            lane,
            str(snapshot.get("breaker_state") or "closed"),
            snapshot.get("cooldown_until"),
            snapshot.get("quota_snapshot"),
            float(snapshot.get("ewma_latency_ms") or 0),
            int(snapshot.get("recent_success_count") or 0),
            int(snapshot.get("recent_failure_count") or 0),
            str(snapshot.get("last_error_class") or ""),
            float(snapshot.get("updated_at") or time.time()),
        )

    def _task_record(self, task: dict[str, object]) -> tuple[object, ...]:
        task_key = str(task.get("task_key") or "").strip()
        owner_id = str(task.get("owner_id") or "").strip()
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if task_key and (not owner_id or not task_id):
            owner_id, separator, task_id = task_key.partition(":")
            if not separator or not owner_id or not task_id:
                raise ValueError("task_key must use owner_id:task_id format")
        if not task_key:
            if not owner_id or not task_id:
                raise ValueError("task requires task_key or owner_id and task_id")
            task_key = f"{owner_id}:{task_id}"
        if not owner_id or not task_id:
            raise ValueError("task requires owner_id and task_id")
        return (
            task_key,
            owner_id,
            task_id,
            str(task.get("mode") or "generate"),
            str(task.get("status") or "queued"),
            str(task.get("model") or ""),
            str(task.get("remote_task_id") or "").strip() or None,
            json.dumps(
                self._safe_metadata(task.get("metadata")),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            self._json_text(self._safe_json_value(task.get("result"))),
            str(task.get("error") or ""),
            float(task.get("updated_at") or time.time()),
        )

    def _decode_task(self, row: sqlite3.Row) -> dict[str, object]:
        metadata = self._decode_json(row["metadata_json"], {})
        result = self._decode_json(row["result_json"], None)
        return {
            "task_key": row["task_key"],
            "owner_id": row["owner_id"],
            "task_id": row["task_id"],
            "mode": row["mode"],
            "status": row["status"],
            "model": row["model"],
            "remote_task_id": row["remote_task_id"],
            "metadata": metadata,
            "result": result,
            "error": row["error"],
            "updated_at": row["updated_at"],
        }

    def _legacy_task_record(self, item: dict[str, object]) -> tuple[object, ...]:
        owner_id = str(item.get("owner_id") or "").strip()
        task_id = str(item.get("id") or item.get("task_id") or "").strip()
        if not owner_id or not task_id:
            raise ValueError("legacy task requires owner_id and id")
        remote_task_id = str(
            item.get("remote_task_id") or item.get("conversation_id") or ""
        ).strip() or None
        status = str(item.get("status") or "queued").strip()
        if status in {"queued", "running"} and not remote_task_id:
            status = "unknown"
        metadata = {
            key: item[key]
            for key in self._SAFE_METADATA_KEYS
            if key in item and key not in self._BLOCKED_METADATA_KEYS
        }
        if remote_task_id:
            metadata["remote_task_id"] = remote_task_id
        return (
            f"{owner_id}:{task_id}",
            owner_id,
            task_id,
            "edit" if item.get("mode") == "edit" else "generate",
            status,
            str(item.get("model") or "gpt-image-2"),
            remote_task_id,
            json.dumps(self._safe_metadata(metadata), ensure_ascii=False, separators=(",", ":")),
            None,
            str(item.get("error") or ""),
            float(item.get("updated_ts") or time.time()),
        )

    @staticmethod
    def _insert_task_batch(
        connection: sqlite3.Connection,
        rows: list[tuple[object, ...]],
    ) -> tuple[int, int]:
        if not rows:
            return 0, 0
        with connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO scheduler_tasks (
                    task_key, owner_id, task_id, mode, status, model,
                    remote_task_id, metadata_json, result_json, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            imported = connection.total_changes - before
        return imported, len(rows) - imported

    @staticmethod
    def _safe_metadata(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = str(key).strip()
            if not normalized_key or normalized_key.lower() in SchedulerStateStore._BLOCKED_METADATA_KEYS:
                continue
            if normalized_key not in SchedulerStateStore._SAFE_METADATA_KEYS:
                continue
            safe = SchedulerStateStore._safe_json_value(item)
            if safe is not None:
                result[normalized_key] = safe
        return result

    @staticmethod
    def _safe_json_value(value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [
                safe
                for item in value
                if (safe := SchedulerStateStore._safe_json_value(item)) is not None
            ]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized_key = str(key).lower()
                if normalized_key in SchedulerStateStore._BLOCKED_METADATA_KEYS:
                    continue
                safe = SchedulerStateStore._safe_json_value(item)
                if safe is not None:
                    result[str(key)] = safe
            return result
        return None

    @staticmethod
    def _json_text(value: object) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_json(value: object, default: object) -> object:
        if not value:
            return default
        try:
            return json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _migration_path(source: Path) -> Path:
        candidate = source.with_name(source.name + ".migrated")
        if not candidate.exists():
            return candidate
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        candidate = source.with_name(f"{source.name}.migrated.{timestamp}")
        suffix = 1
        while candidate.exists():
            candidate = source.with_name(f"{source.name}.migrated.{timestamp}.{suffix}")
            suffix += 1
        return candidate


class SnapshotWriter:
    """Coalesce health changes and flush them from one background thread."""

    def __init__(
        self,
        store: SchedulerStateStore,
        *,
        interval_seconds: float = 1.0,
        pending_threshold: int = 100,
    ):
        self.store = store
        self.interval_seconds = max(0.001, float(interval_seconds))
        self.pending_threshold = max(1, int(pending_threshold))
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, str], dict[str, object]] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def submit(self, snapshot: dict[str, object]) -> None:
        account_key = str(snapshot.get("account_key") or "").strip()
        lane = str(snapshot.get("lane") or "").strip()
        if not account_key or not lane:
            raise ValueError("health snapshot requires account_key and lane")
        with self._lock:
            self._pending[(account_key, lane)] = dict(snapshot)
            if len(self._pending) >= self.pending_threshold:
                self._wake.set()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="scheduler-health-snapshot-writer",
                daemon=True,
            )
            self._thread.start()

    def flush(self) -> None:
        with self._lock:
            batch = list(self._pending.values())
            self._pending.clear()
        if not batch:
            return
        try:
            self.store.upsert_health_batch(batch)
        except Exception:
            with self._lock:
                for item in batch:
                    key = (str(item["account_key"]), str(item["lane"]))
                    self._pending.setdefault(key, item)
            raise

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self.flush()

    def _run(self) -> None:
        try:
            while True:
                self._wake.wait(self.interval_seconds)
                self._wake.clear()
                if self._stop.is_set():
                    break
                try:
                    self.flush()
                except Exception:
                    continue
            try:
                self.flush()
            except Exception:
                pass
        finally:
            self.store.close()
