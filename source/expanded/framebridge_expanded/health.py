"""Read-only, allowlisted health observations for one local FrameBridge profile.

This combines existing status and prerequisite checks. It does not execute a
terminal/CUA probe, contact another node, repair a profile or change boot intent.
Snapshot success, observed readiness and compatibility are separate facts.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import socket
import uuid

from . import __version__, lifecycle, runtime
from .config import state_path


_KINDS = frozenset({"physical-x11", "virtual-x11", "text", "not_started", "unavailable"})
_PHASES = frozenset({"starting", "checking", "access", "unverified", "ready",
                     "retrying", "error", "rejected", "stopped", "closed"})
_SERVICES = frozenset({"active", "activating", "deactivating", "inactive", "failed",
                       "reloading", "maintenance", "refreshing", "not_configured"})
_CAPABILITIES = ("fbexec", "fbterm", "cua_tty_module", "graphical_cua")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _boolean(value):
    return value if type(value) is bool else None


def _name(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", value) else None


def _node_id(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        return None


def _count(value):
    return len(value) if isinstance(value, list) else 0


def _choice(value, options, fallback="unavailable"):
    return value if isinstance(value, str) and value in options else fallback


def _runtime_state(value):
    if value.get("ok") is False or value.get("status_unavailable") or type(value.get("running")) is not bool:
        return "unavailable"
    if value.get("ready") is True and value.get("running") is True:
        return "ready"
    return "running" if value["running"] else "stopped"


def _boot_state(value):
    if value.get("configured") is False:
        return "not_configured"
    if value.get("configured") is not True or type(value.get("enabled")) is not bool:
        return "unavailable"
    return "enabled" if value["enabled"] else "disabled"


def build_report(status_snapshot=None, doctor_snapshot=None, *, observed_at=None,
                 host=None, status_available=True, doctor_available=True):
    """Project snapshots onto a fixed public schema; never copy raw dictionaries.

    Errors deliberately omit exception text and diagnostic free text. Existing
    local ``fb doctor`` remains the detailed troubleshooting interface; this
    summary can be shown to an assistant without connection metadata or paths.
    """
    status = _mapping(status_snapshot)
    diagnosis = _mapping(doctor_snapshot)
    local = _mapping(status.get("runtime"))
    connection = _mapping(status.get("connection"))
    boot = _mapping(status.get("boot"))
    platform = _mapping(diagnosis.get("platform_compatibility"))
    capabilities = _mapping(local.get("capabilities"))
    prerequisite_capabilities = _mapping(diagnosis.get("capabilities"))
    context = _mapping(local.get("display_context"))
    status_available = bool(status_available and isinstance(status_snapshot, dict) and status.get("ok") is not False)
    configured = _boolean(status.get("configured")) if status_available else None
    role = status.get("role") or local.get("role")
    role = _choice(role, {"standalone", "central", "node"}, None)
    local_state = _runtime_state(local) if configured is not False else "not_configured"
    phase = _choice(connection.get("state"), _PHASES)
    intent = _choice(status.get("desired_connection"), {"connected", "disconnected", "not_configured", "not_applicable"})
    kind = _choice(context.get("kind"), _KINDS)
    doctor_available = bool(doctor_available and isinstance(doctor_snapshot, dict) and type(diagnosis.get("ok")) is bool) if configured else False
    diagnostics_ok = _boolean(diagnosis.get("ok")) if doctor_available else None
    connection_available = role == "node" or (
        bool(connection) and connection.get("ok") is not False
        and phase != "unavailable" and type(connection.get("verified")) is bool)
    boot_available = _boot_state(boot) != "unavailable"
    issues = []
    if not status_available:
        issues.append("STATUS_UNAVAILABLE")
    if configured and not doctor_available:
        issues.append("DIAGNOSTICS_UNAVAILABLE")
    if configured and local_state == "unavailable":
        issues.append("RUNTIME_STATUS_UNAVAILABLE")
    if configured and not connection_available:
        issues.append("CONNECTION_STATUS_UNAVAILABLE")
    if configured and not boot_available:
        issues.append("BOOT_STATUS_UNAVAILABLE")
    if diagnostics_ok is False:
        issues.append("PREREQUISITES_NEED_ATTENTION")
    if configured is False:
        health_state, healthy = "unconfigured", False
    elif (not status_available or configured is None or role is None or not doctor_available
          or local_state == "unavailable" or not connection_available or not boot_available):
        health_state, healthy = "unavailable", None
    elif diagnostics_ok is not True or local_state != "ready":
        health_state, healthy = "degraded", False
    elif role == "node":
        health_state, healthy = "healthy", True
    elif intent == "disconnected":
        if connection.get("verified") is False and connection.get("alive") is not True and phase in {"stopped", "closed"}:
            health_state, healthy = "paused", True
        else:
            health_state, healthy = "degraded", False
            issues.append("CONNECTION_INTENT_MISMATCH")
    elif connection.get("verified") is True:
        health_state, healthy = "healthy", True
    else:
        health_state, healthy = "degraded", False
        issues.append("CONNECTION_NOT_VERIFIED")
    if configured and local_state in {"running", "stopped"}:
        issues.append("LOCAL_TOOLS_NOT_READY")
    observation_ok = (status_available and configured is not None and local_state != "unavailable"
                      and (configured is False or (doctor_available and connection_available
                                                   and boot_available and role is not None)))
    build_profile = platform.get("artifact_profile_id") or platform.get("profile_id")
    missing = diagnosis.get("missing_commands")
    missing = sorted({v for v in missing if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", v)}) if isinstance(missing, list) else []
    notes = ["This is a local profile observation, not a fleet scan.",
             "Status and prerequisites are sampled sequentially; this is not an atomic snapshot.",
             "No FBEXEC/FBTERM command, screen capture or input probe was performed."]
    if role == "node":
        notes.append("The central computer provides the ChatGPT connection; this report does not verify that central connection.")
    if configured is False:
        notes.append("This profile is not configured. No profile or connection was created.")
    if kind == "text":
        notes.append("Text mode does not require a graphical session.")
    return {
        "schema_version": 1, "ok": observation_ok, "read_only": True,
        "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        "effective_target": {"selector": "LOCAL", "host": _name(host),
                             "profile_name": _name(status.get("name") or local.get("name")),
                             "role": role, "node_id": _node_id(local.get("node_id"))},
        "software": {"cli_version": __version__, "runtime_version": None,
                     "runtime_version_status": "not_reported"},
        "health": {"state": health_state, "healthy": healthy, "issues": issues},
        "runtime": {"state": local_state, "running": _boolean(local.get("running")) if configured else None,
                    "ready": _boolean(local.get("ready")) if configured else None,
                    "availability": {k: _boolean(capabilities.get(k)) for k in _CAPABILITIES},
                    "operational_probe_performed": False},
        "connection": {"state": "managed_by_central" if role == "node" else phase,
                       "verified": None if role == "node" else _boolean(connection.get("verified")),
                       "desired": intent},
        "boot": {"policy": _boot_state(boot),
                 "service_state": _choice(boot.get("service_state"), _SERVICES)},
        "prerequisites": {"state": "not_run" if configured is False else ("passed" if diagnostics_ok is True else "needs_attention" if diagnostics_ok is False else "unavailable"),
                          "compatible": _boolean(platform.get("ok")) if doctor_available else None,
                          "profile_id": _name(build_profile),
                          "validation_status": _choice(platform.get("validation_status"), {"candidate", "release_build", "validated", "source-unvalidated"}, "not_reported"),
                          "missing_commands": missing,
                          "issue_count": _count(diagnosis.get("issues")),
                          "compatibility_issue_count": _count(platform.get("issues")),
                          "availability": {k: _boolean(prerequisite_capabilities.get(k)) for k in _CAPABILITIES}},
        "desktop": {"kind": kind, "graphical_available": _boolean(capabilities.get("graphical_cua")),
                    "capture_verified_by_this_report": False, "input_verified_by_this_report": False},
        "notes": notes,
    }


def collect(state):
    """Observe only the selected local profile without creating or repairing it."""
    state = state_path(state)
    status_snapshot, diagnosis = None, None
    status_available = doctor_available = True
    try:
        status_snapshot = lifecycle.status(state)
    except Exception:
        status_available = False
    if _mapping(status_snapshot).get("configured") is True:
        try:
            diagnosis = runtime.doctor(state)
        except Exception:
            doctor_available = False
    else:
        doctor_available = False
    try:
        host = socket.gethostname()
    except OSError:
        host = None
    return build_report(status_snapshot, diagnosis, host=host,
                        status_available=status_available, doctor_available=doctor_available)


def render(report):
    """Render the same allowlisted report for a human terminal."""
    target = report["effective_target"]
    software = report["software"]
    state = report["health"]["state"]
    lines = ["FrameBridge " + software["cli_version"] + " — health: " + state,
             "Target: LOCAL = this computer (" + (target["host"] or "hostname unavailable") + ")",
             "Profile: " + (target["profile_name"] or "not available") + " | role: " + (target["role"] or "not available")]
    if target["node_id"]:
        lines.append("Node ID: " + target["node_id"])
    lines.extend(["Observed: " + report["observed_at"],
                  "Local tools: " + report["runtime"]["state"],
                  "Connection: " + ("connected (verified)" if report["connection"]["verified"] is True else report["connection"]["state"]),
                  "Connection intent: " + report["connection"]["desired"],
                  "Automatic startup: " + report["boot"]["policy"],
                  "Current service: " + report["boot"]["service_state"],
                  "Prerequisites: " + report["prerequisites"]["state"]])
    if report["prerequisites"]["profile_id"]:
        lines.append("Build profile: " + report["prerequisites"]["profile_id"])
    lines.append("Desktop: " + report["desktop"]["kind"] + " (capture/input not tested)")
    for issue in report["health"]["issues"]:
        lines.append("Finding: " + issue)
    if report["prerequisites"]["missing_commands"]:
        lines.append("Missing commands: " + ", ".join(report["prerequisites"]["missing_commands"]))
    if state in {"degraded", "unavailable"}:
        lines.append("Inspect details locally: fb status; fb doctor")
    lines.append("Runtime version: not reported; the CLI version is not proof of the running version.")
    lines.extend(report["notes"])
    return "\n".join(lines)
