"""Disposable scheduler tests; synthetic executor, not native FBEXEC evidence.

Run with FRAMEBRIDGE_TEST_PREFIX pointing at the extracted package prefix.
No production profile is opened and no terminal/session is started.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PREFIX = Path(os.environ.get(
    "FRAMEBRIDGE_TEST_PREFIX",
    Path(__file__).resolve().parents[1] / "package9/usr/lib/framebridge-expanded",
)).resolve()
sys.path.insert(0, str(PREFIX / "expanded"))
from framebridge_expanded import jobs


class EnginePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fb-preflight-unit-")
        self.root = Path(self.temp.name) / "profile" / "fbterm"
        self.root.mkdir(parents=True, mode=0o700)
        (self.root / "bin").mkdir(mode=0o700)
        self.engine = self.root / "bin" / "fbterm_engine.py"
        self.calls = []
        self.result_state = "completed"
        self.raise_after_dispatch = False
        self.env = patch.dict(os.environ, {"FRAMEBRIDGE_JOB_SLOTS": "1"})
        self.env.start()

        def execute_original(request, *, exec_root, worker_slot):
            self.calls.append((dict(request), Path(exec_root), worker_slot))
            if self.raise_after_dispatch:
                raise RuntimeError("SYNTHETIC_FAILURE_AFTER_INVOCATION")
            return {
                "ok": self.result_state == "completed",
                "execution_state": self.result_state,
                "job_id": request["job_id"],
                "command_sha256": request["sha256"],
                "exit_code": "0" if self.result_state == "completed" else None,
            }

        self.adapter = SimpleNamespace(
            FBROOT=self.root,
            ENGINE=self.engine,
            handle=lambda payload: {"ok": False, "unhandled": True},
            execute_original=execute_original,
        )
        self.queue = jobs.install(self.adapter)

    def tearDown(self):
        self.queue.close()
        self.env.stop()
        self.temp.cleanup()

    def submit(self, job_id="probe", *, protocol_v2=False, timeout=2):
        payload = {
            "version": 1, "authorized": True, "mode": "EXEC",
            "job_id": job_id, "script": "printf 'harmless\\n'\n",
            "timeout_s": timeout,
        }
        if not protocol_v2:
            return self.adapter.handle(payload)
        return self.queue.wait(self.queue.submit(dict(
            payload, session_id="unit-session", resources=["write:test-resource"],
        ))["job_id"])

    def write_engine(self):
        self.engine.write_text("# Readable synthetic dispatch dependency.\n")

    def assert_not_dispatched(self, result):
        self.assertEqual(result["execution_state"], "not_started")
        self.assertEqual(result["error"], "FBTERM_ENGINE_UNAVAILABLE")
        self.assertFalse(result["ok"])
        self.assertFalse(result["do_not_replay"])
        self.assertEqual(self.calls, [])
        self.assertIsNone(result["started_at"])
        self.assertIsNotNone(result["finished_at"])
        self.assertFalse((self.root / "jobs" / result["job_id"]).exists())
        with sqlite3.connect(self.queue.db) as db:
            state, script = db.execute(
                "SELECT state,script FROM jobs WHERE id=?", (result["job_id"],)
            ).fetchone()
        self.assertEqual(state, "not_started")
        self.assertIsNone(script)

    def test_missing_engine_has_known_not_started_result(self):
        self.assert_not_dispatched(self.submit())

    def test_broken_engine_symlink_is_not_dispatched(self):
        self.engine.symlink_to(self.root / "missing-source.py")
        self.assert_not_dispatched(self.submit())

    def test_engine_directory_is_not_dispatched(self):
        self.engine.mkdir()
        self.assert_not_dispatched(self.submit())

    @unittest.skipIf(os.geteuid() == 0, "Root permission semantics differ")
    def test_unreadable_engine_is_not_dispatched(self):
        self.write_engine()
        self.engine.chmod(0)
        self.assert_not_dispatched(self.submit())

    def test_valid_profile_symlink_calls_original_executor(self):
        original_engine = PREFIX / "original/workstation/fbterm/bin/fbterm_engine.py"
        self.assertTrue(original_engine.is_file())
        self.engine.symlink_to(original_engine)
        result = self.submit()
        self.assertEqual(result["execution_state"], "completed")
        self.assertEqual(len(self.calls), 1)
        request, exec_root, slot = self.calls[0]
        self.assertEqual(exec_root, self.root)
        self.assertEqual(slot, 0)
        self.assertEqual(request["sha256"], hashlib.sha256(request["script"].encode()).hexdigest())

    def test_lane_checks_actual_adapter_engine(self):
        # The slot-specific bin path is not a substitute for adapter.ENGINE.
        other = self.root / "readable-but-unused-engine.py"
        other.write_text("# Unused fixture.\n")
        slot_bin = self.root / "lanes" / "1" / "bin"
        slot_bin.mkdir(mode=0o700, parents=True)
        (slot_bin / "fbterm_engine.py").symlink_to(other)
        result = self.submit(protocol_v2=True)
        self.assert_not_dispatched(result)

    def test_existing_dangling_lane_bin_cannot_fail_before_preflight(self):
        # This reproduces a slot linked through a stale source/profile bin.
        (self.root / "bin").rmdir()
        (self.root / "bin").symlink_to(self.root / "unavailable-package-bin")
        slot = self.root / "lanes" / "1"
        slot.mkdir(mode=0o700, parents=True)
        (slot / "bin").symlink_to(self.root / "bin")
        self.assert_not_dispatched(self.submit(protocol_v2=True))

    def test_repair_requires_new_id_and_releases_resources(self):
        first = self.submit("before-repair")
        self.assert_not_dispatched(first)
        self.write_engine()
        duplicate = self.submit("before-repair")
        self.assertEqual(duplicate, first)
        self.assertEqual(self.calls, [])
        after = self.submit("after-repair")
        self.assertEqual(after["execution_state"], "completed")
        self.assertIsNone(after["blocking_job_id"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0]["job_id"], "after-repair")
        with sqlite3.connect(self.queue.db) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('in_flight','outcome_unknown')"
            ).fetchone()[0], 0)

    def test_dispatch_uncertainty_retains_global_reservation(self):
        self.write_engine()
        self.result_state = "outcome_unknown"
        unknown = self.submit("uncertain")
        self.assertEqual(unknown["execution_state"], "outcome_unknown")
        self.assertEqual(unknown["resource_reservation"], "held")
        self.assertTrue(unknown["do_not_replay"])
        blocked = self.submit("later-job", timeout=1)
        self.assertEqual(blocked["execution_state"], "expired_before_start")
        self.assertEqual(len(self.calls), 1)
        duplicate = self.submit("uncertain")
        self.assertEqual(duplicate["execution_state"], "outcome_unknown")
        self.assertEqual(len(self.calls), 1)

    def test_exception_after_dispatch_stays_unknown(self):
        self.write_engine()
        self.raise_after_dispatch = True
        result = self.submit("dispatch-exception")
        self.assertEqual(result["execution_state"], "outcome_unknown")
        self.assertEqual(result["resource_reservation"], "held")
        self.assertTrue(result["do_not_replay"])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
