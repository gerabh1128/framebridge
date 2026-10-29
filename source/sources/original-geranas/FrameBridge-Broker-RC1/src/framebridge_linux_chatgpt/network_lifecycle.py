from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .memory_store import MemoryStore
from .product_core import ProductError
from .network_security import inspect_network
from .pairing import PairingError, PairingStore, public_pairing_view
from .pairing_client import PairingClientError, cancel_pairing_remote
from .pairing_window import close_pairing_window, pairing_window_status
from .seed_core import SeedContext, atomic_write_json


MONITOR_SCHEMA = 1
DEFAULT_INTERVAL_SECONDS = 1.0


def _candidate_binding(candidate: dict[str, Any] | None) -> dict[str, Any]:
    c=candidate or {}
    return {
        "interface":str(c.get("interface") or ""),
        "source_subnet":str(c.get("subnet") or ""),
        "destination_address":str(c.get("address") or ""),
        "network_fingerprint":str(c.get("network_fingerprint") or ""),
    }


def _window_binding(window: dict[str, Any]) -> dict[str, Any]:
    return {
        "interface":str(window.get("interface") or ""),
        "source_subnet":str(window.get("source_subnet") or ""),
        "destination_address":str(window.get("destination_address") or ""),
        "network_fingerprint":str(window.get("network_fingerprint") or ""),
    }


def compare_binding(window: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    old=_window_binding(window)
    new=_candidate_binding(candidate)
    reasons=[]
    if not candidate:
        reasons.append("NO_ELIGIBLE_PAIRING_CANDIDATE")
    else:
        for key,label in (
            ("interface","INTERFACE_CHANGED"),
            ("source_subnet","SUBNET_CHANGED"),
            ("destination_address","ADDRESS_CHANGED"),
            ("network_fingerprint","NETWORK_FINGERPRINT_CHANGED"),
        ):
            if old.get(key)!=new.get(key):
                reasons.append(label)
    return {
        "changed":bool(reasons),
        "reasons":reasons,
        "old":old,
        "new":new,
    }


def _sync_pairing_config(ctx: SeedContext, view: dict[str, Any]) -> None:
    config=ctx.load_config() or {}
    pairing=config.get("pairing") or {}
    pairing["state"]=view.get("state","expired")
    if view.get("expiration_reason"):
        pairing["expiration_reason"]=view.get("expiration_reason")
    pairing["requires_new_local_action"]=True
    config["pairing"]=pairing
    config["connection_mode"]="PAIRING"
    setup=config.get("setup")
    if isinstance(setup,dict):
        setup["pairing_state"]=view.get("state","expired")
    atomic_write_json(ctx.config_path,config)


def _invalidate_pending_pair(ctx: SeedContext, reason: str) -> dict[str, Any] | None:
    store=PairingStore(ctx.state_dir)
    current=store.load()
    if current.get("state")!="pair_requested":
        return None
    view=store.invalidate_pending(reason)
    _sync_pairing_config(ctx,view)
    return view


def reconcile_broker_cancel(
    ctx: SeedContext,
    *,
    cancel_func=cancel_pairing_remote,
    retry_seconds: float=5.0,
) -> dict[str, Any]:
    store=PairingStore(ctx.state_dir)
    current=store.load()
    if current.get("broker_cancel_required") is not True:
        return {"ok":True,"state":"NOT_REQUIRED","cancelled":False}
    pair_id=str(current.get("pair_id") or "")
    reason=str(current.get("broker_cancel_reason") or current.get("expiration_reason") or "reconcile")
    last=str(current.get("broker_cancel_last_attempt_at") or "")
    if last:
        try:
            last_dt=datetime.fromisoformat(last.replace("Z","+00:00")).astimezone(timezone.utc)
            age=(datetime.now(timezone.utc)-last_dt).total_seconds()
            if age<max(1.0,float(retry_seconds)):
                return {
                    "ok":True,"state":"RETRY_DEFERRED","cancelled":False,
                    "pair_id":pair_id,"retry_after":round(max(0.0,float(retry_seconds)-age),3),
                }
        except Exception:
            pass
    try:
        result=cancel_func(state_dir=ctx.state_dir,reason=reason,record=current)
    except PairingClientError as exc:
        store.note_broker_cancel_error(pair_id,str(exc))
        return {
            "ok":False,"state":"BROKER_CANCEL_PENDING","cancelled":False,
            "pair_id":pair_id,"error":str(exc),
        }
    updated=store.mark_broker_cancelled(pair_id,result)
    ctx.audit.append(
        "pair.broker_cancelled","PASS",
        request={"pair_id":pair_id,"reason":reason},
        result={"broker_state":result.get("state"),"idempotent":result.get("idempotent",False)},
        principal="network-monitor",
    )
    memory=MemoryStore(ctx)
    if memory.ready():
        memory.append(
            "recovery",
            {"operation":"pair.broker_cancelled","pair_id":pair_id,"reason":reason,"broker_state":result.get("state")},
            source="system",
        )
    return {
        "ok":True,"state":"BROKER_CANCELLED","cancelled":True,
        "pair_id":pair_id,"broker_state":result.get("state"),
        "local_state":updated.get("state"),
    }


def _lifecycle_state_path(ctx: SeedContext):
    return ctx.state_dir/"network-lifecycle.json"


def _sync_expired_window(ctx: SeedContext, window: dict[str, Any]) -> dict[str, Any]:
    window_id=str(window.get("window_id") or "")
    if window.get("state")=="expired" and window.get("rule_present") is not False:
        raise ProductError("NETWORK_WINDOW_EXPIRY_UNVERIFIED")
    if not window_id or window.get("state")!="expired" or window.get("close_reason")!="expired":
        return {"ok":True,"synced":False}
    path=_lifecycle_state_path(ctx)
    prior={}
    if path.is_file():
        try:
            prior=json.loads(path.read_text())
        except Exception:
            prior={}
    if prior.get("last_expiry_window_id")==window_id:
        return {"ok":True,"synced":False,"already_synced":True,"window_id":window_id}
    pair_view=_invalidate_pending_pair(ctx,"window_expired")
    broker_cancel=reconcile_broker_cancel(ctx)
    ctx.audit.append(
        "network.pairing_window.expired","PASS",
        request={"window_id":window_id,"expires_at":window.get("expires_at")},
        result={
            "rule_present":window.get("rule_present"),
            "pair_state":(pair_view or {}).get("state"),
            "requires_new_local_action":bool(pair_view),
            "broker_cancel_state":broker_cancel.get("state"),
        },
        principal="network-monitor",
    )
    memory=MemoryStore(ctx)
    if memory.ready():
        memory.append(
            "recovery",
            {
                "operation":"network.pairing_window.expired",
                "window_id":window_id,
                "rule_present":window.get("rule_present"),
                "pair_state":(pair_view or {}).get("state"),
                "requires_new_local_action":bool(pair_view),
                "broker_cancel_state":broker_cancel.get("state"),
            },source="system",
        )
    atomic_write_json(path,{"schema":MONITOR_SCHEMA,"last_expiry_window_id":window_id,"last_expiry_synced_at":time.time()})
    return {
        "ok":True,"synced":True,"window_id":window_id,
        "pair_state":(pair_view or {}).get("state"),
        "requires_new_local_action":bool(pair_view),
        "broker_cancel":broker_cancel,
    }



def _record_invalidation(
    ctx: SeedContext,
    *,
    reason: str,
    comparison: dict[str, Any],
    window: dict[str, Any],
    closed: dict[str, Any],
    pair_view: dict[str, Any] | None,
) -> None:
    result={
        "window_id":window.get("window_id"),
        "close_reason":closed.get("close_reason"),
        "rule_present":closed.get("rule_present"),
        "pair_state":(pair_view or {}).get("state"),
        "requires_new_local_action":True,
        "reasons":comparison.get("reasons") or [reason],
    }
    ctx.audit.append(
        "network.pairing_window.invalidated",
        "PASS",
        request={
            "reason":reason,
            "old":comparison.get("old"),
            "new":comparison.get("new"),
        },
        result=result,
        principal="network-monitor",
    )
    store=MemoryStore(ctx)
    if store.ready():
        store.append(
            "incident",
            {
                "operation":"network.pairing_window.invalidated",
                "reason":reason,
                "reasons":comparison.get("reasons") or [reason],
                "window_id":window.get("window_id"),
                "old":comparison.get("old"),
                "new":comparison.get("new"),
                "pair_state":(pair_view or {}).get("state"),
                "requires_new_local_action":True,
            },
            source="system",
        )


def invalidate_active_window(
    *,
    ctx: SeedContext | None=None,
    reason: str,
    comparison: dict[str, Any] | None=None,
    status_func: Callable[..., dict[str, Any]]=pairing_window_status,
    close_func: Callable[..., dict[str, Any]]=close_pairing_window,
) -> dict[str, Any]:
    ctx=ctx or SeedContext()
    window=status_func(non_interactive=True)
    if window.get("active") is not True:
        return {"ok":True,"schema":MONITOR_SCHEMA,"state":"NO_ACTIVE_PAIRING_WINDOW","invalidated":False}
    if comparison is None:
        comparison={"changed":True,"reasons":[reason.upper()],"old":_window_binding(window),"new":{}}
    closed=close_func(reason,non_interactive=True)
    if closed.get("ok") is not True or closed.get("rule_present") is not False or closed.get("active") is True:
        raise ProductError("NETWORK_WINDOW_CLOSE_UNVERIFIED")
    pair_view=_invalidate_pending_pair(ctx,reason)
    broker_cancel=reconcile_broker_cancel(ctx)
    _record_invalidation(
        ctx,reason=reason,comparison=comparison,window=window,closed=closed,pair_view=pair_view,
    )
    return {
        "ok":True,"schema":MONITOR_SCHEMA,"state":"INVALIDATED","invalidated":True,
        "reason":reason,"reasons":comparison.get("reasons") or [],
        "window_id":window.get("window_id"),"rule_present":closed.get("rule_present"),
        "pair_state":(pair_view or {}).get("state"),"requires_new_local_action":True,
        "broker_cancel":broker_cancel,"old":comparison.get("old"),"new":comparison.get("new"),
    }



def reconcile_once(
    *,
    ctx: SeedContext | None=None,
    snapshot: dict[str, Any] | None=None,
    status_func: Callable[..., dict[str, Any]]=pairing_window_status,
    close_func: Callable[..., dict[str, Any]]=close_pairing_window,
) -> dict[str, Any]:
    ctx=ctx or SeedContext()
    broker_cancel=reconcile_broker_cancel(ctx)
    window=status_func(non_interactive=True)
    if window.get("active") is not True:
        expiry_sync=_sync_expired_window(ctx,window)
        return {
            "ok":True,"schema":MONITOR_SCHEMA,
            "state":"PAIRING_WINDOW_EXPIRED_SYNCED" if expiry_sync.get("synced") else "NO_ACTIVE_PAIRING_WINDOW",
            "invalidated":False,"expiry_sync":expiry_sync,"broker_cancel":broker_cancel,
        }
    try:
        snap=snapshot if snapshot is not None else inspect_network()
        if not isinstance(snap,dict): raise ValueError("NETWORK_SNAPSHOT_NOT_OBJECT")
    except Exception:
        return invalidate_active_window(
            ctx=ctx,reason="reconcile",
            comparison={"changed":True,"reasons":["NETWORK_INSPECTION_FAILED"],"old":_window_binding(window),"new":{}},
            status_func=lambda **_:window,close_func=close_func,
        )
    candidate=snap.get("pairing_candidate")
    comparison=compare_binding(window,candidate)
    if not comparison["changed"]:
        return {
            "ok":True,"schema":MONITOR_SCHEMA,"state":"STABLE","invalidated":False,
            "window_id":window.get("window_id"),"binding":comparison["old"],"broker_cancel":broker_cancel,
        }
    return invalidate_active_window(
        ctx=ctx,reason="network_change",comparison=comparison,
        status_func=lambda **_:window,close_func=close_func,
    )



def run_forever(interval: float=DEFAULT_INTERVAL_SECONDS) -> None:
    interval=max(0.25,float(interval))
    ctx=SeedContext()
    while True:
        try:
            result=reconcile_once(ctx=ctx)
            if result.get("invalidated") is True:
                print(json.dumps(result,sort_keys=True),flush=True)
        except Exception as exc:
            print(json.dumps({
                "ok":False,
                "schema":MONITOR_SCHEMA,
                "state":"MONITOR_ERROR",
                "error":type(exc).__name__,
                "message":str(exc),
            },sort_keys=True),file=sys.stderr,flush=True)
        time.sleep(interval)


def main() -> None:
    parser=argparse.ArgumentParser(prog="framebridge-network-monitor")
    sub=parser.add_subparsers(dest="command",required=True)
    run=sub.add_parser("run")
    run.add_argument("--interval",type=float,default=DEFAULT_INTERVAL_SECONDS)
    sub.add_parser("once")
    close=sub.add_parser("close")
    close.add_argument("--reason",choices=("service_stop","reconcile"),default="service_stop")
    args=parser.parse_args()
    if args.command=="run":
        run_forever(args.interval)
    elif args.command=="once":
        print(json.dumps(reconcile_once(),sort_keys=True,indent=2))
    else:
        print(json.dumps(invalidate_active_window(reason=args.reason),sort_keys=True,indent=2))


if __name__=="__main__":
    main()
