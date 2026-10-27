from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import struct
import subprocess
from pathlib import Path
from typing import Any, Callable

NODE_KEY_NAMESPACE = "framebridge-pairing"
NODE_KEY_ALGORITHM = "ssh-ed25519"


class NodeIdentityError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _ssh_string(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID")
    size = struct.unpack(">I", blob[offset:offset + 4])[0]
    offset += 4
    end = offset + size
    if end > len(blob):
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID")
    return blob[offset:end], end


def read_ed25519_public_key(public_path: Path) -> dict[str, Any]:
    path = Path(public_path)
    if not path.is_file() or path.is_symlink():
        raise NodeIdentityError("NODE_PUBLIC_KEY_MISSING")
    parts = path.read_text(encoding="utf-8").strip().split()
    if len(parts) < 2 or parts[0] != NODE_KEY_ALGORITHM:
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except Exception as exc:
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID") from exc
    alg, off = _ssh_string(blob, 0)
    raw, off = _ssh_string(blob, off)
    if off != len(blob) or alg != b"ssh-ed25519" or len(raw) != 32:
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID")
    x = _b64url(raw)
    jwk = {"crv": "Ed25519", "kty": "OKP", "x": x}
    canonical = json.dumps(
        jwk, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    thumbprint = _b64url(hashlib.sha256(canonical).digest())
    host_id = (
        "urn:ietf:params:oauth:jwk-thumbprint:sha-256:" + thumbprint
    )
    return {
        "algorithm": "Ed25519",
        "public_jwk": jwk,
        "jwk_thumbprint_sha256": thumbprint,
        "host_id": host_id,
        "public_key_sha256": hashlib.sha256(blob).hexdigest(),
    }


def ensure_node_keypair(
    state_dir: Path,
    *,
    comment: str = "framebridge-node",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    root = Path(state_dir) / "node-identity"
    private_path = root / "node_ed25519"
    public_path = root / "node_ed25519.pub"

    if root.exists() and root.is_symlink():
        raise NodeIdentityError("NODE_KEY_DIR_SYMLINK_FORBIDDEN")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)

    private_exists = private_path.exists()
    public_exists = public_path.exists()
    if private_exists != public_exists:
        raise NodeIdentityError("NODE_KEYPAIR_INCOMPLETE")

    if not private_exists:
        proc = runner(
            [
                "/usr/bin/ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                str(comment),
                "-f",
                str(private_path),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            raise NodeIdentityError("NODE_KEYGEN_FAILED")

    if (
        not private_path.is_file()
        or private_path.is_symlink()
        or not public_path.is_file()
        or public_path.is_symlink()
    ):
        raise NodeIdentityError("NODE_KEYPAIR_INVALID")

    os.chmod(private_path, 0o600)
    os.chmod(public_path, 0o644)
    mode = stat.S_IMODE(private_path.stat().st_mode)
    if mode != 0o600:
        raise NodeIdentityError("NODE_PRIVATE_KEY_MODE_INVALID")

    public = read_ed25519_public_key(public_path)
    return {
        "ok": True,
        "private_key_path": str(private_path),
        "public_key_path": str(public_path),
        "public_key_ssh": _safe_public_line(public_path),
        **public,
    }


def _safe_public_line(public_path: Path) -> str:
    parts=Path(public_path).read_text(encoding="utf-8").strip().split()
    if len(parts)<2 or parts[0] != NODE_KEY_ALGORITHM:
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID")
    return parts[0] + " " + parts[1]


def sign_pairing_payload(
    private_path: Path,
    payload: bytes,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    private_path=Path(private_path)
    if not private_path.is_file() or private_path.is_symlink():
        raise NodeIdentityError("NODE_PRIVATE_KEY_MISSING")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="framebridge-pair-sign-") as td:
        msg=Path(td)/"message"
        msg.write_bytes(bytes(payload))
        proc=runner(
            [
                "/usr/bin/ssh-keygen","-Y","sign",
                "-f",str(private_path),
                "-n",NODE_KEY_NAMESPACE,
                str(msg),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        sig=Path(str(msg)+".sig")
        if proc.returncode!=0 or not sig.is_file():
            raise NodeIdentityError("NODE_PAIR_SIGNATURE_FAILED")
        return base64.b64encode(sig.read_bytes()).decode("ascii")


def verify_pairing_payload(
    public_key_ssh: str,
    payload: bytes,
    signature_b64: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    import tempfile
    try:
        signature=base64.b64decode(str(signature_b64),validate=True)
    except Exception:
        return False
    public_key_ssh=str(public_key_ssh).strip()
    if not public_key_ssh.startswith("ssh-ed25519 "):
        return False
    with tempfile.TemporaryDirectory(prefix="framebridge-pair-verify-") as td:
        root=Path(td)
        msg=root/"message"
        sig=root/"message.sig"
        allowed=root/"allowed_signers"
        msg.write_bytes(bytes(payload))
        sig.write_bytes(signature)
        allowed.write_text(
            "framebridge-node " + public_key_ssh + "\n",
            encoding="utf-8",
        )
        proc=runner(
            [
                "/usr/bin/ssh-keygen","-Y","verify",
                "-f",str(allowed),
                "-I","framebridge-node",
                "-n",NODE_KEY_NAMESPACE,
                "-s",str(sig),
            ],
            input=bytes(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return proc.returncode==0


def parse_ed25519_public_key_line(line: str) -> dict[str, Any]:
    parts=str(line).strip().split()
    if len(parts)<2 or parts[0] != NODE_KEY_ALGORITHM:
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID")
    try:
        blob=base64.b64decode(parts[1],validate=True)
    except Exception as exc:
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID") from exc
    alg,off=_ssh_string(blob,0)
    raw,off=_ssh_string(blob,off)
    if off!=len(blob) or alg!=b"ssh-ed25519" or len(raw)!=32:
        raise NodeIdentityError("NODE_PUBLIC_KEY_INVALID")
    x=_b64url(raw)
    jwk={"crv":"Ed25519","kty":"OKP","x":x}
    canonical=json.dumps(
        jwk,sort_keys=True,separators=(",",":"),ensure_ascii=True
    ).encode("ascii")
    thumbprint=_b64url(hashlib.sha256(canonical).digest())
    return {
        "algorithm":"Ed25519",
        "public_jwk":jwk,
        "jwk_thumbprint_sha256":thumbprint,
        "host_id":"urn:ietf:params:oauth:jwk-thumbprint:sha-256:"+thumbprint,
        "public_key_sha256":hashlib.sha256(blob).hexdigest(),
    }
