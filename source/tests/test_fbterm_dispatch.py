"""Regression tests for the private dispatch channel and its durable claims."""

import concurrent.futures
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "sources/original-asus/fbterm/bin"
spec = importlib.util.spec_from_file_location("fbterm_candidate", SOURCE / "fbterm_engine.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(SOURCE, self.root / "bin", ignore=shutil.ignore_patterns("__pycache__"))
        self.term = engine.FBTerm(self.root, "isolated", "isolated")
        self.term.jobs.mkdir()
        # The integration tests use the real FIFO, worker and runner. Only
        # creation of the tmux viewer is replaced in this local environment.
        self.term.ensure = lambda **kwargs: None

    def start_worker(self):
        with (self.root / "display.txt").open("wb") as display:
            worker = subprocess.Popen(
                [sys.executable, str(self.term.bin / "fbterm_engine.py"), "serve"],
                env={
                    "PATH": f"{self.term.bin}:{os.defpath}",
                    "HOME": str(self.root),
                    "FBTERM_ROOT": str(self.root),
                    "FBTERM_SESSION": self.term.session,
                    "FBTERM_TITLE": self.term.title,
                },
                stdin=subprocess.DEVNULL,
                stdout=display,
                stderr=display,
            )
        self.addCleanup(self.stop_worker, worker)
        return worker

    @staticmethod
    def stop_worker(worker):
        worker.terminate()
        try:
            worker.wait(timeout=3)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=3)

    def open_without_worker(self):
        os.mkfifo(self.term.dispatch, 0o600)
        fd = os.open(self.term.dispatch, os.O_RDWR)
        self.addCleanup(os.close, fd)

    def test_real_worker_keeps_output_hashes_and_exit_codes(self):
        worker = self.start_worker()
        first = "printf 'salida\\n'; printf 'aviso\\n' >&2; exit 7"
        result = self.term.execute("first", first, 2)
        self.assertEqual(result["EXIT"], "7")
        self.assertEqual(result["COMMAND_HASH_GATE"], "PASS")
        for stream, expected in [("STDOUT", b"salida\n"), ("STDERR", b"aviso\n")]:
            self.assertEqual(Path(result[f"{stream}_PATH"]).read_bytes(), expected)
            self.assertEqual(result[f"{stream}_SHA256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(self.term.execute("second", "printf 'siguiente\\n'", 2)["EXIT"], "0")
        self.assertIsNone(worker.poll())
        self.assertTrue((self.term.jobs / "first" / "dispatch.started").is_file())

    def test_timeout_before_claim_revokes_launch_and_rejects_late_dispatch(self):
        self.open_without_worker()
        with patch.object(self.term, "_wait_for_completion", return_value=False):
            with self.assertRaises(engine.FBTermNotStarted):
                self.term.execute("cancelled", "exit 0", 1)
        job = self.term.jobs / "cancelled"
        token = (job / "dispatch.cancelled").read_text()
        self.assertFalse((job / "dispatch.started").exists())
        self.assertFalse((job / "DONE").exists())
        with patch.object(engine.subprocess, "run") as run:
            self.term._run_dispatched_job(job.name, token)
            run.assert_not_called()
        with self.assertRaisesRegex(engine.FBTermError, "JOB_ID ya existe"):
            self.term.execute(job.name, "exit 0", 1)

    def test_canceled_or_abandoned_preparation_allows_a_new_job(self):
        self.term._prepare_job(self.term.jobs / "abandoned", "exit 0", 1, "old-token")
        self.term._prepare_job(self.term.jobs / "cancelled", "exit 0", 1, "canceled-token")
        self.assertTrue(self.term._cancel_unstarted(self.term.jobs / "cancelled"))
        self.start_worker()
        self.assertEqual(self.term.execute("new", "exit 0", 2)["EXIT"], "0")
        self.assertTrue((self.term.jobs / "abandoned" / "dispatch.cancelled").is_file())
        self.assertFalse((self.term.jobs / "abandoned" / "DONE").exists())

    def test_claimed_job_without_receipt_stays_unknown_and_blocks(self):
        self.open_without_worker()
        def claim_and_expire(job, timeout_s):
            self.assertTrue(self.term._transition_pending(job, "dispatch.started"))
            return False
        with patch.object(self.term, "_wait_for_completion", side_effect=claim_and_expire):
            with self.assertRaises(engine.FBTermOutcomeUnknown):
                self.term.execute("claimed", "exit 0", 1)
        with self.assertRaisesRegex(engine.FBTermBusy, "claimed"):
            self.term.execute("later", "exit 0", 1)
        self.assertFalse((self.term.jobs / "later").exists())

    def test_unreadable_or_missing_result_cannot_report_completion(self):
        self.open_without_worker()
        def missing_result(job, timeout_s):
            self.assertTrue(self.term._transition_pending(job, "dispatch.started"))
            (job / "DONE").touch()
            return True
        with patch.object(self.term, "_wait_for_completion", side_effect=missing_result):
            with self.assertRaises(engine.FBTermOutcomeUnknown):
                self.term.execute("no-result", "exit 0", 1)

    def test_dispatch_reconciliation_io_error_stays_unknown(self):
        self.open_without_worker()
        with patch.object(self.term, "_wait_for_completion", return_value=False):
            with patch.object(self.term, "_cancel_unstarted", side_effect=OSError("disk")):
                with self.assertRaises(engine.FBTermOutcomeUnknown):
                    self.term.execute("io-error", "exit 0", 1)

    def test_worker_unavailable_does_not_publish_a_job(self):
        with patch.object(engine.time, "monotonic", side_effect=[0, 3]):
            with self.assertRaises(engine.FBTermNotStarted):
                self.term.execute("old-pane", "exit 0", 1)
        self.assertEqual(list(self.term.jobs.iterdir()), [])

    def test_duplicates_and_wrong_tokens_cannot_start_another_runner(self):
        self.term._prepare_job(self.term.jobs / "once", "exit 0", 1, "expected")
        with patch.object(engine.subprocess, "run") as run:
            self.term._run_dispatched_job("once", "different")
            run.assert_not_called()
            self.term._run_dispatched_job("once", "expected")
            self.term._run_dispatched_job("once", "expected")
            run.assert_called_once_with([str(self.term.runner), "once"], check=False)

    def test_cancellation_and_claim_have_exactly_one_winner(self):
        job = self.term.jobs / "race"
        self.term._prepare_job(job, "exit 0", 1, "token")
        gate = threading.Barrier(2)
        def attempt(state):
            gate.wait(timeout=2)
            return self.term._transition_pending(job, state)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            take = executor.submit(attempt, "dispatch.started")
            cancel = executor.submit(attempt, "dispatch.cancelled")
            self.assertNotEqual(take.result(timeout=3), cancel.result(timeout=3))
        self.assertNotEqual((job / "dispatch.started").exists(), (job / "dispatch.cancelled").exists())


if __name__ == "__main__":
    unittest.main()
