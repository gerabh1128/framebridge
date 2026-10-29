from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .node_identity import ensure_node_keypair, sign_pairing_payload
from .pairing import PairingStore, canonical
from .pairing_client import PairingClientError, confirm_pairing_remote
from .product_core import ProductError
from .seed_capabilities import execute_capability, list_capabilities
from .seed_core import SeedContext, atomic_write_json
from .plan_engine import PlanEngine
from .memory_store import MemoryStore

READ_CAPS={x["name"] for x in list_capabilities() if x.get("risk")=="READONLY"}


class OutboundAgentError(RuntimeError):
    pass


def _post(url: str, value: dict[str,Any], timeout: float=10.0) -> dict[str,Any]:
    req=urllib.request.Request(
        url,
        data=json.dumps(value,sort_keys=True,separators=(",",":")).encode("utf-8"),
        headers={"Content-Type":"application/json","Accept":"application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            out=json.loads(resp.read(1024*1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail=""
        try:
            body=json.loads(exc.read(1024*1024).decode("utf-8"))
            if isinstance(body,dict):
                detail=str(body.get("error") or "")
        except Exception:
            detail=""
        if detail=="node_revoked":
            raise OutboundAgentError("OUTBOUND_NODE_REVOKED") from exc
        raise OutboundAgentError("OUTBOUND_BROKER_REJECTED") from exc
    except Exception as exc:
        raise OutboundAgentError("OUTBOUND_BROKER_UNREACHABLE") from exc
    if not isinstance(out,dict):
        raise OutboundAgentError("OUTBOUND_BROKER_RESPONSE_INVALID")
    return out


def _signed_envelope(ctx: SeedContext, kind: str, payload: dict[str,Any]) -> dict[str,Any]:
    identity=ctx.load_identity() or {}
    node_id=str(identity.get("node_id") or "")
    host_id=str(identity.get("node_host_id") or "")
    if not node_id or not host_id:
        raise OutboundAgentError("OUTBOUND_NODE_IDENTITY_MISSING")
    crypto=ensure_node_keypair(ctx.state_dir,comment="framebridge-"+node_id.replace("-","")[:8])
    base={
        "schema":1,
        "kind":str(kind),
        "node_id":node_id,
        "node_host_id":host_id,
        "timestamp":time.time(),
        "nonce":secrets.token_hex(16),
        "payload":payload,
    }
    sig=sign_pairing_payload(crypto["private_key_path"],canonical(base))
    return {
        **base,
        "node_proof":{
            "format":"sshsig",
            "namespace":"framebridge-pairing",
            "signature_b64":sig,
        },
    }


def broker_base(ctx: SeedContext) -> str:
    config=ctx.load_config() or {}
    pairing=config.get("pairing") or {}
    base=str(pairing.get("broker_base_url") or "").rstrip("/")
    if not base:
        raise OutboundAgentError("OUTBOUND_BROKER_NOT_CONFIGURED")
    return base


def sync_local_revocation(ctx: SeedContext) -> dict[str,Any]:
    store=PairingStore(ctx.state_dir)
    current=store.load()
    if current.get("state")=="paired":
        view=store.revoke()
    elif current.get("state")=="revoked":
        view=current
    else:
        raise OutboundAgentError("OUTBOUND_LOCAL_PAIRING_STATE_INVALID")

    config=ctx.load_config() or {}
    pairing=config.get("pairing") or {}
    pairing["state"]="revoked"
    pairing["transport"]="broker_outbound"
    if view.get("revoked_at"):
        pairing["revoked_at"]=view.get("revoked_at")
    config["pairing"]=pairing
    config["connection_mode"]="PAIRING"
    setup=config.get("setup")
    if isinstance(setup,dict):
        setup["pairing_state"]="revoked"
    atomic_write_json(ctx.config_path,config)

    ctx.audit.append(
        "pair.revoked",
        "PASS",
        request={"source":"broker"},
        result={"state":"revoked"},
        principal="pairing-broker",
    )
    MemoryStore(ctx).append(
        "recovery",
        {"event":"pair.revoked","state":"revoked","source":"broker"},
        source="system",
    )
    return {"ok":True,"state":"revoked"}


def execute_plan_operation(ctx: SeedContext, operation: str, payload: dict[str,Any]) -> dict[str,Any]:
    if operation not in {"plan.import","plan.status","plan.apply","plan.rollback"}:
        raise OutboundAgentError("OUTBOUND_PLAN_OPERATION_NOT_ALLOWED")
    if not isinstance(payload,dict):
        raise OutboundAgentError("OUTBOUND_PLAN_PAYLOAD_INVALID")
    engine=PlanEngine(ctx)

    if operation=="plan.import":
        plan=payload.get("plan")
        if not isinstance(plan,dict):
            raise OutboundAgentError("OUTBOUND_PLAN_DOCUMENT_INVALID")
        return engine.import_document(plan)

    plan_id=str(payload.get("plan_id") or "")
    if not plan_id:
        raise OutboundAgentError("OUTBOUND_PLAN_ID_MISSING")

    if operation=="plan.status":
        return engine.show(plan_id)

    expected=str(payload.get("plan_sha256") or "")
    shown=engine.show(plan_id)
    if shown.get("plan_sha256")!=expected:
        raise OutboundAgentError("OUTBOUND_PLAN_SHA_MISMATCH")

    if operation=="plan.apply":
        return engine.apply(plan_id)
    return engine.rollback(plan_id)


def run_once(ctx: SeedContext | None=None) -> dict[str,Any]:
    ctx=ctx or SeedContext()
    store=PairingStore(ctx.state_dir)
    local_pair=store.load()
    if local_pair.get("state")=="paired" and local_pair.get("broker_confirm_required") is True:
        try:
            confirm_pairing_remote(state_dir=ctx.state_dir,record=local_pair)
        except PairingClientError as exc:
            raise OutboundAgentError("OUTBOUND_PAIR_CONFIRM_PENDING") from exc
        local_pair=store.load()
        config=ctx.load_config() or {}
        pairing_cfg=config.get("pairing") or {}
        pairing_cfg["broker_confirm_required"]=False
        pairing_cfg["broker_confirmed_at"]=local_pair.get("broker_confirmed_at")
        config["pairing"]=pairing_cfg
        atomic_write_json(ctx.config_path,config)
    config=ctx.load_config() or {}
    pairing=config.get("pairing") or {}
    if pairing.get("state")!="paired":
        raise OutboundAgentError("OUTBOUND_NODE_NOT_PAIRED")
    if store.load().get("broker_confirm_required") is True:
        raise OutboundAgentError("OUTBOUND_PAIR_CONFIRM_PENDING")
    base=broker_base(ctx)
    poll=_post(
        base+"/fbpair/v1/node/poll",
        _signed_envelope(ctx,"node.poll",{}),
    )
    if poll.get("ok") is not True:
        raise OutboundAgentError("OUTBOUND_POLL_REJECTED")
    req=poll.get("request")
    if not isinstance(req,dict):
        return {"ok":True,"idle":True}
    rid=str(req.get("request_id") or "")
    operation=str(req.get("operation") or "")
    if operation:
        payload=req.get("payload") or {}
        try:
            value=execute_plan_operation(ctx,operation,payload)
            result={"ok":True,"operation":operation,"result":value}
            ctx.audit.append(
                "outbound."+operation,
                "PASS",
                request={"request_id":rid,"operation":operation},
                result={"ok":True},
                principal="paired-ai",
            )
        except Exception as exc:
            result={"ok":False,"operation":operation,"error":str(exc)}
            ctx.audit.append(
                "outbound."+operation,
                "FAIL",
                request={"request_id":rid,"operation":operation},
                result={"ok":False,"error":type(exc).__name__},
                principal="paired-ai",
            )
        cap=operation
    else:
        cap=str(req.get("capability") or "")
        args=req.get("args") or {}
        if cap not in READ_CAPS:
            result={"ok":False,"error":"CAPABILITY_NOT_ALLOWED","capability":cap}
        elif not isinstance(args,dict):
            result={"ok":False,"error":"CAPABILITY_ARGS_NOT_OBJECT","capability":cap}
        else:
            try:
                value=execute_capability(cap,args)
                result={"ok":True,"capability":cap,"result":value}
                ctx.audit.append(
                    "outbound."+cap,
                    "PASS",
                    request={"request_id":rid,"capability":cap},
                    result={"ok":True},
                    principal="paired-ai",
                )
            except Exception as exc:
                result={"ok":False,"capability":cap,"error":str(exc)}
                ctx.audit.append(
                    "outbound."+cap,
                    "FAIL",
                    request={"request_id":rid,"capability":cap},
                    result={"ok":False,"error":type(exc).__name__},
                    principal="paired-ai",
                )
    response=_post(
        base+"/fbpair/v1/node/result",
        _signed_envelope(
            ctx,
            "node.result",
            {"request_id":rid,"result":result},
        ),
    )
    if response.get("ok") is not True:
        raise OutboundAgentError("OUTBOUND_RESULT_REJECTED")
    return {"ok":True,"idle":False,"request_id":rid,"capability":cap}


def run_forever(interval: float=1.0) -> None:
    interval=max(0.5,min(float(interval),30.0))
    ctx=SeedContext()
    while True:
        try:
            run_once(ctx)
        except OutboundAgentError as exc:
            if str(exc)=="OUTBOUND_NODE_REVOKED":
                try:
                    sync_local_revocation(ctx)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(interval)


def main() -> None:
    import argparse
    parser=argparse.ArgumentParser(prog="framebridge-outbound-agent")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("once")
    run=sub.add_parser("run")
    run.add_argument("--interval",type=float,default=1.0)
    args=parser.parse_args()
    if args.command=="once":
        print(json.dumps(run_once(),sort_keys=True))
    else:
        run_forever(args.interval)


if __name__=="__main__":
    main()
