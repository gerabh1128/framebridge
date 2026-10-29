#!/usr/bin/python3
"""GERAMEG CUA-TTY with the explicitly selected local-user input policy.

Reuse the installed LocalServer and Manager without changing their source,
socket protocol, existing demos and PTY handling, or input/session limits for demos.

Remote callers must still enter through authenticated FrameBridge. This
process authenticates LOCAL peers by the original LocalServer's SO_PEERCRED
check. It cannot distinguish an authenticated FBEXEC process from another
process running as gerabh (UID 1000). Both have the same local authority.

The legacy owner_assertion RPC field remains mandatory for wire compatibility
but must be an empty object. It is not a grant, signature, or approval flag.
No Owner R12 signing code, private keys, or FBAUTH state is loaded.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import signal
import stat


EXPECTED_UID = 1000
EXPECTED_HOST = "GERAMEG"
DEFAULT_SOCKET = "/run/user/1000/framebridge-alpha01-cua/agent.sock"
_SESSION_ID = re.compile(r"[a-f0-9]{32}\Z")
_PAYLOAD_HASH = re.compile(r"[a-f0-9]{64}\Z")
_CONTEXT_FIELDS = frozenset(
    {"session_id", "program", "operation", "payload_sha256", "owner_assertion"}
)


def require_local_identity() -> None:
    if (
        os.getuid() != EXPECTED_UID
        or os.geteuid() != EXPECTED_UID
        or os.uname().nodename.upper() != EXPECTED_HOST
    ):
        raise RuntimeError("CUA_LOCAL_UID_OR_HOST_INVALID")


class LocalUidVerifier:
    """Apply the selected UID policy after LocalServer checks the peer UID.

    Manager constructs this context after resolving its own live session and
    validating the input. No caller-provided boolean or assertion grants access.
    """

    def __call__(self, context: object) -> bool:
        try:
            require_local_identity()
            if type(context) is not dict or set(context) != _CONTEXT_FIELDS:
                return False
            if context.get("program") not in ("demo-readonly", "bash"):
                return False
            if context.get("operation") not in ("key.send", "text.type"):
                return False
            session_id = context.get("session_id")
            payload_hash = context.get("payload_sha256")
            if type(session_id) is not str or _SESSION_ID.fullmatch(session_id) is None:
                return False
            if type(payload_hash) is not str or _PAYLOAD_HASH.fullmatch(payload_hash) is None:
                return False
            assertion = context.get("owner_assertion")
            return type(assertion) is dict and not assertion
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return False


def _stop(_signum: int, _frame: object) -> None:
    # Exiting the LocalServer context closes only this runtime's own sessions
    # and socket. Do not leave a stale socket after a normal systemd stop.
    raise SystemExit(0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="GERAMEG CUA-TTY local UID policy")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    require_local_identity()
    if args.socket != DEFAULT_SOCKET:
        raise RuntimeError("CUA_LOCAL_SOCKET_INVALID")

    parent = Path(args.socket).parent
    try:
        parent_state = parent.lstat()
    except FileNotFoundError:
        # LocalServer creates its own private directory with mode 0700.
        pass
    else:
        if (
            not stat.S_ISDIR(parent_state.st_mode)
            or parent_state.st_uid != EXPECTED_UID
            or stat.S_IMODE(parent_state.st_mode) != 0o700
        ):
            raise RuntimeError("CUA_LOCAL_SOCKET_PARENT_UNTRUSTED")

    # Keep imports on the existing service's installed PYTHONPATH. This file
    # does not copy, patch, or replace the original CUA-TTY package.
    from framebridge_cua_tty.core import Manager
    from framebridge_cua_tty.server import LocalServer

    from real_tty_manager import RealTerminalMixin

    class LocalPolicyManager(RealTerminalMixin, Manager):
        def status(self):
            value = super().status()
            # A callable verifier is not evidence of an Owner device. Report
            # the selected authority honestly while retaining session status.
            value["owner_device_connected"] = False
            value["input_enabled"] = "same-uid-1000"
            value["input_policy"] = "local-uid-1000"
            value["owner_signature_required"] = False
            return value

    manager = LocalPolicyManager(
        owner_verifier=LocalUidVerifier(), max_sessions=2, idle_timeout_s=600
    )
    signal.signal(signal.SIGTERM, _stop)
    with LocalServer(args.socket, manager=manager) as server:
        print("CUA_TTY_READY POLICY=LOCAL_UID_1000 OWNER_SIGNATURE_REQUIRED=NO", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
