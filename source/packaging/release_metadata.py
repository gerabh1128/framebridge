"""Bind release packages to their corresponding source; never certify runtime QA."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


PUBLIC_RELEASE_PROFILES = (
    "debian13-amd64-py313", "ubuntu24.04-amd64-py312", "mint22-amd64-py312",
)


def release_source(manifest_path, version, profile_id, current_files, vendor_evidence):
    """Require the same reviewed source and ABI input used by this package.

    The manifest and archive are release inputs kept beside one another. Their
    signatures and the OS acceptance record are verified outside the builder.
    """
    if profile_id not in PUBLIC_RELEASE_PROFILES or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("Public release requires a stable version and an official release profile")
    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text())
    expected_name = "FrameBridge-" + version + "-source.tar.gz"
    if (data.get("version"), data.get("status"), data.get("archive")) != (version, "RELEASE_SOURCE_ASSEMBLED", expected_name):
        raise RuntimeError("Matching release source manifest is required")
    archive = manifest_path.parent / expected_name
    if archive.is_symlink() or not archive.is_file():
        raise RuntimeError("Matching release source archive is missing")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if archive.stat().st_size != data.get("bytes") or digest != data.get("sha256"):
        raise RuntimeError("Release source archive hash or size differs from its manifest")
    vendors = data.get("vendor_inputs", {}).get("vendors", {})
    if set(vendors) != {"cp312", "cp313"}:
        raise RuntimeError("Release source must contain the reviewed cp312 and cp313 inputs")
    abi = "cp313" if profile_id == "debian13-amd64-py313" else "cp312"
    if vendors[abi].get("vendor_sha256") != vendor_evidence.get("vendor_sha256"):
        raise RuntimeError("Package vendor input differs from corresponding source")
    rows = data.get("files", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("Invalid release source file manifest")
    recorded = {row.get("path"): row.get("sha256") for row in rows}
    if len(recorded) != len(rows):
        raise RuntimeError("Duplicate release source manifest path")
    seen = set()
    prefix = "FrameBridge-" + version + "/"
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            if not member.isfile() or not member.name.startswith(prefix):
                raise RuntimeError("Unexpected release source archive member")
            relative = member.name[len(prefix):]
            if not relative or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise RuntimeError("Unsafe release source archive path")
            if relative in seen or relative not in recorded:
                raise RuntimeError("Duplicate or unrecorded release source archive member")
            seen.add(relative)
            with source.extractfile(member) as stream:
                if hashlib.sha256(stream.read()).hexdigest() != recorded[relative]:
                    raise RuntimeError("Release source archive content differs from its manifest")
    if seen != set(recorded):
        raise RuntimeError("Release source manifest contains files absent from the archive")
    # Bind the ABI claims to the archived bytes, including the ABI not selected
    # by this particular package. A manifest field alone is not vendor evidence.
    for vendor_abi, expected_prefix in (
        ("cp313", "source/sources/original-geranas/FrameBridge-Fleet-Hub/vendor"),
        ("cp312", "source/target-vendors/cp312"),
    ):
        if vendors[vendor_abi].get("path") != expected_prefix:
            raise RuntimeError("Unexpected corresponding-source vendor path")
        vendor_digest = hashlib.sha256()
        # validate_vendor sorts Path objects, whose component ordering differs
        # from slash-delimited strings for package/ versus package-1.dist-info/.
        for path, file_digest in sorted(recorded.items(), key=lambda row: PurePosixPath(row[0]).parts):
            if not path.startswith(expected_prefix + "/"):
                continue
            relative = path[len(expected_prefix) + 1:]
            parts = PurePosixPath(relative).parts
            if "__pycache__" in parts or "bin" in parts or relative.endswith(".pyc") or parts[-1] == "FB-ABI-PREPARED.json":
                continue
            vendor_digest.update(relative.encode() + b"\0")
            vendor_digest.update(bytes.fromhex(file_digest))
        if vendor_digest.hexdigest() != vendors[vendor_abi].get("vendor_sha256"):
            raise RuntimeError("Archived vendor bytes differ from their recorded ABI identity")
    for relative, path in current_files.items():
        if recorded.get(relative) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise RuntimeError("Package source differs from corresponding archive: " + relative)
    return {"filename": expected_name, "bytes": data["bytes"], "sha256": digest}
