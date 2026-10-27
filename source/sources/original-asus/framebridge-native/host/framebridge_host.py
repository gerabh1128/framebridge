#!/usr/bin/env python3
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import time
from pathlib import Path
from framebridge_voice_command import start_listen_once

EXEC_SESSION = os.environ.get("FRAMEBRIDGE_EXEC_SESSION", "framebridge")
RESCUE_SESSION = os.environ.get("FRAMEBRIDGE_RESCUE_SESSION", "framebridge-rescue")
STATE_ROOT = Path.home() / ".local" / "share" / "framebridge"
JOBS_ROOT = STATE_ROOT / "jobs-v03"
RESET_GENERATION_PATH = STATE_ROOT / "exec-reset-generation"
UI_HELPER_ROOT = Path(__file__).resolve().parent

# No implicit laboratory destinations are shipped.
# Explicit integrator configuration may populate this compatibility map.
CLASSIC_NODE_TARGETS = {}

NODE_TARGET_RE = re.compile(r"^[A-Z0-9._:-]{1,64}$")

READONLY_PROGRAMS = {
    "pwd", "whoami", "id", "uname", "hostname", "date", "uptime",
    "free", "df", "ls", "stat", "cat", "head", "tail", "grep", "rg",
    "find", "ps", "pgrep", "lspci", "lsusb", "ip", "ss", "journalctl",
    "ollama", "nvidia-smi", "git", "sha256sum", "du", "file", "readlink",
    "realpath", "wc", "sed", "awk", "framebridge-node-ro",
}

DANGEROUS = re.compile(
    r"(^|[\s;&|])(?:sudo|su|rm|dd|mkfs(?:\.\w+)?|fdisk|parted|sfdisk|"
    r"wipefs|reboot|shutdown|poweroff)\b"
    r"|\b(?:chmod|chown|chgrp)\s+-R\b"
    r"|\bsystemctl\s+(?:start|stop|restart|enable|disable|mask|unmask)\b"
    r"|\b(?:apt|apt-get|dnf|yum|pacman)\s+(?:remove|purge|autoremove|"
    r"upgrade|dist-upgrade)\b"
    r"|\bgit\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+--force)\b",
    re.I,
)

def read_message():
    raw = sys.stdin.buffer.read(4)
    if not raw:
        return None
    n = struct.unpack("<I", raw)[0]
    return json.loads(sys.stdin.buffer.read(n).decode("utf-8"))

def send_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()

def tmux_has_session(name):
    return subprocess.call(
        ["tmux", "has-session", "-t", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) == 0

def ensure_session(name):
    if not tmux_has_session(name):
        subprocess.check_call(["tmux", "new-session", "-d", "-s", name])

def ensure_infrastructure():
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_session(EXEC_SESSION)
    ensure_session(RESCUE_SESSION)

def read_reset_generation():
    try:
        return RESET_GENERATION_PATH.read_text(
            encoding="utf-8"
        ).strip()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""

def bump_reset_generation():
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    token = f"{time.time_ns()}-{os.getpid()}"
    tmp = RESET_GENERATION_PATH.with_name(
        RESET_GENERATION_PATH.name + f".tmp-{os.getpid()}"
    )
    tmp.write_text(token + "\n", encoding="utf-8")
    os.replace(tmp, RESET_GENERATION_PATH)
    return token

def route_classic_node(msg):
    target = str(msg.get("target", "")).upper()
    node = CLASSIC_NODE_TARGETS.get(target)
    if node is None or not NODE_TARGET_RE.fullmatch(target):
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_TARGET_UNKNOWN", "channel": "NODE-CLASSIC"}

    mode = str(msg.get("mode", "")).upper()
    lang = str(msg.get("lang", "")).upper()
    authorized = str(msg.get("authorized", "NO")).upper()
    frame_id = str(msg.get("id", ""))
    body_text = str(msg.get("command", "")).strip()

    if mode != "EXEC" or lang != "FRAMEBRIDGE-CAP-V1" or authorized != "NO":
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_CONTRACT_REJECT", "channel": "NODE-CLASSIC"}
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", frame_id):
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_ID_INVALID", "channel": "NODE-CLASSIC"}
    if len(body_text.encode("utf-8")) > 32768:
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_BODY_TOO_LARGE", "channel": "NODE-CLASSIC"}
    try:
        body = json.loads(body_text)
    except Exception:
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_BODY_JSON_INVALID", "channel": "NODE-CLASSIC"}
    if not isinstance(body, dict):
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_BODY_NOT_OBJECT", "channel": "NODE-CLASSIC"}
    if any(k in body for k in ("script", "shell", "command")):
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_RAW_SHELL_FORBIDDEN", "channel": "NODE-CLASSIC"}

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    dedup_root = STATE_ROOT / "node-classic-dedup"
    dedup_root.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9._:-]", "_", frame_id)[:128]
    dedup_path = dedup_root / (target + "--" + safe_id + ".json")
    signature = __import__("hashlib").sha256((target + "\n" + canonical).encode("utf-8")).hexdigest()

    claim = {"state": "pending", "signature": signature, "target": target, "id": frame_id, "created_at": time.time()}
    try:
        fd = os.open(str(dedup_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(claim, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
    except FileExistsError:
        try:
            previous = json.loads(dedup_path.read_text(encoding="utf-8"))
        except Exception:
            return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_DEDUP_STATE_INVALID", "channel": "NODE-CLASSIC"}
        if previous.get("signature") != signature:
            return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_ID_REUSE_MISMATCH", "channel": "NODE-CLASSIC"}
        if previous.get("state") == "done" and isinstance(previous.get("response"), dict):
            cached = dict(previous["response"])
            cached["channel"] = "NODE-CLASSIC-CACHED"
            return cached
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_REQUEST_IN_PROGRESS", "channel": "NODE-CLASSIC"}

    frame = "\n".join([
        "FRAMEEXEC/1",
        "ID=" + frame_id,
        "TARGET=" + node["seed_target"],
        "MODE=EXEC",
        "LANG=framebridge-cap-v1",
        "",
        canonical,
        "FRAMEEND/1",
        "",
    ])
    ssh = [
        "ssh", "-p", str(node["port"]), "-i", node["identity_file"],
        "-oBatchMode=yes", "-oIdentitiesOnly=yes", "-oStrictHostKeyChecking=yes",
        "-oUserKnownHostsFile=" + node["known_hosts_file"], "-oConnectTimeout=5",
        node["user"] + "@" + node["host"],
        'export PATH="$HOME/.local/bin:$PATH"; framebridge classic',
    ]
    try:
        proc = subprocess.run(ssh, input=frame, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        try: dedup_path.unlink()
        except OSError: pass
        return {"executed": False, "exit_code": 124, "output": "", "error": "CLASSIC_NODE_TIMEOUT", "channel": "NODE-CLASSIC"}
    except Exception as exc:
        try: dedup_path.unlink()
        except OSError: pass
        return {"executed": False, "exit_code": None, "output": "", "error": "CLASSIC_NODE_TRANSPORT_ERROR:" + type(exc).__name__, "channel": "NODE-CLASSIC"}

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        try: dedup_path.unlink()
        except OSError: pass
        return {"executed": False, "exit_code": proc.returncode, "output": out, "error": "CLASSIC_NODE_TRANSPORT_FAILED", "channel": "NODE-CLASSIC"}
    if not out.startswith("FRAMERESULT/1") or "CHANNEL=SEED-CLASSIC-TTY" not in out:
        try: dedup_path.unlink()
        except OSError: pass
        return {"executed": False, "exit_code": 3, "output": out, "error": "CLASSIC_NODE_RESULT_INVALID", "channel": "NODE-CLASSIC"}
    response = {"executed": True, "exit_code": 0, "output": out, "error": "", "channel": "NODE-CLASSIC"}
    final_state = {"state": "done", "signature": signature, "target": target, "id": frame_id, "response": response, "completed_at": time.time()}
    tmp = dedup_path.with_name(dedup_path.name + ".tmp-" + str(os.getpid()))
    tmp.write_text(json.dumps(final_state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, dedup_path)
    return response


PLAN_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
PLAN_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

def _node_ssh_argv(node, remote_command):
    return [
        "ssh", "-p", str(node["port"]), "-i", node["identity_file"],
        "-oBatchMode=yes", "-oIdentitiesOnly=yes", "-oStrictHostKeyChecking=yes",
        "-oUserKnownHostsFile=" + node["known_hosts_file"], "-oConnectTimeout=5",
        node["user"] + "@" + node["host"], remote_command,
    ]

def _node_cli_json(node, remote_command, stdin_text=None, timeout=20):
    try:
        proc = subprocess.run(
            _node_ssh_argv(node, remote_command),
            input=stdin_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "NODEPLAN_TIMEOUT", 124
    except Exception as exc:
        return None, "NODEPLAN_TRANSPORT_ERROR:" + type(exc).__name__, 1
    if proc.returncode != 0:
        try:
            value = json.loads((proc.stdout or "").strip())
            return value, str(value.get("error") or "NODEPLAN_REMOTE_ERROR"), proc.returncode
        except Exception:
            return None, "NODEPLAN_TRANSPORT_FAILED", proc.returncode
    try:
        return json.loads((proc.stdout or "").strip()), "", 0
    except Exception:
        return None, "NODEPLAN_RESULT_JSON_INVALID", 3

def route_classic_plan(msg):
    target = str(msg.get("target", "")).upper()
    node = CLASSIC_NODE_TARGETS.get(target)
    if node is None:
        return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_TARGET_UNKNOWN", "channel": "NODE-PLAN"}
    if (
        str(msg.get("mode", "")).upper() != "EXEC"
        or str(msg.get("lang", "")).upper() != "FRAMEBRIDGE-PLAN-V1"
        or str(msg.get("authorized", "NO")).upper() != "NO"
    ):
        return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_CONTRACT_REJECT", "channel": "NODE-PLAN"}

    frame_id = str(msg.get("id", ""))
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", frame_id):
        return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_ID_INVALID", "channel": "NODE-PLAN"}

    raw = str(msg.get("command", "")).strip()
    if len(raw.encode("utf-8")) > 65536:
        return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_BODY_TOO_LARGE", "channel": "NODE-PLAN"}
    try:
        body = json.loads(raw)
    except Exception:
        return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_BODY_JSON_INVALID", "channel": "NODE-PLAN"}
    if not isinstance(body, dict):
        return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_BODY_NOT_OBJECT", "channel": "NODE-PLAN"}

    op = str(body.get("op") or "")
    if op not in {"plan.import", "plan.status", "plan.apply", "plan.rollback"}:
        return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_OP_NOT_ALLOWED", "channel": "NODE-PLAN"}

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    dedup_root = STATE_ROOT / "node-plan-dedup"
    dedup_root.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9._:-]", "_", frame_id)[:128]
    dedup_path = dedup_root / (target + "--" + safe_id + ".json")
    signature = __import__("hashlib").sha256((target + "\n" + canonical).encode("utf-8")).hexdigest()

    try:
        fd = os.open(str(dedup_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {"state": "pending", "signature": signature, "target": target, "id": frame_id, "op": op, "created_at": time.time()},
                fh,
                sort_keys=True,
                separators=(",", ":"),
            )
            fh.write("\n")
    except FileExistsError:
        try:
            previous = json.loads(dedup_path.read_text(encoding="utf-8"))
        except Exception:
            return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_DEDUP_STATE_INVALID", "channel": "NODE-PLAN"}
        if previous.get("signature") != signature:
            return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_ID_REUSE_MISMATCH", "channel": "NODE-PLAN"}
        if previous.get("state") == "done" and isinstance(previous.get("response"), dict):
            cached = dict(previous["response"])
            cached["channel"] = "NODE-PLAN-CACHED"
            return cached
        return {"executed": False, "exit_code": None, "output": "", "error": "NODEPLAN_REQUEST_IN_PROGRESS", "channel": "NODE-PLAN"}

    def fail(error, rc=2, output=""):
        try:
            dedup_path.unlink()
        except OSError:
            pass
        return {"executed": False, "exit_code": rc, "output": output, "error": error, "channel": "NODE-PLAN"}

    def finish(value):
        response = {
            "executed": True,
            "exit_code": 0,
            "output": json.dumps(value, sort_keys=True, separators=(",", ":")),
            "error": "",
            "channel": "NODE-PLAN",
        }
        final_state = {
            "state": "done",
            "signature": signature,
            "target": target,
            "id": frame_id,
            "op": op,
            "response": response,
            "completed_at": time.time(),
        }
        tmp = dedup_path.with_name(dedup_path.name + ".tmp-" + str(os.getpid()))
        tmp.write_text(json.dumps(final_state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dedup_path)
        return response

    if op == "plan.import":
        if set(body) != {"op", "plan"} or not isinstance(body.get("plan"), dict):
            return fail("NODEPLAN_IMPORT_SCHEMA_INVALID")
        plan = body["plan"]
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if (
            not isinstance(steps, list)
            or len(steps) != 1
            or not isinstance(steps[0], dict)
            or steps[0].get("capability") not in {"workspace.write_text", "package.install"}
        ):
            return fail("NODEPLAN_CAPABILITY_NOT_ALLOWED")
        value, error, rc = _node_cli_json(
            node,
            'export PATH="$HOME/.local/bin:$PATH"; framebridge plan import-stdin',
            json.dumps(plan, separators=(",", ":")) + "\n",
        )
        if rc != 0 or not isinstance(value, dict) or value.get("ok") is not True:
            return fail(error or "NODEPLAN_IMPORT_FAILED", rc or 2, json.dumps(value) if value else "")
        return finish(value)

    plan_id = str(body.get("plan_id") or "")
    if not PLAN_ID_SAFE_RE.fullmatch(plan_id):
        return fail("NODEPLAN_PLAN_ID_INVALID")

    value, error, rc = _node_cli_json(
        node,
        'export PATH="$HOME/.local/bin:$PATH"; framebridge plan show ' + plan_id,
    )
    if rc != 0 or not isinstance(value, dict) or value.get("ok") is not True:
        return fail(error or "NODEPLAN_STATUS_FAILED", rc or 2, json.dumps(value) if value else "")

    if op == "plan.status":
        if set(body) != {"op", "plan_id"}:
            return fail("NODEPLAN_STATUS_SCHEMA_INVALID")
        return finish(value)

    if set(body) != {"op", "plan_id", "plan_sha256"}:
        return fail("NODEPLAN_CHANGE_SCHEMA_INVALID")
    expected = str(body.get("plan_sha256") or "")
    if not PLAN_SHA_RE.fullmatch(expected):
        return fail("NODEPLAN_PLAN_SHA_INVALID")
    if value.get("plan_sha256") != expected:
        return fail("NODEPLAN_PLAN_SHA_MISMATCH")

    plan = value.get("plan") or {}
    steps = plan.get("steps") or []
    if len(steps) != 1 or steps[0].get("capability") not in {"workspace.write_text", "package.install"}:
        return fail("NODEPLAN_CAPABILITY_NOT_ALLOWED")
    capability = steps[0].get("capability")

    if op == "plan.apply":
        approval = value.get("approval") or {}
        base_ok = (
            value.get("state") == "APPROVED"
            and approval.get("one_shot") is True
            and approval.get("consumed") is False
            and approval.get("plan_sha256") == expected
            and value.get("apply") is None
        )
        if capability == "workspace.write_text":
            auth_ok = approval.get("source") == "LOCAL_TTY"
        else:
            auth_ok = (
                approval.get("source") == "REMOTE_HUMAN_TOTP"
                and approval.get("auth_method") == "totp-sha1-6-30"
                and isinstance(approval.get("key_id"), str)
                and bool(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", approval.get("key_id") or ""))
                and isinstance(approval.get("challenge_id"), str)
                and bool(re.fullmatch(r"[0-9a-f]{32}", approval.get("challenge_id") or ""))
                and isinstance(approval.get("totp_step"), int)
            )
        if not (base_ok and auth_ok):
            return fail("NODEPLAN_APPLY_PRECONDITION_FAILED")

        changed, error, rc = _node_cli_json(
            node,
            'export PATH="$HOME/.local/bin:$PATH"; framebridge plan apply ' + plan_id,
        )
        if rc != 0 or not isinstance(changed, dict) or changed.get("ok") is not True:
            return fail(error or "NODEPLAN_APPLY_FAILED", rc or 2, json.dumps(changed) if changed else "")
        return finish(changed)

    rollback = value.get("rollback") or {}
    rollback_approval = value.get("rollback_approval") or {}
    base_rb_ok = (
        value.get("state") == "APPLIED"
        and rollback.get("available") is True
        and rollback_approval.get("one_shot") is True
        and rollback_approval.get("consumed") is False
        and rollback_approval.get("plan_sha256") == expected
    )
    if capability == "workspace.write_text":
        rb_auth_ok = rollback_approval.get("source") == "LOCAL_TTY"
    else:
        rb_auth_ok = (
            rollback_approval.get("source") == "REMOTE_HUMAN_TOTP"
            and rollback_approval.get("auth_method") == "totp-sha1-6-30"
            and isinstance(rollback_approval.get("key_id"), str)
            and bool(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", rollback_approval.get("key_id") or ""))
            and isinstance(rollback_approval.get("challenge_id"), str)
            and bool(re.fullmatch(r"[0-9a-f]{32}", rollback_approval.get("challenge_id") or ""))
            and isinstance(rollback_approval.get("totp_step"), int)
        )
    if not (base_rb_ok and rb_auth_ok):
        return fail("NODEPLAN_ROLLBACK_PRECONDITION_FAILED")

    changed, error, rc = _node_cli_json(
        node,
        'export PATH="$HOME/.local/bin:$PATH"; framebridge plan rollback ' + plan_id,
    )
    if rc != 0 or not isinstance(changed, dict) or changed.get("ok") is not True:
        return fail(error or "NODEPLAN_ROLLBACK_FAILED", rc or 2, json.dumps(changed) if changed else "")
    return finish(changed)


def first_program(command):
    try:
        toks = shlex.split(command, posix=True)
    except ValueError:
        return ""
    while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
        toks.pop(0)
    return os.path.basename(toks[0]) if toks else ""

def classify(mode, lang, code, authorized):
    mode = (mode or "READONLY").upper()
    lang = (lang or "BASH").upper()

    if lang not in {"BASH", "PYTHON"}:
        return False, "LANG no soportado"

    if DANGEROUS.search(code) and not authorized:
        return False, (
            "Operacion potencialmente destructiva/privilegiada bloqueada. "
            "Requiere autorizacion explicita y AUTHORIZED=YES."
        )

    if mode == "READONLY":
        if lang != "BASH":
            return False, "READONLY solo admite BASH"
        if "\n" in code.strip():
            return False, "READONLY solo admite una orden simple"
        if any(x in code for x in [";", "&&", "||", "|", "$(", "`", ">", ">>"]):
            return False, "READONLY no admite composicion de shell"
        prog = first_program(code)
        if prog not in READONLY_PROGRAMS:
            return False, f"Programa fuera de allowlist READONLY: {prog or '(vacio)'}"
        return True, "READONLY permitido"

    if mode == "EXEC":
        return True, "EXEC permitido"

    return False, f"MODE no soportado: {mode}"

def safe_job_id(job_id):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", job_id)[:72] or "job"

def create_job(job_id, lang, code):
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    token = f"{safe_job_id(job_id)}-{os.getpid()}-{int(time.time()*1000)}"
    jobdir = JOBS_ROOT / token
    jobdir.mkdir(mode=0o700)

    suffix = ".py" if lang == "PYTHON" else ".sh"
    code_path = jobdir / f"payload{suffix}"
    output_path = jobdir / "output.txt"
    rc_path = jobdir / "exit_code.txt"
    done_path = jobdir / "done"
    wrapper_path = jobdir / "wrapper.sh"

    code_path.write_text(code + ("" if code.endswith("\n") else "\n"), encoding="utf-8")
    os.chmod(code_path, 0o700)

    runner = (
        f"python3 {shlex.quote(str(code_path))}"
        if lang == "PYTHON"
        else f"bash {shlex.quote(str(code_path))}"
    )

    wrapper = f'''#!/usr/bin/env bash
set +e
export PAGER=cat
export GIT_PAGER=cat
export SYSTEMD_PAGER=cat
export MANPAGER=cat
export GIT_TERMINAL_PROMPT=0
export PYTHONUNBUFFERED=1
cd {shlex.quote(str(Path.home()))}

printf '\\n=== FRAMEBRIDGE JOB {token} ===\\n'
{runner} 2>&1 | tee {shlex.quote(str(output_path))}
rc=${{PIPESTATUS[0]}}
printf '%s\\n' "$rc" > {shlex.quote(str(rc_path))}
touch {shlex.quote(str(done_path))}
printf '=== FRAMEBRIDGE DONE rc=%s ===\\n' "$rc"

exec bash --noprofile --norc
'''
    wrapper_path.write_text(wrapper, encoding="utf-8")
    os.chmod(wrapper_path, 0o700)

    return {
        "token": token,
        "jobdir": jobdir,
        "output": output_path,
        "rc": rc_path,
        "done": done_path,
        "wrapper": wrapper_path,
    }

def prune_old_windows():
    try:
        out = subprocess.check_output(
            ["tmux", "list-windows", "-t", EXEC_SESSION, "-F", "#{window_index}\t#{window_name}"],
            text=True,
            errors="replace",
        )
    except Exception:
        return
    jobs = []
    for line in out.splitlines():
        try:
            idx, name = line.split("\t", 1)
        except ValueError:
            continue
        if name.startswith("fb-"):
            jobs.append((int(idx), name))
    jobs.sort()
    for idx, _ in jobs[:-8]:
        subprocess.call(
            ["tmux", "kill-window", "-t", f"{EXEC_SESSION}:{idx}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

def run_isolated(job_id, lang, code, timeout_s):
    ensure_infrastructure()
    reset_generation = read_reset_generation()
    job = create_job(job_id, lang, code)
    win_name = ("fb-" + safe_job_id(job_id))[:45]

    subprocess.check_call([
        "tmux", "new-window", "-d",
        "-t", f"{EXEC_SESSION}:",
        "-n", win_name,
        "bash", str(job["wrapper"]),
    ])

    subprocess.call(
        ["tmux", "select-window", "-t", f"{EXEC_SESSION}:{win_name}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        current_generation = read_reset_generation()

        if current_generation != reset_generation:
            out = ""
            if job["output"].exists():
                out = job["output"].read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            return 130, (
                out[-44000:]
                + "\nABORTED_BY_RESET_EXEC: trabajo aislado terminado"
            ).strip(), job["token"]

        if job["done"].exists():
            rc = 1
            if job["rc"].exists():
                try:
                    rc = int(job["rc"].read_text(encoding="utf-8").strip())
                except Exception:
                    rc = 1
            out = ""
            if job["output"].exists():
                out = job["output"].read_text(encoding="utf-8", errors="replace")
            prune_old_windows()
            return rc, out[-48000:], job["token"]

        time.sleep(0.20)

    subprocess.call(
        ["tmux", "kill-window", "-t", f"{EXEC_SESSION}:{win_name}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    out = ""
    if job["output"].exists():
        out = job["output"].read_text(encoding="utf-8", errors="replace")
    return 124, (out[-44000:] + "\nTIMEOUT: trabajo aislado terminado por FrameBridge").strip(), job["token"]

def control_status():
    lines = []
    for name in (EXEC_SESSION, RESCUE_SESSION):
        exists = tmux_has_session(name)
        lines.append(f"SESSION {name}={'UP' if exists else 'DOWN'}")
        if exists:
            try:
                out = subprocess.check_output(
                    ["tmux", "list-windows", "-t", name,
                     "-F", "#{window_index}:#{window_name}:active=#{window_active}:panes=#{window_panes}"],
                    text=True,
                    errors="replace",
                ).strip()
                if out:
                    lines.append(out)
            except Exception as exc:
                lines.append(f"ERROR list-windows {name}: {exc!r}")
    return "\n".join(lines)

def control_reset_exec():
    # RESCUE is an invariant of RESET_EXEC.
    ensure_session(RESCUE_SESSION)

    generation = bump_reset_generation()

    if tmux_has_session(EXEC_SESSION):
        subprocess.call(
            ["tmux", "kill-session", "-t", EXEC_SESSION],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    ensure_session(EXEC_SESSION)
    ensure_session(RESCUE_SESSION)

    return (
        "RESET_EXEC=PASS\n"
        f"RESET_GENERATION={generation}\n"
        + control_status()
    )

def control_ensure():
    ensure_infrastructure()
    return "ENSURE=PASS\n" + control_status()

def handle_control(action):
    action = (action or "").upper()
    if action == "STATUS":
        return 0, control_status()
    if action == "ENSURE":
        return 0, control_ensure()
    if action == "RESET_EXEC":
        return 0, control_reset_exec()
    return 2, f"ACTION no soportada: {action}"

def _cua_runtime_journal(event, msg=None, rc=None, error_code=""):
    try:
        payload = msg if isinstance(msg, dict) else {}
        base = Path.home() / ".local/state/framebridge/cua-runtime"
        base.mkdir(parents=True, exist_ok=True)
        path = base / "cua-runtime.log"
        record = {
            "time": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
            "event": str(event)[:64],
            "id": str(payload.get("id", ""))[:128],
            "action": str(payload.get("action", ""))[:32],
            "semantic": str(
                payload.get(
                    "semantic_action",
                    payload.get("semantic", "")
                )
            )[:64],
            "authorized": (
                "YES"
                if str(payload.get("authorized", "NO")).upper() == "YES"
                else "NO"
            ),
            "rc": rc,
            "error_code": str(error_code)[:128],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    record,
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    except Exception:
        pass



def run_ui_helper(name, action):
    helper = UI_HELPER_ROOT / name

    if not helper.is_file():
        return 70, "UI_HELPER_NOT_FOUND"

    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault(
        "XAUTHORITY",
        str(Path.home() / ".Xauthority"),
    )

    try:
        proc = subprocess.run(
            [str(helper), str(action)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 71, "UI_HELPER_TIMEOUT"
    except Exception as exc:
        return 72, "UI_HELPER_ERROR:" + type(exc).__name__

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    detail = out if out else err

    return proc.returncode, detail[:4000]


def handle_ui_request(msg, op):
    target = str(msg.get("target", ""))
    authorized = str(msg.get("authorized", "")).upper()
    action = str(msg.get("action", "")).upper()

    if target != "ASUS":
        return 73, "UI_TARGET_MISMATCH"

    if authorized != "YES":
        return 74, "UI_AUTHORIZATION_REQUIRED"

    if op == "progress_console":
        if action not in {"ENSURE", "FOCUS", "STATUS"}:
            return 75, "PROGRESS_ACTION_INVALID"
        return run_ui_helper(
            "open-progress-console.sh",
            action,
        )

    if op == "interactive_terminal":
        if action not in {"OPEN", "ENSURE", "FOCUS", "STATUS", "CLOSE", "TOGGLE"}:
            return 76, "TERMINAL_ACTION_INVALID"
        return run_ui_helper(
            "open-interactive-terminal.sh",
            action,
        )

    return 77, "UI_OPERATION_UNSUPPORTED"

def main():
    while True:
        msg = read_message()
        if msg is None:
            break

        op = str(msg.get("op", "exec")).lower()

        try:
            if op in {"progress_console", "interactive_terminal"}:
                rc, out = handle_ui_request(msg, op)
                send_message({
                    "executed": rc == 0,
                    "exit_code": rc,
                    "output": out,
                    "error": "" if rc == 0 else out,
                    "channel": (
                        "PROGRESS-CONSOLE-DIRECT"
                        if op == "progress_console"
                        else "INTERACTIVE-TERMINAL-DIRECT"
                    ),
                })
                continue

            if op == "control":
                rc, out = handle_control(str(msg.get("action", "")))
                send_message({
                    "executed": rc == 0,
                    "exit_code": rc,
                    "output": out,
                    "error": "" if rc == 0 else out,
                    "channel": "CONTROL-DIRECT",
                })
                continue
            if op == "voice_listen_once":
                resp = start_listen_once()
                send_message({
                    "_fb_voice_request_id": str(msg.get("_fb_voice_request_id", ""))[:128],
                    "executed": bool(resp.get("ok")),
                    "exit_code": 0 if resp.get("ok") else 1,
                    "output": "",
                    "error": "" if resp.get("ok") else str(resp.get("error", "")),
                    "channel": "VOICE-LISTEN-DIRECT",
                    "voice_started": bool(resp.get("started")),
                })
                continue

            if op == "voice_status":
                request_id = str(msg.get("_fb_voice_request_id", ""))[:128]
                target = str(msg.get("target", ""))

                if target != "ASUS":
                    send_message({
                        "executed": False,
                        "exit_code": 53,
                        "output": "",
                        "error": "VOICE_TARGET_MISMATCH",
                        "channel": "VOICE-STATUS-DIRECT",
                        "voice_state": "listo",
                        "voice_available": False,
                        "_fb_voice_request_id": request_id,
                    })
                    continue

                from framebridge_voice_status import read_voice_status

                voice = read_voice_status()

                send_message({
                    "executed": True,
                    "exit_code": 0,
                    "output": "",
                    "error": "",
                    "channel": "VOICE-STATUS-DIRECT",
                    "voice_state": voice["state"],
                    "voice_available": bool(voice["available"]),
                    "voice_updated_at": voice["updated_at"],
                    "_fb_voice_request_id": request_id,
                })
                continue

            if op == "cua":
                from framebridge_cua_linux import handle_cua_message, CuaTransportError

                _cua_runtime_journal("START", msg)

                try:
                    cua_result = handle_cua_message(msg)
                    cua_rc = 0
                    _cua_runtime_journal("END", msg, rc=0)
                except CuaTransportError as exc:
                    cua_result = {
                        "ok": False,
                        "error_code": exc.code,
                        "error": exc.message,
                    }
                    cua_rc = 52
                    _cua_runtime_journal(
                        "END",
                        msg,
                        rc=cua_rc,
                        error_code=exc.code,
                    )
                except Exception:
                    cua_result = {
                        "ok": False,
                        "error_code": "CUA_INTERNAL_ERROR",
                        "error": "CUA internal error",
                    }
                    cua_rc = 53
                    _cua_runtime_journal(
                        "END",
                        msg,
                        rc=cua_rc,
                        error_code="CUA_INTERNAL_ERROR",
                    )

                if (
                    isinstance(cua_result, dict)
                    and cua_result.get("_frame_output")
                ):
                    cua_out = str(cua_result["_frame_output"])
                else:
                    cua_out = json.dumps(
                        cua_result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )

                send_message({
                    "executed": cua_rc == 0,
                    "exit_code": cua_rc,
                    "output": cua_out,
                    "error": "" if cua_rc == 0 else cua_out,
                    "channel": "CUA-DIRECT",
                })
                continue

            if op == "term":
                term_job_id = str(msg.get("id", "job"))
                term_mode = str(msg.get("mode", "EXEC")).upper()
                term_lang = str(msg.get("lang", "BASH")).upper()
                term_code = str(msg.get("command", "")).rstrip()
                term_authorized = (
                    str(msg.get("authorized", "NO")).upper() == "YES"
                )

                try:
                    term_timeout = int(msg.get("timeout", 300))
                except Exception:
                    term_timeout = 300

                term_timeout = max(1, min(term_timeout, 1800))

                if term_lang != "BASH":
                    send_message({
                        "executed": False,
                        "exit_code": None,
                        "output": "",
                        "error": "FBTERM_R1_LANG_REQUIRES_BASH",
                        "channel": "FBTERM-PTY",
                    })
                    continue

                allowed, reason = classify(
                    term_mode,
                    term_lang,
                    term_code,
                    term_authorized,
                )

                if not allowed:
                    send_message({
                        "executed": False,
                        "exit_code": None,
                        "output": "",
                        "error": reason,
                        "channel": "FBTERM-PTY",
                    })
                    continue

                fbterm_root = os.environ.get(
                    "FRAMEBRIDGE_FBTERM_ROOT",
                    os.path.expanduser(
                        "~/.local/share/framebridge/fbterm"
                    ),
                )

                fbterm_engine = os.environ.get(
                    "FRAMEBRIDGE_FBTERM_ENGINE",
                    os.path.join(
                        fbterm_root,
                        "bin",
                        "fbterm_engine.py",
                    ),
                )

                fbterm_session = os.environ.get(
                    "FRAMEBRIDGE_FBTERM_SESSION",
                    "framebridge-fbterm-r1",
                )

                fbterm_title = os.environ.get(
                    "FRAMEBRIDGE_FBTERM_TITLE",
                    "FrameBridge Terminal R1",
                )

                fbterm_incoming = os.path.join(
                    fbterm_root,
                    "incoming",
                )

                os.makedirs(
                    fbterm_incoming,
                    mode=0o700,
                    exist_ok=True,
                )

                safe_term_id = safe_job_id(term_job_id)

                term_command_path = os.path.join(
                    fbterm_incoming,
                    safe_term_id + ".bash",
                )

                if os.path.exists(term_command_path):
                    send_message({
                        "executed": False,
                        "exit_code": None,
                        "output": "",
                        "error": "FBTERM_R1_JOB_ALREADY_EXISTS",
                        "channel": "FBTERM-PTY",
                    })
                    continue

                with open(
                    term_command_path,
                    "x",
                    encoding="utf-8",
                ) as term_file:
                    term_file.write(term_code)
                    term_file.write("\n")

                os.chmod(term_command_path, 0o600)

                term_env = os.environ.copy()
                term_env["FBTERM_ROOT"] = fbterm_root
                term_env["FBTERM_SESSION"] = fbterm_session
                term_env["FBTERM_TITLE"] = fbterm_title

                try:
                    term_proc = subprocess.run(
                        [
                            "python3",
                            fbterm_engine,
                            "exec",
                            safe_term_id,
                            str(term_timeout),
                            term_command_path,
                        ],
                        env=term_env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=term_timeout + 15,
                    )

                except subprocess.TimeoutExpired:
                    send_message({
                        "executed": False,
                        "exit_code": 124,
                        "output": "",
                        "error": "FBTERM_R1_ENGINE_TIMEOUT",
                        "channel": "FBTERM-PTY",
                    })
                    continue

                except Exception as exc:
                    send_message({
                        "executed": False,
                        "exit_code": None,
                        "output": "",
                        "error": (
                            "FBTERM_R1_ENGINE_ERROR:"
                            + type(exc).__name__
                        ),
                        "channel": "FBTERM-PTY",
                    })
                    continue

                if term_proc.returncode != 0:
                    send_message({
                        "executed": False,
                        "exit_code": term_proc.returncode,
                        "output": "",
                        "error": "FBTERM_R1_ENGINE_FAILED",
                        "channel": "FBTERM-PTY",
                    })
                    continue

                try:
                    term_result = json.loads(
                        term_proc.stdout.strip().splitlines()[-1]
                    )

                except Exception:
                    send_message({
                        "executed": False,
                        "exit_code": None,
                        "output": "",
                        "error": "FBTERM_R1_RESULT_PARSE_FAILED",
                        "channel": "FBTERM-PTY",
                    })
                    continue

                term_rc = int(
                    term_result.get("EXIT", 1)
                )

                compact = []

                for key in (
                    "COMMAND_SHA256",
                    "STDOUT_SHA256",
                    "STDERR_SHA256",
                    "STDOUT_BYTES",
                    "STDERR_BYTES",
                    "COMMAND_HASH_GATE",
                ):
                    value = term_result.get(key)

                    if value is not None:
                        compact.append(
                            key + "=" + str(value)
                        )

                # FBTERM_DUPLEX_R1: return last durable RESULTADO_FINAL from stdout
                duplex_final = None
                stdout_path = term_result.get("STDOUT_PATH")
                if stdout_path:
                    try:
                        with open(stdout_path, "r", encoding="utf-8", errors="replace") as fh:
                            stdout_lines = fh.read().splitlines()
                        for stdout_line in reversed(stdout_lines):
                            if stdout_line.startswith("RESULTADO_FINAL="):
                                final_value = stdout_line.split("=", 1)[1].strip()[:1000]
                                duplex_final = final_value
                                if final_value:
                                    compact.append("RESULTADO_FINAL=" + final_value)
                                break
                    except OSError:
                        pass

                send_message({
                    "executed": True,
                    "exit_code": term_rc,
                    "output": " ".join(compact) + (("\nRESULTADO_FINAL=" + duplex_final) if duplex_final else ""),  # FBTERM_DUPLEX_LINEFIX_R2
                    "RESULTADO_FINAL": duplex_final,  # FBTERM_DUPLEX_STRUCTURED_R1
                    "error": "",
                    "job_token": safe_term_id,
                    "channel": "FBTERM-PTY",
                })
                continue

            target = str(msg.get("target", "ASUS")).upper()
            if target != "ASUS":
                if str(msg.get("lang", "")).upper() == "FRAMEBRIDGE-PLAN-V1":
                    send_message(route_classic_plan(msg))
                else:
                    send_message(route_classic_node(msg))
                continue

            job_id = str(msg.get("id", "job"))
            mode = str(msg.get("mode", "READONLY")).upper()
            lang = str(msg.get("lang", "BASH")).upper()
            code = str(msg.get("command", "")).rstrip()
            authorized = str(msg.get("authorized", "NO")).upper() == "YES"

            try:
                timeout_s = int(msg.get("timeout", 300))
            except Exception:
                timeout_s = 300
            timeout_s = max(5, min(timeout_s, 1800))

            allowed, reason = classify(mode, lang, code, authorized)
            if not allowed:
                send_message({
                    "executed": False,
                    "exit_code": None,
                    "output": "",
                    "error": reason,
                    "channel": "EXEC-ISOLATED",
                })
                continue

            rc, out, token = run_isolated(
                job_id=job_id,
                lang=lang,
                code=code,
                timeout_s=timeout_s,
            )
            send_message({
                "executed": True,
                "exit_code": rc,
                "output": out,
                "error": "",
                "job_token": token,
                "channel": "EXEC-ISOLATED",
            })

        except Exception as exc:
            send_message({
                "executed": False,
                "exit_code": None,
                "output": "",
                "error": repr(exc),
                "channel": "HOST-ERROR",
            })

if __name__ == "__main__":
    main()
