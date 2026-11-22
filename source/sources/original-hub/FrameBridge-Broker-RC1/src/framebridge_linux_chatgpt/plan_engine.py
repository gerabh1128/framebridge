from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .product_core import ProductError
from .seed_core import SeedContext
from .memory_store import MemoryStore

PLAN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
STEP_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
REMOTE_APPROVAL_SOURCE = "REMOTE_HUMAN"
TOTP_APPROVAL_SOURCE = "REMOTE_HUMAN_TOTP"
REMOTE_CHALLENGE_RE = re.compile(r"^[a-f0-9]{32}$")
REMOTE_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9@_.-]{1,128}$")
TIMEZONE_RE = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
SERVICE_ACTIONS = {"start", "stop", "restart", "enable", "disable"}
IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
FORWARD_PROTOCOLS = {"any", "tcp", "udp"}
FORWARD_SOURCE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
)
ADMIN_R3_CORE_CAPABILITIES = {
    "package.refresh",
    "package.upgrade",
    "system.file.write_text",
    "system.directory.ensure",
    "system.file.mode",
    "system.daemon_reload",
    "network.forward",
}
PRIVILEGED_CHANGE_CAPABILITIES = {
    "package.install",
    "package.remove",
    "service.manage",
    "system.hostname",
    "system.timezone",
} | ADMIN_R3_CORE_CAPABILITIES
ALLOWED_CHANGE_CAPABILITIES = {"workspace.write_text"} | PRIVILEGED_CHANGE_CAPABILITIES
MAX_STEPS = 20
MAX_TEXT_BYTES = 65536
PRIV_HELPER = "/usr/local/libexec/framebridge-seed-priv"
SUDO = "/usr/bin/sudo"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_hostname(value: str) -> bool:
    value = str(value or "").strip()
    if not value or len(value) > 253 or value.endswith(".") or ".." in value:
        return False
    for label in value.split("."):
        if not (1 <= len(label) <= 63):
            return False
        if label[0] == "-" or label[-1] == "-":
            return False
        if re.fullmatch(r"[A-Za-z0-9-]+", label) is None:
            return False
    return True

def _valid_timezone(value: str) -> bool:
    value = str(value or "").strip()
    if not value or len(value) > 128 or value.startswith("/"):
        return False
    if any(part in ("", ".", "..") for part in Path(value).parts):
        return False
    return TIMEZONE_RE.fullmatch(value) is not None

def _args_sha(args: dict[str, Any]) -> str:
    return sha256(canonical(args))


def _normalize_forward_ipv4_network(value: str, *, source: bool=False) -> str:
    try:
        net=ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        raise ProductError("PLAN_NETWORK_CIDR_INVALID") from None
    if net.version != 4:
        raise ProductError("PLAN_NETWORK_IPV4_REQUIRED")
    if net.is_multicast or net.is_loopback or net.is_link_local:
        raise ProductError("PLAN_NETWORK_CIDR_UNSAFE")
    if source and not any(net.subnet_of(allowed) for allowed in FORWARD_SOURCE_NETS):
        raise ProductError("PLAN_NETWORK_FORWARD_SOURCE_NOT_PRIVATE")
    return str(net)


def _normalize_network_forward_args(args: dict[str, Any]) -> dict[str, Any]:
    required={
        "ingress_interface","egress_interface","source_subnet","destination_subnet",
        "protocol","destination_port","nat","lifetime",
    }
    if set(args) != required:
        raise ProductError("PLAN_NETWORK_FORWARD_ARGS_INVALID")
    ingress=str(args.get("ingress_interface") or "").strip()
    egress=str(args.get("egress_interface") or "").strip()
    if not IFACE_RE.fullmatch(ingress) or not IFACE_RE.fullmatch(egress):
        raise ProductError("PLAN_NETWORK_FORWARD_INTERFACE_INVALID")
    if ingress == egress:
        raise ProductError("PLAN_NETWORK_FORWARD_INTERFACES_MUST_DIFFER")
    source=_normalize_forward_ipv4_network(str(args.get("source_subnet") or ""),source=True)
    destination=_normalize_forward_ipv4_network(str(args.get("destination_subnet") or ""),source=False)
    protocol=str(args.get("protocol") or "").lower()
    if protocol not in FORWARD_PROTOCOLS:
        raise ProductError("PLAN_NETWORK_FORWARD_PROTOCOL_INVALID")
    port=args.get("destination_port")
    if isinstance(port,bool) or not isinstance(port,int) or not (0 <= port <= 65535):
        raise ProductError("PLAN_NETWORK_FORWARD_PORT_INVALID")
    if protocol=="any" and port!=0:
        raise ProductError("PLAN_NETWORK_FORWARD_PORT_REQUIRES_PROTOCOL")
    nat=args.get("nat")
    if not isinstance(nat,bool):
        raise ProductError("PLAN_NETWORK_FORWARD_NAT_INVALID")
    lifetime=str(args.get("lifetime") or "")
    if lifetime!="runtime":
        raise ProductError("PLAN_NETWORK_FORWARD_LIFETIME_INVALID")
    return {
        "ingress_interface":ingress,
        "egress_interface":egress,
        "source_subnet":source,
        "destination_subnet":destination,
        "protocol":protocol,
        "destination_port":port,
        "nat":nat,
        "lifetime":"runtime",
    }


MODE_RE = re.compile(r"^[0-7]{3,4}$")
SYSTEM_PATH_FORBIDDEN_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/boot",
    "/var/lib/framebridge-seed",
)
SYSTEM_PATH_FORBIDDEN_EXACT = {
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers",
}


def _valid_system_path_arg(value: str) -> bool:
    value = str(value or "").strip()
    if not value or len(value) > 4096 or not value.startswith("/"):
        return False
    path = Path(value)
    if any(part in ("", ".", "..") for part in path.parts[1:]):
        return False
    normalized = os.path.normpath(value)
    if normalized in SYSTEM_PATH_FORBIDDEN_EXACT:
        return False
    if normalized.startswith("/etc/sudoers.d/"):
        return False
    if normalized.startswith("/etc/ssl/private/"):
        return False
    if normalized.startswith("/root/.ssh/"):
        return False
    if normalized.startswith("/etc/ssh/ssh_host_"):
        return False
    for prefix in SYSTEM_PATH_FORBIDDEN_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return False
    return True


def _valid_mode_arg(value: str) -> bool:
    value = str(value or "").strip()
    if MODE_RE.fullmatch(value) is None:
        return False
    try:
        mode = int(value, 8)
    except ValueError:
        return False
    return 0 <= mode <= 0o7777


class PlanEngine:
    def __init__(self, seed: SeedContext):
        self.seed = seed
        self.plans_dir = seed.state_dir / "plans"
        self.rollback_dir = seed.state_dir / "rollback"
        self.workspace = seed.state_dir / "workspace"
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.rollback_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _plan_path(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.fullmatch(plan_id):
            raise ProductError("PLAN_ID_INVALID")
        return self.plans_dir / (plan_id + ".json")

    def _load_record(self, plan_id: str) -> dict[str, Any]:
        path = self._plan_path(plan_id)
        if not path.is_file():
            raise ProductError("PLAN_NOT_FOUND")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise ProductError("PLAN_RECORD_INVALID_JSON")
        if not isinstance(value, dict):
            raise ProductError("PLAN_RECORD_NOT_OBJECT")
        return value

    def _write_record(self, plan_id: str, record: dict[str, Any]) -> None:
        path = self._plan_path(plan_id)
        tmp = path.with_name("." + path.name + ".tmp")
        tmp.write_text(
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _validate_workspace_path(self, raw: str) -> Path:
        raw = str(raw or "")
        if not raw or len(raw) > 240:
            raise ProductError("WORKSPACE_PATH_INVALID")
        p = Path(raw)
        if p.is_absolute():
            raise ProductError("WORKSPACE_PATH_ABSOLUTE_FORBIDDEN")
        if any(part in ("", ".", "..") for part in p.parts):
            raise ProductError("WORKSPACE_PATH_TRAVERSAL")
        target = (self.workspace / p).resolve(strict=False)
        root = self.workspace.resolve(strict=False)
        try:
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise ProductError("WORKSPACE_PATH_ESCAPE")
        except ValueError:
            raise ProductError("WORKSPACE_PATH_ESCAPE")
        cur = root
        for part in p.parts[:-1]:
            cur = cur / part
            if cur.exists() and cur.is_symlink():
                raise ProductError("WORKSPACE_SYMLINK_PARENT_BLOCKED")
        if target.exists() and target.is_symlink():
            raise ProductError("WORKSPACE_SYMLINK_TARGET_BLOCKED")
        return target

    def validate_document(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise ProductError("PLAN_NOT_OBJECT")
        if plan.get("schema") != 1:
            raise ProductError("PLAN_SCHEMA_UNSUPPORTED")

        plan_id = str(plan.get("plan_id") or "")
        if not PLAN_ID_RE.fullmatch(plan_id):
            raise ProductError("PLAN_ID_INVALID")

        title = str(plan.get("title") or "").strip()
        if not title or len(title) > 200:
            raise ProductError("PLAN_TITLE_INVALID")

        steps = plan.get("steps")
        if not isinstance(steps, list) or not (1 <= len(steps) <= MAX_STEPS):
            raise ProductError("PLAN_STEPS_INVALID")

        seen: set[str] = set()
        normalized_steps: list[dict[str, Any]] = []

        for step in steps:
            if not isinstance(step, dict):
                raise ProductError("PLAN_STEP_NOT_OBJECT")
            step_id = str(step.get("id") or "")
            if not STEP_ID_RE.fullmatch(step_id) or step_id in seen:
                raise ProductError("PLAN_STEP_ID_INVALID")
            seen.add(step_id)

            capability = str(step.get("capability") or "")
            if capability not in ALLOWED_CHANGE_CAPABILITIES:
                raise ProductError("PLAN_CAPABILITY_NOT_ALLOWED")

            args = step.get("args")
            if not isinstance(args, dict):
                raise ProductError("PLAN_ARGS_NOT_OBJECT")

            if capability == "workspace.write_text":
                allowed_keys = {"path", "text"}
                if set(args) - allowed_keys:
                    raise ProductError("PLAN_ARGS_UNKNOWN_KEY")
                path = str(args.get("path") or "")
                text = str(args.get("text") if args.get("text") is not None else "")
                self._validate_workspace_path(path)
                if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                    raise ProductError("PLAN_TEXT_TOO_LARGE")
                normalized_args = {"path": path, "text": text}

            elif capability in ("package.install", "package.remove"):
                if set(args) != {"package"}:
                    raise ProductError("PLAN_PACKAGE_ARGS_INVALID")
                package = str(args.get("package") or "")
                if not PACKAGE_RE.fullmatch(package):
                    raise ProductError("PLAN_PACKAGE_NAME_INVALID")
                normalized_args = {"package": package}

            elif capability == "service.manage":
                if set(args) != {"service", "action"}:
                    raise ProductError("PLAN_SERVICE_ARGS_INVALID")
                service = str(args.get("service") or "")
                action = str(args.get("action") or "")
                if not SERVICE_RE.fullmatch(service):
                    raise ProductError("PLAN_SERVICE_NAME_INVALID")
                if action not in SERVICE_ACTIONS:
                    raise ProductError("PLAN_SERVICE_ACTION_INVALID")
                normalized_args = {"service": service, "action": action}

            elif capability == "system.hostname":
                if set(args) != {"hostname"}:
                    raise ProductError("PLAN_HOSTNAME_ARGS_INVALID")
                hostname = str(args.get("hostname") or "").strip()
                if not _valid_hostname(hostname):
                    raise ProductError("PLAN_HOSTNAME_INVALID")
                normalized_args = {"hostname": hostname}

            elif capability == "system.timezone":
                if set(args) != {"timezone"}:
                    raise ProductError("PLAN_TIMEZONE_ARGS_INVALID")
                timezone_name = str(args.get("timezone") or "").strip()
                if not _valid_timezone(timezone_name):
                    raise ProductError("PLAN_TIMEZONE_INVALID")
                normalized_args = {"timezone": timezone_name}

            elif capability == "package.refresh":
                if set(args):
                    raise ProductError("PLAN_PACKAGE_REFRESH_ARGS_INVALID")
                normalized_args = {}

            elif capability == "package.upgrade":
                if set(args) != {"package"}:
                    raise ProductError("PLAN_PACKAGE_ARGS_INVALID")
                package = str(args.get("package") or "")
                if not PACKAGE_RE.fullmatch(package):
                    raise ProductError("PLAN_PACKAGE_NAME_INVALID")
                normalized_args = {"package": package}

            elif capability == "system.file.write_text":
                if set(args) != {"path", "text"}:
                    raise ProductError("PLAN_SYSTEM_FILE_ARGS_INVALID")
                path = str(args.get("path") or "")
                text = str(args.get("text") if args.get("text") is not None else "")
                if not _valid_system_path_arg(path):
                    raise ProductError("PLAN_SYSTEM_PATH_INVALID")
                if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                    raise ProductError("PLAN_TEXT_TOO_LARGE")
                normalized_args = {"path": os.path.normpath(path), "text": text}

            elif capability == "system.directory.ensure":
                if set(args) != {"path", "mode"}:
                    raise ProductError("PLAN_SYSTEM_DIRECTORY_ARGS_INVALID")
                path = str(args.get("path") or "")
                mode = str(args.get("mode") or "")
                if not _valid_system_path_arg(path):
                    raise ProductError("PLAN_SYSTEM_PATH_INVALID")
                if not _valid_mode_arg(mode):
                    raise ProductError("PLAN_MODE_INVALID")
                normalized_args = {"path": os.path.normpath(path), "mode": mode}

            elif capability == "system.file.mode":
                if set(args) != {"path", "mode"}:
                    raise ProductError("PLAN_SYSTEM_FILE_MODE_ARGS_INVALID")
                path = str(args.get("path") or "")
                mode = str(args.get("mode") or "")
                if not _valid_system_path_arg(path):
                    raise ProductError("PLAN_SYSTEM_PATH_INVALID")
                if not _valid_mode_arg(mode):
                    raise ProductError("PLAN_MODE_INVALID")
                normalized_args = {"path": os.path.normpath(path), "mode": mode}

            elif capability == "system.daemon_reload":
                if set(args):
                    raise ProductError("PLAN_DAEMON_RELOAD_ARGS_INVALID")
                normalized_args = {}

            elif capability == "network.forward":
                normalized_args = _normalize_network_forward_args(args)

            else:
                raise ProductError("PLAN_CAPABILITY_NOT_ALLOWED")

            normalized_steps.append({
                "id": step_id,
                "capability": capability,
                "risk": "CHANGE",
                "args": normalized_args,
            })

        privileged_steps = [
            x for x in normalized_steps
            if x["capability"] in PRIVILEGED_CHANGE_CAPABILITIES
        ]
        if privileged_steps and (len(normalized_steps) != 1 or len(privileged_steps) != 1):
            raise ProductError("PLAN_PRIVILEGED_MUST_BE_SINGLE_STEP")

        normalized = {
            "schema": 1,
            "plan_id": plan_id,
            "title": title,
            "steps": normalized_steps,
        }
        return normalized

    def import_file(self, path: str) -> dict[str, Any]:
        src = Path(path).expanduser()
        if not src.is_file():
            raise ProductError("PLAN_IMPORT_FILE_NOT_FOUND")
        if src.stat().st_size > 262144:
            raise ProductError("PLAN_IMPORT_FILE_TOO_LARGE")
        try:
            value = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            raise ProductError("PLAN_IMPORT_JSON_INVALID")
        return self.import_document(value)

    def import_document(self, value: dict[str, Any]) -> dict[str, Any]:
        plan = self.validate_document(value)
        plan_id = plan["plan_id"]
        dst = self._plan_path(plan_id)
        if dst.exists():
            raise ProductError("PLAN_ALREADY_EXISTS")

        plan_sha = sha256(canonical(plan))
        record = {
            "plan": plan,
            "plan_sha256": plan_sha,
            "state": "IMPORTED",
            "approval": None,
            "apply": None,
            "rollback": None,
            "rollback_approval": None,
            "imported_at": utc_now(),
        }
        self._write_record(plan_id, record)
        self.seed.audit.append(
            "plan.import",
            "PASS",
            request={"plan_id": plan_id, "plan_sha256": plan_sha},
            result={"steps": len(plan["steps"]), "risk": "CHANGE"},
            principal="local-operator",
        )
        return self.show(plan_id)

    def show(self, plan_id: str) -> dict[str, Any]:
        record = self._load_record(plan_id)
        return {
            "ok": True,
            "plan_id": plan_id,
            "plan_sha256": record.get("plan_sha256"),
            "state": record.get("state"),
            "plan": record.get("plan"),
            "approval": record.get("approval"),
            "apply": record.get("apply"),
            "rollback": record.get("rollback"),
            "rollback_approval": record.get("rollback_approval"),
            "workspace": str(self.workspace),
        }

    def validate(self, plan_id: str) -> dict[str, Any]:
        record = self._load_record(plan_id)
        plan = self.validate_document(record.get("plan"))
        current_sha = sha256(canonical(plan))
        expected_sha = str(record.get("plan_sha256") or "")
        ok = current_sha == expected_sha
        result = {
            "ok": ok,
            "plan_id": plan_id,
            "expected_sha256": expected_sha,
            "current_sha256": current_sha,
            "risk": "CHANGE",
            "steps": len(plan["steps"]),
        }
        if not ok:
            result["error"] = "PLAN_HASH_MISMATCH"
        return result

    def _plan_capability(self, record: dict[str, Any]) -> str:
        steps = ((record.get("plan") or {}).get("steps") or [])
        if len(steps) != 1:
            return "mixed"
        return str(steps[0].get("capability") or "")

    def _remote_approval_metadata_valid(self, approval: dict[str, Any]) -> bool:
        return (
            approval.get("source") == REMOTE_APPROVAL_SOURCE
            and approval.get("auth_method") == "ecdsa-p256"
            and REMOTE_CHALLENGE_RE.fullmatch(str(approval.get("challenge_id") or "")) is not None
            and REMOTE_KEY_ID_RE.fullmatch(str(approval.get("key_id") or "")) is not None
        )

    def _totp_approval_metadata_valid(self, approval: dict[str, Any]) -> bool:
        return (
            approval.get("source") == TOTP_APPROVAL_SOURCE
            and approval.get("auth_method") == "totp-sha1-6-30"
            and REMOTE_CHALLENGE_RE.fullmatch(str(approval.get("challenge_id") or "")) is not None
            and REMOTE_KEY_ID_RE.fullmatch(str(approval.get("key_id") or "")) is not None
            and type(approval.get("totp_step")) is int
        )

    def _privilege_mode(self) -> str:
        seed = getattr(self, "seed", None)
        if seed is None:
            return "totp"
        config = seed.load_config() or {}
        setup = config.get("setup") or {}
        mode = str(setup.get("privilege_mode") or "totp").strip().lower()
        return mode if mode in ("totp", "sudo_tty") else "totp"

    def _run_priv_helper(self, args: list[str], input_text: str | None = None) -> dict[str, Any]:
        # Exit status alone must never authorize a privileged state transition.
        mode = self._privilege_mode()
        if mode == "sudo_tty" and not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise ProductError("LOCAL_TTY_REQUIRED_FOR_SUDO_PASSWORD")
        sudo_argv = [SUDO, PRIV_HELPER, *args] if mode == "sudo_tty" else [SUDO, "-n", PRIV_HELPER, *args]
        try:
            kwargs={"text":True,"stdout":subprocess.PIPE,"stderr":subprocess.PIPE,"check":False,"shell":False}
            if input_text is not None:
                kwargs["input"]=input_text
            proc = subprocess.run(sudo_argv, **kwargs)
        except OSError:
            raise ProductError("PRIV_HELPER_EXEC_FAILED") from None
        try:
            value = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            raise ProductError("PRIV_HELPER_INVALID_JSON") from None

        local_kinds={"approve-install":"package.install","approve-rollback":"package.rollback"}
        op=args[0] if args else ""
        if op in local_kinds and isinstance(value,dict) and "ok" not in value:
            if proc.returncode != 0: raise ProductError("PRIV_HELPER_FAILED")
            fields={"schema","kind","plan_id","plan_sha256","package","source","one_shot","consumed","approved_at"}
            valid=(len(args)==7 and args[1::2]==["--plan-id","--plan-sha256","--package"] and set(value)==fields
                   and type(value.get("schema")) is int and value["schema"]==1
                   and value.get("kind")==local_kinds[op] and value.get("plan_id")==args[2]
                   and value.get("plan_sha256")==args[4] and value.get("package")==args[6]
                   and value.get("source")=="LOCAL_TTY" and value.get("one_shot") is True
                   and value.get("consumed") is False and isinstance(value.get("approved_at"),str))
            if not valid: raise ProductError("PRIV_HELPER_INVALID_RESPONSE")
            try:
                approved_at=datetime.fromisoformat(value["approved_at"].replace("Z","+00:00"))
                if approved_at.tzinfo is None or approved_at.utcoffset() is None: raise ValueError("TIMEZONE_REQUIRED")
            except (ValueError,TypeError,OverflowError):
                raise ProductError("PRIV_HELPER_INVALID_RESPONSE") from None
            return value

        if op=="approve-remote-challenge" and isinstance(value,dict) and "ok" not in value:
            if proc.returncode != 0: raise ProductError("PRIV_HELPER_FAILED")
            fields={"schema","kind","plan_id","plan_sha256","package","source","auth_method","key_id","challenge_id","one_shot","consumed","approved_at"}
            valid=(set(value)==fields and value.get("schema")==2 and value.get("kind") in ("package.install","package.rollback")
                   and PLAN_ID_RE.fullmatch(str(value.get("plan_id") or "")) is not None
                   and re.fullmatch(r"[a-f0-9]{64}",str(value.get("plan_sha256") or "")) is not None
                   and PACKAGE_RE.fullmatch(str(value.get("package") or "")) is not None
                   and value.get("source")==REMOTE_APPROVAL_SOURCE and value.get("auth_method")=="ecdsa-p256"
                   and REMOTE_KEY_ID_RE.fullmatch(str(value.get("key_id") or "")) is not None
                   and REMOTE_CHALLENGE_RE.fullmatch(str(value.get("challenge_id") or "")) is not None
                   and value.get("one_shot") is True and value.get("consumed") is False
                   and isinstance(value.get("approved_at"),str))
            if not valid: raise ProductError("PRIV_HELPER_INVALID_RESPONSE")
            try:
                approved_at=datetime.fromisoformat(value["approved_at"].replace("Z","+00:00"))
                if approved_at.tzinfo is None or approved_at.utcoffset() is None: raise ValueError("TIMEZONE_REQUIRED")
            except (ValueError,TypeError,OverflowError):
                raise ProductError("PRIV_HELPER_INVALID_RESPONSE") from None
            return value

        if op=="approve-totp-challenge" and isinstance(value,dict) and "ok" not in value:
            if proc.returncode != 0: raise ProductError("PRIV_HELPER_FAILED")
            fields={"schema","kind","plan_id","plan_sha256","package","source","auth_method","key_id","challenge_id","totp_step","one_shot","consumed","approved_at"}
            valid=(set(value)==fields and value.get("schema")==3 and value.get("kind") in ("package.install","package.rollback")
                   and PLAN_ID_RE.fullmatch(str(value.get("plan_id") or "")) is not None
                   and re.fullmatch(r"[a-f0-9]{64}",str(value.get("plan_sha256") or "")) is not None
                   and PACKAGE_RE.fullmatch(str(value.get("package") or "")) is not None
                   and value.get("source")==TOTP_APPROVAL_SOURCE and value.get("auth_method")=="totp-sha1-6-30"
                   and REMOTE_KEY_ID_RE.fullmatch(str(value.get("key_id") or "")) is not None
                   and REMOTE_CHALLENGE_RE.fullmatch(str(value.get("challenge_id") or "")) is not None
                   and type(value.get("totp_step")) is int
                   and value.get("one_shot") is True and value.get("consumed") is False
                   and isinstance(value.get("approved_at"),str))
            if not valid: raise ProductError("PRIV_HELPER_INVALID_RESPONSE")
            return value

        generic_approval_kinds = {
            "approve-change": "system.change",
            "approve-change-rollback": "system.change.rollback",
        }
        if args and args[0] in generic_approval_kinds and isinstance(value, dict) and "ok" not in value:
            if proc.returncode != 0:
                raise ProductError("PRIV_HELPER_FAILED")
            fields = {
                "schema", "kind", "plan_id", "plan_sha256", "capability",
                "args_sha256", "source", "one_shot", "consumed", "approved_at",
            }
            valid = (
                len(args) == 9
                and args[1::2] == ["--plan-id","--plan-sha256","--capability","--args-sha256"]
                and set(value) == fields
                and type(value.get("schema")) is int and value["schema"] == 2
                and value.get("kind") == generic_approval_kinds[args[0]]
                and value.get("plan_id") == args[2]
                and value.get("plan_sha256") == args[4]
                and value.get("capability") == args[6]
                and value.get("args_sha256") == args[8]
                and value.get("source") == "LOCAL_TTY"
                and value.get("one_shot") is True
                and value.get("consumed") is False
                and isinstance(value.get("approved_at"), str)
            )
            if not valid:
                raise ProductError("PRIV_HELPER_INVALID_RESPONSE")
            try:
                approved_at=datetime.fromisoformat(value["approved_at"].replace("Z","+00:00"))
                if approved_at.tzinfo is None or approved_at.utcoffset() is None:
                    raise ValueError("TIMEZONE_REQUIRED")
            except (ValueError,TypeError,OverflowError):
                raise ProductError("PRIV_HELPER_INVALID_RESPONSE") from None
            return value

        if not isinstance(value,dict) or type(value.get("ok")) is not bool:
            raise ProductError("PRIV_HELPER_INVALID_RESPONSE")
        if proc.returncode != 0 or value["ok"] is not True:
            error=value.get("error")
            if not isinstance(error,str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}",error) is None: error="PRIV_HELPER_FAILED"
            raise ProductError(error)
        if value.get("error") not in (None,""): raise ProductError("PRIV_HELPER_INVALID_RESPONSE")
        if op in local_kinds or op in generic_approval_kinds or op in ("approve-remote-challenge","approve-totp-challenge"):
            raise ProductError("PRIV_HELPER_INVALID_RESPONSE")
        return value

    def remote_challenge(self, plan_id: str, rollback: bool=False, ttl_seconds: int=300) -> dict[str, Any]:
        record=self._load_record(plan_id)
        validation=self.validate(plan_id)
        if not validation["ok"]: raise ProductError("PLAN_HASH_MISMATCH")
        if self._plan_capability(record)!="package.install": raise ProductError("PLAN_NOT_PRIVILEGED_PACKAGE")
        step=record["plan"]["steps"][0]; package=step["args"]["package"]
        if rollback:
            if record.get("state")!="APPLIED" or (record.get("rollback") or {}).get("available") is not True:
                raise ProductError("ROLLBACK_NOT_AVAILABLE")
            kind="package.rollback"
        else:
            if record.get("state") not in ("IMPORTED","APPROVED"): raise ProductError("PLAN_STATE_NOT_APPROVABLE")
            kind="package.install"
        value=self._run_priv_helper(["remote-challenge","--kind",kind,"--plan-id",plan_id,"--plan-sha256",record["plan_sha256"],"--package",package,"--ttl",str(int(ttl_seconds))])
        required={"ok","schema","kind","plan_id","plan_sha256","package","challenge_id","created_at","expires_at","state","message_b64"}
        valid=(set(value)==required and value.get("ok") is True and value.get("schema")==1 and value.get("kind")==kind
               and value.get("plan_id")==plan_id and value.get("plan_sha256")==record["plan_sha256"] and value.get("package")==package
               and REMOTE_CHALLENGE_RE.fullmatch(str(value.get("challenge_id") or "")) is not None and value.get("state")=="PENDING"
               and isinstance(value.get("message_b64"),str))
        if not valid: raise ProductError("REMOTE_CHALLENGE_INVALID_RESPONSE")
        self.seed.audit.append("plan.remote.challenge","PASS",request={"plan_id":plan_id,"plan_sha256":record["plan_sha256"],"kind":kind},result={"challenge_id":value["challenge_id"],"expires_at":value["expires_at"]},principal="remote-human-broker")
        return value

    def approve_remote(self, plan_id: str, challenge_id: str, key_id: str, signature_b64: str, rollback: bool=False) -> dict[str, Any]:
        record=self._load_record(plan_id)
        validation=self.validate(plan_id)
        if not validation["ok"]: raise ProductError("PLAN_HASH_MISMATCH")
        if self._plan_capability(record)!="package.install": raise ProductError("PLAN_NOT_PRIVILEGED_PACKAGE")
        if REMOTE_CHALLENGE_RE.fullmatch(str(challenge_id)) is None: raise ProductError("REMOTE_CHALLENGE_ID_INVALID")
        if REMOTE_KEY_ID_RE.fullmatch(str(key_id)) is None: raise ProductError("REMOTE_KEY_ID_INVALID")
        step=record["plan"]["steps"][0]; package=step["args"]["package"]
        expected_kind="package.rollback" if rollback else "package.install"
        if rollback:
            if record.get("state")!="APPLIED" or (record.get("rollback") or {}).get("available") is not True: raise ProductError("ROLLBACK_NOT_AVAILABLE")
        else:
            if record.get("state") not in ("IMPORTED","APPROVED"): raise ProductError("PLAN_STATE_NOT_APPROVABLE")
        receipt=self._run_priv_helper(["approve-remote-challenge","--challenge-id",str(challenge_id),"--key-id",str(key_id),"--signature-b64",str(signature_b64)])
        if receipt.get("kind")!=expected_kind or receipt.get("plan_id")!=plan_id or receipt.get("plan_sha256")!=record.get("plan_sha256") or receipt.get("package")!=package:
            raise ProductError("REMOTE_APPROVAL_BINDING_MISMATCH")
        approval={"source":REMOTE_APPROVAL_SOURCE,"plan_sha256":record["plan_sha256"],"approved_at":receipt["approved_at"],"one_shot":True,"consumed":False,"package":package,"auth_method":"ecdsa-p256","key_id":receipt["key_id"],"challenge_id":receipt["challenge_id"]}
        if rollback:
            record["rollback_approval"]=approval; event="plan.rollback.approve.remote"
        else:
            record["approval"]=approval; record["state"]="APPROVED"; event="plan.approve.remote"
        self._write_record(plan_id,record)
        self.seed.audit.append(event,"PASS",request={"plan_id":plan_id,"plan_sha256":record["plan_sha256"],"source":REMOTE_APPROVAL_SOURCE,"challenge_id":receipt["challenge_id"],"key_id":receipt["key_id"]},result={"package":package,"one_shot":True},principal="remote-human")
        return self.show(plan_id)

    def totp_enroll(self, key_id: str, node_label: str, issuer: str='FrameBridge') -> dict[str, Any]:
        if REMOTE_KEY_ID_RE.fullmatch(str(key_id)) is None: raise ProductError("REMOTE_KEY_ID_INVALID")
        if not isinstance(node_label,str) or not node_label.strip() or len(node_label.strip())>128: raise ProductError("TOTP_NODE_LABEL_INVALID")
        if not isinstance(issuer,str) or not issuer.strip() or len(issuer.strip())>64: raise ProductError("TOTP_ISSUER_INVALID")
        value=self._run_priv_helper(["totp-enroll","--key-id",str(key_id),"--node-label",node_label.strip(),"--issuer",issuer.strip()])
        required={"ok","schema","key_id","node_label","issuer","otpauth_uri","secret_sha256","enrolled_at"}
        valid=(set(value)==required and value.get("ok") is True and value.get("schema")==1 and value.get("key_id")==key_id and value.get("node_label")==node_label.strip() and value.get("issuer")==issuer.strip() and isinstance(value.get("otpauth_uri"),str) and value["otpauth_uri"].startswith("otpauth://totp/") and re.fullmatch(r"[a-f0-9]{64}",str(value.get("secret_sha256") or "")) is not None)
        if not valid: raise ProductError("TOTP_ENROLL_INVALID_RESPONSE")
        self.seed.audit.append("totp.enroll","PASS",request={"key_id":key_id,"node_label":node_label.strip(),"issuer":issuer.strip()},result={"secret_sha256":value["secret_sha256"]},principal="local-operator")
        return value

    def totp_challenge(self, plan_id: str, key_id: str, rollback: bool=False, ttl_seconds: int=120) -> dict[str, Any]:
        record=self._load_record(plan_id); validation=self.validate(plan_id)
        if not validation["ok"]: raise ProductError("PLAN_HASH_MISMATCH")
        if self._plan_capability(record)!="package.install": raise ProductError("PLAN_NOT_PRIVILEGED_PACKAGE")
        step=record["plan"]["steps"][0]; package=step["args"]["package"]
        kind="package.rollback" if rollback else "package.install"
        if rollback and (record.get("state")!="APPLIED" or (record.get("rollback") or {}).get("available") is not True): raise ProductError("ROLLBACK_NOT_AVAILABLE")
        if not rollback and record.get("state") not in ("IMPORTED","APPROVED"): raise ProductError("PLAN_STATE_NOT_APPROVABLE")
        value=self._run_priv_helper(["totp-challenge","--kind",kind,"--plan-id",plan_id,"--plan-sha256",record["plan_sha256"],"--package",package,"--key-id",str(key_id),"--ttl",str(int(ttl_seconds))])
        if value.get("ok") is not True or value.get("plan_id")!=plan_id or value.get("plan_sha256")!=record["plan_sha256"] or value.get("totp_key_id")!=key_id: raise ProductError("TOTP_CHALLENGE_INVALID_RESPONSE")
        return value

    def approve_totp(self, plan_id: str, challenge_id: str, key_id: str, code: str, rollback: bool=False) -> dict[str, Any]:
        record=self._load_record(plan_id); validation=self.validate(plan_id)
        if not validation["ok"]: raise ProductError("PLAN_HASH_MISMATCH")
        if not (isinstance(code,str) and len(code)==6 and code.isdigit()): raise ProductError("TOTP_CODE_INVALID")
        step=record["plan"]["steps"][0]; package=step["args"]["package"]; expected="package.rollback" if rollback else "package.install"
        receipt=self._run_priv_helper(["approve-totp-challenge","--challenge-id",str(challenge_id),"--key-id",str(key_id)],input_text=code+"\n")
        if receipt.get("kind")!=expected or receipt.get("plan_id")!=plan_id or receipt.get("plan_sha256")!=record.get("plan_sha256") or receipt.get("package")!=package: raise ProductError("TOTP_APPROVAL_BINDING_MISMATCH")
        approval={"source":TOTP_APPROVAL_SOURCE,"plan_sha256":record["plan_sha256"],"approved_at":receipt["approved_at"],"one_shot":True,"consumed":False,"package":package,"auth_method":"totp-sha1-6-30","key_id":receipt["key_id"],"challenge_id":receipt["challenge_id"],"totp_step":receipt["totp_step"]}
        if rollback: record["rollback_approval"]=approval
        else: record["approval"]=approval; record["state"]="APPROVED"
        self._write_record(plan_id,record); return self.show(plan_id)

    def approve_system(self, plan_id: str) -> dict[str, Any]:
        record = self._load_record(plan_id)
        validation = self.validate(plan_id)
        if not validation["ok"]:
            raise ProductError("PLAN_HASH_MISMATCH")
        if record.get("state") not in ("IMPORTED", "APPROVED"):
            raise ProductError("PLAN_STATE_NOT_APPROVABLE")

        cap = self._plan_capability(record)
        if cap not in PRIVILEGED_CHANGE_CAPABILITIES:
            raise ProductError("PLAN_NOT_PRIVILEGED_CHANGE")

        step = record["plan"]["steps"][0]
        args = step["args"]

        approval = {
            "source": "LOCAL_TTY_PRIVILEGED",
            "plan_sha256": record["plan_sha256"],
            "approved_at": utc_now(),
            "one_shot": True,
            "consumed": False,
            "capability": cap,
        }

        if cap == "package.install":
            package = args["package"]
            self._run_priv_helper([
                "approve-install", "--plan-id", plan_id,
                "--plan-sha256", record["plan_sha256"],
                "--package", package,
            ])
            approval["package"] = package
            audit_result = {"capability": cap, "package": package, "one_shot": True}
        else:
            args_sha = _args_sha(args)
            self._run_priv_helper([
                "approve-change", "--plan-id", plan_id,
                "--plan-sha256", record["plan_sha256"],
                "--capability", cap,
                "--args-sha256", args_sha,
            ])
            approval["args_sha256"] = args_sha
            audit_result = {"capability": cap, "args_sha256": args_sha, "one_shot": True}

        record["approval"] = approval
        record["state"] = "APPROVED"
        self._write_record(plan_id, record)
        self.seed.audit.append(
            "plan.approve.system", "PASS",
            request={
                "plan_id": plan_id,
                "plan_sha256": record["plan_sha256"],
                "source": "LOCAL_TTY_PRIVILEGED",
                "capability": cap,
            },
            result=audit_result,
            principal="local-operator",
        )
        return self.show(plan_id)

    def approve(self, plan_id: str) -> dict[str, Any]:
        record = self._load_record(plan_id)
        cap = self._plan_capability(record)
        if cap == "package.install":
            raise ProductError("USE_APPROVE_SYSTEM_FOR_PACKAGE")
        if cap in PRIVILEGED_CHANGE_CAPABILITIES:
            raise ProductError("USE_APPROVE_SYSTEM_FOR_PRIVILEGED")
        validation = self.validate(plan_id)
        if not validation["ok"]:
            raise ProductError("PLAN_HASH_MISMATCH")
        if record.get("state") not in ("IMPORTED", "APPROVED"):
            raise ProductError("PLAN_STATE_NOT_APPROVABLE")

        approval = {
            "source": "LOCAL_TTY",
            "plan_sha256": record["plan_sha256"],
            "approved_at": utc_now(),
            "one_shot": True,
            "consumed": False,
        }
        record["approval"] = approval
        record["state"] = "APPROVED"
        self._write_record(plan_id, record)
        self.seed.audit.append(
            "plan.approve",
            "PASS",
            request={
                "plan_id": plan_id,
                "plan_sha256": record["plan_sha256"],
                "source": "LOCAL_TTY",
            },
            result={"one_shot": True},
            principal="local-operator",
        )
        return self.show(plan_id)

    def _backup_step(self, plan_id: str, step_id: str, target: Path) -> dict[str, Any]:
        rb_root = self.rollback_dir / plan_id
        rb_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        meta: dict[str, Any] = {
            "step_id": step_id,
            "target": str(target),
            "existed": target.exists(),
        }
        if target.exists():
            if not target.is_file():
                raise ProductError("WORKSPACE_TARGET_NOT_REGULAR_FILE")
            data = target.read_bytes()
            backup = rb_root / (step_id + ".bak")
            backup.write_bytes(data)
            os.chmod(backup, 0o600)
            meta["backup"] = str(backup)
            meta["mode"] = target.stat().st_mode & 0o777
            meta["sha256_before"] = sha256(data)
        return meta

    def _atomic_write(self, target: Path, text: str) -> dict[str, Any]:
        target.parent.mkdir(parents=True, exist_ok=True)
        root = self.workspace.resolve(strict=False)
        try:
            if os.path.commonpath([str(root), str(target.parent.resolve(strict=False))]) != str(root):
                raise ProductError("WORKSPACE_PARENT_ESCAPE")
        except ValueError:
            raise ProductError("WORKSPACE_PARENT_ESCAPE")

        data = text.encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=".framebridge-plan-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return {"sha256_after": sha256(data), "bytes": len(data)}

    def apply(self, plan_id: str) -> dict[str, Any]:
        memory = MemoryStore(self.seed)
        memory.require_ready()
        record = self._load_record(plan_id)
        validation = self.validate(plan_id)
        if not validation["ok"]:
            raise ProductError("PLAN_HASH_MISMATCH")
        if record.get("state") != "APPROVED":
            raise ProductError("PLAN_NOT_APPROVED")
        approval = record.get("approval") or {}
        cap = self._plan_capability(record)
        if cap == "package.install":
            if approval.get("source") not in ("LOCAL_TTY_PRIVILEGED", REMOTE_APPROVAL_SOURCE, TOTP_APPROVAL_SOURCE):
                raise ProductError("PLAN_APPROVAL_SOURCE_INVALID")
            if approval.get("source") == REMOTE_APPROVAL_SOURCE and not self._remote_approval_metadata_valid(approval):
                raise ProductError("PLAN_REMOTE_APPROVAL_INVALID")
            if approval.get("source") == TOTP_APPROVAL_SOURCE and not self._totp_approval_metadata_valid(approval):
                raise ProductError("PLAN_TOTP_APPROVAL_INVALID")
        elif cap in PRIVILEGED_CHANGE_CAPABILITIES:
            if approval.get("source") != "LOCAL_TTY_PRIVILEGED":
                raise ProductError("PLAN_APPROVAL_SOURCE_INVALID")
        elif approval.get("source") != "LOCAL_TTY":
            raise ProductError("PLAN_APPROVAL_SOURCE_INVALID")
        if approval.get("plan_sha256") != record.get("plan_sha256"):
            raise ProductError("PLAN_APPROVAL_HASH_MISMATCH")
        if approval.get("one_shot") is not True or approval.get("consumed") is True:
            raise ProductError("PLAN_APPROVAL_CONSUMED")

        plan = record["plan"]
        applied: list[dict[str, Any]] = []
        rollback_meta: list[dict[str, Any]] = []

        if self._plan_capability(record) == "package.install":
            if approval.get("source") not in ("LOCAL_TTY_PRIVILEGED", REMOTE_APPROVAL_SOURCE, TOTP_APPROVAL_SOURCE):
                raise ProductError("PLAN_PRIVILEGED_APPROVAL_SOURCE_INVALID")
            if approval.get("source") == REMOTE_APPROVAL_SOURCE and not self._remote_approval_metadata_valid(approval):
                raise ProductError("PLAN_REMOTE_APPROVAL_INVALID")
            if approval.get("source") == TOTP_APPROVAL_SOURCE and not self._totp_approval_metadata_valid(approval):
                raise ProductError("PLAN_TOTP_APPROVAL_INVALID")
            step = plan["steps"][0]
            package = step["args"]["package"]
            result = self._run_priv_helper([
                "apply-install", "--plan-id", plan_id,
                "--plan-sha256", record["plan_sha256"],
                "--package", package,
            ])
            approval["consumed"] = True
            approval["consumed_at"] = utc_now()
            record["approval"] = approval
            applied = [{
                "step_id": step["id"],
                "capability": "package.install",
                "package": package,
                "version": result.get("version"),
                "verified": result.get("ok") is True,
            }]
            record["apply"] = {"applied_at": utc_now(), "steps": applied}
            record["rollback"] = {"available": result.get("rollback_available") is True, "kind": "package.remove"}
            record["state"] = "APPLIED"
            self._write_record(plan_id, record)
            self.seed.audit.append(
                "plan.apply", "PASS",
                request={"plan_id":plan_id,"plan_sha256":record["plan_sha256"]},
                result={"steps":1,"verified":True,"rollback_available":record["rollback"]["available"]},
                principal="local-operator",
            )
            memory.append("change", {
                "action":"plan.apply", "plan_id":plan_id, "plan_sha256":record["plan_sha256"],
                "capability":"package.install", "package":package, "version":result.get("version"),
                "verified":True, "rollback_available":record["rollback"]["available"],
            }, source="system")
            memory.checkpoint()
            return self.show(plan_id)


        if cap in PRIVILEGED_CHANGE_CAPABILITIES and cap != "package.install":
            if approval.get("source") != "LOCAL_TTY_PRIVILEGED":
                raise ProductError("PLAN_PRIVILEGED_APPROVAL_SOURCE_INVALID")
            step = plan["steps"][0]
            args = step["args"]
            expected_args_sha = _args_sha(args)
            if approval.get("args_sha256") != expected_args_sha:
                raise ProductError("PLAN_APPROVAL_ARGS_HASH_MISMATCH")

            if cap == "package.remove":
                result = self._run_priv_helper([
                    "apply-remove", "--plan-id", plan_id,
                    "--plan-sha256", record["plan_sha256"],
                    "--package", args["package"],
                ])
            elif cap == "service.manage":
                result = self._run_priv_helper([
                    "apply-service", "--plan-id", plan_id,
                    "--plan-sha256", record["plan_sha256"],
                    "--service", args["service"],
                    "--action", args["action"],
                ])
            elif cap == "system.hostname":
                result = self._run_priv_helper([
                    "apply-hostname", "--plan-id", plan_id,
                    "--plan-sha256", record["plan_sha256"],
                    "--hostname", args["hostname"],
                ])
            elif cap == "system.timezone":
                result = self._run_priv_helper([
                    "apply-timezone", "--plan-id", plan_id,
                    "--plan-sha256", record["plan_sha256"],
                    "--timezone", args["timezone"],
                ])
            elif cap in ADMIN_R3_CORE_CAPABILITIES:
                result = self._run_priv_helper([
                    "apply-admin", "--plan-id", plan_id,
                    "--plan-sha256", record["plan_sha256"],
                    "--capability", cap,
                    "--args-sha256", expected_args_sha,
                ], input_text=json.dumps(args, sort_keys=True, separators=(",", ":")))
            else:
                raise ProductError("PLAN_CAPABILITY_NOT_ALLOWED")

            approval["consumed"] = True
            approval["consumed_at"] = utc_now()
            record["approval"] = approval
            applied = [{
                "step_id": step["id"],
                "capability": cap,
                "args_sha256": expected_args_sha,
                "verified": result.get("ok") is True,
            }]
            if cap.startswith("package."):
                applied[0]["package"] = args["package"]
            if cap == "service.manage":
                applied[0]["service"] = args["service"]
                applied[0]["action"] = args["action"]
            if cap == "system.hostname":
                applied[0]["hostname"] = args["hostname"]
            if cap == "system.timezone":
                applied[0]["timezone"] = args["timezone"]

            record["apply"] = {"applied_at": utc_now(), "steps": applied}
            record["rollback"] = {
                "available": result.get("rollback_available") is True,
                "kind": cap + ".rollback",
            }
            record["state"] = "APPLIED"
            self._write_record(plan_id, record)
            self.seed.audit.append(
                "plan.apply", "PASS",
                request={
                    "plan_id": plan_id,
                    "plan_sha256": record["plan_sha256"],
                    "capability": cap,
                    "args_sha256": expected_args_sha,
                },
                result={
                    "steps": 1,
                    "verified": result.get("ok") is True,
                    "rollback_available": record["rollback"]["available"],
                },
                principal="local-operator",
            )
            memory.append("change", {
                "action":"plan.apply",
                "plan_id":plan_id,
                "plan_sha256":record["plan_sha256"],
                "capability":cap,
                "args_sha256":expected_args_sha,
                "verified":True,
                "rollback_available":record["rollback"]["available"],
            }, source="system")
            memory.checkpoint()
            return self.show(plan_id)


        try:
            for step in plan["steps"]:
                if step["capability"] != "workspace.write_text":
                    raise ProductError("PLAN_CAPABILITY_NOT_ALLOWED")
                target = self._validate_workspace_path(step["args"]["path"])
                backup = self._backup_step(plan_id, step["id"], target)
                write_meta = self._atomic_write(target, step["args"]["text"])
                verify = target.read_bytes()
                if sha256(verify) != write_meta["sha256_after"]:
                    raise ProductError("PLAN_VERIFY_HASH_MISMATCH")
                applied.append({
                    "step_id": step["id"],
                    "capability": step["capability"],
                    "target": str(target),
                    **write_meta,
                    "verified": True,
                })
                rollback_meta.append(backup)
        except Exception:
            for meta in reversed(rollback_meta):
                self._restore_meta(meta)
            raise

        approval["consumed"] = True
        approval["consumed_at"] = utc_now()
        record["approval"] = approval
        record["apply"] = {
            "applied_at": utc_now(),
            "steps": applied,
        }
        record["rollback"] = {
            "available": True,
            "steps": rollback_meta,
        }
        record["state"] = "APPLIED"
        self._write_record(plan_id, record)
        self.seed.audit.append(
            "plan.apply",
            "PASS",
            request={
                "plan_id": plan_id,
                "plan_sha256": record["plan_sha256"],
            },
            result={
                "steps": len(applied),
                "verified": True,
                "rollback_available": True,
            },
            principal="local-operator",
        )
        memory.append("change", {
            "action":"plan.apply", "plan_id":plan_id, "plan_sha256":record["plan_sha256"],
            "capability":"workspace.write_text", "steps":len(applied), "verified":True, "rollback_available":True,
        }, source="system")
        memory.checkpoint()
        return self.show(plan_id)

    def approve_rollback(self, plan_id: str) -> dict[str, Any]:
        record = self._load_record(plan_id)
        if record.get("state") != "APPLIED":
            raise ProductError("PLAN_NOT_APPLIED")
        rollback = record.get("rollback") or {}
        if rollback.get("available") is not True:
            raise ProductError("ROLLBACK_NOT_AVAILABLE")

        cap = self._plan_capability(record)
        step = record["plan"]["steps"][0] if cap != "mixed" else None
        approval = {
            "plan_sha256": record.get("plan_sha256"),
            "approved_at": utc_now(),
            "one_shot": True,
            "consumed": False,
        }

        if cap == "package.install":
            package = step["args"]["package"]
            self._run_priv_helper([
                "approve-rollback", "--plan-id", plan_id,
                "--plan-sha256", record["plan_sha256"],
                "--package", package,
            ])
            source = "LOCAL_TTY_PRIVILEGED"
            approval["package"] = package
            approval["capability"] = cap
            audit_result = {"one_shot": True, "capability": cap, "package": package}
        elif cap in PRIVILEGED_CHANGE_CAPABILITIES:
            args_sha = _args_sha(step["args"])
            self._run_priv_helper([
                "approve-change-rollback", "--plan-id", plan_id,
                "--plan-sha256", record["plan_sha256"],
                "--capability", cap,
                "--args-sha256", args_sha,
            ])
            source = "LOCAL_TTY_PRIVILEGED"
            approval["capability"] = cap
            approval["args_sha256"] = args_sha
            audit_result = {"one_shot": True, "capability": cap, "args_sha256": args_sha}
        else:
            source = "LOCAL_TTY"
            audit_result = {"one_shot": True, "capability": cap}

        approval["source"] = source
        record["rollback_approval"] = approval
        self._write_record(plan_id, record)
        self.seed.audit.append(
            "plan.rollback.approve",
            "PASS",
            request={
                "plan_id": plan_id,
                "plan_sha256": record.get("plan_sha256"),
                "source": source,
                "capability": cap,
            },
            result=audit_result,
            principal="local-operator",
        )
        return self.show(plan_id)

    def _restore_meta(self, meta: dict[str, Any]) -> None:
        target = Path(str(meta["target"]))
        if meta.get("existed"):
            backup = Path(str(meta.get("backup") or ""))
            if not backup.is_file():
                raise ProductError("ROLLBACK_BACKUP_MISSING")
            target.parent.mkdir(parents=True, exist_ok=True)
            data = backup.read_bytes()
            fd, tmp_name = tempfile.mkstemp(prefix=".framebridge-rb-", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, target)
                os.chmod(target, int(meta.get("mode") or 0o600))
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        else:
            try:
                target.unlink()
            except FileNotFoundError:
                pass

    def rollback(self, plan_id: str) -> dict[str, Any]:
        memory = MemoryStore(self.seed)
        memory.require_ready()
        record = self._load_record(plan_id)
        if record.get("state") != "APPLIED":
            raise ProductError("PLAN_NOT_APPLIED")
        rollback = record.get("rollback") or {}
        if rollback.get("available") is not True:
            raise ProductError("ROLLBACK_NOT_AVAILABLE")

        approval = record.get("rollback_approval") or {}
        cap = self._plan_capability(record)
        cap = self._plan_capability(record)
        if cap == "package.install":
            if approval.get("source") not in ("LOCAL_TTY_PRIVILEGED", REMOTE_APPROVAL_SOURCE, TOTP_APPROVAL_SOURCE):
                raise ProductError("ROLLBACK_APPROVAL_SOURCE_INVALID")
            if approval.get("source") == REMOTE_APPROVAL_SOURCE and not self._remote_approval_metadata_valid(approval):
                raise ProductError("ROLLBACK_REMOTE_APPROVAL_INVALID")
            if approval.get("source") == TOTP_APPROVAL_SOURCE and not self._totp_approval_metadata_valid(approval):
                raise ProductError("ROLLBACK_TOTP_APPROVAL_INVALID")
        elif cap in PRIVILEGED_CHANGE_CAPABILITIES:
            if approval.get("source") != "LOCAL_TTY_PRIVILEGED":
                raise ProductError("ROLLBACK_APPROVAL_SOURCE_INVALID")
        elif approval.get("source") != "LOCAL_TTY":
            raise ProductError("ROLLBACK_APPROVAL_SOURCE_INVALID")
        if approval.get("plan_sha256") != record.get("plan_sha256"):
            raise ProductError("ROLLBACK_APPROVAL_HASH_MISMATCH")
        if approval.get("one_shot") is not True or approval.get("consumed") is True:
            raise ProductError("ROLLBACK_APPROVAL_CONSUMED")

        if cap == "package.install":
            step = record["plan"]["steps"][0]
            package = step["args"]["package"]
            result = self._run_priv_helper([
                "apply-rollback", "--plan-id", plan_id,
                "--plan-sha256", record["plan_sha256"],
                "--package", package,
            ])
            approval["consumed"] = True
            approval["consumed_at"] = utc_now()
            record["rollback_approval"] = approval
            rollback["available"] = False
            rollback["completed_at"] = utc_now()
            rollback["verified"] = result.get("rollback_verified") is True
            rollback["verification"] = [{
                "package": package,
                "absent_after_rollback": result.get("absent_after_rollback") is True,
                "verified": result.get("rollback_verified") is True,
            }]
            record["rollback"] = rollback
            record["state"] = "ROLLED_BACK"
            self._write_record(plan_id, record)
            self.seed.audit.append(
                "plan.rollback", "PASS", request={"plan_id":plan_id,"plan_sha256":record["plan_sha256"]},
                result={"steps":1,"rollback_approval_consumed":True,"verified":rollback["verified"]}, principal="local-operator",
            )
            memory.append("recovery", {
                "action":"plan.rollback", "plan_id":plan_id, "plan_sha256":record["plan_sha256"],
                "capability":"package.install", "package":package, "verified":rollback["verified"],
            }, source="system")
            memory.checkpoint()
            return self.show(plan_id)


        if cap in PRIVILEGED_CHANGE_CAPABILITIES and cap != "package.install":
            step = record["plan"]["steps"][0]
            args_sha = _args_sha(step["args"])
            if approval.get("args_sha256") != args_sha:
                raise ProductError("ROLLBACK_APPROVAL_ARGS_HASH_MISMATCH")
            result = self._run_priv_helper([
                "apply-change-rollback", "--plan-id", plan_id,
                "--plan-sha256", record["plan_sha256"],
                "--capability", cap,
                "--args-sha256", args_sha,
            ])
            approval["consumed"] = True
            approval["consumed_at"] = utc_now()
            record["rollback_approval"] = approval
            rollback["available"] = False
            rollback["completed_at"] = utc_now()
            rollback["verified"] = result.get("rollback_verified") is True
            verification = result.get("verification")
            rollback["verification"] = [verification] if isinstance(verification, dict) else []
            record["rollback"] = rollback
            record["state"] = "ROLLED_BACK"
            self._write_record(plan_id, record)
            self.seed.audit.append(
                "plan.rollback", "PASS",
                request={
                    "plan_id": plan_id,
                    "plan_sha256": record["plan_sha256"],
                    "capability": cap,
                    "args_sha256": args_sha,
                },
                result={
                    "steps": 1,
                    "rollback_approval_consumed": True,
                    "verified": rollback["verified"],
                },
                principal="local-operator",
            )
            memory.append("recovery", {
                "action":"plan.rollback",
                "plan_id":plan_id,
                "plan_sha256":record["plan_sha256"],
                "capability":cap,
                "rollback_verified":True,
            }, source="system")
            memory.checkpoint()
            return self.show(plan_id)


        metas = rollback.get("steps") or []
        applied_steps = (record.get("apply") or {}).get("steps") or []
        if len(applied_steps) != len(metas):
            raise ProductError("ROLLBACK_METADATA_MISMATCH")
        for step in applied_steps:
            target = Path(str(step.get("target") or ""))
            expected_after = str(step.get("sha256_after") or "")
            if not target.is_file():
                raise ProductError("ROLLBACK_TARGET_MISSING_OR_INVALID")
            current_sha = sha256(target.read_bytes())
            if current_sha != expected_after:
                raise ProductError("ROLLBACK_TARGET_CHANGED")

        for meta in reversed(metas):
            self._restore_meta(meta)

        verification: list[dict[str, Any]] = []
        for meta in metas:
            target = Path(str(meta.get("target") or ""))
            if meta.get("existed"):
                if not target.is_file():
                    raise ProductError("ROLLBACK_VERIFY_TARGET_MISSING")
                current_sha = sha256(target.read_bytes())
                expected_before = str(meta.get("sha256_before") or "")
                if current_sha != expected_before:
                    raise ProductError("ROLLBACK_VERIFY_HASH_MISMATCH")
                verification.append({
                    "step_id": meta.get("step_id"),
                    "target": str(target),
                    "restored_existing": True,
                    "sha256_restored": current_sha,
                    "verified": True,
                })
            else:
                if target.exists():
                    raise ProductError("ROLLBACK_VERIFY_TARGET_STILL_PRESENT")
                verification.append({
                    "step_id": meta.get("step_id"),
                    "target": str(target),
                    "restored_existing": False,
                    "absent_after_rollback": True,
                    "verified": True,
                })

        approval["consumed"] = True
        approval["consumed_at"] = utc_now()
        record["rollback_approval"] = approval
        rollback["available"] = False
        rollback["completed_at"] = utc_now()
        rollback["verified"] = True
        rollback["verification"] = verification
        record["rollback"] = rollback
        record["state"] = "ROLLED_BACK"
        self._write_record(plan_id, record)
        self.seed.audit.append(
            "plan.rollback",
            "PASS",
            request={
                "plan_id": plan_id,
                "plan_sha256": record.get("plan_sha256"),
            },
            result={
                "steps": len(metas),
                "rollback_approval_consumed": True,
                "verified": True,
            },
            principal="local-operator",
        )
        memory.append("recovery", {
            "action":"plan.rollback", "plan_id":plan_id, "plan_sha256":record.get("plan_sha256"),
            "capability":"workspace.write_text", "steps":len(metas), "verified":True,
        }, source="system")
        memory.checkpoint()
        return self.show(plan_id)

    def list(self) -> dict[str, Any]:
        items = []
        for path in sorted(self.plans_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                items.append({
                    "plan_id": path.stem,
                    "state": record.get("state"),
                    "title": (record.get("plan") or {}).get("title"),
                    "plan_sha256": record.get("plan_sha256"),
                })
            except Exception:
                items.append({"plan_id": path.stem, "state": "INVALID"})
        return {"ok": True, "plans": items}
