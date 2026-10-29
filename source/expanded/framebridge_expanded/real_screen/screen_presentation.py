"""Display an already captured, authenticated PNG in MCP Apps-capable clients.

No HTTP image endpoint, upload, credential, external asset, or new capture is used.
The normal ImageContent remains available to clients that do not render MCP Apps.
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from .screen_frontdoor import READ

TEMPLATE_URI = "ui://framebridge/screen-v1.html"
TOOL_META = {
    "ui": {"resourceUri": TEMPLATE_URI},
    "openai/outputTemplate": TEMPLATE_URI,
    "openai/toolInvocation/invoking": "Loading original screen capture",
    "openai/toolInvocation/invoked": "Original screen capture ready",
}
RESOURCE_META = {
    "ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": []}},
    "openai/widgetDescription": (
        "FrameBridge original screen capture viewer. Shows the full saved PNG, "
        "its node, capture identifier, dimensions and verified SHA-256. "
        "This is a historical capture, not a live screen."
    ),
    "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"]},
}


class ScreenViewResult(BaseModel):
    ok: bool
    node: str
    requested_node: str | None = None
    node_id: str | None = None
    hostname: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


VIEWER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FrameBridge original screen capture</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{margin:0;padding:12px;background:Canvas;color:CanvasText}
header{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
h1{font-size:16px;margin:0}p{font-size:13px;margin:6px 0}
#status{font-weight:600}#details{overflow-wrap:anywhere;font:12px ui-monospace,monospace;white-space:pre-wrap}
.controls{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}
button,a{font:inherit;padding:6px 10px;border:1px solid GrayText;border-radius:6px;background:ButtonFace;color:ButtonText;text-decoration:none}
button:disabled,a[aria-disabled=true]{opacity:.5;pointer-events:none}
#viewport{overflow:auto;border:1px solid GrayText;border-radius:6px;max-height:75vh}
#screen{display:block;max-width:100%;height:auto;margin:auto}
#viewport.native #screen{max-width:none;margin:0}
#screen[hidden],#viewport[hidden]{display:none}
</style></head><body>
<header><h1>FrameBridge original screen capture</h1><span>Saved capture · read only</span></header>
<p id="status" role="status">Waiting for the original PNG.</p>
<pre id="details"></pre>
<div class="controls">
<button id="fit" disabled>Fit full screen</button><button id="native" disabled>Original size</button>
<button id="fullscreen" disabled>Fullscreen</button>
<a id="download" aria-disabled="true">Download original PNG</a>
</div>
<div id="viewport" hidden><img id="screen" hidden alt="Full original FrameBridge screen capture"></div>
<script>
"use strict";
const statusEl=document.getElementById("status"),details=document.getElementById("details");
const img=document.getElementById("screen"),viewport=document.getElementById("viewport");
const fit=document.getElementById("fit"),native=document.getElementById("native");
const full=document.getElementById("fullscreen"),download=document.getElementById("download");
let serial=0,active=null,nextId=1;
const pending=new Map();
function post(method,params,id){const m={jsonrpc:"2.0",method,params};if(id!==undefined)m.id=id;window.parent.postMessage(m,"*");}
function request(method,params){const id=nextId++;post(method,params,id);return new Promise((resolve,reject)=>{pending.set(id,{resolve,reject});setTimeout(()=>{if(pending.has(id)){pending.delete(id);reject(new Error("Host bridge unavailable"));}},5000);});}
function error(message){active=null;img.removeAttribute("src");details.textContent="";img.hidden=true;viewport.hidden=true;statusEl.textContent=message;[fit,native,full].forEach(x=>x.disabled=true);download.removeAttribute("href");download.setAttribute("aria-disabled","true");}
function envelope(value){
 if(!value||typeof value!=="object")return null;
 if(value.structuredContent||value._meta?.framebridgeImage)return value;
 for(const name of ["mcp_tool_result","call_tool_result","toolResult","result"]){const item=value[name];if(item&&typeof item==="object"&&(item.structuredContent||item._meta?.framebridgeImage))return item;}
 return null;
}
async function render(raw){
 const payload=envelope(raw);if(!payload)return;
 const n=++serial,meta=payload.structuredContent;
 if(!meta?.ok){error("Original capture unavailable: "+String(meta?.error||"unknown error"));return;}
 const manifest=meta.result||{},image=payload._meta?.framebridgeImage;
 if(!image||image.mimeType!=="image/png"||typeof image.data!=="string"){error("The host did not provide the original PNG to this viewer.");return;}
 if(!/^[a-f0-9]{32}$/.test(manifest.capture_id||"")||!/^[a-f0-9]{64}$/.test(manifest.sha256||"")){error("Invalid capture reference.");return;}
 const identity=String(meta.node)+" / "+manifest.capture_id+" / "+manifest.sha256;
 if(active===identity)return;
 try{
  statusEl.textContent="Checking original PNG integrity…";
  const binary=atob(image.data),bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));
  if(bytes.length!==manifest.bytes||bytes.length<33||bytes[0]!==137||bytes[1]!==80||bytes[2]!==78||bytes[3]!==71)throw new Error("PNG length or signature mismatch");
  const view=new DataView(bytes.buffer);
  if(view.getUint32(16)!==manifest.width||view.getUint32(20)!==manifest.height)throw new Error("PNG dimensions do not match the capture");
  if(!globalThis.crypto?.subtle)throw new Error("This host cannot verify SHA-256");
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)),x=>x.toString(16).padStart(2,"0")).join("");
  if(digest!==manifest.sha256)throw new Error("PNG SHA-256 mismatch");
  if(n!==serial)return;
  img.src="data:image/png;base64,"+image.data;
  await img.decode();
  if(n!==serial)return;
  if(img.naturalWidth!==manifest.width||img.naturalHeight!==manifest.height)throw new Error("Decoded PNG dimensions mismatch");
  active=identity;
  img.alt="Original full screen of "+String(meta.hostname||meta.node)+", capture "+manifest.capture_id;
  img.hidden=false;viewport.hidden=false;viewport.classList.remove("native");
  const time=manifest.captured_at||manifest.timestamp||manifest.created_at||"not supplied";
  details.textContent="Node: "+String(meta.node)+" ("+String(meta.hostname||"")+")\nCapture: "+manifest.capture_id+"\nCaptured: "+String(time)+"\nOriginal: "+manifest.width+" × "+manifest.height+" · "+manifest.bytes+" bytes\nSHA-256: "+digest;
  statusEl.textContent="Original PNG verified. This saved capture does not show subsequent screen changes.";
  [fit,native,full].forEach(x=>x.disabled=false);
  download.href=img.src;download.download="framebridge-"+manifest.capture_id+".png";download.removeAttribute("aria-disabled");
  window.openai?.notifyIntrinsicHeight?.(Math.min(document.body.scrollHeight,900));
 }catch(e){if(n===serial)error("Cannot display the original capture: "+String(e?.message||e));}
}
function fromOpenAI(){const api=window.openai;if(!api)return;const out=envelope(api.toolResponseMetadata);if(out)render(out);else if(api.toolOutput)render({structuredContent:api.toolOutput,_meta:api.toolResponseMetadata});}
window.addEventListener("message",event=>{
 if(event.source!==window.parent)return;
 const m=event.data;if(!m||m.jsonrpc!=="2.0")return;
 if(m.id!==undefined&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);if(m.error)p.reject(m.error);else p.resolve(m.result);return;}
 if(m.method==="ui/notifications/tool-result")render(m.params);
 if(m.method==="ui/notifications/teardown"){++serial;error("Screen viewer closed.");}
});
window.addEventListener("openai:set_globals",fromOpenAI,{passive:true});
fit.onclick=()=>viewport.classList.remove("native");
native.onclick=()=>viewport.classList.add("native");
full.onclick=()=>{if(window.openai?.requestDisplayMode)window.openai.requestDisplayMode({mode:"fullscreen"});else request("ui/request-display-mode",{mode:"fullscreen"}).catch(()=>{statusEl.textContent="Fullscreen is unavailable. Use Original size to inspect all pixels.";});};
request("ui/initialize",{appInfo:{name:"FrameBridge screen viewer",version:"1.0.0"},appCapabilities:{},protocolVersion:"2026-01-26"}).then(()=>post("ui/notifications/initialized",{})).catch(()=>fromOpenAI());
fromOpenAI();
</script></body></html>"""


def presentation_result(response):
    """Preserve the exact image for the model and pass it to the optional UI."""
    if not isinstance(response, list) or len(response) != 2:
        metadata = response if isinstance(response, dict) else {"ok": False, "node": "", "error": "SCREEN_PRESENTATION_FAILED"}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(metadata, sort_keys=True))],
            structuredContent=metadata, isError=True,
        )
    metadata, image = response
    content = image.to_image_content()
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(metadata, sort_keys=True)), content],
        structuredContent=metadata,
        _meta={"framebridgeImage": {"mimeType": content.mimeType, "data": content.data}},
    )


def register_screen_presentation(mcp, client):
    """Add an explicit render/replay tool without changing observation semantics."""
    @mcp.resource(TEMPLATE_URI, name="FrameBridge original screen",
                  description="Offline viewer for an authenticated saved screen PNG.",
                  mime_type="text/html;profile=mcp-app", meta=dict(RESOURCE_META))
    def framebridge_screen_viewer():
        return VIEWER_HTML

    @mcp.tool(annotations=dict(READ), meta=dict(TOOL_META))
    def framebridge_screen_show(node: str, capture_id: str, authorized: bool) -> Annotated[CallToolResult, ScreenViewResult]:
        """Display or redisplay a saved original screen PNG, without taking a new capture.

        First use framebridge_screen_observe, then pass its node and capture_id here.
        This returns the same full PNG bytes and SHA-256, and an optional ChatGPT viewer.
        It is a historical read only view; use a fresh observation before controlling a screen.
        """
        return presentation_result(client.call("read_capture", node, authorized, capture_id=capture_id))

    return framebridge_screen_show
