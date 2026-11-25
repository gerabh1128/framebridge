"""Local, durable admission gate for preparing package maintenance.

A paused gate never cancels, stops or replays anything. Existing jobs drain.
It is not proof of host-wide quiescence, and does not bypass package leases.
"""
from contextlib import closing
import os
import sqlite3
import time

from . import activity_data as data

SCHEMA = """CREATE TABLE IF NOT EXISTS owner_update_gate (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    paused INTEGER NOT NULL CHECK(paused IN (0,1)),
    changed_at REAL NOT NULL)"""

def paused(db):
    row = db.execute("SELECT paused FROM owner_update_gate WHERE singleton=1").fetchone()
    return bool(row and row[0] == 1)

def snapshot(db):
    gate = db.execute("SELECT paused,changed_at FROM owner_update_gate WHERE singleton=1").fetchone()
    counts = dict(db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))
    allowed = {"completed", "not_started", "expired_before_start"}
    blocking = sum(n for state, n in counts.items() if state not in allowed)
    prepared = bool(gate and gate[0] == 1)
    rows = db.execute("""SELECT id,state FROM jobs WHERE state NOT IN
        ('completed','not_started','expired_before_start') ORDER BY created,id LIMIT 50""").fetchall()
    return {"ok": True, "admission_paused": prepared,
            "changed_at": gate[1] if gate else None,
            "queued": counts.get("queued", 0),
            "running": counts.get("in_flight", 0)+counts.get("running", 0),
            "outcome_unknown": counts.get("outcome_unknown", 0),
            "blocking_count": blocking,
            "blocking_jobs": [{"job_id": r[0], "state": r[1]} for r in rows],
            "queue_drained": prepared and blocking == 0,
            "package_replacement_authorized": False,
            "scope": "local-managed-exec-queue",
            "note": "Review detached effects, other profiles, TTY/CUA and supervisors. The cold package guard remains mandatory."}

def _control(database, action):
    if action not in ("status","prepare","resume"):
        raise ValueError("INVALID_UPDATE_ACTION")
    if action == "status":
        with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            return snapshot(db)
    with closing(sqlite3.connect(database.as_uri()+"?mode=rw", uri=True, timeout=3)) as db:
        # The submit path takes the same SQLite write transaction BEFORE its
        # admission decision. No new durable job can race past this transition.
        db.execute("BEGIN IMMEDIATE")
        current = paused(db)
        desired = action == "prepare"
        if current != desired:
            db.execute("""INSERT INTO owner_update_gate(singleton,paused,changed_at)
                VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET
                paused=excluded.paused,changed_at=excluded.changed_at""", (int(desired),time.time()))
        result = snapshot(db)
        db.commit()
        return result

def control(profile, action):
    data.require(os.getuid()!=0, "LOCAL_PROFILE_OWNER_REQUIRED")
    path = data._database(profile)
    try:
        return _control(path,action)
    except sqlite3.Error as exc:
        raise data.ObservationError("UPDATE_GATE_UNAVAILABLE; no quiescence verified") from exc
