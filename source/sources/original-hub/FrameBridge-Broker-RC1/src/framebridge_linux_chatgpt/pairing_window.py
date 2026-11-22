from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from .network_security import PAIRING_WINDOW_TTL_SECONDS, pairing_policy
from .product_core import ProductError

SUDO = "/usr/bin/sudo"
PRIV_HELPER = "/usr/local/libexec/framebridge-seed-priv"
MIN_TTL = 30
MAX_TTL = PAIRING_WINDOW_TTL_SECONDS


def _decode_helper(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value=json.loads(proc.stdout or "{}")
    except Exception as exc:
        raise ProductError("PAIRING_WINDOW_HELPER_INVALID_JSON") from exc
    if not isinstance(value,dict):
        raise ProductError("PAIRING_WINDOW_HELPER_INVALID_RESPONSE")
    if proc.returncode!=0 or value.get("ok") is not True:
        raise ProductError(str(value.get("error") or "PAIRING_WINDOW_HELPER_FAILED"))
    return value


def _run_helper(args: list[str], *, interactive: bool=True) -> dict[str, Any]:
    argv=[SUDO]
    if not interactive:
        argv.append("-n")
    argv.extend([PRIV_HELPER,*args])
    proc=subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=None,
        check=False,
        shell=False,
    )
    return _decode_helper(proc)


def open_pairing_window(port: int=18765, ttl_seconds: int=PAIRING_WINDOW_TTL_SECONDS) -> dict[str, Any]:
    if not sys.stdin.isatty():
        raise ProductError("PAIRING_WINDOW_LOCAL_TTY_REQUIRED")
    if isinstance(ttl_seconds,bool) or not isinstance(ttl_seconds,int) or not (MIN_TTL <= ttl_seconds <= MAX_TTL):
        raise ProductError("PAIRING_WINDOW_TTL_INVALID")
    policy=pairing_policy(port)
    if policy.get("ok") is not True:
        raise ProductError(str(policy.get("reason") or "PAIRING_WINDOW_POLICY_BLOCKED"))
    candidate=policy.get("candidate") or {}
    required=("interface","subnet","address","network_fingerprint")
    if not all(candidate.get(k) for k in required):
        raise ProductError("PAIRING_WINDOW_CANDIDATE_INCOMPLETE")
    result=_run_helper([
        "pairing-window-open",
        "--interface",str(candidate["interface"]),
        "--source-subnet",str(candidate["subnet"]),
        "--destination-address",str(candidate["address"]),
        "--port",str(int(port)),
        "--network-fingerprint",str(candidate["network_fingerprint"]),
        "--ttl",str(int(ttl_seconds)),
    ])
    result["policy_state"]=policy.get("state")
    result["transport"]="LAN_HTTP"
    result["listen_url_base"]=f"http://{candidate['address']}:{int(port)}"
    return result


def pairing_window_status(*, non_interactive: bool=False) -> dict[str, Any]:
    return _run_helper(["pairing-window-status"],interactive=not non_interactive)


def close_pairing_window(reason: str="user", *, non_interactive: bool=False) -> dict[str, Any]:
    return _run_helper(["pairing-window-close","--reason",str(reason)],interactive=not non_interactive)
