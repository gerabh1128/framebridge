#!/usr/bin/python3
"""Screen assembled public release artifacts without printing matched values."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


PRIVATE_PATHS = {
    ".env", ".git-credentials", ".netrc", ".ssh", "credentials.txt",
    "credentials.json", "cookies.txt", "cookies.sqlite",
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa", "rclone.conf",
}
GZIP_LIMIT = 96 * 1024 * 1024
CONTENT_RULES = (
    ("private-key-material", re.compile(
        rb"-----BEGIN (?:[A-Z0-9-]+ )*PRIVATE KEY(?: BLOCK)?-----"
        rb"(?:\r?\n[^\r\n]{0,160}){0,6}\r?\n[A-Za-z0-9+/]{24,}"
    )),
    ("credential-token", re.compile(
        rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"
        rb"|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}"
        rb"|sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})\b"
    )),
    ("drive-document-link", re.compile(
        rb"https?://(?:drive|docs)\.google\.com/[^\s\"'<>]+", re.IGNORECASE
    )),
)
URL_CREDENTIAL = re.compile(
    rb"\b(?:https?|sftp|postgres(?:ql)?)://[A-Za-z0-9._%+-]{1,100}:"
    rb"[A-Za-z0-9._%+=-]{8,100}@[A-Za-z0-9.-]+"
)
# Exact reviewed HTTPX documentation examples preserved in both ABI vendors.
HTTPX_DOC_FILE_SHA256 = "757f7d551d4348e1e9828f40abb3f360ee0529dc6a2a3c32369f20adff1d1cdd"
HTTPX_DOC_EXAMPLES = {
    "0ebf92f8dd2bb8573a05b4f843b9c501f7192a76051bb9da419d8294329674ce",
    "3d6e008451b0edcc8d3524508217185561c5a2851b4379ecaeb228460d0f6f10",
}
PERSONAL_EMAIL = re.compile(
    rb"\b[A-Za-z0-9._%+-]+@(?:gmail\.com|outlook\.com|hotmail\.com|icloud\.com|yahoo\.com)\b",
    re.IGNORECASE,
)


class PrivacyError(RuntimeError):
    """A finding or an input that could not be verified."""


def external_public_policy(path, repository_root):
    """Keep release-specific private identifiers out of public source trees."""
    if path is None:
        raise PrivacyError("Public release requires an external privacy policy")
    p = Path(path)
    if (p.is_symlink() or not p.is_file()
            or p.resolve().is_relative_to(Path(repository_root).resolve())):
        raise PrivacyError("Public privacy policy must be a regular file outside the repository")
    return p.resolve()


class ReleasePrivacy:
    def __init__(self, policy=None):
        self.rules = CONTENT_RULES
        if policy is not None:
            try:
                values = json.loads(Path(policy).read_text(encoding="utf-8"))
                if (not isinstance(values, list) or not values or len(values) > 1024
                    or not all(isinstance(value, str) and 3 <= len(value) <= 512
                               and value.isprintable() and value == value.strip()
                               for value in values)):
                    raise ValueError
                terms = b"|".join(re.escape(value.encode("utf-8")) for value in values)
            except (OSError, ValueError, UnicodeError):
                raise PrivacyError("Invalid private identifier policy") from None
            pattern = re.compile(rb"(?<![A-Za-z0-9])(?:" + terms + rb")(?![A-Za-z0-9])", re.IGNORECASE)
            self.rules += (("private-identifier", pattern),)

    def check(self, name, data, location, *, personal_email=False):
        rules = self.rules + (("personal-contact", PERSONAL_EMAIL),) if personal_email else self.rules
        if PRIVATE_PATHS.intersection(part.lower() for part in PurePosixPath(name).parts):
            raise PrivacyError(f"{location}: private-file")
        encoded = name.encode("utf-8", errors="surrogateescape")
        if data.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                inflated = compressed.read(GZIP_LIMIT + 1)
            if len(inflated) > GZIP_LIMIT:
                raise PrivacyError(f"{location}: compressed file exceeds review limit")
        else:
            inflated = b""
        reviewed_httpx = (name.endswith(("/vendor/httpx/_urls.py", "/target-vendors/cp312/httpx/_urls.py"))
                          and hashlib.sha256(data).hexdigest() == HTTPX_DOC_FILE_SHA256)
        payloads = (encoded, data, inflated)
        for payload in payloads:
            for match in URL_CREDENTIAL.finditer(payload):
                if not (payload is data and reviewed_httpx
                        and hashlib.sha256(match[0]).hexdigest() in HTTPX_DOC_EXAMPLES):
                    raise PrivacyError(f"{location}: credential-in-url")
        for label, pattern in rules:
            if any(pattern.search(payload) for payload in payloads):
                raise PrivacyError(f"{location}: {label}")

    def tar_stream(self, stream, kind, *, personal_email=False):
        count = 0
        with tarfile.open(fileobj=stream, mode="r|*") as archive:
            for count, member in enumerate(archive, 1):
                if member.isfile():
                    with archive.extractfile(member) as content:
                        data = content.read()
                else:
                    data = member.linkname.encode("utf-8", errors="surrogateescape")
                location = f"{kind} member {count}"
                self.check(member.name, data, location, personal_email=personal_email)
                metadata = (member.uname, member.gname, member.linkname, *member.pax_headers, *member.pax_headers.values())
                self.check("", "\n".join(metadata).encode("utf-8", errors="surrogateescape"),
                           location, personal_email=personal_email)
        if not count:
            raise PrivacyError(f"{kind}: empty archive")
        return count

    def source(self, path):
        with Path(path).open("rb") as archive:
            return self.tar_stream(archive, "source archive")

    def deb(self, path):
        counts = {}
        for surface in ("control", "data"):
            command = "--ctrl-tarfile" if surface == "control" else "--fsys-tarfile"
            with subprocess.Popen(["dpkg-deb", command, str(Path(path).resolve())],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                with process.stdout:
                    counts[surface] = self.tar_stream(
                        process.stdout, f"Debian {surface}", personal_email=(surface == "control"))
                if process.wait():
                    raise PrivacyError(f"Debian {surface}: unreadable archive")
        return counts

    def site(self, path):
        root = Path(path)
        if root.is_symlink() or not root.is_dir():
            raise PrivacyError("Website output must be a directory")
        inventory = hashlib.sha256()
        count = 0
        for item in sorted(root.rglob("*")):
            if item.is_symlink():
                raise PrivacyError("Website output contains a symbolic link")
            if item.is_file():
                count += 1
                name = item.relative_to(root).as_posix()
                data = item.read_bytes()
                self.check(name, data, f"website file {count}", personal_email=True)
                inventory.update(name.encode("utf-8", errors="surrogateescape") + b"\0" + hashlib.sha256(data).digest())
        if not count:
            raise PrivacyError("Website output is empty")
        return {"files": count, "inventory_sha256": inventory.hexdigest()}


def sha256(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True,
                        help="Private JSON array of project-specific identifiers")
    parser.add_argument("artifacts", type=Path, nargs="+",
                        help="Git exports, source archives, Debian packages, manifests or rendered website directories")
    args = parser.parse_args()
    try:
        scanner = ReleasePrivacy(args.policy)
        results = []
        for artifact in args.artifacts:
            if artifact.is_dir():
                results.append({"kind": "website", **scanner.site(artifact)})
                continue
            digest = sha256(artifact)
            if artifact.name.endswith((".tar", ".tar.gz")):
                result = {"kind": "source", "members": scanner.source(artifact)}
            elif artifact.suffix == ".deb":
                result = {"kind": "deb", "members": scanner.deb(artifact)}
            elif artifact.suffix == ".json":
                scanner.check(artifact.name, artifact.read_bytes(), "manifest")
                result = {"kind": "manifest"}
            else:
                raise PrivacyError("Unsupported public artifact")
            if sha256(artifact) != digest:
                raise PrivacyError("Artifact changed during privacy review")
            results.append({**result, "sha256": digest})
    except (PrivacyError, OSError, EOFError, tarfile.TarError) as error:
        parser.exit(1, f"Release privacy review blocked: {error if isinstance(error, PrivacyError) else 'unreadable input'}.\n")
    print(json.dumps({"status": "PASS", "artifacts": results}, sort_keys=True))


if __name__ == "__main__":
    main()
