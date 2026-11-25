"""Durable scheduling inside a single authorized adapter profile."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from . import job_reconciliation as reconciliation, owner_jobs, update_control, job_feedback

ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}")
RESOURCE = re.compile(r"(read|write):([A-Za-z0-9][A-Za-z0-9_.:/-]{0,95})")
OPEN = ("queued", "in_flight", "outcome_unknown")
# Avoid fetching up to 256 KiB of shell text per queued job while scheduling.
META_COLUMNS = ("id, session, request, digest, resources, timeout, state, created, deadline, "
                "started, finished, slot, blocker, result, legacy")


def claims(values):
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError("INVALID_JOB_RESOURCES")
    result = {}
    for value in values:
        match = RESOURCE.fullmatch(value) if isinstance(value, str) else None
        if not match:
            raise ValueError("INVALID_JOB_RESOURCE")
        mode, name = match.groups()
        if result.get(name) != "write":
            result[name] = mode
    return result or {"*": "write"}


def conflict(left, right):
    if "*" in left or "*" in right:
        return True
    return any(name in right and (mode == "write" or right[name] == "write")
               for name, mode in left.items())


def failure(code, job_id, state="not_started"):
    return {"ok": False, "job_protocol": 2, "job_id": job_id, "error": code, "execution_state": state,
            "do_not_replay": state == "outcome_unknown"}


class JobQueue:
    """The adapter owns one scheduler. SQLite commits the claim before dispatch."""
    def __init__(self, root, execute, slots=2, *, recover=None, feedback_root=None):
        if not 1 <= slots <= 8:
            raise ValueError("INVALID_JOB_SLOTS")
        self.root = Path(root)
        self.root.mkdir(exist_ok=True, parents=True, mode=0o700)
        self.owner = (self.root / "scheduler.lock").open("ab")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.owner.close()
            raise
        self.changed = threading.Condition()
        self.recover = recover
        self.feedback_root = feedback_root
        self.execute = execute
        self.slots = slots
        self.db = self.root / "jobs.sqlite3"
        os.close(os.open(self.db, os.O_CREAT | os.O_RDWR, 0o600))
        self.stopping = False
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, request TEXT NOT NULL,
                    digest TEXT NOT NULL, resources TEXT NOT NULL, timeout INTEGER NOT NULL,
                    script TEXT, state TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
                    started REAL, finished REAL, slot INTEGER, blocker TEXT, result TEXT,
                    legacy INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_active ON jobs(state,created);
                CREATE INDEX IF NOT EXISTS jobs_session ON jobs(session,created);
            """)
            db.execute(reconciliation.SCHEMA)
            db.execute(update_control.SCHEMA)
            # A previous command may still have effects. Never redispatch its claim.
            interrupted = db.execute("SELECT id,digest,request FROM jobs WHERE state='in_flight'").fetchall()
            db.execute("UPDATE jobs SET state='outcome_unknown', script=NULL WHERE state='in_flight'")
        for row in interrupted:
            self.report_unknown(row)
        self.workers = [threading.Thread(target=self.work, daemon=True,
                        name=f"fbterm-jobs-{i}") for i in range(slots)]
        for worker in self.workers:
            worker.start()

    def report_unknown(self,row):
        if self.feedback_root is None:
            return
        try:
            job_feedback.record(self.feedback_root,row["id"],row["digest"],row["request"],
                                "outcome_unknown","watchdog")
        except (OSError,ValueError,RuntimeError):
            # An unavailable notification store must never rewrite a receipt or
            # terminate a scheduler worker. Unknown state remains visible in Jobs.
            logging.getLogger(__name__).warning("JOB_FEEDBACK_RECORD_UNAVAILABLE")

    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        reconciliation.install_sql_guards(db, self.root.parent)
        return db

    def close(self):
        with self.changed:
            self.stopping = True
            self.changed.notify_all()
        for worker in self.workers:
            worker.join()
        self.owner.close()

    def submit(self, payload, *, legacy=False):
        job = payload.get("job_id")
        session = payload.get("session_id", "legacy" if legacy else "")
        script = payload.get("script")
        timeout = payload.get("timeout_s", 120)
        if (not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", job)
                or job in (".", "..") or not isinstance(session, str) or not ID.fullmatch(session)):
            return failure("INVALID_JOB_IDENTITY", job)
        if not isinstance(script, str) or not script or len(script.encode()) > 262144:
            return failure("SCRIPT_INVALID", job)
        if type(timeout) is not int or not 1 <= timeout <= 600:
            return failure("INVALID_TIMEOUT", job)
        digest = hashlib.sha256(script.encode()).hexdigest()
        if payload.get("sha256", digest) != digest:
            return failure("SCRIPT_SHA256_MISMATCH", job)
        resources = json.dumps(claims([] if legacy else payload.get("resources", [])), sort_keys=True)
        now = time.time()
        deadline = min(payload.get("start_before", now + timeout), now + timeout)
        identity = (session, digest, resources, timeout, int(legacy))
        with self.changed, closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"SELECT {META_COLUMNS} FROM jobs WHERE id=?", (job,)).fetchone()
            if row is not None:
                if tuple(row[k] for k in ("session","digest","resources","timeout","legacy")) != identity:
                    return failure("JOB_ID_CONFLICT", job)
                return self.waiting_receipt(db, row)
            if update_control.paused(db):
                return failure("UPDATE_PREPARATION_NEW_JOBS_PAUSED", job)
            if db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0] >= 128:
                return failure("JOB_QUEUE_FULL", job)
            db.execute("""INSERT INTO jobs(id, session, digest, resources, timeout, legacy,
                       request, script, state, created, deadline) VALUES(?,?,?,?,?,?,?,?,'queued',?,?)""",
                       (job, *identity, payload.get("origin_request_id") or job, script, now, deadline))
            self.changed.notify_all()
            return self.waiting_receipt(db, db.execute(f"SELECT {META_COLUMNS} FROM jobs WHERE id=?", (job,)).fetchone())

    @staticmethod
    def receipt(row, *, private=False):
        state = row["state"]
        result = json.loads(row["result"] or "{}")
        if not private:
            for key in ("stdout_path", "stderr_path", "result_final"):
                result.pop(key, None)
        response = dict(result, job_protocol=2, job_id=row["id"], session_id=row["session"],
                        origin_request_id=row["request"], command_sha256=row["digest"],
                        execution_state=state, created_at=row["created"], claimed_at=row["started"],
                        started_at=None if state in ("not_started", "expired_before_start") else row["started"],
                        finished_at=row["finished"], resources=json.loads(row["resources"]),
                        worker_slot=row["slot"], blocking_job_id=row["blocker"] or result.get("blocking_job_id"))
        response.setdefault("ok", state in ("queued", "in_flight", "completed"))
        if state == "completed":
            response["command_ok"] = response.get("exit_code") == "0"
        if state == "expired_before_start":
            response.setdefault("error", "JOB_START_DEADLINE_EXPIRED")
        if state == "outcome_unknown":
            response.setdefault("error", "JOB_RECEIPT_UNCONFIRMED")
        response["do_not_replay"] = state in OPEN
        return response

    def waiting_receipt(self, db, row, *, private=False):
        result = self.receipt(row, private=private)
        if row["state"] == "outcome_unknown":
            result["ok"] = False
            result["resource_reservation"] = (
                "released_after_review" if reconciliation.released(db, row["id"]) else "held")
            result["do_not_replay"] = True
        if row["state"] == "queued":
            active = db.execute(f"""SELECT j.id,j.resources,j.state FROM jobs AS j
                                WHERE (j.state IN ('in_flight','outcome_unknown') AND {reconciliation.HELD})
                                OR (j.state='queued' AND j.rowid < (SELECT rowid FROM jobs WHERE id=?))
                                ORDER BY j.created,j.rowid""", (row["id"],)).fetchall()
            held = [r for r in active if r["state"] != "queued"]
            blocker = next((r["id"] for r in active if conflict(result["resources"], json.loads(r["resources"]))), None)
            if not blocker and len(held) >= self.slots:
                blocker = held[0]["id"]
            result["blocking_job_id"] = blocker
            result["wait_reason"] = ("resource" if blocker and any(
                r["id"] == blocker and conflict(result["resources"], json.loads(r["resources"])) for r in active)
                else "capacity" if blocker else "dispatch")
        return result

    def status(self, job, *, private=False):
        with closing(self.connect()) as db:
            row = db.execute(f"SELECT {META_COLUMNS} FROM jobs WHERE id=?", (job,)).fetchone()
            return self.waiting_receipt(db, row, private=private) if row else failure("JOB_NOT_FOUND", job, "outcome_unknown")

    def recent(self, session=""):
        with closing(self.connect()) as db:
            if session:
                rows = db.execute(f"SELECT {META_COLUMNS} FROM jobs AS j WHERE session=? ORDER BY (state IN ('queued','in_flight','outcome_unknown') AND {reconciliation.HELD}) DESC, created DESC LIMIT 100", (session,))
            else:
                rows = db.execute(f"SELECT {META_COLUMNS} FROM jobs AS j ORDER BY (state IN ('queued','in_flight','outcome_unknown') AND {reconciliation.HELD}) DESC, created DESC LIMIT 100")
            return [self.waiting_receipt(db, row) for row in rows]

    def wait(self, job):
        with self.changed:
            while True:
                row = self.status(job, private=True)
                if row["execution_state"] not in ("queued", "in_flight"):
                    return row
                self.changed.wait(1)

    def take(self):
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE jobs SET state='expired_before_start',finished=?,script=NULL,
                       blocker=NULL WHERE state='queued' AND deadline<=?""", (now, now))
            reconciliation.invalidate_changed_decisions(db, now)
            rows = db.execute(f"""SELECT {META_COLUMNS} FROM jobs AS j
                              WHERE j.state IN ('queued','in_flight','outcome_unknown')
                              AND {reconciliation.HELD}
                              ORDER BY j.created,j.rowid""").fetchall()
            if self.recover:
                for row in rows:
                    if row["state"] == "outcome_unknown":
                        result = self.recover(row)
                        if result:
                            db.execute("UPDATE jobs SET state=?,result=?,finished=? WHERE id=?",
                                       (result["execution_state"],json.dumps(result),now,row["id"]))
                            self.changed.notify_all()
                            return None
            active = [r for r in rows if r["state"] != "queued"]
            occupied = {r["slot"] for r in active}
            earlier = [(r["id"], json.loads(r["resources"])) for r in active]
            for row in rows:
                if row["state"] != "queued":
                    continue
                if owner_jobs.is_requested(self.root,row["id"],row["digest"]):
                    result = failure("OWNER_CANCELLED_BEFORE_START",row["id"],"not_started")
                    result["owner_cancel_before_exec"] = True
                    db.execute("UPDATE jobs SET state='not_started',result=?,finished=?,script=NULL,blocker=NULL WHERE id=? AND state='queued'",
                               (json.dumps(result),now,row["id"]))
                    self.changed.notify_all()
                    return None
                resources = json.loads(row["resources"])
                blocker = next((job for job, held in earlier if conflict(resources, held)), None)
                slots = (0,) if row["legacy"] else range(1, self.slots + 1)
                slot = next((n for n in slots if n not in occupied), None)
                if blocker is None and (slot is None or len(active) >= self.slots):
                    blocker = active[0]["id"] if active else None
                if blocker:
                    if row["blocker"] != blocker:
                        db.execute("UPDATE jobs SET blocker=? WHERE id=?", (blocker, row["id"]))
                    earlier.append((row["id"], resources))
                    continue
                db.execute("UPDATE jobs SET state='in_flight',started=?,slot=?,blocker=NULL WHERE id=?",
                           (now, slot, row["id"]))
                return db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone(), slot
        return None

    def work(self):
        while True:
            with self.changed:
                if self.stopping:
                    return
                claim = self.take()
                if claim is None:
                    self.changed.wait(1)
                    continue
                self.changed.notify_all()
            row, slot = claim
            try:
                result = self.execute(row, slot)
                state = result["execution_state"]
                if state not in ("completed","not_started","expired_before_start","outcome_unknown"):
                    raise ValueError("INVALID_EXECUTION_RECEIPT")
            except Exception:
                state = "outcome_unknown"
                result = failure("JOB_RECEIPT_UNCONFIRMED", row["id"], state)
            with self.changed, closing(self.connect()) as db, db:
                db.execute("UPDATE jobs SET state=?,result=?,finished=?,script=NULL WHERE id=?",
                           (state, json.dumps(result), None if state == "outcome_unknown" else time.time(), row["id"]))
                self.changed.notify_all()
            if state == "outcome_unknown":
                self.report_unknown(row)


WIRE_SCRIPT = "# FrameBridge job protocol v2"
WIRE_TYPE = "fbterm_jobs_v2"
TRACE_FIELDS = ("session_id", "origin_request_id", "created_at", "started_at", "finished_at",
                "worker_slot", "resources", "blocking_job_id", "capture_status", "job_protocol")


def install(adapter):
    """Register behind the existing peer-UID and authorization checks."""
    import subprocess
    original = adapter.handle
    root = adapter.FBROOT

    def lane(slot):
        return root / "lanes" / str(slot) if slot else root

    def execute(row, slot):
        directory = lane(slot)
        if time.time() >= row["deadline"]:
            return failure("REQUEST_EXPIRED", row["id"], "expired_before_start")
        # A profile link may become stale after the adapter has started. Check
        # before invoking the original controller or preparing slot links: no
        # user command has been dispatched, so this is known to be not_started.
        # Failures after invocation retain the original fail-closed semantics.
        try:
            engine_ready = adapter.ENGINE.is_file() and os.access(adapter.ENGINE, os.R_OK)
        except OSError:
            engine_ready = False
        if not engine_ready:
            return failure("FBTERM_ENGINE_UNAVAILABLE", row["id"])
        if slot:
            # Old jobs predate resource declarations. Preserve the original
            # fail-closed policy across migration, including unresolved records.
            prior = root / "jobs"
            if prior.is_dir():
                for job in prior.iterdir():
                    if job.is_dir() and not ((job / "DONE").exists() and (job / "result.env").exists()
                                              or (job / "dispatch.cancelled").exists()):
                        return dict(failure("FBTERM_UNRESOLVED_JOB", row["id"]), blocking_job_id=job.name)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not (directory / "bin").exists():
                (directory / "bin").symlink_to(root / "bin", target_is_directory=True)
        if time.time() >= row["deadline"]:
            return failure("REQUEST_EXPIRED", row["id"], "expired_before_start")
        req = {"version": 1, "authorized": True, "mode": "EXEC", "job_id":row["id"],
               "script":row["script"], "timeout_s":row["timeout"], "sha256":row["digest"],
               "start_before":row["deadline"]}
        return adapter.execute_original(req, exec_root=directory, worker_slot=slot)

    def recover(row):
        job = lane(row["slot"]) / "jobs" / row["id"]
        if not (job / "DONE").is_file():
            return None
        try:
            with (job / "result.env").open("rb") as file:
                data = file.read(8193)
            if len(data) > 8192:
                return None
            fields = dict(line.split("=", 1) for line in data.decode("ascii").splitlines())
        except (OSError, ValueError):
            return None
        fields["COMMAND_HASH_GATE"] = "PASS" if fields.get("COMMAND_SHA256") == row["digest"] else "FAIL"
        result = adapter.decode_receipt(subprocess.CompletedProcess([], 0, json.dumps(fields)),
                                        row["id"], row["digest"], job)
        return result if (result["execution_state"] == "completed" or
                          result["execution_state"] == "not_started" and result.get("owner_cancel_before_exec") is True) else None

    queue = JobQueue(root / "queue-v2", execute,
                     int(os.environ.get("FRAMEBRIDGE_JOB_SLOTS", "2")), recover=recover, feedback_root=root)
    serial = threading.Lock()

    def handle(payload):
        if not isinstance(payload, dict):
            return original(payload)
        kind = str(payload.get("request_type", "exec")).strip().lower()
        if kind not in ("exec", WIRE_TYPE):
            # Preserve serialized input for pre-existing CUA and screen routes.
            with serial:
                return original(payload)
        job = payload.get("job_id")
        if (payload.get("version") != 1 or payload.get("authorized") is not True
                or payload.get("mode","EXEC") != "EXEC"):
            return failure("INVALID_AUTHORIZED_EXEC_REQUEST", job)
        if kind == WIRE_TYPE:
            if payload.get("script") != WIRE_SCRIPT or payload.get("sha256") != hashlib.sha256(WIRE_SCRIPT.encode()).hexdigest():
                return failure("INVALID_JOB_ENVELOPE", job)
            action = payload.get("job_action")
            # Feedback RPCs do not submit or wait for an EXEC scheduler slot.
            # The pre-existing authorized adapter/broker transport still applies.
            if action in ("feedback_list","feedback_ack"):
                try:
                    if action == "feedback_list":
                        return dict(job_feedback.list_root(root),job_protocol=2)
                    incident=payload.get("subject_job_id")
                    value=job_feedback.acknowledge_root(root,incident,payload.get("job_sha256"),
                                                       authorized=payload.get("authorized") is True)
                    return dict(value,job_protocol=2,job_id=incident,
                                incident_job_id=value["job_id"])
                except (OSError,ValueError,RuntimeError):
                    return {"ok":False,"job_protocol":2,"job_id":payload.get("subject_job_id"),
                            "error":"FEEDBACK_UNAVAILABLE_OR_IDENTITY_REJECTED"}
            if action == "list":
                session = payload.get("session_id", "")
                if session and (not isinstance(session, str) or not ID.fullmatch(session)):
                    return failure("INVALID_JOB_SESSION", job)
                return {"ok":True, "job_protocol":2, "jobs":queue.recent(session)}
            if action == "status":
                subject = payload.get("subject_job_id")
                if not isinstance(subject, str) or not adapter.JOB_RE.fullmatch(subject):
                    return failure("INVALID_JOB_IDENTITY", job)
                result = queue.status(subject, private=True)
                if result.get("session_id") and payload.get("session_id") and result["session_id"] != payload["session_id"]:
                    return failure("JOB_SESSION_MISMATCH", subject, "outcome_unknown")
                return result
            if action != "submit":
                return failure("INVALID_JOB_ACTION", job)
            payload = dict(payload, job_id=payload.get("subject_job_id"), script=payload.get("job_script"),
                           sha256=payload.get("job_sha256"))
            job = payload["job_id"]
        try:
            response = queue.submit(payload, legacy=kind == "exec")
        except ValueError as exc:
            return failure(str(exc), job)
        if kind == "exec" and response.get("execution_state") in ("queued", "in_flight", "completed"):
            return queue.wait(job)
        return response

    adapter.handle = handle
    return queue
