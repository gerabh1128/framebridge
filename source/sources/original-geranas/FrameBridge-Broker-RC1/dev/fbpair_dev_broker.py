#!/usr/bin/env python3
"""FBPAIR/1 development broker.

DEV ONLY:
- supports real ChatGPT account authentication for local-browser lab validation;
- loopback/private-LAN bind recommended;
- used to test node identity, proof-of-possession, account binding and device-flow mechanics.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import ipaddress
import json
import re
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from framebridge_linux_chatgpt.node_identity import (
    parse_ed25519_public_key_line,
    verify_pairing_payload,
)
from framebridge_linux_chatgpt.pairing import (
    FBPAIR_PROTOCOL,
    canonical,
    pairing_signing_payload,
    sha256_hex,
)
from framebridge_linux_chatgpt.chatgpt_auth import (
    ChatGPTAuthError,
    authenticate_interactive,
)


ALPHABET="ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

READ_CAPS={
    "system.status","system.resources","system.processes","system.time",
    "hardware.summary","kernel.modules",
    "storage.inspect","storage.usage","storage.mounts",
    "network.inspect","network.sockets","network.dns",
    "package.status","package.search","package.upgradable",
    "service.status","service.list","journal.service",
    "user.list","group.list",
    "filesystem.stat","filesystem.list","filesystem.hash","filesystem.read_text",
}

ADMIN_EXEC_NODE_LABEL="ASUS"
ADMIN_EXEC_GERAMEG_NODE_ID="76885f27-da2e-4cca-bbde-6ec981e91e68"
ADMIN_EXEC_MAX_SCRIPT_BYTES=262144
ADMIN_EXEC_JOB_RE=re.compile(r"^[A-Za-z0-9._-]{1,80}$")



def approve_via_chatgpt(store, code: str, account_binding: dict | None=None, auth_fn=authenticate_interactive):
    code=str(code or "").strip()
    if not code:
        return False,"user_code_missing"
    with store.lock:
        pid=store.by_user.get(code)
        item=store.by_pair.get(pid) if pid else None
        if not isinstance(item,dict):
            return False,"pair_not_found"
        if item.get("state")!="pending":
            return False,"pair_not_pending"
        request=item.get("request") or {}
        host_id=str(request.get("node_host_id") or "")
    if not host_id:
        return False,"node_host_id_missing"
    expected=account_binding if isinstance(account_binding,dict) else {}
    saved_client_id=str(expected.get("client_id") or "")
    kwargs={"client_id":saved_client_id} if saved_client_id else {}
    try:
        auth=auth_fn(host_id,**kwargs)
    except ChatGPTAuthError as exc:
        return False,str(exc)
    except Exception:
        return False,"chatgpt_auth_failed"
    if not isinstance(auth,dict) or auth.get("ok") is not True:
        return False,"chatgpt_auth_invalid"
    binding={
        "issuer":auth.get("issuer"),
        "account_binding_id":auth.get("account_binding_id"),
        "client_id":auth.get("client_id"),
    }
    if not all(isinstance(binding[k],str) and binding[k] for k in binding):
        return False,"chatgpt_auth_binding_invalid"
    expected_binding=str(expected.get("account_binding_id") or "")
    expected_issuer=str(expected.get("issuer") or "")
    if expected_binding and binding["account_binding_id"]!=expected_binding:
        return False,"chatgpt_account_binding_mismatch"
    if expected_issuer and binding["issuer"]!=expected_issuer:
        return False,"chatgpt_account_issuer_mismatch"
    ok=store.approve_authenticated(code,binding)
    return (True,None) if ok else (False,"pair_approval_failed")


def user_code() -> str:
    raw="".join(secrets.choice(ALPHABET) for _ in range(8))
    return raw[:4]+"-"+raw[4:]


class Store:
    def __init__(self, discovery_path: Path | None=None, public_base: str=""):
        self.lock=threading.Lock()
        self.by_pair={}
        self.by_user={}
        self.discovery_path=Path(discovery_path) if discovery_path else None
        self.public_base=str(public_base).rstrip("/")
        self.control_queue=collections.defaultdict(collections.deque)
        self.control_requests={}
        self.control_results={}
        self.node_nonces=collections.defaultdict(dict)
        self.node_last_seen={}

    def _persist_discovery(self, item, receipt):
        if self.discovery_path is None:
            return
        path=self.discovery_path
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        current={"schema":1,"nodes":[]}
        if path.is_file():
            try:
                loaded=json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded,dict) and isinstance(loaded.get("nodes"),list):
                    current=loaded
            except Exception:
                pass
        record={
            "schema":1,
            "source":"fbpair",
            "state":"paired",
            "node_id":receipt["node_id"],
            "node_label":receipt["node_label"],
            "node_host_id":receipt["node_host_id"],
            "provider":receipt["provider"],
            "account_binding_id":receipt["account_binding_id"],
            "grant_id":receipt["grant_id"],
            "scopes":receipt["scopes"],
            "paired_at":receipt["paired_at"],
            "revocation_id":receipt["revocation_id"],
            "transport":"broker_outbound",
            "capability_profile":"paired",
            "broker_base_url":self.public_base,
            "public_key_ssh":str((item.get("request") or {}).get("public_key_ssh") or ""),
        }
        nodes=[
            n for n in current.get("nodes",[])
            if n.get("node_id")!=record["node_id"]
        ]
        nodes.append(record)
        current={"schema":1,"nodes":sorted(nodes,key=lambda n:str(n.get("node_label") or n.get("node_id")))}
        tmp=path.with_name("."+path.name+".tmp")
        tmp.write_text(json.dumps(current,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        os.chmod(tmp,0o600)
        os.replace(tmp,path)

    def _remove_discovery_node(self,node_id):
        if self.discovery_path is None or not self.discovery_path.is_file():
            return
        path=self.discovery_path
        try:
            current=json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        nodes=current.get("nodes") if isinstance(current,dict) else None
        if not isinstance(nodes,list):
            return
        kept=[n for n in nodes if str(n.get("node_id") or "")!=str(node_id)]
        if len(kept)==len(nodes):
            return
        value={"schema":1,"nodes":kept}
        tmp=path.with_name("."+path.name+".tmp")
        tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        os.chmod(tmp,0o600)
        os.replace(tmp,path)

    def cancel_pair(self,request):
        pair_id=str((request or {}).get("pair_id") or "")
        node_id=str((request or {}).get("node_id") or "")
        reason=str((request or {}).get("reason") or "")
        with self.lock:
            item=self.by_pair.get(pair_id)
            if not isinstance(item,dict):
                return {"ok":False,"error":"pair_not_found"}
            original=item.get("request") or {}
            if str(original.get("node_id") or "")!=node_id:
                return {"ok":False,"error":"node_mismatch"}
            if time.time()>=item["expires"] and item.get("state") in {"pending","approved","receipt_issued"}:
                item["state"]="expired"
            state=str(item.get("state") or "")
            if state in {"cancelled","expired","denied"}:
                return {"ok":True,"state":state,"pair_id":pair_id,"idempotent":True}
            if state=="paired":
                return {"ok":False,"error":"pair_already_confirmed","state":state}
            if state not in {"pending","approved","receipt_issued"}:
                return {"ok":False,"error":"pair_not_cancellable","state":state}
            item["state"]="cancelled"
            item["cancel_reason"]=reason
            item["cancelled_at"]=time.time()
            code=str(item.get("user_code") or "")
            if code:
                self.by_user.pop(code,None)
            return {"ok":True,"state":"cancelled","pair_id":pair_id,"idempotent":False}

    def confirm_pair(self,request):
        pair_id=str((request or {}).get("pair_id") or "")
        node_id=str((request or {}).get("node_id") or "")
        with self.lock:
            item=self.by_pair.get(pair_id)
            if not isinstance(item,dict):
                return {"ok":False,"error":"pair_not_found"}
            original=item.get("request") or {}
            if str(original.get("node_id") or "")!=node_id:
                return {"ok":False,"error":"node_mismatch"}
            if time.time()>=item["expires"] and item.get("state") in {"pending","approved","receipt_issued"}:
                item["state"]="expired"
            state=str(item.get("state") or "")
            if state=="paired" and item.get("confirmed") is True:
                return {"ok":True,"state":"paired","pair_id":pair_id,"idempotent":True}
            if state in {"cancelled","denied","expired"}:
                return {"ok":False,"error":"pair_not_confirmable","state":state}
            if state!="receipt_issued" or not isinstance(item.get("receipt"),dict):
                return {"ok":False,"error":"receipt_not_issued","state":state}
            receipt=dict(item["receipt"])
            self._persist_discovery(item,receipt)
            item["state"]="paired"
            item["confirmed"]=True
            item["confirmed_at"]=time.time()
            return {"ok":True,"state":"paired","pair_id":pair_id,"idempotent":False}

    def create(self,request,public_base):
        with self.lock:
            pair_id=secrets.token_urlsafe(18)
            device_code=secrets.token_urlsafe(32)
            code=user_code()
            now=time.time()
            item={
                "pair_id":pair_id,
                "device_code":device_code,
                "user_code":code,
                "created":now,
                "expires":now+300,
                "state":"pending",
                "request":request,
            }
            self.by_pair[pair_id]=item
            self.by_user[code]=pair_id
            return {
                "pair_id":pair_id,
                "device_code":device_code,
                "user_code":code,
                "verification_uri":public_base.rstrip("/")+"/device",
                "verification_uri_complete":public_base.rstrip("/")+"/device?user_code="+urllib.parse.quote(code),
                "expires_in":300,
                "interval":2,
            }

    def approve(self,code):
        with self.lock:
            pid=self.by_user.get(code)
            if not pid:
                return False
            item=self.by_pair[pid]
            if time.time()>=item["expires"]:
                if item.get("state") in {"pending","approved","receipt_issued"}:
                    item["state"]="expired"
                return False
            if item.get("state")!="pending":
                return False
            item["state"]="approved"
            return True

    def approve_authenticated(self,code,binding):
        with self.lock:
            pid=self.by_user.get(code)
            if not pid:
                return False
            item=self.by_pair[pid]
            if time.time()>=item["expires"]:
                if item.get("state") in {"pending","approved","receipt_issued"}:
                    item["state"]="expired"
                return False
            if item.get("state")!="pending":
                return False
            if not isinstance(binding,dict):
                return False
            issuer=str(binding.get("issuer") or "")
            account_binding_id=str(binding.get("account_binding_id") or "")
            client_id=str(binding.get("client_id") or "")
            if issuer!="https://auth.openai.com":
                return False
            if not account_binding_id.startswith("fbacct_"):
                return False
            if not client_id.startswith("oaiapp_"):
                return False
            item["authenticated_binding"]={
                "issuer":issuer,
                "account_binding_id":account_binding_id,
                "client_id":client_id,
            }
            item["state"]="approved"
            return True

    def _discovery_records(self):
        if self.discovery_path is None or not self.discovery_path.is_file():
            return []
        try:
            value=json.loads(self.discovery_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        nodes=value.get("nodes") if isinstance(value,dict) else None
        return nodes if isinstance(nodes,list) else []

    def _discovery_node(self,node_id):
        node_id=str(node_id)
        for n in self._discovery_records():
            if isinstance(n,dict) and n.get("source")=="fbpair" and n.get("node_id")==node_id:
                return n
        return None

    def _paired_node(self,node_id):
        node_id=str(node_id)
        for n in self._discovery_records():
            if (
                isinstance(n,dict)
                and n.get("state")=="paired"
                and n.get("source")=="fbpair"
                and n.get("node_id")==node_id
            ):
                return n
        return None

    def queue_read(self,node_id,capability,args):
        node=self._paired_node(node_id)
        if node is None:
            return None,"node_not_paired"
        capability=str(capability)
        if capability not in READ_CAPS:
            return None,"capability_not_allowed"
        if not isinstance(args,dict):
            return None,"args_not_object"
        request_id="fbreq_"+secrets.token_urlsafe(18)
        item={
            "schema":1,
            "request_id":request_id,
            "node_id":str(node_id),
            "capability":capability,
            "args":args,
            "created_at":time.time(),
            "expires_at":time.time()+30,
        }
        with self.lock:
            self.control_requests[request_id]=item
            self.control_results[request_id]={"state":"pending","node_id":str(node_id)}
            self.control_queue[str(node_id)].append(request_id)
        return item,None

    def queue_plan(self,node_id,operation,payload):
        node=self._paired_node(str(node_id))
        if node is None:
            return None,"node_not_paired"
        scopes={str(x) for x in (node.get("scopes") or [])}
        if "node.plan" not in scopes:
            return None,"plan_scope_missing"
        operation=str(operation)
        if operation not in {"plan.import","plan.status","plan.apply","plan.rollback"}:
            return None,"operation_not_allowed"
        if not isinstance(payload,dict):
            return None,"payload_not_object"
        request_id="fbreq_"+secrets.token_urlsafe(18)
        item={
            "schema":1,
            "request_id":request_id,
            "node_id":str(node_id),
            "operation":operation,
            "payload":payload,
            "created_at":time.time(),
            "expires_at":time.time()+60,
        }
        with self.lock:
            self.control_requests[request_id]=item
            self.control_results[request_id]={"state":"pending","node_id":str(node_id)}
            self.control_queue[str(node_id)].append(request_id)
        return item,None

    def queue_admin_exec(self,node_id,payload):
        node=self._paired_node(str(node_id))
        if node is None:
            return None,"node_not_paired"
        label=str(node.get("node_label") or "")
        if not (
            label==ADMIN_EXEC_NODE_LABEL
            or (str(node_id)==ADMIN_EXEC_GERAMEG_NODE_ID and label=="GERAMEG")
        ):
            return None,"admin_exec_node_not_allowed"
        if not isinstance(payload,dict):
            return None,"payload_not_object"
        if payload.get("authorized") is not True:
            return None,"authorized_true_required"
        script=payload.get("script")
        if not isinstance(script,str) or not script:
            return None,"script_required"
        raw=script.encode("utf-8")
        if len(raw)>ADMIN_EXEC_MAX_SCRIPT_BYTES:
            return None,"script_too_large"
        job_id=str(payload.get("job_id") or "")
        if ADMIN_EXEC_JOB_RE.fullmatch(job_id) is None:
            return None,"invalid_job_id"
        try:
            timeout_s=int(payload.get("timeout_s"))
        except Exception:
            return None,"invalid_timeout"
        if not 1<=timeout_s<=600:
            return None,"invalid_timeout"
        digest=hashlib.sha256(raw).hexdigest()
        expected=str(payload.get("sha256") or "").lower()
        if expected!=digest:
            return None,"script_sha256_mismatch"
        request_id="fbreq_"+secrets.token_urlsafe(18)
        normalized={
            "authorized":True,
            "job_id":job_id,
            "script":script,
            "sha256":digest,
            "timeout_s":timeout_s,
        }
        item={
            "schema":1,
            "request_id":request_id,
            "node_id":str(node_id),
            "operation":"admin.exec",
            "payload":normalized,
            "created_at":time.time(),
            "expires_at":time.time()+timeout_s+90,
        }
        with self.lock:
            self.control_requests[request_id]=item
            self.control_results[request_id]={"state":"pending","node_id":str(node_id)}
            self.control_queue[str(node_id)].append(request_id)
        return item,None

    def queue_admin_cua(self,node_id,payload):
        node=self._paired_node(str(node_id))
        if node is None:
            return None,"node_not_paired"
        if str(node.get("node_label") or "")!="ASUS":
            return None,"admin_cua_node_not_allowed"
        if not isinstance(payload,dict):
            return None,"payload_not_object"
        if payload.get("authorized") is not True:
            return None,"authorized_true_required"
        cua=payload.get("cua")
        if not isinstance(cua,dict):
            return None,"cua_payload_required"
        action=str(cua.get("action") or "").upper().strip()
        if action not in {"OBSERVE","CLICK","DOUBLE_CLICK","TYPE","KEY","SCROLL","DRAG"}:
            return None,"cua_action_not_allowed"
        job_id=str(payload.get("job_id") or "")
        if ADMIN_EXEC_JOB_RE.fullmatch(job_id) is None:
            return None,"invalid_job_id"
        try:
            timeout_s=int(payload.get("timeout_s"))
        except Exception:
            return None,"invalid_timeout"
        if not 1<=timeout_s<=180:
            return None,"invalid_timeout"
        request_id="fbreq_"+secrets.token_urlsafe(18)
        normalized={
            "authorized":True,
            "job_id":job_id,
            "timeout_s":timeout_s,
            "cua":cua,
        }
        item={
            "schema":1,
            "request_id":request_id,
            "node_id":str(node_id),
            "operation":"admin.cua",
            "payload":normalized,
            "created_at":time.time(),
            "expires_at":time.time()+timeout_s+90,
        }
        with self.lock:
            self.control_requests[request_id]=item
            self.control_results[request_id]={"state":"pending","node_id":str(node_id)}
            self.control_queue[str(node_id)].append(request_id)
        return item,None

    def next_read(self,node_id):
        node_id=str(node_id)
        with self.lock:
            q=self.control_queue[node_id]
            while q:
                rid=q.popleft()
                item=self.control_requests.get(rid)
                if not item:
                    continue
                if time.time()>=float(item.get("expires_at",0)):
                    self.control_results[rid]={"state":"expired","node_id":node_id}
                    continue
                self.control_results[rid]={"state":"dispatched","node_id":node_id}
                return item
        return None

    def put_result(self,node_id,request_id,result):
        node_id=str(node_id);request_id=str(request_id)
        with self.lock:
            req=self.control_requests.get(request_id)
            if not req or req.get("node_id")!=node_id:
                return False
            if not isinstance(result,dict):
                return False
            self.control_results[request_id]={
                "state":"complete",
                "node_id":node_id,
                "result":result,
                "completed_at":time.time(),
            }
            return True

    def get_result(self,request_id):
        with self.lock:
            value=self.control_results.get(str(request_id))
            return dict(value) if isinstance(value,dict) else None

    def verify_node_envelope(self,envelope):
        if not isinstance(envelope,dict):
            return False,"invalid_envelope"
        node_id=str(envelope.get("node_id") or "")
        node=self._paired_node(node_id)
        if node is None:
            known=self._discovery_node(node_id)
            if isinstance(known,dict) and known.get("state")=="revoked":
                return False,"node_revoked"
            return False,"node_not_paired"
        pub=str(node.get("public_key_ssh") or "")
        if not pub.startswith("ssh-ed25519 "):
            return False,"node_public_key_missing"
        if str(envelope.get("node_host_id") or "")!=str(node.get("node_host_id") or ""):
            return False,"host_id_mismatch"
        try:
            ts=float(envelope.get("timestamp"))
        except Exception:
            return False,"timestamp_invalid"
        if abs(time.time()-ts)>60:
            return False,"timestamp_window"
        nonce=str(envelope.get("nonce") or "")
        if len(nonce)<16:
            return False,"nonce_invalid"
        with self.lock:
            used=self.node_nonces[node_id]
            cutoff=time.time()-120
            for k,v in list(used.items()):
                if v<cutoff:
                    used.pop(k,None)
            if nonce in used:
                return False,"replay"
        proof=envelope.get("node_proof") or {}
        if proof.get("format")!="sshsig" or proof.get("namespace")!="framebridge-pairing":
            return False,"proof_format"
        signed=dict(envelope)
        signed.pop("node_proof",None)
        if not verify_pairing_payload(
            pub,
            canonical(signed),
            str(proof.get("signature_b64") or ""),
        ):
            return False,"signature"
        with self.lock:
            now=time.time()
            self.node_nonces[node_id][nonce]=now
            self.node_last_seen[node_id]=now
        return True,node

    def node_runtime_status(self,node_id):
        node=self._paired_node(str(node_id))
        if node is None:
            return None
        with self.lock:
            last=self.node_last_seen.get(str(node_id))
        now=time.time()
        ready=bool(last is not None and now-float(last)<=5.0)
        return {
            "node_id":str(node_id),
            "paired":True,
            "ready":ready,
            "last_seen_age_seconds":None if last is None else round(max(0.0,now-float(last)),3),
            "transport":"broker_outbound",
        }

    def deny(self,code):
        with self.lock:
            pid=self.by_user.get(code)
            if not pid:
                return False
            item=self.by_pair[pid]
            if item.get("state")!="pending":
                return False
            item["state"]="denied"
            item["denied_at"]=time.time()
            return True

    def token(self,pair_id,device_code):
        with self.lock:
            item=self.by_pair.get(pair_id)
            if not item or not secrets.compare_digest(
                str(item.get("device_code") or ""),str(device_code)
            ):
                return {"error":"invalid_grant"}
            if time.time()>=item["expires"] and item.get("state") in {"pending","approved","receipt_issued"}:
                item["state"]="expired"
            state=str(item.get("state") or "")
            if state=="pending":
                return {"error":"authorization_pending"}
            if state in {"denied","cancelled"}:
                return {"error":"access_denied"}
            if state=="expired":
                return {"error":"expired_token"}
            if state in {"receipt_issued","paired"} and isinstance(item.get("receipt"),dict):
                return dict(item["receipt"])
            if state!="approved":
                return {"error":"invalid_grant"}
            req=item["request"]
            binding=item.get("authenticated_binding") or {}
            account_binding_id=str(binding.get("account_binding_id") or "")
            broker_issuer=str(binding.get("issuer") or "https://dev.framebridge.local")
            if not account_binding_id:
                account_binding_id="fbacct_dev_"+secrets.token_urlsafe(16)
            receipt={
                "protocol":FBPAIR_PROTOCOL,
                "schema":1,
                "state":"paired",
                "pair_id":item["pair_id"],
                "request_sha256":req["request_sha256"],
                "node_id":req["node_id"],
                "node_label":req["node_label"],
                "node_host_id":req["node_host_id"],
                "provider":req["provider"],
                "broker_issuer":broker_issuer,
                "account_binding_id":account_binding_id,
                "grant_id":"fbgrant_dev_"+secrets.token_urlsafe(16),
                "paired_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                "scopes":["node.read","node.plan"],
                "revocation_id":"dev-"+secrets.token_urlsafe(12),
                "node_grant_binding":"proof-of-possession-required",
            }
            item["receipt"]=receipt
            item["state"]="receipt_issued"
            item["receipt_issued_at"]=time.time()
            return dict(receipt)


def verify_request(req):
    if not isinstance(req,dict) or req.get("protocol")!=FBPAIR_PROTOCOL:
        return False,"protocol"
    proof=req.get("node_proof") or {}
    pub=str(req.get("public_key_ssh") or "")
    if proof.get("format")!="sshsig" or proof.get("namespace")!="framebridge-pairing":
        return False,"proof-format"
    try:
        parsed=parse_ed25519_public_key_line(pub)
    except Exception:
        return False,"public-key"
    if parsed["host_id"]!=req.get("node_host_id"):
        return False,"host-id"
    if parsed["public_jwk"]!=req.get("public_jwk"):
        return False,"jwk"
    base=dict(req)
    base.pop("node_proof",None)
    base.pop("public_key_ssh",None)
    claimed=base.pop("request_sha256",None)
    if claimed!=sha256_hex(base):
        return False,"request-sha"
    base["request_sha256"]=claimed
    if not verify_pairing_payload(
        pub,
        canonical(base),
        str(proof.get("signature_b64") or ""),
    ):
        return False,"signature"
    return True,"ok"


PAIR_CANCEL_REASONS={
    "network_change","service_stop","reconcile","window_expired",
    "window_closed","local_operator_cancelled",
}


def _verify_lifecycle_request(store,req,operation):
    if not isinstance(req,dict) or req.get("protocol")!=FBPAIR_PROTOCOL or req.get("schema")!=1:
        return False,"protocol"
    if req.get("operation")!=operation:
        return False,"operation"
    pair_id=str(req.get("pair_id") or "")
    node_id=str(req.get("node_id") or "")
    node_host_id=str(req.get("node_host_id") or "")
    proof=req.get("node_proof") or {}
    pub=str(req.get("public_key_ssh") or "")
    if not pair_id or not node_id or not node_host_id:
        return False,"identity"
    if proof.get("format")!="sshsig" or proof.get("namespace")!="framebridge-pairing":
        return False,"proof-format"
    with store.lock:
        item=store.by_pair.get(pair_id)
        if not isinstance(item,dict):
            return False,"pair-not-found"
        original=dict(item.get("request") or {})
    if str(original.get("node_id") or "")!=node_id:
        return False,"node-id"
    if str(original.get("node_host_id") or "")!=node_host_id:
        return False,"host-id"
    if str(original.get("public_key_ssh") or "")!=pub:
        return False,"public-key-binding"
    if operation=="pair.cancel" and str(req.get("reason") or "") not in PAIR_CANCEL_REASONS:
        return False,"reason"
    if operation=="pair.confirm" and str(req.get("request_sha256") or "")!=str(original.get("request_sha256") or ""):
        return False,"request-sha"
    try:
        parsed=parse_ed25519_public_key_line(pub)
    except Exception:
        return False,"public-key"
    if parsed["host_id"]!=node_host_id:
        return False,"host-id"
    base=dict(req)
    base.pop("node_proof",None)
    base.pop("public_key_ssh",None)
    if not verify_pairing_payload(
        pub,canonical(base),str(proof.get("signature_b64") or "")
    ):
        return False,"signature"
    return True,"ok"


def verify_cancel_request(store,req):
    return _verify_lifecycle_request(store,req,"pair.cancel")


def verify_confirm_request(store,req):
    return _verify_lifecycle_request(store,req,"pair.confirm")


DEFAULT_CONTROL_PEERS = frozenset({'127.0.0.1'})


def _control_peer_literal(value):
    if not isinstance(value,str) or not value or value != value.strip() or any(c in value for c in ("/","*","%")):
        raise ValueError("CONTROL_PEER_CONFIG_INVALID")
    try:
        address=ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("CONTROL_PEER_CONFIG_INVALID") from None
    if address.is_unspecified:
        raise ValueError("CONTROL_PEER_CONFIG_INVALID")
    return str(address)


def parse_control_peers(values=None):
    """Validate explicit extra transport peers for the development entrypoint."""
    if values is None:
        return DEFAULT_CONTROL_PEERS
    if not isinstance(values,(list,tuple,set,frozenset)):
        raise ValueError("CONTROL_PEER_CONFIG_INVALID")
    return frozenset(set(DEFAULT_CONTROL_PEERS) | {_control_peer_literal(value) for value in values})


def control_peer_allowed(peer,allowed):
    try:
        return _control_peer_literal(peer) in allowed
    except ValueError:
        return False


def make_handler(store,public_base,account_binding=None,*,control_peers=None):
    allowed_control_peers=parse_control_peers(control_peers)
    class Handler(BaseHTTPRequestHandler):
        server_version="FrameBridgePairDev/1"

        def log_message(self,fmt,*args):
            return

        def send_json(self,status,value):
            data=json.dumps(value,sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_json(self):
            try:
                n=int(self.headers.get("Content-Length","0"))
                if n<0 or n>1024*1024:
                    raise ValueError
                value=json.loads(self.rfile.read(n).decode())
                if not isinstance(value,dict):
                    raise ValueError
                return value
            except Exception:
                return None

        def do_POST(self):
            if self.path=="/fbpair/v1/device_authorization":
                req=self.read_json()
                ok,reason=verify_request(req)
                if not ok:
                    self.send_json(400,{"error":"invalid_node_proof","reason":reason})
                    return
                self.send_json(200,store.create(req,public_base))
                return
            if self.path=="/fbpair/v1/cancel":
                req=self.read_json() or {}
                ok,reason=verify_cancel_request(store,req)
                if not ok:
                    self.send_json(400,{"ok":False,"error":"invalid_node_cancel","reason":reason})
                    return
                value=store.cancel_pair(req)
                self.send_json(200 if value.get("ok") else 409,value)
                return
            if self.path=="/fbpair/v1/confirm":
                req=self.read_json() or {}
                ok,reason=verify_confirm_request(store,req)
                if not ok:
                    self.send_json(400,{"ok":False,"error":"invalid_node_confirm","reason":reason})
                    return
                value=store.confirm_pair(req)
                self.send_json(200 if value.get("ok") else 409,value)
                return
            if self.path=="/fbpair/v1/token":
                req=self.read_json() or {}
                self.send_json(
                    200,
                    store.token(
                        str(req.get("pair_id") or ""),
                        str(req.get("device_code") or ""),
                    ),
                )
                return
            if self.path=="/fbpair/v1/control/node-status":
                peer=str(self.client_address[0] or "")
                if not control_peer_allowed(peer,allowed_control_peers):
                    self.send_json(403,{"ok":False,"error":"local_only"})
                    return
                req=self.read_json() or {}
                value=store.node_runtime_status(str(req.get("node_id") or ""))
                if value is None:
                    self.send_json(404,{"ok":False,"error":"node_not_paired"})
                else:
                    self.send_json(200,{"ok":True,**value})
                return

            if self.path=="/fbpair/v1/control/request":
                peer=str(self.client_address[0] or "")
                if not control_peer_allowed(peer,allowed_control_peers):
                    self.send_json(403,{"ok":False,"error":"local_only"})
                    return
                req=self.read_json() or {}
                item,error=store.queue_read(
                    str(req.get("node_id") or ""),
                    str(req.get("capability") or ""),
                    req.get("args") or {},
                )
                if error:
                    self.send_json(400,{"ok":False,"error":error})
                else:
                    self.send_json(200,{"ok":True,"request_id":item["request_id"]})
                return

            if self.path=="/fbpair/v1/control/admin-exec":
                peer=str(self.client_address[0] or "")
                if not control_peer_allowed(peer,allowed_control_peers):
                    self.send_json(403,{"ok":False,"error":"local_only"})
                    return
                req=self.read_json() or {}
                item,error=store.queue_admin_exec(
                    str(req.get("node_id") or ""),
                    req.get("payload") or {},
                )
                if error:
                    self.send_json(400,{"ok":False,"error":error})
                else:
                    self.send_json(200,{"ok":True,"request_id":item["request_id"]})
                return

            if self.path=="/fbpair/v1/control/admin-cua":
                peer=str(self.client_address[0] or "")
                if not control_peer_allowed(peer,allowed_control_peers):
                    self.send_json(403,{"ok":False,"error":"local_only"})
                    return
                req=self.read_json() or {}
                item,error=store.queue_admin_cua(
                    str(req.get("node_id") or ""),
                    req.get("payload") or {},
                )
                if error:
                    self.send_json(400,{"ok":False,"error":error})
                else:
                    self.send_json(200,{"ok":True,"request_id":item["request_id"]})
                return

            if self.path=="/fbpair/v1/control/operation":
                peer=str(self.client_address[0] or "")
                if not control_peer_allowed(peer,allowed_control_peers):
                    self.send_json(403,{"ok":False,"error":"local_only"})
                    return
                req=self.read_json() or {}
                item,error=store.queue_plan(
                    str(req.get("node_id") or ""),
                    str(req.get("operation") or ""),
                    req.get("payload") or {},
                )
                if error:
                    self.send_json(400,{"ok":False,"error":error})
                else:
                    self.send_json(200,{"ok":True,"request_id":item["request_id"]})
                return

            if self.path=="/fbpair/v1/control/result":
                peer=str(self.client_address[0] or "")
                if not control_peer_allowed(peer,allowed_control_peers):
                    self.send_json(403,{"ok":False,"error":"local_only"})
                    return
                req=self.read_json() or {}
                value=store.get_result(str(req.get("request_id") or ""))
                if value is None:
                    self.send_json(404,{"ok":False,"error":"request_not_found"})
                else:
                    self.send_json(200,{"ok":True,**value})
                return

            if self.path=="/fbpair/v1/node/poll":
                req=self.read_json() or {}
                ok,detail=store.verify_node_envelope(req)
                if not ok:
                    self.send_json(403,{"ok":False,"error":detail})
                    return
                item=store.next_read(req["node_id"])
                self.send_json(200,{"ok":True,"request":item})
                return

            if self.path=="/fbpair/v1/node/result":
                req=self.read_json() or {}
                ok,detail=store.verify_node_envelope(req)
                if not ok:
                    self.send_json(403,{"ok":False,"error":detail})
                    return
                payload=req.get("payload") or {}
                saved=store.put_result(
                    req["node_id"],
                    str(payload.get("request_id") or ""),
                    payload.get("result") or {},
                )
                self.send_json(200 if saved else 400,{"ok":bool(saved)})
                return

            if self.path=="/dev/authenticate-and-approve":
                peer=str(self.client_address[0] or "")
                if not control_peer_allowed(peer,allowed_control_peers):
                    self.send_json(403,{"ok":False,"error":"local_only"})
                    return
                req=self.read_json() or {}
                ok,error=approve_via_chatgpt(
                    store,
                    str(req.get("user_code") or ""),
                    account_binding=account_binding,
                )
                self.send_json(200 if ok else 400,{"ok":ok,"error":error})
                return

            if self.path=="/dev/approve-authenticated":
                peer=str(self.client_address[0] or "")
                if not control_peer_allowed(peer,allowed_control_peers):
                    self.send_json(403,{"ok":False,"error":"local_only"})
                    return
                req=self.read_json() or {}
                binding={
                    "issuer":req.get("issuer"),
                    "account_binding_id":req.get("account_binding_id"),
                    "client_id":req.get("client_id"),
                }
                ok=store.approve_authenticated(
                    str(req.get("user_code") or ""),
                    binding,
                )
                self.send_json(200 if ok else 400,{"ok":ok})
                return

            if self.path=="/dev/approve":
                req=self.read_json() or {}
                ok=store.approve(str(req.get("user_code") or ""))
                self.send_json(200 if ok else 404,{"ok":ok})
                return
            if self.path=="/dev/deny":
                req=self.read_json() or {}
                ok=store.deny(str(req.get("user_code") or ""))
                self.send_json(200 if ok else 404,{"ok":ok})
                return
            self.send_json(404,{"error":"not_found"})

        def do_GET(self):
            p=urllib.parse.urlparse(self.path)
            if p.path=="/health":
                self.send_json(200,{"ok":True,"protocol":FBPAIR_PROTOCOL,"dev_only":True})
                return
            if p.path=="/device":
                code=urllib.parse.parse_qs(p.query).get("user_code",[""])[0]
                pid=store.by_user.get(code)
                item=store.by_pair.get(pid,{}) if pid else {}
                req=item.get("request") or {}
                safe_code=urllib.parse.quote(code,safe="")
                body=(
                    "<!doctype html><meta charset=utf-8>"
                    "<title>FrameBridge DEV Pairing</title>"
                    "<h1>FrameBridge DEV Pairing</h1>"
                    "<p>DEV LAB — real ChatGPT authentication, development control plane.</p>"
                    "<p>Node: <strong>"+str(req.get("node_label") or "")+"</strong></p>"
                    "<p>Provider: <strong>"+str(req.get("provider") or "")+"</strong></p>"
                    "<p>Host ID: <code>"+str(req.get("node_host_id") or "")+"</code></p>"
                    "<p>User code: <strong>"+code+"</strong></p>"
                    "<button id=go>Continue with ChatGPT</button>"
                    "<pre id=result></pre>"
                    "<script>"
                    "document.getElementById('go').onclick=async()=>{"
                    "const b=document.getElementById('go');b.disabled=true;"
                    "document.getElementById('result').textContent='Opening ChatGPT authentication...';"
                    "try{const r=await fetch('/dev/authenticate-and-approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_code:'"+safe_code+"'})});"
                    "const j=await r.json();document.getElementById('result').textContent=j.ok?'Pairing approved. Return to the terminal.':('Error: '+(j.error||'unknown'));"
                    "}catch(e){document.getElementById('result').textContent='Error: '+e;}finally{b.disabled=false;}};"
                    "</script>"
                    "<p>No ChatGPT password is collected by FrameBridge.</p>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_json(404,{"error":"not_found"})
    return Handler


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--bind",default="127.0.0.1")
    ap.add_argument("--port",type=int,default=18765)
    ap.add_argument("--public-base",default="http://127.0.0.1:18765")
    ap.add_argument("--discovery-file",default="")
    ap.add_argument("--account-binding-file",default="")
    ap.add_argument("--control-peer",action="append",metavar="IP",help="Allow an additional literal transport peer for development control endpoints")
    args=ap.parse_args()
    try:
        control_peers=parse_control_peers(args.control_peer)
    except ValueError as exc:
        ap.error(str(exc))
    account_binding=None
    if args.account_binding_file:
        p=Path(args.account_binding_file).expanduser()
        try:
            value=json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            raise SystemExit("ACCOUNT_BINDING_FILE_INVALID")
        required=("issuer","account_binding_id","client_id")
        if not isinstance(value,dict) or any(not isinstance(value.get(k),str) or not value.get(k) for k in required):
            raise SystemExit("ACCOUNT_BINDING_FILE_INVALID")
        account_binding={k:value[k] for k in required}
    store=Store(
        Path(args.discovery_file) if args.discovery_file else None,
        public_base=args.public_base,
    )
    server=ThreadingHTTPServer(
        (args.bind,args.port),
        make_handler(store,args.public_base,account_binding=account_binding,control_peers=control_peers),
    )
    print(json.dumps({
        "ok":True,
        "dev_only":True,
        "bind":args.bind,
        "port":args.port,
        "public_base":args.public_base,
        "protocol":FBPAIR_PROTOCOL,
    }),flush=True)
    server.serve_forever()


if __name__=="__main__":
    main()
