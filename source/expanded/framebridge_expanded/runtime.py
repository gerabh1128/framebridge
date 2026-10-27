"""Manual process supervision for FrameBridge Expanded; no system services."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from .config import load_config, state_path, private_dir

PREFIX = Path(os.environ.get("FRAMEBRIDGE_EXPANDED_PREFIX", Path(__file__).resolve().parents[2]))
_STOP = False
_LEGACY = None

def _legacy():
    global _LEGACY
    if _LEGACY is None:
        path = PREFIX / "bin/framebridge-original"
        loader = importlib.machinery.SourceFileLoader("framebridge_original_launcher", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        _LEGACY = importlib.util.module_from_spec(spec)
        loader.exec_module(_LEGACY)
    return _LEGACY

def _args(state, config):
    return SimpleNamespace(state_dir=str(state), port=config["ports"]["mcp"],
                           hub_port=config["ports"]["hub"], broker_port=config["ports"]["broker"])

def environment(state, config=None, prepare=False):
    state = state_path(state)
    config = config or load_config(state)
    original = _legacy()
    _, _, env = (original.prepare if prepare else original.settings)(_args(state, config))
    for key in list(env):
        upper = key.upper()
        if upper.startswith(("CONTROL_PLANE_", "OPENAI_", "MCP_TUNNEL_", "TUNNEL_")) or upper in ("HARPOON_API_KEY", "RUNTIMES_API_KEY"):
            env.pop(key, None)
    broker_source = PREFIX / "original/geranas/FrameBridge-Broker-RC1/src"
    fleet_source = PREFIX / "original/geranas/FrameBridge-Fleet-Hub/app"
    env.update({
        "FRAMEBRIDGE_EXPANDED_STATE": str(state), "FRAMEBRIDGE_STATE_DIR": str(state),
        "FRAMEBRIDGE_EXPANDED_PREFIX": str(PREFIX),
        "FRAMEBRIDGE_NATIVE_HOST": str(PREFIX / "bin/framebridge-native-expanded"),
        "FRAMEBRIDGE_BROKER_SOURCE": str(broker_source),
        "FRAMEBRIDGE_FLEET_SOURCE": str(fleet_source),
        "FRAMEBRIDGE_FRONT_DOOR_PORT": str(config["ports"]["mcp"]),
        "FRAMEBRIDGE_FLEET_HUB_PORT": str(config["ports"]["hub"]),
        "FRAMEBRIDGE_HUB_MCP_URL": "http://127.0.0.1:" + str(config["ports"]["hub"]) + "/mcp",
        "FRAMEBRIDGE_BROKER_CONTROL": config.get("broker_url") or "http://127.0.0.1:" + str(config["ports"]["broker"]),
        "PYTHONPATH": os.pathsep.join(map(str, [PREFIX / "expanded", PREFIX / "portable",
            PREFIX / "original/geranas/FrameBridge-Fleet-Hub/vendor", broker_source, fleet_source])),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
        "FBTERM_TITLE": "FrameBridge Expanded", "FRAMEBRIDGE_FBTERM_TITLE": "FrameBridge Expanded",
    })
    from .display_context import configure
    configure(env, config.get("headless", "auto"), state)
    return env

def _atomic(path, value):
    temporary = path.with_name("." + path.name + "-" + secrets.token_hex(4))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def _birth(pid):
    try:
        fields = Path("/proc", str(pid), "stat").read_text().rsplit(") ", 1)[1].split()
        return fields[19] if fields[0] not in ("Z", "X") else None
    except (OSError, IndexError):
        return None

def _alive(record):
    return bool(record.get("birth") and _birth(record.get("pid", 0)) == record["birth"])

def _record(proc, role):
    return {"pid": proc.pid, "birth": _birth(proc.pid), "role": role, "uid": os.getuid()}

def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}

def _port(port):
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0

def status(state):
    state = state_path(state)
    config = load_config(state)
    manager = _read(state / "manager.json")
    worker = _read(state / "worker.json")
    running = _alive(manager)
    children = [dict(r, alive=_alive(r)) for r in worker.get("processes", [])]
    ready = bool(running and children and all(r["alive"] for r in children)
                 and _read(state / "ready.json").get("ready"))
    context = _read(state / "ready.json").get("display_context", {"kind": "not_started"})
    if running and config.get("headless", "auto") == "auto":
        from .display_context import describe, bind_text
        try:
            current_env = environment(state, config)
            if not _display_ok(current_env):
                bind_text(current_env, state, reason="PHYSICAL_X11_UNAVAILABLE_TEXT_TOOLS_ACTIVE")
            context = describe(current_env)
        except (OSError, ValueError, RuntimeError):
            context = {"kind": "text", "physical": False, "reason": "DISPLAY_DETECTION_UNAVAILABLE"}
    return {"ok": True, "role": config["role"], "name": config["name"],
            "node_id": config["node_id"], "running": running, "ready": ready,
            "state_dir": str(state), "mcp_url": None if config["role"] == "node" else
            "http://127.0.0.1:" + str(config["ports"]["mcp"]) + "/mcp",
            "processes": children, "manual_chatgpt_connection": True,
            "display_context": context,
            "capabilities": {"fbexec": ready, "fbterm": ready, "cua_tty_module": ready,
                             "graphical_cua": ready and context.get("kind") in ("physical-x11", "virtual-x11")},
            "error": _read(state / "last-error.json").get("error", "")}

def _display_ok(env):
    if not env.get("DISPLAY"):
        return False
    try:
        result = subprocess.run(["xwininfo", "-root"], env=env, capture_output=True, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def doctor(state):
    state = state_path(state)
    config = load_config(state)
    env = environment(state, config)
    commands = ["bash", "tmux", "tee", "flock", "awk", "sha256sum", "timeout",
                "dbus-run-session", "ip", "lsblk", "systemctl", "systemd-run", "sudo", "uptime"]
    missing = [x for x in commands if not shutil.which(x, path=env.get("PATH"))]
    from .display_context import missing_graphical_commands, missing_virtual_commands, VIRTUAL_INSTALL_COMMAND
    graphical_missing = missing_graphical_commands(env)
    virtual_missing = missing_virtual_commands(env)
    console_missing = [x for x in ("gnome-terminal", "xterm") if not shutil.which(x, path=env.get("PATH"))]
    if config.get("headless") in ("no", False):
        missing.extend(graphical_missing)
    elif config.get("headless") in ("yes", True):
        missing.extend(graphical_missing + virtual_missing)
    from .compatibility import assess, display_support
    compatibility = assess(PREFIX)
    compatible = compatibility["ok"]
    issues = list(compatibility["issues"])
    if config["role"] == "central" and config.get("broker_listen_host", "127.0.0.1") not in ("127.0.0.1", "::1", "localhost"):
        if not config.get("tls_cert") or not config.get("tls_key"):
            issues.append("A central listener outside loopback requires a TLS certificate and private-key reference.")
    if config.get("headless") in ("no", False) and not _display_ok(env):
        issues.append("No usable X11 display. Select headless auto or provide DISPLAY and XAUTHORITY.")
    from .display_context import describe
    from .network import inspect as inspect_network
    display_context = describe(env)
    if config.get("headless") in ("no", False) and display_context.get("kind") != "physical-x11":
        issues.append("No verified active physical X11 session. Log in graphically or explicitly choose a virtual headless display.")
    return {"ok": compatible and not missing and not issues,
            "compatible_debian_13_amd64": compatible and compatibility["profile_id"] == "debian13-amd64-py313",
            "platform_compatibility": compatibility,
            "display_support": display_support(env, display_context),
            "python": platform.python_version(), "missing_commands": missing,
            "display": {"physical-x11": "existing physical X11", "virtual-x11": "explicit private virtual display", "text": "text tools"}.get(display_context.get("kind"), "unavailable"),
            "display_context": display_context,
            "capabilities": {"fbexec": not missing, "fbterm": not missing, "cua_tty_module": True,
                             "graphical_cua": display_context.get("kind") in ("physical-x11", "virtual-x11") and not graphical_missing,
                             "private_desktop": compatible and not missing and not graphical_missing and not virtual_missing,
                             "mode": "graphical" if display_context.get("kind") in ("physical-x11", "virtual-x11") else "text"},
            "optional_missing_commands": {"graphical_cua": graphical_missing, "private_desktop": virtual_missing, "graphical_terminal": console_missing},
            "optional_install_commands": {"private_desktop": VIRTUAL_INSTALL_COMMAND if virtual_missing else None},
            "network": inspect_network(),
            "role": config["role"], "issues": issues}

def adapter_request(payload):
    state = state_path()
    env = environment(state)
    is_cua = payload.get("request_type") == "cua" or payload.get("mode") == "CUA"
    if is_cua:
        from .display_context import check, describe, prepare, DisplayContextError
        try:
            prepare(env)
            check(env)
        except DisplayContextError as exc:
            return {"ok": False, "error": str(exc), "performed": False, "input_may_have_been_sent": False, "do_not_replay": False}
    path = Path(env["FRAMEBRIDGE_TOOL_ROOT"]) / "adapter.sock"
    st = path.stat()
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        raise RuntimeError("The local adapter socket must be owned by this user with mode 0600.")
    request = dict(payload)
    timeout = request.get("timeout_s", 180 if request.get("mode") == "CUA" else 120)
    if type(timeout) is not int or not 1 <= timeout <= 600:
        return {"ok": False, "error": "INVALID_TIMEOUT"}
    raw = (json.dumps(request, ensure_ascii=True) + "\n").encode()
    send_attempted = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout + 15)
            client.connect(str(path))
            send_attempted = True
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            data = bytearray()
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 20 * 1024 * 1024:
                    raise RuntimeError("The adapter response exceeded its size limit.")
        result = json.loads(data)
    except (OSError, TimeoutError, ValueError, RuntimeError):
        if not is_cua:
            raise
        from .adapter import _blocked
        is_input = str(payload.get("cua", {}).get("action", "")).upper() != "OBSERVE"
        return _blocked("CUA_ADAPTER_RESPONSE_UNCERTAIN", send_attempted and is_input)
    if not isinstance(result, dict):
        if is_cua:
            from .adapter import _blocked
            is_input = str(payload.get("cua", {}).get("action", "")).upper() != "OBSERVE"
            return _blocked("CUA_ADAPTER_RESPONSE_INVALID", is_input)
        return {"ok": False, "error": "ADAPTER_RESPONSE_INVALID"}
    if request.get("include_stdout") and result.get("stdout_path"):
        stdout_path = Path(result["stdout_path"]).resolve()
        allowed = Path(env["FBTERM_ROOT"]).resolve() / "jobs"
        if stdout_path.is_relative_to(allowed) and stdout_path.is_file():
            data = stdout_path.read_bytes()
            text = data[:65536].decode("utf-8", "replace")
            text = re.sub(r"(?i)sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}", "[REDACTED]", text)
            result["stdout"] = text
            result["stdout_truncated"] = len(data) > 65536
    if is_cua:
        try:
            check(env)
        except DisplayContextError as exc:
            is_input = str(payload.get("cua", {}).get("action", "")).upper() != "OBSERVE"
            may_have_sent = bool(result.get("input_may_have_been_sent", is_input))
            failure = {"ok": False, "error": str(exc), "result_discarded": True, "input_may_have_been_sent": may_have_sent, "do_not_replay": may_have_sent}
            if not may_have_sent:
                failure["performed"] = False
            return failure
        result = dict(result, display_context=describe(env))
    return result

def _spawn(command, env, state, role):
    log = (state / "logs" / (role + ".log")).open("ab")
    try:
        proc = subprocess.Popen(command, env=env, cwd=state, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, umask=0o077)
    finally:
        log.close()
    return proc, _record(proc, role)

def _terminate(proc, record):
    if not _alive(record):
        return
    if record.get("uid") != os.getuid():
        raise RuntimeError("Refusing to stop a process owned by a different user.")
    try:
        os.killpg(record["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 8
    while _alive(record) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _alive(record):
        os.killpg(record["pid"], signal.SIGKILL)
    if proc:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

def _signal(*_):
    global _STOP
    _STOP = True

def _worker(state):
    state = state_path(state)
    config = load_config(state)
    env = environment(state, config, prepare=True)
    os.environ.update(env)
    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    commands = [
        ("adapter", [sys.executable, "-m", "framebridge_expanded.adapter", "serve"]),
        ("tty", [sys.executable, "-m", "framebridge_expanded.tty", "serve", "--state", str(state)])]
    if config["role"] == "central":
        code = "from framebridge_expanded.broker import serve; serve(host=" + repr(config.get("broker_listen_host", "127.0.0.1")) + ",port=" + str(config["ports"]["broker"]) + ",state_dir=" + repr(str(state)) + ")"
        commands.append(("broker", [sys.executable, "-c", code]))
    if config["role"] == "node":
        code = "from framebridge_expanded.node import run; run(state_dir=" + repr(str(state)) + ")"
        commands.append(("node", [sys.executable, "-c", code]))
    else:
        commands.extend([
            ("fleet", [sys.executable, "-m", "framebridge_expanded.fleet"]),
            ("frontdoor", [sys.executable, str(PREFIX / "portable/front_door_mcp.py")])])
    processes = []
    try:
        for role, command in commands:
            proc, rec = _spawn(command, env, state, role)
            processes.append((proc, rec))
        _atomic(state / "worker.json", {"processes": [r for _, r in processes]})
        deadline = time.monotonic() + 35
        ready = False
        while not _STOP and time.monotonic() < deadline:
            if any(p.poll() is not None for p, _ in processes):
                raise RuntimeError("A component exited during startup. Check this profile's component logs.")
            adapter = Path(env["FRAMEBRIDGE_TOOL_ROOT"], "adapter.sock").is_socket()
            ports = True if config["role"] == "node" else all(_port(config["ports"][k]) for k in ("mcp", "hub"))
            if config["role"] == "central":
                ports = ports and _port(config["ports"]["broker"])
            try:
                from . import tty
                tty_ready = bool(tty.status(state).get("ok"))
            except Exception:
                tty_ready = False
            if adapter and ports and tty_ready:
                ready = True
                break
            time.sleep(0.2)
        if not ready:
            raise RuntimeError("Components did not become ready before the startup deadline.")
        from .display_context import describe
        _atomic(state / "ready.json", {"ready": True, "display": env.get("DISPLAY", ""), "display_context": describe(env)})
        while not _STOP:
            if any(p.poll() is not None for p, _ in processes):
                raise RuntimeError("A component stopped unexpectedly. Check this profile's component logs.")
            time.sleep(0.25)
    finally:
        _atomic(state / "ready.json", {"ready": False})
        try:
            from . import tty
            tty.stop(state)
        except Exception:
            pass
        for proc, rec in reversed(processes):
            _terminate(proc, rec)
        subprocess.run(["tmux", "kill-server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _manager(state):
    state = state_path(state)
    config = load_config(state)
    env = environment(state, config, prepare=True)
    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    lock = (state / "manager.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    processes = []
    try:
        from .display_context import describe, bind_virtual
        from .display_context import private_display_required
        display_available = _display_ok(env)
        private_display = private_display_required(env, config.get("headless", "auto"), display_available)
        if config.get("headless", "auto") == "auto" and not display_available:
            from .display_context import bind_text
            bind_text(env, state, reason="PHYSICAL_X11_UNAVAILABLE_TEXT_TOOLS_ACTIVE")
        if private_display:
            from .display_context import missing_virtual_commands, VIRTUAL_INSTALL_COMMAND
            missing_virtual = missing_virtual_commands(env)
            if missing_virtual:
                raise RuntimeError("Private virtual desktop tools are missing: " + ", ".join(missing_virtual) + ". Install them explicitly: " + VIRTUAL_INSTALL_COMMAND)
            display = next((n for n in range(140, 240) if not Path("/tmp/.X11-unix/X" + str(n)).exists() and not Path("/tmp/.X" + str(n) + "-lock").exists()), None)
            if display is None:
                raise RuntimeError("No private X11 display number is available.")
            env["DISPLAY"] = ":" + str(display)
            env["XDG_SESSION_TYPE"] = "x11"
            env["XDG_CURRENT_DESKTOP"] = "Openbox"
            env["GDK_BACKEND"] = "x11"
            authority = Path(env["XDG_RUNTIME_DIR"]) / "Xauthority"
            env["XAUTHORITY"] = str(authority)
            result = subprocess.run(["xauth", "-f", str(authority), "add", env["DISPLAY"], "MIT-MAGIC-COOKIE-1", secrets.token_hex(16)], env=env, capture_output=True, timeout=5)
            if result.returncode:
                raise RuntimeError("Could not create private X11 display authorization.")
            proc, rec = _spawn(["Xvfb", env["DISPLAY"], "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-noreset", "-auth", str(authority)], env, state, "display")
            processes.append((proc, rec))
            deadline = time.monotonic() + 10
            while not _display_ok(env) and time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("The private display failed to start.")
                time.sleep(0.15)
            if not _display_ok(env):
                raise RuntimeError("The private display did not become ready.")
            proc, rec = _spawn(["openbox", "--sm-disable"], env, state, "window-manager")
            processes.append((proc, rec))
        if private_display:
            bind_virtual(env, state, explicit=config.get("headless") in ("yes", True))
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        proc, rec = _spawn(["dbus-run-session", "--", sys.executable, "-m", "framebridge_expanded.runtime", "_worker", "--state", str(state)], env, state, "private-session")
        processes.append((proc, rec))
        _atomic(state / "manager-children.json", {"processes": [r for _, r in processes]})
        while not _STOP:
            if any(p.poll() is not None for p, _ in processes):
                raise RuntimeError("The private runtime stopped. Check this profile's logs.")
            time.sleep(0.25)
    except Exception as exc:
        _atomic(state / "last-error.json", {"error": str(exc)})
        raise
    finally:
        try:
            for proc, rec in reversed(processes):
                _terminate(proc, rec)
            _atomic(state / "ready.json", {"ready": False})
        finally:
            lock.close()

def start(state):
    state = state_path(state)
    config = load_config(state)
    current = status(state)
    if current["running"]:
        return current
    checks = doctor(state)
    if not checks["ok"]:
        error = "Preflight failed. Run framebridge doctor."
        hint = checks.get("optional_install_commands", {}).get("private_desktop")
        if config.get("headless") in ("yes", True) and hint:
            error = "Private virtual desktop tools are missing. Install them explicitly: " + hint + ". Then run framebridge doctor."
        return {"ok": False, "error": error, "doctor": checks}
    for name in ("logs", "run"):
        private_dir(state / name)
    for name in ("ready.json", "last-error.json", "worker.json"):
        (state / name).unlink(missing_ok=True)
    if config["role"] != "node":
        for key in ("mcp", "hub") + (("broker",) if config["role"] == "central" else ()):
            with socket.socket() as probe:
                # Match server restart semantics while still rejecting live listeners.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", config["ports"][key]))
    env = environment(state, config, prepare=True)
    proc, rec = _spawn([sys.executable, "-m", "framebridge_expanded.runtime", "_manager", "--state", str(state)], env, state, "manager")
    _atomic(state / "manager.json", rec)
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline and _alive(rec):
        result = status(state)
        if result["ready"]:
            return result
        if (state / "last-error.json").exists():
            break
        time.sleep(0.2)
    _terminate(proc, rec)
    result = status(state)
    result.update(ok=False, error=result["error"] or "Startup failed. Check this profile's logs.")
    return result

def stop(state):
    state = state_path(state)
    load_config(state)
    from .connection_background import disconnect
    connection = disconnect(state)
    if not connection.get("ok"):
        raise RuntimeError("The background ChatGPT connection could not be stopped safely; local services were left unchanged.")
    from . import tty

    def verify_tty_stop():
        outcome = tty.stop(state)
        if (not isinstance(outcome, dict) or outcome.get("ok") is not True
                or outcome.get("cleanup_verified") is not True):
            raise RuntimeError("TTY cleanup could not be verified; inspect this profile's terminal status before retrying.")

    # Verify terminal cleanup before stopping local services, then again after
    # their termination to cover a terminal opened during the stop operation.
    verify_tty_stop()
    from .autostart import mark_manual_stop
    mark_manual_stop(state)
    rec = _read(state / "manager.json")
    _terminate(None, rec)
    children = _read(state / "manager-children.json").get("processes", [])
    for child in reversed(children):
        _terminate(None, child)
    for child in reversed(_read(state / "worker.json").get("processes", [])):
        _terminate(None, child)
    _atomic(state / "ready.json", {"ready": False})
    verify_tty_stop()
    result = status(state)
    result["tty_cleanup_verified"] = True
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["_manager", "_worker"])
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    (_manager if args.command == "_manager" else _worker)(args.state)

if __name__ == "__main__":
    main()
