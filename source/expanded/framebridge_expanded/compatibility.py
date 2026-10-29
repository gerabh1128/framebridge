"""Explicit candidate Linux profiles and installed-artifact ABI checks.

Eligibility is not evidence of a successful installation on a target system.
Never import vendored native code merely to diagnose an incompatible artifact.
"""
from __future__ import annotations
import json
import os
import platform
import re
import sys
from pathlib import Path

PROFILES = {
    "debian13-amd64-py313": ("debian", "13", "3.13", "cp313"),
    "ubuntu24.04-amd64-py312": ("ubuntu", "24.04", "3.12", "cp312"),
    "ubuntu26.04-amd64-py314": ("ubuntu", "26.04", "3.14", "cp314"),
    "mint22-amd64-py312": ("linuxmint", "22", "3.12", "cp312"),
    "lmde7-amd64-py313": ("linuxmint", "7", "3.13", "cp313"),
}
SYSTEM_PYTHONS = frozenset({"/usr/bin/python3", "/usr/bin/python3.12",
                            "/usr/bin/python3.13", "/usr/bin/python3.14"})
INSTALLED_PREFIX = Path("/usr/lib/framebridge-expanded")
VENDOR_RELATIVE = Path("original/geranas/FrameBridge-Fleet-Hub/vendor")


def profile_for(os_release):
    """Match named releases only; ID_LIKE never opts other derivatives in."""
    distro = os_release.get("ID", "")
    version = os_release.get("VERSION_ID", "")
    for name, (expected, release, _minor, _abi) in PROFILES.items():
        matches = version == release
        if name == "mint22-amd64-py312":
            matches = bool(re.fullmatch(r"22(?:\.\d+)?", version))
        if distro == expected and matches:
            return name
    return None


def _libc():
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION") or ""
        if value.startswith("glibc "):
            return "glibc", value.split()[-1]
    except (OSError, ValueError):
        pass
    return platform.libc_ver()


def _vendor_issues(prefix, abi):
    vendor = prefix / VENDOR_RELATIVE
    issues = []
    expected = abi.removeprefix("cp")
    if not vendor.is_dir():
        return ["Bundled vendor directory is missing; use a complete artifact for this profile."]
    required = {
        "_cffi_backend": list(vendor.glob("_cffi_backend.cpython-*.so")),
        "pydantic_core": list((vendor / "pydantic_core").glob("_pydantic_core.cpython-*.so")),
        "rpds": list((vendor / "rpds").glob("rpds.cpython-*.so")),
    }
    for module, files in required.items():
        if len(files) != 1:
            issues.append("Expected exactly one bundled native extension for " + module + ".")
    for path in vendor.rglob("*.so"):
        match = re.search(r"\.cpython-([^-]+)-", path.name)
        if match and (match.group(1) != expected or "x86_64-linux-gnu" not in path.name):
            issues.append("Bundled native extension does not match " + abi + " amd64: " + str(path.relative_to(vendor)))
    return issues


def assess(prefix, *, os_release=None, machine=None, python_version=None, libc=None, implementation=None, abi_flags=None):
    """Check profile and artifact identity without importing native modules."""
    prefix = Path(prefix)
    issues = []
    warnings = []
    if os_release is None:
        try:
            os_release = platform.freedesktop_os_release()
        except OSError:
            os_release = {}
    machine = platform.machine() if machine is None else machine
    version = sys.version_info[:2] if python_version is None else python_version[:2]
    minor = ".".join(str(x) for x in version)
    abi = "cp" + "".join(str(x) for x in version)
    implementation = platform.python_implementation() if implementation is None else implementation
    abi_flags = getattr(sys, "abiflags", "") if abi_flags is None else abi_flags
    if implementation != "CPython" or abi_flags:
        issues.append("This artifact requires standard CPython; alternate, debug or free-threaded interpreter ABIs need separate native artifacts.")
    selected = profile_for(os_release)
    if selected is None:
        issues.append("No candidate profile for this release. Choose Debian 13, Ubuntu 24.04/26.04, Linux Mint 22.x or LMDE 7.")
    if machine not in {"x86_64", "amd64"}:
        issues.append("This artifact supports amd64 only; ARM and other architectures need separate artifacts.")
    if selected and minor != PROFILES[selected][2]:
        issues.append("Profile " + selected + " requires Python " + PROFILES[selected][2] + "; running " + minor + ". Install its matching artifact without replacing the system Python.")
    libc_name, libc_version = _libc() if libc is None else libc
    match = re.match(r"(\d+)\.(\d+)", libc_version)
    if libc_name != "glibc" or not match or tuple(map(int, match.groups())) < (2, 34):
        issues.append("Bundled native libraries require glibc >= 2.34; this libc is not eligible.")
    manifest = None
    manifest_path = prefix / "release-profile.json"
    if manifest_path.exists():
        try:
            if manifest_path.stat().st_size > 16384:
                raise ValueError("manifest too large")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1:
                raise ValueError("unsupported schema")
            if manifest.get("profile_id") not in PROFILES:
                raise ValueError("unknown profile")
            expected = PROFILES[manifest["profile_id"]]
            if (manifest.get("architecture"), manifest.get("python_minor"), manifest.get("python_abi")) != ("amd64", expected[2], expected[3]):
                raise ValueError("inconsistent profile ABI")
            if manifest.get("validation_status") not in {"candidate", "release_build", "validated"}:
                raise ValueError("missing validation status")
            if manifest["profile_id"] != selected or manifest["python_minor"] != minor or manifest["python_abi"] != abi:
                issues.append("Installed artifact profile/ABI does not match this operating system and interpreter.")
        except (OSError, UnicodeError, ValueError, TypeError):
            issues.append("Invalid release-profile.json; install a complete matching artifact.")
            manifest = None
    else:
        # Source-tree fallback is intentionally the reviewed release's ABI only.
        if prefix.resolve() == INSTALLED_PREFIX or selected != "debian13-amd64-py313" or minor != "3.13":
            issues.append("Missing release-profile.json; this target requires a profile-specific artifact.")
        else:
            warnings.append("Source checkout has no artifact manifest; only the legacy Debian 13/cp313 profile is eligible.")
    issues.extend(_vendor_issues(prefix, abi))
    validation = manifest.get("validation_status") if manifest else "source-unvalidated"
    if validation == "release_build":
        warnings.append("Release assembly does not establish a tested installation on this target; consult the external release acceptance record.")
    elif validation != "validated":
        warnings.append("Candidate compatibility checks do not establish a tested installation on this target.")
    return {"ok": not issues, "profile_id": selected, "artifact_profile_id": manifest.get("profile_id") if manifest else None,
            "architecture": machine, "python_minor": minor, "python_abi": abi, "python_implementation": implementation, "python_abi_flags": abi_flags,
            "libc": {"name": libc_name, "version": libc_version}, "validation_status": validation,
            "issues": issues, "warnings": warnings}


def display_support(env, context):
    wayland = env.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(env.get("WAYLAND_DISPLAY"))
    notes = []
    if wayland:
        notes.append("Native Wayland desktop capture/control is not implemented. XWayland does not establish access to the full Wayland desktop; text tools remain available, or explicitly select a private virtual X11 display.")
    return {"session_type": "wayland" if wayland else env.get("XDG_SESSION_TYPE", "unknown"),
            "physical_x11_verified": context.get("kind") == "physical-x11",
            "private_virtual_x11": context.get("kind") == "virtual-x11",
            "native_wayland_cua": False, "notes": notes}
