"""Integrity and compatibility of stdout delivered by its producer."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_fbterm_adapter_receipts import adapter
from framebridge_expanded import fleet


class ProducerOutput(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.path = self.root / "lanes/1/jobs/job-test/stdout.bin"
        self.path.parent.mkdir(parents=True)
        self.patch = patch.object(adapter, "FBROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.request = {"request_type": "fbterm_jobs_v2", "job_action": "status", "include_stdout": True}

    def seed(self, data):
        self.path.write_bytes(data)
        return {"ok": True, "mode": "EXEC", "execution_state": "completed", "exit_code": "7",
                "job_id": "job-test", "command_ok": False, "worker_slot": 1, "job_protocol": 2,
                "origin_request_id": "receipt-1", "stdout_sha256": hashlib.sha256(data).hexdigest(),
                "capture_status": "complete", "stdout_bytes": len(data)}

    def check_failure(self, receipt, reason):
        r = adapter.output_receipt(self.request, receipt)
        self.assertEqual((r["origin_request_id"], r["exit_code"], r["execution_state"]),
                         ("receipt-1", "7", "completed"))
        self.assertEqual((r["stdout_omitted_reason"],r["stdout_integrity"],r["capture_status"]),
                         (reason,"unverified","partial"))
        self.assertNotIn("stdout", r)
        self.assertIsNone(r.get("result_final"))

    def test_identical_length_change_omits_output_and_stale_marker(self):
        receipt = self.seed(b"RESULTADO_FINAL=good\n")
        receipt["result_final"] = "cached"
        self.path.write_bytes(b"RESULTADO_FINAL=oops\n")
        self.check_failure(receipt, "STDOUT_INTEGRITY_MISMATCH")

    def test_tail_beyond_preview_is_also_verified(self):
        data = b"x" * (1048576 + 3)
        receipt = self.seed(data)
        self.path.write_bytes(data[:-1] + b"y")
        self.check_failure(receipt, "STDOUT_INTEGRITY_MISMATCH")

    def test_missing_file_and_missing_digest_have_separate_reasons(self):
        receipt = self.seed(b"text")
        self.check_failure(dict(receipt, stdout_sha256=None), "STDOUT_DIGEST_UNAVAILABLE")
        self.path.unlink()
        self.check_failure(receipt, "STDOUT_NOT_FOUND")

    def test_empty_output_is_present_and_verified(self):
        r = adapter.output_receipt(self.request, self.seed(b""))
        self.assertEqual(r["stdout"], "")
        self.assertEqual(r["stdout_integrity"], "verified")
        self.assertIsNone(r["stdout_omitted_reason"])

    def test_bounded_preview_including_large_lines_and_last_marker(self):
        raw = b"a" * 110000 + b"\r\nRESULTADO_FINAL=first\nRESULTADO_FINAL=last\n"
        r = adapter.output_receipt(self.request, self.seed(raw))
        self.assertEqual(r["stdout"], "a" * 65536)
        self.assertTrue(r["stdout_truncated"])
        self.assertEqual(r["result_final"], "last")

    def test_older_record_uses_job_and_slot_ignoring_stored_path(self):
        data = b"correct\r\n"
        receipt = self.seed(data)
        receipt.pop("stdout_bytes")
        other = self.root / "unused"
        other.write_bytes(b"incorrect")
        receipt.update(stdout_path=str(other), stderr_path=str(other))
        r = adapter.output_receipt(self.request, receipt)
        self.assertEqual(r["stdout"],data.decode())
        self.assertNotIn("stdout_path",r)
        self.assertNotIn("stderr_path",r)

    def test_unsafe_substitutions_fail_closed(self):
        receipt = self.seed(b"test")
        self.path.rename(self.root / "old")
        self.path.symlink_to(self.root / "old")
        self.check_failure(receipt,"STDOUT_UNREADABLE")
        self.path.unlink()
        self.path.mkdir()
        self.check_failure(receipt,"STDOUT_NOT_REGULAR")
        self.path.rmdir()
        os.mkfifo(self.path)
        self.check_failure(receipt,"STDOUT_NOT_REGULAR")

    def test_redaction_and_opt_in(self):
        data = (b"ok\nTOKEN=synthetic-value\n-----BEGIN PRIVATE KEY-----\n"
                b"RESULTADO_FINAL=hidden\n-----END PRIVATE KEY-----\nRESULTADO_FINAL=done\n")
        receipt = self.seed(data)
        r = adapter.output_receipt(self.request, receipt)
        self.assertEqual(r["result_final"],"done")
        self.assertIn("[REDACTED",r["stdout"])
        self.assertNotIn("synthetic-value",r["stdout"])
        self.assertNotIn("hidden",r["stdout"])
        silent = adapter.output_receipt(dict(self.request, include_stdout=False), receipt)
        self.assertNotIn("stdout",silent)
        self.assertNotIn("result_final",silent)

    def test_secret_label_after_preview_redacts_entire_line(self):
        r = adapter.output_receipt(self.request, self.seed(b"a"*70000 + b" PASSWORD=synthetic\n"))
        self.assertEqual(r["stdout"],"[REDACTED SENSITIVE LINE]\n")
        self.assertTrue(r["stdout_truncated"])

    def test_in_flight_does_not_read_incomplete_capture(self):
        receipt=dict(self.seed(b"partial"), execution_state="in_flight")
        r = adapter.output_receipt(self.request,receipt)
        self.assertEqual(r["stdout_omitted_reason"],"JOB_NOT_COMPLETED")
        self.assertNotIn("stdout",r)

    def test_producer_verification_is_not_attributed_to_older_node(self):
        for r in ({"exit_code":"0","stdout":"text"}, {"exit_code":"0"}):
            fleet._output_compatibility(r,True)
            self.assertEqual(r["stdout_integrity"],"unverified")
            if "stdout" not in r:
                self.assertEqual(r["stdout_omitted_reason"],"ADAPTER_OUTPUT_UNSUPPORTED")
