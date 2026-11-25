"""Bundle the pinned CPython distribution and its upstream license bundle."""
import hashlib
import os
from build_profiles import VENDOR_RELATIVE
import json
import subprocess
import tarfile
from pathlib import Path

PROFILE = "linux-amd64-cp313"
SUFFIX = "+portable.4"
VERSION = "3.13.18"
INTERPRETER = "/usr/lib/framebridge-expanded/python/bin/python3.13"
INSTALL_SHA = "50c774f6674740739f791c05a9177ed03e2fe02abebda9bca3512eb7309b37cc"
FULL_SHA = "dec4f6d23031233beddfe91b99751977ef96e90642109836b1f9ad80b0019a68"


def verify(path, expected):
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"Bundled Python input checksum mismatch: {path.name}")


def omitted_runtime_member(name):
    """No package installer in the shipped private, fixed runtime."""
    parts = Path(name).parts
    if parts[:3] != ("python", "lib", "python3.13"):
        return name in {"python/bin/pip", "python/bin/pip3", "python/bin/pip3.13"}
    if len(parts) >= 4 and parts[3] == "ensurepip":
        return True
    if len(parts) >= 5 and parts[3] == "site-packages":
        return parts[4] == "pip" or (parts[4].startswith("pip-") and parts[4].endswith(".dist-info"))
    return False


def install(prefix, docs, archives):
    install_archive, full_archive = archives
    verify(install_archive, INSTALL_SHA)
    verify(full_archive, FULL_SHA)
    omitted = 0
    with tarfile.open(install_archive, "r:gz") as archive:
        for member in archive:
            if not member.name.startswith("python/") and member.name != "python":
                raise ValueError("Unexpected runtime archive member")
            if omitted_runtime_member(member.name):
                omitted += 1
                continue
            if "__pycache__" in Path(member.name).parts or member.name.endswith((".pyc", ".pyo")):
                continue
            archive.extract(member, prefix, filter="data")
    if not omitted:
        raise ValueError("Pinned runtime no longer contains expected installer material")
    interpreter = prefix / "python/bin/python3.13"
    if not interpreter.resolve().is_relative_to(prefix / "python") or not interpreter.is_file():
        raise ValueError("Private CPython executable missing")
    licenses = docs / "bundled-python"
    licenses.mkdir(parents=True)
    subprocess.run(["tar", "--zstd", "-xf", str(full_archive.resolve()), "--strip-components=1",
                    "-C", str(licenses), "python/licenses", "python/PYTHON.json"], check=True)
    metadata = json.loads((licenses / "PYTHON.json").read_text())
    if metadata["python_version"] != VERSION or metadata["target_triple"] != "x86_64-unknown-linux-gnu":
        raise ValueError("Upstream runtime metadata mismatch")
    if not (licenses / "licenses/LICENSE.cpython.txt").is_file():
        raise ValueError("Upstream runtime license missing")
    # Keep the vendored ABI modules available to the private interpreter only.
    site = prefix / "python/lib/python3.13/site-packages"
    vendor = prefix / VENDOR_RELATIVE
    if not vendor.is_dir() or not vendor.resolve().is_relative_to(prefix.resolve()):
        raise ValueError("Validated vendor directory missing from package")
    (site / "framebridge-vendor.pth").write_text(os.path.relpath(vendor, site) + "\n")
    for name in ("framebridge", "framebridge-original", "framebridge-native-expanded"):
        entry = prefix / "bin" / name
        line, rest = entry.read_bytes().split(b"\n", 1)
        if line != b"#!/usr/bin/python3":
            raise ValueError(f"Unexpected launcher: {name}")
        entry.write_bytes(b"#!" + INTERPRETER.encode("ascii") + b" -B\n" + rest)
    return {"implementation": "CPython", "version": VERSION, "build": "20261110",
            "archive_sha256": INSTALL_SHA, "full_archive_sha256": FULL_SHA,
            "installer_material": "omitted: pip and ensurepip", "omitted_archive_members": omitted}
