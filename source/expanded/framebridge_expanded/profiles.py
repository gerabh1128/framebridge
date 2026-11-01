"""Explicit, private default-profile references; never discover credentials."""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import pwd
import stat
import tempfile
import uuid
from contextlib import contextmanager

MAXIMUM = 8192
_UNSPECIFIED_IDENTITY = object()


class ProfileSelectionError(ValueError):
    """A preferred profile is unavailable; do not silently select another."""


class _InvalidReferenceContent(ProfileSelectionError):
    """Only explicit reference replacement/removal may recover this error."""


def _need(value, message):
    if not value:
        raise ProfileSelectionError(message)


def _home():
    # Runtime jobs deliberately use an isolated HOME. Preferences belong to
    # the actual Linux account, not that job's synthetic home or XDG directory.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _location(create=False):
    home = _home()
    _need(home.is_absolute(), "The account home directory is unavailable.")
    for path in (home, *home.parents):
        _need(not path.is_symlink(), "The account home directory uses an unsafe symbolic link.")
    info = home.lstat()
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
          and not info.st_mode & 0o022, "The account home directory is unsafe.")
    config = home / ".config"
    directory = config / "framebridge-profiles"
    for path in (config, directory):
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                return directory / "default.json"
            path.mkdir(mode=0o700)
            info = path.lstat()
        forbidden = 0o077 if path == directory else 0o022
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
              and not info.st_mode & forbidden,
              "The default-profile directory is unsafe; its permissions were not changed.")
    return directory / "default.json"


def _read():
    path = _location()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
              and info.st_nlink == 1 and not info.st_mode & 0o077,
              "The default-profile reference is unsafe.")
        if info.st_size > MAXIMUM:
            raise _InvalidReferenceContent("The default-profile reference is too large.")
        raw = os.read(fd, MAXIMUM + 1)
        if len(raw) > MAXIMUM:
            raise _InvalidReferenceContent("The default-profile reference is too large.")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise _InvalidReferenceContent("The default-profile reference is invalid.") from None
        if not (type(value) is dict and type(value.get("schema_version")) is int
                and value["schema_version"] == 1):
            raise _InvalidReferenceContent("The default-profile reference format is unsupported.")
        if (set(value) == {"schema_version", "ambiguous"}
                and value["ambiguous"] is True):
            return value
        if (set(value) != {"schema_version", "state_dir", "node_id"}
                or type(value["state_dir"]) is not str or not value["state_dir"]):
            raise _InvalidReferenceContent("The default-profile reference is invalid.")
        identity = value["node_id"]
        try:
            canonical_identity = str(uuid.UUID(identity)) if type(identity) is str else None
        except ValueError:
            canonical_identity = None
        if canonical_identity is None or identity != canonical_identity:
            raise _InvalidReferenceContent("The default-profile reference identity must be a canonical UUID string.")
        return value
    finally:
        os.close(fd)


def _profile(state, identity=_UNSPECIFIED_IDENTITY):
    from .config import load_config
    _need(isinstance(state, (str, Path)) and bool(str(state)), "Choose a configured profile directory.")
    path = Path(state).expanduser()
    _need(path.is_absolute() and str(path) == os.path.normpath(str(path))
          and not any(ord(c) < 32 for c in str(path)), "The preferred profile path is invalid.")
    for item in (path, *path.parents):
        _need(not item.is_symlink(), "The preferred profile path uses a symbolic link.")
    try:
        info = path.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
              and not info.st_mode & 0o077, "The preferred profile directory is unsafe.")
        config = load_config(path)
        node_id = str(uuid.UUID(config["node_id"]))
    except (OSError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, ProfileSelectionError):
            raise
        raise ProfileSelectionError("The preferred profile is missing or invalid. Use fb --state-dir PROFILE profile select.") from None
    _need(identity is _UNSPECIFIED_IDENTITY or identity == node_id,
          "The preferred profile identity changed. Select the intended profile explicitly.")
    return path, node_id


def selected():
    value = _read()
    if value is None:
        return None
    _need(not value.get("ambiguous"),
          "Multiple managed profiles require an explicit choice. Use fb --state-dir PROFILE profile select.")
    return _profile(value.get("state_dir"), value.get("node_id"))[0]


@contextmanager
def _selection_lock(path):
    lock = os.open(path.parent / ".selection.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(lock)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
              and info.st_nlink == 1 and not info.st_mode & 0o077,
              "The default-profile selection lock is unsafe.")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock)


def _write(value, *, preserve_existing=False):
    path = _location(create=True)
    with _selection_lock(path):
        # Recheck under the shared writer lock. An upgrade may not replace an
        # explicit user selection made after its initial absence check.
        try:
            current = _read()
        except _InvalidReferenceContent:
            if preserve_existing:
                raise
            # Explicit selection may replace malformed bytes only after _read
            # has verified the file's owner, mode, type and sole link.
            current = None
        if preserve_existing and current is not None:
            return False
        fd, temporary = tempfile.mkstemp(prefix=".default-profile-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True


def select(state):
    path, identity = _profile(state)
    _write({"schema_version": 1, "state_dir": str(path), "node_id": identity})
    return {"ok": True, "selected": True, "state_dir": str(path), "node_id": identity}


def show():
    path = selected()
    return {"ok": True, "selected": path is not None, "state_dir": str(path) if path is not None else None}


def clear():
    path = _location()
    removed = False
    if path.parent.exists():
        with _selection_lock(path):
            try:
                value = _read()
                removed = value is not None
            except _InvalidReferenceContent:
                # Clear removes only this metadata-verified reference; neither
                # malformed JSON nor schema errors authorize unsafe-file repair.
                removed = True
            if removed:
                path.unlink()
    return {"ok": True, "selected": False, "reference_removed": removed,
            "profiles_and_credentials_retained": True}


def _preserved(current):
    _need(current is not None, "The default profile changed during migration; retry the reference check.")
    if not current.get("ambiguous"):
        _profile(current.get("state_dir"), current.get("node_id"))
    return {"ok": True, "selected": not current.get("ambiguous", False),
            "existing_reference_preserved": True, "ambiguous": current.get("ambiguous", False)}


def migrate(state, *, ambiguous=False):
    """Use only already-validated package-upgrade capture, never a directory scan."""
    current = _read()
    if current is not None:
        return _preserved(current)
    if ambiguous:
        written = _write({"schema_version": 1, "ambiguous": True}, preserve_existing=True)
        if not written:
            return _preserved(_read())
        return {"ok": True, "selected": False, "ambiguous": True,
                "message": "Choose the intended profile with fb --state-dir PROFILE profile select."}
    path, identity = _profile(state)
    written = _write({"schema_version": 1, "state_dir": str(path), "node_id": identity}, preserve_existing=True)
    if not written:
        return _preserved(_read())
    return {"ok": True, "selected": True, "state_dir": str(path), "node_id": identity}
