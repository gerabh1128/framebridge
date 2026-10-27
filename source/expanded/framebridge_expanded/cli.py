"""English command-line interface for FrameBridge Expanded."""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import shlex

from .config import default_config, load_config, save_config
from . import tunnel, __version__

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


def resolve_state(value=None):
    return Path(value or os.environ.get("FRAMEBRIDGE_EXPANDED_STATE")
                or os.environ.get("FRAMEBRIDGE_STATE_DIR")
                or Path.home() / ".local/share/framebridge-expanded").expanduser().absolute()


def _module(name):
    return importlib.import_module("." + name, __package__)


def _show(value):
    print(json.dumps(_structured_result(value) if _OUTPUT and _OUTPUT.get("json") else value, ensure_ascii=False, indent=2, default=str))


def _ask(label, default="", *, ask=None):
    ask = ask or input
    value = ask(label + (f" [{default}]" if default != "" else "") + ": ").strip()
    return value if value else default


def _yes(label, default=False, *, ask=None):
    answer = _ask(label + (" [Y/n]" if default else " [y/N]"), ask=ask).lower()
    return default if not answer else answer in {"y", "yes"}



def _show_autostart_enabled(result):
    """Summarize observed state without treating configuration as readiness."""
    enabled = result.get("configured") is True and result.get("enabled") is True
    if not enabled:
        print("Automatic startup is not enabled. Check: fb status")
        return 1
    print("Automatic startup enabled.")
    service = result.get("service_state", "unavailable")
    if service not in {"active", "activating", "deactivating", "inactive", "failed", "reloading", "maintenance", "refreshing"}:
        service = "unavailable"
    print("Service: " + service)
    current = result.get("runtime")
    current = current if isinstance(current, dict) else {}
    capabilities = current.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    ready = current.get("ready") is True
    if ready and all(capabilities.get(name) is True for name in ("fbexec", "fbterm", "cua_tty_module")):
        print("Tools: FBEXEC, FBTERM, CUA-TTY ready.")
    elif current.get("status_unavailable"):
        print("Tools: status unavailable. Check: fb status")
    else:
        print("Tools: starting.")
    context = current.get("display_context")
    context = context if isinstance(context, dict) else {}
    kind = context.get("kind")
    if ready and capabilities.get("graphical_cua") is True and kind in {"physical-x11", "virtual-x11"}:
        print("Desktop: " + ("detected; CUA ready." if kind == "physical-x11" else "private; CUA ready."))
    elif kind == "text":
        print("Desktop: text mode.")
    else:
        print("Desktop: checking.")
    if current.get("role") == "node":
        print("Connection: managed by the central computer.")
    else:
        connection = result.get("connection")
        connection = connection if isinstance(connection, dict) else {}
        phase = connection.get("state")
        if connection.get("verified") is True:
            print("Connection: ready.")
        elif phase == "retrying":
            print("Connection: retrying in the background.")
        elif phase in {"starting", "checking", "access", "unverified"}:
            print("Connection: checking in the background.")
        elif phase in {"error", "rejected"}:
            print("Connection: needs attention. Check: fb status connection")
        elif phase in {"stopped", "closed"}:
            print("Connection: stopped. Check: fb status connection")
        else:
            print("Connection: checking. Check: fb status connection")
    return 0 if result.get("ok") is not False and service not in {"failed", "maintenance"} else 1


def setup(state, *, ask=None):
    """Review and save public settings; never start services or a tunnel."""
    state = resolve_state(state)
    if (state / "config.json").exists():
        current = _module("runtime").status(state)
        if current.get("running") or any(p.get("alive") for p in current.get("processes", [])):
            raise RuntimeError("Stop this profile before changing setup. Run fb service stop.")
    config = copy.deepcopy(load_config(state) if (state / "config.json").exists() else default_config())
    print("Set up this computer")
    print("standalone: local tools; central: local tools and a node broker; node: joins a central broker.")
    config["role"] = _ask("Role (standalone/central/node)", config["role"], ask=ask).lower()
    config["name"] = _ask("Computer name", config["name"], ask=ask)
    desktop_mode = config.get("headless", "auto")
    if desktop_mode == "auto":
        print("Desktop: automatic detection.")
    elif desktop_mode in ("yes", True):
        print("Desktop: existing private desktop setting preserved.")
    else:
        print("Desktop: existing physical desktop setting preserved.")
    ports = {}
    for service in ("mcp", "hub", "broker"):
        value = _ask(service.upper() + " port", str(config["ports"][service]), ask=ask)
        try:
            ports[service] = int(value)
        except ValueError:
            raise ValueError("Service ports must be integers.") from None
    config["ports"] = ports
    label = _ask("ChatGPT email label (optional; browser login is separate)", config.get("account_label", ""), ask=ask)
    if label and not _EMAIL.fullmatch(label):
        raise ValueError("Enter an email label or leave it empty.")
    config["account_label"] = label
    certificate_plan = None
    if config["role"] == "central":
        print("Loopback is local only. Choose 0.0.0.0 explicitly to accept IPv4 LAN nodes with HTTPS.")
        config["broker_listen_host"] = _ask("Broker listen host", config.get("broker_listen_host", "127.0.0.1"), ask=ask)
        mode = _ask("TLS certificate setup (generate/existing)", "existing" if config.get("tls_cert") else "generate", ask=ask).lower()
        if mode == "generate":
            if (state / "tls").exists() or any(config.get(k) for k in ("tls_cert", "tls_key", "tls_ca")):
                raise ValueError("This profile already has TLS material. Choose existing certificates or an explicit new profile.")
            tls = _module("tls")
            host = tls._dns_name(_ask("Certificate DNS name", config["name"].replace("_", "-"), ask=ask))
            address = str(tls._address(_ask("Advertised broker IP address", ask=ask)))
            certificate_plan = {"host": host, "address": address, "listen_host": config["broker_listen_host"]}
            print("A private local CA and server certificate will be created only after confirmation.")
        elif mode == "existing":
            config["tls_cert"] = _ask("Broker HTTPS certificate file (path only; optional on loopback)", config.get("tls_cert", ""), ask=ask)
            config["tls_key"] = _ask("Broker HTTPS private key file (path only)", config.get("tls_key", ""), ask=ask)
            config["tls_ca"] = _ask("Trusted CA file (path only; optional)", config.get("tls_ca", ""), ask=ask)
            config["broker_url"] = _ask("Advertised broker URL (optional on loopback)", config.get("broker_url", ""), ask=ask)
            scheme = "https" if config["tls_cert"] and config["tls_key"] else "http"
            config["broker_control_url"] = scheme + "://127.0.0.1:" + str(ports["broker"])
            if scheme == "https":
                print("Existing certificates must cover 127.0.0.1 or localhost for private local control.")
            if config["broker_listen_host"] not in {"localhost", "127.0.0.1", "::1"} and not (config["tls_cert"] and config["tls_key"]):
                raise ValueError("A broker listening outside loopback requires HTTPS certificate and key files.")
        else:
            raise ValueError("Choose generate or existing for TLS certificate setup.")
    elif config["role"] == "node":
        config["broker_url"] = _ask("Central broker URL (verified HTTPS for remote hosts; may be set later)", config.get("broker_url", ""), ask=ask)
        config["tls_ca"] = _ask("Central broker CA file (path only; blank uses system trust)", config.get("tls_ca", ""), ask=ask)
    print("Review this profile. TLS paths refer to node transport files; they are not ChatGPT API keys.")
    _show(config)
    if certificate_plan is not None:
        _show({"new_lan_certificate": certificate_plan})
    if not _yes("Save this configuration?", True, ask=ask):
        print("Configuration was not saved.")
        return 0
    save_config(state, config)
    if certificate_plan is not None:
        _show(_module("tls").create_certificates(state, **certificate_plan))
    print("Configuration saved. No service or ChatGPT connection was started.")
    if config["role"] == "node":
        print("Next: framebridge doctor, framebridge nodes enroll, ask the central operator to approve, then framebridge nodes poll.")
        print("After approval is confirmed, run fb service start.")
    else:
        print("Next: fb connect. This starts the managed tools and connection.")
    print("Use the same --state-dir option with every command when using a custom profile.")
    return 0


def connect_chatgpt(state, *, foreground=False, replace=False):
    config_path = Path(state) / "config.json"
    config = load_config(state) if config_path.exists() or config_path.is_symlink() else default_config()
    if config["role"] == "node":
        raise RuntimeError("A node profile does not publish an MCP server. Connect ChatGPT from the central profile.")
    runtime = _module("runtime")
    port = config["ports"]["mcp"]
    expected_url = "http://127.0.0.1:" + str(port) + "/mcp"
    binary = Path(runtime.PREFIX) / "original/geranas/FrameBridge-MCP-Private/bin/tunnel-client"
    if not binary.is_file():
        raise RuntimeError("The packaged tunnel client is missing.")
    manifest = Path(state) / "autostart/installation.json"
    if not foreground and not replace and (manifest.exists() or manifest.is_symlink()):
        return tunnel.resume_managed_connection(state, _module("autostart").resume_connection)
    if foreground:
        current = runtime.status(state)
        if current.get("running") is not True or current.get("ready") is not True or current.get("mcp_url") != expected_url:
            raise RuntimeError("This profile's MCP service is not ready. Run fb service start for this profile first.")
        def save_public(settings):
            config.update(settings)
            save_config(state, config)
        return tunnel.connect_chatgpt(binary, state, expected_url, config, save_public, foreground=True)
    def configure_connection(profile, settings, runtime_key):
        current = runtime.status(state) if config_path.is_file() else {"running": False}
        if current.get("running") and current.get("mcp_url") != expected_url:
            raise RuntimeError("This profile's MCP endpoint does not match its configuration. Run fb doctor.")
        if not current.get("running"):
            for name in ("mcp", "hub") + (("broker",) if config["role"] == "central" else ()):
                try:
                    with socket.socket() as probe:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        probe.bind(("127.0.0.1", config["ports"][name]))
                except OSError:
                    raise RuntimeError("The local " + name.upper() + " port " + str(config["ports"][name]) + " is already in use. Choose an available port with fb setup.") from None
        return _module("autostart").configure_connection(profile, settings, runtime_key)
    return tunnel.connect_chatgpt(binary, state, expected_url, config, configure_connection=configure_connection)


def nodes_command(args, state):
    config = load_config(state)
    if args.node_command == "list":
        broker = _module("broker")
        broker.configure(None, state_dir=state)
        previous = os.environ.get("FRAMEBRIDGE_EXPANDED_STATE")
        try:
            os.environ["FRAMEBRIDGE_EXPANDED_STATE"] = str(state)
            _show(_module("fleet").discover_nodes())
        finally:
            if previous is None:
                os.environ.pop("FRAMEBRIDGE_EXPANDED_STATE", None)
            else:
                os.environ["FRAMEBRIDGE_EXPANDED_STATE"] = previous
        return 0
    if args.node_command in {"pending", "approve"}:
        broker = _module("broker")
        broker.configure(None, state_dir=state)
        result = broker.list_pending() if args.node_command == "pending" else broker.approve(args.request_id, args.node_id, state_dir=state)
    else:
        node = _module("node")
        if args.node_command == "enroll":
            result = node.enroll(args.central or config.get("broker_url", ""), args.name or config["name"], state_dir=state)
        elif args.node_command == "status":
            result = node.status(state_dir=state)
        elif args.node_command == "poll":
            result = node.poll_enrollment(state_dir=state)
        else:
            result = node.run(state_dir=state, once=args.once)
    if result is not None:
        _show(result)
    return 0


def _target_tty(state, target, payload=None, service=None):
    """Use the existing authenticated node transport; never guess SSH identities."""
    if target == "LOCAL":
        tty = _module("tty")
        return getattr(tty, service)(state) if service else tty.request(state, payload)
    previous = os.environ.get("FRAMEBRIDGE_EXPANDED_STATE")
    os.environ["FRAMEBRIDGE_EXPANDED_STATE"] = str(state)
    try:
        fleet = _module("fleet")
        wire = _module("tty_mcp")
        def execute(**kwargs):
            alias = kwargs.pop("target")
            return fleet.execute_node(alias, **kwargs)
        if service:
            return wire._exec_json(execute, target, [service])
        return wire._remote_request(execute, target, payload)
    finally:
        if previous is None:
            os.environ.pop("FRAMEBRIDGE_EXPANDED_STATE", None)
        else:
            os.environ["FRAMEBRIDGE_EXPANDED_STATE"] = previous


def tty_command(args, state):
    action = getattr(args, "tty_service_action", None) if args.tty_command == "service" else args.tty_command
    target = getattr(args, "node", "LOCAL")
    if action in {"start", "stop", "status"}:
        result = _target_tty(state, target, service=action)
    else:
        payload = {"authorized": True}
        if action == "list":
            payload["op"] = "status"
        elif action == "open":
            payload.update(op="open", program=args.program, rows=args.rows, cols=args.cols)
        elif action == "sudo":
            payload.update(op="sudo", session_id=args.session_id, mode=args.sudo_mode)
        elif action == "observe":
            payload.update(op="observe", session_id=args.session_id)
        elif action == "key":
            payload.update(op="key.send", session_id=args.session_id, key=args.key)
        elif action == "type":
            text = args.text if args.text is not None else input("Text to send: ")
            payload.update(op="text.type", session_id=args.session_id, text=text)
        elif action == "resize":
            payload.update(op="resize", session_id=args.session_id, rows=args.rows, cols=args.cols)
        else:
            payload.update(op="close", session_id=args.session_id)
        result = _target_tty(state, target, payload=payload)
    _show(result)
    return 1 if isinstance(result, dict) and result.get("ok") is False else 0


def tutorial():
    runtime = _module("runtime")
    candidates = [Path(runtime.PREFIX) / "docs/DEBIAN-TUTORIAL.md",
                  Path(__file__).resolve().parents[2] / "docs/DEBIAN-TUTORIAL.md"]
    for path in candidates:
        if path.is_file():
            print(path.read_text(encoding="utf-8"))
            return 0
    raise RuntimeError("The tutorial is not installed. Run framebridge --help.")



_OUTPUT = None


def _structured_result(value):
    """Versioned envelope used only when --json was explicitly requested."""
    return {"schema_version": 1, "product": "FrameBridge", "version": __version__,
            "command": _OUTPUT.get("command") if _OUTPUT else None,
            "ok": not isinstance(value, dict) or value.get("ok") is not False,
            "result": value}


def _result_code(value):
    return 1 if isinstance(value, dict) and value.get("ok") is False else 0


def _scoped_action(action):
    if _OUTPUT and _OUTPUT.get("explicit_state") and action.startswith("fb "):
        return "fb --state-dir " + shlex.quote(_OUTPUT["explicit_state"]) + " " + action[3:]
    return action


def _interaction_required(action):
    action = _scoped_action(action)
    result = {"ok": False, "error": "INTERACTION_REQUIRED",
              "message": "Complete this action in a normal interactive terminal. Do not send credentials through chat.",
              "next_action": action}
    if _OUTPUT and _OUTPUT.get("json"):
        _show(result)
    else:
        print("An interactive terminal is required. Next: " + action, file=sys.stderr)
    return 2


def _saved_noninteractive_ready(state):
    value = _module("lifecycle").status(state)
    boot = value.get("boot", {})
    return all(boot.get(name) is True for name in
               ("configured", "encrypted_credential_present", "linger"))


def _runtime_label(value):
    if value.get("status_unavailable") or "running" not in value:
        return "unavailable"
    return "ready" if value.get("ready") is True else ("running" if value.get("running") is True else "stopped")


def _boot_label(value):
    if value.get("configured") is False:
        return "not configured"
    if value.get("configured") is not True or type(value.get("enabled")) is not bool:
        return "unavailable"
    return "enabled" if value["enabled"] else "disabled"


def _human_status(value, scope=None):
    if scope == "connection":
        conn = value
        print("ChatGPT connection: " + ("connected (verified)" if conn.get("verified") is True else str(conn.get("state", "unavailable"))))
        if conn.get("desired_connection"):
            print("Connection intent: " + str(conn["desired_connection"]))
        return
    if scope == "boot":
        boot = value
        policy = _boot_label(boot)
        print("Automatic startup: " + policy)
        print("Current service: " + str(boot.get("service_state", "unavailable")))
        print("Saved connection material: " + ("present" if boot.get("encrypted_credential_present") is True else ("absent" if boot.get("encrypted_credential_present") is False else "unavailable")))
        return
    if scope == "runtime":
        runtime = value
        print("Local tools: " + _runtime_label(runtime))
        print("Role: " + str(runtime.get("role", "unavailable")))
        return
    if scope == "console":
        print("Console: " + str(value.get("status", value.get("state", "available" if value.get("panes") else "not open"))))
        return
    runtime = value.get("runtime", {})
    runtime = runtime if isinstance(runtime, dict) else {}
    connection = value.get("connection", {})
    connection = connection if isinstance(connection, dict) else {}
    boot = value.get("boot", {})
    boot = boot if isinstance(boot, dict) else {}
    print("FrameBridge " + __version__)
    print("Role: " + str(runtime.get("role", "unavailable")))
    print("Local tools: " + _runtime_label(runtime))
    if runtime.get("role") == "node":
        print("ChatGPT connection: provided by the central computer")
    else:
        print("ChatGPT connection: " + ("connected (verified)" if connection.get("verified") is True else str(connection.get("state", "unavailable"))))
        print("Connection intent: " + str(value.get("desired_connection", "unavailable")))
    print("Automatic startup: " + _boot_label(boot))
    print("Current service: " + str(boot.get("service_state", "unavailable")))
    if value.get("ok") is False:
        print("Some status information is unavailable. Run fb doctor or inspect fb status --json.")


def _status_command(args, state):
    scope = args.scope
    if scope == "console":
        value = _module("console").status(state, "LOCAL")
    else:
        aggregate = _module("lifecycle").status(state)
        value = aggregate.get(scope, {}) if scope else aggregate
        if scope == "connection":
            value = dict(value, desired_connection=aggregate.get("desired_connection", "unavailable"))
    if args.json:
        _show(value)
    else:
        _human_status(value, scope)
    return _result_code(value)


def _doctor_command(args, state):
    value = _module("runtime").doctor(state)
    if args.json:
        _show(value)
    else:
        print("Diagnostics: " + ("passed" if value.get("ok") else "needs attention"))
        compatibility = value.get("platform_compatibility") or {}
        print("Platform: " + ("compatible" if compatibility.get("ok") else "not verified compatible"))
        if compatibility.get("profile_id"):
            print("Build profile: " + str(compatibility["profile_id"]))
        for issue in compatibility.get("issues", []):
            print("Platform issue: " + str(issue))
        print("Desktop: " + str(value.get("display", "unavailable")))
        for item in value.get("missing_commands", []):
            print("Missing command: " + str(item))
        for item in value.get("issues", []):
            print("Issue: " + str(item))
        print("This checks prerequisites; use fb status to inspect the current connection.")
    return _result_code(value)


def _service_command(args, state):
    action = args.service_action
    lifecycle = _module("lifecycle")
    if action == "credentials":
        if not args.confirm:
            result = {"ok": False, "error": "CONFIRMATION_REQUIRED",
                      "message": "This stops and removes the managed connection setup and saved credential. The profile is retained.",
                      "next_action": _scoped_action("fb service credentials forget --confirm")}
            _show(result)
            return 2
        result = lifecycle.forget_credentials(state)
    elif action == "boot":
        if args.boot_action == "enable" and (args.json or args.non_interactive):
            boot = lifecycle.status(state).get("boot", {})
            if not boot.get("configured") or not boot.get("linger"):
                return _interaction_required("fb service boot enable")
        result = getattr(lifecycle, "boot_" + args.boot_action)(state)
    else:
        if action == "start" and (args.json or args.non_interactive):
            config_path = state / "config.json"
            if config_path.is_file() and load_config(state)["role"] == "node":
                if not lifecycle.status(state).get("boot", {}).get("configured"):
                    return _interaction_required("fb service start")
        result = getattr(lifecycle, "service_" + action)(state)
    if args.json:
        _show(result)
    elif result.get("ok") is False:
        print("Managed operation failed: " + str(result.get("error", result.get("message", "inspect fb status --json"))), file=sys.stderr)
    else:
        print("Managed " + ("startup policy" if action == "boot" else "service") + ": " + str(result.get("state", result.get("operation", "request completed"))))
        if result.get("initial_configuration") is True:
            print("Initial node service setup: automatic startup enabled.")
        if action == "boot":
            print("Current execution and saved connection material are retained.")
        elif action == "credentials":
            print("The profile and user service persistence are retained.")
        if isinstance(result.get("runtime"), dict) and isinstance(result.get("boot"), dict):
            _human_status(result)
        elif result.get("message"):
            print(str(result["message"]))
    return _result_code(result)


def _help_command(args):
    guide = _module("guide")
    topic = "all" if args.command == "tutorial" else args.topic
    if args.json:
        value = guide.get_guide(topic)
        _show(value)
        return _result_code(value)
    text = guide.render_text(topic)
    if sys.stdout.isatty() and sys.stdin.isatty() and not args.non_interactive:
        import pydoc
        pydoc.pager(text)
    else:
        print(text)
    return 0


def _add_common_options(parser):
    # SUPPRESS preserves an option supplied before a nested subcommand.
    if not any("--state-dir" in item.option_strings for item in parser._actions):
        parser.add_argument("--state-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="Emit one versioned JSON result; never request interactive input.")
    parser.add_argument("--non-interactive", action="store_true", default=argparse.SUPPRESS,
                        help="Never request interactive input; report when a terminal action is needed.")
    for item in parser._actions:
        if isinstance(item, argparse._SubParsersAction):
            for child in item.choices.values():
                _add_common_options(child)

def build_parser():
    parser = argparse.ArgumentParser(prog="framebridge",
        description="One command-line interface for human operators and authorized agents. fb is the short name for framebridge.",
        epilog="Normal first use: fb connect. Advanced setup is optional. Read fb help for the guide and fb help compatibility for older commands.")
    parser.add_argument("--state-dir", help="Profile directory; defaults to ~/.local/share/framebridge-expanded.")
    parser.set_defaults(json=False, non_interactive=False)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    connect = sub.add_parser("connect", help="Connect or resume ChatGPT; normal first use needs no separate setup/start.")
    connect.add_argument("connection", nargs="?", choices=("chatgpt",), default=None, help=argparse.SUPPRESS)
    modes = connect.add_mutually_exclusive_group()
    modes.add_argument("--foreground", action="store_true", help="Advanced temporary connection; this terminal remains open.")
    modes.add_argument("--replace", action="store_true", help="Replace saved connection settings through hidden local prompts.")
    disconnect = sub.add_parser("disconnect", help="Pause ChatGPT until the next connect, including after restart; keep local tools.")
    disconnect.add_argument("disconnection", nargs="?", choices=("chatgpt",), default=None, help=argparse.SUPPRESS)
    status = sub.add_parser("status", help="Observe local tools, ChatGPT connection and startup policy without changing them.")
    status.add_argument("scope", nargs="?", choices=("connection", "runtime", "boot", "console"))
    sub.add_parser("doctor", help="Diagnose prerequisites and problems; does not repair or connect.")
    sub.add_parser("setup", help="Optional advanced role/profile setup; does not start services or connect.")
    help_parser = sub.add_parser("help", help="Read the installed, versioned English guide.")
    help_parser.add_argument("topic", nargs="?", default="start", help="Guide topic; use all for the complete reference.")
    service = sub.add_parser("service", help="Manage current services separately from future startup policy.")
    services = service.add_subparsers(dest="service_action", required=True)
    services.add_parser("start", help="Start services; the first node start also prepares automatic startup.")
    services.add_parser("stop", help="Stop this profile's managed stack now without changing boot policy.")
    boot = services.add_parser("boot", help="Change future startup policy; retain current execution and saved material.")
    boot_actions = boot.add_subparsers(dest="boot_action", required=True)
    for action in ("enable", "disable"):
        boot_actions.add_parser(action)
    credentials = services.add_parser("credentials", help="Explicit destructive management of saved connection setup.")
    forget = credentials.add_subparsers(dest="credential_action", required=True).add_parser("forget",
        help="Stop/remove managed connection setup and saved credential; retain profile and user persistence.")
    forget.add_argument("--confirm", action="store_true", help="Explicitly confirm this destructive operation.")
    screen = sub.add_parser("screen-access", help="Explicit persistent read-only access to the real local framebuffer.")
    ss = screen.add_subparsers(dest="screen_access_command", required=True)
    ss.add_parser("status", help="Read the access policy without changing permissions.")
    enable = ss.add_parser("enable", help="Administrator opt-in: sudo fb screen-access enable --user NAME.")
    enable.add_argument("--user", required=True)
    ss.add_parser("disable", help="Revoke only the exact owned unchanged framebuffer ACL.")
    certificates = sub.add_parser("certificates", help="Manage profile-scoped private LAN TLS certificates.")
    create = certificates.add_subparsers(dest="certificate_command", required=True).add_parser("create",
        help="Create a private CA/server certificate without overwriting existing material.")
    create.add_argument("--host", required=True, help="Certificate DNS name; localhost is included automatically.")
    create.add_argument("--address", required=True, help="Broker IP included in the certificate and advertised by default.")
    create.add_argument("--listen-host", help="Explicit bind IP; preserve the saved setting when omitted.")
    create.add_argument("--advertise-host", help="Advertise the certificate DNS name instead of its IP.")
    nodes = sub.add_parser("nodes", help="Manage node enrollment, approval and agent state.")
    ns = nodes.add_subparsers(dest="node_command", required=True)
    for name in ("list", "pending", "status", "poll"):
        ns.add_parser(name)
    approve = ns.add_parser("approve", help="Approve after checking the node identity.")
    approve.add_argument("request_id")
    approve.add_argument("--node-id", required=True, help="Expected UUID from the pending request.")
    enroll = ns.add_parser("enroll", help="Submit enrollment; this does not start the agent.")
    enroll.add_argument("--central")
    enroll.add_argument("--name")
    run = ns.add_parser("run", help="Advanced foreground node agent.")
    run.add_argument("--once", action="store_true", help="Process at most one polling cycle.")
    console = sub.add_parser("console", help="Open or reattach the shared text Bash/FBTERM work view.")
    console.add_argument("--node", default="LOCAL", help="Local alias; remote interactive attachment requires explicit SSH.")
    console.add_argument("--mode", choices=("auto", "gui", "tty"), default="auto")
    console.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    console.add_argument("--status", action="store_true", help=argparse.SUPPRESS)
    login_flags = console.add_mutually_exclusive_group()
    login_flags.add_argument("--enable-login", action="store_const", const="enable", dest="login_action", help=argparse.SUPPRESS)
    login_flags.add_argument("--disable-login", action="store_const", const="disable", dest="login_action", help=argparse.SUPPRESS)
    login_flags.add_argument("--login-status", action="store_const", const="status", dest="login_action", help=argparse.SUPPRESS)
    con_sub = console.add_subparsers(dest="console_command")
    login = con_sub.add_parser("login", help="Optional work-view opening after authenticated tty1 login; not service startup.")
    login.add_argument("console_login_action", choices=("enable", "status", "disable"))
    tty = sub.add_parser("tty", help="Manage persistent Bash sessions; persistence is between calls, not reboots.")
    ts = tty.add_subparsers(dest="tty_command", required=True, metavar="ACTION")
    tty_service = ts.add_parser("service", help="Manage the terminal daemon.")
    tss = tty_service.add_subparsers(dest="tty_service_action", required=True)
    for name in ("start", "stop", "status"):
        item = tss.add_parser(name)
        item.add_argument("--node", default="LOCAL")
    for name in ("start", "stop", "status", "list"):
        item = ts.add_parser(name, help="List session state." if name == "list" else argparse.SUPPRESS)
        item.add_argument("--node", default="LOCAL")
    sudo = ts.add_parser("sudo", help="Per-session sudo eligibility; changing mode recreates Bash and loses shell variables.")
    modes = sudo.add_subparsers(dest="sudo_mode", required=True)
    for mode in ("on", "status", "off"):
        item = modes.add_parser(mode)
        item.add_argument("session_id")
        item.add_argument("--node", default="LOCAL")
    op = ts.add_parser("open", help="Create a real Bash session and return its ID.")
    op.add_argument("--node", default="LOCAL")
    op.add_argument("--program", choices=("bash",), default="bash")
    op.add_argument("--rows", type=int, default=24)
    op.add_argument("--cols", type=int, default=80)
    for name in ("observe", "key", "type", "resize", "close"):
        item = ts.add_parser(name, help={"observe":"Read terminal text, not a real screen capture.",
             "key":"Send a named key such as ENTER or CTRL_C.", "type":"Type text without adding Enter.",
             "resize":"Change terminal dimensions.", "close":"Close this session and its processes; not detachment."}[name])
        item.add_argument("session_id")
        item.add_argument("--node", default="LOCAL")
        if name == "key":
            item.add_argument("key")
        elif name == "type":
            item.add_argument("text", nargs="?")
        elif name == "resize":
            item.add_argument("--rows", type=int, required=True)
            item.add_argument("--cols", type=int, required=True)
    # Compatibility parsers retain their actual historical scope.
    sub.add_parser("start", help=argparse.SUPPRESS, description="Compatibility: starts only local runtime; use fb service start for the managed stack.")
    sub.add_parser("stop", help=argparse.SUPPRESS, description="Compatibility: pauses this boot's managed worker and stops local runtime; startup policy is retained.")
    sub.add_parser("tutorial", help=argparse.SUPPRESS, description="Compatibility: read the complete versioned guide; prefer fb help.")
    connection = sub.add_parser("connection", help=argparse.SUPPRESS)
    connection.add_subparsers(dest="connection_action", required=True).add_parser("status")
    autostart = sub.add_parser("autostart", help=argparse.SUPPRESS,
        description="Compatibility controls. WARNING: disable removes managed setup and saved connection material. Prefer fb service boot disable for policy only.")
    auto = autostart.add_subparsers(dest="autostart_action", required=True)
    for action in ("enable", "status", "disable"):
        auto.add_parser(action)
    # Do not advertise duplicate compatibility spellings in ordinary help.
    for group in (sub, ts):
        group._choices_actions[:] = [a for a in group._choices_actions if a.help is not argparse.SUPPRESS]
    _add_common_options(parser)
    return parser
def main(argv=None):
    global _OUTPUT
    parser = build_parser()
    args = parser.parse_args(argv)
    previous = _OUTPUT
    _OUTPUT = {"json": args.json, "command": args.command,
               "explicit_state": str(resolve_state(args.state_dir)) if args.command != "screen-access" and (args.state_dir or os.environ.get("FRAMEBRIDGE_EXPANDED_STATE") or os.environ.get("FRAMEBRIDGE_STATE_DIR")) else None}
    try:
        return _dispatch(parser, args)
    finally:
        _OUTPUT = previous


def _dispatch(parser, args):
    state = None if args.command == "screen-access" else resolve_state(args.state_dir)
    if args.command is None:
        if args.json or args.non_interactive or not sys.stdin.isatty():
            if args.json:
                return _help_command(argparse.Namespace(command="help", topic="start", json=True, non_interactive=True))
            parser.print_help()
            return 0
        print("FrameBridge\n1. Connect\n2. Status\n3. Doctor\n4. Open console\n5. Help\n6. Advanced setup\n0. Exit")
        try:
            choice = input("Choose an action: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            return 130
        action = {"1":["connect"], "2":["status"], "3":["doctor"], "4":["console"],
                  "5":["help"], "6":["setup"]}.get(choice)
        if choice == "0":
            return 0
        if action is None:
            print("Choose one of the listed actions.", file=sys.stderr)
            return 2
        return main(["--state-dir", str(state)] + action)
    try:
        if args.command == "screen-access":
            value = _module("screen_access").command(args)
            _show(value)
            return _result_code(value)
        if args.command in {"help", "tutorial"}:
            return _help_command(args)
        if args.command == "setup":
            if args.json or args.non_interactive or not sys.stdin.isatty():
                return _interaction_required("fb setup")
            return setup(state)
        if args.command == "connect":
            if args.json or args.non_interactive:
                config_path = state / "config.json"
                if config_path.is_file() and load_config(state)["role"] == "node":
                    raise RuntimeError("A node profile does not publish an MCP server. Connect ChatGPT from the central profile.")
                manifest = state / "autostart/installation.json"
                if args.foreground or args.replace or not manifest.is_file() or not _saved_noninteractive_ready(state):
                    return _interaction_required("fb connect" + (" --foreground" if args.foreground else (" --replace" if args.replace else "")))
                if args.json:
                    import contextlib
                    with contextlib.redirect_stdout(sys.stderr):
                        code = connect_chatgpt(state, foreground=False, replace=False)
                    value = _module("lifecycle").status(state)
                    if code:
                        value = dict(value, ok=False, error="CONNECTION_NOT_READY")
                    _show(value)
                    return code
            return connect_chatgpt(state, foreground=args.foreground, replace=args.replace)
        if args.command == "status":
            return _status_command(args, state)
        if args.command == "doctor":
            return _doctor_command(args, state)
        if args.command == "service":
            return _service_command(args, state)
        if args.command == "connection":
            value = _module("connection_background").status(state)
            _show(value)
            return _result_code(value)
        if args.command == "disconnect":
            module = "connection_background" if args.disconnection == "chatgpt" else "lifecycle"
            result = _module(module).disconnect(state)
            if args.json or args.disconnection == "chatgpt":
                _show(result)
            elif result.get("ok") is False:
                print("Connection pause failed: " + str(result.get("error", "inspect fb status connection --json")), file=sys.stderr)
            else:
                print("ChatGPT connection paused until fb connect. Local tools and saved connection material are retained.")
            return _result_code(result)
        if args.command == "autostart":
            autostart = _module("autostart")
            if args.autostart_action == "enable" and (args.json or args.non_interactive):
                value = autostart.status(state)
                if not value.get("configured") or not value.get("linger"):
                    return _interaction_required("fb service boot enable")
            result = getattr(autostart, args.autostart_action)(state)
            if args.json or args.autostart_action == "status":
                _show(result)
                return _result_code(result)
            if args.autostart_action == "enable":
                if result.get("state") == "cancelled":
                    print("Automatic startup was not enabled.")
                    return 130
                return _show_autostart_enabled(result)
            print("Automatic startup disabled." if result.get("state") == "disabled" else "No managed automatic startup configuration was found.")
            try:
                current = autostart.status(state)
            except (OSError, ValueError, RuntimeError):
                current = {}
            service = current.get("service_state", "unavailable")
            if service not in {"active", "activating", "deactivating", "inactive", "failed", "reloading", "maintenance", "refreshing"}:
                service = "unavailable"
            print("Service: " + service)
            if result.get("desktop_setting_restored") is False:
                print("Current profile settings were preserved; no previous desktop setting was restored.")
            if current.get("linger") is True:
                print("User service persistence remains enabled.")
            return _result_code(result)
        if args.command == "certificates":
            value = _module("tls").create_certificates(state, args.host, args.address, listen_host=args.listen_host, advertise_host=args.advertise_host)
            _show(value)
            return _result_code(value)
        if args.command == "nodes":
            if args.json and args.node_command == "run" and not args.once:
                return _interaction_required("fb nodes run")
            return nodes_command(args, state)
        if args.command == "console":
            action = getattr(args, "console_login_action", None) or args.login_action
            if getattr(args, "console_login_action", None) and args.login_action:
                raise ValueError("Choose one console login command form.")
            if args.status and action:
                raise ValueError("Choose console status or a login action, not both.")
            if action:
                if args.node != "LOCAL":
                    raise ValueError("Console login setup is local; run it on the requested node.")
                result = getattr(_module("console_startup"), action)(state)
                _show(result)
                return _result_code(result)
            console = _module("console")
            if args.status:
                _show(console.status(state, args.node))
                return 0
            if args.json or args.non_interactive:
                return _interaction_required("fb console")
            return console.run(state, args.node, inside=args.inside, mode=args.mode)
        if args.command == "tty":
            if args.tty_command == "type" and args.text is None and (args.json or args.non_interactive):
                return _interaction_required("fb tty type SESSION_ID TEXT")
            return tty_command(args, state)
        if args.command == "stop":
            _module("autostart").stop_for_current_boot(state)
        value = getattr(_module("runtime"), args.command)(state)
        _show(value)
        return _result_code(value)
    except (EOFError, KeyboardInterrupt):
        if args.json:
            _show({"ok":False, "error":"CANCELLED"})
        else:
            print("\nCancelled.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        if args.json:
            _show({"ok":False, "error":"COMMAND_FAILED", "message":str(exc)})
        else:
            print("Error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
