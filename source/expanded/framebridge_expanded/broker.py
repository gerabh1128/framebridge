"""Authenticated operator boundary around the unchanged Original FBPAIR broker."""
from __future__ import annotations

import hashlib
import http.client
import importlib
import importlib.util
import ipaddress
import json
import math
import copy
import os
import re
import secrets
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import lru_cache, partial
from http.server import ThreadingHTTPServer
from pathlib import Path

class BrokerError(ValueError):
    """A fixed public failure code; never include HTTP bodies or credentials."""


class BrokerRequestError(BrokerError):
    """An admin request whose outcome must be checked before any replay."""

    def __init__(self, message, *, request_id, execution_state):
        super().__init__(message)
        self.request_id = request_id
        self.execution_state = execution_state


_SETTINGS_LOCK = threading.RLock()

_state = None
_url = None
_control_path = None
_JOB = re.compile(r"[A-Za-z0-9._-]{1,80}")
_LABEL = re.compile(r"[A-Za-z0-9._-]{1,64}")
_ISSUED_REQUEST_ID = re.compile(r"(?:[0-9a-f]{32}|fbreq_[A-Za-z0-9_-]{24})")
_CONTROL_TIMEOUT = 10
_RESULT_GRACE = 10
_ACTIVE_GRACE = 90


def state_path(state_dir=None):
    from .config import state_path as canonical_state
    return canonical_state(state_dir or os.environ.get("FRAMEBRIDGE_EXPANDED_STATE")
                           or os.environ.get("FRAMEBRIDGE_STATE_DIR")).resolve()


def _config(state_dir=None):
    p = state_path(state_dir) / "config.json"
    if not p.is_file():
        return {}
    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        raise BrokerError("CONFIG_INVALID")
    return data


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost" and all(ipaddress.ip_address(a[4][0]).is_loopback
                                           for a in socket.getaddrinfo(host, None))


def validate_endpoint(url):
    """Permit verified HTTPS, or literal/local loopback HTTP, without URL credentials."""
    if not isinstance(url, str) or len(url) > 2048:
        raise BrokerError("BROKER_URL_INVALID")
    parsed = urllib.parse.urlsplit(url)
    try:
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        valid_port = False
    if (not parsed.hostname or not valid_port or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.scheme not in ("http", "https")
            or any(ord(c) < 33 for c in url)):
        raise BrokerError("BROKER_URL_INVALID")
    if parsed.scheme == "http" and not _loopback(parsed.hostname):
        raise BrokerError("BROKER_HTTP_LOOPBACK_REQUIRED")
    return url.rstrip("/")


NODE_HEARTBEAT_FEATURE = "node-heartbeat-v1"
NODE_HEARTBEAT_PATH = "/fbpair/v1/node/heartbeat"


class _NodeHeartbeatStore:
    """Observe liveness during a single dispatched request without dequeuing more."""

    def _node_verification_lock(self, envelope):
        if not isinstance(envelope, dict):
            return None
        node_id = str(envelope.get("node_id") or "")
        # Unknown public callers must not grow a per-node lock cache.
        if self._paired_node(node_id) is None:
            return None
        with self.lock:
            if not hasattr(self, "_node_verifier_locks"):
                self._node_verifier_locks = {}
            return self._node_verifier_locks.setdefault(node_id, threading.Lock())

    def verify_node_envelope(self, envelope, *, refresh_last_seen=True):
        guard = self._node_verification_lock(envelope)
        if guard is None:
            if not isinstance(envelope, dict):
                return False, "invalid_envelope"
            known = self._discovery_node(str(envelope.get("node_id") or ""))
            return False, ("node_revoked" if isinstance(known, dict)
                           and known.get("state") == "revoked" else "node_not_paired")
        # The original verifier releases Store.lock while checking SSHSIG.
        # Serialize each node's verification through nonce check and commit,
        # without holding the global store lock across subprocess verification.
        with guard:
            if refresh_last_seen:
                return super().verify_node_envelope(envelope)
            # Nonces are still consumed by the original verifier, but result
            # delivery can only refresh liveness after the receipt is accepted.
            shadow = copy.copy(self)
            shadow.node_last_seen = {}
            return super(_NodeHeartbeatStore, shadow).verify_node_envelope(envelope)

    def next_read(self, node_id):
        node_id = str(node_id)
        with self.lock:
            queue = self.control_queue[node_id]
            while queue:
                rid = queue.popleft()
                item = self.control_requests.get(rid)
                receipt = self.control_results.get(rid)
                if not item or not receipt or receipt.get("state") != "pending":
                    continue
                now = time.time()
                start_by = item["expires_at"]
                if now >= start_by:
                    receipt["state"] = "expired"
                    continue
                receipt.update(state="dispatched", dispatched_at=now)
                active_until = start_by
                if item.get("operation") in {"admin.exec", "admin.cua"}:
                    active_until = now + item["payload"]["timeout_s"] + _ACTIVE_GRACE
                if not hasattr(self, "_heartbeat_dispatched"):
                    self._heartbeat_dispatched = {}
                self._heartbeat_dispatched.setdefault(node_id, {})[rid] = (start_by, active_until)
                return item
        return None

    def _put_result_locked(self, node_id, request_id, result):
        receipt = self.control_results.get(request_id)
        if not receipt or receipt.get("node_id") != node_id or not isinstance(result, dict):
            return False
        if receipt.get("state") == "complete":
            return receipt.get("result") == result
        if receipt.get("state") != "dispatched":
            return False
        receipt.update(state="complete", result=result, completed_at=time.time())
        getattr(self, "_heartbeat_dispatched", {}).get(node_id, {}).pop(request_id, None)
        return True

    def put_result(self, node_id, request_id, result):
        with self.lock:
            return self._put_result_locked(str(node_id), str(request_id), result)

    def receive_node_result(self, node_id, request_id, result):
        """Settle an authenticated delivery without recreating a lost receipt."""
        with self.lock:
            if self._put_result_locked(node_id, request_id, result):
                self.node_last_seen[node_id] = time.time()
                return {"ok": True}
            if (request_id not in self.control_results and request_id not in self.control_requests
                    and _ISSUED_REQUEST_ID.fullmatch(request_id)):
                # The broker keeps requests in memory. A frozen node may have
                # kept an already executed result across a broker restart.
                # Tell it to stop retrying, without reporting a saved result.
                return {"ok": True, "disposition": "orphaned"}
            return {"ok": False}

    def get_result(self, request_id):
        request_id = str(request_id)
        with self.lock:
            receipt = self.control_results.get(request_id)
            if receipt is None:
                return None
            if receipt["state"] == "pending":
                item = self.control_requests[request_id]
                if time.time() >= item["expires_at"]:
                    receipt["state"] = "expired"
            return dict(receipt)

    def admin_request_status(self, node_id, request_id, job_id, cancel_pending):
        with self.lock:
            rid = request_id or getattr(self, "_admin_requests_by_job", {}).get((node_id, job_id))
            item = self.control_requests.get(rid)
            if (not item or item.get("node_id") != node_id
                    or item.get("operation") not in {"admin.exec", "admin.cua"}):
                return {"state": "unknown", "node_id": node_id}
            actual_job_id = item["payload"]["job_id"]
            if job_id and job_id != actual_job_id:
                return {"state": "unknown", "node_id": node_id}
            receipt = self.control_results[rid]
            if receipt["state"] == "pending":
                if time.time() >= item["expires_at"]:
                    receipt["state"] = "expired"
                elif cancel_pending:
                    receipt["state"] = "cancelled"
            return dict(receipt, request_id=rid, job_id=actual_job_id)

    def _active_node_requests(self, node_id, now):
        # Caller holds the existing store lock. A heartbeat never extends either deadline.
        active = []
        # Track only currently dispatched work, never rescan historical receipts.
        dispatched = getattr(self, "_heartbeat_dispatched", {}).get(node_id, {})
        for rid, (start_by, active_until) in list(dispatched.items()):
            state = self.control_results.get(rid)
            if (not isinstance(state, dict) or state.get("node_id") != node_id
                    or state.get("state") != "dispatched"):
                dispatched.pop(rid, None)
                continue
            request = self.control_requests.get(rid)
            if not isinstance(request, dict) or request.get("node_id") != node_id:
                dispatched.pop(rid, None)
                continue
            if (type(active_until) not in (int, float) or not math.isfinite(active_until)
                    or request.get("expires_at") != start_by or now >= active_until):
                dispatched.pop(rid, None)
                continue
            active.append(rid)
        return active

    def accept_node_heartbeat(self, envelope):
        fields = {"schema", "kind", "node_id", "node_host_id", "timestamp", "nonce",
                  "payload", "node_proof"}
        if (not isinstance(envelope, dict) or set(envelope) != fields
                or type(envelope.get("schema")) is not int or envelope["schema"] != 1
                or envelope.get("kind") != "node.heartbeat"):
            return False, "heartbeat_envelope_invalid"
        payload = envelope.get("payload")
        if (not isinstance(payload, dict) or set(payload) != {"request_id"}
                or type(payload["request_id"]) is not str
                or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", payload["request_id"]) is None):
            return False, "heartbeat_payload_invalid"
        node_id, rid = envelope.get("node_id"), payload["request_id"]
        timestamp = envelope.get("timestamp")
        if (type(node_id) is not str or type(timestamp) not in (int, float)
                or not -math.inf < timestamp < math.inf
                or not isinstance(envelope.get("node_proof"), dict)):
            return False, "heartbeat_envelope_invalid"
        guard = self._node_verification_lock(envelope)
        if guard is None:
            return False, "heartbeat_node_not_paired"
        with guard:
            with self.lock:
                if rid not in self._active_node_requests(node_id, time.time()):
                    return False, "heartbeat_request_not_active"
            # Reuse the original SSHSIG/host/timestamp/nonce verifier unchanged.
            # Its nonce map is shared, but liveness is staged in a shadow dict:
            # expiry or completion during crypto must not refresh real liveness.
            shadow = copy.copy(self)
            shadow.node_last_seen = {}
            ok, detail = super(_NodeHeartbeatStore, shadow).verify_node_envelope(envelope)
            if not ok:
                return False, detail
            with self.lock:
                if rid not in self._active_node_requests(node_id, time.time()):
                    return False, "heartbeat_request_not_active"
                self.node_last_seen[node_id] = shadow.node_last_seen[node_id]
        return True, {"ok": True, "request_id": rid, "busy": True}

    def node_runtime_status(self, node_id):
        value = super().node_runtime_status(node_id)
        if value is None:
            return None
        with self.lock:
            active = self._active_node_requests(str(node_id), time.time())
        fresh = value.get("ready") is True
        busy = bool(active)
        return dict(value, heartbeat_fresh=fresh, busy=busy,
                    active_request_count=len(active),
                    activity_state=("busy" if fresh and busy else "idle" if fresh
                                    else "busy-heartbeat-stale" if busy else "offline"))


def _node_message_response(store, path, body):
    """Keep each authenticated wire operation bound to its endpoint."""
    if path == NODE_HEARTBEAT_PATH:
        ok, detail = store.accept_node_heartbeat(body)
        return (200, detail) if ok else (403, {"ok": False, "error": detail})
    kind = {"/fbpair/v1/node/poll": "node.poll", "/fbpair/v1/node/result": "node.result"}.get(path)
    if kind is None:
        raise BrokerError("ENDPOINT_NOT_AVAILABLE")
    if not isinstance(body, dict):
        return 403, {"ok": False, "error": "invalid_envelope"}
    if body.get("kind") != kind:
        return 403, {"ok": False, "error": "node_message_kind_mismatch"}
    timestamp, payload = body.get("timestamp"), body.get("payload")
    if (type(body.get("schema")) is not int or body["schema"] != 1
            or type(body.get("node_id")) is not str
            or type(timestamp) not in (int, float) or not -math.inf < timestamp < math.inf
            or not isinstance(payload, dict) or not isinstance(body.get("node_proof"), dict)):
        return 403, {"ok": False, "error": "invalid_envelope"}
    poll = kind == "node.poll"
    if not poll:
        request_id, result = payload.get("request_id"), payload.get("result")
        if (not isinstance(request_id, str) or _JOB.fullmatch(request_id) is None
                or not isinstance(result, dict)):
            return 400, {"ok": False, "error": "node_result_payload_invalid"}
    ok, detail = store.verify_node_envelope(body, refresh_last_seen=poll)
    if not ok:
        return 403, {"ok": False, "error": detail}
    if poll:
        item = store.next_read(body["node_id"])
        return 200, {"ok": True, "request": item,
                     "protocol_features": [NODE_HEARTBEAT_FEATURE]}
    response = store.receive_node_result(body["node_id"], request_id, result)
    return (200 if response["ok"] else 400), response


def configure(broker_url=None, state_dir=None, control_key_path=None):
    from framebridge_expanded.config import broker_control_url

    global _state, _url, _control_path
    # Workers must see one fully validated profile, including explicit changes.
    with _SETTINGS_LOCK:
        state = state_path(state_dir)
        config = _config(state)
        url = validate_endpoint(broker_url or broker_control_url(config))
        control_path = Path(control_key_path or state / "broker" / "control.key")
        _state, _url, _control_path = state, url, control_path


def _settings():
    with _SETTINGS_LOCK:
        if _state is None:
            configure()
        return _state, _url, _control_path


def _private_read(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise BrokerError("PRIVATE_STATE_PERMISSIONS_INVALID")
        data = os.read(fd, 4097)
        if len(data) > 4096:
            raise BrokerError("PRIVATE_STATE_TOO_LARGE")
        return data
    finally:
        os.close(fd)


def _control_secret(state_dir, create=False):
    p = state_path(state_dir) / "broker" / "control.key"
    if create:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = p.parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise BrokerError("PRIVATE_CONTROL_DIRECTORY_INVALID")
        if not p.exists():
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                raw = secrets.token_urlsafe(48).encode()
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                raise BrokerError("CONTROL_IDENTITY_CREATE_FAILED") from None
    secret = _private_read(p).decode("ascii").strip()
    if len(secret) < 40:
        raise BrokerError("CONTROL_IDENTITY_INVALID")
    return secret


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BrokerError("BROKER_REDIRECT_BLOCKED")


class _ControlHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context, source_address):
        super().__init__(context=context)
        self.source_address = source_address

    def https_open(self, request):
        connect = partial(http.client.HTTPSConnection, source_address=self.source_address)
        return self.do_open(connect, request, context=self._context)


def _control_source_address(url, config):
    """Bind the local control connection to loopback for a concrete local listener."""
    endpoint = urllib.parse.urlsplit(url)
    if (endpoint.scheme != "https"
            or endpoint.port != config.get("ports", {}).get("broker", 20878)):
        return None
    try:
        listener = ipaddress.ip_address(config.get("broker_listen_host"))
        destination = ipaddress.ip_address(endpoint.hostname)
    except ValueError:
        return None
    if listener != destination or listener.is_loopback or listener.is_unspecified or listener.is_multicast:
        return None
    return ("::1" if listener.version == 6 else "127.0.0.1", 0)


def post_json(url, value, *, state_dir=None, timeout=10, control=False):
    validate_endpoint(url)
    config = _config(state_dir)
    ca = os.environ.get("FRAMEBRIDGE_TLS_CA") or config.get("tls_ca") or None
    context = ssl.create_default_context(cafile=str(ca) if ca else None)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if control:
        _, _, configured = _settings()
        headers["Authorization"] = "Bearer " + _private_read(configured).decode("ascii").strip()
    request = urllib.request.Request(url, data=json.dumps(value, separators=(",", ":"),
                                                         allow_nan=False).encode(),
                                     headers=headers, method="POST")
    source_address = _control_source_address(url, config) if control else None
    https = (_ControlHTTPSHandler(context, source_address) if source_address
             else urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), https)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise BrokerError("BROKER_RESPONSE_TOO_LARGE")
        result = json.loads(raw)
    except BrokerError:
        raise
    except urllib.error.HTTPError as exc:
        raise BrokerError("BROKER_HTTP_REJECTED_" + str(exc.code)) from None
    except Exception:
        raise BrokerError("BROKER_TRANSPORT_FAILED") from None
    if not isinstance(result, dict):
        raise BrokerError("BROKER_RESPONSE_INVALID")
    return result


def _control(path, payload, *, timeout=_CONTROL_TIMEOUT):
    state, url, _ = _settings()
    return post_json(url + "/fbpair/v1/control/" + path, payload,
                     state_dir=state, control=True, timeout=timeout)


def _control_until(path, payload, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BrokerError("BROKER_RESULT_TIMEOUT")
    if remaining >= _CONTROL_TIMEOUT:
        return _control(path, payload)
    return _control(path, payload, timeout=remaining)


def list_confirmed_nodes():
    result = _control("nodes", {})
    if result.get("ok") is not True or not isinstance(result.get("nodes"), list):
        raise BrokerError("BROKER_DISCOVERY_FAILED")
    nodes = result["nodes"]
    ids, aliases = set(), set()
    for item in nodes:
        if not isinstance(item, dict):
            raise BrokerError("BROKER_DISCOVERY_INVALID")
        ident, alias = item.get("node_id"), item.get("alias")
        if not isinstance(ident, str) or not isinstance(alias, str):
            raise BrokerError("BROKER_DISCOVERY_INVALID")
        if ident in ids or alias.casefold() in aliases:
            raise BrokerError("BROKER_DISCOVERY_AMBIGUOUS")
        ids.add(ident)
        aliases.add(alias.casefold())
    return nodes


def list_pending():
    result = _control("pending", {})
    if result.get("ok") is not True:
        raise BrokerError("BROKER_PENDING_FAILED")
    return result.get("requests", [])


def approve(request_id, expected_node_id, state_dir=None):
    if state_dir is not None:
        configure(state_dir=state_dir)
    return _control("approve", {"request_id": request_id, "expected_node_id": expected_node_id})


def revoke(node_id):
    return _control("revoke", {"node_id": node_id})


def request_status(node_id, *, request_id="", job_id="", cancel_pending=False):
    """Read the matching admin receipt on the authenticated control channel."""
    return _control("request-status", {"node_id": node_id, "request_id": request_id,
                                       "job_id": job_id, "cancel_pending": cancel_pending},
                    timeout=2)


def _complete_result(receipt, node_id, request_id):
    if receipt.get("node_id") != node_id:
        raise BrokerError("BROKER_RESULT_NODE_MISMATCH")
    result = receipt.get("result")
    if not isinstance(result, dict):
        raise BrokerError("BROKER_RESULT_INVALID")
    return dict(result, request_id=request_id, node_id=node_id)


def _settle_admin_request(node_id, request_id, job_id):
    receipt = {}
    if request_id or job_id:
        try:
            receipt = request_status(node_id, request_id=request_id or "", job_id=job_id,
                                     cancel_pending=True)
        except BrokerError:
            pass
    if (receipt.get("ok") is True and receipt.get("node_id") == node_id
            and receipt.get("request_id") and (not request_id or receipt["request_id"] == request_id)
            and (not job_id or receipt.get("job_id") == job_id)):
        request_id = receipt["request_id"]
        state = receipt.get("state")
        if state == "complete" and isinstance(receipt.get("result"), dict):
            return _complete_result(receipt, node_id, request_id)
        if state in {"expired", "cancelled"}:
            message = "BROKER_REQUEST_CANCELLED" if state == "cancelled" else "BROKER_REQUEST_EXPIRED"
            raise BrokerRequestError(message, request_id=request_id,
                                     execution_state="expired_before_dispatch")
        if state == "dispatched":
            raise BrokerRequestError("BROKER_REQUEST_IN_FLIGHT", request_id=request_id,
                                     execution_state="in_flight")
    raise BrokerRequestError("BROKER_REQUEST_OUTCOME_UNKNOWN", request_id=request_id,
                             execution_state="outcome_unknown")


def request(node_id, capability, args, timeout_s=120):
    if type(timeout_s) is not int or not 1 <= timeout_s <= 600:
        raise BrokerError("INVALID_TIMEOUT")
    if not isinstance(args, dict):
        raise BrokerError("ARGS_NOT_OBJECT")
    path = {"admin.exec": "admin-exec", "admin.cua": "admin-cua"}.get(capability, "request")
    admin = path != "request"
    payload = {"node_id": node_id}
    if admin:
        deadline = time.monotonic() + timeout_s + _RESULT_GRACE
        payload.update(payload=args, start_by=time.time() + timeout_s + _RESULT_GRACE)
    else:
        payload.update(capability=capability, args=args)
    confirmed = {n["node_id"]: n for n in list_confirmed_nodes()}
    if node_id not in confirmed:
        raise BrokerError("NODE_NOT_CONFIRMED")
    if confirmed[node_id].get("ready") is not True:
        if confirmed[node_id].get("busy") is True:
            raise BrokerError("NODE_BUSY_HEARTBEAT_STALE")
        raise BrokerError("NODE_OFFLINE")
    if admin and confirmed[node_id].get("supports_start_deadline") is not True:
        raise BrokerError("BROKER_START_DEADLINE_UNSUPPORTED")
    job_id = args.get("job_id")
    job_id = job_id if isinstance(job_id, str) and _JOB.fullmatch(job_id) else ""
    if admin and time.monotonic() >= deadline:
        raise BrokerRequestError("BROKER_REQUEST_EXPIRED", request_id=None,
                                 execution_state="expired_before_dispatch")
    try:
        created = _control_until(path, payload, deadline) if admin else _control(path, payload)
    except BrokerError as exc:
        if not admin or str(exc) == "BROKER_HTTP_REJECTED_400":
            raise
        return _settle_admin_request(node_id, None, job_id)
    if created.get("ok") is not True:
        if admin and created.get("ok") is not False:
            return _settle_admin_request(node_id, None, job_id)
        raise BrokerError("BROKER_REQUEST_REJECTED")
    rid = created.get("request_id")
    if not isinstance(rid, str) or not rid:
        if admin:
            return _settle_admin_request(node_id, None, job_id)
        raise BrokerError("BROKER_REQUEST_REJECTED")
    if not admin:
        # Discovery and enqueue have their own bounded HTTP calls. A READ's
        # result budget starts after the broker has actually queued the work.
        deadline = time.monotonic() + timeout_s + _RESULT_GRACE
    while time.monotonic() < deadline:
        try:
            final = _control_until("result", {"request_id": rid}, deadline)
            if final.get("node_id") != node_id:
                raise BrokerError("BROKER_RESULT_NODE_MISMATCH")
            if final.get("state") == "complete":
                return _complete_result(final, node_id, rid)
        except BrokerError as exc:
            from .node import _retryable_broker_failure
            if not _retryable_broker_failure(exc):
                if not admin:
                    raise
                break
        else:
            if final.get("state") in {"expired", "cancelled"}:
                if admin:
                    message = ("BROKER_REQUEST_CANCELLED" if final["state"] == "cancelled"
                               else "BROKER_REQUEST_EXPIRED")
                    raise BrokerRequestError(message, request_id=rid,
                                             execution_state="expired_before_dispatch")
                raise BrokerError("BROKER_REQUEST_EXPIRED")
        time.sleep(max(0, min(0.2, deadline - time.monotonic())))
    if admin:
        return _settle_admin_request(node_id, rid, job_id)
    raise BrokerError("BROKER_RESULT_TIMEOUT")


@lru_cache(maxsize=1)
def original_modules():
    from .fleet import bootstrap_original
    bootstrap_original()
    root = Path(os.environ.get("FRAMEBRIDGE_BROKER_SOURCE")
                or str(Path(os.environ.get("FRAMEBRIDGE_EXPANDED_PREFIX", "/usr/lib/framebridge-expanded"))
                       / "original/geranas/FrameBridge-Broker-RC1"))
    if not root.exists():
        root = Path(__file__).resolve().parents[2] / "sources/original-geranas/FrameBridge-Broker-RC1"
    src = root if (root / "framebridge_linux_chatgpt").is_dir() else root / "src"
    if not src.is_dir():
        raise BrokerError("ORIGINAL_BROKER_SOURCE_UNAVAILABLE")
    sys.path.insert(0, str(src)) if str(src) not in sys.path else None
    package = importlib.import_module("framebridge_linux_chatgpt")
    path = str(src / "framebridge_linux_chatgpt")
    if path not in package.__path__:
        package.__path__.append(path)
    script = (root.parent if root == src else root) / "dev/fbpair_dev_broker.py"
    if not script.is_file():
        raise BrokerError("ORIGINAL_BROKER_SOURCE_UNAVAILABLE")
    spec = importlib.util.spec_from_file_location("_framebridge_expanded_original_broker", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _admin_payload(operation, payload):
    if not isinstance(payload, dict) or payload.get("authorized") is not True:
        raise BrokerError("AUTHORIZED_TRUE_REQUIRED")
    value = dict(payload)
    timeout = value.get("timeout_s", 120)
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise BrokerError("INVALID_TIMEOUT")
    job = value.get("job_id", "")
    if not isinstance(job, str) or _JOB.fullmatch(job) is None:
        raise BrokerError("INVALID_JOB_ID")
    if operation == "admin.exec":
        script = value.get("script")
        if not isinstance(script, str) or not script or len(script.encode()) > 262144:
            raise BrokerError("SCRIPT_INVALID")
        digest = hashlib.sha256(script.encode()).hexdigest()
        if value.get("sha256") != digest:
            raise BrokerError("SCRIPT_SHA256_MISMATCH")
    elif operation == "admin.cua":
        if timeout > 180:
            raise BrokerError("INVALID_CUA_TIMEOUT")
        cua = value.get("cua")
        if not isinstance(cua, dict):
            raise BrokerError("CUA_PAYLOAD_INVALID")
        if cua.get("action") not in {"OBSERVE", "CLICK", "DOUBLE_CLICK", "TYPE", "KEY", "SCROLL", "DRAG"}:
            raise BrokerError("CUA_ACTION_UNSUPPORTED")
        # Original admin.cua carries the adapter's cua object, not an EXEC script.
    else:
        raise BrokerError("OPERATION_NOT_ALLOWED")
    value["timeout_s"] = timeout
    return value


def make_server(host="127.0.0.1", port=20878, state_dir=None):
    """Build without starting; tests and runtime own the server lifecycle."""
    from framebridge_expanded.config import broker_connect_host, broker_endpoint

    state = state_path(state_dir)
    config = _config(state)
    if not isinstance(host, str) or not host:
        raise BrokerError("BROKER_BIND_INVALID")
    tls_cert, tls_key = config.get("tls_cert"), config.get("tls_key")
    if not _loopback(host) and not (tls_cert and tls_key):
        raise BrokerError("BROKER_REMOTE_BIND_REQUIRES_TLS")
    if type(port) is not int or not 1 <= port <= 65535:
        raise BrokerError("BROKER_PORT_INVALID")
    secret = _control_secret(state, create=True)
    original = original_modules()
    discovery = state / "broker" / "paired-nodes.json"
    public_base = config.get("broker_url") or broker_endpoint(
        "https" if tls_cert else "http", broker_connect_host(host), port)
    public_base = validate_endpoint(public_base)
    admin_lock = threading.RLock()

    class Store(_NodeHeartbeatStore, original.Store):
        def token(self, pair_id, device_code):
            result = super().token(pair_id, device_code)
            if result.get("state") == "paired":
                result.update(broker_issuer=public_base,
                              account_binding_id="local-operator-" + hashlib.sha256(secret.encode()).hexdigest()[:24],
                              scopes=["node.read", "admin.exec", "admin.cua"])
                with self.lock:
                    self.by_pair[pair_id]["receipt"] = dict(result)
            return result

        def queue_admin(self, node_id, operation, payload, start_by=None):
            node = self._paired_node(node_id)
            if node is None:
                return None, "node_not_paired"
            if operation not in node.get("scopes", []):
                return None, "node_scope_denied"
            try:
                value = _admin_payload(operation, payload)
            except BrokerError as exc:
                return None, str(exc)
            if start_by is not None and (type(start_by) not in (int, float)
                                         or not -math.inf < start_by < math.inf):
                return None, "INVALID_START_DEADLINE"
            with self.lock:
                if not hasattr(self, "_admin_requests_by_job"):
                    self._admin_requests_by_job = {}
                key = (node_id, value["job_id"])
                existing = self._admin_requests_by_job.get(key)
                if existing:
                    previous = self.control_requests[existing]
                    if previous["operation"] != operation or previous["payload"] != value:
                        return None, "JOB_ID_CONFLICT"
                    return previous, None
                now = time.time()
                expires_at = now + value["timeout_s"] + _RESULT_GRACE
                if start_by is not None:
                    expires_at = min(expires_at, start_by)
                rid = secrets.token_hex(16)
                item = {"schema": 1, "request_id": rid, "node_id": node_id,
                        "operation": operation, "payload": value, "created_at": now,
                        "expires_at": expires_at}
                pending = now < expires_at
                self._admin_requests_by_job[key] = rid
                self.control_requests[rid] = item
                self.control_results[rid] = {"state": "pending" if pending else "expired",
                                             "node_id": node_id}
                if pending:
                    self.control_queue[node_id].append(rid)
            return item, None

    store = Store(discovery, public_base)
    BaseHandler = original.make_handler(store, public_base)

    class Handler(BaseHandler):
        server_version = "FrameBridgeOriginal/1"

        def do_GET(self):
            self.send_json(200 if self.path == "/health" else 404,
                           {"ok": self.path == "/health", "protocol": "FBPAIR/1"})

        def do_POST(self):
            if self.path.startswith("/fbpair/v1/control/"):
                if not _loopback(str(self.client_address[0])):
                    self.send_json(403, {"ok": False, "error": "CONTROL_LOOPBACK_REQUIRED"})
                    return
                if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + secret):
                    self.send_json(403, {"ok": False, "error": "CONTROL_AUTH_REQUIRED"})
                    return
                try:
                    body = self.read_json()
                    if not isinstance(body, dict):
                        raise BrokerError("REQUEST_INVALID")
                    with admin_lock:
                        self._control(body)
                except BrokerError as exc:
                    self.send_json(400, {"ok": False, "error": str(exc)})
                except Exception:
                    self.send_json(500, {"ok": False, "error": "BROKER_INTERNAL_ERROR"})
                return
            if self.path in {"/fbpair/v1/node/poll", "/fbpair/v1/node/result", NODE_HEARTBEAT_PATH}:
                status, response = _node_message_response(store, self.path, self.read_json() or {})
                self.send_json(status, response)
                return
            if self.path not in {"/fbpair/v1/device_authorization", "/fbpair/v1/token",
                                 "/fbpair/v1/confirm", "/fbpair/v1/cancel",
                                 "/fbpair/v1/node/poll", "/fbpair/v1/node/result"}:
                self.send_json(404, {"ok": False, "error": "ENDPOINT_NOT_AVAILABLE"})
                return
            super().do_POST()

        def _control(self, body):
            route = self.path.rsplit("/", 1)[-1]
            if route == "pending":
                pending = []
                for item in list(store.by_pair.values()):
                    req = item["request"]
                    if item.get("state") == "pending" and item["expires"] > time.time():
                        pending.append({"request_id": item["pair_id"], "node_id": req["node_id"],
                                        "node_label": req["node_label"], "state": "pending",
                                        "fingerprint": original.parse_ed25519_public_key_line(
                                            req.get("public_key_ssh", ""))["public_key_sha256"]})
                self.send_json(200, {"ok": True, "requests": pending})
            elif route == "approve":
                rid, expected = body.get("request_id"), body.get("expected_node_id")
                item = store.by_pair.get(rid)
                if not item or item["request"]["node_id"] != expected:
                    raise BrokerError("APPROVAL_NODE_MISMATCH")
                req = item["request"]
                alias = req["node_label"]
                reserved = {"local", str(config.get("name", "")).casefold()}
                if not isinstance(alias, str) or _LABEL.fullmatch(alias) is None or alias.casefold() in reserved:
                    raise BrokerError("NODE_ALIAS_RESERVED_OR_INVALID")
                if expected == config.get("node_id"):
                    raise BrokerError("NODE_ID_RESERVED")
                for node in store._discovery_records():
                    if node.get("state") == "paired" and (node.get("node_id") == expected
                            or str(node.get("node_label", "")).casefold() == alias.casefold()):
                        raise BrokerError("NODE_ALREADY_REGISTERED")
                for other in store.by_pair.values():
                    if other is item:
                        continue
                    if other.get("state") in {"approved", "receipt_issued"} and other["expires"] > time.time():
                        if (other["request"].get("node_id") == expected
                                or str(other["request"].get("node_label", "")).casefold() == alias.casefold()):
                            raise BrokerError("NODE_ALIAS_OR_ID_PENDING")
                if not store.approve(item["user_code"]):
                    raise BrokerError("PAIR_NOT_APPROVABLE")
                self.send_json(200, {"ok": True, "request_id": rid, "node_id": expected,
                                     "state": "approved", "approval_method": "authenticated-local-operator"})
            elif route == "nodes":
                nodes = []
                for n in store._discovery_records():
                    if n.get("state") != "paired":
                        continue
                    status = store.node_runtime_status(n["node_id"])
                    nodes.append({"node_id": n["node_id"], "alias": n["node_label"],
                                  "hostname": n["node_label"], "transport": "broker_outbound",
                                  "ready": status.get("ready") is True, "pairing_state": "paired",
                                  "supports_start_deadline": True,
                                  "busy": status.get("busy") is True,
                                  "heartbeat_fresh": status.get("heartbeat_fresh") is True,
                                  "last_seen_age_seconds": status.get("last_seen_age_seconds"),
                                  "activity_state": status.get("activity_state", "offline")})
                self.send_json(200, {"ok": True, "nodes": nodes})
            elif route == "node-status":
                self.send_json(200, store.node_runtime_status(str(body.get("node_id", ""))))
            elif route == "revoke":
                node_id = body.get("node_id")
                records = store._discovery_records()
                found = False
                for n in records:
                    if n.get("node_id") == node_id:
                        n["state"] = "revoked"
                        found = True
                if not found:
                    raise BrokerError("NODE_NOT_FOUND")
                tmp = discovery.with_name(".paired-nodes.revocation.tmp")
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump({"schema": 1, "nodes": records}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, discovery)
                self.send_json(200, {"ok": True, "node_id": node_id, "state": "revoked"})
            elif route in {"request", "admin-exec", "admin-cua"}:
                node_id = str(body.get("node_id", ""))
                if route == "request":
                    item, error = store.queue_read(node_id, body.get("capability"), body.get("args"))
                else:
                    operation = "admin.exec" if route == "admin-exec" else "admin.cua"
                    item, error = store.queue_admin(node_id, operation, body.get("payload"),
                                                    body.get("start_by"))
                if error:
                    raise BrokerError(str(error))
                self.send_json(200, {"ok": True, "request_id": item["request_id"]})
            elif route == "result":
                result = store.get_result(str(body.get("request_id", "")))
                if result is None:
                    raise BrokerError("REQUEST_NOT_FOUND")
                self.send_json(200, {"ok": True, **result})
            elif route == "request-status":
                node_id = body.get("node_id")
                request_id, job_id = body.get("request_id", ""), body.get("job_id", "")
                cancel_pending = body.get("cancel_pending", False)
                if (not isinstance(node_id, str) or not node_id
                        or any(not isinstance(value, str) or (value and not _JOB.fullmatch(value))
                               for value in (request_id, job_id))
                        or not (request_id or job_id) or type(cancel_pending) is not bool):
                    raise BrokerError("REQUEST_SELECTOR_INVALID")
                receipt = store.admin_request_status(node_id, request_id, job_id, cancel_pending)
                self.send_json(200, {"ok": True, **receipt})
            else:
                self.send_json(404, {"ok": False, "error": "ENDPOINT_NOT_AVAILABLE"})

    class Server(ThreadingHTTPServer):
        """Bound workers and negotiate TLS outside the accept loop."""

        daemon_threads = True
        max_workers = 128
        tls_handshake_timeout = 5.0
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

        def __init__(self, *args, **kwargs):
            self._worker_slots = threading.BoundedSemaphore(self.max_workers)
            super().__init__(*args, **kwargs)

        def process_request(self, request, client_address):
            # Never wait for a worker in the accept loop. Excess connections
            # close immediately and may retry through the existing client policy.
            if not self._worker_slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._worker_slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                if isinstance(request, ssl.SSLSocket):
                    previous_timeout = request.gettimeout()
                    try:
                        request.settimeout(self.tls_handshake_timeout)
                        request.do_handshake()
                        # HTTP and node long-poll retain their previous timeout.
                        request.settimeout(previous_timeout)
                    except (OSError, ValueError):
                        return
                try:
                    self.finish_request(request, client_address)
                except Exception:
                    self.handle_error(request, client_address)
            finally:
                try:
                    self.shutdown_request(request)
                finally:
                    self._worker_slots.release()
    server = Server((host, port), Handler)
    server.daemon_threads = True
    server.expanded_store = store
    if tls_cert or tls_key:
        if not tls_cert or not tls_key:
            server.server_close()
            raise BrokerError("TLS_CERT_AND_KEY_REQUIRED")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(tls_cert), str(tls_key))
        server.socket = context.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False)
    return server


def serve(host="127.0.0.1", port=20878, state_dir=None):
    server = make_server(host, port, state_dir)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
