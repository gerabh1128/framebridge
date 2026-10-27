"""Read-only network diagnostics, independent of a desktop applet's manager view."""
from __future__ import annotations
import json
import os
import re
import stat
from pathlib import Path
import shutil
import subprocess

_IFACE = re.compile(r"[A-Za-z0-9_.:@-]{1,100}\Z")

def _command(argv, *, timeout=4):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "LC_ALL": "C", "LANG": "C"})
        return result.returncode, result.stdout
    except (OSError, subprocess.SubprocessError):
        return None, ""

def _json(argv):
    code, text = _command(argv)
    if code != 0:
        return None
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if type(value) is list else None

def _active(service):
    code, text = _command(["systemctl", "is-active", service])
    return code == 0 and text.strip() == "active"

def _available(name):
    return bool(shutil.which(name) or any(
        (Path(directory) / name).is_file() and os.access(Path(directory) / name, os.X_OK)
        for directory in ["/usr/sbin", "/sbin", "/usr/bin", "/bin"]))

def _ifupdown(state_file=Path("/run/network/ifstate")):
    """Read active interface names from bounded state, never credential/config files."""
    try:
        fd = os.open(state_file, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
                    or info.st_mode & 0o022 or info.st_size > 65536):
                return set()
            raw = os.read(fd, 65537)
        finally:
            os.close(fd)
        if len(raw) > 65536:
            return set()
        text = raw.decode("ascii")
    except (OSError, UnicodeError):
        return set()
    names = set()
    for line in text.splitlines():
        if "=" not in line:
            continue
        name, mapped = line.split("=", 1)
        if _IFACE.fullmatch(name) and _IFACE.fullmatch(mapped):
            names.add(name)
    return names

def _nm_devices():
    """Only device metadata, never connection profiles, SSIDs or saved secrets."""
    code, text = _command([
        "nmcli", "-t", "-f", "GENERAL.DEVICE,GENERAL.STATE,GENERAL.NM-MANAGED", "device", "show",
    ])
    if code != 0:
        return None
    devices = {}
    current = None
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        # nmcli escapes colons/backslashes in terse output. Linux interface
        # names containing either are excluded rather than guessed.
        if key == "GENERAL.DEVICE":
            current = value if _IFACE.fullmatch(value) and "\\" not in value else None
            if current:
                devices[current] = {"managed": None, "state": ""}
        elif current and key == "GENERAL.STATE":
            devices[current]["state"] = value[:120]
        elif current and key == "GENERAL.NM-MANAGED":
            devices[current]["managed"] = {"yes": True, "no": False}.get(value.strip().lower())
    return devices

def summarize(addresses, routes, nm_devices=None, ifupdown=None, managers=None):
    """Separate observed kernel connectivity from network-manager ownership."""
    nm_devices = {} if nm_devices is None else nm_devices
    ifupdown = set() if ifupdown is None else set(ifupdown)
    managers = {} if managers is None else managers
    defaults = []
    for family, rows in routes.items():
        for row in rows or []:
            name = row.get("dev")
            if row.get("dst") != "default" or not isinstance(name, str) or _IFACE.fullmatch(name) is None:
                continue
            defaults.append({"family": family, "interface": name,
                             "gateway": row.get("gateway"), "metric": row.get("metric")})
    interfaces = []
    for row in addresses or []:
        name = row.get("ifname")
        if not isinstance(name, str) or _IFACE.fullmatch(name) is None:
            continue
        flags = row.get("flags") if isinstance(row.get("flags"), list) else []
        addresses_public = []
        for address in row.get("addr_info", []):
            if (isinstance(address, dict) and address.get("family") in {"inet", "inet6"}
                    and isinstance(address.get("local"), str)):
                addresses_public.append({"family": address["family"], "address": address["local"],
                                         "prefixlen": address.get("prefixlen"),
                                         "scope": address.get("scope")})
        nm = nm_devices.get(name, {})
        managed = nm.get("managed")
        if managed is True:
            owner = "NetworkManager"
        elif name in ifupdown:
            owner = "ifupdown"
        else:
            owner = "unverified"
        default = any(route["interface"] == name for route in defaults)
        link = name != "lo" and "UP" in flags and row.get("operstate") in {"UP", "UNKNOWN"}
        usable_address = any(a["scope"] == "global" for a in addresses_public)
        interfaces.append({
            "name": name, "link_up": link, "operstate": row.get("operstate"),
            "addresses": addresses_public, "has_default_route": default,
            "manager": owner, "networkmanager_managed": managed,
            "networkmanager_state": nm.get("state", ""),
            "local_route_ready": bool(link and usable_address and default),
        })
    ready = any(item["local_route_ready"] for item in interfaces)
    warnings = []
    for item in interfaces:
        if item["local_route_ready"] and item["networkmanager_managed"] is False:
            warnings.append({
                "code": "CONNECTED_INTERFACE_NOT_MANAGED_BY_NETWORKMANAGER",
                "interface": item["name"],
                "message": "A NetworkManager desktop applet may not represent this working interface.",
            })
    return {"ok": True, "interfaces": interfaces, "default_routes": defaults,
            "managers": managers, "connectivity": "LINK_ADDRESS_DEFAULT_ROUTE" if ready else "NOT_ESTABLISHED",
            "internet_verified": False,
            "internet_note": "Link, address and route checks do not verify DNS or Internet access.",
            "warnings": warnings}

def inspect():
    addresses = _json(["ip", "-j", "address", "show"])
    routes4 = _json(["ip", "-j", "-4", "route", "show", "default"])
    routes6 = _json(["ip", "-j", "-6", "route", "show", "default"])
    nm = _nm_devices() if shutil.which("nmcli") else None
    managers = {
        "NetworkManager": {"available": bool(shutil.which("nmcli")), "active": _active("NetworkManager.service")},
        "ifupdown": {"available": _available("ifquery"), "active": _active("networking.service")},
        "systemd-networkd": {"active": _active("systemd-networkd.service")},
    }
    active = _ifupdown() if managers["ifupdown"]["active"] else set()
    value = summarize(addresses, {"ipv4": routes4, "ipv6": routes6}, nm, active, managers)
    if addresses is None or (routes4 is None and routes6 is None):
        value.update(ok=False, error="NETWORK_KERNEL_STATUS_UNAVAILABLE")
    if nm is None and managers["NetworkManager"]["available"]:
        value["warnings"].append({"code": "NETWORKMANAGER_STATUS_UNAVAILABLE"})
    return value
