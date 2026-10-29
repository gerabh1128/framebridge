from __future__ import annotations

import hashlib
import json
import os
import re
import platform
import shutil
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .product_core import ProductError

MAX_OUTPUT = 32768


def _run(args: list[str], timeout: float = 5.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "stdout": proc.stdout[:MAX_OUTPUT],
            "stderr": proc.stderr[:4096],
        }
    except FileNotFoundError:
        return {"ok": False, "error": "COMMAND_NOT_FOUND"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "COMMAND_TIMEOUT"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def _os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key] = value.strip().strip('"')
    return out


def system_status(args: dict[str, Any]) -> dict[str, Any]:
    osr = _os_release()
    uptime = _run(["uptime", "-p"]) if shutil.which("uptime") else {"ok": False}
    return {
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "os_id": osr.get("ID"),
        "os_name": osr.get("PRETTY_NAME"),
        "uptime": uptime.get("stdout", "").strip(),
    }


def hardware_summary(args: dict[str, Any]) -> dict[str, Any]:
    cpu_model = ""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(errors="replace").splitlines():
            if line.lower().startswith("model name") and ":" in line:
                cpu_model = line.split(":", 1)[1].strip()
                break

    mem_total_kib = None
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    mem_total_kib = int(parts[1])
                break

    return {
        "cpu_model": cpu_model or None,
        "logical_cpus": os.cpu_count(),
        "memory_total_kib": mem_total_kib,
        "architecture": platform.machine(),
    }


def storage_inspect(args: dict[str, Any]) -> dict[str, Any]:
    if not shutil.which("lsblk"):
        return {"ok": False, "error": "LSBLK_NOT_FOUND"}
    result = _run([
        "lsblk",
        "-J",
        "-o",
        "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,ROTA,MODEL",
    ])
    if not result.get("ok"):
        return result
    try:
        return {
            "ok": True,
            "blockdevices": json.loads(result["stdout"]).get("blockdevices", []),
        }
    except Exception:
        return {"ok": False, "error": "LSBLK_JSON_INVALID"}


def network_inspect(args: dict[str, Any]) -> dict[str, Any]:
    if not shutil.which("ip"):
        return {"ok": False, "error": "IP_COMMAND_NOT_FOUND"}
    addresses = _run(["ip", "-j", "address"])
    routes = _run(["ip", "-j", "route"])
    out: dict[str, Any] = {
        "ok": bool(addresses.get("ok") and routes.get("ok")),
    }
    try:
        out["addresses"] = json.loads(addresses.get("stdout", "[]"))
    except Exception:
        out["addresses"] = []
        out["ok"] = False
    try:
        out["routes"] = json.loads(routes.get("stdout", "[]"))
    except Exception:
        out["routes"] = []
        out["ok"] = False
    return out


def service_status(args: dict[str, Any]) -> dict[str, Any]:
    service = str(args.get("service") or "").strip()
    if not service:
        raise ProductError("SERVICE_REQUIRED")
    if len(service) > 128:
        raise ProductError("SERVICE_NAME_TOO_LONG")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@_.-")
    if any(ch not in allowed for ch in service):
        raise ProductError("SERVICE_NAME_INVALID")
    if not shutil.which("systemctl"):
        return {"ok": False, "error": "SYSTEMCTL_NOT_FOUND"}

    active = _run(["systemctl", "is-active", service])
    enabled = _run(["systemctl", "is-enabled", service])
    return {
        "service": service,
        "active": active.get("stdout", "").strip(),
        "active_rc": active.get("rc"),
        "enabled": enabled.get("stdout", "").strip(),
        "enabled_rc": enabled.get("rc"),
    }



PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
SIMPLE_NAME_RE = re.compile(r"^[A-Za-z0-9@_.-]{1,128}$")
SAFE_TEXT_ROOTS = (
    Path("/etc"),
    Path("/usr/local/etc"),
    Path("/var/log"),
    Path("/var/lib/framebridge-seed"),
)
SAFE_TEXT_FILES = {
    Path("/usr/lib/os-release"),
}
SENSITIVE_PATH_PARTS = (
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d/",
    "/etc/ssl/private/",
    "/etc/wireguard/",
    "/etc/openvpn/",
    "/etc/NetworkManager/system-connections/",
    "/etc/apt/auth.conf",
    "/.ssh/",
)
SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:^|[._-])(?:secret|token|password|passwd|credential|private[_-]?key|api[_-]?key)(?:$|[._-])"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)^\s*([A-Za-z0-9_.-]*(?:secret|token|password|passwd|api[_-]?key|"
    r"private[_-]?key|cookie|authorization|credential)[A-Za-z0-9_.-]*)\s*[:=]"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@")
PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
PRIVATE_KEY_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _bounded_int(value: Any, *, default: int, low: int, high: int, error: str) -> int:
    if value is None:
        return default
    if type(value) is not int or not (low <= value <= high):
        raise ProductError(error)
    return value


def _simple_name(value: Any, error: str) -> str:
    name = str(value or "").strip()
    if not SIMPLE_NAME_RE.fullmatch(name):
        raise ProductError(error)
    return name


def _package_name(value: Any) -> str:
    package = str(value or "").strip()
    if not PACKAGE_RE.fullmatch(package):
        raise ProductError("PACKAGE_NAME_INVALID")
    return package


def _sanitize_text(text: str) -> str:
    out: list[str] = []
    in_key = False
    for line in str(text).splitlines():
        if PRIVATE_KEY_BEGIN_RE.search(line):
            in_key = True
            out.append("[REDACTED PRIVATE KEY BLOCK]")
            continue
        if in_key:
            if PRIVATE_KEY_END_RE.search(line):
                in_key = False
            continue
        m = SENSITIVE_ASSIGNMENT_RE.search(line)
        if m:
            out.append(f"{m.group(1)}=[REDACTED]")
            continue
        line = BEARER_RE.sub("Bearer [REDACTED]", line)
        line = URL_USERINFO_RE.sub(r"\1[REDACTED]@", line)
        out.append(line)
    return "\n".join(out)


def _safe_text_path(raw: Any) -> Path:
    value = str(raw or "").strip()
    if not value or len(value) > 4096 or "\x00" in value or "\n" in value:
        raise ProductError("PATH_INVALID")
    src = Path(value)
    if not src.is_absolute():
        raise ProductError("PATH_ABSOLUTE_REQUIRED")
    try:
        resolved = src.resolve(strict=True)
    except FileNotFoundError:
        raise ProductError("PATH_NOT_FOUND") from None
    except OSError:
        raise ProductError("PATH_INVALID") from None

    allowed = False
    for root in SAFE_TEXT_ROOTS:
        try:
            if os.path.commonpath([str(root), str(resolved)]) == str(root):
                allowed = True
                break
        except ValueError:
            pass
    if not allowed and resolved not in SAFE_TEXT_FILES:
        raise ProductError("PATH_OUTSIDE_SAFE_READ_ROOTS")

    lower = str(resolved).lower()
    if any(part.lower() in lower for part in SENSITIVE_PATH_PARTS):
        raise ProductError("SENSITIVE_PATH_FORBIDDEN")
    if SENSITIVE_NAME_RE.search(resolved.name):
        raise ProductError("SENSITIVE_PATH_FORBIDDEN")
    if not resolved.is_file():
        raise ProductError("PATH_NOT_REGULAR_FILE")
    return resolved


def system_resources(args: dict[str, Any]) -> dict[str, Any]:
    mem: dict[str, int] = {}
    p = Path("/proc/meminfo")
    if p.is_file():
        for line in p.read_text(errors="replace").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            fields = rest.strip().split()
            if fields and fields[0].isdigit():
                mem[key] = int(fields[0])

    load = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)
    process_count = 0
    proc = Path("/proc")
    if proc.is_dir():
        process_count = sum(1 for x in proc.iterdir() if x.name.isdigit())

    return {
        "load_1": load[0],
        "load_5": load[1],
        "load_15": load[2],
        "memory_total_kib": mem.get("MemTotal"),
        "memory_available_kib": mem.get("MemAvailable"),
        "swap_total_kib": mem.get("SwapTotal"),
        "swap_free_kib": mem.get("SwapFree"),
        "process_count": process_count,
    }


def system_processes(args: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int(args.get("limit"), default=40, low=1, high=200, error="PROCESS_LIMIT_INVALID")
    result = _run([
        "ps", "-eo",
        "pid=,ppid=,user=,stat=,pcpu=,pmem=,etime=,comm=",
        "--sort=-pcpu",
    ])
    if not result.get("ok"):
        return result
    rows = []
    for raw in result.get("stdout", "").splitlines():
        parts = raw.split(None, 7)
        if len(parts) != 8:
            continue
        pid, ppid, user, state, cpu, mem, etime, comm = parts
        rows.append({
            "pid": int(pid) if pid.isdigit() else pid,
            "ppid": int(ppid) if ppid.isdigit() else ppid,
            "user": user,
            "state": state,
            "cpu_pct": cpu,
            "mem_pct": mem,
            "etime": etime,
            "comm": comm,
        })
        if len(rows) >= limit:
            break
    return {"ok": True, "processes": rows, "limit": limit}


def system_time(args: dict[str, Any]) -> dict[str, Any]:
    timezone = None
    if shutil.which("timedatectl"):
        r = _run(["timedatectl", "show", "-p", "Timezone", "--value"])
        if r.get("ok"):
            timezone = r.get("stdout", "").strip() or None
    if timezone is None:
        tz = Path("/etc/timezone")
        if tz.is_file():
            timezone = tz.read_text(errors="replace").strip() or None
    return {
        "local_time": datetime.now().astimezone().isoformat(),
        "timezone": timezone,
    }


def kernel_modules(args: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int(args.get("limit"), default=100, low=1, high=500, error="MODULE_LIMIT_INVALID")
    if not shutil.which("lsmod"):
        return {"ok": False, "error": "LSMOD_NOT_FOUND"}
    r = _run(["lsmod"])
    if not r.get("ok"):
        return r
    rows = []
    for line in r.get("stdout", "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        rows.append({
            "module": parts[0],
            "size": int(parts[1]) if parts[1].isdigit() else parts[1],
            "used_by_count": int(parts[2]) if parts[2].isdigit() else parts[2],
        })
        if len(rows) >= limit:
            break
    return {"ok": True, "modules": rows, "limit": limit}


def storage_usage(args: dict[str, Any]) -> dict[str, Any]:
    r = _run(["df", "-B1", "-P", "-T"])
    if not r.get("ok"):
        return r
    rows = []
    for line in r.get("stdout", "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        fs, fstype, total, used, avail, pct = parts[:6]
        mount = " ".join(parts[6:])
        rows.append({
            "filesystem": fs,
            "fstype": fstype,
            "bytes_total": int(total) if total.isdigit() else total,
            "bytes_used": int(used) if used.isdigit() else used,
            "bytes_available": int(avail) if avail.isdigit() else avail,
            "use_percent": pct,
            "mountpoint": mount,
        })
    return {"ok": True, "filesystems": rows}


def storage_mounts(args: dict[str, Any]) -> dict[str, Any]:
    if not shutil.which("findmnt"):
        return {"ok": False, "error": "FINDMNT_NOT_FOUND"}
    r = _run(["findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE"])
    if not r.get("ok"):
        return r
    try:
        return {"ok": True, "filesystems": json.loads(r["stdout"]).get("filesystems", [])}
    except Exception:
        return {"ok": False, "error": "FINDMNT_JSON_INVALID"}


def network_sockets(args: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int(args.get("limit"), default=100, low=1, high=500, error="SOCKET_LIMIT_INVALID")
    if not shutil.which("ss"):
        return {"ok": False, "error": "SS_NOT_FOUND"}
    r = _run(["ss", "-H", "-lntu"])
    if not r.get("ok"):
        return r
    rows = []
    for line in r.get("stdout", "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        rows.append({
            "netid": parts[0],
            "state": parts[1],
            "local": parts[4] if len(parts) > 4 else None,
            "peer": parts[5] if len(parts) > 5 else None,
        })
        if len(rows) >= limit:
            break
    return {"ok": True, "listeners": rows, "limit": limit}


def network_dns(args: dict[str, Any]) -> dict[str, Any]:
    path = Path("/etc/resolv.conf")
    if not path.exists():
        return {"ok": False, "error": "RESOLV_CONF_NOT_FOUND"}
    try:
        resolved = path.resolve(strict=True)
    except Exception:
        resolved = path
    lines = []
    try:
        text = path.read_text(errors="replace")
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("nameserver ", "search ", "domain ", "options ")):
            lines.append(stripped)
    return {
        "ok": True,
        "path": str(path),
        "resolved_path": str(resolved),
        "directives": lines,
    }


def package_status_read(args: dict[str, Any]) -> dict[str, Any]:
    package = _package_name(args.get("package"))
    r = _run(["dpkg-query", "-W", "-f=${Status}|${Version}|${Architecture}", package])
    if not r.get("ok"):
        return {"ok": True, "package": package, "installed": False, "version": None, "architecture": None}
    out = r.get("stdout", "").strip()
    parts = out.split("|")
    installed = len(parts) >= 1 and parts[0] == "install ok installed"
    return {
        "ok": True,
        "package": package,
        "installed": installed,
        "version": parts[1] if installed and len(parts) > 1 else None,
        "architecture": parts[2] if installed and len(parts) > 2 else None,
    }


def package_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query or len(query) > 80:
        raise ProductError("PACKAGE_QUERY_INVALID")
    if not re.fullmatch(r"[A-Za-z0-9+._ -]+", query):
        raise ProductError("PACKAGE_QUERY_INVALID")
    limit = _bounded_int(args.get("limit"), default=30, low=1, high=100, error="PACKAGE_LIMIT_INVALID")
    if not shutil.which("apt-cache"):
        return {"ok": False, "error": "APT_CACHE_NOT_FOUND"}
    r = _run(["apt-cache", "search", "--names-only", re.escape(query)], timeout=15.0)
    if not r.get("ok"):
        return r
    rows = []
    for line in r.get("stdout", "").splitlines():
        if " - " in line:
            name, desc = line.split(" - ", 1)
        else:
            name, desc = line, ""
        rows.append({"package": name.strip(), "description": desc.strip()})
        if len(rows) >= limit:
            break
    return {"ok": True, "query": query, "results": rows, "limit": limit}


def package_upgradable(args: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int(args.get("limit"), default=100, low=1, high=500, error="PACKAGE_LIMIT_INVALID")
    if not shutil.which("apt"):
        return {"ok": False, "error": "APT_NOT_FOUND"}
    r = _run(["apt", "list", "--upgradable"], timeout=20.0)
    if not r.get("ok"):
        return r
    rows = []
    for line in r.get("stdout", "").splitlines():
        line = line.strip()
        if not line or line.startswith("Listing...") or "/" not in line:
            continue
        first, *rest = line.split()
        name, suite = first.split("/", 1)
        version = rest[0] if rest else None
        rows.append({"package": name, "suite": suite, "candidate_version": version})
        if len(rows) >= limit:
            break
    return {"ok": True, "packages": rows, "limit": limit}


def service_list(args: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int(args.get("limit"), default=100, low=1, high=500, error="SERVICE_LIMIT_INVALID")
    if not shutil.which("systemctl"):
        return {"ok": False, "error": "SYSTEMCTL_NOT_FOUND"}
    r = _run([
        "systemctl", "list-units", "--type=service", "--all",
        "--no-legend", "--no-pager", "--plain",
    ], timeout=10.0)
    if not r.get("ok"):
        return r
    rows = []
    for line in r.get("stdout", "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        rows.append({
            "unit": parts[0],
            "load": parts[1],
            "active": parts[2],
            "sub": parts[3],
            "description": parts[4] if len(parts) > 4 else "",
        })
        if len(rows) >= limit:
            break
    return {"ok": True, "services": rows, "limit": limit}


def journal_service(args: dict[str, Any]) -> dict[str, Any]:
    service = _simple_name(args.get("service"), "SERVICE_NAME_INVALID")
    lines = _bounded_int(args.get("lines"), default=80, low=1, high=500, error="JOURNAL_LINES_INVALID")
    if not shutil.which("journalctl"):
        return {"ok": False, "error": "JOURNALCTL_NOT_FOUND"}
    r = _run([
        "journalctl", "-u", service, "-n", str(lines),
        "--no-pager", "--output=short-iso-precise",
    ], timeout=10.0)
    if not r.get("ok"):
        return r
    return {
        "ok": True,
        "service": service,
        "lines": lines,
        "text": _sanitize_text(r.get("stdout", ""))[:MAX_OUTPUT],
    }


def user_list(args: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int(args.get("limit"), default=200, low=1, high=1000, error="USER_LIMIT_INVALID")
    r = _run(["getent", "passwd"])
    if not r.get("ok"):
        return r
    rows = []
    for line in r.get("stdout", "").splitlines():
        parts = line.split(":")
        if len(parts) != 7:
            continue
        name, _pw, uid, gid, gecos, home, shell = parts
        rows.append({
            "name": name,
            "uid": int(uid) if uid.isdigit() else uid,
            "gid": int(gid) if gid.isdigit() else gid,
            "gecos": gecos,
            "home": home,
            "shell": shell,
        })
        if len(rows) >= limit:
            break
    return {"ok": True, "users": rows, "limit": limit}


def group_list(args: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_int(args.get("limit"), default=200, low=1, high=1000, error="GROUP_LIMIT_INVALID")
    r = _run(["getent", "group"])
    if not r.get("ok"):
        return r
    rows = []
    for line in r.get("stdout", "").splitlines():
        parts = line.split(":")
        if len(parts) != 4:
            continue
        name, _pw, gid, members = parts
        rows.append({
            "name": name,
            "gid": int(gid) if gid.isdigit() else gid,
            "members": [x for x in members.split(",") if x],
        })
        if len(rows) >= limit:
            break
    return {"ok": True, "groups": rows, "limit": limit}


def filesystem_stat(args: dict[str, Any]) -> dict[str, Any]:
    raw = str(args.get("path") or "").strip()
    if not raw or len(raw) > 4096 or "\x00" in raw or "\n" in raw:
        raise ProductError("PATH_INVALID")
    path = Path(raw)
    if not path.is_absolute():
        raise ProductError("PATH_ABSOLUTE_REQUIRED")
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"ok": True, "path": raw, "exists": False}
    return {
        "ok": True,
        "path": raw,
        "exists": True,
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "is_symlink": path.is_symlink(),
        "size": st.st_size,
        "mode": oct(st.st_mode & 0o7777),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mtime_ns": st.st_mtime_ns,
    }


def filesystem_list(args: dict[str, Any]) -> dict[str, Any]:
    raw = str(args.get("path") or "").strip()
    if not raw or len(raw) > 4096 or "\x00" in raw or "\n" in raw:
        raise ProductError("PATH_INVALID")
    path = Path(raw)
    if not path.is_absolute():
        raise ProductError("PATH_ABSOLUTE_REQUIRED")
    limit = _bounded_int(args.get("limit"), default=100, low=1, high=500, error="FILE_LIST_LIMIT_INVALID")
    if not path.is_dir():
        raise ProductError("DIRECTORY_NOT_FOUND")
    rows = []
    try:
        children = sorted(path.iterdir(), key=lambda x: x.name)
    except PermissionError:
        raise ProductError("DIRECTORY_PERMISSION_DENIED") from None
    for child in children[:limit]:
        try:
            st = child.lstat()
            rows.append({
                "name": child.name,
                "is_file": child.is_file(),
                "is_dir": child.is_dir(),
                "is_symlink": child.is_symlink(),
                "size": st.st_size,
                "mode": oct(st.st_mode & 0o7777),
            })
        except OSError:
            rows.append({"name": child.name, "error": "STAT_FAILED"})
    return {"ok": True, "path": raw, "entries": rows, "limit": limit}


def filesystem_hash(args: dict[str, Any]) -> dict[str, Any]:
    raw = str(args.get("path") or "").strip()
    if not raw or len(raw) > 4096 or "\x00" in raw or "\n" in raw:
        raise ProductError("PATH_INVALID")
    path = Path(raw)
    if not path.is_absolute():
        raise ProductError("PATH_ABSOLUTE_REQUIRED")
    if not path.is_file():
        raise ProductError("PATH_NOT_REGULAR_FILE")
    size = path.stat().st_size
    if size > 16 * 1024 * 1024:
        raise ProductError("FILE_TOO_LARGE_TO_HASH")
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except PermissionError:
        raise ProductError("FILE_PERMISSION_DENIED") from None
    return {"ok": True, "path": raw, "size": size, "sha256": h.hexdigest()}


def filesystem_read_text(args: dict[str, Any]) -> dict[str, Any]:
    path = _safe_text_path(args.get("path"))
    max_bytes = _bounded_int(args.get("max_bytes"), default=16384, low=1, high=32768, error="MAX_BYTES_INVALID")
    try:
        data = path.read_bytes()
    except PermissionError:
        raise ProductError("FILE_PERMISSION_DENIED") from None
    if len(data) > max_bytes:
        data = data[:max_bytes]
        truncated = True
    else:
        truncated = False
    if b"\x00" in data:
        raise ProductError("BINARY_FILE_NOT_ALLOWED")
    text = data.decode("utf-8", errors="replace")
    return {
        "ok": True,
        "path": str(path),
        "bytes_returned": len(data),
        "truncated": truncated,
        "text": _sanitize_text(text),
    }


CAPABILITIES = {
    "system.status": system_status,
    "system.resources": system_resources,
    "system.processes": system_processes,
    "system.time": system_time,
    "hardware.summary": hardware_summary,
    "kernel.modules": kernel_modules,
    "storage.inspect": storage_inspect,
    "storage.usage": storage_usage,
    "storage.mounts": storage_mounts,
    "network.inspect": network_inspect,
    "network.sockets": network_sockets,
    "network.dns": network_dns,
    "package.status": package_status_read,
    "package.search": package_search,
    "package.upgradable": package_upgradable,
    "service.status": service_status,
    "service.list": service_list,
    "journal.service": journal_service,
    "user.list": user_list,
    "group.list": group_list,
    "filesystem.stat": filesystem_stat,
    "filesystem.list": filesystem_list,
    "filesystem.hash": filesystem_hash,
    "filesystem.read_text": filesystem_read_text,
}


def list_capabilities() -> list[dict[str, Any]]:
    return [
        {"name": "system.status", "risk": "READONLY"},
        {"name": "system.resources", "risk": "READONLY"},
        {"name": "system.processes", "risk": "READONLY", "args": {"limit": "int:1..200"}},
        {"name": "system.time", "risk": "READONLY"},
        {"name": "hardware.summary", "risk": "READONLY"},
        {"name": "kernel.modules", "risk": "READONLY", "args": {"limit": "int:1..500"}},
        {"name": "storage.inspect", "risk": "READONLY"},
        {"name": "storage.usage", "risk": "READONLY"},
        {"name": "storage.mounts", "risk": "READONLY"},
        {"name": "network.inspect", "risk": "READONLY"},
        {"name": "network.sockets", "risk": "READONLY", "args": {"limit": "int:1..500"}},
        {"name": "network.dns", "risk": "READONLY"},
        {"name": "package.status", "risk": "READONLY", "args": {"package": "debian-package-name"}},
        {"name": "package.search", "risk": "READONLY", "args": {"query": "string", "limit": "int:1..100"}},
        {"name": "package.upgradable", "risk": "READONLY", "args": {"limit": "int:1..500"}},
        {"name": "service.status", "risk": "READONLY", "args": {"service": "systemd-unit"}},
        {"name": "service.list", "risk": "READONLY", "args": {"limit": "int:1..500"}},
        {"name": "journal.service", "risk": "READONLY", "args": {"service": "systemd-unit", "lines": "int:1..500"}},
        {"name": "user.list", "risk": "READONLY", "args": {"limit": "int:1..1000"}},
        {"name": "group.list", "risk": "READONLY", "args": {"limit": "int:1..1000"}},
        {"name": "filesystem.stat", "risk": "READONLY", "args": {"path": "absolute-path"}},
        {"name": "filesystem.list", "risk": "READONLY", "args": {"path": "absolute-directory", "limit": "int:1..500"}},
        {"name": "filesystem.hash", "risk": "READONLY", "args": {"path": "absolute-file<=16MiB"}},
        {"name": "filesystem.read_text", "risk": "READONLY", "args": {"path": "safe-absolute-text-path", "max_bytes": "int:1..32768"}},
    ]


def execute_capability(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(name).strip()
    fn = CAPABILITIES.get(name)
    if fn is None:
        raise ProductError("CAPABILITY_NOT_ALLOWED")
    args = args or {}
    if not isinstance(args, dict):
        raise ProductError("CAPABILITY_ARGS_NOT_OBJECT")
    return fn(args)
