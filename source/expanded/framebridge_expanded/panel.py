"""Run the role-gated local panel as the FrameBridge profile owner."""
from __future__ import annotations

import base64
from datetime import UTC, datetime
import getpass
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from urllib.parse import urlsplit

from . import __version__, broker, installation
from .config import load_config, private_dir

PORT = 29189
HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")


def _offline_hosts(state):
    path = state / "panel-inventory.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError("The panel inventory must be a private regular file owned by this account.")
        value = json.loads(stream.read(16385))
    if not isinstance(value, list) or len(value) > 100 or any(
            not isinstance(name, str) or not HOSTNAME.fullmatch(name) for name in value):
        raise ValueError("The panel inventory must contain a JSON list of computer names.")
    return value


def inventory(state, config):
    name = config["name"]
    hosts = {name.casefold(): {"id": name.casefold(), "name": name, "role": "central", "ready": False}}
    port = config["ports"]["broker"]
    scheme = "https" if config.get("tls_cert") and config.get("tls_key") else "http"
    local_broker = config.get("broker_control_url") or f"{scheme}://127.0.0.1:{port}"
    if urlsplit(local_broker).hostname in {"127.0.0.1", "localhost", "::1"}:
        try:
            broker.configure(broker_url=local_broker, state_dir=state)
            for node in broker.list_confirmed_nodes():
                alias = node["alias"]
                if HOSTNAME.fullmatch(alias) and alias.casefold() not in hosts:
                    hosts[alias.casefold()] = {"id": alias.casefold(), "name": alias, "role": "node",
                                               "ready": node["ready"] is True}
        except (broker.BrokerError, OSError, ValueError):
            pass
    for alias in _offline_hosts(state):
        hosts.setdefault(alias.casefold(), {"id": alias.casefold(), "name": alias,
                                            "role": "unverified", "ready": False})
    return {"at": datetime.now(UTC).isoformat(timespec="milliseconds"), "version": __version__,
            "hosts": list(hosts.values())}


def serve(state):
    config = load_config(state)
    if os.getuid() == 0 or config["role"] != "central":
        raise ValueError("Run fb panel as the owner of a central profile.")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("The panel requires a local terminal for its private session.")
    root = installation.panel_path()
    installation._checked(root, directory=True)
    spec = importlib.util.spec_from_file_location("framebridge_panel_server", root / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.path.insert(0, str(root / "vendor/waitress-3.0.2-py3-none-any.whl"))
    from waitress import create_server

    snapshot = inventory(state, config)
    password = getpass.getpass("Clave temporal del panel (mínimo 16 caracteres): ")
    if len(password) < 16 or len(password) > 256:
        raise ValueError("Use a temporary passphrase of 16–256 characters.")
    credential = base64.b64encode(("framebridge:" + password).encode("utf-8"))
    digest = hashlib.sha256(b"Basic " + credential).digest()
    del password, credential
    origin = f"http://127.0.0.1:{PORT}"
    state = private_dir(state)
    db = state / "panel-bitacora.sqlite3"
    if db.is_symlink():
        raise ValueError("The panel journal must not be a symbolic link.")
    previous_mask = os.umask(0o077)
    try:
        app = module.make_app(db, digest, origin, snapshot)
        server = create_server(app, host="127.0.0.1", port=PORT, threads=4,
                               channel_timeout=30, max_request_body_size=65536,
                               max_request_header_size=16384, connection_limit=40, ident="FrameBridge")
        print(f"Panel privado: {origin}/  ·  Usuario: framebridge  ·  Ctrl+C para cerrar.", flush=True)
        try:
            server.run()
        finally:
            server.close()
    finally:
        os.umask(previous_mask)
    return 0
