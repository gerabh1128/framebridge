#!/usr/bin/python3
"""Archive the reviewed community source layout without runtime profiles or QA logs."""
import argparse
import calendar
import gzip
import hashlib
import io
import json
import re
from pathlib import Path
import tarfile
import sys

from build_profiles import CANDIDATE_VERSION, SOURCE_VENDOR, target_profile, validate_vendor, vendor_versions
from release_metadata import PUBLIC_RELEASE_PROFILES

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parent
EPOCH = calendar.timegm((2026, 10, 27, 0, 0, 0))
ARCHIVE_ROOT = "FrameBridge-" + CANDIDATE_VERSION
ARCHIVE_NAME = ARCHIVE_ROOT + "-source.tar.gz"
SOURCE_FOLDERS = ("sources", "expanded", "portable", "packaging", "docs", "tests")
ROOT_FILES = ("README.md", "LICENSE", "LICENSING.md", "COPYRIGHT.md",
              "LICENSE-STATUS.md", "OFFICIAL-RELEASES.md", "CONTRIBUTING.md", "SECURITY.md", "RELEASE-NOTES.md")
ROOT_FOLDERS = ("docs", "provenance", "licensing")
BLOG_POLICY_FILES = ("README.md", "LICENSING.md", "POST-TEMPLATE.md", "LICENSE-CC-BY-SA-4.0.txt")
ARCHIVE_README_NAME = "README-SOURCE-ARCHIVE.md"
DEPENDENCY_SOURCE_ASSETS = [{'filename': 'FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz', 'bytes': 82606998, 'sha256': '64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a'}, {'filename': 'FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz', 'bytes': 26941745, 'sha256': '6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9'}]
ARCHIVE_README = f"# FrameBridge {CANDIDATE_VERSION} compatibility candidate source\n\nThis source archive is a QA candidate, not a signed public release or evidence\nthat an OS/desktop lifecycle has been tested. Original signed 0.3.1 assets retain\ntheir separate immutable identity. Check TARGET-VENDOR-PROVENANCE.json for the\nnative ABI inputs actually included in this archive.\n\nThe offline builder requires Python 3, Debian dpkg/dpkg-deb and documented host\nbuild prerequisites. It writes source/build/<profile> and source/release/<profile>;\nit never installs a package, fetches dependencies, changes system Python or starts\nFrameBridge services. Run from this archive's top-level directory:\n\n    python3 source/packaging/build_expanded.py --target debian13-amd64-py313\n    python3 source/packaging/build_expanded.py --target lmde7-amd64-py313\n\nIf the cp312 input is recorded as included:\n\n    python3 source/packaging/build_expanded.py --target ubuntu24.04-amd64-py312 --vendor-dir source/target-vendors/cp312\n    python3 source/packaging/build_expanded.py --target mint22-amd64-py312 --vendor-dir source/target-vendors/cp312\n\nIf the cp314 input is recorded as included:\n\n    python3 source/packaging/build_expanded.py --target ubuntu26.04-amd64-py314 --vendor-dir source/target-vendors/cp314\n\nThe default archive includes only the reviewed original cp313 vendor tree.\nThe archive producer can include reviewed alternate ABI trees using:\n\n    python3 source/packaging/archive_sources.py --vendor-cp312 /reviewed/vendor-cp312 --vendor-cp314 /reviewed/vendor-cp314\n\nThose arguments must identify complete reviewed trees with the same 29 pinned\ndistribution names/versions as the original. The producer validates WHEEL/RECORD,\nCPython ABI, amd64 ELF and glibc baseline before including them. Local preparation\nmarkers and executable entry-point scripts are omitted. All included files and\ntheir hashes appear in SOURCE-ARCHIVE-MANIFEST.json alongside the finished archive;\nthe embedded provenance records input ABI identity without workstation paths.\n--check-vendor on build_expanded.py validates inputs without building a package.\nAn absent alternate tree must be supplied separately with equivalent provenance;\nchanging Depends cannot convert the original cp313 extensions into cp312/cp314.\n\nPackage output names contain the candidate version and a separate profile\nfolder. release-profile.json inside the installed prefix declares the exact\ncandidate profile. Build success and profile eligibility do not establish a\nvalidated installation, graphical control, real TTY or reboot lifecycle.\n\nOriginal FrameBridge material is GNU GPL version 3 only. Read LICENSE,\nLICENSING.md, COPYRIGHT.md, LICENSE-STATUS.md and OFFICIAL-RELEASES.md for scope,\nattribution, third-party terms and the official-release trust model.\n\nThe project source asset is accompanied by these separate unchanged dependency\nsource assets; they are not embedded or renamed by this helper:\n\n- FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz\n  82,606,998 bytes\n  SHA-256 64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a\n- FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz\n  26,941,745 bytes\n  SHA-256 6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9\n\nThe Preview1 name preserves an immutable shared asset's identity. Retain the R2\nsource-notice supplements and both dependency indices under licensing/third-party.\nMatching package versions do not constitute an independent source rebuild of the\nretained tunnel executable; its exact own-source revision is not asserted.\n\nThe blog directory contains licensing policies and the post template. Draft\narticles, private runtime profiles, QA logs, build outputs and Git history are\noutside the source allowlist. Only source/evidence/BASELINE.json is included from\ntechnical evidence. Release signatures and publication status are separate\nrecords; this candidate archiver does not sign or publish anything.\n".encode("utf-8")
PUBLIC_ARCHIVE_README = f"""# FrameBridge {CANDIDATE_VERSION} corresponding source

This archive supplies the project source and the reviewed cp313 and cp312 vendor
inputs for Debian 13, Ubuntu 24.04 and Linux Mint 22.x on amd64. Keep its matching
SOURCE-ARCHIVE-MANIFEST.json sidecar beside the archive. Signatures, publication
and actual OS lifecycle acceptance are recorded separately; assembly alone does
not certify them. Historical signed assets retain their immutable identities.

The offline builder needs Python 3, dpkg/dpkg-deb and the documented host build
prerequisites. It never fetches dependencies, installs packages or starts services.
Extract the archive beside its matching sidecar and run from the extracted root:

    python3 source/packaging/build_expanded.py --target debian13-amd64-py313 --public-release --source-manifest ../SOURCE-ARCHIVE-MANIFEST.json
    python3 source/packaging/build_expanded.py --target ubuntu24.04-amd64-py312 --vendor-dir source/target-vendors/cp312 --public-release --source-manifest ../SOURCE-ARCHIVE-MANIFEST.json
    python3 source/packaging/build_expanded.py --target mint22-amd64-py312 --vendor-dir source/target-vendors/cp312 --public-release --source-manifest ../SOURCE-ARCHIVE-MANIFEST.json

Public-release mode verifies the archive hash, every current project source file
and the selected reviewed vendor identity before assembling a package. It does
not assert an independent rebuild of third-party native wheels or tunnel-client.
--check-vendor validates inputs without staging a package.

Original FrameBridge material is GNU GPL version 3 only. Third-party material
retains its applicable terms. Read LICENSE, LICENSING.md, COPYRIGHT.md and the
source-delivery records. Distribute these separate, unchanged dependency assets
with the release; their historical names do not identify the installer version:

- FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz
  Bytes: 82606998
  SHA-256: 64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a
- FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz
  Bytes: 26941745
  SHA-256: 6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9

The exact own-source revision of the retained tunnel executable is not asserted.
Retain the R2 source-notice supplements, both dependency indices and original
notices. Private profiles, credentials, QA logs, build outputs, Git history and
unpublished blog drafts are excluded from the archive allowlist.
""".encode("utf-8")

GPL_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def source_files():
    selected = {}

    def add(path):
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            raise RuntimeError("Source archives must not include symbolic links: " + str(relative))
        if any(part in {"__pycache__", ".git", "build", "release", "qa"} for part in relative.parts) or path.suffix in {".pyc", ".log"}:
            return
        if path.is_file():
            selected[relative.as_posix()] = path

    for folder in SOURCE_FOLDERS:
        directory = BASE / folder
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError("Required source directory missing: " + folder)
        for path in directory.rglob("*"):
            add(path)
    # The builder needs this input; no other evidence, QA log, or diff is selected.
    baseline = BASE / "evidence/BASELINE.json"
    if not baseline.is_file():
        raise RuntimeError("Required build baseline missing.")
    add(baseline)
    for name in ROOT_FILES:
        path = ROOT / name
        if not path.is_file():
            raise RuntimeError("Required repository input missing: " + name)
        add(path)
    for name in ROOT_FOLDERS:
        directory = ROOT / name
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError("Required repository directory missing: " + name)
        for path in directory.rglob("*"):
            add(path)
    # Preserve policy references, but do not publish draft blog articles implicitly.
    for name in BLOG_POLICY_FILES:
        path = ROOT / "blog" / name
        if not path.is_file():
            raise RuntimeError("Required blog licensing policy input missing: " + name)
        add(path)
    if sha(ROOT / "LICENSE") != GPL_SHA256:
        raise RuntimeError("The GPLv3 text differs from the recorded official license.")
    required_notices = ROOT / "licensing/third-party"
    for name in ("THIRD-PARTY-NOTICES.md", "DEPENDENCY-SOURCES.json", "SOURCE-AVAILABILITY.md", "R2-SUPPLEMENTS.json", "NATIVE-DEPENDENCY-TERMS.json", "NATIVE-DEPENDENCY-TERMS.md", "TUNNEL-DEPENDENCY-SOURCES.json"):
        path = required_notices / name
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError("Required third-party source/notice input missing: " + name)
    return selected

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-release", action="store_true", help="Assemble corresponding source for all three official release profiles")
    parser.add_argument("--vendor-cp312", type=Path, help="Reviewed cp312 vendor input to include")
    parser.add_argument("--vendor-cp314", type=Path, help="Reviewed cp314 vendor input to include")
    args = parser.parse_args(argv)
    if args.public_release and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", CANDIDATE_VERSION):
        parser.error("--public-release requires a stable project version")
    if args.public_release and (args.vendor_cp312 is None or args.vendor_cp314 is not None):
        parser.error("--public-release requires --vendor-cp312 and excludes experimental cp314 input")
    names = source_files()
    expected_versions = vendor_versions(BASE / SOURCE_VENDOR)
    provenance = {"schema": 1, "version": CANDIDATE_VERSION, "validation_status": "release_source" if args.public_release else "candidate",
                  "release_profiles": list(PUBLIC_RELEASE_PROFILES) if args.public_release else [],
                  "vendors": {"cp313": {"path": "source/" + SOURCE_VENDOR.as_posix(),
                                        **validate_vendor(BASE / SOURCE_VENDOR, target_profile("debian13-amd64-py313"), expected_versions)}}}
    for abi, directory, profile_id in (("cp312", args.vendor_cp312, "ubuntu24.04-amd64-py312"),
                                       ("cp314", args.vendor_cp314, "ubuntu26.04-amd64-py314")):
        if directory is None:
            continue
        evidence = validate_vendor(directory, target_profile(profile_id), expected_versions)
        archive_path = "source/target-vendors/" + abi
        provenance["vendors"][abi] = {"path": archive_path, **evidence}
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if path.is_symlink():
                raise RuntimeError("Alternate vendor source symlink rejected: " + str(relative))
            if (not path.is_file() or any(part in {"__pycache__", ".git", "build", "release", "qa", "bin"} for part in relative.parts)
                    or path.suffix in {".pyc", ".log"} or path.name == "FB-ABI-PREPARED.json"):
                continue
            name = archive_path + "/" + relative.as_posix()
            if name in names:
                raise RuntimeError("Archive input path collision: " + name)
            names[name] = path
    provenance["omitted_vendor_abis"] = sorted({"cp312", "cp314"} - set(provenance["vendors"]))
    if provenance["omitted_vendor_abis"] and not args.public_release:
        print("Candidate source excludes alternate vendor ABIs: " + ", ".join(provenance["omitted_vendor_abis"]), file=sys.stderr)
    generated = {ARCHIVE_README_NAME: PUBLIC_ARCHIVE_README if args.public_release else ARCHIVE_README,
                 "TARGET-VENDOR-PROVENANCE.json": (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()}
    release = BASE / "release" / ("public-source" if args.public_release else "candidate-source")
    release.mkdir(parents=True, exist_ok=True)
    output = release / ARCHIVE_NAME
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                           mtime=EPOCH, compresslevel=6) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for relative, data in sorted(generated.items()):
                    info = tarfile.TarInfo(ARCHIVE_ROOT + "/" + relative)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = EPOCH
                    info.mode = 0o644
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                for relative, path in sorted(names.items()):
                    info = archive.gettarinfo(str(path), arcname=ARCHIVE_ROOT + "/" + relative)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = EPOCH
                    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
    manifest = {"status": "RELEASE_SOURCE_ASSEMBLED" if args.public_release else "COMPATIBILITY_CANDIDATE_BUILD", "version": CANDIDATE_VERSION, "vendor_inputs": provenance, "archive": output.name,
                "bytes": output.stat().st_size, "sha256": sha(output),
                "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "generated": True}
                          for name, data in sorted(generated.items())]
                         + [{"path": relative, "sha256": sha(path)}
                            for relative, path in sorted(names.items())],
                "draft_blog_articles_included": False,
                "evidence_allowlist": ["source/evidence/BASELINE.json"],
                "dependency_sources": DEPENDENCY_SOURCE_ASSETS[0]["filename"],
                "dependency_source_archives": DEPENDENCY_SOURCE_ASSETS}
    (release / "SOURCE-ARCHIVE-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"archive": str(output), "bytes": output.stat().st_size,
                      "sha256": manifest["sha256"], "files": len(names) + len(generated),
                      "status": manifest["status"]}))

if __name__ == "__main__":
    main()
