"""Bounded, read-only activity observations for the desktop UI.

The desktop activity center observes only the owning local profile.
No remote administration, credential prompts or queue mutation.
"""
from __future__ import annotations
from contextlib import closing
from dataclasses import dataclass
import json
import math
import os
import pwd
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import uuid

FB = "/usr/bin/fb"
HISTORY_PAGE = 200
STATES = frozenset(("queued", "in_flight", "running", "completed",
                    "not_started", "expired_before_start", "outcome_unknown"))
JOB_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}")


class ObservationError(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise ObservationError(reason)


def safe_text(value, maximum=120):
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable())[:maximum]


def timestamp(value):
    return value if type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 4102444800 else None


def canonical_id(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) and str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Profile:
    path: Path
    node_id: str
    name: str
    role: str


def profile():
    from . import config, profiles
    p = profiles.selected() or (Path(pwd.getpwuid(os.getuid()).pw_dir) /
                               ".local/share/framebridge-expanded")
    return profile_at(p)


def profile_at(p):
    """Bind to the exact local profile selected by the supervisor."""
    from . import config, profiles
    p = Path(p)
    require((p / "config.json").is_file(), "PROFILE_NOT_CONFIGURED")
    p, ident = profiles._profile(p)
    c = config.load_config(p)
    return Profile(p, ident, c["name"], c["role"])


def check_profile(p):
    from . import config, profiles
    path, ident = profiles._profile(p.path, p.node_id)
    c = config.load_config(path)
    require(c["role"] == p.role and c["name"] == p.name, "PROFILE_CHANGED")
    return ident


def local_argv(p, *args):
    check_profile(p)
    return [FB, "--state-dir", str(p.path), *args]


def owner_environment():
    env = {k:v for k,v in os.environ.items()
           if not k.startswith(("FRAMEBRIDGE_", "FBTERM_")) and k not in
           ("PYTHONPATH","PYTHONHOME","TMUX","TMUX_TMPDIR")}
    env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    return env


def query(argv, *, timeout=12):
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                           env=owner_environment(), timeout=timeout, check=False)
        require(r.returncode == 0, "COMMAND_REJECTED_OR_UNAVAILABLE")
        require(len(r.stdout) <= 8 * 1024 * 1024, "RESPONSE_TOO_LARGE")
        d = json.loads(r.stdout)
        require(type(d) is dict and d.get("product") == "FrameBridge"
                and d.get("ok") is True and "result" in d, "RESPONSE_UNVERIFIED")
        result = d["result"]
        if isinstance(result, dict):
            require(result.get("ok") is not False, "OPERATION_REJECTED")
        return result
    except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError):
        raise ObservationError("QUERY_UNAVAILABLE") from None


def local_status(p):
    d = query(local_argv(p, "--json", "status"))
    r = d.get("runtime", {})
    require(d.get("role") == p.role and r.get("node_id") == p.node_id, "PROFILE_BINDING_MISMATCH")
    boot = d.get("boot", {})
    conn = d.get("connection", {})
    worker = d.get("worker", {})
    running = r.get("running") is True
    ready = running and r.get("ready") is True
    contact = r.get("node_connection", {}).get("broker_acceptance_fresh") is True
    return {"name": p.name, "role": p.role, "running": running, "ready": ready,
            "boot": boot.get("enabled"), "managed": boot.get("configured") is True,
            "worker": safe_text(worker.get("state"), 70),
            "connection": ("fresh" if contact else "unverified") if p.role == "node"
                else ("verified" if conn.get("verified") is True and conn.get("alive") is True else "unverified"),
            "display": safe_text(r.get("display_context", {}).get("kind")),
            "updated": time.time()}


def _database(p):
    check_profile(p)
    base = p.path
    for part in ("home", ".local", "share", "framebridge", "fbterm", "queue-v2"):
        base /= part
        info = base.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o022, "QUEUE_DIRECTORY_UNSAFE")
    require(stat.S_IMODE(base.stat().st_mode) == 0o700, "QUEUE_DIRECTORY_UNSAFE")
    db = base / "jobs.sqlite3"
    info = db.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and not info.st_mode & 0o077, "QUEUE_FILE_UNSAFE")
    return db


def _job(row):
    ident = row.get("job_id", row.get("id"))
    require(isinstance(ident, str) and JOB_ID.fullmatch(ident), "JOB_ID_UNVERIFIED")
    state = row.get("execution_state", row.get("state"))
    if state not in STATES:
        state = "unverified"
    ec = row.get("exit_code")
    code = str(ec) if type(ec) in (str, int) and re.fullmatch(r"-?[0-9]{1,6}", str(ec)) else None
    return {"id": ident, "state": state, "created": timestamp(row.get("created_at", row.get("created"))),
            "started": timestamp(row.get("started_at", row.get("started"))),
            "finished": timestamp(row.get("finished_at", row.get("finished"))),
            "exit_code": code}


def local_jobs(p, offset=0):
    require(type(offset) is int and 0 <= offset <= 1000000, "INVALID_HISTORY_OFFSET")
    db = _database(p)
    try:
        with closing(sqlite3.connect(db.as_uri()+"?mode=ro", uri=True, timeout=.5)) as c:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA query_only=ON")
            c.execute("BEGIN")
            counts = dict(c.execute("SELECT state,count(*) FROM jobs GROUP BY state"))
            sql = """SELECT id,state,created,started,finished,
                CASE WHEN json_valid(result) THEN json_extract(result,'$.exit_code') END AS exit_code
                FROM jobs ORDER BY (state IN ('queued','in_flight','outcome_unknown','running')) DESC,
                created DESC,id LIMIT ? OFFSET ?"""
            rows = [_job(dict(x)) for x in c.execute(sql, (HISTORY_PAGE, offset))]
            c.rollback()
        return {"jobs": rows, "counts": {k: v for k,v in counts.items() if k in STATES},
                "other": sum(v for k,v in counts.items() if k not in STATES),
                "total": sum(counts.values()), "complete_counts": True, "offset": offset,
                "updated": time.time()}
    except sqlite3.Error:
        raise ObservationError("QUEUE_UNAVAILABLE") from None


def verified_output(result,node_id,job_id):
    require(result.get("node_id")==node_id and result.get("job_id")==job_id,
            "RECEIPT_IDENTITY_MISMATCH")
    require(result.get("execution_state")=="completed"
            and result.get("stdout_integrity")=="verified"
            and result.get("stdout_sanitized") is True
            and isinstance(result.get("stdout"),str), "SAFE_OUTPUT_UNAVAILABLE")
    value = result["stdout"]
    require(len(value.encode("utf-8")) <= 262144, "OUTPUT_TOO_LARGE")
    return {"text":"".join(c for c in value if c.isprintable() or c in "\n\t"),
            "truncated":result.get("stdout_truncated") is True}


def job_output(p,job_id):
    require(isinstance(job_id,str) and JOB_ID.fullmatch(job_id), "JOB_ID_UNVERIFIED")
    check_profile(p)
    r = query(local_argv(p,"--json","nodes","jobs","--target","LOCAL",
                         "--job-id",job_id,"--include-stdout"),timeout=28)
    return verified_output(r,p.node_id,job_id)


def activity_indicator(status, history):
    """Return a factual local tray state, never infer idle from unavailable data."""
    if not status:
        return "attention", "State unverified"
    if not status.get("running"):
        return "stopped", "Stopped"
    if not status.get("ready"):
        return "attention", "Starting"
    if history is None:
        return "attention", "Activity unverified"
    counts=history.get("counts",{})
    if history.get("other") or counts.get("outcome_unknown",0):
        return "attention", "Unconfirmed work"
    if counts.get("in_flight",0) or counts.get("running",0):
        return "busy", "Running"
    if counts.get("queued",0):
        return "attention", "Queued work"
    if status.get("role")=="node" and status.get("connection")!="fresh":
        return "attention", "Connection unverified"
    return "ready", "Ready"


def service(p, action):
    require(action in ("start","stop","restart"), "INVALID_SERVICE_ACTION")
    status = local_status(p)
    require(status["managed"], "INITIAL_SETUP_REQUIRES_TERMINAL")
    if action in ("stop","restart"):
        j = local_jobs(p)
        require(not j.get("other") and not any(j["counts"].get(s,0) for s in ("queued","running","in_flight","outcome_unknown")),
                "WORK_ACTIVE_OR_UNCONFIRMED")
        r = query(local_argv(p,"--json","service","stop"),timeout=65)
        require(r.get("state") == "stopped" and r.get("ok") is True,"STOP_NOT_CONFIRMED")
        if action == "stop":
            return "stopped"
        require(local_status(p)["running"] is False,"STOP_NOT_CONFIRMED")
    r = query(local_argv(p,"--json","service","start"),timeout=65)
    require(r.get("ok") is True and r.get("state") in ("started","start_requested"),"START_NOT_ACCEPTED")
    return "start_requested"


def local_transcript(p):
    """Only the existing local read-only session; no engine start or input."""
    check_profile(p)
    from . import runtime, viewer, config
    env = runtime._observation_environment(p.path, config.load_config(p.path))
    _,root,_,_ = viewer._profile(env)
    tmux = viewer._tmux(env)
    require(viewer._session(tmux,env) and viewer._worker_present(root,tmux,env), "ORIGINAL_VIEWER_UNAVAILABLE")
    out = viewer._run(tmux+["capture-pane","-p","-J","-S","-1500","-t",viewer.SESSION+":0.0"],env,timeout=3)
    require(out.returncode == 0,"ORIGINAL_VIEWER_UNAVAILABLE")
    return "".join(c for c in out.stdout[-262144:] if c.isprintable() or c in "\n\t")
