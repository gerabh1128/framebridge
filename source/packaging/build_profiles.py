"""Explicit Debian-family candidate targets and offline vendor validation.

This module never installs dependencies or imports the target's native modules.
Profiles describe eligible build inputs, not completed OS/desktop validation.
"""
import base64
import csv
from email.parser import Parser
import hashlib
from pathlib import Path
import re


CANDIDATE_VERSION = "0.3.3"
VENDOR_RELATIVE = Path("original/geranas/FrameBridge-Fleet-Hub/vendor")
SOURCE_VENDOR = Path("sources/original-geranas/FrameBridge-Fleet-Hub/vendor")
GLIBC_BASELINE = (2, 34)
PROFILES = {
    "debian13-amd64-py313": {"os_id": "debian", "os_version": "13", "python_minor": "3.13"},
    "ubuntu24.04-amd64-py312": {"os_id": "ubuntu", "os_version": "24.04", "python_minor": "3.12"},
    "mint22-amd64-py312": {"os_id": "linuxmint", "os_version": "22", "python_minor": "3.12"},
    "ubuntu26.04-amd64-py314": {"os_id": "ubuntu", "os_version": "26.04", "python_minor": "3.14"},
    "lmde7-amd64-py313": {"os_id": "linuxmint", "os_version": "7", "python_minor": "3.13"},
}


def target_profile(profile_id):
    profile = dict(PROFILES[profile_id])
    profile.update(schema=1, profile_id=profile_id, architecture="amd64",
                   python_abi="cp" + profile["python_minor"].replace(".", ""),
                   validation_status="candidate", glibc_minimum="2.34")
    return profile


def _normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def distribution_metadata(directory):
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError("Vendor input must be a real directory: " + str(directory))
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("Vendor symlinks are not supported: " + str(path))
    result = {}
    for info in sorted(directory.glob("*.dist-info")):
        metadata = info / "METADATA"
        if not metadata.is_file():
            raise RuntimeError("Missing vendor METADATA: " + str(info))
        msg = Parser().parsestr(metadata.read_text(encoding="utf-8"))
        name, version = msg.get("Name"), msg.get("Version")
        if not name or not version or _normalized(name) in result:
            raise RuntimeError("Missing or duplicate vendor distribution identity: " + str(info))
        result[_normalized(name)] = (version, info, msg)
    if not result:
        raise RuntimeError("Vendor input contains no distribution metadata")
    return result


def vendor_versions(directory):
    return {name: value[0] for name, value in distribution_metadata(directory).items()}


def _python_allowed(requirement, minor):
    # Fail closed on unsupported specifier syntax rather than silently omitting it.
    version = tuple(map(int, minor.split("."))) + (0,)
    for part in requirement.split(","):
        match = re.fullmatch(r"\s*(>=|<=|!=|==|>|<)\s*(\d+(?:\.\d+){0,2})(\.\*)?\s*", part)
        if not match:
            raise RuntimeError("Unsupported Requires-Python specifier: " + requirement)
        op, number, wildcard = match.groups()
        components = tuple(map(int, number.split(".")))
        wanted = components + (0,) * (3 - len(components))
        if wildcard:
            if op not in ("==", "!="):
                raise RuntimeError("Invalid Requires-Python wildcard: " + requirement)
            equal = version[:len(components)] == components
            allowed = equal if op == "==" else not equal
        else:
            allowed = {">=": version >= wanted, "<=": version <= wanted,
                       ">": version > wanted, "<": version < wanted,
                       "==": version == wanted, "!=": version != wanted}[op]
        if not allowed:
            return False
    return True


def _wheel_tag_allowed(tag, profile):
    parts = tag.split("-")
    if len(parts) != 3:
        return False
    interpreters, abis, platforms = [part.split(".") for part in parts]
    target = profile["python_abi"]
    target_number = int(target[2:])
    for interpreter in interpreters:
        for abi in abis:
            if abi == "none" and interpreter in ("py3", target):
                python_ok = True
            elif abi == target and interpreter == target:
                python_ok = True
            elif abi == "abi3" and re.fullmatch(r"cp3\d+", interpreter):
                python_ok = int(interpreter[2:]) <= target_number
            else:
                python_ok = False
            if not python_ok:
                continue
            for platform in platforms:
                if platform == "any" and abi == "none":
                    return True
                match = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", platform)
                legacy = {"manylinux1_x86_64": (2, 5), "manylinux2010_x86_64": (2, 12),
                          "manylinux2014_x86_64": (2, 17)}
                baseline = tuple(map(int, match.groups())) if match else legacy.get(platform)
                if baseline and baseline <= GLIBC_BASELINE and abi != "none":
                    return True
    return False


def validate_vendor(directory, profile, expected_versions=None):
    directory = Path(directory)
    distributions = distribution_metadata(directory)
    versions = {name: value[0] for name, value in distributions.items()}
    if expected_versions is not None and versions != expected_versions:
        raise RuntimeError("Vendor distribution names/versions differ from reviewed source inputs")
    owners = {}
    native_owners = set()
    packages = []
    for name, (version, info, metadata) in distributions.items():
        requires_python = metadata.get("Requires-Python")
        if requires_python and not _python_allowed(requires_python, profile["python_minor"]):
            raise RuntimeError("Vendor Requires-Python rejects target: " + name)
        wheel, record = info / "WHEEL", info / "RECORD"
        if not wheel.is_file() or not record.is_file():
            raise RuntimeError("Missing vendor WHEEL/RECORD: " + name)
        wheel_metadata = Parser().parsestr(wheel.read_text(encoding="utf-8"))
        tags = wheel_metadata.get_all("Tag", [])
        if not tags or not any(_wheel_tag_allowed(tag, profile) for tag in tags):
            raise RuntimeError("Vendor wheel ABI/platform rejects target: " + name + " " + repr(tags))
        if (wheel_metadata.get("Root-Is-Purelib", "").lower() == "false"
                and any(_wheel_tag_allowed(tag, profile) and not tag.endswith("-any") for tag in tags)):
            native_owners.add(name)
        for row in csv.reader(record.read_text(encoding="utf-8").splitlines()):
            if row:
                owners.setdefault(row[0], []).append(name)
                relative = Path(row[0])
                if relative.is_absolute():
                    raise RuntimeError("Absolute vendor RECORD path: " + row[0])
                # Wheel entry-point scripts are not shipped in this module tree.
                if ".." in relative.parts or "bin" in relative.parts or "__pycache__" in relative.parts:
                    continue
                file = directory / relative
                if len(row) < 3 or not file.is_file():
                    raise RuntimeError("Missing or malformed vendor RECORD file: " + row[0])
                if not row[1] and file != record:
                    raise RuntimeError("Vendor RECORD digest missing: " + row[0])
                if row[1]:
                    algorithm, separator, encoded = row[1].partition("=")
                    wanted = base64.urlsafe_b64encode(hashlib.sha256(file.read_bytes()).digest()).decode().rstrip("=")
                    if algorithm != "sha256" or not separator or wanted != encoded:
                        raise RuntimeError("Vendor RECORD digest mismatch: " + row[0])
                if row[2] and int(row[2]) != file.stat().st_size:
                    raise RuntimeError("Vendor RECORD size mismatch: " + row[0])
        packages.append({"name": name, "version": version, "wheel_tags": tags})
    native = []
    for path in sorted(directory.rglob("*.so")):
        relative = path.relative_to(directory).as_posix()
        if len(owners.get(relative, [])) != 1:
            raise RuntimeError("Native vendor file is not owned by one RECORD: " + relative)
        if owners[relative][0] not in native_owners:
            raise RuntimeError("Native vendor file lacks matching native WHEEL metadata: " + relative)
        match = re.search(r"\.cpython-(\d+)-x86_64-linux-gnu\.so$", path.name)
        if match:
            if "cp" + match.group(1) != profile["python_abi"]:
                raise RuntimeError("Native vendor extension ABI rejects target: " + relative)
        elif not path.name.endswith(".abi3.so"):
            raise RuntimeError("Native vendor extension has unrecognized ABI suffix: " + relative)
        data = path.read_bytes()
        if len(data) < 20 or data[:6] != b"\x7fELF\x02\x01" or int.from_bytes(data[18:20], "little") != 62:
            raise RuntimeError("Native vendor extension is not ELF amd64: " + relative)
        needed = [tuple(map(int, values)) for values in re.findall(rb"GLIBC_(\d+)\.(\d+)", data)]
        if needed and max(needed) > GLIBC_BASELINE:
            raise RuntimeError("Native vendor extension exceeds glibc 2.34 baseline: " + relative)
        native.append(relative)
    required = ("_cffi_backend.cpython-", "pydantic_core/_pydantic_core.cpython-", "rpds/rpds.cpython-")
    if any(not any(path.startswith(prefix) for path in native) for prefix in required):
        raise RuntimeError("Required cffi/pydantic_core/rpds native vendor extensions missing")
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc" and "bin" not in path.relative_to(directory).parts and path.name != "FB-ABI-PREPARED.json":
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return {"vendor_sha256": digest.hexdigest(), "packages": packages, "native_extensions": native}
