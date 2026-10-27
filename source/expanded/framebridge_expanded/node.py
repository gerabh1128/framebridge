"""Original FBPAIR identity and outbound protocol for an explicitly enrolled node."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import socket
import time
import uuid
from pathlib import Path

from . import broker


class NodeError(ValueError):
    pass


def _original(name):
    broker.original_modules()
    return importlib.import_module("framebridge_linux_chatgpt." + name)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _context(state_dir=None, create=False, node_label=None):
    state = broker.state_path(state_dir)
    config = broker._config(state)
    label = node_label or config.get("name") or socket.gethostname()
    if not isinstance(label, str) or broker._LABEL.fullmatch(label) is None or label.casefold() == "local":
        raise NodeError("NODE_LABEL_INVALID")
    ctx = _original("seed_core").SeedContext(state_dir=state / "node", config_dir=state / "node/config")
    identity = ctx.load_identity()
    if identity:
        node_id = identity.get("node_id")
        if config.get("node_id") and config["node_id"] != node_id:
            raise NodeError("NODE_IDENTITY_CONFIG_MISMATCH")
        if create and identity.get("hostname") != label:
            raise NodeError("NODE_IDENTITY_LABEL_MISMATCH")
    elif create:
        node_id = config.get("node_id") or str(uuid.uuid4())
        try:
            if str(uuid.UUID(node_id)) != node_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise NodeError("NODE_ID_INVALID") from None
        crypto = _original("node_identity").ensure_node_keypair(ctx.state_dir,
                                                               comment="framebridge-" + node_id[:8])
        identity = {"schema": 2, "node_id": node_id, "node_id_short": node_id.replace("-", "")[:8],
                    "node_host_id": crypto["host_id"], "node_public_jwk": crypto["public_jwk"],
                    "node_public_key_sha256": crypto["public_key_sha256"], "hostname": label,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _write_json(ctx.identity_path, identity)
    else:
        raise NodeError("NODE_IDENTITY_MISSING")
    if create and (config.get("node_id") != node_id or config.get("name") != label):
        config.update(schema=1, node_id=node_id, name=label)
        _write_json(state / "config.json", config)
    return state, config, ctx, identity


def _poster(state):
    def post(url, value, *, timeout=10):
        return broker.post_json(url, value, state_dir=state, timeout=timeout)
    return post


def enroll(broker_url, node_label, state_dir=None):
    url = broker.validate_endpoint(broker_url)
    state, config, ctx, identity = _context(state_dir, create=True, node_label=node_label)
    store = _original("pairing").PairingStore(ctx.state_dir)
    existing = store.load()
    if existing.get("state") in {"pair_requested", "paired"}:
        raise NodeError("NODE_ENROLLMENT_ALREADY_EXISTS")
    manifest = _original("seed_capabilities").list_capabilities()
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = _original("pairing_client").start_pairing(
        state_dir=ctx.state_dir, node_id=identity["node_id"], node_label=node_label,
        node_host_id=identity["node_host_id"], public_jwk=identity["node_public_jwk"],
        provider="chatgpt", capability_manifest_sha256=digest, broker_base_url=url,
        allow_insecure_dev=url.startswith("http://"), poster=_poster(state))
    config["broker_url"] = url
    _write_json(state / "config.json", config)
    return dict(result, node_id=identity["node_id"], node_label=node_label,
                fingerprint=identity["node_public_key_sha256"])


def poll_enrollment(state_dir=None):
    state, config, ctx, identity = _context(state_dir)
    url = broker.validate_endpoint(config.get("broker_url", ""))
    store = _original("pairing").PairingStore(ctx.state_dir)
    pairing = store.load()
    if pairing.get("broker_base_url") and pairing["broker_base_url"].rstrip("/") != url:
        raise NodeError("PAIRING_BROKER_CONFIG_MISMATCH")
    if pairing.get("state") == "paired" and pairing.get("broker_confirm_required") is not True:
        return status(state)
    result = _original("pairing_client").poll_pairing(
        state_dir=ctx.state_dir, allow_insecure_dev=url.startswith("http://"), poster=_poster(state))
    pairing = store.load()
    if pairing.get("state") == "paired":
        _write_json(ctx.config_path, {"schema": 1, "provider": "chatgpt", "pairing": {
            "state": "paired", "broker_base_url": url,
            "broker_confirm_required": pairing.get("broker_confirm_required") is True}})
    return dict(result, node_id=identity["node_id"])


def status(state_dir=None):
    try:
        state, config, ctx, identity = _context(state_dir)
    except NodeError as exc:
        return {"ok": False, "error": str(exc), "state": "not_enrolled"}
    pairing = _original("pairing").PairingStore(ctx.state_dir).load()
    return {"ok": True, "node_id": identity["node_id"], "node_label": identity["hostname"],
            "fingerprint": identity["node_public_key_sha256"],
            "state": pairing.get("state", "not_paired"),
            "broker_confirm_required": pairing.get("broker_confirm_required") is True,
            "broker_url": config.get("broker_url"), "agent_state": "not_checked"}


def dispatch_request(request, adapter_request=None, state_dir=None):
    """Recheck binding and authorization before invoking the real local Original adapter."""
    if not isinstance(request, dict):
        return {"ok": False, "error": "REQUEST_INVALID"}
    try:
        _, _, _, identity = _context(state_dir)
        if request.get("node_id") != identity["node_id"]:
            raise NodeError("REQUEST_NODE_MISMATCH")
        expires = request.get("expires_at")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not expires > time.time():
            raise NodeError("REQUEST_EXPIRED")
        rid = request.get("request_id")
        if not isinstance(rid, str) or not rid or len(rid) > 80:
            raise NodeError("REQUEST_ID_INVALID")
        operation = request.get("operation")
        if operation:
            payload = broker._admin_payload(operation, request.get("payload"))
            payload = dict(payload, version=1, mode="EXEC")
            if operation == "admin.cua":
                payload["request_type"] = "cua"
            if adapter_request is None:
                from .runtime import adapter_request
            result = adapter_request(payload)
            if not isinstance(result, dict):
                raise NodeError("ADAPTER_RESPONSE_INVALID")
            result = dict(result, mcp_job_id=payload["job_id"])
            if operation == "admin.exec":
                result["mcp_script_sha256"] = payload["sha256"]
            if payload.get("include_stdout") is not True:
                result.update(stdout="", stdout_sanitized=False, stdout_truncated=False,
                              stdout_omitted_reason="NOT_REQUESTED")
            return result
        cap, args = request.get("capability"), request.get("args")
        if cap not in broker.original_modules().READ_CAPS or not isinstance(args, dict):
            raise NodeError("CAPABILITY_NOT_ALLOWED")
        value = _original("seed_capabilities").execute_capability(cap, args)
        if isinstance(value, dict) and value.get("ok") is False:
            return {"ok": False, "capability": cap, "error": "READ_CAPABILITY_FAILED"}
        return {"ok": True, "capability": cap, "result": value}
    except (NodeError, broker.BrokerError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        return {"ok": False, "error": "NODE_DISPATCH_FAILED"}


def run(state_dir=None, once=False):
    state, config, ctx, identity = _context(state_dir)
    url = broker.validate_endpoint(config.get("broker_url", ""))
    pairing = _original("pairing").PairingStore(ctx.state_dir).load()
    if pairing.get("state") != "paired" or pairing.get("broker_confirm_required") is True:
        raise NodeError("NODE_NOT_CONFIRMED")
    if pairing.get("node_id") != identity["node_id"] or pairing.get("broker_base_url", "").rstrip("/") != url:
        raise NodeError("PAIRING_BINDING_MISMATCH")
    outbound = _original("outbound_agent")
    while True:
        poll = broker.post_json(url + "/fbpair/v1/node/poll",
                                outbound._signed_envelope(ctx, "node.poll", {}), state_dir=state)
        if poll.get("ok") is not True:
            raise NodeError("NODE_POLL_REJECTED")
        req = poll.get("request")
        result = {"ok": True, "idle": True}
        if isinstance(req, dict):
            value = dispatch_request(req, state_dir=state)
            posted = broker.post_json(url + "/fbpair/v1/node/result", outbound._signed_envelope(
                ctx, "node.result", {"request_id": req.get("request_id"), "result": value}), state_dir=state)
            if posted.get("ok") is not True:
                raise NodeError("NODE_RESULT_REJECTED")
            result = {"ok": True, "idle": False, "request_id": req.get("request_id")}
        if once:
            return result
        time.sleep(1)
