#!/usr/bin/python3
"""Reproducible Debian package builder; never installs or starts the payload."""
import argparse
import ast
import calendar
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import shlex
import subprocess

from build_profiles import CANDIDATE_VERSION, PROFILES, SOURCE_VENDOR, VENDOR_RELATIVE, target_profile, validate_vendor, vendor_versions

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parent
VERSION = CANDIDATE_VERSION
PUBLIC_FILENAME = "FrameBridge-0.3.1.deb"
GPL_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
EPOCH = calendar.timegm((2026, 10, 27, 0, 0, 0))
PACKAGE = "framebridge-original-expanded"
RELEASE = BASE / "release"
STAGE = BASE / "build/package-root"
PREFIX = STAGE / "usr/lib/framebridge-expanded"

DEPENDENCY_SOURCE_ASSETS = [{'filename': 'FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz', 'bytes': 82606998, 'sha256': '64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a'}, {'filename': 'FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz', 'bytes': 26941745, 'sha256': '6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9'}]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# These are release inputs, not files inferred from installed runtime profiles.
LEGAL_FILES = ("LICENSE", "LICENSING.md", "COPYRIGHT.md", "LICENSE-STATUS.md", "OFFICIAL-RELEASES.md")
THIRD_PARTY_REQUIRED = ("THIRD-PARTY-NOTICES.md", "DEPENDENCY-SOURCES.json", "SOURCE-AVAILABILITY.md", "R2-SUPPLEMENTS.json", "NATIVE-DEPENDENCY-TERMS.json", "NATIVE-DEPENDENCY-TERMS.md", "TUNNEL-DEPENDENCY-SOURCES.json")
BLOG_POLICY_FILES = ("README.md", "LICENSING.md", "POST-TEMPLATE.md", "LICENSE-CC-BY-SA-4.0.txt")

def regular_files(directory):
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Legal input must not be a symbolic link: " + str(path.relative_to(ROOT)))
        if path.is_file():
            yield path

def legal_preflight():
    for relative in LEGAL_FILES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Required project legal input missing: " + relative)
    if sha(ROOT / "LICENSE") != GPL_SHA256:
        raise RuntimeError("The GPLv3 legal text differs from the recorded official text.")
    directory = ROOT / "licensing/third-party"
    for name in THIRD_PARTY_REQUIRED:
        path = directory / name
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise RuntimeError("Required third-party legal input missing: licensing/third-party/" + name)
    index = json.loads((directory / "DEPENDENCY-SOURCES.json").read_text())
    if not isinstance(index, (dict, list)):
        raise RuntimeError("Dependency source index must be a JSON object or array.")
    if not isinstance(index, dict) or index.get("archive") != DEPENDENCY_SOURCE_ASSETS[0]:
        raise RuntimeError("The immutable Python/Rust dependency source identity changed.")
    native = json.loads((directory / "TUNNEL-DEPENDENCY-SOURCES.json").read_text())
    expected = DEPENDENCY_SOURCE_ASSETS[1]
    if (native.get("archive"), native.get("size_bytes"), native.get("sha256")) != (expected["filename"], expected["bytes"], expected["sha256"]):
        raise RuntimeError("The tunnel dependency source identity changed.")
    licenses = directory / "licenses"
    if not licenses.is_dir() or not any(regular_files(licenses)):
        raise RuntimeError("Third-party license texts are missing.")
    supplement = licenses / "wit-bindgen-rt-0.39.0-LICENSE-MIT.txt"
    if not supplement.is_file() or sha(supplement) != "23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3":
        raise RuntimeError("The pinned wit-bindgen source notice supplement is missing or changed.")
    for name in BLOG_POLICY_FILES:
        if not (ROOT / "blog" / name).is_file():
            raise RuntimeError("Required referenced blog policy input missing: blog/" + name)
    # Do not accidentally ship local workstation paths in newly assembled notices.
    private_roots = (str(ROOT.resolve()), str(BASE.resolve()), str(Path.home()))
    inputs = [ROOT / name for name in LEGAL_FILES] + list(regular_files(ROOT / "licensing"))
    for path in inputs:
        data = path.read_bytes()
        for private_root in private_roots:
            if private_root.encode() in data:
                raise RuntimeError("Local path found in legal input: " + str(path.relative_to(ROOT)))
    return index

def install_legal_material(docs):
    for relative in LEGAL_FILES:
        shutil.copy2(ROOT / relative, docs / relative)
    for directory in ("licensing", "docs", "provenance"):
        source = ROOT / directory
        for path in regular_files(source):
            destination = docs / directory / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    (docs / "blog").mkdir()
    for name in BLOG_POLICY_FILES:
        shutil.copy2(ROOT / "blog" / name, docs / "blog" / name)
    shutil.copy2(ROOT / "RELEASE-NOTES.md", docs / "RELEASE-NOTES.md")
    (docs / "copyright").write_text(
        "FrameBridge Original Expanded — compatibility candidate " + VERSION + "\n\n"
        "Original FrameBridge material: GNU GPL version 3 only (GPL-3.0-only).\n"
        "The complete license is included in LICENSE. COPYRIGHT.md and LICENSING.md\n"
        "identify attribution, scope and third-party exceptions.\n\n"
        "Third-party material retains its applicable copyrights and terms.\n"
        "See licensing/third-party/THIRD-PARTY-NOTICES.md, retained native terms\n"
        "and the original notices alongside vendored components.\n\n"
        "Official release signatures and public readiness are recorded separately.\n")
    (docs / "THIRD-PARTY-NOTICES.md").write_text(
        "# Included third-party notices\n\n"
        "The assembled notices and license texts are included under\n"
        "[licensing/third-party](licensing/third-party/THIRD-PARTY-NOTICES.md).\n"
        "Original vendored notices are preserved with their components.\n")
    lines = ["# Source availability for FrameBridge " + VERSION, "",
             "Candidate source must be retained with its build identity; no public archive is asserted.", "",
             "The release source set also includes these separate assets:", ""]
    for item in DEPENDENCY_SOURCE_ASSETS:
        lines.extend(["- " + item["filename"],
                      "  Bytes: " + str(item["bytes"]),
                      "  SHA-256: " + item["sha256"]])
    lines.extend(["", "The Preview1 name deliberately identifies an immutable shared asset.",
                  "Retain the R2 source-notice supplement under licensing/third-party.",
                  "The tunnel dependency asset records exact dependency archives and",
                  "retained notices; it does not assert an independently rebuilt tunnel",
                  "executable or an identified exact own-source revision.", "",
                  "See licensing/third-party/SOURCE-AVAILABILITY.md and both dependency",
                  "source indices for scope and provenance. Publication readiness and",
                  "signature status are external release records, not builder claims.", ""])
    (docs / "SOURCE-AVAILABILITY.md").write_text("\n".join(lines))
    (docs / "COMMUNITY-RELEASE-STATUS.json").write_text(json.dumps({
        "schema_version": 1, "version": VERSION, "status": "COMPATIBILITY_CANDIDATE_BUILD",
        "certified_as_RC7": False,
        "public_readiness": "EXTERNAL_RELEASE_RECORD",
        "signature_status": "EXTERNAL_RELEASE_RECORD",
        "vm_lifecycle_validation": "NOT_ASSERTED_BY_BUILDER",
        "source_archive": "CANDIDATE_SOURCE_REQUIRED_BEFORE_PUBLICATION",
        "dependency_source_archives": DEPENDENCY_SOURCE_ASSETS
    }, indent=2) + "\n")

def maintainer_prerm(version):
    """Revoke helper grants before a downgrade removes the helper's code.

    Equal-version reinstalls and forward upgrades retain managed credentials.
    The fixed installed cleanup module validates the helper's root ownership,
    permissions and ancestors before running its bounded purge-all command.
    """
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+:~\-]*", version):
        raise ValueError("Invalid Debian package version for maintainer script")
    helper_cleanup = (
        'import runpy; runpy.run_path('
        '"/usr/lib/framebridge-expanded/expanded/framebridge_expanded/uninstall.py")'
        '["purge_credential_helpers"]()'
    )
    return (
        "#!/bin/sh\nset -eu\n"
        'case "$1" in\n'
        "  remove|deconfigure)\n"
        "    /usr/bin/python3 -I -S /usr/lib/framebridge-expanded/expanded/framebridge_expanded/uninstall.py --all-users\n"
        "    if [ -e /etc/framebridge-expanded/screen-access.json ] || [ -L /etc/framebridge-expanded/screen-access.json ]; then\n"
        "      /usr/lib/framebridge-expanded/bin/framebridge-screen-access disable\n"
        "    fi\n"
        "    ;;\n"
        "  upgrade)\n"
        '    /usr/bin/dpkg --validate-version "$2"\n'
        '    if /usr/bin/dpkg --compare-versions "$2" lt ' + shlex.quote(version) + "; then\n"
        "      /usr/bin/python3 -I -S -c " + shlex.quote(helper_cleanup) + "\n"
        "    fi\n"
        "    ;;\nesac\n"
    )


def package_relationships(profile):
    """Keep the opt-in virtual window manager out of automatic APT installs."""
    return {
        "Depends": "python3 (>= " + profile["python_minor"] + "), python3 (<< 3." + str(int(profile["python_minor"].split(".")[1]) + 1) + "), libc6 (>= 2.34), libgcc-s1, python3-cryptography, sudo, libpam-systemd, dbus-user-session, udev, libx11-6, ca-certificates, iproute2, procps, systemd, bash, tmux, coreutils, util-linux, mawk | gawk, gnome-terminal, wmctrl, xdotool, x11-utils, xclip, imagemagick, xterm, dbus-x11, xauth, x11-xkb-utils, fonts-dejavu-core",
        "Suggests": "xvfb, openbox",
    }


def main(argv=None):
    global VERSION, PUBLIC_FILENAME, RELEASE, STAGE, PREFIX
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=sorted(PROFILES))
    parser.add_argument("--version", default=CANDIDATE_VERSION)
    parser.add_argument("--vendor-dir", type=Path, help="Reviewed target-ABI Fleet Hub vendor tree; never installed globally")
    parser.add_argument("--check-vendor", action="store_true", help="Validate inputs without staging or building a package")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+:~\-]*", args.version) or args.version == "0.3.1":
        parser.error("A distinct valid Debian candidate version is required")
    subprocess.run(["dpkg", "--validate-version", args.version], check=True)
    VERSION = args.version
    version_assignments = [node for node in ast.parse((BASE / "expanded/framebridge_expanded/__init__.py").read_text()).body
                           if isinstance(node, ast.Assign) and any(isinstance(item, ast.Name) and item.id == "__version__" for item in node.targets)]
    if len(version_assignments) != 1 or ast.literal_eval(version_assignments[0].value) != VERSION:
        raise RuntimeError("Candidate package version differs from application __version__")
    profile = target_profile(args.target)
    profile["package_version"] = VERSION
    original_vendor = BASE / SOURCE_VENDOR
    vendor = args.vendor_dir.resolve() if args.vendor_dir else original_vendor
    if args.vendor_dir is None and profile["python_abi"] != "cp313":
        parser.error("This target requires --vendor-dir with reviewed " + profile["python_abi"] + " wheels")
    vendor_evidence = validate_vendor(vendor, profile, vendor_versions(original_vendor))
    profile["vendor"] = vendor_evidence
    PUBLIC_FILENAME = "FrameBridge-" + VERSION + "-" + args.target + ".deb"
    RELEASE = BASE / "release" / args.target
    STAGE = BASE / "build" / args.target / "package-root"
    PREFIX = STAGE / "usr/lib/framebridge-expanded"
    legal_preflight()
    baseline = json.loads((BASE / "evidence/BASELINE.json").read_text())
    for relative, expected in baseline["source_hashes"].items():
        if sha(BASE / "sources" / relative) != expected:
            raise RuntimeError("Original source changed: " + relative)
    if args.check_vendor:
        print(json.dumps(profile, sort_keys=True))
        return
    if STAGE.exists():
        shutil.rmtree(STAGE)
    PREFIX.mkdir(parents=True)
    RELEASE.mkdir(parents=True, exist_ok=True)
    for name, dest in [("original-asus", "asus"), ("original-geranas", "geranas"), ("original-gerameg", "gerameg")]:
        source = BASE / "sources" / name
        if not source.is_dir():
            raise RuntimeError("Required public source snapshot missing: " + name)
        shutil.copytree(source, PREFIX / "original" / dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    staged_vendor = PREFIX / VENDOR_RELATIVE
    shutil.rmtree(staged_vendor)
    shutil.copytree(vendor, staged_vendor, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "bin", "FB-ABI-PREPARED.json"))
    if validate_vendor(staged_vendor, profile, vendor_versions(original_vendor)) != vendor_evidence:
        raise RuntimeError("Staged vendor tree differs from reviewed input")
    (PREFIX / "release-profile.json").write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    for source in ("portable", "expanded", "docs"):
        shutil.copytree(BASE / source, PREFIX / source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (PREFIX / "bin").mkdir()
    for name in ("framebridge", "framebridge-original", "framebridge-native-expanded", "framebridge-screen-access", "framebridge-credential-helper"):
        shutil.copy2(BASE / "packaging" / name, PREFIX / "bin" / name)
        (PREFIX / "bin" / name).chmod(0o755)
    rules = STAGE / "usr/lib/udev/rules.d"
    rules.mkdir(parents=True)
    shutil.copy2(BASE / "packaging/70-framebridge-screen.rules", rules / "70-framebridge-screen.rules")
    layout = {
        "adapter": "original/asus/tool-adapter/bin/framebridge_tool_adapter.py",
        "adapter_bin": "original/asus/tool-adapter/bin",
        "fbterm_bin": "original/asus/fbterm/bin",
        "native_host": "original/asus/framebridge-native/host/framebridge_host.py",
        "native_host_dir": "original/asus/framebridge-native/host"
    }
    (PREFIX / "layout.json").write_text(json.dumps(layout, indent=2) + "\n")
    bindir = STAGE / "usr/bin"
    bindir.mkdir(parents=True)
    for name in ("framebridge", "fb", "framebridge-expanded"):
        (bindir / name).symlink_to("../lib/framebridge-expanded/bin/framebridge")
    applications = STAGE / "usr/share/applications"
    applications.mkdir(parents=True)
    (applications / "framebridge-console.desktop").write_text(
        "[Desktop Entry]\nVersion=1.0\nType=Application\nName=FrameBridge Console\n"
        "Comment=Open the persistent local Bash and FBTERM activity console\n"
        "Exec=/usr/bin/fb console\nIcon=utilities-terminal\nTerminal=false\n"
        "Categories=System;TerminalEmulator;\nStartupWMClass=Gnome-terminal\n")
    docs = STAGE / "usr/share/doc" / PACKAGE
    docs.mkdir(parents=True)
    for file in sorted((BASE / "docs").glob("*")):
        if file.is_file():
            shutil.copy2(file, docs / file.name)
    # Ship the same owner/agent entry points as the reviewed source repository.
    user_guides = {"README.md": ROOT / "README.md",
                   "FRAMEBRIDGE-AGENT-START.md": ROOT / "docs/FRAMEBRIDGE-AGENT-START.md"}
    for name, source in user_guides.items():
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("Required user guide is missing or not a regular file: " + str(source))
        for destination in (PREFIX / "docs", docs):
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination / name)
    readme = (BASE / "docs/DEBIAN-TUTORIAL.md").read_bytes()
    (docs / "README.Debian.gz").write_bytes(gzip.compress(readme, mtime=EPOCH))
    install_legal_material(docs)
    man = STAGE / "usr/share/man/man1"
    man.mkdir(parents=True)
    man_source = (BASE / "docs/framebridge.1").read_bytes() if (BASE / "docs/framebridge.1").exists() else b".TH FRAMEBRIDGE 1\n.SH NAME\nframebridge - FrameBridge Original Expanded\n.SH SYNOPSIS\nframebridge [--state-dir PATH] setup|start|stop|status|doctor\n.SH DESCRIPTION\nRun framebridge tutorial for the Debian installation and usage guide.\n"
    (man / "framebridge.1.gz").write_bytes(gzip.compress(man_source, mtime=EPOCH))
    (man / "fb.1.gz").symlink_to("framebridge.1.gz")
    control = STAGE / "DEBIAN"
    control.mkdir()
    installed = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file() and not p.is_symlink())
    relationships = package_relationships(profile)
    (control / "control").write_text(
        "Package: " + PACKAGE + "\nVersion: " + VERSION + "\nSection: utils\nPriority: optional\nArchitecture: amd64\n"
        "Maintainer: FrameBridge <framebridge@localhost>\n"
        "Homepage: https://framebridge.betancourt.one/\n"
        "Installed-Size: " + str((installed + 1023) // 1024) + "\n"
        "Depends: " + relationships["Depends"] + "\n"
        "Suggests: " + relationships["Suggests"] + "\n"
        "Description: FrameBridge with standalone, central and node modes\n"
        " Original execution and desktop programs, a real persistent terminal,\n"
        " authenticated node enrollment, and a manual ChatGPT tunnel wizard.\n"
        " Includes real active-screen capture, saved original PNG viewing,\n"
        " per-console sudo capability, explicit read-only framebuffer access,\n"
        " a shared graphical/text console, and an English Debian tutorial.\n"
        " Optional virtual displays are explicitly identified as virtual.\n"
        " No phone or Owner Device pairing. Services start after connection setup.\n"
        " Installing the package alone does not activate a connection.\n")
    (control / "prerm").write_text(maintainer_prerm(VERSION))
    (control / "prerm").chmod(0o755)
    rows = []
    for path in sorted(STAGE.rglob("*")):
        if path.is_file() and not path.is_symlink() and control not in path.parents:
            rows.append(hashlib.md5(path.read_bytes()).hexdigest() + "  " + str(path.relative_to(STAGE)))
    (control / "md5sums").write_text("\n".join(rows) + "\n")
    for path in STAGE.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        elif os.access(path, os.X_OK):
            path.chmod(0o755)
        else:
            path.chmod(0o644)
        os.utime(path, (EPOCH, EPOCH), follow_symlinks=False)
    manifest = [{"path": str(p.relative_to(STAGE)), "sha256": sha(p), "bytes": p.stat().st_size}
                for p in sorted(STAGE.rglob("*")) if p.is_file() and not p.is_symlink()]
    (RELEASE / "RELEASE-PROFILE.json").write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    (RELEASE / "PACKAGE-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    output = RELEASE / ("framebridge-expanded_" + VERSION + "_amd64.deb")
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(EPOCH))
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "-z6", "--build", str(STAGE), str(output)], check=True, env=env)
    shutil.copy2(output, RELEASE / PUBLIC_FILENAME)
    print(json.dumps({"package": str(output), "bytes": output.stat().st_size, "sha256": sha(output), "payload_entries": len(manifest), "profile_id": args.target, "validation_status": "candidate", "public_filename": PUBLIC_FILENAME, "status": "COMPATIBILITY_CANDIDATE_BUILD"}))

if __name__ == "__main__":
    main()
