from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time

from .product_core import ProductError
from .seed_core import SEED_VERSION, SeedContext, atomic_write_json
from .classic_tty import execute_frame, render_result, render_snapshot
from .console_tty import run_console
from .plan_engine import PlanEngine
from .memory_store import MemoryStore
from .first_run import SetupChoice, detect_system, provider_instructions, render_privilege_explanation, save_setup_config, setup_summary
from .console_chat import run_console_chat
from .qr_terminal import render_qr_compact, render_qr_mono, render_qr_ansi, render_qr_unicode, TerminalQrError
from .pairing import FBPAIR_PROTOCOL, PairingStore, pairing_qr_payload, public_pairing_view
from .pairing_client import PairingClientError, poll_pairing, start_pairing
from .network_security import inspect_network, pairing_policy, forwarding_policy
from .pairing_window import open_pairing_window, pairing_window_status, close_pairing_window
from .network_lifecycle import reconcile_once, reconcile_broker_cancel


def emit(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _sync_pairing_config(ctx: SeedContext, view: dict) -> dict:
    config=ctx.load_config() or {}
    pairing=config.get("pairing") or {}
    pairing.update({
        "protocol":FBPAIR_PROTOCOL,
        "state":view.get("state","not_paired"),
        "transport":"broker_outbound" if view.get("state")!="not_paired" else "unpaired",
    })
    for key in (
        "pair_id","broker_base_url","account_issuer",
        "account_subject_sha256","revocation_id","paired_at",
        "broker_confirm_required","broker_confirmed_at",
        "broker_cancel_required","broker_cancelled_at","broker_cancel_state",
    ):
        if view.get(key) is not None:
            pairing[key]=view.get(key)
    config["pairing"]=pairing
    config["connection_mode"]="PAIRED" if view.get("state")=="paired" else "PAIRING"
    setup=config.get("setup")
    if isinstance(setup,dict):
        setup["pairing_state"]=view.get("state","not_paired")
    atomic_write_json(ctx.config_path,config)
    return config


def _render_pair_qr(view: dict, mode: str) -> None:
    payload=pairing_qr_payload(view)
    if not sys.stdout.isatty():
        return
    if mode=="compact":
        render_qr_compact(payload)
    elif mode=="mono":
        render_qr_mono(payload)
    elif mode=="unicode":
        render_qr_unicode(payload)
    else:
        render_qr_ansi(payload)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="framebridge",
        description="FrameBridge Seed - Linux text-mode bootstrap",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SEED_VERSION}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize node identity and provider")
    init.add_argument("--provider", default="chatgpt")
    init.add_argument("--hostname", default="")

    sub.add_parser("identity", help="Show persistent node identity")
    sub.add_parser("doctor", help="Run READONLY bootstrap diagnostics")
    sub.add_parser("status", help="Show Seed status")
    sub.add_parser("fingerprint", help="Show non-secret node fingerprint")
    sub.add_parser("snapshot", help="Generate Classic AI context snapshot")
    sub.add_parser("console", help="Open FrameBridge Classic in the current TTY")
    classic = sub.add_parser("classic", help="Process one Classic frame from stdin or file")
    classic.add_argument("--file", default="-")
    setup = sub.add_parser("setup", help="Run the FrameBridge first-run setup wizard")
    setup.add_argument("--provider", choices=("chatgpt","ollama","lmstudio","other"), default="")
    setup.add_argument("--privilege-mode", choices=("totp","sudo_tty"), default="")
    setup.add_argument("--language", default="en")
    setup.add_argument("--non-interactive", action="store_true")
    sub.add_parser("setup-status", help="Show first-run setup state")

    net = sub.add_parser("net", help="Inspect FrameBridge network/firewall policy")
    net_sub = net.add_subparsers(dest="net_command", required=True)
    net_sub.add_parser("status", help="Show READONLY FBNET interface/firewall status")
    p_net_policy = net_sub.add_parser("pairing-policy", help="Render pairing-window policy without applying changes")
    p_net_policy.add_argument("--port", type=int, default=18765)
    p_net_forward = net_sub.add_parser("forwarding-policy", help="Render typed runtime forwarding policy without applying changes")
    p_net_forward.add_argument("--in-interface", required=True)
    p_net_forward.add_argument("--out-interface", required=True)
    p_net_forward.add_argument("--source-subnet", required=True)
    p_net_forward.add_argument("--destination-subnet", required=True)
    p_net_forward.add_argument("--protocol", choices=("any","tcp","udp"), default="any")
    p_net_forward.add_argument("--destination-port", type=int, default=0)
    p_net_forward.add_argument("--nat", action="store_true")
    p_net_forward.add_argument("--lifetime", choices=("runtime",), default="runtime")

    p_window = net_sub.add_parser("pairing-window", help="Manage the short-lived local LAN pairing firewall window")
    p_window_sub = p_window.add_subparsers(dest="pairing_window_command", required=True)
    p_window_open = p_window_sub.add_parser("open", help="Open a scoped runtime pairing window after local authorization")
    p_window_open.add_argument("--port", type=int, default=18765)
    p_window_open.add_argument("--ttl", type=int, default=600)
    p_window_sub.add_parser("status", help="Show the current pairing-window transaction")
    p_window_close = p_window_sub.add_parser("close", help="Close the active pairing window")
    p_window_close.add_argument("--reason", choices=("user","cancelled","paired","denied","network_change","service_stop","reconcile"), default="user")

    net_sub.add_parser("reconcile", help="Re-evaluate an active pairing window against current network identity")

    pair = sub.add_parser("pair", help="Pair this Linux node with an authorized AI account")
    pair_sub = pair.add_subparsers(dest="pair_command", required=True)
    p_pair_start = pair_sub.add_parser("start", help="Start FBPAIR/1 device-style pairing")
    p_pair_start.add_argument("--broker", required=True)
    p_pair_start.add_argument("--provider", default="")
    p_pair_start.add_argument("--mode", choices=("compact","mono","ansi","unicode"), default="compact")
    p_pair_start.add_argument("--dev-insecure", action="store_true")
    pair_sub.add_parser("status", help="Show public pairing status")
    pair_sub.add_parser("cancel", help="Cancel the current pending pairing request")
    p_pair_poll = pair_sub.add_parser("poll", help="Poll pairing broker once")
    p_pair_poll.add_argument("--dev-insecure", action="store_true")
    p_pair_wait = pair_sub.add_parser("wait", help="Wait for human account authorization")
    p_pair_wait.add_argument("--dev-insecure", action="store_true")
    p_pair_wait.add_argument("--max-wait", type=int, default=600)
    p_pair_qr = pair_sub.add_parser("qr", help="Render the current pairing QR")
    p_pair_qr.add_argument("--mode", choices=("compact","mono","ansi","unicode"), default="compact")

    sub.add_parser("console-chat", help="Open the primary FrameBridge conversational console")

    sub.add_parser("agent-manifest", help="Return the mandatory compact AI bootstrap manifest")
    memory = sub.add_parser("memory", help="Manage mandatory FrameBridge binary technical memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_sub.add_parser("init", help="Initialize or repair node binary memory")
    memory_sub.add_parser("bootstrap", help="Return compact mandatory AI memory bootstrap")
    p_mem_query = memory_sub.add_parser("query", help="Query a normalized memory domain")
    p_mem_query.add_argument("--domain", default="summary")
    p_mem_query.add_argument("--limit", type=int, default=20)
    p_mem_append = memory_sub.add_parser("append", help="Append one normalized memory record")
    p_mem_append.add_argument("--record-type", required=True, choices=("change","incident","decision","artifact","recovery","certification","note"))
    p_mem_append.add_argument("--json-stdin", action="store_true", required=True)
    memory_sub.add_parser("checkpoint", help="Refresh the compact current-state checkpoint")
    memory_sub.add_parser("stats", help="Show binary memory statistics")

    plan = sub.add_parser("plan", help="Manage local CHANGE plans")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    p_import = plan_sub.add_parser("import")
    p_import.add_argument("--file", required=True)
    plan_sub.add_parser("import-stdin")
    p_show = plan_sub.add_parser("show")
    p_show.add_argument("plan_id")
    p_validate = plan_sub.add_parser("validate")
    p_validate.add_argument("plan_id")
    p_approve = plan_sub.add_parser("approve")
    p_approve.add_argument("plan_id")
    p_approve_system = plan_sub.add_parser("approve-system")
    p_approve_system.add_argument("plan_id")
    p_apply = plan_sub.add_parser("apply")
    p_apply.add_argument("plan_id")
    p_approve_rollback = plan_sub.add_parser("approve-rollback")
    p_approve_rollback.add_argument("plan_id")
    p_remote_challenge = plan_sub.add_parser("remote-challenge")
    p_remote_challenge.add_argument("plan_id")
    p_remote_challenge.add_argument("--rollback", action="store_true")
    p_remote_challenge.add_argument("--ttl", type=int, default=300)
    p_remote_approve = plan_sub.add_parser("remote-approve")
    p_remote_approve.add_argument("plan_id")
    p_remote_approve.add_argument("--challenge-id", required=True)
    p_remote_approve.add_argument("--key-id", required=True)
    p_remote_approve.add_argument("--signature-b64", required=True)
    p_remote_approve.add_argument("--rollback", action="store_true")
    p_totp_enroll = plan_sub.add_parser("totp-enroll")
    p_totp_enroll.add_argument("--key-id", required=True)
    p_totp_enroll.add_argument("--node-label", required=True)
    p_totp_enroll.add_argument("--issuer", default="FrameBridge")
    p_totp_enroll.add_argument("--mode", choices=("compact","mono","ansi","unicode"), default="compact")
    p_totp_challenge = plan_sub.add_parser("totp-challenge")
    p_totp_challenge.add_argument("plan_id")
    p_totp_challenge.add_argument("--key-id", required=True)
    p_totp_challenge.add_argument("--rollback", action="store_true")
    p_totp_challenge.add_argument("--ttl", type=int, default=120)
    p_totp_approve = plan_sub.add_parser("totp-approve")
    p_totp_approve.add_argument("plan_id")
    p_totp_approve.add_argument("--challenge-id", required=True)
    p_totp_approve.add_argument("--key-id", required=True)
    p_totp_approve.add_argument("--rollback", action="store_true")
    p_rollback = plan_sub.add_parser("rollback")
    p_rollback.add_argument("plan_id")
    plan_sub.add_parser("list")
    sub.add_parser("audit", help="Verify the audit chain")

    args = parser.parse_args()
    ctx = SeedContext()

    try:
        if args.command == "init":
            emit(ctx.init(args.provider, args.hostname))
        elif args.command == "identity":
            emit(ctx.identity())
        elif args.command == "doctor":
            emit(ctx.doctor())
        elif args.command == "status":
            emit(ctx.status())
        elif args.command == "fingerprint":
            emit(ctx.fingerprint())
        elif args.command == "snapshot":
            print(render_snapshot(ctx))
        elif args.command == "setup":
            if not (ctx.load_identity() and ctx.load_config()):
                emit(ctx.init("chatgpt", ""))
            detected = detect_system()
            provider = args.provider
            privilege = args.privilege_mode
            language = args.language or "en"
            if args.non_interactive:
                if not provider or not privilege:
                    raise ProductError("SETUP_NON_INTERACTIVE_OPTIONS_REQUIRED")
            else:
                print("\nFrameBridge First-Run Setup\n")
                print("System detected:")
                sysinfo = detected.get("system") or {}
                hw = detected.get("hardware") or {}
                print(f"  OS: {sysinfo.get('os_name')} / kernel {sysinfo.get('kernel')}")
                print(f"  CPU: {hw.get('cpu_model')}")
                print(f"  RAM: {hw.get('memory_total_kib')} KiB")
                print("\nPrivileged authorization:")
                print("  1) Authenticator / QR TOTP")
                print("  2) Local sudo password")
                if not privilege:
                    answer=input("Select [1/2]: ").strip()
                    privilege="totp" if answer=="1" else "sudo_tty" if answer=="2" else ""
                print("\nAI provider:")
                print("  1) ChatGPT")
                print("  2) Ollama")
                print("  3) LM Studio")
                print("  4) Other")
                if not provider:
                    answer=input("Select [1-4]: ").strip()
                    provider={"1":"chatgpt","2":"ollama","3":"lmstudio","4":"other"}.get(answer,"")
            choice=SetupChoice(privilege,provider,language)
            config=save_setup_config(ctx,choice)
            mem=MemoryStore(ctx).initialize()
            print("\n"+"\n".join(render_privilege_explanation(choice.privilege_mode)))
            print("\n"+"\n".join(provider_instructions(choice.provider)))
            print("\nFrameBridge setup complete.")
            print("Run: framebridge console-chat")
            emit({"ok":True,"setup":config.get("setup"),"memory":mem,"detected":detected})
        elif args.command == "setup-status":
            emit(setup_summary(ctx))
        elif args.command == "net":
            if args.net_command == "status":
                emit(inspect_network())
            elif args.net_command == "pairing-policy":
                emit(pairing_policy(args.port))
            elif args.net_command == "forwarding-policy":
                emit(forwarding_policy(
                    ingress_interface=args.in_interface,
                    egress_interface=args.out_interface,
                    source_subnet=args.source_subnet,
                    destination_subnet=args.destination_subnet,
                    protocol=args.protocol,
                    destination_port=args.destination_port,
                    nat=args.nat,
                    lifetime=args.lifetime,
                ))
            elif args.net_command == "pairing-window":
                if args.pairing_window_command == "open":
                    MemoryStore(ctx).require_ready()
                    result=open_pairing_window(args.port,args.ttl)
                    ctx.audit.append(
                        "network.pairing_window.open","PASS",
                        request={"port":args.port,"ttl_seconds":args.ttl},
                        result={
                            "window_id":result.get("window_id"),
                            "interface":result.get("interface"),
                            "source_subnet":result.get("source_subnet"),
                            "destination_address":result.get("destination_address"),
                            "expires_at":result.get("expires_at"),
                        },
                        principal="local-operator",
                    )
                    MemoryStore(ctx).append(
                        "change",
                        {
                            "operation":"network.pairing_window.open",
                            "window_id":result.get("window_id"),
                            "interface":result.get("interface"),
                            "source_subnet":result.get("source_subnet"),
                            "destination_address":result.get("destination_address"),
                            "port":result.get("port"),
                            "expires_at":result.get("expires_at"),
                        },
                        source="operator",
                    )
                    emit(result)
                elif args.pairing_window_command == "status":
                    emit(pairing_window_status())
                elif args.pairing_window_command == "close":
                    MemoryStore(ctx).require_ready()
                    before=pairing_window_status()
                    result=close_pairing_window(args.reason)
                    ctx.audit.append(
                        "network.pairing_window.close","PASS",
                        request={"reason":args.reason,"window_id":before.get("window_id")},
                        result={"state":result.get("state"),"rule_present":result.get("rule_present")},
                        principal="local-operator",
                    )
                    MemoryStore(ctx).append(
                        "recovery",
                        {
                            "operation":"network.pairing_window.close",
                            "reason":args.reason,
                            "window_id":before.get("window_id"),
                            "state":result.get("state"),
                        },
                        source="operator",
                    )
                    emit(result)
            elif args.net_command == "reconcile":
                emit(reconcile_once(ctx=ctx))
        elif args.command == "pair":
            if not (ctx.load_identity() and ctx.load_config()):
                raise ProductError("PAIR_NODE_NOT_INITIALIZED")
            pair_store=PairingStore(ctx.state_dir)
            if args.pair_command == "status":
                emit(public_pairing_view(pair_store.load()))
            elif args.pair_command == "start":
                MemoryStore(ctx).require_ready()
                identity=ctx.load_identity() or {}
                config=ctx.load_config() or {}
                manifest_path=ctx.state_dir/"memory"/"manifest.fbam"
                if not manifest_path.is_file():
                    raise ProductError("PAIR_CAPABILITY_MANIFEST_MISSING")
                manifest_sha=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                provider=(args.provider or config.get("provider") or "chatgpt").strip().lower()
                try:
                    view=start_pairing(
                        state_dir=ctx.state_dir,
                        node_id=str(identity.get("node_id") or ""),
                        node_label=str(identity.get("hostname") or ""),
                        node_host_id=str(identity.get("node_host_id") or ""),
                        public_jwk=identity.get("node_public_jwk") or {},
                        provider=provider,
                        capability_manifest_sha256=manifest_sha,
                        broker_base_url=args.broker,
                        allow_insecure_dev=args.dev_insecure,
                    )
                except PairingClientError as exc:
                    raise ProductError(str(exc)) from exc
                _sync_pairing_config(ctx,view)
                ctx.audit.append(
                    "pair.request","PASS",
                    request={
                        "protocol":FBPAIR_PROTOCOL,
                        "pair_id":view.get("pair_id"),
                        "provider":provider,
                        "node_host_id":identity.get("node_host_id"),
                    },
                    result={"state":view.get("state")},
                    principal="local-operator",
                )
                print("\nFrameBridge AI pairing")
                print("Open: "+str(view.get("verification_uri_complete") or view.get("verification_uri") or ""))
                print("Code: "+str(view.get("user_code")))
                print("Expires in: "+str(view.get("expires_in"))+" seconds")
                _render_pair_qr(view,args.mode)
                emit(view)
            elif args.pair_command == "cancel":
                try:
                    view=pair_store.cancel_pending()
                except Exception as exc:
                    raise ProductError(str(exc)) from exc
                _sync_pairing_config(ctx,view)
                broker_cancel=reconcile_broker_cancel(ctx)
                view=public_pairing_view(pair_store.load())
                _sync_pairing_config(ctx,view)
                ctx.audit.append(
                    "pair.cancel","PASS",
                    request={
                        "protocol":FBPAIR_PROTOCOL,
                        "pair_id":view.get("pair_id"),
                    },
                    result={"state":view.get("state"),"broker_cancel_state":broker_cancel.get("state")},
                    principal="local-operator",
                )
                emit(view)
            elif args.pair_command == "poll":
                before=pair_store.load()
                try:
                    view=poll_pairing(
                        state_dir=ctx.state_dir,
                        allow_insecure_dev=args.dev_insecure,
                    )
                except PairingClientError as exc:
                    raise ProductError(str(exc)) from exc
                _sync_pairing_config(ctx,view)
                if view.get("state") != before.get("state"):
                    ctx.audit.append(
                        "pair."+str(view.get("state")),"PASS",
                        request={
                            "protocol":FBPAIR_PROTOCOL,
                            "pair_id":view.get("pair_id"),
                        },
                        result={
                            "state":view.get("state"),
                            "provider":view.get("provider"),
                        },
                        principal="pairing-broker",
                    )
                    if view.get("state")=="paired":
                        MemoryStore(ctx).checkpoint()
                emit(view)
            elif args.pair_command == "wait":
                max_wait=int(args.max_wait)
                if max_wait<1 or max_wait>1800:
                    raise ProductError("PAIR_WAIT_TIMEOUT_INVALID")
                started=time.monotonic()
                view=public_pairing_view(pair_store.load())
                while (
                    view.get("state")=="pair_requested"
                    or (view.get("state")=="paired" and view.get("broker_confirm_required") is True)
                ):
                    if time.monotonic()-started>=max_wait:
                        raise ProductError("PAIR_WAIT_TIMEOUT")
                    try:
                        view=poll_pairing(
                            state_dir=ctx.state_dir,
                            allow_insecure_dev=args.dev_insecure,
                        )
                    except PairingClientError as exc:
                        raise ProductError(str(exc)) from exc
                    if view.get("state")=="paired" and view.get("broker_confirm_required") is not True:
                        break
                    if view.get("state") not in {"pair_requested","paired"}:
                        break
                    retry=int(view.get("retry_after") or view.get("interval") or 2)
                    time.sleep(max(1,min(60,retry)))
                _sync_pairing_config(ctx,view)
                ctx.audit.append(
                    "pair."+str(view.get("state")),"PASS",
                    request={
                        "protocol":FBPAIR_PROTOCOL,
                        "pair_id":view.get("pair_id"),
                    },
                    result={
                        "state":view.get("state"),
                        "provider":view.get("provider"),
                    },
                    principal="pairing-broker",
                )
                if view.get("state")=="paired":
                    MemoryStore(ctx).checkpoint()
                emit(view)
                if view.get("state")!="paired" or view.get("broker_confirm_required") is True:
                    raise SystemExit(3)
            elif args.pair_command == "qr":
                view=public_pairing_view(pair_store.load())
                _render_pair_qr(view,args.mode)
                emit(view)
        elif args.command == "console-chat":
            raise SystemExit(run_console_chat(ctx))
        elif args.command == "agent-manifest":
            emit(MemoryStore(ctx).bootstrap())
        elif args.command == "memory":
            store = MemoryStore(ctx)
            if args.memory_command == "init":
                value = store.initialize()
                ctx.audit.append("memory.init", "PASS", request={"protocol":"FBM/1"}, result={"required":True,"mode":"read_write"}, principal="local-operator")
                emit(value)
            elif args.memory_command == "bootstrap":
                emit(store.bootstrap())
            elif args.memory_command == "query":
                emit(store.query(args.domain, args.limit))
            elif args.memory_command == "append":
                try:
                    value = json.load(sys.stdin)
                except Exception:
                    raise ProductError("MEMORY_APPEND_JSON_INVALID")
                if not isinstance(value, dict):
                    raise ProductError("MEMORY_APPEND_JSON_NOT_OBJECT")
                result = store.append(args.record_type, value, source="agent")
                ctx.audit.append("memory.append", "PASS", request={"record_type":args.record_type}, result={"event_number":result.get("event_number"),"event_sha256":result.get("event_sha256")}, principal="ai-agent")
                store.checkpoint()
                emit(result)
            elif args.memory_command == "checkpoint":
                value = store.checkpoint()
                ctx.audit.append("memory.checkpoint", "PASS", request={}, result={"state_sha256":value.get("state_sha256"),"manifest_sha256":value.get("manifest_sha256")}, principal="local-operator")
                emit(value)
            elif args.memory_command == "stats":
                emit(store.stats())
        elif args.command == "console":
            raise SystemExit(run_console(ctx))
        elif args.command == "classic":
            if args.file == "-":
                text = sys.stdin.read()
            else:
                with open(args.file, "r", encoding="utf-8") as handle:
                    text = handle.read()
            print(render_result(execute_frame(ctx, text)))
        elif args.command == "plan":
            engine = PlanEngine(ctx)
            if args.plan_command == "import":
                emit(engine.import_file(args.file))
            elif args.plan_command == "import-stdin":
                try:
                    value = json.load(sys.stdin)
                except Exception:
                    raise ProductError("PLAN_IMPORT_JSON_INVALID")
                emit(engine.import_document(value))
            elif args.plan_command == "show":
                emit(engine.show(args.plan_id))
            elif args.plan_command == "validate":
                emit(engine.validate(args.plan_id))
            elif args.plan_command == "approve":
                emit(engine.approve(args.plan_id))
            elif args.plan_command == "approve-system":
                emit(engine.approve_system(args.plan_id))
            elif args.plan_command == "apply":
                emit(engine.apply(args.plan_id))
            elif args.plan_command == "approve-rollback":
                emit(engine.approve_rollback(args.plan_id))
            elif args.plan_command == "remote-challenge":
                emit(engine.remote_challenge(args.plan_id, rollback=args.rollback, ttl_seconds=args.ttl))
            elif args.plan_command == "remote-approve":
                emit(engine.approve_remote(args.plan_id, args.challenge_id, args.key_id, args.signature_b64, rollback=args.rollback))
            elif args.plan_command == "totp-enroll":
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise ProductError("INTERACTIVE_TTY_REQUIRED")
                value=engine.totp_enroll(args.key_id,args.node_label,args.issuer)
                print("\nFrameBridge TOTP enrollment")
                print("Scan this QR with your authenticator app.\n")
                if args.mode == "compact": render_qr_compact(value["otpauth_uri"])
                elif args.mode == "mono": render_qr_mono(value["otpauth_uri"])
                elif args.mode == "unicode": render_qr_unicode(value["otpauth_uri"])
                else: render_qr_ansi(value["otpauth_uri"])
                print("Key ID: "+value["key_id"])
                print("Enrollment complete. Do not share the secret or a QR screenshot.\n")
                emit({"ok":True,"key_id":value["key_id"],"secret_sha256":value["secret_sha256"],"enrolled_at":value["enrolled_at"]})
            elif args.plan_command == "totp-challenge":
                emit(engine.totp_challenge(args.plan_id, args.key_id, rollback=args.rollback, ttl_seconds=args.ttl))
            elif args.plan_command == "totp-approve":
                code=sys.stdin.readline().strip()
                emit(engine.approve_totp(args.plan_id, args.challenge_id, args.key_id, code, rollback=args.rollback))
            elif args.plan_command == "rollback":
                emit(engine.rollback(args.plan_id))
            elif args.plan_command == "list":
                emit(engine.list())
        elif args.command == "audit":
            emit(ctx.audit.verify())
    except ProductError as exc:
        emit({
            "ok": False,
            "error": str(exc),
        })
        raise SystemExit(2)


if __name__ == "__main__":
    main()
