"""Authenticated operator boundary around the unchanged Original FBPAIR broker."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import ipaddress
import json
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
from functools import lru_cache
from http.server import ThreadingHTTPServer
from pathlib import Path


class BrokerError(ValueError):
    """A fixed public failure code; never include HTTP bodies or credentials."""


_state = None
_url = None
_control_path = None
_JOB = re.compile(r"[A-Za-z0-9._-]{1,80}")
_LABEL = re.compile(r"[A-Za-z0-9._-]{1,64}")


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


def configure(broker_url=None, state_dir=None, control_key_path=None):
    global _state, _url, _control_path
    _state = state_path(state_dir)
    config = _config(_state)
    port = config.get("ports", {}).get("broker", 20878)
    _url = validate_endpoint(broker_url or config.get("broker_control_url") or config.get("broker_url")
                             or f"http://127.0.0.1:{port}")
    _control_path = Path(control_key_path or _state / "broker" / "control.key")


def _settings():
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
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=context))
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


def _control(path, payload):
    state, url, _ = _settings()
    return post_json(url + "/fbpair/v1/control/" + path, payload,
                     state_dir=state, control=True)


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


def request(node_id, capability, args, timeout_s=120):
    if type(timeout_s) is not int or not 1 <= timeout_s <= 600:
        raise BrokerError("INVALID_TIMEOUT")
    if not isinstance(args, dict):
        raise BrokerError("ARGS_NOT_OBJECT")
    confirmed = {n["node_id"]: n for n in list_confirmed_nodes()}
    if node_id not in confirmed:
        raise BrokerError("NODE_NOT_CONFIRMED")
    if confirmed[node_id].get("ready") is not True:
        raise BrokerError("NODE_OFFLINE")
    path = {"admin.exec": "admin-exec", "admin.cua": "admin-cua"}.get(capability, "request")
    payload = {"node_id": node_id}
    if path == "request":
        payload.update(capability=capability, args=args)
    else:
        payload["payload"] = args
    created = _control(path, payload)
    if created.get("ok") is not True or not created.get("request_id"):
        raise BrokerError("BROKER_REQUEST_REJECTED")
    rid = created["request_id"]
    deadline = time.monotonic() + timeout_s + 10
    while time.monotonic() < deadline:
        final = _control("result", {"request_id": rid})
        if final.get("node_id") != node_id:
            raise BrokerError("BROKER_RESULT_NODE_MISMATCH")
        if final.get("state") == "complete":
            result = final.get("result")
            if not isinstance(result, dict):
                raise BrokerError("BROKER_RESULT_INVALID")
            return dict(result, request_id=rid, node_id=node_id)
        if final.get("state") == "expired":
            raise BrokerError("BROKER_REQUEST_EXPIRED")
        time.sleep(0.2)
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
    public_base = config.get("broker_url") or (f"https://{host}:{port}" if tls_cert else f"http://{host}:{port}")
    public_base = validate_endpoint(public_base)
    admin_lock = threading.RLock()

    class Store(original.Store):
        def token(self, pair_id, device_code):
            result = super().token(pair_id, device_code)
            if result.get("state") == "paired":
                result.update(broker_issuer=public_base,
                              account_binding_id="local-operator-" + hashlib.sha256(secret.encode()).hexdigest()[:24],
                              scopes=["node.read", "admin.exec", "admin.cua"])
                with self.lock:
                    self.by_pair[pair_id]["receipt"] = dict(result)
            return result

        def queue_admin(self, node_id, operation, payload):
            node = self._paired_node(node_id)
            if node is None:
                return None, "node_not_paired"
            if operation not in node.get("scopes", []):
                return None, "node_scope_denied"
            try:
                value = _admin_payload(operation, payload)
            except BrokerError as exc:
                return None, str(exc)
            rid = secrets.token_hex(16)
            item = {"schema": 1, "request_id": rid, "node_id": node_id,
                    "operation": operation, "payload": value, "created_at": time.time(),
                    "expires_at": time.time() + value["timeout_s"] + 90}
            with self.lock:
                self.control_requests[rid] = item
                self.control_results[rid] = {"state": "pending", "node_id": node_id}
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
                                  "ready": status.get("ready") is True, "pairing_state": "paired"})
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
                    item, error = store.queue_admin(node_id, operation, body.get("payload"))
                if error:
                    raise BrokerError(str(error))
                self.send_json(200, {"ok": True, "request_id": item["request_id"]})
            elif route == "result":
                result = store.get_result(str(body.get("request_id", "")))
                if result is None:
                    raise BrokerError("REQUEST_NOT_FOUND")
                self.send_json(200, {"ok": True, **result})
            else:
                self.send_json(404, {"ok": False, "error": "ENDPOINT_NOT_AVAILABLE"})

    if ":" in host:
        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
    else:
        Server = ThreadingHTTPServer
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
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve(host="127.0.0.1", port=20878, state_dir=None):
    server = make_server(host, port, state_dir)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
