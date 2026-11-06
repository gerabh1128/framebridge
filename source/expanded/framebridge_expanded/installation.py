"""Package role discovery. LAN advertisements guide placement, never trust."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid

DIRECTORY = Path("/var/lib/framebridge-expanded-deployment")
SERVICE = Path("/etc/avahi/services/framebridge-original-expanded-role.service")
RESERVATION = Path("/etc/framebridge-expanded/lan-central")
SERVICE_TYPE = "_framebridge-central._tcp"
MANAGED = b"<!-- FrameBridge Original Expanded managed role -->"
LOCAL_NETWORKS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
DNS_NAME = re.compile(r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
IDENTITY = re.compile(r"[a-f0-9]{32}")


class DiscoveryPending(ValueError):
    """An incomplete LAN check is never treated as an empty LAN."""


def _command(*args):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=7,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8"}).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise DiscoveryPending("LAN_DISCOVERY_UNAVAILABLE") from None


def _lan_address(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in LOCAL_NETWORKS)


def _lan_interfaces():
    interfaces = {}
    for item in json.loads(_command("/usr/sbin/ip", "-j", "-4", "address", "show", "up")):
        flags = set(item.get("flags", []))
        if not {"UP", "LOWER_UP", "MULTICAST"} <= flags or flags & {"LOOPBACK", "POINTOPOINT"}:
            continue
        addresses = {a["local"] for a in item.get("addr_info", []) if _lan_address(a.get("local", ""))}
        if addresses:
            interfaces[item["ifname"]] = addresses
    if len(interfaces) > 1:
        default = {r.get("dev") for r in json.loads(_command("/usr/sbin/ip", "-j", "-4", "route", "show", "default"))}
        interfaces = {name: addresses for name, addresses in interfaces.items() if name in default}
    if len(interfaces) != 1:
        raise DiscoveryPending("LAN_INTERFACE_UNAVAILABLE_OR_AMBIGUOUS")
    return next(iter(interfaces.items()))


def _parse_browse(output, interface):
    entries = {}
    for line in output.splitlines():
        fields = line.split(";", 9)
        if len(fields) < 6 or fields[1:3] != [interface, "IPv4"] or fields[4:6] != [SERVICE_TYPE, "local"]:
            continue
        key = fields[3]
        if fields[0] == "-":
            entries.pop(key, None)
        elif fields[0] == "+":
            entries.setdefault(key, None)
        elif fields[0] == "=":
            try:
                host, address, port, raw_txt = fields[6:10]
                txt = shlex.split(raw_txt)
                identity = next(item[3:] for item in txt if item.startswith("id="))
                role = next(item[5:] for item in txt if item.startswith("role="))
                if ("v=1" not in txt or not IDENTITY.fullmatch(identity) or role not in {"claim", "central"}
                        or not DNS_NAME.fullmatch(host) or not _lan_address(address) or not 1024 <= int(port) <= 65535):
                    raise ValueError()
            except (ValueError, StopIteration):
                raise DiscoveryPending("LAN_ADVERTISEMENT_INVALID") from None
            entries[key] = {"id": identity, "role": role, "host": host, "address": address, "port": int(port)}
    if any(entry is None for entry in entries.values()):
        raise DiscoveryPending("LAN_ADVERTISEMENT_UNRESOLVED")
    unique = {entry["id"]: entry for entry in entries.values()}
    if len(unique) != len(entries):
        raise DiscoveryPending("LAN_IDENTITY_COLLISION")
    return list(unique.values())


def _browse(interface):
    output = _command("/usr/bin/avahi-browse", "-d", "local", "-rtpk", SERVICE_TYPE)
    return _parse_browse(output, interface)


def _checked(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("INSTALLATION_PATH_UNTRUSTED")
    return info


def _replace(path, content):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _reservation(addresses):
    try:
        _checked(RESERVATION.parent, directory=True)
        if _checked(RESERVATION).st_size > 300:
            raise ValueError("LAN_DNS_RESERVATION_INVALID")
    except FileNotFoundError:
        return None
    name = RESERVATION.read_text(encoding="ascii").strip()
    if not DNS_NAME.fullmatch(name):
        raise ValueError("LAN_DNS_RESERVATION_INVALID")
    result = _command("/usr/bin/getent", "ahostsv4", name + ".")
    resolved = {line.split()[0] for line in result.splitlines() if line.split()}
    if not resolved or not all(map(_lan_address, resolved)):
        raise DiscoveryPending("LAN_DNS_RESERVATION_UNRESOLVED")
    if resolved & addresses and resolved - addresses:
        raise DiscoveryPending("LAN_DNS_RESERVATION_AMBIGUOUS")
    return None if resolved <= addresses else name


def _service_owned():
    try:
        if _checked(SERVICE).st_size > 2048 or MANAGED not in SERVICE.read_bytes():
            raise ValueError("LAN_ADVERTISEMENT_PATH_OCCUPIED")
        return True
    except FileNotFoundError:
        return False


def _announce(identity, role, port):
    if not isinstance(identity, str) or not IDENTITY.fullmatch(identity) or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("LAN_ADVERTISEMENT_IDENTITY_INVALID")
    _checked(SERVICE.parent, directory=True)
    _service_owned()
    xml = f"""<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
{MANAGED.decode()}
<service-group>
  <name replace-wildcards="yes">FrameBridge on %h</name>
  <service protocol="ipv4">
    <type>{SERVICE_TYPE}</type><domain-name>local</domain-name><port>{port}</port>
    <txt-record>v=1</txt-record><txt-record>id={identity}</txt-record><txt-record>role={role}</txt-record>
  </service>
</service-group>
"""
    _replace(SERVICE, xml.encode("ascii"))
    _command("/usr/sbin/avahi-daemon", "--reload")


def _withdraw():
    if _service_owned():
        SERVICE.unlink()
        try:
            _command("/usr/sbin/avahi-daemon", "--reload")
        except DiscoveryPending:
            pass


def _decision(role, source, **details):
    return {"schema": 1, "role": role, "source": source, **details}


def _single_central(entries):
    if len(entries) == 1 and entries[0]["role"] == "central":
        return entries[0]
    if entries:
        raise DiscoveryPending("LAN_CENTRAL_OR_CLAIM_CONFLICT")
    return None


def discover_new():
    interface, addresses = _lan_interfaces()
    reserved = _reservation(addresses)
    if reserved:
        return _decision("node", "local_dns_reservation", central_hint=reserved)
    central = _single_central(_browse(interface))
    if central:
        return _decision("node", "lan_discovery", central_hint=central["host"])
    identity = uuid.uuid4().hex
    committed = False
    try:
        for role in ("claim", "central"):
            _announce(identity, role, 20878)
            observed = _browse(interface)
            if (len(observed) != 1 or observed[0]["id"] != identity
                    or observed[0]["role"] != role or observed[0]["address"] not in addresses):
                raise DiscoveryPending("LAN_CENTRAL_CLAIM_UNCONFIRMED")
        committed = True
        return _decision("central", "lan_bootstrap", identity=identity, broker_port=20878)
    finally:
        if not committed:
            _withdraw()


def status():
    path = DIRECTORY / "role.json"
    try:
        _checked(DIRECTORY, directory=True)
        if _checked(path).st_size > 4096:
            raise ValueError("INSTALLATION_RECORD_INVALID")
    except FileNotFoundError:
        return _decision("unconfigured", "no_installation_record")
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("schema") != 1 or result.get("role") not in {"central", "node", "pending", "legacy"}:
        raise ValueError("INSTALLATION_RECORD_INVALID")
    # A role record from the 0.4 candidates can still carry the retired field.
    result.pop("panel_installed", None)
    return result


def _existing_role():
    from . import package_upgrade
    try:
        package_upgrade._private(package_upgrade.ROOT, 0)
        rows = json.loads(package_upgrade._read(package_upgrade.ROOT / "pending.json", 0))["profiles"]
    except FileNotFoundError:
        rows = []
    roles = {row.get("role") for row in rows}
    if roles == {"central"}:
        ports = {row.get("broker_port") for row in rows}
        port = next(iter(ports)) if len(ports) == 1 else None
        return _decision("central", "preserved_profile", identity=uuid.uuid4().hex,
                         broker_port=port if type(port) is int and 1024 <= port <= 65535 else 20878)
    if roles == {"node"}:
        return _decision("node", "preserved_profile")
    return _decision("legacy", "preserve_existing_profiles")


@contextmanager
def _installer_lock():
    if os.getuid() != 0 or os.geteuid() != 0:
        raise ValueError("INSTALLATION_ROOT_REQUIRED")
    DIRECTORY.mkdir(mode=0o755, exist_ok=True)
    _checked(DIRECTORY, directory=True)
    fd = os.open(DIRECTORY / ".lock", os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def configure(previous_version=""):
    with _installer_lock():
        current = status()
        if current["role"] in {"central", "node"} or (current["role"] == "legacy" and not previous_version):
            result = current
        elif current["role"] in {"unconfigured", "legacy"} and previous_version:
            result = _existing_role()
        else:
            try:
                # Discard a claim left by an interrupted configure on this host.
                _withdraw()
                result = discover_new()
            except (DiscoveryPending, OSError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, DiscoveryPending) else "LAN_DISCOVERY_CONFIGURATION_UNAVAILABLE"
                result = _decision("pending", "lan_detection_incomplete", reason=reason)
        if result["role"] == "central":
            # An already authorized central keeps its role through offline upgrades.
            try:
                _announce(result["identity"], "central", result["broker_port"])
                result["advertisement"] = "configured"
            except (DiscoveryPending, FileNotFoundError):
                result["advertisement"] = "daemon_unavailable"
        _replace(DIRECTORY / "role.json", (json.dumps(result, sort_keys=True) + "\n").encode())
        return result


def remove():
    with _installer_lock():
        _withdraw()


def main():
    # The dpkg hook runs Python isolated from environment and user import paths.
    if len(sys.argv) not in (2, 3):
        raise ValueError("INVALID_INSTALLATION_ACTION")
    if sys.argv[1] == "configure":
        print(json.dumps(configure(sys.argv[2] if len(sys.argv) == 3 else ""), sort_keys=True))
    elif sys.argv[1] == "remove":
        remove()
    else:
        raise ValueError("INVALID_INSTALLATION_ACTION")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from framebridge_expanded.installation import main
    main()
