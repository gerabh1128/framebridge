"""Explicit, persistent, read-only fb0 access for one local account.

No permission changes occur on import, status, or package installation.
The administrator explicitly opts in with sudo fb screen-access enable --user NAME.
The selected UID can read framebuffer pixels even after logout or a VT switch.
Disable restores only this operation's exact unchanged ACL and retains backups.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import struct
import subprocess
import sys
import time
import uuid

DEVICE = Path("/dev/fb0")
POLICY = Path("/etc/framebridge-expanded/screen-access.json")
STATE = Path("/var/lib/framebridge-expanded/screen-access")
RULE = Path("/usr/lib/udev/rules.d/70-framebridge-screen.rules")
XATTR = "system.posix_acl_access"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
RISK = ("The selected account can read real /dev/fb0 pixels through its processes "
        "after logout or a VT switch. No framebuffer write permission is granted.")
UNDEFINED = 0xffffffff


def require(value, code):
    if not value:
        raise RuntimeError(code)


def protected_stat(info, *, directory=False, private=False):
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            "PROTECTED_PATH_TYPE_INVALID")
    require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022,
            "PROTECTED_PATH_OWNERSHIP_INVALID")
    if private:
        require(not stat.S_IMODE(info.st_mode) & 0o077, "PRIVATE_PATH_MODE_INVALID")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        protected_stat(os.fstat(fd), directory=True)
        os.fsync(fd)
    finally:
        os.close(fd)


def secure_directory(path, *, create=False, private=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "PROTECTED_PATH_INVALID")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            require(create, "PROTECTED_DIRECTORY_MISSING")
            os.mkdir(current, 0o700 if current == path and private else 0o755)
            sync_directory(current.parent)
            info = current.lstat()
        protected_stat(info, directory=True, private=private and current == path)
    return path


def read_json(path, *, private=False):
    secure_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        protected_stat(os.fstat(fd), private=private)
        data = os.read(fd, 65537)
        require(len(data) <= 65536, "PROTECTED_RECORD_TOO_LARGE")
        value = json.loads(data)
        require(isinstance(value, dict), "PROTECTED_RECORD_INVALID")
        return value
    finally:
        os.close(fd)


def write_json(path, value, *, private=True, exclusive=False):
    secure_directory(path.parent, create=True, private=path.parent == STATE)
    if path.exists():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            protected_stat(os.fstat(fd), private=private)
        finally:
            os.close(fd)
        require(not exclusive, "PROTECTED_RECORD_EXISTS")
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600 if private else 0o644)
    try:
        payload = (json.dumps(value, sort_keys=True) + "\n").encode()
        os.fchmod(fd, 0o600 if private else 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def locked():
    require(os.geteuid() == 0, "ROOT_REQUIRED_USE_SUDO")
    secure_directory(STATE, create=True, private=True)
    fd = os.open(STATE / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        protected_stat(os.fstat(fd), private=True)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def account(name, expected_uid=None):
    require(isinstance(name, str) and 0 < len(name) <= 256
            and not any(c.isspace() or ord(c) < 32 for c in name), "ACCOUNT_INVALID")
    try:
        user = pwd.getpwnam(name)
    except KeyError:
        raise RuntimeError("ACCOUNT_NOT_FOUND") from None
    require(user.pw_uid > 0, "ROOT_ACCOUNT_NOT_ALLOWED")
    require(expected_uid is None or user.pw_uid == expected_uid, "ACCOUNT_IDENTITY_CHANGED")
    return {"user": user.pw_name, "uid": user.pw_uid}


def validate_policy(value):
    require(value.get("schema") == 1 and type(value.get("enabled")) is bool
            and value.get("device") == str(DEVICE) and type(value.get("uid")) is int
            and value["uid"] > 0 and isinstance(value.get("user"), str)
            and 0 < len(value["user"]) <= 256
            and not any(c.isspace() or ord(c) < 32 for c in value["user"]),
            "SCREEN_ACCESS_POLICY_INVALID")
    # Status/revocation must still work after an account is removed or its name
    # is reused. The live name/UID identity is checked before every grant.
    return value


def load_policy():
    try:
        return validate_policy(read_json(POLICY))
    except FileNotFoundError:
        return None
    except RuntimeError as exc:
        if str(exc) == "PROTECTED_DIRECTORY_MISSING" and not POLICY.parent.exists():
            return None
        raise


def acl_entries(raw, mode):
    if raw is None:
        return [(1, (mode >> 6) & 7, UNDEFINED),
                (4, (mode >> 3) & 7, UNDEFINED), (32, mode & 7, UNDEFINED)]
    require(len(raw) >= 28 and (len(raw) - 4) % 8 == 0
            and struct.unpack("<I", raw[:4])[0] == 2, "ACL_FORMAT_INVALID")
    entries = [struct.unpack("<HHI", raw[i:i + 8]) for i in range(4, len(raw), 8)]
    require(all(tag in (1, 2, 4, 8, 16, 32) and perm <= 7 for tag, perm, ident in entries),
            "ACL_FORMAT_INVALID")
    for tag in (1, 4, 32):
        require(sum(e[0] == tag for e in entries) == 1, "ACL_FORMAT_INVALID")
    require(sum(e[0] == 16 for e in entries) <= 1, "ACL_FORMAT_INVALID")
    require(all(ident == UNDEFINED for tag, perm, ident in entries if tag in (1, 4, 16, 32)),
            "ACL_FORMAT_INVALID")
    for tag in (2, 8):
        ids = [ident for kind, perm, ident in entries if kind == tag]
        require(len(ids) == len(set(ids)) and UNDEFINED not in ids, "ACL_FORMAT_INVALID")
    require(not any(e[0] in (2, 8) for e in entries) or any(e[0] == 16 for e in entries),
            "ACL_MASK_MISSING")
    return entries


def read_only_acl(raw, mode, uid):
    entries = acl_entries(raw, mode)
    mask = next((perm for tag, perm, ident in entries if tag == 16),
                next(perm for tag, perm, ident in entries if tag == 4))
    require(mask & 4, "ACL_MASK_HAS_NO_READ_REVIEW_REQUIRED")
    existing = [perm for tag, perm, ident in entries if tag == 2 and ident == uid]
    require(not existing or existing == [4], "EXISTING_USER_ACL_REVIEW_REQUIRED")
    if existing:
        return raw
    entries.append((2, 4, uid))
    if not any(tag == 16 for tag, perm, ident in entries):
        entries.append((16, mask, UNDEFINED))
    order = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5}
    entries.sort(key=lambda e: (order[e[0]], e[2]))
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)


def device_snapshot(fd):
    info = os.fstat(fd)
    require(stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == 29
            and os.minor(info.st_rdev) == 0 and info.st_uid == 0,
            "FRAMEBUFFER_DEVICE_INVALID")
    try:
        raw = os.getxattr(fd, XATTR)
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise
        raw = None
    meta = {"dev": info.st_dev, "inode": info.st_ino, "rdev": info.st_rdev,
            "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    return meta, raw


def encode(raw):
    return None if raw is None else base64.b64encode(raw).decode("ascii")


def decode(value):
    return None if value is None else base64.b64decode(value, validate=True)


def probe_user(policy):
    code = ("import os,json;fd=os.open('/dev/fb0',os.O_RDONLY|os.O_NOFOLLOW);"
            "os.close(fd);print(json.dumps({'read_open':True,"
            "'write_access':os.access('/dev/fb0',os.W_OK)}))")
    user = pwd.getpwnam(policy["user"])
    require(user.pw_uid == policy["uid"], "ACCOUNT_IDENTITY_CHANGED")
    result = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", code],
                            user=user.pw_uid, group=user.pw_gid,
                            extra_groups=os.getgrouplist(user.pw_name, user.pw_gid),
                            env=ENV, capture_output=True, timeout=8)
    require(result.returncode == 0, "FRAMEBUFFER_READ_PROBE_FAILED")
    require(json.loads(result.stdout) == {"read_open": True, "write_access": False},
            "FRAMEBUFFER_ACCESS_NOT_READ_ONLY")


def load_record():
    try:
        value = read_json(STATE / "active.json", private=True)
    except FileNotFoundError:
        return None
    require(value.get("schema") == 1 and isinstance(value.get("meta"), dict)
            and type(value.get("uid")) is int and value["uid"] > 0, "ACCESS_BACKUP_INVALID")
    meta = value["meta"]
    require(set(meta) == {"dev", "inode", "rdev", "uid", "gid", "mode", "boot"}
            and all(type(meta[k]) is int and meta[k] >= 0
                    for k in ("dev", "inode", "rdev", "uid", "gid", "mode"))
            and meta["rdev"] == os.makedev(29, 0) and meta["uid"] == 0
            and meta["mode"] <= 0o777 and isinstance(meta["boot"], str)
            and 0 < len(meta["boot"]) <= 64, "ACCESS_BACKUP_IDENTITY_INVALID")
    require("before_acl" in value and "expected_acl" in value, "ACCESS_BACKUP_INVALID")
    decode(value["before_acl"])
    decode(value["expected_acl"])
    return value


def same_device(left, right):
    return all(left.get(key) == right.get(key) for key in ("dev", "inode", "rdev", "boot"))


def restore_owned(fd, record):
    meta, raw = device_snapshot(fd)
    if not same_device(meta, record["meta"]):
        return "DEVICE_RECREATED_NO_OLD_GRANT"
    require(meta == record["meta"], "FRAMEBUFFER_METADATA_CHANGED_REVIEW_REQUIRED")
    before, expected = decode(record["before_acl"]), decode(record["expected_acl"])
    if raw == before:
        return "ORIGINAL_ACL_ALREADY_RESTORED"
    require(raw == expected, "FRAMEBUFFER_ACL_CHANGED_REVIEW_REQUIRED")
    if raw != before:
        if before is None:
            os.removexattr(fd, XATTR)
        else:
            os.setxattr(fd, XATTR, before, os.XATTR_REPLACE)
    require(device_snapshot(fd) == (meta, before), "FRAMEBUFFER_RESTORE_VERIFY_FAILED")
    return "ORIGINAL_ACL_RESTORED"


def apply_locked(policy):
    account(policy["user"], policy["uid"])
    try:
        fd = os.open(DEVICE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"status": "ENABLED_WAITING_FOR_FB0", "changed": False}
    try:
        meta, before = device_snapshot(fd)
        previous = load_record()
        if previous and same_device(previous["meta"], meta):
            require(previous["meta"] == meta, "FRAMEBUFFER_METADATA_CHANGED_REVIEW_REQUIRED")
            require(previous["uid"] == policy["uid"], "ACCESS_BACKUP_IDENTITY_CHANGED")
            require(before == decode(previous["expected_acl"]), "FRAMEBUFFER_ACL_CHANGED_REVIEW_REQUIRED")
            probe_user(policy)
            return {"status": "ENABLED_READ_ONLY", "changed": False}
        expected = read_only_acl(before, meta["mode"], policy["uid"])
        record = {"schema": 1, "uid": policy["uid"], "user": policy["user"],
                  "meta": meta, "before_acl": encode(before), "expected_acl": encode(expected)}
        backup = STATE / ("backup-" + str(time.time_ns()) + "-" + uuid.uuid4().hex + ".json")
        write_json(backup, record, exclusive=True)
        write_json(STATE / "active.json", record)
        require(device_snapshot(fd) == (meta, before), "FRAMEBUFFER_CHANGED_BEFORE_APPLY")
        if before != expected:
            os.setxattr(fd, XATTR, expected,
                        os.XATTR_CREATE if before is None else os.XATTR_REPLACE)
        try:
            require(device_snapshot(fd) == (meta, expected), "FRAMEBUFFER_APPLY_VERIFY_FAILED")
            probe_user(policy)
        except Exception:
            restore_owned(fd, record)
            raise
        return {"status": "ENABLED_READ_ONLY", "changed": before != expected}
    finally:
        os.close(fd)


def reload_rules():
    result = subprocess.run(["/usr/bin/udevadm", "control", "--reload-rules"],
                            env=ENV, capture_output=True, timeout=8)
    require(result.returncode == 0, "UDEV_RULE_RELOAD_FAILED")


def status():
    policy = load_policy()
    info = {"enabled": bool(policy and policy["enabled"]), "device": str(DEVICE),
            "persistent": bool(policy and policy["enabled"]), "risk": RISK,
            "status": "DISABLED" if not policy else
                      ("ENABLED" if policy["enabled"] else "DISABLED_REVOKE_PENDING")}
    if policy:
        info.update({"user": policy["user"], "uid": policy["uid"]})
    try:
        info["current_account_can_read"] = os.access(DEVICE, os.R_OK)
        info["current_account_can_write"] = os.access(DEVICE, os.W_OK)
        info["device_present"] = DEVICE.exists()
        info["udev_rule_present"] = RULE.is_file() and not RULE.is_symlink()
    except OSError:
        info["device_present"] = False
    return info


def enable(user):
    chosen = account(user)
    with locked():
        secure_directory(RULE.parent)
        fd = os.open(RULE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            protected_stat(os.fstat(fd))
        finally:
            os.close(fd)
        old = load_policy()
        if old:
            require(old["enabled"] and all(old[k] == chosen[k] for k in ("user", "uid")),
                    "DISABLE_EXISTING_ACCESS_POLICY_FIRST")
            policy = old
        else:
            policy = {"schema": 1, "enabled": True, "device": str(DEVICE), **chosen}
            write_json(POLICY, policy, private=False, exclusive=True)
        try:
            reload_rules()
            answer = apply_locked(policy)
        except Exception:
            # Stop later udev grants. Keep the tombstone and protected backup
            # for an explicit disable/recovery instead of concealing a failure.
            write_json(POLICY, dict(policy, enabled=False), private=False)
            raise
        return dict(answer, persistent=True, user=chosen["user"], uid=chosen["uid"], risk=RISK)


def disable():
    with locked():
        policy = load_policy()
        if policy is None:
            return {"status": "DISABLED", "changed": False, "persistent": False}
        write_json(POLICY, dict(policy, enabled=False), private=False)
        reload_rules()
        record = load_record()
        result = "NO_CURRENT_OWNED_GRANT"
        if record:
            require(record["uid"] == policy["uid"], "ACCESS_BACKUP_IDENTITY_CHANGED")
            try:
                fd = os.open(DEVICE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                result = "DEVICE_ABSENT_NO_OLD_GRANT"
            else:
                try:
                    result = restore_owned(fd, record)
                finally:
                    os.close(fd)
            # Successful revocation must not leave an active record belonging
            # to an earlier enable, even when the device has been recreated.
            (STATE / "active.json").unlink()
            sync_directory(STATE)
        POLICY.unlink()
        sync_directory(POLICY.parent)
        return {"status": "DISABLED", "changed": True, "persistent": False, "restore": result}


def apply_device():
    with locked():
        policy = load_policy()
        if policy is None or not policy["enabled"]:
            return {"status": "DISABLED", "changed": False}
        return apply_locked(policy)


def command(args):
    if args.screen_access_command == "status":
        return status()
    if args.screen_access_command == "enable":
        return enable(args.user)
    if args.screen_access_command == "disable":
        return disable()
    raise ValueError("SCREEN_ACCESS_COMMAND_INVALID")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    item = sub.add_parser("enable")
    item.add_argument("--user", required=True)
    sub.add_parser("disable")
    sub.add_parser("status")
    sub.add_parser("apply-device")
    args = parser.parse_args(argv)
    try:
        result = (enable(args.user) if args.action == "enable" else
                  disable() if args.action == "disable" else
                  status() if args.action == "status" else apply_device())
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        # No passwords, environment, process argv, or screen pixels are read.
        print("Error: " + (str(exc) if isinstance(exc, RuntimeError)
                           else "SCREEN_ACCESS_OPERATION_FAILED"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
