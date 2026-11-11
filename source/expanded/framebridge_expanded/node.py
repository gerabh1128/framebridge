"""Original FBPAIR identity and outbound protocol for an explicitly enrolled node."""
from __future__ import annotations

import errno
import hashlib
import http.client
import importlib
import json
import logging
import math
import os
import socket
import ssl
import threading
import time
import uuid
import urllib.error
from pathlib import Path

from . import broker


_LOG = logging.getLogger(__name__)
_RETRY_MAX_SECONDS = 30
_HEARTBEAT_FEATURE = "node-heartbeat-v1"
_HEARTBEAT_INTERVAL_SECONDS = 1
_HEARTBEAT_TIMEOUT_SECONDS = 2
_STATUS_WRITE_INTERVAL_SECONDS = 5


class _BrokerContact:
    """A bounded local observation, never an authorization or pairing source."""

    def __init__(self, state, node_id):
        self.path = state / "node-runtime-status.json"
        self.node_id = node_id
        self.pid = os.getpid()
        fields = Path("/proc/self/stat").read_text().rsplit(") ", 1)[1].split()
        self.birth = fields[19]
        self.written_at = None

    def accepted(self, operation, *, heartbeat_v1):
        moment = time.monotonic()
        if self.written_at is not None and moment - self.written_at < _STATUS_WRITE_INTERVAL_SECONDS:
            return
        try:
            _write_json(self.path, {"schema": 1, "node_id": self.node_id,
                "worker_pid": self.pid, "worker_birth": self.birth,
                "last_broker_acceptance_unix": time.time(),
                "last_accepted_operation": operation,
                "heartbeat_v1": heartbeat_v1 is True})
        except OSError:
            # Diagnostics failing must not discard the result of an action.
            _LOG.warning("NODE_CONTACT_STATUS_WRITE_UNAVAILABLE")
        else:
            self.written_at = moment


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
        return {"ok": False, "error": "REQUEST_INVALID", "execution_state": "not_started"}
    operation = request.get("operation")
    attempted = False
    try:
        _, _, _, identity = _context(state_dir)
        if request.get("node_id") != identity["node_id"]:
            raise NodeError("REQUEST_NODE_MISMATCH")
        expires = request.get("expires_at")
        if type(expires) not in (int, float) or not time.time() < expires < math.inf:
            raise NodeError("REQUEST_EXPIRED")
        rid = request.get("request_id")
        if not isinstance(rid, str) or not rid or len(rid) > 80:
            raise NodeError("REQUEST_ID_INVALID")
        if operation:
            payload = broker._admin_payload(operation, request.get("payload"))
            payload = dict(payload, version=1, mode="EXEC", start_before=expires)
            if operation == "admin.cua":
                payload["request_type"] = "cua"
            if adapter_request is None:
                from .runtime import adapter_request
            if expires <= time.time():
                raise NodeError("REQUEST_EXPIRED")
            attempted = True
            result = adapter_request(payload)
            if not isinstance(result, dict):
                raise NodeError("ADAPTER_RESPONSE_INVALID")
            result = dict(result, mcp_job_id=payload["job_id"], executed_hostname=socket.gethostname())
            if operation == "admin.exec":
                result["mcp_script_sha256"] = payload["sha256"]
            if payload.get("include_stdout") is not True:
                result.update(stdout="", stdout_sanitized=False, stdout_truncated=False,
                              stdout_omitted_reason="NOT_REQUESTED")
            return result
        cap, args = request.get("capability"), request.get("args")
        if cap not in broker.original_modules().READ_CAPS or not isinstance(args, dict):
            raise NodeError("CAPABILITY_NOT_ALLOWED")
        execute = _original("seed_capabilities").execute_capability
        if expires <= time.time():
            raise NodeError("REQUEST_EXPIRED")
        attempted = True
        value = execute(cap, args)
        if isinstance(value, dict) and value.get("ok") is False:
            return {"ok": False, "capability": cap, "error": "READ_CAPABILITY_FAILED",
                    "executed_hostname": socket.gethostname()}
        return {"ok": True, "capability": cap, "result": value,
                "executed_hostname": socket.gethostname()}
    except (NodeError, broker.BrokerError) as exc:
        error = str(exc)
    except Exception:
        error = "NODE_DISPATCH_FAILED"
    if attempted:
        failure = {"ok": False, "error": error, "execution_state": "outcome_unknown"}
        if operation:
            failure["do_not_replay"] = True
        return failure
    state = "expired_before_start" if error == "REQUEST_EXPIRED" else "not_started"
    return {"ok": False, "error": error, "execution_state": state}


def _retryable_broker_failure(exc):
    # post_json intentionally hides transport details from its public error. Its
    # exception context still distinguishes interruptions from TLS/JSON failures;
    # never log that context or treat an unknown failure as transient.
    code = str(exc)
    if code in {"BROKER_HTTP_REJECTED_502", "BROKER_HTTP_REJECTED_503", "BROKER_HTTP_REJECTED_504"}:
        return True
    if code != "BROKER_TRANSPORT_FAILED":
        return False
    cause = exc.__context__
    if isinstance(cause, urllib.error.URLError):
        cause = cause.reason
    if isinstance(cause, ssl.SSLError):
        return False
    if isinstance(cause, socket.gaierror):
        return cause.errno == socket.EAI_AGAIN
    if isinstance(cause, (ConnectionError, TimeoutError, http.client.IncompleteRead)):
        return True
    return isinstance(cause, OSError) and cause.errno in {
        errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT,
        errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
    }


def _post_with_recovery(url, operation, payload, *, ctx, state, outbound, recover):
    delay = 1
    retries = 0
    while True:
        # A new signature/nonce is mandatory: the previous attempt may have been
        # verified even when its response was lost. Do not replay its envelope.
        envelope = outbound._signed_envelope(ctx, operation, payload)
        try:
            result = broker.post_json(url, envelope, state_dir=state)
        except broker.BrokerError as exc:
            if not recover or not _retryable_broker_failure(exc):
                raise
            retries += 1
            _LOG.warning("NODE_BROKER_RETRY operation=%s error=%s delay_seconds=%d",
                         operation, str(exc), delay)
            # Signals and cancellation exceptions are deliberately not caught.
            time.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_SECONDS)
        else:
            if retries:
                _LOG.warning("NODE_BROKER_RECOVERED operation=%s retries=%d", operation, retries)
            return result


def _heartbeat_supported(poll):
    features = poll.get("protocol_features")
    return type(features) is list and _HEARTBEAT_FEATURE in features


def _dispatch_with_heartbeat(request, *, url, ctx, state, outbound, contact=None):
    """Execute once while the signing thread reports liveness, without polling.

    The worker is the only dispatcher; it never signs envelopes or starts the
    next request. A failed heartbeat does not imply that input was not sent and
    therefore must not cancel/replay the action or discard its eventual result.
    """
    finished = threading.Event()
    outcome = []

    def execute():
        try:
            outcome.append((True, dispatch_request(request, state_dir=state)))
        except BaseException as exc:
            # Preserve cancellation/failure propagation from the original direct
            # call, including SystemExit, without leaving the waiter blocked.
            outcome.append((False, exc))
        finally:
            finished.set()

    worker = threading.Thread(target=execute, name="framebridge-node-dispatch", daemon=True)
    worker.start()
    heartbeat_enabled = True
    unavailable_reported = False
    while not finished.wait(_HEARTBEAT_INTERVAL_SECONDS):
        if not heartbeat_enabled:
            continue
        try:
            envelope = outbound._signed_envelope(ctx, "node.heartbeat",
                                                 {"request_id": request["request_id"]})
            reply = broker.post_json(url + "/fbpair/v1/node/heartbeat", envelope,
                                     state_dir=state, timeout=_HEARTBEAT_TIMEOUT_SECONDS)
        except broker.BrokerError as exc:
            # Do not retry an uncertain heartbeat envelope or log its contents.
            # A later attempt has a new nonce and never dispatches another job.
            if not unavailable_reported:
                _LOG.warning("NODE_HEARTBEAT_UNAVAILABLE")
                unavailable_reported = True
            if not _retryable_broker_failure(exc):
                heartbeat_enabled = False
        except Exception:
            # A signing/format failure is not evidence that input was absent.
            _LOG.warning("NODE_HEARTBEAT_PREPARATION_FAILED")
            heartbeat_enabled = False
        else:
            if not (type(reply) is dict and reply.get("ok") is True
                    and reply.get("busy") is True
                    and reply.get("request_id") == request["request_id"]):
                _LOG.warning("NODE_HEARTBEAT_REJECTED")
                heartbeat_enabled = False
            elif contact is not None:
                contact.accepted("node.heartbeat", heartbeat_v1=True)
            if heartbeat_enabled and unavailable_reported:
                _LOG.warning("NODE_HEARTBEAT_RECOVERED")
                unavailable_reported = False
    # Event publication happens after the value/exception is appended; a join is
    # unnecessary and cannot prolong shutdown after a completed dispatcher.
    successful, value = outcome[0]
    if not successful:
        raise value
    return value


def run(state_dir=None, once=False):
    state, config, ctx, identity = _context(state_dir)
    url = broker.validate_endpoint(config.get("broker_url", ""))
    pairing = _original("pairing").PairingStore(ctx.state_dir).load()
    if pairing.get("state") != "paired" or pairing.get("broker_confirm_required") is True:
        raise NodeError("NODE_NOT_CONFIRMED")
    if pairing.get("node_id") != identity["node_id"] or pairing.get("broker_base_url", "").rstrip("/") != url:
        raise NodeError("PAIRING_BINDING_MISMATCH")
    outbound = _original("outbound_agent")
    contact = _BrokerContact(state, identity["node_id"])
    while True:
        poll = _post_with_recovery(url + "/fbpair/v1/node/poll", "node.poll", {},
                                   ctx=ctx, state=state, outbound=outbound, recover=not once)
        if poll.get("ok") is not True:
            raise NodeError("NODE_POLL_REJECTED")
        supports_heartbeat = _heartbeat_supported(poll)
        contact.accepted("node.poll", heartbeat_v1=supports_heartbeat)
        req = poll.get("request")
        result = {"ok": True, "idle": True}
        if isinstance(req, dict):
            if supports_heartbeat:
                value = _dispatch_with_heartbeat(req, url=url, ctx=ctx,
                                                 state=state, outbound=outbound, contact=contact)
            else:
                # Compatibility with a frozen central: never invent another
                # poll while executing or downgrade a signed request to shell.
                value = dispatch_request(req, state_dir=state)
            # Original put_result only records this request's result; repeating
            # that acknowledgement cannot execute the command. Keep the value
            # here and never return to dispatch/poll while its delivery is pending.
            posted = _post_with_recovery(url + "/fbpair/v1/node/result", "node.result",
                                         {"request_id": req.get("request_id"), "result": value},
                                         ctx=ctx, state=state, outbound=outbound, recover=not once)
            if posted.get("ok") is not True:
                raise NodeError("NODE_RESULT_REJECTED")
            result = {"ok": True, "idle": False, "request_id": req.get("request_id")}
            if posted.get("disposition") == "orphaned":
                _LOG.warning("NODE_RESULT_ORPHANED request_id=%s", req.get("request_id"))
                result["delivery_disposition"] = "orphaned"
            else:
                contact.accepted("node.result", heartbeat_v1=supports_heartbeat)
        if once:
            return result
        time.sleep(1)
