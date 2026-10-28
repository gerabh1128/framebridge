#!/usr/bin/env python3
"""Publish a reviewed release by normal authenticated fast-forward Git push."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

REPOSITORY = "http://127.0.0.1:3005/gerardo/framebridge.git"
API = "http://127.0.0.1:3005/api/v1"
CONTAINER = "geranas-gitea"

def require(value, message):
    if not value:
        raise ValueError(message)

def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True, **kwargs)

def remote_refs():
    raw = run(["git", "-c", "credential.helper=", "ls-remote", REPOSITORY]).stdout.decode()
    return dict((ref, sha) for sha, ref in (line.split() for line in raw.splitlines()))

def api_request(method, path, token):
    request = urllib.request.Request(API + path, method=method,
                                      headers={"Authorization": "Basic " + base64.b64encode(("gerardo:" + token).encode()).decode()})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status, response.read()

def check_bundle(args, source):
    data = args.bundle.read_bytes()
    require(hashlib.sha256(data).hexdigest() == args.bundle_sha256, "Bundle hash mismatch")
    run(["git", "clone", "--bare", "--no-hardlinks", args.bundle, source])
    run(["git", "--git-dir", source, "fsck", "--full"])
    actual = run(["git", "--git-dir", source, "rev-parse", args.commit + "^{commit}"]).stdout.decode().strip()
    require(actual == args.commit, "Reviewed release commit missing")
    run(["git", "--git-dir", source, "merge-base", "--is-ancestor", args.old_commit, args.commit])
    resolved = run(["git", "--git-dir", source, "rev-parse", args.tag + "^{}"]).stdout.decode().strip()
    require(resolved == args.commit, "Release tag does not point to reviewed commit")
    refs = remote_refs()
    require(refs.get("refs/heads/main") == args.old_commit, "Canonical main changed; review before publishing")
    require("refs/tags/" + args.tag not in refs, "Release tag already exists; never overwrite")
    return refs

def record(path, state):
    temporary = path.with_suffix(".partial")
    with open(temporary, "x", encoding="utf-8") as stream:
        json.dump(state, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)

def askpass_server(directory, token):
    address = str(directory / "credential.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(address)
    os.chmod(address, 0o600)
    server.listen(2)
    server.settimeout(0.5)
    stop = threading.Event()
    def serve():
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(3)
                request = connection.recv(32)
                if request == b"username":
                    connection.sendall(b"gerardo\n")
                elif request == b"password":
                    connection.sendall(token.encode() + b"\n")
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = directory / "askpass"
    client.write_text(
        "#!/usr/bin/python3\nimport socket,sys\n"
        "p=sys.argv[1].lower()\n"
        "kind=b'username' if p.startswith('username') else b'password' if p.startswith('password') else None\n"
        "if kind is None: raise SystemExit(1)\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        f"s.connect({address!r})\ns.sendall(kind)\ns.shutdown(socket.SHUT_WR)\n"
        "data=b''\n"
        "while True:\n chunk=s.recv(4096)\n if not chunk: break\n data+=chunk\n"
        "sys.stdout.buffer.write(data)\n")
    client.chmod(0o700)
    return server, stop, thread, client

def root_directory(path):
    require(path.is_absolute() and path.resolve(strict=True) == path, "Root staging path must be absolute without symlink parents")
    require(path.is_dir() and path.stat().st_mode & 0o077 == 0, "Root staging directory must be private")
    for current in (path, *path.parents):
        metadata = current.stat()
        require(metadata.st_uid == 0 and metadata.st_mode & 0o022 == 0,
                "Root staging path has a non-root or shared-writable ancestor")

def publish(args):
    require(os.geteuid() == 0 and socket.gethostname().lower() == "geranas",
            "Run on GERANAS using the authorized normal administrator context")
    for sha in (args.bundle_sha256,):
        require(re.fullmatch(r"[a-f0-9]{64}", sha), "Invalid bundle SHA-256")
    for sha in (args.commit, args.old_commit):
        require(re.fullmatch(r"[a-f0-9]{40}", sha), "Invalid source commit")
    require(re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", args.tag), "Invalid release tag")
    require(args.bundle.is_file() and not args.bundle.is_symlink(), "Unsafe bundle path")
    bundle_stat = args.bundle.stat()
    require(bundle_stat.st_uid == 0 and bundle_stat.st_mode & 0o022 == 0 and bundle_stat.st_nlink == 1,
            "Bundle must be a root-owned reviewed regular file without shared write access")
    root_directory(args.bundle.parent)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="fb-gitea-push-", dir="/run") as tmp:
        directory = Path(tmp)
        source = directory / "source.git"
        before = check_bundle(args, source)
        state = {"schema": 1, "repository": "gerardo/framebridge", "old_commit": args.old_commit,
                 "commit": args.commit, "tag": args.tag, "bundle_sha256": args.bundle_sha256,
                 "phase": "CHECKED", "push_performed": False, "token_revoked": False}
        if not args.apply:
            print(json.dumps(state, sort_keys=True))
            return
        require(args.journal is not None and args.journal.parent.is_dir(), "Explicit existing journal directory required")
        root_directory(args.journal.parent)
        require(not args.journal.exists() and not args.journal.is_symlink(), "Existing transaction; never replay blindly")
        # Reserve the exact new transaction before minting a credential.
        descriptor = os.open(args.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        record(args.journal, state)
        token_name = "framebridge-release-" + uuid.uuid4().hex
        state["temporary_token_name"] = token_name
        state["phase"] = "TOKEN_CREATION_REQUESTED"
        record(args.journal, state)
        token = None
        failure = None
        server = stop = thread = None
        try:
            # Capture the token only in this process's memory. Never log this result.
            result = run(["docker", "exec", "--user", "git", CONTAINER, "gitea",
                          "--config", "/etc/gitea/app.ini", "admin", "user", "generate-access-token",
                          "--username", "gerardo", "--token-name", token_name, "--raw",
                          "--scopes", "write:repository,write:user"])
            token = result.stdout.decode().strip()
            require(re.fullmatch(r"[a-fA-F0-9]{40,128}", token), "Unexpected credential response; token needs owner review")
            state["phase"] = "TEMPORARY_TOKEN_CREATED"
            record(args.journal, state)
            # The installed token-management route requires Basic authentication.
            # Gitea accepts a PAT as the Basic password; prove that gate before pushing.
            status, _ = api_request("GET", "/users/gerardo/tokens?limit=1", token)
            require(status == 200, "Normal Basic token-management authentication failed")
            state["token_management_auth_verified"] = True
            record(args.journal, state)
            server, stop, thread, client = askpass_server(directory, token)
            env = dict(os.environ, GIT_ASKPASS=str(client), GIT_TERMINAL_PROMPT="0", LC_ALL="C")
            # No force flag. The normal receive-pack checks and Gitea hooks run.
            run(["git", "--git-dir", source, "-c", "credential.helper=", "push", "--atomic",
                 REPOSITORY, args.commit + ":refs/heads/main", "refs/tags/" + args.tag], env=env, timeout=120)
            state["push_performed"] = True
            state["phase"] = "PUSH_COMPLETED"
            after = remote_refs()
            require(after.get("refs/heads/main") == args.commit, "Main ref readback mismatch")
            resolved_tag = after.get("refs/tags/" + args.tag + "^{}", after.get("refs/tags/" + args.tag))
            require(resolved_tag == args.commit, "Release tag readback mismatch")
            state["refs_verified"] = True
            record(args.journal, state)
        except Exception as error:
            # Commands never contain the credential; do not print captured HTTP/Git output.
            failure = f"{type(error).__name__}: {error}"
            state["failure"] = failure
        finally:
            if stop is not None:
                stop.set()
                thread.join(timeout=2)
                server.close()
            if token is not None:
                try:
                    status, _ = api_request("DELETE", "/users/gerardo/tokens/" +
                                             urllib.parse.quote(token_name, safe=""), token)
                    require(status == 204, "Unexpected token revocation response")
                    try:
                        api_request("GET", "/user", token)
                    except urllib.error.HTTPError as error:
                        require(error.code == 401, "Revoked credential did not return HTTP 401")
                    else:
                        raise ValueError("Credential remains accepted after revocation")
                    state["token_revoked"] = True
                except Exception as error:
                    state["revocation_failure"] = f"{type(error).__name__}: {error}"
                    failure = failure or "Temporary token revocation requires review"
            token = None
            state["phase"] = ("COMPLETED" if state.get("refs_verified") and
                              state["token_revoked"] and failure is None else "REVIEW_REQUIRED")
            record(args.journal, state)
        print(json.dumps(state, sort_keys=True))
        require(failure is None, failure)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--old-commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        publish(args)
    except Exception as error:
        print(f"FrameBridge Gitea publication: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
