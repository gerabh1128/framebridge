"""Stage a pinned APT source in a DEB; never access network or host APT."""
from pathlib import Path
import re
import subprocess
import tempfile

KEY = "/etc/apt/keyrings/framebridge-stable-archive.gpg"
SOURCE = "/etc/apt/sources.list.d/framebridge-stable.sources"
STABLE_URI = "https://framebridge.betancourt.one/debian/stable/"
STABLE_FPR = "69D0E9D83699F96AB986B06F3C24F85D892A517E"
QA_URI = "file:/mnt/fbqa/repo"

def validate_public_key(raw, expected):
    if not 0 < len(raw) <= 65536:
        raise ValueError("APT_BOOTSTRAP_KEY_SIZE")
    with tempfile.TemporaryDirectory(prefix="fb-apt-keycheck-") as home:
        path = Path(home) / "archive.gpg"
        path.write_bytes(raw)
        base = ["gpg", "--batch", "--no-options", "--homedir", home]
        # Require a binary transferable public certificate, not a keybox,
        # ASCII secret key or a bundle with an unrelated primary.
        packets = subprocess.run(base + ["--list-packets", str(path)],
                                 capture_output=True, text=True, timeout=10, check=True)
        if re.search(r":secret (?:sub )?key packet:", packets.stdout):
            raise ValueError("APT_BOOTSTRAP_SECRET_KEY_REFUSED")
        listing = subprocess.run(base + ["--with-colons", "--show-keys", str(path)],
                                 capture_output=True, text=True, timeout=10, check=True)
        lines = [line.split(":") for line in listing.stdout.splitlines()]
        primary = [line for line in lines if line[0] in ("pub", "sec")]
        fingerprints = [line[9] for line in lines if line[0] == "fpr"]
        if len(primary) != 1 or primary[0][0] != "pub" or not fingerprints or fingerprints[0] != expected:
            raise ValueError("APT_BOOTSTRAP_FINGERPRINT_MISMATCH")
        if primary[0][1] in ("r", "e", "d") or raw.startswith(b"-----BEGIN"):
            raise ValueError("APT_BOOTSTRAP_KEY_UNUSABLE")
    return expected

def stage(root, key, mode, qa_fingerprint=None):
    if mode not in ("stable", "qa"):
        raise ValueError("APT_BOOTSTRAP_MODE_INVALID")
    if mode == "qa":
        if not isinstance(qa_fingerprint,str) or not re.fullmatch(r"[0-9A-F]{40}",qa_fingerprint):
            raise ValueError("APT_QA_FINGERPRINT_REQUIRED")
        if qa_fingerprint == STABLE_FPR:
            raise ValueError("APT_QA_MUST_NOT_USE_STABLE_KEY")
    elif qa_fingerprint is not None:
        raise ValueError("APT_STABLE_FINGERPRINT_IS_PINNED")
    uri, fpr, suite = (STABLE_URI, STABLE_FPR, "stable") if mode == "stable" else (QA_URI, qa_fingerprint, "qa")
    raw = Path(key).read_bytes()
    validate_public_key(raw, fpr)
    root = Path(root)
    for name, payload in ((KEY, raw), (SOURCE, (
        "Types: deb\nURIs: " + uri + "\nSuites: " + suite + "\nComponents: main\n"
        "Architectures: amd64\nSigned-By: " + KEY + "\nEnabled: yes\n"
    ).encode("ascii"))):
        path = root / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)
        path.chmod(0o644)
    return {"mode": mode, "uri": uri, "suite": suite, "fingerprint": fpr,
            "keyring": KEY, "source": SOURCE}, (SOURCE, KEY)
