from __future__ import annotations

import io
import tarfile
import unittest
from unittest import mock

import services.backup_service as backup_module
from services.backup_service import BackupService


class BackupQuotaSnapshotTests(unittest.TestCase):
    def test_auth_key_snapshot_includes_consistent_quota_database_snapshot(self):
        quota_bytes = b"SQLite quota snapshot"
        backend = mock.Mock()
        backend.get_backend_info.return_value = {"backend": "test"}
        backend.load_auth_keys.return_value = [{"id": "key-1"}]
        quota = mock.Mock()
        quota.snapshot_bytes.return_value = quota_bytes
        service = BackupService(quota_service_instance=quota)

        with mock.patch.object(backup_module, "config") as config:
            config.app_version = "test"
            config.get_storage_backend.return_value = backend
            payload = service._build_backup_archive(
                {"include": {"auth_keys_snapshot": True}},
                trigger="manual",
            )

        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            self.assertEqual(archive.extractfile("data/auth_quota.db").read(), quota_bytes)
        quota.snapshot_bytes.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
