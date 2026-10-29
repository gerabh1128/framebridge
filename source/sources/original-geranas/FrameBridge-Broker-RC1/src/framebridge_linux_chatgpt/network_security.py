from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
from typing import Any

SCHEMA = 1
PAIRING_WINDOW_TTL_SECONDS = 600

RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
ULA_V6 = ipaddress.ip_network("fc00::/7")

OVERLAY_NAMES = {"tailscale0"}
VIRTUAL_PREFIXES = (
    "docker", "br-", "virbr", "veth", "lxc", "waydroid",
    "tap", "tun", "zt", "wg",
)


def _run(argv: list[str], timeout: int = 5) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _run_json(argv: list[str]) -> list[dict[str, Any]]:
    try:
        cp = _run(argv)
    except Exception:
        return []
    if cp.returncode != 0:
        return []
    try:
        value = json.loads(cp.stdout or "[]")
    except Exception:
        return []
    return value if isinstance(value, list) else []


def address_class(address: str) -> str:
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return "INVALID"
    if ip.is_loopback:
        return "LOOPBACK"
    if ip.version == 4:
        if any(ip in net for net in RFC1918):
            return "PRIVATE_LAN"
        if ip.is_link_local:
            return "LINK_LOCAL"
        if ip.is_global:
            return "PUBLIC"
        return "SPECIAL"
    if ip in ULA_V6:
        return "ULA"
    if ip.is_link_local:
        return "LINK_LOCAL"
    if ip.is_global:
        return "PUBLIC"
    return "SPECIAL"


def classify_interface(
    name: str,
    *,
    has_device: bool = False,
    wireless: bool = False,
) -> str:
    n = str(name or "")
    if n == "lo":
        return "LOOPBACK"
    if n in OVERLAY_NAMES:
        return "PRIVATE_OVERLAY"
    if n.startswith(VIRTUAL_PREFIXES):
        return "VIRTUAL_LOCAL"
    if wireless or n.startswith(("wl", "wlan")):
        return "PHYSICAL_LAN"
    if has_device or n.startswith(("en", "eth")):
        return "PHYSICAL_LAN"
    return "UNKNOWN"


def _sysfs_interface_kind(name: str) -> str:
    base = Path("/sys/class/net") / name
    return classify_interface(
        name,
        has_device=(base / "device").exists(),
        wireless=(base / "wireless").exists(),
    )


def _prefix_subnet(address: str, prefixlen: int) -> str:
    try:
        return str(ipaddress.ip_interface(f"{address}/{int(prefixlen)}").network)
    except Exception:
        return ""


def _default_routes() -> list[dict[str, Any]]:
    routes = _run_json(["ip", "-j", "-4", "route", "show", "default"])
    clean: list[dict[str, Any]] = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        clean.append(
            {
                "dev": str(r.get("dev") or ""),
                "gateway": str(r.get("gateway") or ""),
                "metric": int(r.get("metric") or 0),
                "prefsrc": str(r.get("prefsrc") or ""),
            }
        )
    return clean


def _network_manager_identity(ifname: str) -> dict[str, str]:
    result = {"connection": "", "connection_uuid": ""}
    try:
        cp = _run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", ifname])
    except Exception:
        return result
    if cp.returncode != 0:
        return result
    connection = (cp.stdout or "").strip().splitlines()
    if not connection:
        return result
    name = connection[0].strip()
    if not name or name == "--":
        return result
    result["connection"] = name
    try:
        cp2 = _run(["nmcli", "-g", "connection.uuid", "connection", "show", name])
    except Exception:
        return result
    if cp2.returncode == 0:
        result["connection_uuid"] = (cp2.stdout or "").strip().splitlines()[0] if (cp2.stdout or "").strip() else ""
    return result


def collect_interfaces() -> list[dict[str, Any]]:
    links = _run_json(["ip", "-j", "link", "show"])
    addrs = _run_json(["ip", "-j", "addr", "show"])
    addr_by_name = {str(x.get("ifname") or ""): x for x in addrs if isinstance(x, dict)}
    out: list[dict[str, Any]] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        name = str(link.get("ifname") or "")
        if not name:
            continue
        a = addr_by_name.get(name, {})
        ipv4 = []
        ipv6 = []
        for ai in a.get("addr_info") or []:
            if not isinstance(ai, dict):
                continue
            local = str(ai.get("local") or "")
            if not local:
                continue
            item = {
                "address": local,
                "prefixlen": int(ai.get("prefixlen") or 0),
                "class": address_class(local),
                "scope": str(ai.get("scope") or ""),
            }
            if ai.get("family") == "inet":
                ipv4.append(item)
            elif ai.get("family") == "inet6":
                ipv6.append(item)
        kind = _sysfs_interface_kind(name)
        out.append(
            {
                "name": name,
                "kind": kind,
                "operstate": str(link.get("operstate") or ""),
                "flags": list(link.get("flags") or []),
                "ipv4": ipv4,
                "ipv6": ipv6,
            }
        )
    return out


def choose_pairing_candidate(
    interfaces: list[dict[str, Any]],
    routes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select only an unambiguous physical private-LAN binding.

    Metric zero is valid. Route preferred source is honored. Equal-priority
    candidates that differ in interface/address/prefix/gateway are blocked,
    not resolved by comparing payload dictionaries or snapshot order.
    """
    route_by_dev: dict[str, list[dict[str, Any]]] = {}
    for route in routes:
        route_by_dev.setdefault(str(route.get("dev") or ""), []).append(route)

    choices: dict[tuple, tuple[tuple, dict[str, Any]]] = {}
    for iface in interfaces:
        if iface.get("kind") != "PHYSICAL_LAN":
            continue
        if str(iface.get("operstate") or "").upper() not in {"UP", "UNKNOWN"}:
            continue
        name = str(iface.get("name") or "")
        if not name:
            continue
        dev_routes = route_by_dev.get(name, [])
        if dev_routes:
            minimum = min(int(r.get("metric") or 0) for r in dev_routes)
            best_routes = [r for r in dev_routes if int(r.get("metric") or 0) == minimum]
        else:
            best_routes = [{}]
        for selected_route in best_routes:
            preferred = str(selected_route.get("prefsrc") or "")
            metric = int(selected_route.get("metric") or 0) if dev_routes else None
            for addr in iface.get("ipv4") or []:
                address = str(addr.get("address") or "")
                if address_class(address) != "PRIVATE_LAN":
                    continue
                if preferred and address != preferred:
                    continue
                prefix = addr.get("prefixlen")
                if isinstance(prefix, bool) or not isinstance(prefix, int) or not 1 <= prefix <= 32:
                    continue
                gateway = str(selected_route.get("gateway") or "")
                item = {
                    "interface": name,
                    "interface_kind": "PHYSICAL_LAN",
                    "address": address,
                    "prefixlen": prefix,
                    "subnet": _prefix_subnet(address, prefix),
                    "address_class": "PRIVATE_LAN",
                    "gateway": gateway,
                    "route_metric": metric,
                }
                rank = (0 if dev_routes else 1, metric if metric is not None else 0,
                        0 if preferred else 1)
                identity = (name, address, prefix, gateway, metric)
                choices[identity] = (rank, item)
    if not choices:
        return None
    best_rank = min(rank for rank, item in choices.values())
    top = [item for rank, item in choices.values() if rank == best_rank]
    if len(top) != 1:
        return None
    selected = dict(top[0])
    selected.update(_network_manager_identity(selected["interface"]))
    binding = {key: selected.get(key) for key in (
        "interface", "address", "prefixlen", "gateway", "route_metric", "connection_uuid"
    )}
    selected["network_fingerprint_schema"] = 2
    raw = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    selected["network_fingerprint"] = hashlib.sha256(raw).hexdigest()
    return selected


def _service_active(name: str) -> bool:
    try:
        cp = _run(["systemctl", "is-active", "--quiet", name])
    except Exception:
        return False
    return cp.returncode == 0


def _ufw_config_enabled(path: Path=Path("/etc/ufw/ufw.conf")) -> bool:
    try:
        text=path.read_text(encoding="utf-8",errors="replace")
    except OSError:
        return False
    for raw in text.splitlines():
        line=raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("ENABLED="):
            return line.split("=",1)[1].strip().lower() in {"yes","true","1"}
    return False


def firewall_backend() -> dict[str, Any]:
    ufw_systemd_active=_service_active("ufw")
    ufw_config_enabled=_ufw_config_enabled()
    if ufw_systemd_active or ufw_config_enabled:
        return {
            "backend": "ufw",
            "active": True,
            "systemd_active": ufw_systemd_active,
            "config_enabled": ufw_config_enabled,
            "managed_pairing_rules": True,
        }
    if _service_active("firewalld"):
        return {"backend": "firewalld", "active": True, "managed_pairing_rules": False}
    if _service_active("nftables"):
        return {"backend": "nftables", "active": True, "managed_pairing_rules": False}
    return {"backend": "none", "active": False, "managed_pairing_rules": False}


def ufw_pairing_plan(candidate: dict[str, Any], port: int) -> dict[str, Any]:
    iface = str(candidate.get("interface") or "")
    subnet = str(candidate.get("subnet") or "")
    address = str(candidate.get("address") or "")
    fp = str(candidate.get("network_fingerprint") or "")
    if not iface or not subnet or not address or not fp:
        return {"ok": False, "error": "PAIRING_CANDIDATE_INCOMPLETE"}
    return {
        "ok": True,
        "backend": "ufw",
        "execution_backend": "iptables-runtime-inside-ufw",
        "chain": "ufw-user-input",
        "execute": False,
        "operation": "runtime_scoped_accept",
        "interface": iface,
        "source_subnet": subnet,
        "destination_address": address,
        "port": int(port),
        "protocol": "tcp",
        "network_fingerprint": fp,
        "ttl_seconds": PAIRING_WINDOW_TTL_SECONDS,
        "requires_local_tty": True,
        "runtime_only": True,
        "reboot_fail_closed": True,
        "automatic_expiry": True,
        "persistent_ufw_rule": False,
        "rule_comment_template": "FrameBridge:<window_id>:pairing",
    }


def inspect_network() -> dict[str, Any]:
    interfaces = collect_interfaces()
    routes = _default_routes()
    candidate = choose_pairing_candidate(interfaces, routes)
    fw = firewall_backend()
    return {
        "ok": True,
        "schema": SCHEMA,
        "interfaces": interfaces,
        "default_routes": routes,
        "pairing_candidate": candidate,
        "firewall": fw,
        "policy": {
            "local_pairing_transport": "LAN_HTTP",
            "tailscale_required": False,
            "public_http_allowed": False,
            "wildcard_pairing_bind_allowed": False,
            "local_tty_required": True,
            "pairing_window_ttl_seconds": PAIRING_WINDOW_TTL_SECONDS,
        },
    }


def pairing_policy(port: int = 18765) -> dict[str, Any]:
    snap = inspect_network()
    candidate = snap.get("pairing_candidate")
    fw = snap.get("firewall") or {}
    if not candidate:
        return {
            "ok": False,
            "schema": SCHEMA,
            "state": "BLOCKED",
            "reason": "NO_ELIGIBLE_PHYSICAL_PRIVATE_LAN",
            "execute": False,
            "snapshot": snap,
        }
    if not fw.get("active"):
        return {
            "ok": False,
            "schema": SCHEMA,
            "state": "BLOCKED",
            "reason": "NO_ACTIVE_FIREWALL_BACKEND",
            "execute": False,
            "snapshot": snap,
        }
    if fw.get("backend") != "ufw":
        return {
            "ok": False,
            "schema": SCHEMA,
            "state": "BLOCKED",
            "reason": "FIREWALL_BACKEND_NOT_YET_MANAGED",
            "execute": False,
            "snapshot": snap,
        }
    return {
        "ok": True,
        "schema": SCHEMA,
        "state": "ELIGIBLE_REQUIRES_LOCAL_TTY",
        "execute": False,
        "candidate": candidate,
        "firewall_plan": ufw_pairing_plan(candidate, int(port)),
        "network_change_policy": "INVALIDATE_PENDING_PAIR_AND_CLOSE_OLD_WINDOW",
    }

CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
FORWARD_PROTOCOLS = {"any", "tcp", "udp"}


def _valid_interface_name(value: str) -> bool:
    import re
    return re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", str(value or "")) is not None


def _normalize_ipv4_network(value: str) -> str:
    try:
        net = ipaddress.ip_network(str(value), strict=False)
    except ValueError as exc:
        raise ValueError("NETWORK_CIDR_INVALID") from exc
    if net.version != 4:
        raise ValueError("NETWORK_IPV4_REQUIRED")
    if net.is_multicast or net.is_loopback or net.is_link_local:
        raise ValueError("NETWORK_CIDR_UNSAFE")
    return str(net)


def _source_network_allowed(value: str) -> bool:
    net = ipaddress.ip_network(value, strict=False)
    if net.version != 4:
        return False
    if net.subnet_of(CGNAT_V4):
        return True
    return any(net.subnet_of(x) for x in RFC1918)


def forwarding_policy(
    *,
    ingress_interface: str,
    egress_interface: str,
    source_subnet: str,
    destination_subnet: str,
    protocol: str = "any",
    destination_port: int = 0,
    nat: bool = False,
    lifetime: str = "runtime",
) -> dict[str, Any]:
    ingress_interface = str(ingress_interface or "").strip()
    egress_interface = str(egress_interface or "").strip()
    protocol = str(protocol or "any").strip().lower()
    lifetime = str(lifetime or "runtime").strip().lower()
    try:
        destination_port = int(destination_port)
    except Exception:
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_PORT_INVALID", "execute": False}

    if not _valid_interface_name(ingress_interface) or not _valid_interface_name(egress_interface):
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_INTERFACE_INVALID", "execute": False}
    if ingress_interface == egress_interface:
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_INTERFACES_MUST_DIFFER", "execute": False}

    try:
        source_subnet = _normalize_ipv4_network(source_subnet)
        destination_subnet = _normalize_ipv4_network(destination_subnet)
    except ValueError as exc:
        return {"ok": False, "state": "BLOCKED", "reason": str(exc), "execute": False}

    if not _source_network_allowed(source_subnet):
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_SOURCE_NOT_PRIVATE", "execute": False}

    if protocol not in FORWARD_PROTOCOLS:
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_PROTOCOL_INVALID", "execute": False}
    if destination_port < 0 or destination_port > 65535:
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_PORT_INVALID", "execute": False}
    if protocol == "any" and destination_port != 0:
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_PORT_REQUIRES_PROTOCOL", "execute": False}
    if lifetime != "runtime":
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_PERSISTENCE_REQUIRES_SEPARATE_CAPABILITY", "execute": False}

    fw = firewall_backend()
    if not fw.get("active"):
        return {"ok": False, "state": "BLOCKED", "reason": "NO_ACTIVE_FIREWALL_BACKEND", "execute": False}
    if fw.get("backend") != "ufw":
        return {"ok": False, "state": "BLOCKED", "reason": "FIREWALL_BACKEND_NOT_YET_MANAGED", "execute": False}

    names = {x.get("name") for x in collect_interfaces()}
    if ingress_interface not in names or egress_interface not in names:
        return {"ok": False, "state": "BLOCKED", "reason": "NETWORK_FORWARD_INTERFACE_NOT_PRESENT", "execute": False}

    args = {
        "ingress_interface": ingress_interface,
        "egress_interface": egress_interface,
        "source_subnet": source_subnet,
        "destination_subnet": destination_subnet,
        "protocol": protocol,
        "destination_port": destination_port,
        "nat": bool(nat),
        "lifetime": "runtime",
    }
    return {
        "ok": True,
        "schema": SCHEMA,
        "state": "ELIGIBLE_REQUIRES_TYPED_PRIVILEGED_PLAN",
        "execute": False,
        "capability": "network.forward",
        "args": args,
        "security": {
            "local_tty_privileged_required": True,
            "independent_rollback_approval_required": True,
            "runtime_only": True,
            "reboot_fail_closed": True,
            "nat_explicit": bool(nat),
            "wildcard_source_forbidden": True,
        },
    }
