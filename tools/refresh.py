#!/usr/bin/env python3
"""Refresh the public FrameBridge APT mirror in a clean checkout of apt-mirror."""
import gzip
import hashlib
import re
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from email.parser import Parser
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath

ORIGIN = "https://framebridge.betancourt.one/debian/"
FINGERPRINT = "2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808"
SUITES = ("trixie", "noble", "mint22")
INDEXES = {"main/binary-amd64/Packages", "main/binary-amd64/Packages.gz"}
ROOT = Path(__file__).resolve().parents[1] / "debian"


def download(path, limit):
    with urllib.request.urlopen(ORIGIN + path, timeout=45) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("Insecure redirect")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Unexpected size for " + path)
    return data


def put(root, path, data):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != data:
        raise ValueError("Conflicting data in this snapshot: " + str(path))
    target.write_bytes(data)


def checked(data, size, hashes):
    if len(data) != size or any(hashlib.new(alg, data).hexdigest() != digest for alg, digest in hashes.items()):
        raise ValueError("Size or digest mismatch")
    return data


def gpg(home, *args, input_data=None):
    return subprocess.run(["gpg", "--batch", "--no-options", "--homedir", str(home), *args],
                          input=input_data, capture_output=True, check=True).stdout


def verify(home, filename, *args):
    status = gpg(home, "--status-fd=1", "--verify", str(filename), *map(str, args)).decode()
    valid = [line.split() for line in status.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if len(valid) != 1 or FINGERPRINT not in (valid[0][2], valid[0][-1]):
        raise ValueError("Release signed by an unexpected key")


def index_hashes(release):
    rows = {}
    for alg in ("SHA256", "SHA512"):
        for line in release[alg].strip().splitlines():
            digest, size, path = line.split()
            if path not in INDEXES:
                raise ValueError("Unexpected index: " + path)
            item = rows.setdefault(path, (int(size), {}))
            if item[0] != int(size) or alg.lower() in item[1]:
                raise ValueError("Inconsistent index entry")
            item[1][alg.lower()] = digest
    if set(rows) != INDEXES or any(set(h) != {"sha256", "sha512"} for _, h in rows.values()):
        raise ValueError("Incomplete index digest set")
    return rows


def mirror_suite(stage, home, suite):
    dist = "dists/" + suite + "/"
    for name in ("InRelease", "Release", "Release.gpg"):
        put(stage, dist + name, download(dist + name, 1024 * 1024))
    inrelease = stage / dist / "InRelease"
    release_file = stage / dist / "Release"
    verify(home, inrelease)
    verify(home, stage / dist / "Release.gpg", release_file)
    clear = gpg(home, "--decrypt", str(inrelease))
    if clear != release_file.read_bytes():
        raise ValueError("InRelease and Release differ")
    release = Parser().parsestr(clear.decode())
    expected = {"Origin": "FrameBridge", "Suite": suite, "Codename": suite, "Components": "main",
                "Architectures": "amd64", "Acquire-By-Hash": "yes"}
    if any(release[k] != v for k, v in expected.items()):
        raise ValueError("Unexpected Release identity")
    now = datetime.now(timezone.utc)
    issued = parsedate_to_datetime(release["Date"])
    if issued > now + timedelta(seconds=10) or parsedate_to_datetime(release["Valid-Until"]) <= now:
        raise ValueError("Release outside its validity period")
    old = ROOT / dist / "Release"
    if old.exists():
        prior = parsedate_to_datetime(Parser().parsestr(old.read_text())["Date"])
        if issued < prior or (issued == prior and old.read_bytes() != clear):
            raise ValueError("Origin rollback or conflicting release date")
    for path, (size, hashes) in index_hashes(release).items():
        data = checked(download(dist + path, 8 * 1024 * 1024), size, hashes)
        put(stage, dist + path, data)
        for alg, digest in hashes.items():
            put(stage, Path(dist + path).parent / "by-hash" / alg.upper() / digest, data)
    idx = stage / dist / "main/binary-amd64"
    if gzip.decompress((idx / "Packages.gz").read_bytes()) != (idx / "Packages").read_bytes():
        raise ValueError("Plain and compressed package lists differ")
    for stanza in (idx / "Packages").read_text().strip().split("\n\n"):
        pkg = Parser().parsestr(stanza)
        name = pkg["Filename"]
        if (not name or not re.fullmatch(r"pool/[A-Za-z0-9_+.~/-]+\.deb", name)
                or ".." in PurePosixPath(name).parts or pkg["Architecture"] != "amd64"
                or pkg["Package"] != "framebridge-original-expanded"):
            raise ValueError("Unexpected package path or identity")
        size = int(pkg["Size"])
        if not 0 < size <= 100_000_000:
            raise ValueError("Package exceeds the mirror file-size limit")
        hashes = {alg.lower(): pkg[alg] for alg in ("SHA256", "SHA512")}
        cached = ROOT / name
        data = cached.read_bytes() if cached.exists() else download(name, size)
        put(stage, name, checked(data, size, hashes))
        print(suite, pkg["Version"], size, hashes["sha256"])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "gpg"
        home.mkdir(mode=0o700)
        stage = Path(tmp) / "mirror"
        key = download("framebridge-archive-keyring.asc", 256 * 1024)
        listing = gpg(home, "--with-colons", "--show-keys", input_data=key).decode().splitlines()
        if sum(line.startswith("pub:") for line in listing) != 1 or next(
                line.split(":")[9] for line in listing if line.startswith("fpr:")) != FINGERPRINT:
            raise ValueError("Public signing key fingerprint mismatch")
        gpg(home, "--import", input_data=key)
        put(stage, "framebridge-archive-keyring.asc", key)
        for suite in SUITES:
            mirror_suite(stage, home, suite)
        # Publish the resulting clean checkout in one Git commit. Retain earlier
        # pool and by-hash paths so clients can finish downloads from older lists.
        shutil.copytree(stage, ROOT, dirs_exist_ok=True)


if __name__ == "__main__":
    main()
