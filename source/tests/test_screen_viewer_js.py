"""Run the actual viewer JS under GJS with a small DOM fixture.

This tests JS state and integrity handling, not browser decoding, CSP or ChatGPT UI.
"""
import base64
import json
import shutil
import subprocess
import unittest

from framebridge_expanded.real_screen.screen_presentation import VIEWER_HTML
from test_screen_frontdoor import png


@unittest.skipUnless(shutil.which("gjs"), "GJS is not installed on this test host")
class ScreenViewerJavaScriptTests(unittest.TestCase):
    def test_original_bytes_recovery_errors_and_inflight_teardown(self):
        data = png()
        import hashlib
        payload = {
            "structuredContent": {
                "ok": True, "node": "LOCAL", "hostname": "test-fixture",
                "result": {
                    "capture_id": "1" * 32, "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data), "width": 100, "height": 40,
                    "captured_at": "2026-10-24T12:00:00Z",
                },
            },
            "_meta": {"framebridgeImage": {"mimeType": "image/png", "data": base64.b64encode(data).decode()}},
        }
        script = VIEWER_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        bootstrap = r"""
const GLib=imports.gi.GLib;
const listeners={},elements={};
class Element {
 constructor(){this.hidden=true;this.disabled=true;this.textContent="";this.attrs={};const classes=new Set();this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x)};}
 setAttribute(k,v){this.attrs[k]=v;} removeAttribute(k){delete this.attrs[k];if(k==="src"||k==="href")this[k]="";}
 async decode(){const b=GLib.base64_decode(this.src.split(",")[1]);const d=new DataView(b.buffer,b.byteOffset,b.byteLength);this.naturalWidth=d.getUint32(16);this.naturalHeight=d.getUint32(20);}
}
const fakeDocument={getElementById:id=>elements[id]||(elements[id]=new Element()),body:{scrollHeight:640}};
const fakeWindow={parent:{postMessage:()=>{}},addEventListener:(name,fn)=>{listeners[name]=fn;}};
globalThis.setTimeout=()=>0;
globalThis.atob=value=>Array.from(GLib.base64_decode(value),x=>String.fromCharCode(x)).join("");
async function realDigest(name,bytes){if(name!=="SHA-256")throw Error("wrong algorithm");const hex=GLib.compute_checksum_for_data(GLib.ChecksumType.SHA256,bytes);return Uint8Array.from(hex.match(/../g),x=>parseInt(x,16)).buffer;}
globalThis.crypto={subtle:{digest:realDigest}};
let checks=0;
function check(v,m){if(!v)throw Error(m);checks++;}
"""
        cases = r"""
return (async()=>{
 const fixture=PAYLOAD,original="data:image/png;base64,"+fixture._meta.framebridgeImage.data;
 await render(fixture);
 check(img.src===original,"original bytes changed");check(!img.hidden&&!viewport.hidden,"valid image hidden");
 check(statusEl.textContent.includes("Original PNG verified"),"integrity success missing");
 check(details.textContent.includes(fixture.structuredContent.result.sha256),"hash not displayed");
 check(download.href===original,"download bytes changed");
 native.onclick();check(viewport.classList.contains("native"),"native size unavailable");
 fit.onclick();check(!viewport.classList.contains("native"),"fit unavailable");
 await render({structuredContent:{ok:false,error:"TEST_ERROR"}});
 check(img.hidden&&viewport.hidden,"error did not hide image");check(active===null,"error retained active capture");
 check(!img.src&&!download.href&&!details.textContent,"error retained stale image data");
 await render(fixture);check(!img.hidden&&img.src===original,"same original did not recover after error");
 const bad=JSON.parse(JSON.stringify(fixture));bad.structuredContent.result.sha256="0".repeat(64);
 await render(bad);check(img.hidden&&statusEl.textContent.includes("SHA-256 mismatch"),"corrupt digest not rejected");
 await render(fixture);check(!img.hidden,"valid image did not recover after bad digest");
 const slow=JSON.parse(JSON.stringify(fixture));slow.structuredContent.result.capture_id="2".repeat(32);
 let finish;crypto.subtle.digest=(...args)=>new Promise(resolve=>{finish=()=>realDigest(...args).then(resolve);});
 const inflight=render(slow);check(typeof finish==="function","digest did not start");
 listeners.message({source:window.parent,data:{jsonrpc:"2.0",method:"ui/notifications/teardown",params:{}}});
 await finish();await inflight;
 check(img.hidden&&viewport.hidden&&!img.src,"inflight render revived after teardown");
 check(fit.disabled&&native.disabled&&full.disabled,"teardown left controls enabled");
 return checks;
})();
"""
        executable = bootstrap + "\nconst run = function(window,document,crypto,atob,setTimeout){" + script + cases.replace("PAYLOAD", json.dumps(payload)) + "};\n"
        executable += 'const loop=new GLib.MainLoop(null,false);let failed=false;run(fakeWindow,fakeDocument,globalThis.crypto,globalThis.atob,globalThis.setTimeout).then(n=>{print("PASS "+n+" viewer state assertions");loop.quit();}).catch(e=>{failed=true;printerr(String(e.stack||e));loop.quit();});loop.run();if(failed)imports.system.exit(1);'
        result = subprocess.run([shutil.which("gjs"), "-c", executable],
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS 16 viewer state assertions", result.stdout)


if __name__ == "__main__":
    unittest.main()
