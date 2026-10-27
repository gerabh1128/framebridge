from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import ssl
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .node_identity import ensure_node_keypair, sign_pairing_payload
from .pairing import (
    PairingError,
    PairingStore,
    attach_node_proof,
    create_pair_request,
    pairing_signing_payload,
    public_pairing_view,
    validate_device_authorization,
)


class PairingClientError(RuntimeError):
    pass


def _is_local_ip(host: str) -> bool:
    try:
        addr=ipaddress.ip_address(str(host))
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local:
        return True
    if addr.version==4:
        return any(addr in net for net in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ))
    return addr in ipaddress.ip_network("fc00::/7")


def _broker_base_ok(url: str, allow_insecure_dev: bool) -> bool:
    try:
        p=urllib.parse.urlparse(str(url))
    except Exception:
        return False
    if p.scheme=="https" and p.netloc:
        return True
    if p.scheme=="http" and p.netloc:
        host=str(p.hostname or "")
        if _is_local_ip(host):
            return True
        if allow_insecure_dev:
            return True
    return False


def _endpoint(base: str, path: str) -> str:
    return str(base).rstrip("/") + "/" + path.lstrip("/")


def post_json(
    url: str,
    value: dict[str, Any],
    *,
    timeout: float=10.0,
    opener: Callable[..., Any] | None=None,
) -> dict[str, Any]:
    data=json.dumps(
        value, sort_keys=True, separators=(",",":"), ensure_ascii=True
    ).encode("utf-8")
    req=urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type":"application/json",
            "Accept":"application/json",
            "User-Agent":"FrameBridge-Linux-Core/0.5",
        },
    )
    try:
        if opener is None:
            with urllib.request.urlopen(req,timeout=timeout) as resp:
                raw=resp.read(1024*1024)
        else:
            raw=opener(req,timeout=timeout)
            if hasattr(raw,"read"):
                raw=raw.read(1024*1024)
        result=json.loads(bytes(raw).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            raw=exc.read(1024*1024)
            result=json.loads(bytes(raw).decode("utf-8"))
        except Exception as parse_exc:
            raise PairingClientError("PAIR_BROKER_RESPONSE_INVALID") from parse_exc
        if not isinstance(result,dict):
            raise PairingClientError("PAIR_BROKER_RESPONSE_INVALID")
        return result
    except (urllib.error.URLError,TimeoutError,OSError) as exc:
        raise PairingClientError("PAIR_BROKER_UNREACHABLE") from exc
    except Exception as exc:
        raise PairingClientError("PAIR_BROKER_RESPONSE_INVALID") from exc
    if not isinstance(result,dict):
        raise PairingClientError("PAIR_BROKER_RESPONSE_INVALID")
    return result


def start_pairing(
    *,
    state_dir: Path,
    node_id: str,
    node_label: str,
    node_host_id: str,
    public_jwk: dict[str, Any],
    provider: str,
    capability_manifest_sha256: str,
    broker_base_url: str,
    allow_insecure_dev: bool=False,
    poster: Callable[..., dict[str,Any]]=post_json,
) -> dict[str,Any]:
    if not _broker_base_ok(broker_base_url,allow_insecure_dev):
        raise PairingClientError("PAIR_BROKER_URL_INVALID")
    crypto=ensure_node_keypair(Path(state_dir),comment="framebridge-"+str(node_id)[:8])
    request=create_pair_request(
        node_id=node_id,
        node_label=node_label,
        node_host_id=node_host_id,
        public_jwk=public_jwk,
        provider=provider,
        capability_manifest_sha256=capability_manifest_sha256,
    )
    signature=sign_pairing_payload(
        crypto["private_key_path"],
        pairing_signing_payload(request),
    )
    signed=attach_node_proof(
        request,
        public_key_ssh=crypto["public_key_ssh"],
        signature_b64=signature,
    )
    response=poster(
        _endpoint(broker_base_url,"fbpair/v1/device_authorization"),
        signed,
    )
    pending=validate_device_authorization(
        request,response,allow_insecure_dev=allow_insecure_dev
    )
    pending["broker_base_url"]=str(broker_base_url).rstrip("/")
    PairingStore(Path(state_dir)).begin(pending)
    return public_pairing_view(pending)


def confirm_pairing_remote(
    *,
    state_dir: Path,
    allow_insecure_dev: bool=False,
    poster: Callable[..., dict[str,Any]]=post_json,
    record: dict[str,Any] | None=None,
) -> dict[str,Any]:
    store=PairingStore(Path(state_dir))
    current=dict(record) if isinstance(record,dict) else store.load()
    if current.get("state")!="paired":
        raise PairingClientError("PAIR_CONFIRM_STATE_INVALID")
    pair_id=str(current.get("pair_id") or "")
    node_id=str(current.get("node_id") or "")
    node_host_id=str(current.get("node_host_id") or "")
    request_sha256=str(current.get("request_sha256") or "")
    broker=str(current.get("broker_base_url") or "")
    if not pair_id or not node_id or not node_host_id or len(request_sha256)!=64:
        raise PairingClientError("PAIR_CONFIRM_STATE_INCOMPLETE")
    if not _broker_base_ok(broker,allow_insecure_dev):
        raise PairingClientError("PAIR_BROKER_URL_INVALID")
    crypto=ensure_node_keypair(Path(state_dir),comment="framebridge-"+node_id.replace("-","")[:8])
    base={
        "protocol":"FBPAIR/1",
        "schema":1,
        "operation":"pair.confirm",
        "pair_id":pair_id,
        "request_sha256":request_sha256,
        "node_id":node_id,
        "node_host_id":node_host_id,
        "confirm_nonce":secrets.token_urlsafe(24),
        "confirmed_at":datetime.now(timezone.utc).isoformat(),
    }
    signature=sign_pairing_payload(crypto["private_key_path"],pairing_signing_payload(base))
    signed=attach_node_proof(
        base,public_key_ssh=crypto["public_key_ssh"],signature_b64=signature
    )
    response=poster(_endpoint(broker,"fbpair/v1/confirm"),signed)
    if isinstance(response,dict) and response.get("ok") is not True and str(response.get("state") or "") in {"cancelled","denied","expired"}:
        store.invalidate_unconfirmed_pair(pair_id,str(response.get("state")))
        return response
    if not isinstance(response,dict) or response.get("ok") is not True or response.get("state")!="paired":
        error=str((response or {}).get("error") or "pair_confirm_failed").upper() if isinstance(response,dict) else "PAIR_CONFIRM_RESPONSE_INVALID"
        raise PairingClientError("PAIR_BROKER_"+error)
    store.mark_broker_confirmed(pair_id,response)
    return response


def cancel_pairing_remote(
    *,
    state_dir: Path,
    reason: str,
    allow_insecure_dev: bool=False,
    poster: Callable[..., dict[str,Any]]=post_json,
    record: dict[str,Any] | None=None,
) -> dict[str,Any]:
    reason=str(reason or "").strip().lower()
    if reason not in {
        "network_change","service_stop","reconcile","window_expired",
        "window_closed","local_operator_cancelled",
    }:
        raise PairingClientError("PAIR_CANCEL_REASON_INVALID")
    store=PairingStore(Path(state_dir))
    current=dict(record) if isinstance(record,dict) else store.load()
    pair_id=str(current.get("pair_id") or "")
    node_id=str(current.get("node_id") or "")
    node_host_id=str(current.get("node_host_id") or "")
    broker=str(current.get("broker_base_url") or "")
    if not pair_id or not node_id or not node_host_id:
        raise PairingClientError("PAIR_CANCEL_STATE_INCOMPLETE")
    if not _broker_base_ok(broker,allow_insecure_dev):
        raise PairingClientError("PAIR_BROKER_URL_INVALID")
    crypto=ensure_node_keypair(Path(state_dir),comment="framebridge-"+node_id.replace("-","")[:8])
    base={
        "protocol":"FBPAIR/1",
        "schema":1,
        "operation":"pair.cancel",
        "pair_id":pair_id,
        "node_id":node_id,
        "node_host_id":node_host_id,
        "reason":reason,
        "cancel_nonce":secrets.token_urlsafe(24),
        "cancelled_at":datetime.now(timezone.utc).isoformat(),
    }
    signature=sign_pairing_payload(crypto["private_key_path"],pairing_signing_payload(base))
    signed=attach_node_proof(
        base,
        public_key_ssh=crypto["public_key_ssh"],
        signature_b64=signature,
    )
    response=poster(_endpoint(broker,"fbpair/v1/cancel"),signed)
    if not isinstance(response,dict):
        raise PairingClientError("PAIR_CANCEL_RESPONSE_INVALID")
    if response.get("ok") is True and str(response.get("state") or "") in {
        "cancelled","expired","denied",
    }:
        return response
    error=str(response.get("error") or "pair_cancel_failed").upper()
    raise PairingClientError("PAIR_BROKER_"+error)


def poll_pairing(
    *,
    state_dir: Path,
    allow_insecure_dev: bool=False,
    poster: Callable[..., dict[str,Any]]=post_json,
) -> dict[str,Any]:
    store=PairingStore(Path(state_dir))
    pending=store.load()
    if pending.get("state")=="paired" and pending.get("broker_confirm_required") is True:
        try:
            confirm_pairing_remote(
                state_dir=Path(state_dir),allow_insecure_dev=allow_insecure_dev,
                poster=poster,record=pending,
            )
        except PairingClientError:
            result=public_pairing_view(store.load())
            result["broker_status"]="confirm_pending"
            return result
        return public_pairing_view(store.load())
    if pending.get("state")!="pair_requested":
        raise PairingClientError("PAIR_NOT_PENDING")
    pair_id=str(pending.get("pair_id") or "")
    broker=str(pending.get("broker_base_url") or "")
    if not _broker_base_ok(broker,allow_insecure_dev):
        raise PairingClientError("PAIR_BROKER_URL_INVALID")

    now=datetime.now(timezone.utc)
    try:
        expires=datetime.fromisoformat(
            str(pending.get("expires_at") or "").replace("Z","+00:00")
        ).astimezone(timezone.utc)
    except Exception as exc:
        raise PairingClientError("PAIR_EXPIRY_STATE_INVALID") from exc
    if now>=expires:
        current=store.transition_pending(
            pair_id,"expired",
            {"expired_at":now.isoformat(),"expiration_reason":"pairing_timeout"},
        )
        return public_pairing_view(current)

    last=pending.get("last_poll_at")
    interval=max(1,int(pending.get("interval") or 5))
    if last:
        try:
            last_dt=datetime.fromisoformat(
                str(last).replace("Z","+00:00")
            ).astimezone(timezone.utc)
        except Exception as exc:
            raise PairingClientError("PAIR_POLL_STATE_INVALID") from exc
        remaining=interval-(now-last_dt).total_seconds()
        if remaining>0:
            result=public_pairing_view(pending)
            result["broker_status"]="local_poll_interval"
            result["retry_after"]=max(1,int(remaining+0.999))
            return result

    pending=store.update_pending_fields(pair_id,{"last_poll_at":now.isoformat()})
    if pending.get("state")!="pair_requested":
        return public_pairing_view(pending)
    device_code=str(pending.get("device_code") or "")
    if not device_code:
        return public_pairing_view(pending)

    response=poster(
        _endpoint(broker,"fbpair/v1/token"),
        {
            "protocol":"FBPAIR/1",
            "pair_id":pair_id,
            "device_code":device_code,
        },
    )

    # A network monitor may have invalidated this pair while the POST was in flight.
    current=store.load()
    if current.get("state")!="pair_requested" or str(current.get("pair_id") or "")!=pair_id:
        return public_pairing_view(current)

    error=str(response.get("error") or "")
    if error in {"authorization_pending","slow_down"}:
        if error=="slow_down":
            current=store.update_pending_fields(
                pair_id,{"interval":min(60,int(current.get("interval") or 5)+5)}
            )
        result=public_pairing_view(current)
        result["broker_status"]=error
        return result
    if error=="access_denied":
        current=store.transition_pending(
            pair_id,"denied",{"denied_at":datetime.now(timezone.utc).isoformat()}
        )
        return public_pairing_view(current)
    if error in {"expired_token","expired"}:
        current=store.transition_pending(
            pair_id,"expired",
            {"expired_at":datetime.now(timezone.utc).isoformat(),"expiration_reason":"broker_expired"},
        )
        return public_pairing_view(current)
    if error:
        raise PairingClientError("PAIR_BROKER_"+error.upper())
    try:
        store.complete(response)
    except PairingError as exc:
        # If local invalidation won the race, do not revive the pairing.
        current=store.load()
        if current.get("state")!="pair_requested":
            return public_pairing_view(current)
        raise PairingClientError(str(exc)) from exc
    paired=store.load()
    try:
        confirm_pairing_remote(
            state_dir=Path(state_dir),
            allow_insecure_dev=allow_insecure_dev,
            poster=poster,
            record=paired,
        )
    except PairingClientError:
        current=public_pairing_view(store.load())
        current["broker_status"]="confirm_pending"
        return current
    return public_pairing_view(store.load())

