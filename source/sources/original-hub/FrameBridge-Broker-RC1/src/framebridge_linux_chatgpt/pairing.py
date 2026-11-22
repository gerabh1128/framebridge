from __future__ import annotations

import hashlib
import ipaddress
import json
import fcntl
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FBPAIR_PROTOCOL = "FBPAIR/1"
PAIR_STATES = {
    "not_paired",
    "pair_requested",
    "paired",
    "denied",
    "expired",
    "revoked",
}
PROVIDERS = {"chatgpt", "ollama", "lmstudio", "other"}


class PairingError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


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


def _https_or_loopback_http(uri: str, allow_insecure_dev: bool=False) -> bool:
    try:
        p = urlparse(str(uri))
    except Exception:
        return False
    if p.scheme == "https" and bool(p.netloc):
        return True
    if p.scheme == "http" and bool(p.netloc):
        host=str(p.hostname or "")
        if _is_local_ip(host):
            return True
        if allow_insecure_dev:
            return True
    return False


def create_pair_request(
    *,
    node_id: str,
    node_label: str,
    node_host_id: str,
    public_jwk: dict[str, Any],
    provider: str,
    capability_manifest_sha256: str,
) -> dict[str, Any]:
    provider = str(provider).strip().lower()
    if provider not in PROVIDERS:
        raise PairingError("PAIR_PROVIDER_UNSUPPORTED")
    node_label=str(node_label).strip()
    if not node_id or not node_host_id or not node_label or len(node_label)>253:
        raise PairingError("PAIR_NODE_IDENTITY_REQUIRED")
    manifest_sha = str(capability_manifest_sha256).strip().lower()
    if len(manifest_sha) != 64 or any(c not in "0123456789abcdef" for c in manifest_sha):
        raise PairingError("PAIR_CAPABILITY_MANIFEST_SHA_INVALID")
    nonce = secrets.token_urlsafe(32)
    request = {
        "protocol": FBPAIR_PROTOCOL,
        "schema": 1,
        "node_id": str(node_id),
        "node_label": node_label,
        "node_host_id": str(node_host_id),
        "public_jwk": public_jwk,
        "provider": provider,
        "capability_manifest_sha256": manifest_sha,
        "node_nonce": nonce,
        "requested_at": utc_now(),
    }
    request["request_sha256"] = sha256_hex(request)
    return request


def attach_node_proof(
    request: dict[str, Any],
    *,
    public_key_ssh: str,
    signature_b64: str,
) -> dict[str, Any]:
    if request.get("protocol") != FBPAIR_PROTOCOL:
        raise PairingError("PAIR_REQUEST_PROTOCOL_INVALID")
    if not str(public_key_ssh).startswith("ssh-ed25519 "):
        raise PairingError("PAIR_PUBLIC_KEY_INVALID")
    if not signature_b64:
        raise PairingError("PAIR_NODE_PROOF_REQUIRED")
    value=dict(request)
    value["public_key_ssh"]=str(public_key_ssh)
    value["node_proof"]={
        "format":"sshsig",
        "namespace":"framebridge-pairing",
        "signature_b64":str(signature_b64),
    }
    return value


def pairing_signing_payload(request: dict[str, Any]) -> bytes:
    value=dict(request)
    value.pop("node_proof",None)
    value.pop("public_key_ssh",None)
    return canonical(value)


def validate_device_authorization(
    request: dict[str, Any],
    response: dict[str, Any],
    *,
    allow_insecure_dev: bool=False,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise PairingError("PAIR_DEVICE_RESPONSE_INVALID")
    required = {
        "pair_id",
        "device_code",
        "user_code",
        "verification_uri",
        "expires_in",
    }
    if not required.issubset(response):
        raise PairingError("PAIR_DEVICE_RESPONSE_INCOMPLETE")
    pair_id = str(response["pair_id"])
    device_code = str(response["device_code"])
    user_code = str(response["user_code"])
    verification_uri = str(response["verification_uri"])
    complete = str(response.get("verification_uri_complete") or "")
    try:
        expires_in = int(response["expires_in"])
        interval = int(response.get("interval", 5))
    except Exception as exc:
        raise PairingError("PAIR_DEVICE_RESPONSE_INVALID") from exc
    if not pair_id or len(device_code) < 24 or not user_code:
        raise PairingError("PAIR_DEVICE_RESPONSE_INVALID")
    if not _https_or_loopback_http(verification_uri, allow_insecure_dev):
        raise PairingError("PAIR_VERIFICATION_URI_INVALID")
    if complete and not _https_or_loopback_http(complete, allow_insecure_dev):
        raise PairingError("PAIR_VERIFICATION_URI_COMPLETE_INVALID")
    if not (30 <= expires_in <= 1800):
        raise PairingError("PAIR_EXPIRES_INVALID")
    if not (1 <= interval <= 60):
        raise PairingError("PAIR_INTERVAL_INVALID")
    try:
        requested_dt=datetime.fromisoformat(
            str(request["requested_at"]).replace("Z","+00:00")
        ).astimezone(timezone.utc)
    except Exception as exc:
        raise PairingError("PAIR_REQUEST_TIME_INVALID") from exc
    expires_at=(requested_dt+timedelta(seconds=expires_in)).isoformat()

    return {
        "protocol": FBPAIR_PROTOCOL,
        "schema": 1,
        "state": "pair_requested",
        "pair_id": pair_id,
        "request_sha256": request["request_sha256"],
        "node_id": request["node_id"],
        "node_label": request["node_label"],
        "node_host_id": request["node_host_id"],
        "provider": request["provider"],
        "device_code": device_code,
        "device_code_sha256": hashlib.sha256(device_code.encode("utf-8")).hexdigest(),
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": complete or None,
        "expires_in": expires_in,
        "expires_at": expires_at,
        "interval": interval,
        "requested_at": request["requested_at"],
        "last_poll_at": None,
    }


def public_pairing_view(record: dict[str, Any]) -> dict[str, Any]:
    value = dict(record)
    value.pop("device_code", None)
    value.pop("account_subject", None)
    value.pop("provider_access_token", None)
    value.pop("provider_refresh_token", None)
    return value


def pairing_qr_payload(record: dict[str, Any]) -> str:
    if record.get("state") != "pair_requested":
        raise PairingError("PAIR_NOT_PENDING")
    return str(
        record.get("verification_uri_complete")
        or record.get("verification_uri")
        or ""
    )


def validate_pair_receipt(
    pending: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    if pending.get("state") != "pair_requested":
        raise PairingError("PAIR_NOT_PENDING")
    if not isinstance(receipt, dict):
        raise PairingError("PAIR_RECEIPT_INVALID")
    checks = {
        "protocol": receipt.get("protocol") == FBPAIR_PROTOCOL,
        "schema": receipt.get("schema") == 1,
        "state": receipt.get("state") == "paired",
        "pair_id": receipt.get("pair_id") == pending.get("pair_id"),
        "request_sha256": receipt.get("request_sha256") == pending.get("request_sha256"),
        "node_id": receipt.get("node_id") == pending.get("node_id"),
        "node_label": receipt.get("node_label") == pending.get("node_label"),
        "node_host_id": receipt.get("node_host_id") == pending.get("node_host_id"),
        "provider": receipt.get("provider") == pending.get("provider"),
    }
    if not all(checks.values()):
        raise PairingError("PAIR_RECEIPT_BINDING_MISMATCH")
    broker_issuer = str(receipt.get("broker_issuer") or "")
    account_binding_id = str(receipt.get("account_binding_id") or "")
    grant_id = str(receipt.get("grant_id") or "")
    if (
        not broker_issuer
        or not account_binding_id
        or len(account_binding_id)>160
        or not grant_id
        or len(grant_id)>160
    ):
        raise PairingError("PAIR_ACCOUNT_BINDING_MISSING")
    return {
        "protocol": FBPAIR_PROTOCOL,
        "schema": 1,
        "state": "paired",
        "pair_id": pending["pair_id"],
        "request_sha256": pending["request_sha256"],
        "node_id": pending["node_id"],
        "node_label": pending["node_label"],
        "node_host_id": pending["node_host_id"],
        "provider": pending["provider"],
        "broker_issuer": broker_issuer,
        "account_binding_id": account_binding_id,
        "grant_id": grant_id,
        "paired_at": str(receipt.get("paired_at") or utc_now()),
        "scopes": sorted({str(x) for x in (receipt.get("scopes") or [])}),
        "revocation_id": str(receipt.get("revocation_id") or ""),
        "node_grant_binding": str(receipt.get("node_grant_binding") or "proof-of-possession-required"),
    }


class PairingStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "pairing.json"
        self.lock_path = Path(state_dir) / ".pairing.lock"

    @contextmanager
    def _exclusive(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags=os.O_RDWR|os.O_CREAT|getattr(os,"O_NOFOLLOW",0)
        fd=os.open(self.lock_path,flags,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd,fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"protocol": FBPAIR_PROTOCOL, "schema": 1, "state": "not_paired"}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("state") not in PAIR_STATES:
            raise PairingError("PAIR_STATE_INVALID")
        return value

    def load(self) -> dict[str, Any]:
        # Atomic replacement makes a lock-free reader see old-or-new complete JSON.
        return self._load_unlocked()

    def _save_unlocked(self, value: dict[str, Any]) -> None:
        if value.get("state") not in PAIR_STATES:
            raise PairingError("PAIR_STATE_INVALID")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(prefix=".pairing.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def save(self, value: dict[str, Any]) -> None:
        with self._exclusive():
            self._save_unlocked(value)

    def begin(self, pending: dict[str, Any]) -> dict[str, Any]:
        with self._exclusive():
            current = self._load_unlocked()
            if current.get("state") == "paired":
                raise PairingError("PAIR_ALREADY_PAIRED")
            if current.get("state") == "pair_requested":
                raise PairingError("PAIR_REQUEST_ALREADY_PENDING")
            self._save_unlocked(pending)
            return public_pairing_view(pending)

    def update_pending_fields(self, pair_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        pair_id=str(pair_id or "")
        if not pair_id:
            raise PairingError("PAIR_ID_REQUIRED")
        with self._exclusive():
            current=self._load_unlocked()
            if current.get("state")!="pair_requested" or str(current.get("pair_id") or "")!=pair_id:
                return current
            current=dict(current)
            current.update(dict(fields or {}))
            self._save_unlocked(current)
            return current

    def complete(self, receipt: dict[str, Any]) -> dict[str, Any]:
        with self._exclusive():
            current = self._load_unlocked()
            paired = validate_pair_receipt(current, receipt)
            paired["broker_base_url"]=str(current.get("broker_base_url") or "")
            paired["broker_confirm_required"]=True
            self._save_unlocked(paired)
            return public_pairing_view(paired)

    def invalidate_unconfirmed_pair(self, pair_id: str, broker_state: str) -> dict[str, Any]:
        pair_id=str(pair_id or "")
        broker_state=str(broker_state or "unknown").lower()
        with self._exclusive():
            current=self._load_unlocked()
            if current.get("state")!="paired" or current.get("broker_confirm_required") is not True or str(current.get("pair_id") or "")!=pair_id:
                return current
            current=dict(current)
            current["state"]="expired"
            current["expired_at"]=utc_now()
            current["expiration_reason"]="broker_confirm_"+broker_state
            current["requires_new_local_action"]=True
            current["broker_confirm_required"]=False
            current["broker_confirm_state"]=broker_state
            self._save_unlocked(current)
            return current

    def mark_broker_confirmed(self, pair_id: str, result: dict[str, Any]) -> dict[str, Any]:
        pair_id=str(pair_id or "")
        with self._exclusive():
            current=self._load_unlocked()
            if current.get("state")!="paired" or str(current.get("pair_id") or "")!=pair_id:
                return current
            current=dict(current)
            current["broker_confirm_required"]=False
            current["broker_confirmed_at"]=utc_now()
            current["broker_confirm_state"]=str((result or {}).get("state") or "paired")
            self._save_unlocked(current)
            return current

    def transition_pending(self, pair_id: str, new_state: str, fields: dict[str, Any] | None=None) -> dict[str, Any]:
        pair_id=str(pair_id or "")
        if new_state not in {"denied","expired"}:
            raise PairingError("PAIR_TRANSITION_STATE_INVALID")
        with self._exclusive():
            current=self._load_unlocked()
            if current.get("state")!="pair_requested" or str(current.get("pair_id") or "")!=pair_id:
                return current
            current=dict(current)
            current.pop("device_code",None)
            current["state"]=new_state
            current.update(dict(fields or {}))
            self._save_unlocked(current)
            return current

    def cancel_pending(self) -> dict[str, Any]:
        with self._exclusive():
            current = self._load_unlocked()
            if current.get("state") != "pair_requested":
                raise PairingError("PAIR_NOT_PENDING")
            current=dict(current)
            current.pop("device_code", None)
            current["state"] = "revoked"
            current["revoked_at"] = utc_now()
            current["revocation_reason"] = "local_operator_cancelled"
            if current.get("pair_id") and current.get("broker_base_url"):
                current["broker_cancel_required"]=True
                current["broker_cancel_reason"]="local_operator_cancelled"
            self._save_unlocked(current)
            return public_pairing_view(current)

    def invalidate_pending(self, reason: str) -> dict[str, Any]:
        reason=str(reason or "").strip().lower()
        if reason not in {"network_change","service_stop","reconcile","window_expired","window_closed"}:
            raise PairingError("PAIR_INVALIDATION_REASON_INVALID")
        with self._exclusive():
            current=self._load_unlocked()
            if current.get("state") != "pair_requested":
                raise PairingError("PAIR_NOT_PENDING")
            current=dict(current)
            current.pop("device_code",None)
            current["state"]="expired"
            current["expired_at"]=utc_now()
            current["expiration_reason"]=reason
            current["requires_new_local_action"]=True
            if current.get("pair_id") and current.get("broker_base_url"):
                current["broker_cancel_required"]=True
                current["broker_cancel_reason"]=reason
            self._save_unlocked(current)
            return public_pairing_view(current)

    def mark_broker_cancelled(self, pair_id: str, result: dict[str, Any]) -> dict[str, Any]:
        pair_id=str(pair_id or "")
        with self._exclusive():
            current=self._load_unlocked()
            if str(current.get("pair_id") or "")!=pair_id:
                return current
            current=dict(current)
            current["broker_cancel_required"]=False
            current["broker_cancel_state"]=str((result or {}).get("state") or "cancelled")
            current["broker_cancelled_at"]=utc_now()
            current.pop("broker_cancel_last_error",None)
            self._save_unlocked(current)
            return current

    def note_broker_cancel_error(self, pair_id: str, error: str) -> dict[str, Any]:
        pair_id=str(pair_id or "")
        with self._exclusive():
            current=self._load_unlocked()
            if str(current.get("pair_id") or "")!=pair_id:
                return current
            current=dict(current)
            current["broker_cancel_required"]=True
            current["broker_cancel_last_error"]=str(error or "PAIR_BROKER_CANCEL_FAILED")[:128]
            current["broker_cancel_attempts"]=int(current.get("broker_cancel_attempts") or 0)+1
            current["broker_cancel_last_attempt_at"]=utc_now()
            self._save_unlocked(current)
            return current

    def revoke(self) -> dict[str, Any]:
        with self._exclusive():
            current = self._load_unlocked()
            if current.get("state") != "paired":
                raise PairingError("PAIR_NOT_PAIRED")
            current=dict(current)
            current["state"] = "revoked"
            current["revoked_at"] = utc_now()
            self._save_unlocked(current)
            return public_pairing_view(current)

