from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

AUTHORIZATION_ENDPOINT="https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT="https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_ENDPOINT="https://auth.openai.com/.well-known/openid-configuration"
RESOURCE="https://api.openai.com/v1"
DYNAMIC_CLIENT_ID="dynamic_agent_client"
DEFAULT_SCOPES=("openid","profile","email","offline_access","resource.invoke","chatgpt.tokens.use.direct")
AGENT_NAME="FrameBridge"


class ChatGPTAuthError(RuntimeError):
    pass


def discover_openai(*, requests_module=None) -> dict[str,str]:
    if requests_module is None:
        import requests as requests_module
    response=requests_module.get(DISCOVERY_ENDPOINT,timeout=15)
    if response.status_code!=200:
        raise ChatGPTAuthError("CHATGPT_DISCOVERY_FAILED")
    value=response.json()
    if not isinstance(value,dict):
        raise ChatGPTAuthError("CHATGPT_DISCOVERY_INVALID")
    issuer=str(value.get("issuer") or "")
    auth=str(value.get("authorization_endpoint") or "")
    token=str(value.get("token_endpoint") or "")
    jwks=str(value.get("jwks_uri") or "")
    if issuer!="https://auth.openai.com":
        raise ChatGPTAuthError("CHATGPT_DISCOVERY_INVALID")
    for url in (auth,token,jwks):
        if not url.startswith("https://auth.openai.com/"):
            raise ChatGPTAuthError("CHATGPT_DISCOVERY_INVALID")
    return {
        "issuer":issuer,
        "authorization_endpoint":auth,
        "token_endpoint":token,
        "jwks_uri":jwks,
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _random(n: int=48) -> str:
    return secrets.token_urlsafe(n)


def account_binding_id(issuer: str, subject: str) -> str:
    raw=(str(issuer)+"\0"+str(subject)).encode("utf-8")
    return "fbacct_"+hashlib.sha256(raw).hexdigest()[:40]


def build_authorization(
    host_id: str,
    redirect_uri: str,
    *,
    scopes: tuple[str,...]=DEFAULT_SCOPES,
    authorization_endpoint: str=AUTHORIZATION_ENDPOINT,
    client_id: str=DYNAMIC_CLIENT_ID,
) -> tuple[str,dict[str,str]]:
    host_id=str(host_id).strip()
    if not host_id:
        raise ChatGPTAuthError("CHATGPT_HOST_ID_REQUIRED")
    state=_random(32)
    nonce=_random(32)
    verifier=_random(64)
    challenge=_b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    client_id=str(client_id or DYNAMIC_CLIENT_ID)
    tx={
        "state":state,
        "nonce":nonce,
        "code_verifier":verifier,
        "redirect_uri":str(redirect_uri),
        "created_at":str(time.time()),
        "client_id":client_id,
    }
    query={
        "client_id":client_id,
        "ext_agent_host_id":host_id,
        "response_type":"code",
        "redirect_uri":str(redirect_uri),
        "scope":" ".join(scopes),
        "resource":RESOURCE,
        "state":state,
        "nonce":nonce,
        "code_challenge_method":"S256",
        "code_challenge":challenge,
    }
    if client_id==DYNAMIC_CLIENT_ID:
        query["agent_name_hint"]=AGENT_NAME
    return str(authorization_endpoint)+"?"+urllib.parse.urlencode(query),tx


class _Callback:
    def __init__(self):
        self.event=threading.Event()
        self.query: dict[str,str]={}


def _handler(callback: _Callback):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args):
            return
        def do_GET(self):
            p=urllib.parse.urlparse(self.path)
            if p.path!="/auth/callback":
                self.send_response(404);self.end_headers();return
            q=urllib.parse.parse_qs(p.query,keep_blank_values=True)
            callback.query={k:(v[0] if v else "") for k,v in q.items()}
            callback.event.set()
            body=(
                b"<!doctype html><meta charset=utf-8>"
                b"<title>FrameBridge</title>"
                b"<h2>FrameBridge</h2>"
                b"<p>ChatGPT authentication returned to FrameBridge.</p>"
                b"<p>You may close this browser tab.</p>"
            )
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def start_loopback_listener() -> tuple[HTTPServer,_Callback,str]:
    cb=_Callback()
    server=HTTPServer(("127.0.0.1",0),_handler(cb))
    port=int(server.server_address[1])
    uri=f"http://127.0.0.1:{port}/auth/callback"
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    return server,cb,uri


def exchange_and_validate(
    callback_query: dict[str,str],
    tx: dict[str,str],
    *,
    requests_module=None,
    jwt_module=None,
    provider_metadata: dict[str,str] | None=None,
) -> dict[str,Any]:
    if callback_query.get("state") != tx.get("state"):
        raise ChatGPTAuthError("CHATGPT_STATE_MISMATCH")
    if callback_query.get("error"):
        raise ChatGPTAuthError("CHATGPT_AUTH_DENIED")
    code=str(callback_query.get("code") or "")
    requested_client_id=str(tx.get("client_id") or DYNAMIC_CLIENT_ID)
    callback_client_id=str(callback_query.get("client_id") or "")
    if requested_client_id==DYNAMIC_CLIENT_ID:
        issued_client_id=callback_client_id
        if not code or not issued_client_id or issued_client_id==DYNAMIC_CLIENT_ID:
            raise ChatGPTAuthError("CHATGPT_CALLBACK_INCOMPLETE")
    else:
        issued_client_id=requested_client_id
        if callback_client_id and callback_client_id!=requested_client_id:
            raise ChatGPTAuthError("CHATGPT_AUTH_CLIENT_MISMATCH")
        if not code:
            raise ChatGPTAuthError("CHATGPT_CALLBACK_INCOMPLETE")

    if requests_module is None:
        import requests as requests_module
    if jwt_module is None:
        import jwt as jwt_module

    provider_metadata=provider_metadata or discover_openai(requests_module=requests_module)
    token_endpoint=provider_metadata["token_endpoint"]
    issuer=provider_metadata["issuer"]
    jwks_uri=provider_metadata["jwks_uri"]

    token_resp=requests_module.post(
        token_endpoint,
        data={
            "grant_type":"authorization_code",
            "client_id":issued_client_id,
            "code":code,
            "code_verifier":tx["code_verifier"],
            "redirect_uri":tx["redirect_uri"],
            "resource":RESOURCE,
        },
        timeout=20,
    )
    if token_resp.status_code != 200:
        raise ChatGPTAuthError("CHATGPT_TOKEN_EXCHANGE_FAILED")
    tokens=token_resp.json()
    id_token=str(tokens.get("id_token") or "")
    if not id_token:
        raise ChatGPTAuthError("CHATGPT_ID_TOKEN_MISSING")

    jwk_client=jwt_module.PyJWKClient(jwks_uri)
    signing_key=jwk_client.get_signing_key_from_jwt(id_token)
    alg=str(getattr(signing_key,"algorithm_name","") or "")
    if not alg:
        try:
            alg=str(jwt_module.get_unverified_header(id_token).get("alg") or "")
        except Exception:
            alg=""
    if alg not in {"RS256","ES256","EdDSA"}:
        raise ChatGPTAuthError("CHATGPT_ID_TOKEN_ALG_INVALID")

    claims=jwt_module.decode(
        id_token,
        signing_key.key,
        algorithms=[alg],
        audience=issued_client_id,
        issuer=issuer,
        options={"require":["sub","exp","iat"]},
        leeway=5,
    )
    if claims.get("nonce") != tx.get("nonce"):
        raise ChatGPTAuthError("CHATGPT_NONCE_MISMATCH")
    subject=str(claims.get("sub") or "")
    if not subject:
        raise ChatGPTAuthError("CHATGPT_SUBJECT_MISSING")

    granted=str(tokens.get("scope") or callback_query.get("scope") or "")
    scopes=sorted({x for x in granted.split() if x})
    email=str(claims.get("email") or "")

    # No OAuth access/refresh/ID token is persisted or returned.
    return {
        "ok":True,
        "issuer":issuer,
        "client_id":issued_client_id,
        "account_binding_id":account_binding_id(issuer,subject),
        "subject_sha256":hashlib.sha256(subject.encode("utf-8")).hexdigest(),
        "email_sha256":hashlib.sha256(email.encode("utf-8")).hexdigest() if email else None,
        "scopes":scopes,
        "identity_only": "chatgpt.tokens.use.direct" not in scopes,
    }


def authenticate_interactive(
    host_id: str,
    *,
    browser_opener=None,
    timeout_seconds: int=300,
    client_id: str=DYNAMIC_CLIENT_ID,
) -> dict[str,Any]:
    server,cb,redirect_uri=start_loopback_listener()
    try:
        provider=discover_openai()
        url,tx=build_authorization(
            host_id,
            redirect_uri,
            authorization_endpoint=provider["authorization_endpoint"],
            client_id=client_id,
        )
        if browser_opener is None:
            import webbrowser
            opened=webbrowser.open(url,new=1,autoraise=True)
        else:
            opened=bool(browser_opener(url))
        if not opened:
            raise ChatGPTAuthError("CHATGPT_BROWSER_OPEN_FAILED")
        if not cb.event.wait(timeout_seconds):
            raise ChatGPTAuthError("CHATGPT_AUTH_TIMEOUT")
        return exchange_and_validate(cb.query,tx,provider_metadata=provider)
    finally:
        server.shutdown()
        server.server_close()
