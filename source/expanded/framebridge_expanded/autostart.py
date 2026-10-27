"""Explicit user-service startup with host-bound encrypted systemd credentials."""
from __future__ import annotations
import argparse
import fcntl
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import re
import resource
import secrets
import signal
import stat
import subprocess
import sys
import time
from .config import load_config, save_config, private_dir, state_path, default_config, validate

CREDENTIAL = "chatgpt-runtime-key"
_STOP = False


def _paths(state):
    state = state_path(state)
    directory = private_dir(private_dir(state) / "autostart")
    name = "framebridge-autostart-" + hashlib.sha256(str(state).encode()).hexdigest()[:16] + ".service"
    return state, directory, Path.home() / ".config/systemd/user" / name


def _read(path, maximum=131072):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError("Unsafe autostart file ownership or permissions.")
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise RuntimeError("Autostart file exceeds the size limit.")
        return data
    finally:
        os.close(fd)


def _write(path, data, replace=False):
    path = Path(path)
    if path.exists() or path.is_symlink():
        if not replace:
            raise RuntimeError("Refusing to replace an existing autostart file.")
        _read(path)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path, value, replace=False):
    _write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(), replace)


def _command(args, *, data=None, timeout=35, check=True):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Autostart command failed: " + Path(args[0]).name + ". No command output was exported.")
    return result


def _systemctl(*args, check=True):
    return _command(["systemctl", "--user", *args], check=check)


def _quote(value, *, command=False):
    value = str(value)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("Control characters are not allowed in service paths.")
    if command:
        value = value.replace("$", "$$")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit_text(state, encrypted, *, node=False):
    module_path = Path(__file__).resolve().parents[1]
    executable = [sys.executable, "-B", "-m", "framebridge_expanded.autostart", "_run", "--state", str(state)]
    lines = ["# FrameBridge managed autostart v1", "[Unit]", "Description=FrameBridge profile and ChatGPT connection", "StartLimitIntervalSec=0", "ConditionPathExists=" + _quote(module_path / "framebridge_expanded/autostart.py"), "", "[Service]", "Type=simple", "UMask=0077", "LimitCORE=0", "Environment=" + _quote("PYTHONPATH=" + str(module_path)), "Environment=LANG=C.UTF-8", "Environment=LC_ALL=C.UTF-8"]
    lines += ["ExecStart=" + " ".join(_quote(x, command=True) for x in executable), "Restart=on-failure", "RestartSec=15", "TimeoutStopSec=25", "KillMode=control-group", "StandardInput=null", "StandardOutput=null", "StandardError=null", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _linger(user):
    directory = Path("/var/lib/systemd/linger")
    try:
        info = directory.stat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or directory.is_symlink():
        raise RuntimeError("The systemd linger directory is unavailable or unsafe.")
    try:
        marker = (directory / user).lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(marker.st_mode) or marker.st_uid != 0:
        raise RuntimeError("The systemd linger record is unsafe.")
    return True


def _enabled_units():
    result = _systemctl("list-unit-files", "--state=enabled", "--no-legend", "--no-pager")
    return sorted(line.split()[0] for line in result.stdout.decode(errors="replace").splitlines() if line.split())


def _check_unit_directory(path):
    for directory in [Path.home() / ".config", Path.home() / ".config/systemd", path.parent]:
        if directory.is_symlink():
            raise RuntimeError("User service directories must not be symbolic links.")
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeError("User service directories must be owned by this user and not writable by others.")


def _manifest(directory):
    path = directory / "installation.json"
    try:
        value = json.loads(_read(path))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise RuntimeError("Unsupported autostart installation record.")
    return value


def status(state):
    state, directory, unit = _paths(state)
    manifest = _manifest(directory)
    properties = _systemctl("show", unit.name, "--property=ActiveState,SubState,UnitFileState", check=False)
    values = dict(line.split("=", 1) for line in properties.stdout.decode(errors="replace").splitlines() if "=" in line)
    from . import runtime, connection_background
    result = {"configured": bool(manifest), "unit": unit.name, "service_active": values.get("ActiveState") == "active", "service_state": values.get("ActiveState", "unavailable"), "enabled": values.get("UnitFileState") == "enabled", "linger": _linger(pwd.getpwuid(os.getuid()).pw_name), "encrypted_credential_present": False, "runtime": _safe_runtime_status(runtime, state), "connection": connection_background.status(state)}
    if manifest and not manifest.get("node"):
        try:
            cipher = _read(directory / "credential.cred")
            result["encrypted_credential_present"] = hashlib.sha256(cipher).hexdigest() == manifest.get("credential_sha256")
        except FileNotFoundError:
            pass
    try:
        result["worker"] = json.loads(_read(directory / "worker-status.json"))
    except FileNotFoundError:
        result["worker"] = {"state": "not_started"}
    result["boot_desired_enabled"] = boot_requested(state)
    result["desired_connection"] = "not_configured" if not manifest else ("not_applicable" if manifest.get("node") else ("connected" if connection_requested(state) else "disconnected"))
    return result


def _safe_runtime_status(runtime, state):
    try:
        return runtime.status(state)
    except Exception:
        return {"running": False, "ready": False, "status_unavailable": True}


def _configuration_action(state, action):
    _, directory, _ = _paths(state)
    fd = os.open(directory / "configuration.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError("Unsafe autostart configuration lock.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another autostart configuration is already in progress.") from None
        return action(state)
    finally:
        os.close(fd)


def enable(state):
    try:
        return _configuration_action(state, _enable)
    except (EOFError, KeyboardInterrupt):
        print(flush=True)
        return {"configured": False, "state": "cancelled"}


def disable(state):
    return _configuration_action(state, _disable)


def _committed_connection_status(state):
    try:
        return status(state)
    except (EOFError, KeyboardInterrupt):
        # Enabling the service already committed. Cancelling only this status
        # read must not suggest that the installed connection was cancelled.
        return {"configured": True, "enabled": True, "interrupted_after_commit": True}


def configure_connection(state, public_settings, runtime_key, *, output=None):
    """Install this connection once, using only the supplied hidden in-memory key."""
    if not isinstance(runtime_key, str) or not runtime_key or not runtime_key.isascii() or len(runtime_key) > 16384 or any(ord(ch) < 33 or ord(ch) > 126 for ch in runtime_key):
        raise ValueError("Enter one nonempty MCP server access key.")
    allowed = {"account_label", "tunnel_id", "control_plane_organization_id", "control_plane_url_path"}
    if not isinstance(public_settings, dict) or set(public_settings) - allowed:
        raise ValueError("Unsupported public connection settings.")
    from .tunnel import TunnelSpec
    settings = dict(public_settings)
    spec = TunnelSpec(settings.get("tunnel_id", ""), settings.get("control_plane_organization_id", ""), settings.get("control_plane_url_path", ""))
    label = settings.get("account_label", "")
    if not isinstance(label, str) or len(label) > 320 or any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
        raise ValueError("Enter a valid account label.")
    settings.update(spec.public_settings())
    settings["account_label"] = label
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        return _configuration_action(state, lambda selected: _configure_connection(selected, settings, runtime_key))
    finally:
        runtime_key = ""


def _connection_public_digest(config):
    fields = {name: config.get(name, "") for name in ("account_label", "tunnel_id", "control_plane_organization_id", "control_plane_url_path", "role", "node_id")}
    fields["mcp_port"] = config["ports"]["mcp"]
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resume_connection(state):
    """Resume the verified saved connection without asking for its key again."""
    return _configuration_action(state, _resume_connection)


def _resume_connection(state):
    if os.geteuid() == 0:
        raise RuntimeError("Run connect as the profile owner, not as root.")
    state, directory, unit = _paths(state)
    config = load_config(state)
    record = _manifest(directory)
    error = "The saved connection cannot be verified. Run fb connect chatgpt --replace."
    if not record or record.get("node") or config["role"] == "node" or record.get("unit") != unit.name:
        raise RuntimeError(error)
    try:
        valid_unit = hashlib.sha256(_read(unit)).hexdigest() == record.get("unit_sha256")
        valid_cipher = hashlib.sha256(_read(directory / "credential.cred")).hexdigest() == record.get("credential_sha256")
        bound = record.get("connection_settings_sha256")
        if bound:
            valid_settings = _connection_public_digest(config) == bound
        else:
            # Older releases only bound the complete profile when converting its desktop setting.
            old_bound = record.get("changed_config_sha256")
            valid_settings = bool(old_bound) and hashlib.sha256(_read(state / "config.json")).hexdigest() == old_bound
        if record.get("headless_changed"):
            valid_backup = hashlib.sha256(_read(directory / "config-before.json")).hexdigest() == record.get("config_before_sha256")
        else:
            valid_backup = True
    except (OSError, ValueError):
        raise RuntimeError(error) from None
    if not (valid_unit and valid_cipher and valid_settings and valid_backup):
        raise RuntimeError(error)
    from .tunnel import TunnelSpec
    TunnelSpec(config["tunnel_id"], config.get("control_plane_organization_id", ""), config.get("control_plane_url_path", ""))
    user = pwd.getpwuid(os.getuid()).pw_name
    if not _linger(user):
        _command(["sudo", "-v"], timeout=180)
        _command(["sudo", "-n", "--", "loginctl", "enable-linger", user])
    was_manually_stopped = manual_stop_requested(state)
    _connection_action(state, lambda _: _set_connection_requested(state, True))
    _clear_manual_stop(state)
    current = status(state)
    local = current.get("runtime", {})
    # A live managed worker owns local tools independently of its transport.
    # Change its requested connection state instead of restarting those tools.
    if current.get("service_active") and local.get("running") and local.get("ready") and current.get("worker", {}).get("state") not in {"failed", "stopped"} and not was_manually_stopped:
        if not current.get("enabled") and boot_requested(state) is not False:
            _systemctl("enable", unit.name)
        return _committed_connection_status(state)
    _systemctl("stop", unit.name)
    from . import connection_background
    stopped = connection_background.disconnect(state)
    if stopped.get("ok") is False or stopped.get("alive") or connection_background.status(state).get("alive"):
        raise RuntimeError("The previous connection is still stopping. Try connecting again shortly.")
    _systemctl("daemon-reload")
    if boot_requested(state) is False:
        _systemctl("start", unit.name)
    else:
        _systemctl("enable", "--now", unit.name)
    return _committed_connection_status(state)


def _optional_read(path):
    try:
        return _read(path)
    except FileNotFoundError:
        return None


def _configure_connection(state, settings, runtime_key):
    if os.geteuid() == 0:
        raise RuntimeError("Run connect as the profile owner, not as root.")
    state, directory, unit = _paths(state)
    config_path = state / "config.json"
    original_config = _optional_read(config_path)
    config = load_config(state) if original_config is not None else default_config()
    if config["role"] == "node":
        raise RuntimeError("Connect ChatGPT from the central profile.")
    config.update(settings)
    headless_before = config.get("headless", "auto")
    changed_headless = headless_before in ("no", False)
    if changed_headless:
        config["headless"] = "auto"
    validate(config)
    config_bytes = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode()
    record_path = directory / "installation.json"
    blob = directory / "credential.cred"
    backup = directory / "config-before.json"
    manual_stop = directory / "manual-stop.json"
    intent = directory / "connection-intent.json"
    targets = (config_path, unit, blob, record_path, backup, manual_stop, intent)
    originals = {path: _optional_read(path) for path in targets}
    previous = _manifest(directory)
    if previous:
        if previous.get("node") or previous.get("unit") != unit.name or originals[unit] is None or hashlib.sha256(originals[unit]).hexdigest() != previous.get("unit_sha256"):
            raise RuntimeError("The existing service changed outside FrameBridge; nothing was replaced.")
        if originals[blob] is None or hashlib.sha256(originals[blob]).hexdigest() != previous.get("credential_sha256"):
            raise RuntimeError("The encrypted connection changed outside FrameBridge; nothing was replaced.")
        if previous.get("headless_changed") and (originals[backup] is None or hashlib.sha256(originals[backup]).hexdigest() != previous.get("config_before_sha256")):
            raise RuntimeError("The profile backup changed outside FrameBridge; nothing was replaced.")
    elif any(originals[path] is not None for path in (unit, blob, backup)):
        raise RuntimeError("Existing unowned connection files; nothing was replaced.")
    _check_unit_directory(unit)
    version = _command(["systemd-creds", "--version"]).stdout.decode(errors="replace")
    match = re.match(r"systemd (\d+)", version)
    if not match or int(match.group(1)) < 256:
        raise RuntimeError("Protected connections require systemd 256 or later.")
    _command(["sudo", "-v"], timeout=180)
    user = pwd.getpwuid(os.getuid()).pw_name
    linger_before = _linger(user)
    units_before = _enabled_units()
    properties = _systemctl("show", unit.name, "--property=ActiveState,UnitFileState", check=False)
    values = dict(line.split("=", 1) for line in properties.stdout.decode(errors="replace").splitlines() if "=" in line)
    was_enabled = values.get("UnitFileState") == "enabled"
    was_active = values.get("ActiveState") in {"active", "activating", "reloading"}
    try:
        encrypted = _command(["sudo", "-n", "--", "systemd-creds", "encrypt", "--uid=" + str(os.getuid()), "--with-key=host", "--name=" + CREDENTIAL, "-", "-"], data=runtime_key.encode()).stdout
    finally:
        runtime_key = ""
    if len(encrypted) < 80 or len(encrypted) > 131072:
        raise RuntimeError("Connection protection did not complete.")
    # Recheck the exact originals after authentication/encryption and before any stop.
    if any(_optional_read(path) != data for path, data in originals.items()):
        raise RuntimeError("The profile changed during connection setup; nothing was replaced.")
    text = unit_text(state, blob).encode()
    record = dict(previous) if previous else {"schema": 1, "unit": unit.name, "node": False, "linger_before": linger_before, "enabled_units_before": units_before, "headless_before": headless_before, "headless_changed": changed_headless, "config_before_sha256": hashlib.sha256(original_config).hexdigest() if original_config is not None else ""}
    backup_bytes = originals[backup]
    if changed_headless and not record.get("headless_changed"):
        record.update(headless_before=headless_before, headless_changed=True, config_before_sha256=hashlib.sha256(original_config).hexdigest())
        backup_bytes = original_config
    elif not previous and changed_headless:
        backup_bytes = original_config
    if record.get("headless_changed"):
        # Disabling changes only the desktop preference, preserving the newly selected connection.
        restored_profile = dict(config)
        restored_profile["headless"] = record["headless_before"]
        backup_bytes = (json.dumps(restored_profile, indent=2, sort_keys=True) + "\n").encode()
        record["config_before_sha256"] = hashlib.sha256(backup_bytes).hexdigest()
        record["changed_config_sha256"] = hashlib.sha256(config_bytes).hexdigest()
    record.update(unit_sha256=hashlib.sha256(text).hexdigest(), credential_sha256=hashlib.sha256(encrypted).hexdigest(), connection_settings_sha256=_connection_public_digest(config))
    from . import connection_background
    background_before = connection_background.status(state)
    changed = {}
    service_touched = False
    manual_connection_stopped = False
    def write(path, data):
        changed[path] = data
        _write(path, data, replace=originals.get(path) is not None)
    try:
        if previous:
            service_touched = True
            _systemctl("stop", unit.name)
        disconnected = connection_background.disconnect(state)
        if disconnected.get("ok") is False or disconnected.get("alive"):
            raise RuntimeError("The previous connection could not be stopped safely; nothing was replaced.")
        manual_connection_stopped = not previous and background_before.get("alive") is True
        if connection_background.status(state).get("alive"):
            raise RuntimeError("The previous connection is still stopping; nothing was replaced.")
        write(blob, encrypted)
        write(unit, text)
        write(config_path, config_bytes)
        if backup_bytes is not None and backup_bytes != originals[backup]:
            write(backup, backup_bytes)
        write(record_path, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode())
        write(intent, (json.dumps({"schema": 1, "connect": True, "revision": connection_request_revision(state) + 1}, sort_keys=True) + "\n").encode())
        _command(["sudo", "-n", "--", "loginctl", "enable-linger", user])
        _systemctl("daemon-reload")
        service_touched = True
        if boot_requested(state) is False:
            _systemctl("start", unit.name)
        else:
            _systemctl("enable", "--now", unit.name)
    except BaseException as error:
        rollback_failed = False
        if service_touched or unit in changed:
            try:
                rollback_failed = bool(_systemctl("disable", "--now", unit.name, check=False).returncode)
            except Exception:
                rollback_failed = True
        for path, installed in reversed(list(changed.items())):
            try:
                current = _optional_read(path)
                if current == originals[path]:
                    continue
                if current != installed:
                    rollback_failed = True
                    continue
                if originals[path] is None:
                    path.unlink()
                else:
                    _write(path, originals[path], replace=True)
            except Exception:
                rollback_failed = True
        try:
            if originals[manual_stop] is not None and _optional_read(manual_stop) is None:
                _write(manual_stop, originals[manual_stop])
            _systemctl("daemon-reload")
            if previous:
                result = _systemctl("enable" if was_enabled else "disable", unit.name, check=False)
                rollback_failed = rollback_failed or bool(result.returncode)
                if was_active:
                    restarted = _systemctl("start", unit.name, check=False)
                    rollback_failed = rollback_failed or bool(restarted.returncode)
            if not linger_before and _enabled_units() == units_before:
                restored_linger = _command(["sudo", "-n", "--", "loginctl", "disable-linger", user], check=False)
                rollback_failed = rollback_failed or bool(restored_linger.returncode)
        except Exception:
            rollback_failed = True
        if rollback_failed:
            raise RuntimeError("Connection setup did not complete, and restoration could not be fully verified. Inspect fb autostart status before retrying.") from None
        if manual_connection_stopped:
            raise RuntimeError("Connection setup did not complete. The previous manually started connection was stopped; reconnect it using its original key.") from None
        raise
    return _committed_connection_status(state)


def _enable(state, *, start_now=True, confirm=True):
    if os.geteuid() == 0:
        raise RuntimeError("Run autostart enable as the profile owner, not as root.")
    state, directory, unit = _paths(state)
    config = load_config(state)
    if not confirm and config["role"] != "node":
        raise RuntimeError("Only node startup can be initialized without the connection wizard.")
    original_config = _read(state / "config.json")
    existing = _manifest(directory)
    if existing:
        if existing.get("unit") != unit.name or hashlib.sha256(_read(unit)).hexdigest() != existing.get("unit_sha256"):
            raise RuntimeError("The service changed outside FrameBridge; refusing to enable it.")
        if not existing.get("node") and hashlib.sha256(_read(directory / "credential.cred")).hexdigest() != existing.get("credential_sha256"):
            raise RuntimeError("The encrypted credential changed outside FrameBridge; refusing to enable it.")
        user = pwd.getpwuid(os.getuid()).pw_name
        if not _linger(user):
            _command(["sudo", "-v"], timeout=180)
            _command(["sudo", "-n", "--", "loginctl", "enable-linger", user])
        _set_boot_requested(state, True)
        _systemctl("daemon-reload")
        if start_now:
            _systemctl("enable", "--now", unit.name)
        else:
            _systemctl("enable", unit.name)
        return status(state)
    version = _command(["systemd-creds", "--version"]).stdout.decode(errors="replace")
    match = re.match(r"systemd (\d+)", version)
    if not match or int(match.group(1)) < 256:
        raise RuntimeError("Encrypted user credentials require systemd 256 or later.")
    node = config["role"] == "node"
    if not node and not config.get("tunnel_id"):
        raise RuntimeError("Save a tunnel with fb connect chatgpt before enabling automatic startup.")
    print("FrameBridge will start automatically when this computer starts.")
    print("Available tools are detected automatically.")
    if not node:
        print("Your connection key will be encrypted on this computer.")
    original_headless = config.get("headless", "auto")
    changed_headless = original_headless in ("no", False)
    if confirm and input("Enable automatic startup? [y/N]: ").strip().lower() not in {"y", "yes"}:
        return {"configured": False, "state": "cancelled"}
    print("[1/3] Preparing automatic startup...", flush=True)
    _check_unit_directory(unit)
    if unit.exists() or unit.is_symlink() or (directory / "credential.cred").exists() or (directory / "credential.cred").is_symlink():
        raise RuntimeError("Existing unowned service or encrypted credential; nothing was replaced.")
    _command(["sudo", "-v"], timeout=180)
    user = pwd.getpwuid(os.getuid()).pw_name
    linger_before = _linger(user)
    units_before = _enabled_units()
    encrypted = b""
    print("[2/3] Preparing this node..." if node else "[2/3] Protecting the connection key...", flush=True)
    if not node:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        runtime_key = getpass.getpass("Connection key (hidden): ")
        if not runtime_key or not runtime_key.isascii() or len(runtime_key) > 16384 or any(ord(ch) < 33 or ord(ch) > 126 for ch in runtime_key):
            raise ValueError("The runtime key must be nonempty and contain no whitespace or control characters.")
        try:
            encrypted = _command(["sudo", "-n", "--", "systemd-creds", "encrypt", "--uid=" + str(os.getuid()), "--with-key=host", "--name=" + CREDENTIAL, "-", "-"], data=runtime_key.encode()).stdout
        finally:
            runtime_key = ""
        if len(encrypted) < 80 or len(encrypted) > 131072:
            raise RuntimeError("Credential encryption returned an invalid result.")
    blob = directory / "credential.cred"
    text = unit_text(state, blob, node=node).encode()
    record = {"schema": 1, "unit": unit.name, "unit_sha256": hashlib.sha256(text).hexdigest(), "credential_sha256": hashlib.sha256(encrypted).hexdigest() if encrypted else "", "node": node, "linger_before": linger_before, "enabled_units_before": units_before, "headless_before": original_headless, "headless_changed": changed_headless, "config_before_sha256": hashlib.sha256(original_config).hexdigest()}
    created = []
    try:
        if encrypted:
            _write(blob, encrypted); created.append(blob)
        _write(unit, text); created.append(unit)
        if changed_headless:
            if _read(state / "config.json") != original_config:
                raise RuntimeError("The profile changed during configuration; no profile settings were overwritten.")
            _write(directory / "config-before.json", original_config)
            created.append(directory / "config-before.json")
            config["headless"] = "auto"
            save_config(state, config)
            record["changed_config_sha256"] = hashlib.sha256(_read(state / "config.json")).hexdigest()
        _json(directory / "installation.json", record); created.append(directory / "installation.json")
        print("[3/3] Enabling automatic startup...", flush=True)
        _command(["sudo", "-n", "--", "loginctl", "enable-linger", user])
        _systemctl("daemon-reload")
        if start_now:
            _systemctl("enable", "--now", unit.name)
        else:
            _systemctl("enable", unit.name)
    except BaseException:
        if unit in created:
            _systemctl("disable", "--now", unit.name, check=False)
        for path in reversed(created):
            try: path.unlink()
            except FileNotFoundError: pass
        if changed_headless and record.get("changed_config_sha256") == hashlib.sha256(_read(state / "config.json")).hexdigest():
            _write(state / "config.json", original_config, replace=True)
        _systemctl("daemon-reload", check=False)
        if not linger_before:
            try:
                if _enabled_units() == units_before:
                    _command(["sudo", "-n", "--", "loginctl", "disable-linger", user], check=False)
            except Exception:
                pass
        raise
    return status(state)


def _disable(state):
    state, directory, unit = _paths(state)
    record = _manifest(directory)
    if not record:
        return {"configured": False, "state": "already_disabled"}
    if record.get("unit") != unit.name or hashlib.sha256(_read(unit)).hexdigest() != record.get("unit_sha256"):
        raise RuntimeError("The service changed outside FrameBridge; refusing to remove it.")
    blob = directory / "credential.cred"
    if not record.get("node") and hashlib.sha256(_read(blob)).hexdigest() != record.get("credential_sha256"):
        raise RuntimeError("The encrypted credential changed outside FrameBridge; refusing to remove it.")
    backup = None
    if record.get("headless_changed"):
        backup = _read(directory / "config-before.json")
        if hashlib.sha256(backup).hexdigest() != record.get("config_before_sha256"):
            raise RuntimeError("The profile backup changed outside FrameBridge; refusing to remove it.")
    owned_policy = [directory / "boot-policy.json", directory / "connection-intent.json"]
    for policy in owned_policy:
        if policy.exists() or policy.is_symlink():
            _read(policy)
    restore_linger = not record.get("linger_before") and [name for name in _enabled_units() if name != unit.name] == record.get("enabled_units_before")
    if restore_linger:
        _command(["sudo", "-v"], timeout=180)
    _systemctl("disable", "--now", unit.name)
    unit.unlink()
    if not record.get("node"):
        blob.unlink()
    config_restored = False
    if record.get("headless_changed") and hashlib.sha256(_read(state / "config.json")).hexdigest() == record.get("changed_config_sha256"):
        _write(state / "config.json", backup, replace=True)
        config_restored = True
    _systemctl("daemon-reload")
    linger_restored = False
    if restore_linger and _enabled_units() == record.get("enabled_units_before"):
        user = pwd.getpwuid(os.getuid()).pw_name
        _command(["sudo", "-n", "--", "loginctl", "disable-linger", user])
        linger_restored = True
    if backup is not None:
        (directory / "config-before.json").unlink()
    for policy in owned_policy:
        policy.unlink(missing_ok=True)
    (directory / "installation.json").unlink()
    return {"configured": False, "state": "disabled", "encrypted_credential_removed": True, "desktop_setting_restored": config_restored, "linger_restored": linger_restored}


def _credential(state):
    # Debian 13 systemd 257 cannot load user encrypted credentials directly.
    # Its systemd-creds CLI delegates owner-scoped decryption to the OS broker.
    _, directory, _ = _paths(state)
    record = _manifest(directory)
    if not record or record.get("node"):
        raise RuntimeError("The encrypted runtime credential is not configured.")
    cipher = _read(directory / "credential.cred")
    if hashlib.sha256(cipher).hexdigest() != record.get("credential_sha256"):
        raise RuntimeError("The encrypted runtime credential changed outside FrameBridge.")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    result = _command(["systemd-creds", "decrypt", "--user", "--name=" + CREDENTIAL, "-", "-"], data=cipher, timeout=15)
    try:
        key = result.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise RuntimeError("The decrypted runtime credential is invalid.") from None
    if not key or len(key) > 16384 or any(ord(ch) < 33 or ord(ch) == 127 for ch in key):
        raise RuntimeError("The decrypted runtime credential is invalid.")
    return key


def stop_for_current_boot(state):
    # systemctl stop also cancels an already queued Restart=on-failure job.
    # Keep the unit enabled and the encrypted credential for the next boot.
    directory = _intent_directory(state)
    if directory is None:
        return {"configured": False, "state": "not_configured"}
    _, _, unit = _paths(state)
    record = _manifest(directory)
    if record.get("unit") != unit.name or hashlib.sha256(_read(unit)).hexdigest() != record.get("unit_sha256"):
        raise RuntimeError("The service changed outside FrameBridge; refusing to stop it.")
    _systemctl("stop", unit.name)
    return {"configured": True, "state": "stopped_for_current_boot", "autostart_configuration_preserved": True}


def _intent_directory(state):
    directory = state_path(state) / "autostart"
    if not directory.exists() and not directory.is_symlink():
        return None
    private_dir(directory)
    return directory if _manifest(directory) else None


def mark_manual_stop(state):
    directory = _intent_directory(state)
    if directory is None:
        return False
    _json(directory / "manual-stop.json", {"requested": True, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}, replace=True)
    return True


def manual_stop_requested(state):
    directory = _intent_directory(state)
    if directory is None:
        return False
    try:
        record = json.loads(_read(directory / "manual-stop.json"))
    except FileNotFoundError:
        return False
    return isinstance(record, dict) and record.get("requested") is True and record.get("boot_id") == Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _clear_manual_stop(state):
    directory = _intent_directory(state)
    if directory is None:
        return
    path = directory / "manual-stop.json"
    try:
        _read(path)
    except FileNotFoundError:
        return
    path.unlink()


def boot_requested(state):
    """Explicit boot policy; None retains the normal first-connect default."""
    directory = _intent_directory(state)
    if directory is None:
        return None
    try:
        value = json.loads(_read(directory / "boot-policy.json"))
    except FileNotFoundError:
        return None
    if type(value) is not dict or value.get("schema") != 1 or type(value.get("enabled")) is not bool:
        raise RuntimeError("The saved boot policy cannot be verified.")
    return value["enabled"]


def _set_boot_requested(state, enabled):
    if type(enabled) is not bool:
        raise ValueError("Boot policy must be true or false.")
    directory = _intent_directory(state)
    if directory is None:
        return False
    _json(directory / "boot-policy.json", {"schema": 1, "enabled": enabled}, replace=True)
    return True


def connection_requested(state):
    """Desired transport state survives worker recovery and reboot until connect."""
    directory = _intent_directory(state)
    if directory is None:
        return True
    try:
        value = json.loads(_read(directory / "connection-intent.json"))
    except FileNotFoundError:
        return True
    if type(value) is not dict or value.get("schema") != 1 or type(value.get("connect")) is not bool:
        raise RuntimeError("The saved connection intent cannot be verified.")
    return value["connect"]


def connection_request_revision(state):
    directory = _intent_directory(state)
    if directory is None:
        return 0
    try:
        value = json.loads(_read(directory / "connection-intent.json"))
    except FileNotFoundError:
        return 0
    revision = value.get("revision", 0) if type(value) is dict else None
    if type(revision) is not int or revision < 0:
        raise RuntimeError("The saved connection intent cannot be verified.")
    return revision


def _set_connection_requested(state, requested):
    if type(requested) is not bool:
        raise ValueError("Connection intent must be true or false.")
    directory = _intent_directory(state)
    if directory is None:
        return False
    _json(directory / "connection-intent.json", {"schema": 1, "connect": requested, "revision": connection_request_revision(state) + 1}, replace=True)
    return True


def _connection_action(state, action):
    """Serialize the worker's transport startup with an explicit pause request."""
    directory = _intent_directory(state)
    if directory is None:
        return action(state)
    fd = os.open(directory / "connection-action.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError("Unsafe connection action lock.")
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another connection operation is still finishing. Try again shortly.")
                time.sleep(0.05)
        return action(state)
    finally:
        os.close(fd)


def _worker_status(directory, name):
    _json(directory / "worker-status.json", {"state": name, "updated_at": time.time()}, replace=True)


def worker(state):
    global _STOP
    _STOP = False
    state, directory, unit = _paths(state)
    config = load_config(state)
    from . import runtime, connection_background, tunnel
    from .display_context import DisplayContextError
    def stop(signum, frame):
        global _STOP
        _STOP = True
    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    lockfd = os.open(directory / "worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(lockfd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(lockfd)
        raise RuntimeError("Unsafe autostart worker lock.")
    fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    connected = False
    attempted_connection = False
    last_revision = None
    next_connection_attempt = 0.0
    try:
        _clear_manual_stop(state)
        while not _STOP:
            if manual_stop_requested(state):
                return 0
            try:
                current = runtime.start(state)
                if manual_stop_requested(state):
                    return 0
            except DisplayContextError:
                if config.get("headless") in ("no", False):
                    _worker_status(directory, "waiting_for_graphical_login")
                    time.sleep(3)
                    continue
                raise
            if current.get("running") and current.get("ready"):
                break
            _worker_status(directory, "waiting_for_graphical_login" if config.get("headless") in ("no", False) else "waiting_for_local_services")
            time.sleep(3)
        if _STOP:
            return 0
        _worker_status(directory, "local_services_ready")
        if config["role"] == "node":
            while not _STOP:
                if manual_stop_requested(state):
                    return 0
                if not runtime.status(state).get("running"):
                    return 0 if manual_stop_requested(state) else 1
                time.sleep(2)
            return 0
        while not _STOP:
            if manual_stop_requested(state):
                return 0
            def synchronize_connection(_state):
                nonlocal connected, attempted_connection, last_revision, next_connection_attempt
                revision = connection_request_revision(state)
                if revision != last_revision:
                    attempted_connection = False
                    next_connection_attempt = 0.0
                    last_revision = revision
                current = connection_background.status(state)
                connected = connected or current.get("alive") is True
                if not connection_requested(state):
                    if current.get("alive"):
                        stopped = connection_background.disconnect(state)
                        if stopped.get("ok") is False or stopped.get("alive"):
                            raise RuntimeError("The paused connection is still stopping.")
                    _worker_status(directory, "disconnected")
                    return
                if not current.get("alive") and (not attempted_connection or current.get("state") not in ("stopped", "closed")):
                    if time.monotonic() < next_connection_attempt:
                        _worker_status(directory, "reconnecting")
                        return
                    attempted_connection = True
                    next_connection_attempt = time.monotonic() + 15
                    key = _credential(state)
                    try:
                        spec = tunnel.TunnelSpec(config["tunnel_id"], config.get("control_plane_organization_id", ""), config.get("control_plane_url_path", ""))
                        binary = runtime.PREFIX / "original/geranas/FrameBridge-MCP-Private/bin/tunnel-client"
                        mcp = "http://127.0.0.1:" + str(config["ports"]["mcp"]) + "/mcp"
                        command = tunnel.build_command(binary, state, mcp, spec)
                        _worker_status(directory, "connecting")
                        connected = True
                        connection_background.start(command, key, state, spec.tunnel_id, mcp, output=io.StringIO(), wait_timeout=1)
                    finally:
                        key = ""
                    current = connection_background.status(state)
                if not current.get("alive"):
                    _worker_status(directory, "disconnected" if current.get("state") in ("stopped", "closed") else "reconnecting")
                else:
                    _worker_status(directory, "connected" if current.get("verified") else "reconnecting")
            _connection_action(state, synchronize_connection)
            if not runtime.status(state).get("running"):
                return 0 if manual_stop_requested(state) else 1
            time.sleep(2)
        return 0
    except Exception:
        _worker_status(directory, "failed")
        return 1
    finally:
        if connected:
            connection_background.disconnect(state)
        runtime.stop(state)
        _worker_status(directory, "stopped")
        os.close(lockfd)
        for sig, handler in old.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["_run"])
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    return worker(args.state)


if __name__ == "__main__":
    raise SystemExit(main())
