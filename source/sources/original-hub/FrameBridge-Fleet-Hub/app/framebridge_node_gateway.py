from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

NODE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9@_.-]{1,128}$")
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
REMOTE_APPROVAL_SOURCE = "REMOTE_HUMAN"
TOTP_APPROVAL_SOURCE = "REMOTE_HUMAN_TOTP"
REMOTE_CHALLENGE_RE = re.compile(r"^[a-f0-9]{32}$")
REMOTE_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
PRIVILEGED_CHANGE_CAPS = {
    "package.install",
    "package.remove",
    "service.manage",
    "system.hostname",
    "system.timezone",
    "package.refresh",
    "package.upgrade",
    "system.file.write_text",
    "system.directory.ensure",
    "system.file.mode",
    "system.daemon_reload",
}

READ_CAPS = {
    "system.status",
    "system.resources",
    "system.processes",
    "system.time",
    "hardware.summary",
    "kernel.modules",
    "storage.inspect",
    "storage.usage",
    "storage.mounts",
    "network.inspect",
    "network.sockets",
    "network.dns",
    "package.status",
    "package.search",
    "package.upgradable",
    "service.status",
    "service.list",
    "journal.service",
    "user.list",
    "group.list",
    "filesystem.stat",
    "filesystem.list",
    "filesystem.hash",
    "filesystem.read_text",
}

READ_ANNOTATIONS = ToolAnnotations(
    title="FrameBridge node read",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

CHANGE_ANNOTATIONS = ToolAnnotations(
    title="FrameBridge node approved plan apply",
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


class NodeGatewayError(RuntimeError):
    pass


def _approval_source_ok(capability: str | None, source: str) -> bool:
    if capability == "package.install":
        return source in (
            "LOCAL_TTY_PRIVILEGED",
            REMOTE_APPROVAL_SOURCE,
            TOTP_APPROVAL_SOURCE,
        )
    if capability in PRIVILEGED_CHANGE_CAPS:
        return source == "LOCAL_TTY_PRIVILEGED"
    return source == "LOCAL_TTY"


def _registry_path() -> Path:
    return Path(
        os.environ.get(
            "FRAMEBRIDGE_NODE_REGISTRY",
            "~/.config/framebridge/nodes.json",
        )
    ).expanduser()


def _account_binding_path() -> Path:
    return Path(
        os.environ.get(
            "FRAMEBRIDGE_ACCOUNT_BINDING",
            "~/.config/framebridge/account-binding.json",
        )
    ).expanduser()


def _paired_discovery_path() -> Path:
    return Path(
        os.environ.get(
            "FRAMEBRIDGE_PAIRED_DISCOVERY",
            "~/.local/share/framebridge/pairing-broker/paired-nodes.json",
        )
    ).expanduser()


def _load_secure_json_optional(path: Path, prefix: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    mode=stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise NodeGatewayError(prefix+"_PERMISSIONS_TOO_OPEN")
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise NodeGatewayError(prefix+"_INVALID_JSON")
    if not isinstance(value,dict):
        raise NodeGatewayError(prefix+"_NOT_OBJECT")
    return value


def _load_account_binding() -> dict[str, Any] | None:
    value=_load_secure_json_optional(_account_binding_path(),"ACCOUNT_BINDING")
    if value is None:
        return None
    if value.get("schema")!=1:
        raise NodeGatewayError("ACCOUNT_BINDING_SCHEMA_INVALID")
    if value.get("provider")!="chatgpt":
        raise NodeGatewayError("ACCOUNT_BINDING_PROVIDER_INVALID")
    if value.get("issuer")!="https://auth.openai.com":
        raise NodeGatewayError("ACCOUNT_BINDING_ISSUER_INVALID")
    binding=str(value.get("account_binding_id") or "")
    client=str(value.get("client_id") or "")
    if not binding.startswith("fbacct_"):
        raise NodeGatewayError("ACCOUNT_BINDING_ID_INVALID")
    if not client.startswith("oaiapp_"):
        raise NodeGatewayError("ACCOUNT_BINDING_CLIENT_INVALID")
    return value


def _load_paired_discovery() -> list[dict[str, Any]]:
    account=_load_account_binding()
    if account is None:
        return []
    value=_load_secure_json_optional(_paired_discovery_path(),"PAIRED_DISCOVERY")
    if value is None:
        return []
    if value.get("schema")!=1 or not isinstance(value.get("nodes"),list):
        raise NodeGatewayError("PAIRED_DISCOVERY_SCHEMA_INVALID")
    result=[]
    for raw in value["nodes"]:
        if not isinstance(raw,dict):
            continue
        if raw.get("schema")!=1:
            continue
        if raw.get("source")!="fbpair" or raw.get("state")!="paired":
            continue
        if raw.get("provider")!=account.get("provider"):
            continue
        if raw.get("account_binding_id")!=account.get("account_binding_id"):
            continue
        node_id=str(raw.get("node_id") or "")
        label=str(raw.get("node_label") or "")
        try:
            parsed=uuid.UUID(node_id)
        except Exception:
            continue
        if NODE_RE.fullmatch(label) is None:
            continue
        scopes=sorted({str(x) for x in (raw.get("scopes") or []) if str(x)})
        if "node.read" not in scopes:
            continue
        result.append({
            "alias":label,
            "hostname":label,
            "transport":"broker_outbound",
            "target":parsed.hex[:8],
            "capability_profile":str(raw.get("capability_profile") or "paired"),
            "source":"fbpair",
            "pairing_state":"paired",
            "provider":"chatgpt",
            "node_id":node_id,
            "scopes":scopes,
            "operational_transport_ready":False,
        })
    return result


def _load_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.is_file():
        raise NodeGatewayError("NODE_REGISTRY_NOT_FOUND")

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise NodeGatewayError("NODE_REGISTRY_PERMISSIONS_TOO_OPEN")

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise NodeGatewayError("NODE_REGISTRY_INVALID_JSON")

    if not isinstance(value, dict):
        raise NodeGatewayError("NODE_REGISTRY_NOT_OBJECT")
    nodes = value.get("nodes")
    if not isinstance(nodes, dict):
        raise NodeGatewayError("NODE_REGISTRY_NODES_INVALID")
    return value


def _node(alias: str) -> dict[str, Any]:
    alias = str(alias).strip()
    if not NODE_RE.fullmatch(alias):
        raise NodeGatewayError("NODE_ALIAS_INVALID")

    registry = _load_registry()
    raw = registry["nodes"].get(alias)
    if isinstance(raw, dict):
        node = dict(raw)
        node["alias"] = alias
        node["source"] = "registry"
        return node

    account=_load_account_binding()
    discovery=_load_secure_json_optional(
        _paired_discovery_path(),
        "PAIRED_DISCOVERY",
    )
    if account and discovery and isinstance(discovery.get("nodes"),list):
        for item in discovery["nodes"]:
            if not isinstance(item,dict):
                continue
            if item.get("schema")!=1:
                continue
            if item.get("source")!="fbpair" or item.get("state")!="paired":
                continue
            if item.get("provider")!=account.get("provider"):
                continue
            if item.get("account_binding_id")!=account.get("account_binding_id"):
                continue
            if str(item.get("node_label") or "")!=alias:
                continue
            node_id=str(item.get("node_id") or "")
            try:
                parsed=uuid.UUID(node_id)
            except Exception:
                continue
            scopes={str(x) for x in (item.get("scopes") or [])}
            if "node.read" not in scopes:
                continue
            base=str(item.get("broker_base_url") or "").rstrip("/")
            if not base:
                continue
            return {
                "alias":alias,
                "hostname":alias,
                "target":parsed.hex[:8],
                "node_id":node_id,
                "transport":"broker_outbound",
                "broker_base_url":base,
                "capability_profile":str(item.get("capability_profile") or "paired"),
                "source":"fbpair",
                "pairing_state":"paired",
                "provider":"chatgpt",
                "scopes":sorted(scopes),
            }

    raise NodeGatewayError("NODE_NOT_FOUND")


def _validate_node(node: dict[str, Any]) -> None:
    if node.get("transport") != "ssh":
        raise NodeGatewayError("NODE_TRANSPORT_UNSUPPORTED")

    host = str(node.get("host") or "")
    user = str(node.get("user") or "")
    target = str(node.get("target") or "")
    try:
        port = int(node.get("port"))
    except Exception:
        raise NodeGatewayError("NODE_PORT_INVALID")

    if not host or any(ch.isspace() for ch in host):
        raise NodeGatewayError("NODE_HOST_INVALID")
    if not user or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user):
        raise NodeGatewayError("NODE_USER_INVALID")
    if not target or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", target):
        raise NodeGatewayError("NODE_TARGET_INVALID")
    if not (1 <= port <= 65535):
        raise NodeGatewayError("NODE_PORT_INVALID")

    key = Path(str(node.get("identity_file") or "")).expanduser()
    known = Path(str(node.get("known_hosts_file") or "")).expanduser()

    if not key.is_file():
        raise NodeGatewayError("NODE_IDENTITY_FILE_MISSING")
    if stat.S_IMODE(key.stat().st_mode) & 0o077:
        raise NodeGatewayError("NODE_IDENTITY_FILE_PERMISSIONS_TOO_OPEN")
    if not known.is_file():
        raise NodeGatewayError("NODE_KNOWN_HOSTS_MISSING")


def _ssh_argv(node: dict[str, Any], remote_command: str) -> list[str]:
    _validate_node(node)
    return [
        "ssh",
        "-p",
        str(int(node["port"])),
        "-oBatchMode=yes",
        "-oIdentitiesOnly=yes",
        "-oStrictHostKeyChecking=yes",
        "-oUserKnownHostsFile=" + str(Path(str(node["known_hosts_file"])).expanduser()),
        "-oConnectTimeout=5",
        "-i",
        str(Path(str(node["identity_file"])).expanduser()),
        str(node["user"]) + "@" + str(node["host"]),
        remote_command,
    ]


def _run_ssh(
    node: dict[str, Any],
    remote_command: str,
    input_text: str = "",
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _ssh_argv(node, remote_command),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        shell=False,
    )


def _sanitize_text(value: str, limit: int = 4096) -> str:
    text = "".join(
        ch for ch in str(value)
        if ch in "\n\r\t" or ord(ch) >= 32
    )
    return text[:limit]


def node_status(alias: str) -> dict[str, Any]:
    node = _node(alias)
    if node.get("transport")=="broker_outbound":
        live=_broker_node_read(node,"system.status",{})
        return {
            "ok":bool(live.get("ok")),
            "node":alias,
            "hostname":node.get("hostname"),
            "transport":"broker_outbound",
            "pairing_state":"paired",
            "provider":node.get("provider"),
            "status_source":"system.status",
            "status":live.get("result") if isinstance(live.get("result"),dict) else live,
        }

    proc = _run_ssh(
        node,
        "export PATH=$HOME/.local/bin:$PATH; framebridge status",
        timeout=15,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "node": alias,
            "error": "NODE_SSH_OR_STATUS_FAILED",
            "ssh_rc": proc.returncode,
            "stderr": _sanitize_text(proc.stderr, 1024),
        }
    try:
        status = json.loads(proc.stdout)
    except Exception:
        return {
            "ok": False,
            "node": alias,
            "error": "NODE_STATUS_INVALID_JSON",
        }
    return {
        "ok": bool(status.get("ok")),
        "node": alias,
        "hostname": node.get("hostname"),
        "transport": "ssh-gateway",
        "status": status,
    }


def _parse_classic_result(raw: str) -> dict[str, Any]:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    try:
        start = lines.index("FRAMERESULT/1")
        end = lines.index("FRAMEEND/1", start + 1)
    except ValueError:
        raise NodeGatewayError("NODE_CLASSIC_RESULT_FRAME_INVALID")

    headers: dict[str, str] = {}
    body_start = None
    for i, line in enumerate(lines[start + 1:end], start + 1):
        if not line.strip():
            body_start = i + 1
            break
        if "=" in line:
            key, value = line.split("=", 1)
            headers[key.strip()] = value.strip()

    if body_start is None:
        raise NodeGatewayError("NODE_CLASSIC_RESULT_BODY_MISSING")

    body_text = "\n".join(lines[body_start:end]).strip()
    try:
        body = json.loads(body_text)
    except Exception:
        raise NodeGatewayError("NODE_CLASSIC_RESULT_JSON_INVALID")

    exit_code = int(headers.get("EXIT", "1"))
    return {
        "ok": exit_code == 0,
        "id": headers.get("ID"),
        "exit": exit_code,
        "channel": headers.get("CHANNEL"),
        "capability": headers.get("CAPABILITY"),
        "body_sha256": headers.get("BODY_SHA256"),
        "duration_ms": headers.get("DURATION_MS"),
        "result": body,
    }


def _broker_post(base: str, path: str, value: dict[str, Any], timeout: float=5.0) -> dict[str, Any]:
    url=str(base).rstrip("/")+"/"+str(path).lstrip("/")
    data=json.dumps(value,sort_keys=True,separators=(",",":")).encode("utf-8")
    req=urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type":"application/json",
            "Accept":"application/json",
            "User-Agent":"FrameBridge-Gateway/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            raw=resp.read(1024*1024)
        result=json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise NodeGatewayError("BROKER_OUTBOUND_UNREACHABLE") from exc
    if not isinstance(result,dict):
        raise NodeGatewayError("BROKER_OUTBOUND_INVALID_RESPONSE")
    return result


def _broker_node_ready(node: dict[str, Any]) -> bool:
    try:
        base=str(node.get("broker_base_url") or "")
        node_id=str(node.get("node_id") or "")
        if not base or not node_id:
            return False
        value=_broker_post(
            base,
            "/fbpair/v1/control/node-status",
            {"node_id":node_id},
            timeout=2.0,
        )
        return value.get("ok") is True and value.get("ready") is True
    except Exception:
        return False


def _broker_node_read(node: dict[str, Any], capability: str, args: dict[str, Any]) -> dict[str, Any]:
    base=str(node.get("broker_base_url") or "")
    node_id=str(node.get("node_id") or "")
    if not base or not node_id:
        raise NodeGatewayError("BROKER_OUTBOUND_NODE_INVALID")

    created=_broker_post(
        base,
        "/fbpair/v1/control/request",
        {
            "node_id":node_id,
            "capability":capability,
            "args":args,
        },
    )
    if created.get("ok") is not True:
        raise NodeGatewayError("BROKER_OUTBOUND_REQUEST_REJECTED")
    request_id=str(created.get("request_id") or "")
    if not request_id:
        raise NodeGatewayError("BROKER_OUTBOUND_REQUEST_ID_MISSING")

    final=None
    for _ in range(32):
        time.sleep(0.25)
        value=_broker_post(
            base,
            "/fbpair/v1/control/result",
            {"request_id":request_id},
        )
        state=value.get("state")
        if state=="complete":
            final=value
            break
        if state=="expired":
            raise NodeGatewayError("BROKER_OUTBOUND_REQUEST_EXPIRED")
    if final is None:
        raise NodeGatewayError("BROKER_OUTBOUND_RESULT_TIMEOUT")

    payload=final.get("result")
    if not isinstance(payload,dict):
        raise NodeGatewayError("BROKER_OUTBOUND_RESULT_INVALID")
    result=dict(payload)
    result["node"]=node.get("alias")
    result["hostname"]=node.get("hostname")
    result["transport"]="broker_outbound"
    result["request_id"]=request_id
    return result


def node_read(
    alias: str,
    capability: str,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capability = str(capability).strip()
    if capability not in READ_CAPS:
        raise NodeGatewayError("NODE_CAPABILITY_NOT_ALLOWED")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise NodeGatewayError("NODE_ARGS_NOT_OBJECT")

    node = _node(alias)
    if node.get("transport")=="broker_outbound":
        return _broker_node_read(node,capability,args)

    frame_id = "mcp-node-" + uuid.uuid4().hex[:12]
    body = {
        "capability": capability,
        "args": args,
    }
    frame = "\n".join([
        "FRAMEEXEC/1",
        "ID=" + frame_id,
        "TARGET=" + str(node["target"]),
        "MODE=EXEC",
        "LANG=framebridge-cap-v1",
        "AUTHORIZED=NO",
        "TIMEOUT=30",
        "",
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        "FRAMEEND/1",
        "",
    ])

    proc = _run_ssh(
        node,
        "export PATH=$HOME/.local/bin:$PATH; framebridge classic --file -",
        input_text=frame,
        timeout=20,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "node": alias,
            "capability": capability,
            "error": "NODE_SSH_OR_CLASSIC_FAILED",
            "ssh_rc": proc.returncode,
            "stderr": _sanitize_text(proc.stderr, 1024),
        }

    result = _parse_classic_result(proc.stdout)
    result["node"] = alias
    result["hostname"] = node.get("hostname")
    result["transport"] = "ssh-gateway"
    return result



def node_capabilities(alias: str) -> dict[str, Any]:
    node = _node(alias)
    proc = _run_ssh(
        node,
        'exec "$HOME/.local/bin/framebridge" capabilities',
        timeout=15,
    )
    value = _json_result(proc, "NODE_CAPABILITIES_FAILED")
    raw = value.get("capabilities")
    if not isinstance(raw, list):
        raise NodeGatewayError("NODE_CAPABILITIES_INVALID")
    items = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        risk = str(item.get("risk") or "")
        if name in READ_CAPS and risk == "READONLY":
            items.append(item)
    return {
        "ok": True,
        "node": alias,
        "hostname": node.get("hostname"),
        "transport": "ssh-gateway",
        "capabilities": items,
        "count": len(items),
    }


def _validate_plan_id(plan_id: str) -> str:
    value = str(plan_id).strip()
    if not PLAN_ID_RE.fullmatch(value):
        raise NodeGatewayError("PLAN_ID_INVALID")
    return value


def _validate_sha256(value: str) -> str:
    value = str(value).strip().lower()
    if not SHA256_RE.fullmatch(value):
        raise NodeGatewayError("PLAN_SHA256_INVALID")
    return value


def _json_result(proc: subprocess.CompletedProcess[str], error: str) -> dict[str, Any]:
    if proc.returncode != 0:
        raise NodeGatewayError(error + "_RC_" + str(proc.returncode))
    try:
        value = json.loads(proc.stdout)
    except Exception:
        raise NodeGatewayError(error + "_INVALID_JSON")
    if not isinstance(value, dict):
        raise NodeGatewayError(error + "_NOT_OBJECT")
    return value


def _plan_show_raw(node: dict[str, Any], plan_id: str) -> dict[str, Any]:
    plan_id = _validate_plan_id(plan_id)
    proc = _run_ssh(
        node,
        'exec "$HOME/.local/bin/framebridge" plan show ' + plan_id,
        timeout=15,
    )
    return _json_result(proc, "NODE_PLAN_SHOW_FAILED")


def _broker_plan_operation(
    node: dict[str, Any],
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    base=str(node.get("broker_base_url") or "")
    node_id=str(node.get("node_id") or "")
    if not base or not node_id:
        raise NodeGatewayError("BROKER_OUTBOUND_NODE_INVALID")
    created=_broker_post(
        base,
        "/fbpair/v1/control/operation",
        {
            "node_id":node_id,
            "operation":operation,
            "payload":payload,
        },
    )
    if created.get("ok") is not True:
        raise NodeGatewayError(str(created.get("error") or "BROKER_PLAN_REQUEST_REJECTED"))
    request_id=str(created.get("request_id") or "")
    if not request_id:
        raise NodeGatewayError("BROKER_PLAN_REQUEST_ID_MISSING")

    final=None
    for _ in range(64):
        time.sleep(0.25)
        value=_broker_post(
            base,
            "/fbpair/v1/control/result",
            {"request_id":request_id},
        )
        state=value.get("state")
        if state=="complete":
            final=value
            break
        if state=="expired":
            raise NodeGatewayError("BROKER_PLAN_REQUEST_EXPIRED")
    if final is None:
        raise NodeGatewayError("BROKER_PLAN_RESULT_TIMEOUT")

    envelope=final.get("result")
    if not isinstance(envelope,dict):
        raise NodeGatewayError("BROKER_PLAN_RESULT_INVALID")
    if envelope.get("ok") is not True:
        raise NodeGatewayError(str(envelope.get("error") or "BROKER_PLAN_OPERATION_FAILED"))
    result=envelope.get("result")
    if not isinstance(result,dict):
        raise NodeGatewayError("BROKER_PLAN_PAYLOAD_INVALID")
    return result


def node_plan_status(alias: str, plan_id: str) -> dict[str, Any]:
    node = _node(alias)
    plan_id = _validate_plan_id(plan_id)
    if node.get("transport")=="broker_outbound":
        raw=_broker_plan_operation(
            node,
            "plan.status",
            {"plan_id":plan_id},
        )
    else:
        raw = _plan_show_raw(node, plan_id)
    approval = raw.get("approval") or {}
    plan = raw.get("plan") or {}
    steps = []
    for step in plan.get("steps") or []:
        args = step.get("args") or {}
        item = {
            "id": step.get("id"),
            "capability": step.get("capability"),
            "risk": step.get("risk"),
        }
        if "path" in args:
            item["path"] = args.get("path")
        if "package" in args:
            item["package"] = args.get("package")
        steps.append(item)
    rollback = raw.get("rollback") or {}
    rollback_approval = raw.get("rollback_approval") or {}
    return {
        "ok": raw.get("ok") is True,
        "node": alias,
        "hostname": node.get("hostname"),
        "plan_id": raw.get("plan_id"),
        "plan_sha256": raw.get("plan_sha256"),
        "state": raw.get("state"),
        "title": plan.get("title"),
        "steps": steps,
        "approval": {
            "source": approval.get("source"),
            "one_shot": approval.get("one_shot"),
            "consumed": approval.get("consumed"),
            "plan_sha256": approval.get("plan_sha256"),
            "auth_method": approval.get("auth_method"),
            "key_id": approval.get("key_id"),
            "challenge_id": approval.get("challenge_id"),
            "totp_step": approval.get("totp_step"),
        } if approval else None,
        "apply_present": raw.get("apply") is not None,
        "rollback_available": rollback.get("available") is True,
        "rollback_verified": rollback.get("verified") is True,
        "rollback_approval": {
            "source": rollback_approval.get("source"),
            "one_shot": rollback_approval.get("one_shot"),
            "consumed": rollback_approval.get("consumed"),
            "plan_sha256": rollback_approval.get("plan_sha256"),
            "auth_method": rollback_approval.get("auth_method"),
            "key_id": rollback_approval.get("key_id"),
            "challenge_id": rollback_approval.get("challenge_id"),
            "totp_step": rollback_approval.get("totp_step"),
        } if rollback_approval else None,
        "transport": "broker_outbound" if node.get("transport")=="broker_outbound" else "ssh-gateway",
    }


def node_plan_import(alias: str, plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise NodeGatewayError("PLAN_NOT_OBJECT")
    plan_id = _validate_plan_id(str(plan.get("plan_id") or ""))
    body = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(body.encode("utf-8")) > 32768:
        raise NodeGatewayError("PLAN_BODY_TOO_LARGE")
    node = _node(alias)
    if node.get("transport")=="broker_outbound":
        raw=_broker_plan_operation(
            node,
            "plan.import",
            {"plan":plan},
        )
        if raw.get("ok") is not True:
            return {
                "ok":False,
                "node":alias,
                "plan_id":plan_id,
                "error":str(raw.get("error") or "NODE_PLAN_IMPORT_FAILED"),
            }
        return node_plan_status(alias,plan_id)

    proc = _run_ssh(
        node,
        'exec "$HOME/.local/bin/framebridge" plan import-stdin',
        input_text=body + "\n",
        timeout=20,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": "NODE_PLAN_IMPORT_FAILED",
            "ssh_rc": proc.returncode,
            "stderr": _sanitize_text(proc.stderr, 1024),
        }
    try:
        raw = json.loads(proc.stdout)
    except Exception:
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": "NODE_PLAN_IMPORT_INVALID_JSON",
        }
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": str(raw.get("error") if isinstance(raw, dict) else "NODE_PLAN_IMPORT_FAILED"),
        }
    return node_plan_status(alias, plan_id)


def node_plan_apply(alias: str, plan_id: str, expected_sha256: str) -> dict[str, Any]:
    plan_id = _validate_plan_id(plan_id)
    expected_sha256 = _validate_sha256(expected_sha256)
    node = _node(alias)
    if node.get("transport")=="broker_outbound":
        _broker_plan_operation(
            node,
            "plan.apply",
            {
                "plan_id":plan_id,
                "plan_sha256":expected_sha256,
            },
        )
        return node_plan_status(alias,plan_id)

    pre = node_plan_status(alias, plan_id)
    approval = pre.get("approval") or {}
    pre_steps = pre.get("steps") or []
    capability = pre_steps[0].get("capability") if len(pre_steps) == 1 else None
    source = approval.get("source")
    source_ok = _approval_source_ok(capability, source)
    remote_meta_ok = True
    if source == REMOTE_APPROVAL_SOURCE:
        remote_meta_ok = (approval.get("auth_method") == "ecdsa-p256" and REMOTE_KEY_ID_RE.fullmatch(str(approval.get("key_id") or "")) is not None and REMOTE_CHALLENGE_RE.fullmatch(str(approval.get("challenge_id") or "")) is not None)
    elif source == TOTP_APPROVAL_SOURCE:
        remote_meta_ok = (approval.get("auth_method") == "totp-sha1-6-30" and REMOTE_KEY_ID_RE.fullmatch(str(approval.get("key_id") or "")) is not None and REMOTE_CHALLENGE_RE.fullmatch(str(approval.get("challenge_id") or "")) is not None and type(approval.get("totp_step")) is int)
    checks = {
        "state_approved": pre.get("state") == "APPROVED",
        "approval_source_expected": source_ok,
        "approval_remote_metadata_valid": remote_meta_ok,
        "approval_one_shot": approval.get("one_shot") is True,
        "approval_not_consumed": approval.get("consumed") is False,
        "plan_sha256_match": pre.get("plan_sha256") == expected_sha256,
        "approval_sha256_match": approval.get("plan_sha256") == expected_sha256,
        "not_already_applied": pre.get("apply_present") is False,
    }
    if not all(checks.values()):
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": "PLAN_PRECONDITION_FAILED",
            "preconditions": checks,
            "state": pre.get("state"),
        }

    proc = _run_ssh(
        node,
        'exec "$HOME/.local/bin/framebridge" plan apply ' + plan_id,
        timeout=30,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": "NODE_PLAN_APPLY_FAILED",
            "ssh_rc": proc.returncode,
            "stderr": _sanitize_text(proc.stderr, 1024),
        }
    try:
        apply_raw = json.loads(proc.stdout)
    except Exception:
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": "NODE_PLAN_APPLY_INVALID_JSON",
        }

    post_raw = _plan_show_raw(node, plan_id)
    post_approval = post_raw.get("approval") or {}
    apply_meta = post_raw.get("apply") or {}
    rollback = post_raw.get("rollback") or {}
    applied_steps = apply_meta.get("steps") or []
    step_summaries = []
    all_verified = bool(applied_steps)
    for step in applied_steps:
        verified = step.get("verified") is True
        all_verified = all_verified and verified
        item = {
            "step_id": step.get("step_id"),
            "capability": step.get("capability"),
            "verified": verified,
        }
        for key in ("target", "bytes", "sha256_after", "package", "version"):
            if step.get(key) is not None:
                item[key] = step.get(key)
        step_summaries.append(item)

    audit_proc = _run_ssh(
        node,
        'exec "$HOME/.local/bin/framebridge" audit',
        timeout=15,
    )
    try:
        audit = _json_result(audit_proc, "NODE_AUDIT_FAILED")
    except Exception as exc:
        audit = {"ok": False, "error": str(exc)}

    postchecks = {
        "state_applied": post_raw.get("state") == "APPLIED",
        "approval_consumed": post_approval.get("consumed") is True,
        "plan_sha256_match": post_raw.get("plan_sha256") == expected_sha256,
        "steps_verified": all_verified,
        "rollback_available": rollback.get("available") is True,
        "audit_chain_ok": audit.get("ok") is True,
    }
    ok = all(postchecks.values())
    return {
        "ok": ok,
        "node": alias,
        "hostname": node.get("hostname"),
        "plan_id": plan_id,
        "plan_sha256": expected_sha256,
        "state": post_raw.get("state"),
        "approval_consumed": post_approval.get("consumed"),
        "preconditions": checks,
        "postconditions": postchecks,
        "steps": step_summaries,
        "rollback_available": rollback.get("available") is True,
        "audit": audit,
        "transport": "ssh-gateway",
        "error": None if ok else "PLAN_POSTCONDITION_FAILED",
    }


def node_plan_rollback(alias: str, plan_id: str, expected_sha256: str) -> dict[str, Any]:
    plan_id = _validate_plan_id(plan_id)
    expected_sha256 = _validate_sha256(expected_sha256)
    node = _node(alias)
    if node.get("transport")=="broker_outbound":
        _broker_plan_operation(
            node,
            "plan.rollback",
            {
                "plan_id":plan_id,
                "plan_sha256":expected_sha256,
            },
        )
        return node_plan_status(alias,plan_id)

    pre = node_plan_status(alias, plan_id)
    rb_approval = pre.get("rollback_approval") or {}
    pre_steps = pre.get("steps") or []
    capability = pre_steps[0].get("capability") if len(pre_steps) == 1 else None
    source = rb_approval.get("source")
    source_ok = _approval_source_ok(capability, source)
    remote_meta_ok = True
    if source == REMOTE_APPROVAL_SOURCE:
        remote_meta_ok = (rb_approval.get("auth_method") == "ecdsa-p256" and REMOTE_KEY_ID_RE.fullmatch(str(rb_approval.get("key_id") or "")) is not None and REMOTE_CHALLENGE_RE.fullmatch(str(rb_approval.get("challenge_id") or "")) is not None)
    elif source == TOTP_APPROVAL_SOURCE:
        remote_meta_ok = (rb_approval.get("auth_method") == "totp-sha1-6-30" and REMOTE_KEY_ID_RE.fullmatch(str(rb_approval.get("key_id") or "")) is not None and REMOTE_CHALLENGE_RE.fullmatch(str(rb_approval.get("challenge_id") or "")) is not None and type(rb_approval.get("totp_step")) is int)
    checks = {
        "state_applied": pre.get("state") == "APPLIED",
        "rollback_available": pre.get("rollback_available") is True,
        "rollback_approval_source_expected": source_ok,
        "rollback_approval_remote_metadata_valid": remote_meta_ok,
        "rollback_approval_one_shot": rb_approval.get("one_shot") is True,
        "rollback_approval_not_consumed": rb_approval.get("consumed") is False,
        "plan_sha256_match": pre.get("plan_sha256") == expected_sha256,
        "rollback_approval_sha256_match": rb_approval.get("plan_sha256") == expected_sha256,
        "apply_present": pre.get("apply_present") is True,
    }
    if not all(checks.values()):
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": "ROLLBACK_PRECONDITION_FAILED",
            "preconditions": checks,
            "state": pre.get("state"),
        }

    proc = _run_ssh(
        node,
        'exec "$HOME/.local/bin/framebridge" plan rollback ' + plan_id,
        timeout=30,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": "NODE_PLAN_ROLLBACK_FAILED",
            "ssh_rc": proc.returncode,
            "stderr": _sanitize_text(proc.stderr, 1024),
        }

    try:
        json.loads(proc.stdout)
    except Exception:
        return {
            "ok": False,
            "node": alias,
            "plan_id": plan_id,
            "error": "NODE_PLAN_ROLLBACK_INVALID_JSON",
        }

    post_raw = _plan_show_raw(node, plan_id)
    post_rb = post_raw.get("rollback") or {}
    post_approval = post_raw.get("rollback_approval") or {}
    verification = post_rb.get("verification") or []
    all_verified = bool(verification) and all(x.get("verified") is True for x in verification)

    audit_proc = _run_ssh(
        node,
        'exec "$HOME/.local/bin/framebridge" audit',
        timeout=15,
    )
    try:
        audit = _json_result(audit_proc, "NODE_AUDIT_FAILED")
    except Exception as exc:
        audit = {"ok": False, "error": str(exc)}

    postchecks = {
        "state_rolled_back": post_raw.get("state") == "ROLLED_BACK",
        "rollback_approval_consumed": post_approval.get("consumed") is True,
        "plan_sha256_match": post_raw.get("plan_sha256") == expected_sha256,
        "rollback_unavailable": post_rb.get("available") is False,
        "rollback_verified": post_rb.get("verified") is True and all_verified,
        "audit_chain_ok": audit.get("ok") is True,
    }
    ok = all(postchecks.values())
    return {
        "ok": ok,
        "node": alias,
        "hostname": node.get("hostname"),
        "plan_id": plan_id,
        "plan_sha256": expected_sha256,
        "state": post_raw.get("state"),
        "rollback_approval_consumed": post_approval.get("consumed"),
        "preconditions": checks,
        "postconditions": postchecks,
        "rollback_available": post_rb.get("available") is True,
        "rollback_verified": post_rb.get("verified") is True,
        "verification": verification,
        "audit": audit,
        "transport": "ssh-gateway",
        "error": None if ok else "ROLLBACK_POSTCONDITION_FAILED",
    }


def discover_nodes() -> dict:
    try:
        registry = _load_registry()
        items = []
        seen_targets=set()
        seen_aliases=set()
        for alias, raw in sorted(registry["nodes"].items()):
            if not isinstance(raw, dict):
                continue
            target=str(raw.get("target") or "")
            entry={
                "alias": alias,
                "hostname": raw.get("hostname"),
                "transport": raw.get("transport"),
                "target": target,
                "capability_profile": raw.get("capability_profile", "seed-readonly"),
                "source":"registry",
            }
            raw_node_id=str(raw.get("node_id") or "")
            if raw_node_id:
                try:
                    entry["node_id"]=str(uuid.UUID(raw_node_id))
                except Exception:
                    pass
            if raw.get("transport")=="broker_outbound":
                candidate=dict(raw)
                candidate["alias"]=alias
                entry["operational_transport_ready"]=_broker_node_ready(candidate)
            else:
                entry["operational_transport_ready"]=True
            items.append(entry)
            seen_targets.add(target)
            seen_aliases.add(alias)

        for item in _load_paired_discovery():
            if item["target"] in seen_targets:
                continue
            item=dict(item)
            try:
                resolved=_node(item["alias"])
                item["operational_transport_ready"]=_broker_node_ready(resolved)
            except Exception:
                item["operational_transport_ready"]=False
            if item["alias"] in seen_aliases:
                item["alias"]=item["alias"]+"-"+item["target"]
            items.append(item)
            seen_targets.add(item["target"])
            seen_aliases.add(item["alias"])

        items.sort(key=lambda x:str(x.get("alias") or ""))
        return {
            "ok": True,
            "nodes": items,
            "sources":{
                "registry":sum(1 for x in items if x.get("source")=="registry"),
                "fbpair":sum(1 for x in items if x.get("source")=="fbpair"),
            },
        }
    except Exception:
        return {"ok": False, "error": "NODE_DISCOVERY_FAILED"}


def register_node_tools(mcp) -> None:
    @mcp.tool(
        description=(
            "WRITE. Import one structured FrameBridge Seed plan into a configured node. "
            "Importing a plan does not approve or apply it. Seed validates schema, "
            "capability allowlist, arguments, and risk constraints."
        ),
        annotations=CHANGE_ANNOTATIONS,
    )
    def framebridge_node_plan_import(node: str, plan: dict) -> dict:
        try:
            return node_plan_import(node, plan)
        except Exception as exc:
            return {"ok": False, "node": node, "error": str(exc)}

    @mcp.tool(
        description=(
            "CHANGE. Roll back exactly one APPLIED FrameBridge Seed plan after a new "
            "LOCAL_TTY one-shot rollback approval. Requires exact plan SHA-256. The tool "
            "cannot create rollback approval, run raw shell, or bypass Seed policy."
        ),
        annotations=CHANGE_ANNOTATIONS,
    )
    def framebridge_node_plan_rollback(node: str, plan_id: str, plan_sha256: str) -> dict:
        try:
            return node_plan_rollback(node, plan_id, plan_sha256)
        except Exception as exc:
            return {"ok": False, "node": node, "plan_id": plan_id, "error": str(exc)}

    @mcp.tool(
        description=(
            "READ-ONLY. Read one existing FrameBridge Seed plan status from a node. "
            "Does not import, approve, apply, or modify the plan."
        ),
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_plan_status(node: str, plan_id: str) -> dict:
        try:
            return node_plan_status(node, plan_id)
        except Exception as exc:
            return {"ok": False, "node": node, "plan_id": plan_id, "error": str(exc)}

    @mcp.tool(
        description=(
            "CHANGE. Apply exactly one existing FrameBridge Seed plan that was already "
            "approved locally in the node TTY. Requires exact plan SHA-256. The tool "
            "cannot create plans, approve plans, run raw shell, or bypass Seed policy."
        ),
        annotations=CHANGE_ANNOTATIONS,
    )
    def framebridge_node_plan_apply(node: str, plan_id: str, plan_sha256: str) -> dict:
        try:
            return node_plan_apply(node, plan_id, plan_sha256)
        except Exception as exc:
            return {"ok": False, "node": node, "plan_id": plan_id, "error": str(exc)}

    @mcp.tool(
        description=(
            "READ-ONLY. List FrameBridge nodes available to this account, "
            "including configured registry nodes and account-authorized FBPAIR nodes. "
            "Does not execute commands on the nodes."
        ),
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_nodes() -> dict:
        return discover_nodes()

    @mcp.tool(
        description=(
            "READ-ONLY. List typed READONLY capabilities advertised by one "
            "FrameBridge Seed node after Gateway allowlist filtering."
        ),
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_capabilities(node: str) -> dict:
        try:
            return node_capabilities(node)
        except Exception as exc:
            return {"ok": False, "node": node, "error": str(exc)}

    @mcp.tool(
        description=(
            "READ-ONLY. Execute one allowlisted typed READONLY capability on a "
            "FrameBridge Seed node. No raw shell and no CHANGE capability is accepted."
        ),
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_read(node: str, capability: str, args: dict | None = None) -> dict:
        try:
            return node_read(node, capability, args or {})
        except Exception as exc:
            return {
                "ok": False,
                "node": node,
                "capability": capability,
                "error": str(exc),
            }

    @mcp.tool(
        description=(
            "READ-ONLY. Read FrameBridge Seed status from a configured node. "
            "No system changes are performed."
        ),
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_status(node: str) -> dict:
        try:
            return node_status(node)
        except Exception as exc:
            return {"ok": False, "node": node, "error": str(exc)}

    @mcp.tool(
        description="READ-ONLY. Read OS/kernel/uptime status from a FrameBridge Seed node.",
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_system_status(node: str) -> dict:
        try:
            return node_read(node, "system.status", {})
        except Exception as exc:
            return {"ok": False, "node": node, "error": str(exc)}

    @mcp.tool(
        description="READ-ONLY. Read CPU and memory summary from a FrameBridge Seed node.",
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_hardware_summary(node: str) -> dict:
        try:
            return node_read(node, "hardware.summary", {})
        except Exception as exc:
            return {"ok": False, "node": node, "error": str(exc)}

    @mcp.tool(
        description="READ-ONLY. Inspect block devices and filesystems on a FrameBridge Seed node.",
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_storage_inspect(node: str) -> dict:
        try:
            return node_read(node, "storage.inspect", {})
        except Exception as exc:
            return {"ok": False, "node": node, "error": str(exc)}

    @mcp.tool(
        description="READ-ONLY. Inspect addresses and routes on a FrameBridge Seed node.",
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_network_inspect(node: str) -> dict:
        try:
            return node_read(node, "network.inspect", {})
        except Exception as exc:
            return {"ok": False, "node": node, "error": str(exc)}

    @mcp.tool(
        description=(
            "READ-ONLY. Read active/enabled state of one named systemd service "
            "on a FrameBridge Seed node."
        ),
        annotations=READ_ANNOTATIONS,
    )
    def framebridge_node_service_status(node: str, service: str) -> dict:
        try:
            if not SERVICE_RE.fullmatch(str(service)):
                raise NodeGatewayError("SERVICE_NAME_INVALID")
            return node_read(node, "service.status", {"service": str(service)})
        except Exception as exc:
            return {"ok": False, "node": node, "error": str(exc)}
