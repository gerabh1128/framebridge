"""Narrow privileged host-credential backend for systemd 255.

Run only through the root-owned installed launcher.  Administrative protect and
revoke operations use ordinary sudo authorization.  Each protected credential
gets one literal, owner-specific sudoers decrypt command; there is no general
sudo permission for systemd-creds.  No owner-controlled paths are opened here.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import resource
import secrets
import stat
import subprocess
import sys

ROOT = Path("/var/lib/framebridge-expanded/credentials")
SUDOERS = Path("/etc/sudoers.d")
LAUNCHER = Path("/usr/lib/framebridge-expanded/bin/framebridge-credential-helper")
CODE = Path("/usr/lib/framebridge-expanded/expanded/framebridge_expanded/credential_helper.py")
CREDS = "/usr/bin/systemd-creds"
VISUDO = "/usr/sbin/visudo"
HEX = re.compile(r"[0-9a-f]{64}\Z")
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\$?\Z")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
MAX_KEY = 16384
MAX_CIPHER = 131072


def _fail():
    raise RuntimeError("The protected credential operation could not be verified.")


def _root_path(path, *, directory=False, private=False):
    """Check every path component; root-owned parents cannot be swapped by users."""
    path = Path(path)
    if not path.is_absolute():
        _fail()
    for item in reversed((path, *path.parents)):
        info = item.lstat()
        last = item == path
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            _fail()
        if not last or directory:
            if not stat.S_ISDIR(info.st_mode):
                _fail()
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail()
        if last and private and info.st_mode & 0o077:
            _fail()
    return path


def _installation():
    if os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site:
        _fail()
    if Path(__file__).absolute() != CODE:
        _fail()
    _root_path(CODE)
    _root_path(LAUNCHER)
    _root_path(Path(CREDS))
    _root_path(Path(VISUDO))


def _caller():
    raw = os.environ.get("SUDO_UID", "")
    if not raw.isascii() or not raw.isdecimal() or str(int(raw)) != raw or int(raw) <= 0:
        _fail()
    owner = pwd.getpwuid(int(raw))
    if not USER.fullmatch(owner.pw_name) or os.environ.get("SUDO_USER") != owner.pw_name:
        _fail()
    # sudo sets these identity variables, and generated rules explicitly deny
    # SETENV.  There is deliberately no caller-selected UID command argument.
    if os.environ.get("SUDO_GID") != str(owner.pw_gid):
        _fail()
    return owner


def _run(args, data=None):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=ENV, cwd="/",
                            close_fds=True, timeout=30)
    if result.returncode:
        _fail()
    return result.stdout


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare_root():
    # Only this fixed root-owned tree is created.  No profile or HOME is opened.
    for directory in (ROOT.parent, ROOT):
        _root_path(directory.parent, directory=True)
        try:
            directory.mkdir(mode=0o700)
            _sync(directory.parent)
        except FileExistsError:
            pass
        _root_path(directory, directory=True, private=directory == ROOT)
    _root_path(SUDOERS, directory=True)


def _read(path, maximum=200000, *, policy=False):
    _root_path(path, private=not policy)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & (0o027 if policy else 0o077)):
            _fail()
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            _fail()
        return data
    finally:
        os.close(fd)


def _install(path, data, *, policy=False):
    _root_path(path.parent, directory=True)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if policy:
            temporary.chmod(0o440)
            _run([VISUDO, "-c", "-f", str(temporary)])
        # link is atomic and never overwrites a pre-existing/symlink target.
        os.link(temporary, path, follow_symlinks=False)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
        _sync(path.parent)


def _cleanup_temporaries():
    # Recover only this installer's root-owned, fixed-format staging files.
    patterns = ((ROOT, r"\.[1-9][0-9]*-[0-9a-f]{64}-[0-9a-f]{64}\.json\.[0-9a-f]{24}", False),
                (SUDOERS, r"\.framebridge-credential-[1-9][0-9]*-[0-9a-f]{64}-[0-9a-f]{64}\.[0-9a-f]{24}", True),
                (SUDOERS, r"\.framebridge-credential-authorize-[1-9][0-9]*-[0-9a-f]{64}-(?:host|user)\.[0-9a-f]{24}", True))
    for directory, pattern, policy in patterns:
        removed = False
        for path in directory.iterdir():
            if re.fullmatch(pattern, path.name):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                            or info.st_mode & (0o027 if policy else 0o077)
                            or info.st_nlink not in {1, 2}
                            or info.st_size > (4096 if policy else 200000)):
                        _fail()
                    if info.st_nlink == 2:
                        # A crash after publish/link but before staging unlink.
                        published = directory / path.name[1:].rsplit(".", 1)[0]
                        other = published.lstat()
                        if (not stat.S_ISREG(other.st_mode) or other.st_uid != 0
                                or (other.st_dev, other.st_ino) != (info.st_dev, info.st_ino)):
                            _fail()
                finally:
                    os.close(fd)
                path.unlink(); removed = True
        if removed:
            _sync(directory)


def _ids(profile, digest=None):
    if not isinstance(profile, str) or not HEX.fullmatch(profile):
        _fail()
    if digest is not None and (not isinstance(digest, str) or not HEX.fullmatch(digest)):
        _fail()


def _paths(uid, profile, digest):
    stem = str(uid) + "-" + profile + "-" + digest
    return ROOT / (stem + ".json"), SUDOERS / ("framebridge-credential-" + stem)


def _policy(owner, profile, digest):
    # All fields are allowlisted tokens; no glob, free path, flags or shell.
    return (owner.pw_name + " ALL=(root) NOLOG_INPUT: NOLOG_OUTPUT: NOSETENV: NOPASSWD: " + str(LAUNCHER)
            + " decrypt " + profile + " " + digest + "\n").encode("ascii")


def _authorization(owner, profile, kind):
    _ids(profile)
    if kind not in {"host", "user"}:
        _fail()
    path = SUDOERS / ("framebridge-credential-authorize-" + str(owner.pw_uid) + "-" + profile + "-" + kind)
    if kind == "host":
        command = str(LAUNCHER) + " protect " + profile
    else:
        command = CREDS + " encrypt --uid=" + str(owner.pw_uid) + " --with-key=host --name=chatgpt-runtime-key - -"
    prefix = owner.pw_name + " ALL=(root) NOLOG_INPUT: NOLOG_OUTPUT: NOSETENV: NOPASSWD: "
    cancel = str(LAUNCHER) + " unauthorize " + profile + " " + kind
    return path, (prefix + command + "\n" + prefix + cancel + "\n").encode("ascii")


def authorize(owner, profile, kind):
    """Administrative, no secret input: exact no-I/O-log permission only."""
    path, expected = _authorization(owner, profile, kind)
    if path.exists() or path.is_symlink():
        if _read(path, 4096, policy=True) != expected:
            _fail()
        return
    _install(path, expected, policy=True)


def unauthorize(owner, profile, kind, *, required=False):
    path, expected = _authorization(owner, profile, kind)
    if not path.exists() and not path.is_symlink():
        if required:
            _fail()
        return
    if _read(path, 4096, policy=True) != expected:
        _fail()
    path.unlink()
    _sync(SUDOERS)


def _record(owner, profile, digest, *, require_policy=True):
    record_path, policy_path = _paths(owner.pw_uid, profile, digest)
    value = json.loads(_read(record_path))
    if (type(value) is not dict or value.get("schema") != 1
            or value.get("uid") != owner.pw_uid or value.get("username") != owner.pw_name
            or value.get("home") != owner.pw_dir or value.get("profile") != profile
            or value.get("sha256") != digest):
        _fail()
    name = value.get("name", "")
    if not re.fullmatch(r"framebridge-" + str(owner.pw_uid) + "-" + profile + r"-[0-9a-f]{32}", name):
        _fail()
    try:
        cipher = base64.b64decode(value["cipher"], validate=True)
    except (KeyError, TypeError, ValueError):
        _fail()
    if not 80 <= len(cipher) <= MAX_CIPHER or hashlib.sha256(cipher).hexdigest() != digest:
        _fail()
    if require_policy and _read(policy_path, 4096, policy=True) != _policy(owner, profile, digest):
        _fail()
    return value, cipher, record_path, policy_path


def protect(owner, profile, key):
    _ids(profile)
    if (not key or len(key) > MAX_KEY or any(c < 33 or c > 126 for c in key)):
        _fail()
    name = "framebridge-" + str(owner.pw_uid) + "-" + profile + "-" + secrets.token_hex(16)
    try:
        cipher = _run([CREDS, "encrypt", "--with-key=host", "--name=" + name, "-", "-"], key)
    finally:
        key = b""
    if not 80 <= len(cipher) <= MAX_CIPHER:
        _fail()
    digest = hashlib.sha256(cipher).hexdigest()
    record_path, policy_path = _paths(owner.pw_uid, profile, digest)
    value = {"schema": 1, "uid": owner.pw_uid, "username": owner.pw_name,
             "home": owner.pw_dir, "profile": profile, "name": name,
             "sha256": digest, "cipher": base64.b64encode(cipher).decode("ascii")}
    data = (json.dumps(value, sort_keys=True) + "\n").encode("ascii")
    _install(record_path, data)
    try:
        _install(policy_path, _policy(owner, profile, digest), policy=True)
    except BaseException:
        if policy_path.exists() or policy_path.is_symlink():
            if _read(policy_path, 4096, policy=True) != _policy(owner, profile, digest):
                _fail()
            policy_path.unlink()
            _sync(SUDOERS)
        record_path.unlink()
        _sync(ROOT)
        raise
    return cipher


def decrypt(owner, profile, digest):
    _ids(profile, digest)
    value, cipher, _, _ = _record(owner, profile, digest)
    key = _run([CREDS, "decrypt", "--name=" + value["name"], "-", "-"], cipher)
    if not key or len(key) > MAX_KEY or any(c < 33 or c > 126 for c in key):
        _fail()
    return key


def revoke(owner, profile, digest):
    _ids(profile, digest)
    record_path, policy_path = _paths(owner.pw_uid, profile, digest)
    if record_path.exists() or record_path.is_symlink():
        _record(owner, profile, digest, require_policy=False)
    if policy_path.exists() or policy_path.is_symlink():
        if _read(policy_path, 4096, policy=True) != _policy(owner, profile, digest):
            _fail()
    # Remove authorization first.  A failure cannot leave a decryption grant to
    # a missing/replaced record.  The lock excludes concurrent decrypt/rotation.
    policy_path.unlink(missing_ok=True)
    _sync(SUDOERS)
    record_path.unlink(missing_ok=True)
    _sync(ROOT)


def _keep_digests(value):
    values = [] if value == "-" else value.split(",")
    if len(values) > 128 or any(not HEX.fullmatch(v) for v in values):
        _fail()
    return set(values)


def prune(owner, profile, keep):
    """Administrative crash recovery: remove only this owner's profile grants."""
    _ids(profile)
    authorizations = []
    for kind in ("host", "user"):
        path, expected = _authorization(owner, profile, kind)
        if path.exists() or path.is_symlink():
            if _read(path, 4096, policy=True) != expected:
                _fail()
            authorizations.append(path)
    digests = set()
    prefix = str(owner.pw_uid) + "-" + profile + "-"
    for path in ROOT.glob(prefix + "*"):
        match = re.fullmatch(re.escape(prefix) + r"([0-9a-f]{64})\.json", path.name)
        if not match:
            _fail()
        digest = match.group(1)
        _record(owner, profile, digest, require_policy=False)
        digests.add(digest)
    for path in SUDOERS.glob("framebridge-credential-" + prefix + "*"):
        digest = path.name.removeprefix("framebridge-credential-" + prefix)
        _ids(profile, digest)
        if _read(path, 4096, policy=True) != _policy(owner, profile, digest):
            _fail()
        digests.add(digest)
    # Prevalidation above completes before any grant or encrypted record moves.
    for digest in sorted(digests - keep):
        revoke(owner, profile, digest)
    for path in authorizations:
        path.unlink()
    if authorizations:
        _sync(SUDOERS)


def purge_all():
    """Administrative uninstall cleanup, not permitted by decrypt sudo rules."""
    _cleanup_temporaries()
    entries = []
    for path in sorted(ROOT.iterdir()):
        if path.name == ".lock":
            continue
        match = re.fullmatch(r"([1-9][0-9]*)-([0-9a-f]{64})-([0-9a-f]{64})\.json", path.name)
        if not match:
            _fail()
        uid, profile, digest = match.groups()
        # A removed/reused account must not prevent revocation.  Validate the
        # original principal from the root-owned record without trusting passwd.
        value = json.loads(_read(path))
        if type(value) is not dict or not USER.fullmatch(value.get("username", "")):
            _fail()
        owner = type("Owner", (), {"pw_uid": int(uid), "pw_name": value["username"],
                                    "pw_dir": value.get("home")})()
        _, _, record_path, policy_path = _record(owner, profile, digest, require_policy=False)
        if policy_path.exists() or policy_path.is_symlink():
            if _read(policy_path, 4096, policy=True) != _policy(owner, profile, digest):
                _fail()
        entries.append((record_path, policy_path))
    policies = []
    for policy_path in sorted(SUDOERS.glob("framebridge-credential-*")):
        auth = re.fullmatch(r"framebridge-credential-authorize-([1-9][0-9]*)-([0-9a-f]{64})-(host|user)", policy_path.name)
        if auth:
            uid, profile, kind = auth.groups()
            data = _read(policy_path, 4096, policy=True)
            username = data.split(b" ", 1)[0].decode("ascii")
            if not USER.fullmatch(username):
                _fail()
            owner = type("Owner", (), {"pw_uid": int(uid), "pw_name": username})()
            if data != _authorization(owner, profile, kind)[1]:
                _fail()
            policies.append(policy_path)
            continue
        match = re.fullmatch(r"framebridge-credential-([1-9][0-9]*)-([0-9a-f]{64})-([0-9a-f]{64})", policy_path.name)
        if not match:
            _fail()
        uid, profile, digest = match.groups()
        data = _read(policy_path, 4096, policy=True)
        username = data.split(b" ", 1)[0].decode("ascii")
        if not USER.fullmatch(username):
            _fail()
        owner = type("Owner", (), {"pw_name": username})()
        if data != _policy(owner, profile, digest):
            _fail()
        policies.append(policy_path)
    # Prevalidate every record and policy before removing any of them.
    for policy_path in policies:
        policy_path.unlink()
    _sync(SUDOERS)
    for record_path, _ in entries:
        record_path.unlink()
    _sync(ROOT)


@contextmanager
def _registry_lock():
    fd = os.open(ROOT / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
            _fail()
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def main(argv=None):
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        _installation()
        if not args or args[0] not in {"protect", "decrypt", "revoke", "prune", "authorize", "unauthorize", "purge-all"}:
            _fail()
        operation = args[0]
        if len(args) != {"protect": 2, "decrypt": 3, "revoke": 3, "prune": 3, "authorize": 3, "unauthorize": 3, "purge-all": 1}[operation]:
            _fail()
        owner = _caller() if operation != "purge-all" else None
        if operation in {"authorize", "unauthorize"}:
            _ids(args[1])
            if args[2] not in {"host", "user"}:
                _fail()
        elif operation == "prune":
            _ids(args[1]); keep = _keep_digests(args[2])
        elif operation != "purge-all":
            _ids(*args[1:])
        _prepare_root()
        result = b""
        with _registry_lock():
            _cleanup_temporaries()
            if operation == "protect":
                # sudo already selected this exact NOLOG policy. Consume its
                # encryption permission before accepting any key bytes.
                unauthorize(owner, args[1], "host", required=True)
                key = sys.stdin.buffer.read(MAX_KEY + 1)
                result = protect(owner, args[1], key)
                key = b""
            elif operation == "decrypt":
                result = decrypt(owner, args[1], args[2])
            elif operation == "revoke":
                revoke(owner, args[1], args[2])
            elif operation == "prune":
                prune(owner, args[1], keep)
            elif operation == "authorize":
                authorize(owner, args[1], args[2])
            elif operation == "unauthorize":
                unauthorize(owner, args[1], args[2])
            else:
                purge_all()
        # stdout belongs to an unprivileged caller and can block indefinitely.
        # Never hold the shared registry lock while writing or flushing it.
        try:
            if result:
                sys.stdout.buffer.write(result)
                sys.stdout.buffer.flush()
        except BaseException:
            if operation == "protect" and result:
                # A broken receipt pipe must not orphan an authorization.  The
                # lock is reacquired only for this bounded administrative undo.
                with _registry_lock():
                    revoke(owner, args[1], hashlib.sha256(result).hexdigest())
            raise
        return 0
    except (Exception, KeyboardInterrupt):
        # Never export subprocess output, arguments, record contents or traces.
        sys.stderr.write("FrameBridge protected credential operation failed.\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
