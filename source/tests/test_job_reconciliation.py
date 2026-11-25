"""Offline recovery tests; every database and engine root is disposable."""
from contextlib import closing, contextmanager, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from framebridge_expanded import cli, jobs, job_reconciliation as recovery


class OfflineReconciliation(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.old_umask)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.profile = Path(temp.name) / "profile"
        self.fbroot = self.profile / "home/.local/share/framebridge/fbterm"
        self.root = self.fbroot / "queue-v2"
        self.root.mkdir(mode=0o700, parents=True)
        (self.fbroot / "jobs").mkdir(mode=0o700)
        self.calls = []
        self.unknown = {"job_id": "uncertain", "session_id": "legacy",
                        "origin_request_id": "original-request", "script": "true",
                        "timeout_s": 1}
        self.digest = hashlib.sha256(b"true").hexdigest()
        with self.queue() as q:
            q.submit(self.unknown, legacy=True)
            self.assertEqual(q.wait("uncertain")["execution_state"], "outcome_unknown")
        # Age a synthetic historical claim, never a real user's ledger.
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            now = time.time() - 60
            db.execute("UPDATE jobs SET created=?, started=?, deadline=? WHERE id=?",
                       (now, now + 0.01, now + 1, "uncertain"))
        self.evidence = self.profile / "operator-review.json"
        self.review = {"schema": 1, "job_id": "uncertain",
                       "request_id": "original-request", "command_sha256": self.digest,
                       "reviewed_local_processes": True, "reviewed_external_effects": True,
                       "no_pending_effects": True, "accept_unknown": True,
                       "review_reference": "TEST-REVIEW"}
        self.evidence.write_text(json.dumps(self.review))
        self.evidence.chmod(0o600)

    def execute(self, row, slot):
        self.calls.append(row["id"])
        if row["id"] == "uncertain":
            return {"ok": False, "execution_state": "outcome_unknown",
                    "error": "FBTERM_RECEIPT_INVALID", "engine_rc": 2}
        return {"ok": True, "execution_state": "completed", "exit_code": "0"}

    @contextmanager
    def queue(self, *, recover=None):
        q = jobs.JobQueue(self.root, self.execute, recover=recover)
        try:
            yield q
        finally:
            q.close()

    def row(self, path=None):
        with closing(sqlite3.connect(path or self.root / "jobs.sqlite3")) as db:
            return db.execute("SELECT * FROM jobs WHERE id='uncertain'").fetchone()

    def count_decisions(self, path=None):
        with closing(sqlite3.connect(path or self.root / "jobs.sqlite3")) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                              (recovery.TABLE,)).fetchone():
                return 0
            return db.execute(f"SELECT COUNT(*) FROM {recovery.TABLE}").fetchone()[0]

    def reconcile(self, **kwargs):
        options = {"job_id": "uncertain", "request": "original-request",
                   "digest": self.digest, "evidence_path": self.evidence,
                   "confirm": lambda summary: True}
        options.update(kwargs)
        return recovery.reconcile_offline(self.profile, **options)

    def refuse(self, reason, **kwargs):
        before = self.row()
        with self.assertRaisesRegex((RuntimeError, OSError, ValueError), reason):
            self.reconcile(**kwargs)
        self.assertEqual(self.row(), before)
        self.assertEqual(self.count_decisions(), 0)

    def test_global_block_persists_until_explicit_offline_review(self):
        with self.queue() as q:
            reply = q.submit({"job_id": "blocked", "script": "true", "timeout_s": 1},
                             legacy=True)
            self.assertEqual(reply["blocking_job_id"], "uncertain")
            self.assertEqual(q.wait("blocked")["execution_state"], "expired_before_start")
            self.assertEqual(q.status("uncertain")["resource_reservation"], "held")
        self.assertEqual(self.calls, ["uncertain"])
        before = self.row()
        result = self.reconcile()
        self.assertEqual(result["execution_state"], "outcome_unknown")
        self.assertTrue(result["do_not_replay"])
        self.assertFalse(result["outcome_proven"])
        self.assertEqual(self.row(), before)
        self.assertEqual(self.count_decisions(), 1)
        audit = Path(result["audit_directory"])
        self.assertEqual(self.row(audit / "before.sqlite3"), before)
        self.assertEqual(self.count_decisions(audit / "before.sqlite3"), 0)
        for name, key in (("before.sqlite3", "backup_sha256"),
                          ("decision.json", "decision_sha256"),
                          ("evidence.json", "evidence_sha256")):
            self.assertEqual(hashlib.sha256((audit / name).read_bytes()).hexdigest(),
                             result[key])
            self.assertEqual((audit / name).stat().st_mode & 0o777, 0o600)
        for job in ("fresh", "after-restart"):
            with self.queue() as q:
                old = q.status("uncertain")
                self.assertEqual(old["execution_state"], "outcome_unknown")
                self.assertEqual(old["resource_reservation"], "released_after_review")
                self.assertTrue(old["do_not_replay"])
                self.assertFalse(old["ok"])
                self.assertTrue(q.submit(self.unknown, legacy=True)["do_not_replay"])
                self.assertEqual(q.submit(dict(self.unknown, script="false"), legacy=True)["error"],
                                 "JOB_ID_CONFLICT")
                q.submit({"job_id": job, "script": "true", "timeout_s": 5}, legacy=True)
                self.assertEqual(q.wait(job)["execution_state"], "completed")
        self.assertEqual(self.calls, ["uncertain", "fresh", "after-restart"])
        self.assertEqual(self.row(), before)

    def test_refuses_without_exact_operator_confirmation(self):
        self.refuse("RECOVERY_CONFIRMATION_REQUIRED", confirm=lambda summary: False)
        self.assertFalse((self.root / "reconciliation-evidence").exists())

    def test_refuses_active_scheduler(self):
        with self.queue():
            self.refuse("RECOVERY_PROFILE_OR_ENGINE_ACTIVE")

    def test_refuses_live_original_controller(self):
        with recovery._offline_lock(self.fbroot / "controller.lock", create=True):
            self.refuse("RECOVERY_PROFILE_OR_ENGINE_ACTIVE")

    def test_refuses_live_lane_worker(self):
        lane = self.fbroot / "lanes/1"
        lane.mkdir(mode=0o700, parents=True)
        with recovery._offline_lock(lane / "worker.lock", create=True):
            self.refuse("RECOVERY_PROFILE_OR_ENGINE_ACTIVE")

    def test_requires_identity_matched_to_database(self):
        self.review["request_id"] = "other-request"
        self.evidence.write_text(json.dumps(self.review))
        self.refuse("RECOVERY_JOB_NOT_ELIGIBLE", request="other-request")

    def test_refuses_unreviewed_external_effects(self):
        self.review["reviewed_external_effects"] = False
        self.evidence.write_text(json.dumps(self.review))
        self.refuse("RECOVERY_EVIDENCE_IDENTITY_OR_ATTESTATION_INVALID")

    def test_refuses_integer_as_boolean(self):
        self.review["no_pending_effects"] = 1
        self.evidence.write_text(json.dumps(self.review))
        self.refuse("RECOVERY_EVIDENCE_IDENTITY_OR_ATTESTATION_INVALID")

    def test_refuses_world_readable_evidence(self):
        self.evidence.chmod(0o644)
        self.refuse("RECOVERY_UNTRUSTED_FILE")

    def test_refuses_symlink_evidence(self):
        alias = self.profile / "alias.json"
        alias.symlink_to(self.evidence)
        self.refuse("Too many levels|symbolic link", evidence_path=alias)

    def test_refuses_existing_original_job_evidence(self):
        (self.fbroot / "jobs/uncertain").mkdir(mode=0o700)
        self.refuse("RECOVERY_ORIGINAL_EVIDENCE_PRESENT")

    def test_refuses_another_unresolved_engine_directory(self):
        (self.fbroot / "jobs/other").mkdir(mode=0o700)
        self.refuse("RECOVERY_OTHER_UNRESOLVED_ENGINE_JOB")

    def test_refuses_other_queued_work(self):
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.execute("""INSERT INTO jobs(id,session,request,digest,resources,timeout,legacy,
                          script,state,created,deadline)
                          SELECT 'pending',session,'pending-request',digest,resources,timeout,
                                 legacy,'true','queued',created,deadline
                          FROM jobs WHERE id='uncertain'""")
        self.refuse("RECOVERY_OTHER_WORK_PENDING")

    def test_refuses_unexpired_claim(self):
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.execute("UPDATE jobs SET deadline=? WHERE id='uncertain'", (time.time()+60,))
        self.refuse("RECOVERY_JOB_NOT_ELIGIBLE")

    def test_evidence_change_while_confirming_fails_closed(self):
        def change(summary):
            self.review["review_reference"] = "CHANGED"
            self.evidence.write_text(json.dumps(self.review))
            return True
        self.refuse("RECOVERY_EVIDENCE_CHANGED", confirm=change)

    def test_failure_before_commit_keeps_unknown_held(self):
        with patch.object(recovery, "_write_new", side_effect=OSError("SIMULATED_AUDIT_FAILURE")):
            self.refuse("SIMULATED_AUDIT_FAILURE")
        with self.queue() as q:
            self.assertEqual(q.status("uncertain")["resource_reservation"], "held")

    def test_stale_decision_cannot_release_changed_receipt(self):
        self.reconcile()
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.execute("UPDATE jobs SET result=? WHERE id='uncertain'",
                       (json.dumps({"error": "changed-test-fixture"}),))
        with self.queue() as q:
            self.assertEqual(q.status("uncertain")["resource_reservation"], "held")
            reply = q.submit({"job_id": "still-blocked", "script": "true", "timeout_s": 1},
                             legacy=True)
            self.assertEqual(reply["blocking_job_id"], "uncertain")
            self.assertEqual(q.wait("still-blocked")["execution_state"], "expired_before_start")

    def test_other_uncertain_job_keeps_its_own_reservation(self):
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.execute("""INSERT INTO jobs(id,session,request,digest,resources,timeout,legacy,
                       script,state,created,deadline,started,slot,result)
                       SELECT 'other',session,'other-request',digest,resources,timeout,legacy,
                              NULL,'outcome_unknown',created,deadline,started,slot,result
                       FROM jobs WHERE id='uncertain'""")
        self.reconcile()
        with self.queue() as q:
            self.assertEqual(q.status("uncertain")["resource_reservation"], "released_after_review")
            self.assertEqual(q.status("other")["resource_reservation"], "held")
            reply = q.submit({"job_id": "blocked-again", "script": "true", "timeout_s": 1},
                             legacy=True)
            self.assertEqual(reply["blocking_job_id"], "other")
            q.wait("blocked-again")

    def test_duplicate_administrative_decision_refused(self):
        self.reconcile()
        before = self.row()
        with self.assertRaisesRegex(RuntimeError, "RECOVERY_DECISION_ALREADY_EXISTS"):
            self.reconcile()
        self.assertEqual(self.row(), before)
        self.assertEqual(self.count_decisions(), 1)

    def test_old_schema_and_downgrade_are_fail_closed(self):
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.execute(f"DROP TABLE {recovery.TABLE}")  # disposable old-schema fixture
        self.reconcile()
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            # This is the old scheduler's active-state query.
            rows = db.execute("SELECT id FROM jobs WHERE state IN ('in_flight','outcome_unknown')").fetchall()
        self.assertEqual(rows, [("uncertain",)])

    def test_original_automatic_recovery_still_works_without_review(self):
        with self.queue(recover=lambda row: {"ok": True, "execution_state": "completed",
                                             "exit_code": "0"}) as q:
            deadline = time.monotonic() + 5
            while q.status("uncertain")["execution_state"] != "completed":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.assertEqual(self.count_decisions(), 0)
            self.assertEqual(self.calls, ["uncertain"])

    def test_reappearing_original_evidence_reholds_reservation(self):
        self.reconcile()
        (self.fbroot / "jobs/uncertain").mkdir(mode=0o700)
        with self.queue() as q:
            self.assertEqual(q.status("uncertain")["resource_reservation"], "held")
            reply = q.submit({"job_id": "reheld", "script": "true", "timeout_s": 1},
                             legacy=True)
            self.assertEqual(reply["blocking_job_id"], "uncertain")
            self.assertEqual(q.wait("reheld")["execution_state"], "expired_before_start")
        # Once observed by the scheduler, invalidation is durable even if
        # the directory subsequently disappears. This is a synthetic fixture.
        (self.fbroot / "jobs/uncertain").rmdir()
        with self.queue() as q:
            self.assertEqual(q.status("uncertain")["resource_reservation"], "held")
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db:
            row=db.execute(f"SELECT invalidated_at,invalidation_reason FROM {recovery.TABLE}").fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1],"REVIEW_BINDING_OR_EVIDENCE_CHANGED")
        self.assertEqual(self.calls, ["uncertain"])

    def test_changed_evidence_record_fails_closed(self):
        self.reconcile()
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.execute(f"UPDATE {recovery.TABLE} SET evidence_json='changed-test-fixture'")
        with self.queue() as q:
            self.assertEqual(q.status("uncertain")["resource_reservation"], "held")

    def test_changed_operator_identity_fails_closed(self):
        self.reconcile()
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as db, db:
            db.execute(f"UPDATE {recovery.TABLE} SET operator_uid=?", (os.getuid()+1,))
        with self.queue() as q:
            self.assertEqual(q.status("uncertain")["resource_reservation"], "held")

    def test_duplicate_evidence_keys_rejected(self):
        raw=json.dumps(self.review)
        self.evidence.write_text(raw[:-1]+', "accept_unknown": true}')
        self.refuse("RECOVERY_DUPLICATE_EVIDENCE_KEY")

    def test_original_evidence_appearing_during_confirmation_refused(self):
        def appeared(summary):
            (self.fbroot / "jobs/uncertain").mkdir(mode=0o700)
            return True
        self.refuse("RECOVERY_ORIGINAL_EVIDENCE_PRESENT", confirm=appeared)

    def test_noninteractive_cli_never_invokes_offline_release(self):
        args = ["--state-dir", str(self.profile), "--json", "recovery", "reconcile",
                "--job-id", "uncertain", "--request-id", "original-request",
                "--command-sha256", self.digest, "--evidence-file", str(self.evidence)]
        stream = io.StringIO()
        with patch.object(cli, "resolve_state", return_value=self.profile), \
             patch.object(recovery, "reconcile_offline") as mocked, redirect_stdout(stream):
            rc = cli.main(args)
        mocked.assert_not_called()
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(stream.getvalue())["result"]["error"], "INTERACTION_REQUIRED")
        self.assertEqual(self.count_decisions(), 0)


if __name__ == "__main__":
    unittest.main()
