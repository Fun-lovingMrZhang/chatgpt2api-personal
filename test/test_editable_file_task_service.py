from __future__ import annotations

from services.editable_file_task_service import EditableFileTaskService
from services.scheduler.state_store import SchedulerStateStore


def test_editable_file_tasks_use_shared_normalized_task_table(tmp_path):
    store = SchedulerStateStore(tmp_path / "app.db")
    service = EditableFileTaskService(tmp_path / "editable_file_tasks.json", state_store=store)
    service._tasks["owner-1:file-1"] = {
        "id": "file-1",
        "owner_id": "owner-1",
        "status": "success",
        "kind": "ppt",
        "model": "gpt-4o",
        "created_at": "2026-09-09 00:00:00",
        "updated_at": "2026-09-09 00:00:01",
        "created_ts": 1.0,
        "updated_ts": 2.0,
        "result": {"zip_url": "/files/ppt/file-1.zip"},
    }

    service._save_locked()

    record = store.get_task("file:owner-1:file-1")
    assert record is not None
    assert record["mode"] == "editable_ppt"
    assert record["result"]["zip_url"] == "/files/ppt/file-1.zip"
    assert not (tmp_path / "editable_file_tasks.json").exists()

    reloaded = EditableFileTaskService(tmp_path / "editable_file_tasks.json", state_store=store)
    result = reloaded.list_tasks({"id": "owner-1"}, ["file-1"])
    assert result["missing_ids"] == []
    assert result["items"][0]["status"] == "success"


def test_editable_file_service_does_not_load_image_tasks_from_shared_table(tmp_path):
    store = SchedulerStateStore(tmp_path / "app.db")
    store.save_task({
        "task_key": "owner-1:image-1",
        "owner_id": "owner-1",
        "task_id": "image-1",
        "mode": "generate",
        "status": "success",
    })

    service = EditableFileTaskService(tmp_path / "editable_file_tasks.json", state_store=store)

    assert service.list_tasks({"id": "owner-1"}, ["image-1"])["items"] == []
