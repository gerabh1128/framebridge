"""Profile-scoped LAN TLS bootstrap; never changes OS trust or OpenAI credentials."""
from __future__ import annotations

import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

from .config import load_config, save_config, state_path, private_dir


def _crypto():
    # The original network_security module has network policy helpers, but no
    # certificate generator. Reuse the captured SDK's cryptography dependency.
    from .runtime import PREFIX
    for vendor in (Path(PREFIX) / "original/geranas/FrameBridge-Fleet-Hub/vendor",
                   Path(PREFIX) / "sources/original-geranas/FrameBridge-Fleet-Hub/vendor"):
        if vendor.is_dir():
            value = str(vendor)
            if value not in sys.path:
                sys.path.insert(0, value)
            break
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    return x509, hashes, serialization, ec, NameOID, ExtendedKeyUsageOID


def _dns_name(value):
    if not isinstance(value, str):
        raise ValueError("Enter a certificate DNS name.")
    try:
        value = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("The certificate DNS name is invalid.") from None
    if not value or len(value) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split(".")):
        raise ValueError("Use a DNS name containing letters, numbers, hyphens, and dots.")
    return value


def _address(value):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        raise ValueError("Enter an IPv4 or IPv6 address for the broker certificate.") from None
    if address.is_unspecified or address.is_multicast or getattr(address, "scope_id", None):
        raise ValueError("The advertised address must identify a computer, not a wildcard or multicast address.")
    return address


def _url(host, port):
    return "https://" + ("[" + host + "]" if ":" in host else host) + ":" + str(port)


def _write(path, data, mode):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(mode)


def create_certificates(state, host, address, *, listen_host=None, advertise_host=None):
    """Create a new private CA and server certificate in a stopped central profile."""
    state = state_path(state)
    config = load_config(state)
    if config["role"] != "central":
        raise ValueError("Create LAN certificates in a central profile. Run framebridge setup first.")
    from .runtime import status
    current = status(state)
    if current.get("running") or any(p.get("alive") for p in current.get("processes", [])):
        raise RuntimeError("Stop this profile before creating certificates.")
    directory = state / "tls"
    if directory.exists() or directory.is_symlink() or any(config.get(k) for k in ("tls_cert", "tls_key", "tls_ca")):
        raise FileExistsError("This profile already has TLS material or references. Use existing certificates or an explicit new profile.")
    host = _dns_name(host)
    address = _address(address)
    listen = listen_host if listen_host is not None else config.get("broker_listen_host", "127.0.0.1")
    if listen != "localhost":
        try:
            ipaddress.ip_address(listen)
        except (ValueError, TypeError):
            raise ValueError("The listen host must be an IP address or localhost.") from None
    advertised = str(address) if advertise_host is None else str(advertise_host)
    if advertised not in {host, str(address), "localhost", "127.0.0.1", "::1"}:
        raise ValueError("The advertised host must match the certificate DNS name or one of its IP addresses.")
    x509, hashes, serialization, ec, NameOID, ExtendedKeyUsageOID = _crypto()
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    server_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FrameBridge " + config["name"] + " local CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                      data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                      crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    dns_names = list(dict.fromkeys([host, "localhost"]))
    ips = list(dict.fromkeys([address, ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]))
    san = x509.SubjectAlternativeName([x509.DNSName(v) for v in dns_names] + [x509.IPAddress(v) for v in ips])
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    server = (x509.CertificateBuilder().subject_name(server_name).issuer_name(ca.subject)
              .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - datetime.timedelta(minutes=5))
              .not_valid_after(now + datetime.timedelta(days=825))
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .add_extension(san, critical=False)
              .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                          crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
    state = private_dir(state)
    directory.mkdir(mode=0o700)  # Exclusive: never overwrite an existing CA or key.
    ca_file = directory / "ca-public.pem"
    ca_key_file = directory / "ca-private.key"
    server_file = directory / "server.pem"
    server_key_file = directory / "server-private.key"
    encoding = serialization.Encoding.PEM
    fmt = serialization.PrivateFormat.PKCS8
    no_password = serialization.NoEncryption()
    updated = dict(config)
    updated.update(tls_ca=str(ca_file), tls_cert=str(server_file), tls_key=str(server_key_file),
                   broker_url=_url(advertised, config["ports"]["broker"]),
                   broker_control_url=_url("127.0.0.1", config["ports"]["broker"]),
                   broker_listen_host=listen)
    public = {
        "ca_public_file": str(ca_file),
        "ca_fingerprint_sha256": ca.fingerprint(hashes.SHA256()).hex(),
        "server_certificate_file": str(server_file),
        "server_fingerprint_sha256": server.fingerprint(hashes.SHA256()).hex(),
        "dns_subject_alternative_names": dns_names,
        "ip_subject_alternative_names": [str(v) for v in ips],
        "broker_url": updated["broker_url"],
        "broker_control_url": updated["broker_control_url"],
        "broker_listen_host": listen,
        "private_key_values_exported": False,
        "openai_credentials_created_or_saved": False,
    }
    try:
        _write(ca_key_file, ca_key.private_bytes(encoding, fmt, no_password), 0o600)
        _write(server_key_file, server_key.private_bytes(encoding, fmt, no_password), 0o600)
        _write(server_file, server.public_bytes(encoding) + ca.public_bytes(encoding), 0o600)
        _write(ca_file, ca.public_bytes(encoding), 0o644)
        _write(directory / "certificates.json", (json.dumps(public, indent=2) + "\n").encode(), 0o600)
        save_config(state, updated)
    except Exception:
        shutil.rmtree(directory)
        raise
    return public
