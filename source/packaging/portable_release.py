"""Bind the reviewed private runtime, source asset and stable archive key."""
from pathlib import Path
import hashlib
import json
import stat

import bundled_runtime
import apt_bootstrap

PROFILE = bundled_runtime.PROFILE
SPEC = "portable-release-inputs.json"
KEY_NAME = "portable-archive-keyring.gpg"
SOURCE_NAME = "FrameBridge-CPython-3.13.18-20261110-runtime-sources.tar"

def identify(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise RuntimeError("Portable release input must be a regular file")
    with path.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    return {"filename": path.name, "bytes": path.stat().st_size, "sha256": digest}

def load_inputs(packaging, source_bundle, *, archives=None, key=None):
    packaging = Path(packaging)
    spec = json.loads((packaging/SPEC).read_text())
    if set(spec) != {"schema","profile","python_version","upstream_tag","upstream_commit","runtime_inputs","runtime_sources","archive_key"}:
        raise RuntimeError("Portable release input schema mismatch")
    if (spec["schema"],spec["profile"],spec["python_version"],spec["upstream_tag"],spec["upstream_commit"]) != (
            1,PROFILE,bundled_runtime.VERSION,"20261110","9244801782b78e4c4afc5c82a4524aa2101d3298"):
        raise RuntimeError("Portable release runtime identity mismatch")
    if spec["runtime_inputs"] != {"install_only_sha256":bundled_runtime.INSTALL_SHA,"full_sha256":bundled_runtime.FULL_SHA}:
        raise RuntimeError("Portable release binary input mismatch")
    if spec["runtime_sources"].get("filename")!=SOURCE_NAME or identify(source_bundle)!=spec["runtime_sources"]:
        raise RuntimeError("Portable runtime source bundle hash or identity mismatch")
    pinned=packaging/KEY_NAME
    expected={"fingerprint":apt_bootstrap.STABLE_FPR,**identify(pinned)}
    if spec["archive_key"]!=expected:
        raise RuntimeError("Portable archive key identity mismatch")
    apt_bootstrap.validate_public_key(pinned.read_bytes(),apt_bootstrap.STABLE_FPR)
    if key is not None and Path(key).read_bytes()!=pinned.read_bytes():
        raise RuntimeError("Portable build archive key differs from corresponding source")
    if archives is not None:
        bundled_runtime.verify(Path(archives[0]),bundled_runtime.INSTALL_SHA)
        bundled_runtime.verify(Path(archives[1]),bundled_runtime.FULL_SHA)
    return spec
