"""Offline, operator-authorized reconciliation of a missing-receipt job.

Never synthesize an engine receipt, change a job's state or redispatch its
script. The queue records a separate, durable decision tied to the exact
historical identity. This is an owner-local maintenance operation, not an
MCP operation or a security boundary against the same Unix account.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

TABLE = "job_reconciliations_v1"
DECISION = "retain_unknown_release_reservation"
SCHEMA = """
CREATE TABLE IF NOT EXISTS job_reconciliations_v1 (
    job_id TEXT PRIMARY KEY NOT NULL,
    session TEXT NOT NULL,
    request TEXT NOT NULL,
    digest TEXT NOT NULL CHECK(length(digest)=64),
    resources TEXT NOT NULL,
    timeout INTEGER NOT NULL,
    legacy INTEGER NOT NULL,
    created REAL NOT NULL,
    deadline REAL NOT NULL,
    started REAL NOT NULL,
    slot INTEGER NOT NULL,
    original_result_sha256 TEXT NOT NULL CHECK(length(original_result_sha256)=64),
    decision TEXT NOT NULL CHECK(decision='retain_unknown_release_reservation'),
    operator_uid INTEGER NOT NULL CHECK(operator_uid>0),
    released_at REAL NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
    backup_sha256 TEXT NOT NULL CHECK(length(backup_sha256)=64),
    decision_sha256 TEXT NOT NULL CHECK(length(decision_sha256)=64),
    audit_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    invalidated_at REAL,
    invalidation_reason TEXT,
    CHECK((invalidated_at IS NULL AND invalidation_reason IS NULL)
          OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL))
)
"""
# Bind the decision to the *entire* immutable claim and uncertain receipt.
# An old release record cannot authorize a changed identity, resource claim
# or result. No older version will interpret this additional table as success.
MATCH = """
    r.job_id=j.id AND r.session=j.session AND r.request=j.request
    AND r.digest=j.digest AND r.resources=j.resources
    AND r.timeout=j.timeout AND r.legacy=j.legacy
    AND r.created=j.created AND r.deadline=j.deadline
    AND r.started=j.started AND r.slot=j.slot
    AND r.original_result_sha256=fb_receipt_sha256(j.result)
    AND j.script IS NULL AND j.finished IS NULL
    AND r.decision='retain_unknown_release_reservation'
    AND r.invalidated_at IS NULL
    AND r.operator_uid=fb_owner_uid() AND r.operator_uid>0
    AND r.released_at>=j.deadline AND r.released_at>=j.started+j.timeout+8
    AND r.evidence_sha256=fb_text_sha256(r.evidence_json)
    AND length(r.backup_sha256)=64 AND length(r.decision_sha256)=64
    AND fb_original_absent(j.id)=1
"""
HELD = f"""(j.state!='outcome_unknown' OR NOT EXISTS (
    SELECT 1 FROM {TABLE} AS r WHERE {MATCH}
))"""
ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")
REQUEST = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")
FIELDS = ("id", "session", "request", "digest", "resources", "timeout",
          "legacy", "created", "deadline", "started", "slot", "result",
          "state", "script", "finished")


def _text_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else None


def _receipt_hash(value):
    # Distinguish a missing receipt from every possible string. Do not
    # duplicate the private original receipt in the reconciliation table.
    if value is None:
        return hashlib.sha256(b"N").hexdigest()
    if isinstance(value, str):
        return hashlib.sha256(b"T" + value.encode("utf-8")).hexdigest()
    return None


def _original_absent(fbroot, job_id):
    # If original evidence returns after a decision, hold the reservation
    # again. The ordinary authentic-receipt recovery path can then inspect it.
    if not isinstance(job_id, str) or ID.fullmatch(job_id) is None:
        return 0
    try:
        _directory(fbroot, private=True)
        for root in _roots(fbroot):
            jobs_dir = root / "jobs"
            if not jobs_dir.exists() and not jobs_dir.is_symlink():
                continue
            _directory(jobs_dir, private=True)
            try:
                (jobs_dir / job_id).lstat()
            except FileNotFoundError:
                continue
            return 0
        return 1
    except (OSError, RuntimeError, ValueError):
        return 0


def install_sql_guards(db, fbroot):
    # These functions validate a durable decision in the same transaction
    # that claims work. Filesystem observations are deliberately not marked
    # deterministic.
    db.create_function("fb_receipt_sha256", 1, _receipt_hash, deterministic=True)
    db.create_function("fb_text_sha256", 1, _text_hash, deterministic=True)
    db.create_function("fb_owner_uid", 0, os.getuid, deterministic=True)
    db.create_function("fb_original_absent", 1,
                       lambda job_id: _original_absent(Path(fbroot), job_id))


def released(db, job_id):
    """Read only the decision's existence; never expose the evidence text."""
    return db.execute(f"""
        SELECT 1 FROM jobs AS j JOIN {TABLE} AS r ON {MATCH}
        WHERE j.id=? AND j.state='outcome_unknown'
    """, (job_id,)).fetchone() is not None


def invalidate_changed_decisions(db, now):
    """Persist a fail-closed decision invalidation before another dispatch."""
    db.execute(f"""
        UPDATE {TABLE} AS r
        SET invalidated_at=?, invalidation_reason='REVIEW_BINDING_OR_EVIDENCE_CHANGED'
        WHERE r.invalidated_at IS NULL AND EXISTS (
            SELECT 1 FROM jobs AS j WHERE j.id=r.job_id AND j.state='outcome_unknown'
            AND NOT COALESCE(({MATCH}), 0)
        )
    """, (now,))


def need(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def _directory(path, *, private=False):
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
         and not info.st_mode & 0o022
         and (not private or stat.S_IMODE(info.st_mode) == 0o700),
         "RECOVERY_UNTRUSTED_DIRECTORY")


def _file_info(info):
    need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
         and info.st_nlink == 1 and not info.st_mode & 0o077,
         "RECOVERY_UNTRUSTED_FILE")


def _read_private(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _file_info(info)
        need(info.st_size <= maximum, "RECOVERY_EVIDENCE_TOO_LARGE")
        data = stream.read(maximum + 1)
        need(len(data) == info.st_size and len(data) <= maximum,
             "RECOVERY_EVIDENCE_CHANGED")
        return data


@contextmanager
def _offline_lock(path, *, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        _file_info(os.fstat(fd))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("RECOVERY_PROFILE_OR_ENGINE_ACTIVE") from None
        info = os.fstat(fd)
        current = path.lstat()
        need((info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
             "RECOVERY_LOCK_REPLACED")
        yield (path, info.st_dev, info.st_ino)
    finally:
        os.close(fd)


def _check_lock_identities(locks):
    for path, dev, inode in locks:
        info = path.lstat()
        _file_info(info)
        need((info.st_dev, info.st_ino) == (dev, inode),
             "RECOVERY_LOCK_REPLACED")


def _roots(fbroot):
    lanes = fbroot / "lanes"
    yield fbroot
    if lanes.exists() or lanes.is_symlink():
        _directory(lanes)
        for lane in sorted(lanes.iterdir()):
            need(re.fullmatch(r"[1-8]", lane.name) is not None,
                 "RECOVERY_UNEXPECTED_LANE")
            _directory(lane, private=True)
            yield lane


def _safe_marker(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    _file_info(info)
    return True


def _check_original_evidence(roots, job_id):
    # This first maintenance operation only supports the missing-directory
    # case. A present original receipt, or any unfinished engine directory,
    # requires a different investigation. Never remove or fabricate markers.
    for root in roots:
        jobs_dir = root / "jobs"
        if not jobs_dir.exists() and not jobs_dir.is_symlink():
            continue
        _directory(jobs_dir, private=True)
        for entry in jobs_dir.iterdir():
            _directory(entry, private=True)
            need(entry.name != job_id,
                 "RECOVERY_ORIGINAL_EVIDENCE_PRESENT")
            need((_safe_marker(entry / "DONE") and
                  _safe_marker(entry / "result.env")) or
                 _safe_marker(entry / "dispatch.cancelled"),
                 "RECOVERY_OTHER_UNRESOLVED_ENGINE_JOB")


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json_bytes(data):
    return (json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


def _evidence(path, job_id, request, digest):
    data = _read_private(path, 4096)
    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, "RECOVERY_DUPLICATE_EVIDENCE_KEY")
            result[key] = value
        return result
    value = json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    expected = {
        "schema": 1, "job_id": job_id, "request_id": request,
        "command_sha256": digest, "reviewed_local_processes": True,
        "reviewed_external_effects": True, "no_pending_effects": True,
        "accept_unknown": True,
    }
    need(type(value) is dict and set(value) == set(expected) | {"review_reference"},
         "RECOVERY_EVIDENCE_SCHEMA_INVALID")
    for key, expected_value in expected.items():
        need(type(value[key]) is type(expected_value) and value[key] == expected_value,
             "RECOVERY_EVIDENCE_IDENTITY_OR_ATTESTATION_INVALID")
    need(isinstance(value["review_reference"], str)
         and REFERENCE.fullmatch(value["review_reference"]) is not None,
         "RECOVERY_EVIDENCE_REFERENCE_INVALID")
    return value, data


def _snapshot(db, destination, expected_row):
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(destination)) as target:
        db.backup(target, pages=256, sleep=0.01)
        need(target.execute("PRAGMA quick_check").fetchone() == ("ok",),
             "RECOVERY_BACKUP_INTEGRITY_FAILED")
        target.row_factory = sqlite3.Row
        row = target.execute("SELECT * FROM jobs WHERE id=?", (expected_row["id"],)).fetchone()
        need(row is not None and all(row[k] == expected_row[k] for k in FIELDS),
             "RECOVERY_BACKUP_IDENTITY_MISMATCH")
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())
    return _sha(destination)


def reconcile_offline(profile, *, job_id, request, digest, evidence_path, confirm):
    """Record a reviewed release while the local scheduler/engines are offline.

    The caller supplies a human confirmation callback. The CLI is the only
    supported caller in production. Locks prove the cooperating runtime is
    quiescent; a human must independently review detached or external effects.
    A missing receipt by itself is never authority to release a reservation.
    """
    need(os.getuid() != 0 and os.getuid() == os.geteuid(),
         "RECOVERY_RUN_AS_PROFILE_OWNER")
    need(isinstance(job_id, str) and ID.fullmatch(job_id) is not None
         and isinstance(request, str) and REQUEST.fullmatch(request) is not None
         and isinstance(digest, str) and DIGEST.fullmatch(digest) is not None,
         "RECOVERY_INVALID_IDENTITY")
    profile = Path(profile).absolute()
    need(profile.resolve(strict=True) == profile, "RECOVERY_NONCANONICAL_PROFILE")
    _directory(profile, private=True)
    fbroot = profile
    for part in ("home", ".local", "share", "framebridge", "fbterm"):
        fbroot /= part
        _directory(fbroot)
    _directory(fbroot, private=True)
    queue = fbroot / "queue-v2"
    _directory(queue, private=True)
    dbpath = queue / "jobs.sqlite3"
    _file_info(dbpath.lstat())
    value, raw = _evidence(Path(evidence_path), job_id, request, digest)
    evidence_hash = hashlib.sha256(raw).hexdigest()
    with ExitStack() as stack:
        locks = [stack.enter_context(_offline_lock(queue / "scheduler.lock"))]
        roots = list(_roots(fbroot))
        for root in roots:
            for name in ("controller.lock", "worker.lock"):
                locks.append(stack.enter_context(_offline_lock(root / name, create=True)))
        _check_lock_identities(locks)
        database_identity = (dbpath.stat().st_dev, dbpath.stat().st_ino)
        _check_original_evidence(roots, job_id)
        for name in ("-wal", "-shm", "-journal"):
            path = Path(str(dbpath) + name)
            if path.exists() or path.is_symlink():
                _file_info(path.lstat())
        uri = dbpath.as_uri()
        db = stack.enter_context(closing(sqlite3.connect(uri + "?mode=rw", uri=True, timeout=5)))
        db.row_factory = sqlite3.Row
        install_sql_guards(db, fbroot)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            need(tuple(db.execute("PRAGMA quick_check").fetchone()) == ("ok",),
                 "RECOVERY_DATABASE_INTEGRITY_FAILED")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            need(row is not None and row["state"] == "outcome_unknown"
                 and row["request"] == request and row["digest"] == digest
                 and row["script"] is None and row["finished"] is None
                 and type(row["slot"]) is int and 0 <= row["slot"] <= 8
                 and type(row["timeout"]) is int and 1 <= row["timeout"] <= 600
                 and all(isinstance(row[key], (int, float)) and math.isfinite(row[key])
                         for key in ("created", "deadline", "started"))
                 and time.time() >= max(row["deadline"], row["started"] + row["timeout"] + 8),
                 "RECOVERY_JOB_NOT_ELIGIBLE")
            need(db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','in_flight')").fetchone()[0] == 0,
                 "RECOVERY_OTHER_WORK_PENDING")
            table_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                      (TABLE,)).fetchone() is not None
            if table_exists:
                need(db.execute(f"SELECT 1 FROM {TABLE} WHERE job_id=?", (job_id,)).fetchone() is None,
                     "RECOVERY_DECISION_ALREADY_EXISTS")
            summary = {
                "job_id": job_id, "request_id": request, "command_sha256": digest,
                "evidence_sha256": evidence_hash, "review_reference": value["review_reference"],
                "decision": DECISION, "execution_state": "outcome_unknown",
                "do_not_replay": True,
            }
            need(confirm(dict(summary)) is True, "RECOVERY_CONFIRMATION_REQUIRED")
            # Keep all cooperating locks through the second inspection and
            # transaction. An operator's attestation is not a receipt.
            need(list(_roots(fbroot)) == roots, "RECOVERY_LANES_CHANGED")
            _check_lock_identities(locks)
            current_db = dbpath.lstat()
            _file_info(current_db)
            need((current_db.st_dev, current_db.st_ino) == database_identity,
                 "RECOVERY_DATABASE_REPLACED")
            _check_original_evidence(roots, job_id)
            need(_read_private(Path(evidence_path), 4096) == raw,
                 "RECOVERY_EVIDENCE_CHANGED")
            audit_root = queue / "reconciliation-evidence"
            audit_root.mkdir(mode=0o700, exist_ok=True)
            _directory(audit_root, private=True)
            audit = Path(tempfile.mkdtemp(prefix="review-", dir=audit_root))
            # Use an independent read connection while the write transaction
            # excludes other writers. The backup contains the predecision row.
            with closing(sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5)) as source:
                backup_hash = _snapshot(source, audit / "before.sqlite3", row)
            _write_new(audit / "evidence.json", raw)
            released_at = time.time()
            record = dict(summary, operator_uid=os.getuid(), released_at=released_at,
                          backup_sha256=backup_hash, audit_id=audit.name,
                          reservation_released=True)
            decision_bytes = _json_bytes(record)
            decision_hash = hashlib.sha256(decision_bytes).hexdigest()
            _write_new(audit / "decision.json", decision_bytes)
            _sync_dir(audit)
            _sync_dir(audit_root)
            _sync_dir(queue)
            db.execute(SCHEMA)
            values = {k: row[k] for k in ("session", "request", "digest", "resources",
                      "timeout", "legacy", "created", "deadline", "started", "slot")}
            values.update(job_id=job_id, original_result_sha256=_receipt_hash(row["result"]), decision=DECISION,
                          operator_uid=os.getuid(), released_at=released_at,
                          evidence_sha256=evidence_hash, backup_sha256=backup_hash,
                          decision_sha256=decision_hash, audit_id=audit.name,
                          evidence_json=raw.decode("utf-8"))
            fields = tuple(values)
            db.execute(f"INSERT INTO {TABLE} ({','.join(fields)}) VALUES ({','.join(':'+k for k in fields)})",
                       values)
            _check_lock_identities(locks)
            current_db = dbpath.lstat()
            _file_info(current_db)
            need((current_db.st_dev, current_db.st_ino) == database_identity,
                 "RECOVERY_DATABASE_REPLACED")
            need(released(db, job_id), "RECOVERY_DECISION_IDENTITY_MISMATCH")
            # The jobs row has not been updated. A crash before COMMIT leaves
            # the reservation held and retains any prepared audit directory.
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return dict(record, decision_sha256=decision_hash, ok=True,
                    outcome_proven=False, audit_directory=str(audit))
