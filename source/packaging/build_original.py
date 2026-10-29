#!/usr/bin/python3
"""Build the original snapshot on GERAMEG; does not install or start software."""
import calendar
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

BASE = Path(__file__).resolve().parents[1]
VERSION = "0.1.0~original.20261021.1"
EPOCH = calendar.timegm((2026, 10, 21, 0, 0, 0))
RELEASE = BASE / "release"
STAGE = BASE / "build/package-root"
PREFIX = STAGE / "usr/lib/framebridge-original"

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    assert (BASE / "portable/front_door_mcp.py").is_file(), "portable source missing"
    assert (BASE / "sources/original-asus/SOURCE-MANIFEST.json").is_file(), "ASUS snapshot missing"
    assert (BASE / "sources/original-geranas/MANIFEST.json").is_file(), "GERANAS snapshot missing"
    if STAGE.exists():
        shutil.rmtree(STAGE)
    RELEASE.mkdir(exist_ok=True)
    PREFIX.mkdir(parents=True)
    shutil.copytree(BASE / "sources/original-geranas", PREFIX / "original/geranas")
    shutil.copytree(BASE / "sources/original-asus", PREFIX / "original/asus")
    shutil.copytree(BASE / "portable", PREFIX / "portable", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (PREFIX / "bin").mkdir()
    shutil.copy2(BASE / "packaging/framebridge-original", PREFIX / "bin/framebridge-original")
    (PREFIX / "bin/framebridge-original").chmod(0o755)
    layout = {
        "adapter": "original/asus/tool-adapter/bin/framebridge_tool_adapter.py",
        "adapter_bin": "original/asus/tool-adapter/bin",
        "fbterm_bin": "original/asus/fbterm/bin",
        "native_host": "original/asus/framebridge-native/host/framebridge_host.py",
        "native_host_dir": "original/asus/framebridge-native/host",
    }
    (PREFIX / "layout.json").write_text(json.dumps(layout, indent=2) + "\n")
    bindir = STAGE / "usr/bin"
    bindir.mkdir()
    (bindir / "framebridge-original").symlink_to("../lib/framebridge-original/bin/framebridge-original")
    docs = STAGE / "usr/share/doc/framebridge-original"
    docs.mkdir(parents=True)
    for p in [BASE / "README-ORIGINAL.md", BASE / "evidencia-mt/CONEXION-MANUAL.md"]:
        if p.exists():
            shutil.copy2(p, docs / p.name)
    for p in (BASE / "portable").glob("*.diff"):
        shutil.copy2(p, docs / p.name)
    control = STAGE / "DEBIAN"
    control.mkdir()
    installed = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file() and not p.is_symlink())
    (control / "control").write_text(
        "Package: framebridge-original\n"
        "Version: " + VERSION + "\n"
        "Section: utils\nPriority: optional\nArchitecture: amd64\n"
        "Maintainer: FrameBridge <framebridge@localhost>\n"
        "Installed-Size: " + str((installed + 1023) // 1024) + "\n"
        "Depends: python3 (>= 3.13), python3 (<< 3.14), libc6 (>= 2.34), libgcc-s1, bash, tmux, coreutils, util-linux, mawk | gawk, gnome-terminal, wmctrl, xdotool, x11-utils, xclip, imagemagick, xterm\n"
        "Description: FrameBridge Original with manual ChatGPT connection\n"
        " Exact ASUS executor and native desktop programs with GERANAS MCP dependencies\n"
        " and tunnel client. A separate profile and explicit manual startup avoid\n"
        " collision with existing FrameBridge installations. No phone pairing or\n"
        " Owner Device is required by this local executable path.\n")
    lines = []
    for p in sorted(STAGE.rglob("*")):
        if p.is_file() and not p.is_symlink() and control not in p.parents:
            lines.append(hashlib.md5(p.read_bytes()).hexdigest() + "  " + str(p.relative_to(STAGE)))
    (control / "md5sums").write_text("\n".join(lines) + "\n")
    manifest = [{"path": str(p.relative_to(STAGE)), "sha256": sha(p), "bytes": p.stat().st_size}
                for p in sorted(STAGE.rglob("*")) if p.is_file() and not p.is_symlink()]
    (RELEASE / "PACKAGE-MANIFEST.json").write_text(json.dumps({
        "package": "framebridge-original", "version": VERSION, "architecture": "amd64",
        "source_host": "GERAMEG", "files": manifest}, indent=2) + "\n")
    for p in STAGE.rglob("*"):
        if not p.is_symlink():
            p.chmod(0o755 if p.is_dir() or p.stat().st_mode & 0o111 else 0o644)
    for p in sorted(STAGE.rglob("*"), reverse=True):
        os.utime(p, (EPOCH, EPOCH), follow_symlinks=False)
    STAGE.chmod(0o755)
    os.utime(STAGE, (EPOCH, EPOCH))
    output = RELEASE / ("framebridge-original_" + VERSION + "_amd64.deb")
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(EPOCH)
    subprocess.run(["dpkg-deb", "--root-owner-group", "--uniform-compression", "-Zxz", "-z6",
                    "--build", str(STAGE), str(output)], check=True, env=env)
    shutil.copyfile(output, RELEASE / "framebridge.deb")
    paths = [output, RELEASE / "framebridge.deb", RELEASE / "PACKAGE-MANIFEST.json"]
    (RELEASE / "SHA256SUMS.txt").write_text("".join(sha(p) + "  " + p.name + "\n" for p in paths))
    print(json.dumps({"package": str(output), "bytes": output.stat().st_size, "sha256": sha(output),
                      "files": len(manifest)}, indent=2))
if __name__ == "__main__":
    main()
