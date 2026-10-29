"""Source-availability diagnostics for the read-only FBTERM catalogue."""
import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from framebridge_expanded.console_ui import fbterm_dashboard as dashboard


class DashboardSourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def legacy_job(self):
        job = self.root / "jobs" / "legacy-job"
        job.mkdir(parents=True)
        (job / "exit.code").write_text("0\n")
        return job

    def scan(self):
        catalogue = dashboard.Catalogue(self.root)
        catalogue.scan()
        return catalogue

    def test_missing_optional_receipts_keeps_legacy_exit_without_read_errors(self):
        self.legacy_job()
        catalogue = self.scan()
        self.assertEqual(catalogue.errors, 0)
        self.assertEqual(catalogue.total, 1)
        self.assertEqual(catalogue.counts, {"EXIT": 1})
        self.assertEqual(catalogue.recent[0].exit_code, 0)
        self.assertFalse((self.root / "receipts").exists())

    def test_receipts_permission_denied_remains_a_source_error(self):
        self.legacy_job()
        receipts = self.root / "receipts"
        receipts.mkdir()
        real_scandir = dashboard.os.scandir

        def scandir(path):
            if Path(path) == receipts:
                raise PermissionError(errno.EACCES, "fixture denied", str(path))
            return real_scandir(path)

        with mock.patch.object(dashboard.os, "scandir", side_effect=scandir):
            catalogue = self.scan()
        self.assertEqual(catalogue.errors, 1)
        self.assertEqual(catalogue.counts, {"EXIT": 1})

    def test_receipts_that_is_not_a_directory_remains_a_source_error(self):
        self.legacy_job()
        (self.root / "receipts").write_text("not a directory")
        catalogue = self.scan()
        self.assertEqual(catalogue.errors, 1)
        self.assertEqual(catalogue.counts, {"EXIT": 1})

    def test_missing_required_jobs_remains_a_source_error(self):
        catalogue = self.scan()
        self.assertEqual(catalogue.errors, 1)
        self.assertEqual(catalogue.total, 0)
        self.assertFalse((self.root / "jobs").exists())
        self.assertFalse((self.root / "receipts").exists())

    def test_receipt_entry_disappearing_during_iteration_remains_a_read_error(self):
        self.legacy_job()
        receipts = self.root / "receipts"
        receipts.mkdir()
        entry = mock.Mock()
        entry.name = "vanished.json"
        entry.is_file.side_effect = FileNotFoundError(
            errno.ENOENT, "fixture entry disappeared")
        iterator = mock.MagicMock()
        iterator.__enter__.return_value = iter([entry])
        real_scandir = dashboard.os.scandir

        def scandir(path):
            return iterator if Path(path) == receipts else real_scandir(path)

        with mock.patch.object(dashboard.os, "scandir", side_effect=scandir):
            catalogue = self.scan()
        self.assertEqual(catalogue.errors, 1)
        self.assertEqual(catalogue.counts, {"EXIT": 1})

    def test_completed_receipt_without_job_is_mismatch_not_a_source_error(self):
        (self.root / "jobs").mkdir()
        receipts = self.root / "receipts"
        receipts.mkdir()
        receipt = {
            "schema": 4, "job_id": "completed-tombstone", "phase": "COMPLETED",
            "started_at": 1, "finished_at": 2, "exit_code": 0,
        }
        (receipts / "completed-tombstone.json").write_text(json.dumps(receipt))
        catalogue = self.scan()
        self.assertEqual(catalogue.errors, 0)
        self.assertEqual(catalogue.total, 1)
        self.assertEqual(catalogue.counts, {"MISMATCH": 1})
        self.assertIsNone(catalogue.recent[0].exit_code)


if __name__ == "__main__":
    unittest.main()
