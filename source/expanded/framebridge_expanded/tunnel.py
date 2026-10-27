"""Manual ChatGPT connection using the packaged, unmodified tunnel client."""
from __future__ import annotations

from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import warnings

KEY_ENV = "FRAMEBRIDGE_EXPANDED_RUNTIME_KEY"
_ID = re.compile(r"tunnel_[A-Za-z0-9_-]{1,128}\Z")
_ENV_REF = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]*\Z")
_RUNTIME_PREFIXES = ("TUNNEL_CLIENT_", "CONTROL_PLANE_", "OPENAI_", "MCP_", "HEALTH_", "LOG_", "CLOUDFLARED_", "HARPOON_")
_RUNTIME_NAMES = {"TUNNEL_ID", "TUNNEL_BIN", "MCP_URL", "CRED_FILE", "OPEN_WEB_UI", "PID_FILE", "ALLOW_REMOTE_UI", "ADMIN_UI_LOG_BUFFER_EVENTS", "PROXY_CHECK_INTERVAL", KEY_ENV}


class TunnelInputError(ValueError):
    """A safe error that never includes the pasted command or credential."""


@dataclass(frozen=True)
class TunnelSpec:
    tunnel_id: str
    organization_id: str = ""
    url_path: str = ""

    def __post_init__(self):
        if not isinstance(self.tunnel_id, str) or not _ID.fullmatch(self.tunnel_id):
            raise TunnelInputError("A valid tunnel ID is required.")
        if not isinstance(self.organization_id, str) or (self.organization_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.organization_id)):
            raise TunnelInputError("The organization identifier is invalid.")
        if not isinstance(self.url_path, str) or (self.url_path and (not self.url_path.startswith("/") or self.url_path.startswith("//") or any(c in self.url_path for c in ("?", "#", "\\", "..")))):
            raise TunnelInputError("The control-plane path is invalid.")

    def public_settings(self):
        return {"tunnel_id": self.tunnel_id,
                "control_plane_organization_id": self.organization_id,
                "control_plane_url_path": self.url_path}


def parse_tunnel_input(text: str) -> TunnelSpec:
    """Extract public setup data; never execute or retain the pasted command."""
    if not isinstance(text, str) or len(text) > 16384:
        raise TunnelInputError("Paste a tunnel ID or a supported tunnel-client command.")
    raw = text.strip()
    if any(c in raw for c in ("\n", "\r", "\x00", ";", "|", "&", "<", ">", "`", "$(", "${", "#")):
        raise TunnelInputError("Shell expressions are not accepted. Paste only the tunnel ID or one tunnel-client command.")
    try:
        words = shlex.split(raw, posix=True)
    except ValueError:
        raise TunnelInputError("The pasted command has incomplete quoting.") from None
    if len(words) == 1 and _ID.fullmatch(words[0]):
        return TunnelSpec(words[0])
    if not words:
        raise TunnelInputError("A tunnel ID is required.")
    program = Path(words.pop(0)).name
    if program in {"framebridge-original", "framebridge", "fb"}:
        if not words or words.pop(0) != "tunnel":
            raise TunnelInputError("Paste a tunnel-client command or only its tunnel ID.")
        if words[:1] == ["--"]:
            words.pop(0)
    elif program != "tunnel-client":
        raise TunnelInputError("Only a tunnel-client command or a tunnel ID is accepted.")
    if words[:2] == ["runtimes", "connect"]:
        mode, words = "connect", words[2:]
    elif words[:1] in (["run"], ["init"]):
        mode, words = words[0], words[1:]
    else:
        raise TunnelInputError("Use a run, init, or runtimes connect command containing an explicit tunnel ID.")
    options = {
        "--control-plane.tunnel-id": "id", "--tunnel-id": "id",
        "--control-plane.organization-id": "org", "--organization-id": "org",
        "--control-plane.base-url": "base", "--control-plane-base-url": "base",
        "--control-plane.url-path": "path", "--control-plane-url-path": "path",
        "--control-plane.api-key": "credential", "--runtime-api-key": "credential",
        "--control-plane-api-key-ref": "credential",
        "--mcp.server-url": "mcp", "--mcp-server-url": "mcp",
        "--health.listen-addr": "health", "--health-listen-addr": "health",
        "--health.url-file": "health_file",
        "--alias": "alias", "--name": "name", "--description": "description",
        "--profile": "profile", "--profile-dir": "profile_dir", "--sample": "sample",
    }
    found = {}
    while words:
        flag = words.pop(0)
        name, equals, value = flag.partition("=")
        if name == "--json" and mode == "connect" and not equals:
            continue
        kind = options.get(name)
        if kind is None:
            raise TunnelInputError("That command contains an unsupported option. Paste only its tunnel ID instead.")
        if not equals:
            if not words or words[0].startswith("--"):
                raise TunnelInputError("The pasted command has an option without a value.")
            value = words.pop(0)
        if not value or kind in found:
            raise TunnelInputError("The pasted command contains missing or repeated setup values.")
        found[kind] = value
    identifier = found.get("id", "")
    if not _ID.fullmatch(identifier):
        raise TunnelInputError("The command must contain one explicit tunnel ID.")
    if "credential" in found and not _ENV_REF.fullmatch(found["credential"]):
        raise TunnelInputError("Do not include a literal key or a key file. Enter the runtime key at the separate hidden prompt.")
    if "base" in found:
        u = urlsplit(found["base"])
        if u.scheme != "https" or u.netloc != "api.openai.com" or u.path not in ("", "/") or u.query or u.fragment:
            raise TunnelInputError("Use the OpenAI control plane. Paste only the tunnel ID if the command uses another endpoint.")
    if "mcp" in found:
        hint = found["mcp"]
        if hint.startswith("url="):
            hint = hint[4:].split(",", 1)[0]
        u = urlsplit(hint)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise TunnelInputError("The copied MCP URL cannot contain credentials, a query, or a fragment.")
    org = found.get("org", "")
    if org and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", org):
        raise TunnelInputError("The organization identifier is invalid.")
    path = found.get("path", "")
    if path and (not path.startswith("/") or path.startswith("//") or any(c in path for c in ("?", "#", "\\", ".."))):
        raise TunnelInputError("The control-plane path must be a plain absolute URL path.")
    return TunnelSpec(identifier, org, path)


def child_environment(secret: str, base=None, *, state=None):
    """Return a new child environment; never change os.environ."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith(_RUNTIME_PREFIXES) or key in _RUNTIME_NAMES:
            env.pop(key, None)
    if state is not None:
        from .config import private_dir
        home = private_dir(Path(state) / "chatgpt-home")
        for name in (".config", ".cache", ".local/state", ".run", "tmp"):
            private_dir(home / name)
        env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local/state"),
                    "XDG_RUNTIME_DIR": str(home / ".run"), "TMPDIR": str(home / "tmp")})
    env[KEY_ENV] = secret
    return env


def build_command(binary, state, mcp_url, spec: TunnelSpec):
    u = urlsplit(mcp_url)
    if u.scheme != "http" or u.hostname not in {"localhost", "127.0.0.1", "::1"} or u.username or u.password or u.query or u.fragment:
        raise TunnelInputError("The application MCP endpoint must be local HTTP without credentials.")
    args = [str(binary), "run",
            "--control-plane.base-url", "https://api.openai.com",
            "--control-plane.tunnel-id", spec.tunnel_id,
            "--control-plane.api-key", "env:" + KEY_ENV,
            "--mcp.server-url", "url=" + mcp_url,
            "--health.listen-addr", "127.0.0.1:0",
            "--log.level", "info"]
    if spec.organization_id:
        args.extend(["--control-plane.organization-id", spec.organization_id])
    if spec.url_path:
        args.extend(["--control-plane.url-path", spec.url_path])
    return args


def _stop_owned(process):
    if process.poll() is not None:
        return
    for sig, seconds in ((signal.SIGINT, 5), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=seconds)
            return
        except subprocess.TimeoutExpired:
            pass


def run_foreground(command, secret, *, state=None, output=None, popen=None, observe=None):
    """Keep the owned client in the foreground, with private logs and status."""
    from . import connection_monitor as monitor
    output = output or sys.stdout
    popen = popen or subprocess.Popen
    progress = monitor.Progress(output)
    process = reader = log = attempt = None
    reader_errors = []
    stop_reader = threading.Event()
    env = {}
    previous = {}
    ready_once = False
    last_detail = None
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    try:
        if state is None:
            raise monitor.ConnectionMonitorError("A private profile is required for connection logs.")
        log = monitor.PrivateLog(state, secret)
        attempt = monitor.Attempt(state)
        args = list(command)
        # Never reuse an endpoint file from an earlier connection attempt.
        while "--health.url-file" in args:
            index = args.index("--health.url-file")
            del args[index:index + 2]
        args.extend(["--health.url-file", str(attempt.health_file)])
        expected_tunnel = args[args.index("--control-plane.tunnel-id") + 1]
        expected_mcp = args[args.index("--mcp.server-url") + 1]
        if expected_mcp.startswith("url="):
            expected_mcp = expected_mcp[4:]
        env = child_environment(secret, state=state)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, interrupt)
        progress.change("starting", "[1/3] Starting the secure tunnel client.")
        output.write("Private connection log: " + str(log.path) + "\n")
        output.flush()
        started = time.time()
        process = popen(args, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        start_new_session=True, bufsize=0, umask=0o077)
        env.pop(KEY_ENV, None)
        log.write("Connection attempt started; tunnel=" + expected_tunnel + "; child_pid=" + str(process.pid))
        reader = threading.Thread(target=monitor.capture_output,
                                  args=(process.stdout, log, reader_errors, stop_reader), daemon=True)
        reader.start()
        next_probe = 0.0
        waiting_since = time.monotonic()
        instructions_shown = False
        while True:
            rc = process.poll()
            if rc is not None:
                log.write("Tunnel client exited; code=" + str(rc))
                if not ready_once:
                    progress.change("error", "[ERROR] The tunnel stopped before it was ready. Check the private connection log.")
                    return rc if rc > 0 else 1
                progress.change("closed" if rc == 0 else "error",
                                "[CLOSED] The tunnel client stopped." if rc == 0 else
                                "[ERROR] The tunnel connection stopped unexpectedly. Check the private connection log.")
                return rc if rc >= 0 else 1
            if reader_errors:
                progress.change("error", "[ERROR] Private connection logging failed. The tunnel will be stopped.")
                return 1
            if time.monotonic() >= next_probe:
                status, detail = monitor.probe(attempt.health_file, expected_tunnel, expected_mcp, started)
                if observe is not None:
                    observe(status)
                next_probe = time.monotonic() + 1.0
                if detail != last_detail:
                    log.write("Connection check: " + detail)
                    last_detail = detail
                if process.poll() is not None:
                    continue
                messages = {
                    "starting": "[1/3] Waiting for the tunnel client to start.",
                    "checking": "[2/3] Verifying the local MCP service.",
                    "access": "[3/3] Verifying tunnel access and authenticated polling.",
                    "ready": "[OK] Secure tunnel connected; local MCP service verified.",
                    "rejected": "[ERROR] Tunnel access was rejected. Press Ctrl+C and check the runtime key and tunnel permissions.",
                }
                if status == "ready":
                    ready_once = True
                    progress.change(status, messages[status])
                    if not instructions_shown:
                        output.write("Complete or select this tunnel in ChatGPT. A successful tool call verifies that connection.\n")
                        output.write("Keep this terminal open. Press Ctrl+C to disconnect.\n")
                        output.flush()
                        instructions_shown = True
                elif ready_once:
                    progress.change("retrying", "[RETRYING] Tunnel readiness was lost. Waiting for verified recovery; see the private log.")
                elif status == "rejected":
                    progress.change(status, messages[status])
                elif time.monotonic() - waiting_since >= 90:
                    progress.change("unverified", "[NOT READY] The connection is not verified yet. The client is retrying; check the private log or press Ctrl+C.")
                else:
                    progress.change(status, messages[status])
            progress.tick()
            time.sleep(0.2)
    except KeyboardInterrupt:
        progress.change("closed", "[STOPPED] Disconnecting this tunnel client.")
        return 130
    except Exception as error:
        if log is not None:
            try:
                log.write("Connection wrapper failed; type=" + type(error).__name__ + "; detail=" + str(error))
            except Exception:
                pass
        progress.change("error", "[ERROR] The connection could not be started or monitored. Check the private connection log and profile permissions.")
        return 1
    finally:
        cleanup_failed = False
        for sig in previous:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except Exception:
                cleanup_failed = True
        try:
            if process is not None:
                try:
                    _stop_owned(process)
                    if process.poll() is None:
                        cleanup_failed = True
                except Exception:
                    cleanup_failed = True
        finally:
            stop_reader.set()
            if reader is not None:
                reader.join(timeout=3)
            reader_finished = reader is None or not reader.is_alive()
            if not reader_finished or reader_errors:
                cleanup_failed = True
            try:
                if process is not None and process.stdout is not None and reader_finished:
                    process.stdout.close()
            except Exception:
                cleanup_failed = True
            try:
                progress.clear()
                if attempt is not None:
                    attempt.close()
            except Exception:
                cleanup_failed = True
            finally:
                try:
                    # Never close descriptors out from under an active writer.
                    if log is not None and reader_finished:
                        log.close()
                except Exception:
                    cleanup_failed = True
                finally:
                    env.pop(KEY_ENV, None)
                    secret = None
                    for sig, handler in previous.items():
                        try:
                            signal.signal(sig, handler)
                        except Exception:
                            cleanup_failed = True
        if cleanup_failed:
            progress.change("error", "[ERROR] Connection cleanup could not be fully verified. Check this client process before reconnecting.")
            return 1


def _hidden(prompt):
    if not sys.stdin.isatty():
        raise TunnelInputError("Use an interactive terminal for the hidden setup prompts.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise TunnelInputError("A terminal with hidden input is required.") from None


def _public_answer(label, saved=""):
    suffix = " [" + saved + "]" if saved else ""
    return input(label + suffix + ": ").strip() or saved


def _wait_managed_connection(state, current, output, *, wait_timeout=95):
    from . import autostart
    from .connection_monitor import Progress
    progress = Progress(output)
    deadline = time.monotonic() + max(0, wait_timeout)
    try:
        while True:
            if current.get("interrupted_after_commit"):
                progress.change("closed", "Connection continues in the background.")
                return 130
            connection = current.get("connection", {})
            local = current.get("runtime", {})
            if (current.get("configured") and current.get("enabled")
                    and current.get("service_active") and current.get("service_state") == "active"
                    and current.get("worker", {}).get("state") != "failed"
                    and local.get("running") and local.get("ready")
                    and connection.get("alive") and connection.get("verified")):
                progress.change("ready", "[OK] Connected.")
                return 0
            if (current.get("service_state") == "failed"
                    or current.get("worker", {}).get("state") == "failed"
                    or connection.get("state") in {"rejected", "error"}):
                progress.change("error", "[ERROR] Connection failed. Run fb connection status for details.")
                return 1
            if not current.get("configured") or not current.get("enabled"):
                progress.change("error", "[ERROR] Connection setup could not be completed.")
                return 1
            if time.monotonic() >= deadline:
                progress.change("unverified", "[PENDING] Connection is still being established in the background.")
                return 1
            progress.tick()
            time.sleep(0.2)
            current = autostart.status(state)
    finally:
        progress.clear()


def resume_managed_connection(state, resume_connection, *, output=None, wait_timeout=95):
    output = output or sys.stdout
    resumed = False
    try:
        output.write("Connecting...\n")
        output.flush()
        current = resume_connection(state)
        resumed = bool(current.get("configured") and current.get("enabled"))
        return _wait_managed_connection(state, current, output, wait_timeout=wait_timeout)
    except (EOFError, KeyboardInterrupt):
        output.write("\nConnection continues in the background.\n" if resumed else "\nConnection cancelled.\n")
        return 130


def _connect_managed(state, public_settings, configure_connection, output, *, wait_timeout=95):
    if not sys.stdin.isatty():
        raise TunnelInputError("Use an interactive terminal for connection setup.")
    runtime_key = None
    committed = False
    try:
        saved_label = public_settings.get("account_label", "")
        if saved_label and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", saved_label):
            saved_label = ""
        label = _public_answer("ChatGPT account (email label)", saved_label)
        if label and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", label):
            raise TunnelInputError("Enter an email label or leave it empty.")
        saved_id = public_settings.get("tunnel_id", "")
        if not _ID.fullmatch(saved_id):
            saved_id = ""
        raw = _hidden("Tunnel ID (hidden; Enter reuses saved ID): ")
        tunnel_id = raw.strip() or saved_id
        if not _ID.fullmatch(tunnel_id):
            raise TunnelInputError("Enter only a valid tunnel ID beginning with tunnel_.")
        spec = TunnelSpec(tunnel_id)
        reuse_saved = tunnel_id == saved_id
        raw = None
        if reuse_saved:
            spec = TunnelSpec(spec.tunnel_id, public_settings.get("control_plane_organization_id", ""), public_settings.get("control_plane_url_path", ""))
        runtime_key = _hidden("MCP server access key (hidden): ")
        if (not runtime_key or not runtime_key.isascii() or len(runtime_key.encode()) > 16384
                or any(ord(ch) < 33 or ord(ch) == 127 or ch.isspace() for ch in runtime_key)):
            raise TunnelInputError("Enter a single nonempty MCP server access key.")
        settings = dict(spec.public_settings(), account_label=label)
        output.write("Connecting...\n")
        output.flush()
        current = configure_connection(state, settings, runtime_key)
        runtime_key = None
        committed = bool(current.get("configured") and current.get("enabled"))
        return _wait_managed_connection(state, current, output, wait_timeout=wait_timeout)
    except (EOFError, KeyboardInterrupt):
        output.write("\nConnection continues in the background.\n" if committed else "\nConnection cancelled.\n")
        return 130
    finally:
        runtime_key = None


def connect_chatgpt(binary, state, mcp_url, public_settings=None,
                    save_public: Callable | None = None, *, output=None, foreground=False,
                    configure_connection: Callable | None = None, wait_timeout=95):
    """Connect with one hidden key; normal setup uses protected Linux services."""
    output = output or sys.stdout
    public_settings = dict(public_settings or {})
    if configure_connection is not None and not foreground:
        return _connect_managed(state, public_settings, configure_connection, output, wait_timeout=wait_timeout)
    from . import connection_background
    current = connection_background.status(state)
    if current["alive"]:
        output.write("This profile already has a background ChatGPT connection. Use framebridge connection status.\n")
        return 0 if current["verified"] else 1
    label = public_settings.get("account_label", "")
    output.write("ChatGPT login happens in your browser. The saved email is only an account label.\n")
    if label:
        output.write("An account label is saved for this profile.\n")
    output.write("Create or select your tunnel manually in OpenAI Platform.\n")
    output.write("The runtime key needs Tunnels Read + Use. No Admin API key is needed here.\n")
    output.write("The command is parsed as data; this profile's local MCP server will be used.\n")
    secret = None
    try:
        raw = _hidden("Tunnel ID or full tunnel-client command (hidden; Enter reuses the saved ID): ")
        reuse_saved = not raw.strip()
        if reuse_saved:
            raw = public_settings.get("tunnel_id", "")
        spec = parse_tunnel_input(raw)
        if reuse_saved:
            spec = TunnelSpec(spec.tunnel_id, public_settings.get("control_plane_organization_id", ""), public_settings.get("control_plane_url_path", ""))
        raw = None
        if save_public:
            save_public(spec.public_settings())
        secret = _hidden("Runtime API key (hidden; kept in memory for this connection): ").strip()
        if not secret or any(c.isspace() for c in secret) or "\x00" in secret:
            raise TunnelInputError("Enter a single nonempty runtime key.")
        command = build_command(binary, state, mcp_url, spec)
        if foreground:
            return connection_background.run_foreground(command, secret, state, output=output)
        return connection_background.start(command, secret, state, output=output)
    except (EOFError, KeyboardInterrupt):
        output.write("\nConnection cancelled.\n")
        return 130
    finally:
        secret = None
