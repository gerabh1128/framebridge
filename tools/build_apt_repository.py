#!/usr/bin/env python3
"""Build and verify three signed APT suites from the reviewed distribution DEBs."""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PACKAGE = "framebridge-original-expanded"
FINGERPRINT = "2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808"
DEPENDENCY_SOURCE_ASSETS = {
    "FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz": (82606998, "64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a"),
    "FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz": (26941745, "6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9"),
}
PROFILES = {
    "debian": ("trixie", "Debian 13", "debian13-amd64-py313", "3.13"),
    "ubuntu": ("noble", "Ubuntu 24.04", "ubuntu24.04-amd64-py312", "3.12"),
    "mint": ("mint22", "Linux Mint 22.x", "mint22-amd64-py312", "3.12"),
}

def require(condition, message):
    if not condition:
        raise ValueError(message)

def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm, data).hexdigest()

def run(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, capture_output=True, **kwargs)

def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

def public_fingerprint(key):
    with tempfile.TemporaryDirectory(prefix="framebridge-public-key-") as tmp:
        os.chmod(tmp, 0o700)
        result = run(["gpg", "--no-options", "--homedir", tmp, "--batch",
                      "--with-colons", "--show-keys", key]).stdout.decode()
    fingerprints = [line.split(":")[9] for line in result.splitlines()
                    if line.startswith("fpr:")]
    primary_keys = [line for line in result.splitlines() if line.startswith("pub:")]
    require(len(primary_keys) == 1 and fingerprints and fingerprints[0] == FINGERPRINT,
            "Expected exactly one official primary release key")
    return fingerprints[0]

def control(path):
    text = run(["dpkg-deb", "--field", path]).stdout.decode()
    fields = {}
    for line in text.splitlines():
        if line and not line[0].isspace():
            name, separator, value = line.partition(":")
            require(separator, "Invalid Debian control field")
            fields[name] = value.strip()
    require(not any(k in fields for k in ("Filename", "Size", "MD5sum", "SHA256", "SHA512")),
            "Artifact control contains repository-generated fields")
    return text.rstrip() + "\n", fields

def release_bytes(suite, label, date, files):
    lines = ["Origin: FrameBridge", "Label: FrameBridge", f"Suite: {suite}",
             f"Codename: {suite}", f"Date: {email.utils.format_datetime(date)}",
             f"Valid-Until: {email.utils.format_datetime(date + dt.timedelta(days=30))}",
             "Architectures: amd64", "Components: main",
             f"Description: FrameBridge for {label}", "Acquire-By-Hash: yes"]
    for algorithm, heading in (("sha256", "SHA256"), ("sha512", "SHA512")):
        lines.append(heading + ":")
        lines.extend(f" {digest(data, algorithm)} {len(data):16d} {name}"
                     for name, data in sorted(files.items()))
    return ("\n".join(lines) + "\n").encode()


def verify_detached(public_key, signature, payload):
    with tempfile.TemporaryDirectory(prefix="framebridge-asset-verify-") as tmp:
        tmp = Path(tmp)
        ring = tmp / "release.gpg"
        ring.write_bytes(run(["gpg", "--no-options", "--homedir", tmp, "--batch", "--output", "-",
                              "--dearmor", public_key]).stdout)
        result = run(["gpgv", "--homedir", tmp, "--keyring", ring, "--status-fd", "1", signature, payload])
        require(f"[GNUPG:] VALIDSIG {FINGERPRINT} ".encode() in result.stdout, "Unexpected dependency-source signer")

def prepare_downloads(args, inputs):
    source = getattr(args, "source", None)
    dependencies = getattr(args, "dependency_source", [])
    if source is None:
        require(not dependencies, "Dependency assets require the corresponding project-source archive")
        return []
    require(source.is_file() and not source.is_symlink() and
            source.name == f"FrameBridge-{args.version}-source.tar.gz", "Unexpected project-source archive")
    require({p.name for p in dependencies} == set(DEPENDENCY_SOURCE_ASSETS) and
            len(dependencies) == len(DEPENDENCY_SOURCE_ASSETS), "Exactly the reviewed two dependency-source archives required")
    for path in dependencies:
        expected_size, expected_sha = DEPENDENCY_SOURCE_ASSETS[path.name]
        require(path.is_file() and not path.is_symlink(), "Unsafe dependency-source archive")
        data = path.read_bytes()
        require(len(data) == expected_size and digest(data) == expected_sha, "Dependency-source archive hash mismatch")
        verify_detached(args.public_key, path.with_name(path.name + ".asc"), path)
    directory = args.output / "downloads"
    directory.mkdir()
    assets = []
    def add(name, data, needs_signing):
        target = directory / name
        require(not target.exists(), "Duplicate release filename")
        target.write_bytes(data)
        assets.append({"filename": name, "size_bytes": len(data), "sha256": digest(data),
                       "signature": name + ".asc", "needs_signing": needs_signing})
    for _, _, _, profile, _, data in inputs:
        add(f"FrameBridge-{args.version}-{profile}.deb", data, True)
    add(source.name, source.read_bytes(), True)
    for path in dependencies:
        add(path.name, path.read_bytes(), False)
        shutil.copyfile(path.with_name(path.name + ".asc"), directory / (path.name + ".asc"))
    shutil.copyfile(args.public_key, directory / "framebridge-release-key.asc")
    sums = "".join(item["sha256"] + "  " + item["filename"] + "\n" for item in assets)
    add("SHA256SUMS", sums.encode(), True)
    return assets

def prepare(args):
    output = args.output
    require(not output.exists(), "Output must be a new directory; never overwrite a published repository")
    require(re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", args.version), "Invalid version")
    public_fingerprint(args.public_key)
    epoch = args.epoch if args.epoch is not None else int(dt.datetime.now(dt.timezone.utc).timestamp())
    date = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    inputs = []
    for distro, (suite, label, profile, python) in PROFILES.items():
        path = getattr(args, distro)
        require(path.is_file() and not path.is_symlink(), f"{distro}: artifact is not a regular file")
        text, fields = control(path)
        require(fields.get("Package") == PACKAGE, f"{distro}: wrong binary package")
        require(fields.get("Version") == args.version, f"{distro}: wrong version")
        require(fields.get("Architecture") == "amd64", f"{distro}: wrong architecture")
        require(re.search(r"python3\s*\(>=\s*" + re.escape(python), fields.get("Depends", "")),
                f"{distro}: Python dependency does not match the tested profile")
        data = path.read_bytes()
        inputs.append((distro, suite, label, profile, text, data))
    output.mkdir(parents=True, mode=0o755)
    records = []
    for distro, suite, label, profile, text, data in inputs:
        relative = f"pool/main/f/framebridge/{PACKAGE}_{args.version}_{profile}.deb"
        artifact = output / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(data)
        require(digest(artifact.read_bytes()) == digest(data), "Copied artifact hash mismatch")
        stanza = text + f"Filename: {relative}\nSize: {len(data)}\nSHA256: {digest(data)}\nSHA512: {digest(data, 'sha512')}\n\n"
        plain = stanza.encode()
        indexes = {"main/binary-amd64/Packages": plain,
                   "main/binary-amd64/Packages.gz": gzip.compress(plain, mtime=0)}
        suite_dir = output / "dists" / suite
        for name, index in indexes.items():
            target = suite_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(index)
            for algorithm in ("sha256", "sha512"):
                by_hash = target.parent / "by-hash" / algorithm.upper() / digest(index, algorithm)
                by_hash.parent.mkdir(parents=True, exist_ok=True)
                by_hash.write_bytes(index)
        (suite_dir / "Release").write_bytes(release_bytes(suite, label, date, indexes))
        records.append({"distribution": distro, "label": label, "suite": suite,
                        "profile": profile, "filename": relative,
                        "size": len(data), "sha256": digest(data)})
    downloads = prepare_downloads(args, inputs)
    shutil.copyfile(args.public_key, output / "framebridge-archive-keyring.asc")
    files = {p.relative_to(output).as_posix(): {"bytes": p.stat().st_size,
             "sha256": digest(p.read_bytes())} for p in sorted(output.rglob("*")) if p.is_file()}
    manifest = {"schema": 1, "package": PACKAGE, "version": args.version,
                "fingerprint": FINGERPRINT, "prepared_utc": date.isoformat(),
                "artifacts": records, "download_assets": downloads, "files": files}
    dump(output / "APT-INPUT-MANIFEST.json", manifest)
    manifest_hash = digest((output / "APT-INPUT-MANIFEST.json").read_bytes())
    print(json.dumps({"prepared": True, "signed": False, "directory": str(output),
                      "version": args.version, "manifest_sha256": manifest_hash,
                      "suites": [x["suite"] for x in records]}, sort_keys=True))

def check_inputs(output, expected):
    p = output / "APT-INPUT-MANIFEST.json"
    require(p.is_file() and not p.is_symlink(), "Input manifest missing or unsafe")
    raw = p.read_bytes()
    require(digest(raw) == expected, "Manifest hash mismatch")
    manifest = json.loads(raw)
    require(manifest.get("package") == PACKAGE and manifest.get("fingerprint") == FINGERPRINT,
            "Wrong package or signing identity")
    require(len(manifest.get("artifacts", [])) == 3, "Exactly three distribution artifacts required")
    require({x["suite"] for x in manifest["artifacts"]} == {x[0] for x in PROFILES.values()},
            "Unexpected distribution suite set")
    for name, metadata in manifest["files"].items():
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "Unsafe manifest path")
        path = output / relative
        require(path.is_file() and not path.is_symlink(), f"Missing or unsafe input: {name}")
        data = path.read_bytes()
        require(len(data) == metadata["bytes"] and digest(data) == metadata["sha256"],
                f"Input changed after review: {name}")
    public_fingerprint(output / "framebridge-archive-keyring.asc")
    return manifest

def signature_paths(output, sign_only=False):
    for suite, *_ in PROFILES.values():
        root = output / "dists" / suite
        yield root / "Release", root / "InRelease", True
        yield root / "Release", root / "Release.gpg", False
    manifest = json.loads((output / "APT-INPUT-MANIFEST.json").read_text())
    for asset in manifest.get("download_assets", []):
        if sign_only and not asset["needs_signing"]:
            continue
        filename = asset["filename"]
        require(Path(filename).name == filename and Path(asset["signature"]).name == asset["signature"],
                "Unsafe download signature path")
        yield output / "downloads" / filename, output / "downloads" / asset["signature"], False

def verify_signatures(output, manifest):
    with tempfile.TemporaryDirectory(prefix="framebridge-apt-verify-") as tmp:
        tmp = Path(tmp)
        keyring = tmp / "release.gpg"
        keyring.write_bytes(run(["gpg", "--no-options", "--homedir", tmp, "--batch", "--output", "-", "--dearmor",
                                 output / "framebridge-archive-keyring.asc"]).stdout)
        for release, signed, clearsign in signature_paths(output):
            require(signed.is_file() and not signed.is_symlink(), "Missing signature")
            args = ["gpgv", "--homedir", tmp, "--keyring", keyring, "--status-fd", "1"]
            result = run(args + ([signed] if clearsign else [signed, release]))
            require(f"[GNUPG:] VALIDSIG {FINGERPRINT} ".encode() in result.stdout,
                    "Signature made by unexpected key")
            if clearsign:
                plaintext = run(["gpgv", "--homedir", tmp, "--keyring", keyring,
                                 "--output", "-", signed]).stdout
                require(plaintext == release.read_bytes(), "Clear-signed release differs from reviewed input")
    return {"verified": True, "version": manifest["version"],
            "suites": [x["suite"] for x in manifest["artifacts"]], "fingerprint": FINGERPRINT}

def sign(args):
    require(os.getuid() != 0 and sys.stdin.isatty() and os.isatty(1),
            "Signing must run as the owner in a normal interactive terminal")
    manifest = check_inputs(args.output, args.manifest_sha256)
    require(args.key_home.is_dir() and not args.key_home.is_symlink(), "Unsafe signing home")
    key_stat = args.key_home.stat()
    require(key_stat.st_uid == os.getuid() and key_stat.st_mode & 0o077 == 0, "Signing home must be owner-private")
    targets = list(signature_paths(args.output, sign_only=True))
    require(all(not target.exists() for _, target, _ in targets), "Signatures already exist; do not overwrite")
    common = ["gpg", "--no-options", "--homedir", args.key_home, "--pinentry-mode", "default",
              "--local-user", FINGERPRINT, "--digest-algo", "SHA256"]
    try:
        for source, target, clearsign in targets:
            check_inputs(args.output, args.manifest_sha256)
            temporary = target.with_name(target.name + ".partial")
            require(not temporary.exists(), "Incomplete previous signing transaction needs review")
            subprocess.run([str(x) for x in common + ["--output", temporary,
                           "--clearsign" if clearsign else "--detach-sign", source]], check=True)
            temporary.rename(target)
        result = verify_signatures(args.output, manifest)
        result["manifest_sha256"] = args.manifest_sha256
        dump(args.output / "APT-SIGNING-RECEIPT.json", result)
        print(json.dumps(result, sort_keys=True))
    finally:
        run(["gpgconf", "--homedir", args.key_home, "--kill", "gpg-agent"])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--version", required=True)
    for distro in PROFILES:
        prepare_parser.add_argument("--" + distro, type=Path, required=True)
    prepare_parser.add_argument("--public-key", type=Path, required=True)
    prepare_parser.add_argument("--epoch", type=int)
    prepare_parser.add_argument("--source", type=Path, help="Reviewed corresponding project-source tar.gz")
    prepare_parser.add_argument("--dependency-source", type=Path, action="append", default=[],
                                help="Reviewed dependency-source archive with its unchanged sibling .asc")
    for name in ("sign", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--output", type=Path, required=True)
        sub.add_argument("--manifest-sha256", required=True)
        if name == "sign":
            sub.add_argument("--key-home", type=Path,
                default=Path.home() / ".local/share/framebridge-release-signing")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.command == "sign":
            sign(args)
        else:
            manifest = check_inputs(args.output, args.manifest_sha256)
            print(json.dumps(verify_signatures(args.output, manifest), sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"FrameBridge APT repository: {error}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
