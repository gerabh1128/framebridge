#!/usr/bin/python3
"""Archive the reviewed community source layout without runtime profiles or QA logs."""
import calendar
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parent
EPOCH = calendar.timegm((2026, 10, 26, 0, 0, 0))
ARCHIVE_ROOT = "FrameBridge-0.3.1"
ARCHIVE_NAME = "FrameBridge-0.3.1-source.tar.gz"
SOURCE_FOLDERS = ("sources", "expanded", "portable", "packaging", "docs", "tests")
ROOT_FILES = ("README.md", "LICENSE", "LICENSING.md", "COPYRIGHT.md",
              "LICENSE-STATUS.md", "OFFICIAL-RELEASES.md", "CONTRIBUTING.md", "SECURITY.md", "RELEASE-NOTES.md")
ROOT_FOLDERS = ("docs", "provenance", "licensing")
BLOG_POLICY_FILES = ("README.md", "LICENSING.md", "POST-TEMPLATE.md", "LICENSE-CC-BY-SA-4.0.txt")
ARCHIVE_README_NAME = "README-SOURCE-ARCHIVE.md"
DEPENDENCY_SOURCE_ASSETS = [{'filename': 'FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz', 'bytes': 82606998, 'sha256': '64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a'}, {'filename': 'FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz', 'bytes': 26941745, 'sha256': '6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9'}]
ARCHIVE_README = "# FrameBridge 0.3.1 source archive\n\nThis archive preserves the repository layout needed by the offline builder.\nFrom this directory, run python3 source/packaging/build_expanded.py to build\nFrameBridge-0.3.1.deb. The builder writes source/build and source/release;\nit does not install or start FrameBridge. Debian's dpkg-deb and the documented\nbuild prerequisites must already be available.\n\nOriginal FrameBridge material is GNU GPL version 3 only. Read LICENSE,\nLICENSING.md, COPYRIGHT.md, LICENSE-STATUS.md and OFFICIAL-RELEASES.md for\nscope, attribution, third-party terms and the official-release trust model.\n\nThe project source asset is accompanied by these separate dependency assets:\n\n- FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz\n  82,606,998 bytes\n  SHA-256 64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a\n- FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz\n  26,941,745 bytes\n  SHA-256 6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9\n\nThe Preview1 filename deliberately preserves the identity of an immutable\nshared asset. Retain licensing/third-party/R2-SUPPLEMENTS.json and its referenced\nsource notices with the dependency sources. The source indices and\nSOURCE-AVAILABILITY.md there record scope and provenance. Do not substitute\nunrelated archives. The retained tunnel executable is not claimed to have been\nindependently rebuilt from an identified own-source revision.\n\nThe blog directory contains licensing policies and the post template.\nEditorial article drafts are omitted because they are not source build inputs.\n\nOnly source/evidence/BASELINE.json is included from technical evidence. Runtime\nprofiles, private QA logs, generated build/release files and Git history are\noutside the source archive allowlist.\n\nA completed archive or builder run is not a declaration of public readiness.\nRelease checksums, signatures and publication status are recorded separately.\n".encode("utf-8")
GPL_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def source_files():
    selected = {}

    def add(path):
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            raise RuntimeError("Source archives must not include symbolic links: " + str(relative))
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
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

def main():
    names = source_files()
    release = BASE / "release"
    release.mkdir(exist_ok=True)
    output = release / ARCHIVE_NAME
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                           mtime=EPOCH, compresslevel=6) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive:
                info = tarfile.TarInfo(ARCHIVE_ROOT + "/" + ARCHIVE_README_NAME)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = EPOCH
                info.mode = 0o644
                info.size = len(ARCHIVE_README)
                archive.addfile(info, io.BytesIO(ARCHIVE_README))
                for relative, path in sorted(names.items()):
                    info = archive.gettarinfo(str(path), arcname=ARCHIVE_ROOT + "/" + relative)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = EPOCH
                    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
    manifest = {"status": "COMMUNITY_RELEASE_BUILD", "archive": output.name,
                "bytes": output.stat().st_size, "sha256": sha(output),
                "files": [{"path": ARCHIVE_README_NAME,
                           "sha256": hashlib.sha256(ARCHIVE_README).hexdigest(), "generated": True}]
                         + [{"path": relative, "sha256": sha(path)}
                            for relative, path in sorted(names.items())],
                "draft_blog_articles_included": False,
                "evidence_allowlist": ["source/evidence/BASELINE.json"],
                "dependency_sources": DEPENDENCY_SOURCE_ASSETS[0]["filename"],
                "dependency_source_archives": DEPENDENCY_SOURCE_ASSETS}
    (release / "SOURCE-ARCHIVE-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"archive": str(output), "bytes": output.stat().st_size,
                      "sha256": manifest["sha256"], "files": len(names) + 1,
                      "status": manifest["status"]}))

if __name__ == "__main__":
    main()
