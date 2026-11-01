"""Validated, private profiles for FrameBridge Expanded."""
from __future__ import annotations
import json
import os
import re
import socket
import stat
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
ROLES = ("standalone", "central", "node")
DEFAULT_STATE = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "framebridge-expanded"

def state_path(state=None):
    value = state or os.environ.get("FRAMEBRIDGE_EXPANDED_STATE") or os.environ.get("FRAMEBRIDGE_STATE_DIR")
    if not value:
        from .profiles import selected
        value = selected() or DEFAULT_STATE
    return Path(value).expanduser().absolute()

def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Profile directories must not be symbolic links.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.stat()
    if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
        raise ValueError("Profile directories must belong to the current user and have mode 0700.")
    return path

def default_config(role="standalone", name=None):
    if role not in ROLES:
        raise ValueError("Choose standalone, central, or node.")
    name = name or re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower()).strip("-") or "computer"
    return {
        "schema_version": SCHEMA_VERSION, "role": role, "name": name,
        "node_id": str(uuid.uuid4()),
        "ports": {"mcp": 20865, "hub": 20877, "broker": 20878},
        "headless": "auto", "broker_listen_host": "127.0.0.1",
        "broker_url": "", "tls_ca": "", "tls_cert": "", "tls_key": "",
        "account_label": "", "tunnel_id": ""
    }

def validate(config):
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported profile format.")
    if config.get("role") not in ROLES:
        raise ValueError("Choose standalone, central, or node.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", config.get("name", "")):
        raise ValueError("The node name must use 1-63 letters, numbers, hyphens, or underscores.")
    uuid.UUID(config["node_id"])
    ports = config.get("ports", {})
    if set(ports) != {"mcp", "hub", "broker"} or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports.values()):
        raise ValueError("Ports must be distinct integers between 1024 and 65535.")
    if len(set(ports.values())) != len(ports):
        raise ValueError("Choose a different port for each service.")
    if config.get("headless", "auto") not in ("auto", "yes", "no", True, False):
        raise ValueError("Headless mode must be auto, yes, or no.")
    for key in ("api_key", "runtime_key", "password", "token", "secret"):
        if key in config:
            raise ValueError("Runtime credentials must not be stored in profile configuration.")
    broker_url = config.get("broker_url", "")
    if broker_url:
        url = urlsplit(broker_url)
        if url.username or url.password or url.query or url.fragment or url.scheme not in ("https", "http") or not url.hostname:
            raise ValueError("Use a broker URL without credentials, query parameters, or fragments.")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("A remote broker requires verified HTTPS.")
    return config

def load_config(state=None):
    state = state_path(state)
    path = state / "config.json"
    if not path.is_file():
        raise ValueError("This profile is not configured. Run framebridge setup first.")
    if path.is_symlink():
        raise ValueError("Profile configuration must not be a symbolic link.")
    st = path.stat()
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError("Profile configuration must belong to the current user and have mode 0600.")
    return validate(json.loads(path.read_text(encoding="utf-8")))

def save_config(state, config):
    validate(config)
    state = private_dir(state_path(state))
    target = state / "config.json"
    if target.is_symlink():
        raise ValueError("Profile configuration must not be a symbolic link.")
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=state)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return config
