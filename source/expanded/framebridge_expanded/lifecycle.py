"""Explicit lifecycle scopes shared by human and agent command interfaces."""
from __future__ import annotations

import hashlib
import os
import pwd
import time

from . import autostart, connection_background, runtime
from .config import load_config, state_path


def _owned_setup(state):
    state, directory, unit = autostart._paths(state)
    record = autostart._manifest(directory)
    if record and (record.get("unit") != unit.name or hashlib.sha256(autostart._read(unit)).hexdigest() != record.get("unit_sha256")):
        raise RuntimeError("The managed service changed outside FrameBridge; no lifecycle change was made.")
    return state, directory, unit, record


def _runtime_stopped(result):
    if type(result) is not dict or result.get("ok") is False or result.get("status_unavailable") or result.get("running") is not False:
        return False
    processes = result.get("processes", [])
    return type(processes) is list and all(type(item) is dict and item.get("alive") is False for item in processes)


def _settled_runtime_stop(state, result):
    """Observe bounded asynchronous cleanup without treating query-ok as stopped."""
    if _runtime_stopped(result):
        return result
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        time.sleep(0.05)
        result = runtime.status(state)
        if _runtime_stopped(result):
            break
    return result


def status(state):
    """One observation model: actual tools/transport, intended transport and boot."""
    state = state_path(state)
    configured = (state / "config.json").is_file()
    config = load_config(state) if configured else {}
    local = autostart._safe_runtime_status(runtime, state) if configured else {"running": False, "ready": False, "configured": False}
    connection = connection_background.status(state)
    directory = state / "autostart"
    if directory.exists() or directory.is_symlink():
        startup = autostart.status(state)
    else:
        startup = {"configured": False, "enabled": False, "service_active": False, "service_state": "not_configured", "linger": None, "encrypted_credential_present": False}
    boot = {key: startup.get(key) for key in ("configured", "enabled", "service_active", "service_state", "linger", "encrypted_credential_present", "unit")}
    boot["desired_enabled"] = startup.get("boot_desired_enabled")
    if config.get("role") == "node":
        desired = "not_applicable"
    elif not boot["configured"]:
        desired = "not_configured"
    else:
        desired = "connected" if autostart.connection_requested(state) else "disconnected"
    return {"ok": True, "configured": configured, "role": config.get("role"), "name": config.get("name"),
            "runtime": local, "connection": connection, "boot": boot,
            "desired_connection": desired,
            "disconnect_scope": "until_explicit_connect",
            "next_boot_connection": "paused" if desired == "disconnected" else ("requested" if desired == "connected" else "not_applicable"),
            "worker": startup.get("worker", {"state": "not_configured"})}


def service_start(state):
    """Start the configured stack now without changing its boot enablement."""
    def action(value):
        state, directory, unit, record = _owned_setup(value)
        if not record and load_config(state)["role"] == "node":
            autostart._enable(state, start_now=True, confirm=False)
            return {"ok": True, "state": "start_requested", "scope": "managed_stack",
                    "initial_configuration": True, "boot_policy_changed": True,
                    "boot_default_enabled": True, "status": status(state)}
        if not record:
            result = runtime.start(state)
            return {"ok": bool(result.get("running") and result.get("ready")), "state": "started" if result.get("running") and result.get("ready") else "not_ready",
                    "scope": "local_tools", "boot_policy_changed": False, "runtime": result}
        autostart._clear_manual_stop(state)
        autostart._systemctl("start", unit.name)
        return {"ok": True, "state": "start_requested", "scope": "managed_stack", "boot_policy_changed": False, "status": status(state)}
    return autostart._configuration_action(state, action)


def service_stop(state):
    """Stop current tools and transport; retain desired transport and boot policy."""
    def action(value):
        state, directory, unit, record = _owned_setup(value)
        if record:
            autostart.mark_manual_stop(state)
            autostart._systemctl("stop", unit.name)
        connection = connection_background.disconnect(state)
        local = _settled_runtime_stop(state, runtime.stop(state))
        success = connection.get("ok") is not False and not connection.get("alive") and _runtime_stopped(local)
        return {"ok": success, "state": "stopped" if success else "stop_incomplete",
                "scope": "managed_stack" if record else "local_tools", "boot_policy_changed": False, "saved_material_preserved": True,
                "connection_intent_preserved": True, "connection": connection, "runtime": local}
    return autostart._configuration_action(state, action)


def _boot(state, enabled):
    def action(value):
        state, directory, unit, record = _owned_setup(value)
        initialized = False
        if not record:
            if enabled and load_config(state)["role"] == "node":
                autostart._enable(state, start_now=False, confirm=False)
                state, directory, unit, record = _owned_setup(state)
                initialized = True
            else:
                raise RuntimeError("Managed startup is not configured. Use fb connect, or fb service start for a node profile.")
        if enabled and not autostart._linger(pwd.getpwuid(os.getuid()).pw_name):
            raise RuntimeError("OWNER_AUTHENTICATION_REQUIRED: run fb connect in a normal terminal, or fb autostart enable for a node, to enable startup after logout.")
        policy = directory / "boot-policy.json"
        before = autostart._optional_read(policy)
        autostart._set_boot_requested(state, enabled)
        installed = autostart._read(policy)
        try:
            autostart._systemctl("enable" if enabled else "disable", unit.name)
        except BaseException:
            if autostart._optional_read(policy) == installed:
                if before is None:
                    policy.unlink()
                else:
                    autostart._write(policy, before, replace=True)
            raise
        return {"ok": True, "state": "boot_enabled" if enabled else "boot_disabled",
                "boot_policy_changed": True, "current_execution_unchanged": True, "initial_configuration": initialized,
                "saved_material_preserved": True, "status": status(state)}
    return autostart._configuration_action(state, action)


def boot_enable(state):
    return _boot(state, True)


def boot_disable(state):
    return _boot(state, False)


def disconnect(state):
    """Persist a pause until explicit connect, including worker recovery/reboot."""
    def action(value):
        def pause(inner):
            autostart._set_connection_requested(inner, False)
            result = connection_background.disconnect(inner)
            return {"ok": result.get("ok") is not False and not result.get("alive"),
                    "state": "disconnected" if result.get("ok") is not False and not result.get("alive") else "disconnect_incomplete",
                    "desired_connection": "disconnected",
                    "scope": "until_explicit_connect", "local_tools_preserved": True,
                    "saved_material_preserved": True, "boot_policy_changed": False,
                    "connection": result}
        return autostart._connection_action(value, pause)
    return autostart._configuration_action(state, action)


def forget_credentials(state):
    """Remove the managed connection setup/material; retain profile and linger.

    The command front end requires explicit confirmation before invoking this
    destructive operation. Legacy autostart disable retains its old contract.
    """
    def action(value):
        state, directory, unit, record = _owned_setup(value)
        if not record:
            return {"ok": True, "state": "already_forgotten", "profile_preserved": True}
        if record.get("node"):
            raise RuntimeError("This node profile has no ChatGPT credential to forget.")
        blob = directory / "credential.cred"
        if hashlib.sha256(autostart._read(blob)).hexdigest() != record.get("credential_sha256"):
            raise RuntimeError("The encrypted credential changed outside FrameBridge; nothing was forgotten.")
        backup = directory / "config-before.json"
        if record.get("headless_changed"):
            if hashlib.sha256(autostart._read(backup)).hexdigest() != record.get("config_before_sha256"):
                raise RuntimeError("The profile backup changed outside FrameBridge; nothing was forgotten.")
        owned_metadata = [directory / "connection-intent.json", directory / "manual-stop.json", directory / "boot-policy.json"]
        for path in owned_metadata:
            if path.exists() or path.is_symlink():
                autostart._read(path)
        autostart.mark_manual_stop(state)
        autostart._systemctl("disable", "--now", unit.name)
        connection = connection_background.disconnect(state)
        if connection.get("ok") is False or connection.get("alive"):
            raise RuntimeError("The connection is still stopping; its saved material was retained.")
        local = _settled_runtime_stop(state, runtime.stop(state))
        if not _runtime_stopped(local):
            raise RuntimeError("Local tools are still stopping; the saved connection material was retained.")
        blob.unlink()
        unit.unlink()
        if record.get("headless_changed"):
            backup.unlink()
        for path in owned_metadata:
            path.unlink(missing_ok=True)
        (directory / "installation.json").unlink()
        autostart._systemctl("daemon-reload")
        return {"ok": True, "state": "forgotten", "encrypted_credential_removed": True,
                "managed_configuration_removed": True, "profile_preserved": True,
                "linger_preserved": True, "boot_enabled": False}
    return autostart._configuration_action(state, action)
