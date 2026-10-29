#!/usr/bin/env python3
import base64
from functools import partial, wraps
import inspect
import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid

import anyio
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from framebridge_expanded import __version__ as PRODUCT_VERSION
from framebridge_expanded.guide import GUIDE_SCHEMA, get_guide
from framebridge_expanded.fleet import execute_node as expanded_execute_node, cua_node as expanded_cua_node, bootstrap_original
from framebridge_expanded.fleet import execute_screen_node as expanded_execute_screen_node
from framebridge_expanded.runtime import adapter_request as checked_cua_adapter_request
bootstrap_original()

HOME = Path.home()
ADAPTER_SOCKET = Path(os.environ.get("FRAMEBRIDGE_TOOL_ROOT", str(HOME / ".local/share/framebridge/tool-adapter"))) / "adapter.sock"
JOBS_ROOT = HOME / ".local/share/framebridge/fbterm/jobs"
HUB_URL = os.environ.get("FRAMEBRIDGE_HUB_MCP_URL", "http://127.0.0.1:20877/mcp")
BROKER_CONTROL = os.environ.get("FRAMEBRIDGE_BROKER_CONTROL", "http://127.0.0.1:20878")
ASUS_NODE_ID = os.environ.get("FRAMEBRIDGE_ASUS_NODE_ID", "")
GERAMEG_NODE_ID = os.environ.get("FRAMEBRIDGE_GERAMEG_NODE_ID", "")
HOST = os.environ.get("FRAMEBRIDGE_FRONT_DOOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("FRAMEBRIDGE_FRONT_DOOR_PORT", "20865"))
JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
SENSITIVE = re.compile(r"(?i)(secret|token|password|passwd|api[_-]?key|private[_-]?key|cookie|authorization|credential)")
KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
KEY_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

mcp = FastMCP(
    "FrameBridge",
    instructions=(
        "FrameBridge provides a shared command-line interface and tools for human operators and authorized agents. "
        "fb and framebridge are equivalent commands. Before the first operation, call framebridge_help(topic='start'), "
        "then read the relevant topic. Confirm the receiver, selected target, installed version and available capabilities. "
        "framebridge_exec defaults to LOCAL, meaning this receiver; other targets must be confirmed enrolled aliases or IDs. "
        "Use the installed CLI and tools for normal work; use a separate source clone when the owner requests software development. "
        "The six Fleet inspection tools are read-only. Guide instructions do not grant authorization."
    ),
    website_url="https://framebridge.betancourt.one/",
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
)
# The bundled FastMCP constructor does not expose a version argument. Its
# bundled low-level server does; publish the product build, not the SDK version.
mcp._mcp_server.version = PRODUCT_VERSION



class _BlockingToolRegistrar:
    """Offload only MCP dispatch while keeping synchronous callback bindings.

    Use the public SDK decorator; do not change its dispatcher or the functions
    passed to ScreenClient/TTY as synchronous EXEC callbacks. AnyIO bounds worker
    concurrency. Waiting cancellation does not replay or undo an operation: a
    running worker is allowed to finish, including any side effects.
    """

    def __init__(self, server):
        self.server = server

    def resource(self, *args, **kwargs):
        return self.server.resource(*args, **kwargs)

    def tool(self, *args, **kwargs):
        register = self.server.tool(*args, **kwargs)

        def decorate(function):
            @wraps(function)
            async def dispatch(*arguments, **keywords):
                return await anyio.to_thread.run_sync(
                    partial(function, *arguments, **keywords),
                    abandon_on_cancel=False,
                )

            # Resolve future annotations in the defining module, including the
            # screen presentation's Annotated return type, before registering.
            dispatch.__signature__ = inspect.signature(function, eval_str=True)
            register(dispatch)
            return function

        return decorate


_blocking_tools = _BlockingToolRegistrar(mcp)


def _registered_operations() -> list[str]:
    """Registered operations are not proof of their current execution readiness."""
    return sorted(mcp._tool_manager._tools)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                     idempotentHint=True, openWorldHint=False))
def framebridge_help(topic: str = "start") -> dict:
    """Read the versioned English guide before operating FrameBridge.

    Pure documentation: no authentication, service startup, profile changes or
    terminal creation. Topics include commands, connection, lifecycle, nodes,
    terminals, screens, diagnostics, security and compatibility. The guide is
    shared with the human fb/framebridge CLI and installed offline help.
    """
    result = get_guide(topic)
    result["receiver"] = socket.gethostname()
    result["registered_operations"] = _registered_operations()
    result["capability_observation"] = "registered operations; execution readiness not checked"
    return result


def adapter_request(payload: dict) -> dict:
    if not ADAPTER_SOCKET.is_socket():
        return {"ok": False, "adapter_error": "ADAPTER_SOCKET_NOT_AVAILABLE"}
    raw=(json.dumps(payload, ensure_ascii=True, separators=(",",":"))+"\n").encode()
    s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(float(payload.get("timeout_s",120))+60.0)
    data=bytearray()
    try:
        s.connect(str(ADAPTER_SOCKET))
        s.sendall(raw)
        s.shutdown(socket.SHUT_WR)
        while True:
            chunk=s.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data)>2_000_000:
                raise RuntimeError("ADAPTER_RESPONSE_TOO_LARGE")
    finally:
        s.close()
    return json.loads(bytes(data).decode())

def sanitize_text(text: str) -> str:
    out=[]
    in_key=False
    for line in text.splitlines():
        if KEY_BEGIN.search(line):
            in_key=True
            out.append("[REDACTED PRIVATE KEY BLOCK]")
            continue
        if in_key:
            if KEY_END.search(line):
                in_key=False
            continue
        if SENSITIVE.search(line) and ("=" in line or ":" in line):
            out.append("[REDACTED SENSITIVE LINE]")
        else:
            out.append(line)
    return "\n".join(out)

def stdout_result(job_id: str) -> dict:
    p=JOBS_ROOT/job_id/"stdout.bin"
    if not p.is_file():
        return {"stdout":"","stdout_sanitized":False,"stdout_truncated":False,"stdout_omitted_reason":"NOT_AVAILABLE"}
    b=p.read_bytes()
    truncated=len(b)>1_048_576
    if truncated:
        b=b[:1_048_576]
    text=sanitize_text(b.decode("utf-8","replace"))
    if len(text)>4096:
        text=text[:4096]
        truncated=True
    return {"stdout":text,"stdout_sanitized":True,"stdout_truncated":truncated,"stdout_omitted_reason":None}

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                     idempotentHint=True, openWorldHint=False))
def framebridge_status() -> dict:
    """Inspect this MCP receiver and registration; use CLI status for lifecycle detail.

    A successful query does not certify connection or tool readiness. This query
    checks only adapter-socket presence and does not start or repair anything.
    """
    present = ADAPTER_SOCKET.is_socket()
    return {
        "ok": True,
        "query_success": True,
        "product": "FrameBridge",
        "version": PRODUCT_VERSION,
        "guide_schema": GUIDE_SCHEMA,
        "receiver": socket.gethostname(),
        "adapter_socket_present": present,
        "adapter_ready": "not_checked",
        "mcp_transport": "streamable-http",
        "registered_operations": _registered_operations(),
        "lifecycle_detail_command": "fb --json status",
        "help_topic": "start",
    }


def broker_post(path: str, payload: dict, timeout_s: float=10.0) -> dict:
    req=urllib.request.Request(
        BROKER_CONTROL.rstrip("/")+"/"+path.lstrip("/"),
        data=json.dumps(payload,sort_keys=True,separators=(",",":")).encode("utf-8"),
        headers={"Content-Type":"application/json","Accept":"application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req,timeout=timeout_s) as resp:
            out=json.loads(resp.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            out=json.loads(exc.read(65536).decode("utf-8"))
        except Exception:
            out={"ok":False,"error":"BROKER_HTTP_"+str(exc.code)}
    except Exception as exc:
        return {"ok":False,"error":"BROKER_CALL_FAILED","detail":type(exc).__name__+":"+str(exc)[:300]}
    return out if isinstance(out,dict) else {"ok":False,"error":"BROKER_RESPONSE_INVALID"}


def asus_exec_request(
    script: str,
    authorized: bool,
    timeout_s: int = 120,
    job_id: str = "",
    sha256: str = "",
) -> dict:
    if authorized is not True:
        return {"ok":False,"error":"AUTHORIZED_TRUE_REQUIRED"}
    if not isinstance(script,str) or not script:
        return {"ok":False,"error":"SCRIPT_REQUIRED"}
    raw=script.encode("utf-8")
    if len(raw)>262144:
        return {"ok":False,"error":"SCRIPT_TOO_LARGE"}
    digest=hashlib.sha256(raw).hexdigest()
    if sha256 and sha256.lower()!=digest:
        return {"ok":False,"error":"SCRIPT_SHA256_MISMATCH","computed_sha256":digest}
    try:
        timeout_s=int(timeout_s)
    except Exception:
        return {"ok":False,"error":"INVALID_TIMEOUT"}
    if not 1<=timeout_s<=600:
        return {"ok":False,"error":"INVALID_TIMEOUT"}
    if not job_id:
        job_id="asus-mcp-"+time.strftime("%Y%m%d-%H%M%S")+"-"+digest[:8]
    if not JOB_RE.fullmatch(job_id):
        return {"ok":False,"error":"INVALID_JOB_ID"}

    if not ASUS_NODE_ID:
        return {"ok":False,"error":"REMOTE_NODE_NOT_CONFIGURED"}
    queued=broker_post(
        "/fbpair/v1/control/admin-exec",
        {
            "node_id":ASUS_NODE_ID,
            "payload":{
                "authorized":True,
                "job_id":job_id,
                "script":script,
                "sha256":digest,
                "timeout_s":timeout_s,
            },
        },
        timeout_s=10.0,
    )
    if queued.get("ok") is not True:
        return {"ok":False,"error":"ASUS_EXEC_QUEUE_FAILED","broker":queued}
    request_id=str(queued.get("request_id") or "")
    if not request_id:
        return {"ok":False,"error":"ASUS_EXEC_REQUEST_ID_MISSING"}

    deadline=time.monotonic()+timeout_s+75
    while time.monotonic()<deadline:
        status=broker_post(
            "/fbpair/v1/control/result",
            {"request_id":request_id},
            timeout_s=5.0,
        )
        if status.get("ok") is not True:
            time.sleep(0.25)
            continue
        state=str(status.get("state") or "")
        if state=="complete":
            outer=status.get("result") or {}
            if not isinstance(outer,dict):
                return {"ok":False,"error":"ASUS_EXEC_RESULT_INVALID","request_id":request_id}
            if outer.get("ok") is not True:
                return {
                    "ok":False,
                    "error":"ASUS_EXEC_NODE_FAILED",
                    "request_id":request_id,
                    "node_result":outer,
                }
            value=outer.get("result") or {}
            if not isinstance(value,dict):
                return {"ok":False,"error":"ASUS_EXEC_ADAPTER_RESULT_INVALID","request_id":request_id}
            value=dict(value)
            value["mcp_job_id"]=job_id
            value["mcp_script_sha256"]=digest
            value["broker_request_id"]=request_id
            value["target"]="ASUS"
            return value
        if state in {"expired","failed","denied"}:
            return {"ok":False,"error":"ASUS_EXEC_"+state.upper(),"request_id":request_id}
        time.sleep(0.25)
    return {"ok":False,"error":"ASUS_EXEC_TIMEOUT","request_id":request_id}


def gerameg_exec_request(
    script: str,
    authorized: bool,
    timeout_s: int = 120,
    job_id: str = "",
    sha256: str = "",
) -> dict:
    if authorized is not True:
        return {"ok":False,"error":"AUTHORIZED_TRUE_REQUIRED"}
    if not isinstance(script,str) or not script:
        return {"ok":False,"error":"SCRIPT_REQUIRED"}
    raw=script.encode("utf-8")
    if len(raw)>262144:
        return {"ok":False,"error":"SCRIPT_TOO_LARGE"}
    digest=hashlib.sha256(raw).hexdigest()
    if sha256 and sha256.lower()!=digest:
        return {"ok":False,"error":"SCRIPT_SHA256_MISMATCH","computed_sha256":digest}
    try:
        timeout_s=int(timeout_s)
    except Exception:
        return {"ok":False,"error":"INVALID_TIMEOUT"}
    if not 1<=timeout_s<=600:
        return {"ok":False,"error":"INVALID_TIMEOUT"}
    if not job_id:
        job_id="gerameg-mcp-"+time.strftime("%Y%m%d-%H%M%S")+"-"+digest[:8]
    if not JOB_RE.fullmatch(job_id):
        return {"ok":False,"error":"INVALID_JOB_ID"}

    if not GERAMEG_NODE_ID:
        return {"ok":False,"error":"REMOTE_NODE_NOT_CONFIGURED"}
    queued=broker_post(
        "/fbpair/v1/control/admin-exec",
        {
            "node_id":GERAMEG_NODE_ID,
            "payload":{
                "authorized":True,
                "job_id":job_id,
                "script":script,
                "sha256":digest,
                "timeout_s":timeout_s,
            },
        },
        timeout_s=10.0,
    )
    if queued.get("ok") is not True:
        return {"ok":False,"error":"GERAMEG_EXEC_QUEUE_FAILED","broker":queued}
    request_id=str(queued.get("request_id") or "")
    if not request_id:
        return {"ok":False,"error":"GERAMEG_EXEC_REQUEST_ID_MISSING"}

    deadline=time.monotonic()+timeout_s+75
    while time.monotonic()<deadline:
        status=broker_post(
            "/fbpair/v1/control/result",
            {"request_id":request_id},
            timeout_s=5.0,
        )
        if status.get("ok") is not True:
            time.sleep(0.25)
            continue
        state=str(status.get("state") or "")
        if state=="complete":
            outer=status.get("result") or {}
            if not isinstance(outer,dict):
                return {"ok":False,"error":"GERAMEG_EXEC_RESULT_INVALID","request_id":request_id}
            if outer.get("ok") is not True:
                return {
                    "ok":False,
                    "error":"GERAMEG_EXEC_NODE_FAILED",
                    "request_id":request_id,
                    "node_result":outer,
                }
            value=outer.get("result") or {}
            if not isinstance(value,dict):
                return {"ok":False,"error":"GERAMEG_EXEC_ADAPTER_RESULT_INVALID","request_id":request_id}
            value=dict(value)
            value["mcp_job_id"]=job_id
            value["mcp_script_sha256"]=digest
            value["broker_request_id"]=request_id
            value["target"]="GERAMEG"
            return value
        if state in {"expired","failed","denied"}:
            return {"ok":False,"error":"GERAMEG_EXEC_"+state.upper(),"request_id":request_id}
        time.sleep(0.25)
    return {"ok":False,"error":"GERAMEG_EXEC_TIMEOUT","request_id":request_id}



def framebridge_exec_geranas(
    script: str,
    authorized: bool,
    timeout_s: int = 120,
    job_id: str = "",
    sha256: str = "",
    include_stdout: bool = False,
) -> dict:
    """Historical alias for original local FBTERM execution on this receiver."""
    return _framebridge_exec_geranas_impl(
        script=script,
        authorized=authorized,
        timeout_s=timeout_s,
        job_id=job_id,
        sha256=sha256,
        include_stdout=include_stdout,
    )


def _framebridge_exec_geranas_impl(
    script: str,
    authorized: bool,
    timeout_s: int = 120,
    job_id: str = "",
    sha256: str = "",
    include_stdout: bool = False,
) -> dict:
    if authorized is not True:
        return {"ok":False,"error":"AUTHORIZED_TRUE_REQUIRED"}
    if not isinstance(script,str) or not script:
        return {"ok":False,"error":"SCRIPT_REQUIRED"}
    raw=script.encode()
    if len(raw)>262144:
        return {"ok":False,"error":"SCRIPT_TOO_LARGE"}
    digest=hashlib.sha256(raw).hexdigest()
    if sha256 and sha256.lower()!=digest:
        return {"ok":False,"error":"SCRIPT_SHA256_MISMATCH","computed_sha256":digest}
    try:
        timeout_s=int(timeout_s)
    except Exception:
        return {"ok":False,"error":"INVALID_TIMEOUT"}
    if not 1<=timeout_s<=600:
        return {"ok":False,"error":"INVALID_TIMEOUT"}
    if not job_id:
        job_id="mcp-"+time.strftime("%Y%m%d-%H%M%S")+"-"+digest[:8]+"-"+uuid.uuid4().hex[:12]
    if not JOB_RE.fullmatch(job_id):
        return {"ok":False,"error":"INVALID_JOB_ID"}
    req={
        "version":1,
        "job_id":job_id,
        "mode":"EXEC",
        "authorized":True,
        "timeout_s":timeout_s,
        "sha256":digest,
        "script":script,
    }
    result=adapter_request(req)
    result["mcp_job_id"]=job_id
    result["mcp_script_sha256"]=digest
    if isinstance(result.get("result_final"),str):
        result["result_final"]=sanitize_text(result["result_final"])
    if include_stdout is True:
        result.update(stdout_result(job_id))
    else:
        result.update({"stdout":"","stdout_sanitized":False,"stdout_truncated":False,"stdout_omitted_reason":"NOT_REQUESTED"})
    return result


def _cloud_dsm_rpc(payload: dict) -> dict:
    """Optional private local socket; root-only approval never exposed through MCP."""
    import socket as _socket
    import json as _json
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(38)
    try:
        sock.connect(os.environ.get("FRAMEBRIDGE_CLOUD_DSM_SOCKET", str(HOME / ".local/share/framebridge/dsm/cloud.sock")))
        body = (_json.dumps(payload, separators=(",",":"),ensure_ascii=True) + "\n").encode("utf8")
        if len(body)>12000:
            return {"ok":False,"error":"CLOUD_REQUEST_TOO_LARGE","target":"CLOUD"}
        sock.sendall(body)
        result = b""
        while len(result)<32768 and b"\n" not in result:
            part=sock.recv(8192)
            if not part:break
            result+=part
        if b"\n" not in result:
            return {"ok":False,"error":"CLOUD_RESPONSE_INCOMPLETE","target":"CLOUD"}
        output = _json.loads(result.split(b"\n",1)[0])
        if not isinstance(output,dict):
            return {"ok":False,"error":"CLOUD_RESPONSE_INVALID","target":"CLOUD"}
        output["target"]="CLOUD"
        return output
    except (OSError,ValueError,UnicodeError) as e:
        return {"ok":False,"error":"CLOUD_DSM_UNAVAILABLE",
                "error_type":type(e).__name__,"target":"CLOUD"}
    finally:
        sock.close()


def framebridge_cloud_dsm(action: str, authorized: bool = False, params_json: str = "{}") -> dict:
    """CLOUD XPEnology agentless DSM: persistent agentless DSM with temporary owner 2FA; typed reads and Owner-MFA gated writes.
    action: status,system_info,file_station_info,shares,list_folder,users,
            create_folder,rename_folder,pending.
    params_json: JSON object (no passwords/OTP). Writes require local owner OTP approval.
    """
    import json as _json
    try:
        params = _json.loads(params_json)
        if not isinstance(params,dict) or len(params_json)>2000:
            return {"ok":False,"error":"CLOUD_PARAMETERS_INVALID"}
    except (ValueError,TypeError):
        return {"ok":False,"error":"CLOUD_PARAMETERS_JSON_INVALID"}
    if action=="approve":
        return {"ok":False,"error":"OWNER_APPROVAL_LOCAL_TTY_ONLY"}
    return _cloud_dsm_rpc({"action":action,"authorized":bool(authorized),"params":params})


@_blocking_tools.tool()
def framebridge_exec(
    script: str,
    authorized: bool,
    timeout_s: int = 120,
    job_id: str = "",
    sha256: str = "",
    include_stdout: bool = False,
    target: str = "LOCAL",
) -> dict:
    """Execute through original local FBTERM or a confirmed Fleet node alias."""
    if target == "LOCAL":
        result = _framebridge_exec_geranas_impl(script, authorized, timeout_s, job_id, sha256, include_stdout)
        if isinstance(result, dict):
            result = dict(result)
            result["target"] = "LOCAL"
        return result
    return expanded_execute_node(target, script, authorized, timeout_s, job_id, sha256, include_stdout)


def _cua_frame_fields(frame_output: str) -> dict:
    result={}
    safe_lines=[]
    for line in str(frame_output or "").splitlines():
        if "=" not in line:
            safe_lines.append(line)
            continue
        key,value=line.split("=",1)
        key=key.strip()
        if key=="BASE64":
            result["image_base64"]=value.strip()
            continue
        if key=="MIME":
            result["image_mime"]=value.strip()
        if key in {
            "RESULTADO_FINAL","VISUAL_CAPTURE_GATE","CUA_PROTOCOL_VERSION",
            "CHAT_AUTHORIZATION_GATE","CHAT_AUTHORIZATION_MODE","ACTION","SEMANTIC",
            "COORD_SPACE","COORD_SPACE_WIDTH","COORD_SPACE_HEIGHT",
            "REQUESTED_X","REQUESTED_Y","REQUESTED_X1","REQUESTED_Y1",
            "REQUESTED_X2","REQUESTED_Y2","ROOT_X","ROOT_Y","ROOT_X1","ROOT_Y1",
            "ROOT_X2","ROOT_Y2","BUTTON","KEY","DIRECTION","CLICKS","CHAR_COUNT",
            "TYPE_MODE","WIDTH","HEIGHT","SOURCE_X","SOURCE_Y","SOURCE_WIDTH",
            "SOURCE_HEIGHT","DISPLAY_WIDTH","DISPLAY_HEIGHT","CAPTURE_SCOPE",
            "COORD_SPACE_DEFAULT","COORD_SPACE_AVAILABLE","WINDOW_X","WINDOW_Y",
            "WINDOW_WIDTH","WINDOW_HEIGHT","ACTIVE_WINDOW_ID","ACTIVE_WINDOW_ID_BEFORE",
            "ACTIVE_WINDOW_ID_AFTER","WINDOW_TITLE","ACTIVE_WINDOW_TITLE_BEFORE",
            "ACTIVE_WINDOW_TITLE_AFTER","CURSOR_X","CURSOR_Y","SHA256","BYTES","MIME",
            "EXPECTED_WINDOW_GATE","EXPECTED_SOURCE_GEOMETRY_GATE",
        }:
            result[key.lower()]=sanitize_text(value.strip())
        safe_lines.append(line)
    result["frame_output"]=sanitize_text("\n".join(
        line for line in safe_lines if not line.startswith("BASE64=")
    ))
    return result


def local_cua_request(cua: dict, authorized: bool, timeout_s: int, job_id: str) -> dict:
    if authorized is not True:
        return {"ok":False,"error":"AUTHORIZED_TRUE_REQUIRED"}
    if not isinstance(cua,dict):
        return {"ok":False,"error":"CUA_PAYLOAD_REQUIRED"}
    action=str(cua.get("action") or "").upper().strip()
    if action not in {"OBSERVE","CLICK","DOUBLE_CLICK","TYPE","KEY","SCROLL","DRAG"}:
        return {"ok":False,"error":"CUA_ACTION_UNSUPPORTED"}
    try:
        timeout_s=int(timeout_s)
    except Exception:
        return {"ok":False,"error":"INVALID_CUA_TIMEOUT"}
    if not 1<=timeout_s<=180:
        return {"ok":False,"error":"INVALID_CUA_TIMEOUT"}
    if not job_id:
        seed=json.dumps({"action":action,"time":time.time_ns()},sort_keys=True).encode()
        digest=hashlib.sha256(seed).hexdigest()
        job_id="cua-local-"+time.strftime("%Y%m%d-%H%M%S")+"-"+digest[:8]
    if not JOB_RE.fullmatch(job_id):
        return {"ok":False,"error":"INVALID_JOB_ID"}

    result=checked_cua_adapter_request({
        "version":1,"mode":"EXEC","request_type":"cua",
        "authorized":True,"job_id":job_id,"timeout_s":timeout_s,"cua":cua,
    })
    if not isinstance(result,dict):
        return {"ok":False,"error":"CUA_LOCAL_ADAPTER_RESULT_INVALID","target":"LOCAL"}
    value=dict(result)
    frame_output=str(value.pop("frame_output",""))
    if frame_output:
        value.update(_cua_frame_fields(frame_output))
    value["target"]="LOCAL"
    return value

def dispatch_cua_request(cua: dict, authorized: bool, timeout_s: int, job_id: str, target: str = "LOCAL") -> dict:
    """Reuse the original local CUA contract and route confirmed remote nodes."""
    if target == "LOCAL":
        return local_cua_request(cua, authorized, timeout_s, job_id)
    result = expanded_cua_node(target, cua, authorized, timeout_s, job_id)
    if not isinstance(result, dict):
        return {"ok":False,"error":"NODE_CUA_RESPONSE_INVALID"}
    result = dict(result)
    frame_output = str(result.pop("frame_output", ""))
    if frame_output:
        result.update(_cua_frame_fields(frame_output))
    return result


def asus_cua_request(cua: dict, authorized: bool, timeout_s: int, job_id: str) -> dict:
    if authorized is not True:
        return {"ok":False,"error":"AUTHORIZED_TRUE_REQUIRED"}
    if not isinstance(cua,dict):
        return {"ok":False,"error":"CUA_PAYLOAD_REQUIRED"}
    action=str(cua.get("action") or "").upper().strip()
    if action not in {"OBSERVE","CLICK","DOUBLE_CLICK","TYPE","KEY","SCROLL","DRAG"}:
        return {"ok":False,"error":"CUA_ACTION_UNSUPPORTED"}
    try:
        timeout_s=int(timeout_s)
    except Exception:
        return {"ok":False,"error":"INVALID_CUA_TIMEOUT"}
    if not 1<=timeout_s<=180:
        return {"ok":False,"error":"INVALID_CUA_TIMEOUT"}
    if not job_id:
        seed=json.dumps({"action":action,"time":time.time_ns()},sort_keys=True).encode()
        digest=hashlib.sha256(seed).hexdigest()
        job_id="cua-geranas-"+time.strftime("%Y%m%d-%H%M%S")+"-"+digest[:8]
    if not JOB_RE.fullmatch(job_id):
        return {"ok":False,"error":"INVALID_JOB_ID"}

    if not ASUS_NODE_ID:
        return {"ok":False,"error":"REMOTE_NODE_NOT_CONFIGURED"}
    queued=broker_post(
        "/fbpair/v1/control/admin-cua",
        {
            "node_id":ASUS_NODE_ID,
            "payload":{
                "authorized":True,
                "job_id":job_id,
                "timeout_s":timeout_s,
                "cua":cua,
            },
        },
        timeout_s=10.0,
    )
    if queued.get("ok") is not True:
        return {"ok":False,"error":"ASUS_CUA_QUEUE_FAILED","broker":queued}
    rid=str(queued.get("request_id") or "")
    deadline=time.monotonic()+timeout_s+75
    while time.monotonic()<deadline:
        status=broker_post("/fbpair/v1/control/result",{"request_id":rid},timeout_s=5.0)
        if status.get("ok") is not True:
            time.sleep(.2)
            continue
        state=str(status.get("state") or "")
        if state=="complete":
            outer=status.get("result") or {}
            if not isinstance(outer,dict):
                return {"ok":False,"error":"ASUS_CUA_RESULT_INVALID","request_id":rid}
            if outer.get("ok") is not True:
                return {"ok":False,"error":"ASUS_CUA_NODE_FAILED","request_id":rid,"node_result":outer}
            value=outer.get("result") or {}
            if not isinstance(value,dict):
                return {"ok":False,"error":"ASUS_CUA_ADAPTER_RESULT_INVALID","request_id":rid}
            value=dict(value)
            frame_output=str(value.pop("frame_output",""))
            if frame_output:
                value.update(_cua_frame_fields(frame_output))
            value["broker_request_id"]=rid
            value["target"]="ASUS"
            return value
        if state in {"expired","failed","denied"}:
            return {"ok":False,"error":"ASUS_CUA_"+state.upper(),"request_id":rid}
        time.sleep(.2)
    return {"ok":False,"error":"ASUS_CUA_TIMEOUT","request_id":rid}


@_blocking_tools.tool()
def framebridge_cua(
    action: str,
    authorized: bool,
    semantic_action: str = "GENERIC",
    coord_space: str = "ROOT",
    capture_scope: str = "ROOT",
    observe_after: bool = False,
    observe_scope: str = "WINDOW",
    x: int = -1,
    y: int = -1,
    button: str = "left",
    text: str = "",
    type_mode: str = "KEYS",
    key: str = "",
    direction: str = "DOWN",
    clicks: int = 3,
    x1: int = -1,
    y1: int = -1,
    x2: int = -1,
    y2: int = -1,
    steps: int = 8,
    duration_ms: int = 320,
    preview_width: int = 0,
    preview_height: int = 0,
    expected_window_id: str = "",
    expected_source_width: int = 0,
    expected_source_height: int = 0,
    timeout_s: int = 60,
    job_id: str = "",
    target: str = "LOCAL",
):
    """Use the configured graphical display; display_context labels physical or virtual. OBSERVE with capture_scope=node:ALIAS captures that node's actual active physical screen."""
    if authorized is not True:
        return {"ok":False,"error":"AUTHORIZED_TRUE_REQUIRED"}
    action=str(action).upper().strip()
    node_scope = str(capture_scope).strip()
    if node_scope.lower().startswith("node:"):
        if action != "OBSERVE":
            return {"ok": False, "error": "NODE_SCREEN_SCOPE_OBSERVE_ONLY", "hint": "Use the bound screen tools for TTY input or the graphical CUA with target for X11 input."}
        return _real_screen_client.call("observe", node_scope[5:], True)
    if action not in {"OBSERVE","CLICK","DOUBLE_CLICK","TYPE","KEY","SCROLL","DRAG"}:
        return {"ok":False,"error":"CUA_ACTION_UNSUPPORTED"}
    coord_space=str(coord_space).upper().strip()
    capture_scope=str(capture_scope).upper().strip()
    observe_scope=str(observe_scope).upper().strip()
    if coord_space not in {"ROOT","PREVIEW","WINDOW","WINDOW_PREVIEW"}:
        return {"ok":False,"error":"COORDINATE_SPACE_INVALID"}
    if capture_scope not in {"ROOT","WINDOW"} or observe_scope not in {"ROOT","WINDOW"}:
        return {"ok":False,"error":"CAPTURE_SCOPE_INVALID"}

    payload={
        "action":action,
        "semantic_action":str(semantic_action).upper().strip() or "GENERIC",
        "coord_space":coord_space,
        "capture_scope":capture_scope,
    }
    if preview_width>0: payload["preview_width"]=int(preview_width)
    if preview_height>0: payload["preview_height"]=int(preview_height)
    if expected_window_id: payload["expected_window_id"]=str(expected_window_id)
    if expected_source_width>0: payload["expected_source_width"]=int(expected_source_width)
    if expected_source_height>0: payload["expected_source_height"]=int(expected_source_height)

    if action in {"CLICK","DOUBLE_CLICK"}:
        if x<0 or y<0: return {"ok":False,"error":"CLICK_COORDINATES_REQUIRED"}
        payload.update({"x":int(x),"y":int(y),"button":str(button).lower()})
    elif action=="TYPE":
        if not text: return {"ok":False,"error":"TYPE_TEXT_REQUIRED"}
        tm=str(type_mode).upper().strip()
        if tm not in {"KEYS","CLIPBOARD_EXACT"}: return {"ok":False,"error":"TYPE_MODE_INVALID"}
        payload.update({"text":str(text),"type_mode":tm})
    elif action=="KEY":
        if not key: return {"ok":False,"error":"KEY_REQUIRED"}
        payload["key"]=str(key)
    elif action=="SCROLL":
        payload["direction"]=str(direction).upper()
        payload["steps"]=int(steps)
        payload["clicks"]=int(clicks)
        if x>=0 and y>=0:
            payload.update({"x":int(x),"y":int(y)})
        elif x>=0 or y>=0:
            return {"ok":False,"error":"SCROLL_POINT_INCOMPLETE"}
    elif action=="DRAG":
        if min(x1,y1,x2,y2)<0: return {"ok":False,"error":"DRAG_COORDINATES_REQUIRED"}
        payload.update({
            "x1":int(x1),"y1":int(y1),"x2":int(x2),"y2":int(y2),
            "button":str(button).lower(),"steps":int(steps),"duration_ms":int(duration_ms),
        })

    result=dispatch_cua_request(payload,True,timeout_s,job_id,target)
    if bool(observe_after) and action!="OBSERVE" and bool(result.get("ok")):
        post=dict(payload)
        post.clear()
        post={"action":"OBSERVE","capture_scope":observe_scope}
        if expected_window_id:
            post["expected_window_id"]=str(expected_window_id)
        post_id=(str(job_id or "cua")[:72]+"-post")[:80]
        post_result=dispatch_cua_request(post,True,timeout_s,post_id,target)
        result["post_ok"]=bool(post_result.get("ok"))
        result["post_resultado_final"]=post_result.get("resultado_final")
        for key_name in (
            "image_base64","image_mime","source_width","source_height",
            "display_width","display_height","capture_scope","active_window_id",
            "window_title","sha256","bytes"
        ):
            if key_name in post_result:
                result[key_name]=post_result[key_name]

    image_b64=result.get("image_base64")
    mime=str(result.get("image_mime") or "")
    if isinstance(image_b64,str) and image_b64:
        try:
            data=base64.b64decode(image_b64,validate=True)
            fmt="jpeg" if mime=="image/jpeg" else ("png" if mime=="image/png" else None)
            if fmt:
                meta=dict(result)
                meta.pop("image_base64",None)
                return [meta, Image(data=data,format=fmt)]
        except Exception:
            result["image_decode_error"]="INVALID_BASE64"
    return result


async def hub_call(tool: str, args: dict) -> dict:
    try:
        async with streamablehttp_client(HUB_URL) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result=await session.call_tool(tool,args)
    except Exception as exc:
        return {"ok":False,"error":"FLEET_HUB_CALL_FAILED","detail":str(exc)[:500]}
    if bool(getattr(result,"isError",False)):
        return {"ok":False,"error":"FLEET_HUB_TOOL_ERROR"}
    structured=getattr(result,"structuredContent",None)
    if isinstance(structured,dict):
        return structured
    for item in getattr(result,"content",[]):
        txt=getattr(item,"text",None)
        if isinstance(txt,str):
            try:
                obj=json.loads(txt)
            except Exception:
                continue
            if isinstance(obj,dict):
                return obj
            return {"ok":True,"result":obj}
    return {"ok":False,"error":"FLEET_HUB_EMPTY_RESULT"}

@mcp.tool()
async def framebridge_fleet_nodes() -> dict:
    """Read enrolled node inventory from this profile hub; no enrollment or changes."""
    return await hub_call("framebridge_fleet_nodes",{})

@mcp.tool()
async def framebridge_fleet_node_system_status(node: str) -> dict:
    """Read a confirmed node system status without changing its configuration."""
    return await hub_call("framebridge_fleet_node_system_status",{"node":node})

@mcp.tool()
async def framebridge_fleet_node_hardware_summary(node: str) -> dict:
    """Read a confirmed node hardware summary; this operation is observational."""
    return await hub_call("framebridge_fleet_node_hardware_summary",{"node":node})

@mcp.tool()
async def framebridge_fleet_node_storage_inspect(node: str) -> dict:
    """Inspect node storage metadata; never partition, format, mount or repair."""
    return await hub_call("framebridge_fleet_node_storage_inspect",{"node":node})

@mcp.tool()
async def framebridge_fleet_node_network_inspect(node: str) -> dict:
    """Inspect confirmed node network state; no address, routing or firewall changes."""
    return await hub_call("framebridge_fleet_node_network_inspect",{"node":node})

@mcp.tool()
async def framebridge_fleet_node_service_status(node: str, service: str) -> dict:
    """Read a named service status on a confirmed node; never start, stop or enable it."""
    return await hub_call("framebridge_fleet_node_service_status",{"node":node,"service":service})

def _screen_exec_request(
    *, script: str, authorized: bool, timeout_s: int = 120,
    job_id: str = "", sha256: str = "", include_stdout: bool = False,
    target: str = "LOCAL",
) -> dict:
    """Keep authenticated Fleet identity without opening an EXEC terminal."""
    return expanded_execute_screen_node(target, script, authorized, timeout_s,
                                        job_id, sha256, include_stdout)


from framebridge_expanded.real_screen.screen_frontdoor import register_framebridge_screen_tools, resolve_profile_node
_real_screen_client = register_framebridge_screen_tools(
    _blocking_tools, _screen_exec_request, image_factory=Image,
    resolve_node=resolve_profile_node, stdout_limit_bytes=65536, transfer_timeout_s=45,
)
from framebridge_expanded.real_screen.screen_presentation import register_screen_presentation
register_screen_presentation(_blocking_tools, _real_screen_client)

from framebridge_expanded.tty_mcp import register_tools as register_tty_tools
register_tty_tools(_blocking_tools, framebridge_exec, screen_client=_real_screen_client)

if __name__ == "__main__":
    from framebridge_expanded.http_server import run_streamable_mcp
    run_streamable_mcp(mcp)
