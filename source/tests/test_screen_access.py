"""Framebuffer policy regressions: no real device, ACL, udev or privilege changes."""
import contextlib
from contextlib import ExitStack
import io
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest import mock

from framebridge_expanded import cli, screen_access as sa


def acl(entries):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *item) for item in entries)


U = sa.UNDEFINED
BASE = [(1, 6, U), (4, 6, U), (32, 0, U)]
EXTENDED = [(1, 6, U), (2, 7, 1001), (4, 6, U), (8, 2, 99), (16, 6, U), (32, 0, U)]
META = {"dev": 1, "inode": 2, "rdev": 7424, "uid": 0, "gid": 44,
        "mode": 0o660, "boot": "test-boot"}
POLICY = {"schema": 1, "enabled": True, "device": "/dev/fb0", "user": "fbtest", "uid": 1000}


class AclPlanTests(unittest.TestCase):
    def test_plain_mode_adds_only_named_read_and_same_mask(self):
        result = sa.acl_entries(sa.read_only_acl(None, 0o660, 1000), 0o660)
        self.assertEqual(result, [(1, 6, U), (2, 4, 1000), (4, 6, U), (16, 6, U), (32, 0, U)])

    def test_existing_acl_other_users_groups_mask_unchanged(self):
        original = acl(EXTENDED)
        result = sa.acl_entries(sa.read_only_acl(original, 0o660, 1000), 0o660)
        self.assertEqual([e for e in result if e != (2, 4, 1000)], EXTENDED)
        self.assertEqual(next(p for t, p, u in result if t == 16), 6)

    def test_mask_without_read_is_rejected_instead_of_broadened(self):
        raw = acl([(1, 6, U), (2, 7, 1001), (4, 6, U), (16, 2, U), (32, 0, U)])
        with self.assertRaisesRegex(RuntimeError, "ACL_MASK_HAS_NO_READ"):
            sa.read_only_acl(raw, 0o620, 1000)
        with self.assertRaisesRegex(RuntimeError, "ACL_MASK_HAS_NO_READ"):
            sa.read_only_acl(None, 0o600, 1000)

    def test_existing_read_acl_is_unmodified_and_not_claimed(self):
        raw = acl([(1, 6, U), (2, 4, 1000), (4, 6, U), (16, 6, U), (32, 0, U)])
        self.assertEqual(sa.read_only_acl(raw, 0o660, 1000), raw)

    def test_existing_user_write_acl_requires_review(self):
        for permission in (0, 2, 6, 7):
            raw = acl([(1, 6, U), (2, permission, 1000), (4, 6, U), (16, 6, U), (32, 0, U)])
            with self.assertRaisesRegex(RuntimeError, "EXISTING_USER_ACL"):
                sa.read_only_acl(raw, 0o660, 1000)

    def test_corrupt_acl_duplicate_user_and_missing_mask_rejected(self):
        for raw in (b"invalid", acl([(1, 6, U), (4, 6, U), (32, 0, U), (2, 4, 1000)]),
                    acl([(1, 6, U), (2, 4, 1000), (2, 4, 1000), (4, 6, U), (16, 6, U), (32, 0, U)])):
            with self.assertRaises(RuntimeError):
                sa.read_only_acl(raw, 0o660, 1000)


class ProtectedFilesystemTests(unittest.TestCase):
    def test_only_root_owned_regular_nonwritable_files_accepted(self):
        sa.protected_stat(SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0))
        for kind, perm, owner in ((stat.S_IFLNK, 0o777, 0), (stat.S_IFREG, 0o664, 0),
                                  (stat.S_IFREG, 0o644, 1000), (stat.S_IFDIR, 0o755, 0)):
            with self.assertRaises(RuntimeError):
                sa.protected_stat(SimpleNamespace(st_mode=kind | perm, st_uid=owner))

    def test_corrupt_backup_identity_is_refused_without_silently_revoking_record(self):
        record = {"schema": 1, "uid": 1000, "meta": META, "before_acl": None,
                  "expected_acl": sa.encode(sa.read_only_acl(None, 0o660, 1000))}
        with mock.patch.object(sa, "read_json", return_value=record):
            self.assertEqual(sa.load_record(), record)
        for changed in ({k: v for k, v in META.items() if k != "inode"},
                        dict(META, rdev=sa.os.makedev(29, 1)), dict(META, mode=True)):
            with mock.patch.object(sa, "read_json", return_value=dict(record, meta=changed)):
                with self.assertRaisesRegex(RuntimeError, "BACKUP_IDENTITY_INVALID"):
                    sa.load_record()

    def test_private_backup_rejects_world_readability(self):
        with self.assertRaisesRegex(RuntimeError, "PRIVATE_PATH_MODE"):
            sa.protected_stat(SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0), private=True)

    def test_secure_directory_rejects_symlink_ancestor(self):
        root = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        link = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
        with mock.patch.object(Path, "lstat", side_effect=[root, link]):
            with self.assertRaisesRegex(RuntimeError, "PROTECTED_PATH_TYPE"):
                sa.secure_directory(Path("/etc/framebridge-expanded"))

    def test_nonroot_enable_disable_apply_never_open_device(self):
        with mock.patch.object(sa.os, "geteuid", return_value=1000), \
             mock.patch.object(sa, "account", return_value={"user": "fbtest", "uid": 1000}), \
             mock.patch.object(sa.os, "open") as opened:
            for action in (lambda: sa.enable("fbtest"), sa.disable, sa.apply_device):
                with self.assertRaisesRegex(RuntimeError, "ROOT_REQUIRED"):
                    action()
            opened.assert_not_called()

    def test_root_target_account_is_rejected(self):
        with mock.patch.object(sa.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=0, pw_name="root")):
            with self.assertRaisesRegex(RuntimeError, "ROOT_ACCOUNT_NOT_ALLOWED"):
                sa.account("root")

    def test_policy_remains_revocable_after_account_is_removed(self):
        with mock.patch.object(sa.pwd, "getpwnam", side_effect=KeyError("deleted")) as lookup:
            self.assertEqual(sa.validate_policy(POLICY), POLICY)
            lookup.assert_not_called()
        with mock.patch.object(sa.pwd, "getpwnam", side_effect=KeyError("deleted")):
            with self.assertRaisesRegex(RuntimeError, "ACCOUNT_NOT_FOUND"):
                sa.apply_locked(POLICY)

    def test_uid_reuse_name_change_is_rejected(self):
        with mock.patch.object(sa.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1002, pw_name="fbtest")):
            with self.assertRaisesRegex(RuntimeError, "ACCOUNT_IDENTITY_CHANGED"):
                sa.account("fbtest", 1000)


class RestoreTests(unittest.TestCase):
    def record(self, before=None):
        return {"schema": 1, "uid": 1000, "meta": META,
                "before_acl": sa.encode(before), "expected_acl": sa.encode(sa.read_only_acl(before, 0o660, 1000))}

    def test_revoke_plain_acl_restores_absent_xattr(self):
        record = self.record()
        with mock.patch.object(sa, "device_snapshot", side_effect=[(META, sa.decode(record["expected_acl"])), (META, None)]), \
             mock.patch.object(sa.os, "removexattr") as removed, mock.patch.object(sa.os, "setxattr") as written:
            self.assertEqual(sa.restore_owned(9, record), "ORIGINAL_ACL_RESTORED")
            removed.assert_called_once_with(9, sa.XATTR)
            written.assert_not_called()

    def test_revoke_restores_exact_original_extended_acl(self):
        before = acl(EXTENDED)
        record = self.record(before)
        with mock.patch.object(sa, "device_snapshot", side_effect=[(META, sa.decode(record["expected_acl"])), (META, before)]), \
             mock.patch.object(sa.os, "setxattr") as written:
            sa.restore_owned(9, record)
            written.assert_called_once_with(9, sa.XATTR, before, sa.os.XATTR_REPLACE)

    def test_external_acl_change_is_not_overwritten(self):
        with mock.patch.object(sa, "device_snapshot", return_value=(META, b"external")), \
             mock.patch.object(sa.os, "removexattr") as removed, mock.patch.object(sa.os, "setxattr") as written:
            with self.assertRaisesRegex(RuntimeError, "ACL_CHANGED_REVIEW"):
                sa.restore_owned(9, self.record())
            removed.assert_not_called()
            written.assert_not_called()

    def test_chmod_chgrp_or_mask_change_of_same_device_requires_review(self):
        for changed in (dict(META, gid=55), dict(META, mode=0o640), dict(META, uid=1)):
            with mock.patch.object(sa, "device_snapshot", return_value=(changed, sa.decode(self.record()["expected_acl"]))), \
                 mock.patch.object(sa.os, "removexattr") as removed, mock.patch.object(sa.os, "setxattr") as written:
                with self.assertRaisesRegex(RuntimeError, "METADATA_CHANGED_REVIEW"):
                    sa.restore_owned(9, self.record())
                removed.assert_not_called()
                written.assert_not_called()

    def test_recreated_device_is_not_touched_by_old_backup(self):
        with mock.patch.object(sa, "device_snapshot", return_value=(dict(META, boot="new-boot"), None)), \
             mock.patch.object(sa.os, "removexattr") as removed:
            self.assertEqual(sa.restore_owned(9, self.record()), "DEVICE_RECREATED_NO_OLD_GRANT")
            removed.assert_not_called()

    def test_crash_before_acl_write_and_repeated_restore_are_idempotent(self):
        with mock.patch.object(sa, "device_snapshot", return_value=(META, None)), \
             mock.patch.object(sa.os, "removexattr") as removed:
            self.assertEqual(sa.restore_owned(9, self.record()), "ORIGINAL_ACL_ALREADY_RESTORED")
            removed.assert_not_called()


class TransactionTests(unittest.TestCase):
    @contextlib.contextmanager
    def simulated(self, *, initial=None, record=None):
        state = {"acl": initial, "record": record, "writes": []}
        def save(path, value, **kwargs):
            state["writes"].append((path.name, value))
            if path.name == "active.json":
                state["record"] = value
        def set_acl(fd, name, value, flags):
            state["acl"] = value
        def remove_acl(fd, name):
            state["acl"] = None
        with ExitStack() as stack:
            for name, value in [
                ("account", mock.Mock(return_value={"user": "fbtest", "uid": 1000})),
                ("device_snapshot", mock.Mock(side_effect=lambda fd: (META, state["acl"]))),
                ("load_record", mock.Mock(side_effect=lambda: state["record"])),
                ("write_json", mock.Mock(side_effect=save)),
                ("probe_user", mock.Mock()),
            ]:
                state[name] = stack.enter_context(mock.patch.object(sa, name, value))
            state["opened"] = stack.enter_context(mock.patch.object(sa.os, "open", return_value=9))
            stack.enter_context(mock.patch.object(sa.os, "close"))
            state["set"] = stack.enter_context(mock.patch.object(sa.os, "setxattr", side_effect=set_acl))
            state["remove"] = stack.enter_context(mock.patch.object(sa.os, "removexattr", side_effect=remove_acl))
            yield state

    def test_apply_backs_up_before_write_and_remains_read_only(self):
        with self.simulated() as state:
            answer = sa.apply_locked(POLICY)
            self.assertEqual(answer["status"], "ENABLED_READ_ONLY")
            self.assertEqual(state["writes"][0][0][:7], "backup-")
            self.assertEqual(state["writes"][1][0], "active.json")
            self.assertIn((2, 4, 1000), sa.acl_entries(state["acl"], 0o660))
            self.assertEqual(state["set"].call_args.args[-1], sa.os.XATTR_CREATE)

    def test_second_apply_is_idempotent_and_does_not_rebackup(self):
        with self.simulated() as state:
            sa.apply_locked(POLICY)
            state["set"].reset_mock()
            state["writes"].clear()
            self.assertFalse(sa.apply_locked(POLICY)["changed"])
            state["set"].assert_not_called()
            self.assertEqual(state["writes"], [])

    def test_reapply_never_adopts_changed_metadata_as_fresh_baseline(self):
        with self.simulated() as state:
            sa.apply_locked(POLICY)
            state["set"].reset_mock()
            state["writes"].clear()
            state["device_snapshot"].side_effect = lambda fd: (dict(META, mode=0o640), state["acl"])
            with self.assertRaisesRegex(RuntimeError, "METADATA_CHANGED_REVIEW"):
                sa.apply_locked(POLICY)
            state["set"].assert_not_called()
            self.assertEqual(state["writes"], [])

    def test_probe_failure_rolls_back_exact_original_acl(self):
        with self.simulated() as state:
            state["probe_user"].side_effect = RuntimeError("PROBE_TEST_FAILURE")
            with self.assertRaisesRegex(RuntimeError, "PROBE_TEST_FAILURE"):
                sa.apply_locked(POLICY)
            self.assertIsNone(state["acl"])
            state["remove"].assert_called_once()
            self.assertEqual(sa.restore_owned(9, state["record"]), "ORIGINAL_ACL_ALREADY_RESTORED")

    def test_external_acl_change_blocks_reapply(self):
        with self.simulated() as state:
            sa.apply_locked(POLICY)
            state["acl"] = b"external"
            state["set"].reset_mock()
            with self.assertRaisesRegex(RuntimeError, "ACL_CHANGED_REVIEW"):
                sa.apply_locked(POLICY)
            state["set"].assert_not_called()

    def test_disabled_udev_invocation_does_not_open_device(self):
        with mock.patch.object(sa, "locked", contextlib.nullcontext), \
             mock.patch.object(sa, "load_policy", return_value=dict(POLICY, enabled=False)), \
             mock.patch.object(sa, "apply_locked") as apply:
            self.assertEqual(sa.apply_device()["status"], "DISABLED")
            apply.assert_not_called()

    def test_disable_without_policy_is_successful_idempotent(self):
        with mock.patch.object(sa, "locked", contextlib.nullcontext), \
             mock.patch.object(sa, "load_policy", return_value=None), \
             mock.patch.object(sa, "reload_rules") as reload, mock.patch.object(sa.os, "open") as opened:
            self.assertFalse(sa.disable()["changed"])
            reload.assert_not_called()
            opened.assert_not_called()

    def test_disable_conflict_deactivates_policy_preserves_backup_and_tombstone(self):
        record = {"schema": 1, "uid": 1000, "meta": META, "before_acl": None, "expected_acl": "AA=="}
        with mock.patch.object(sa, "locked", contextlib.nullcontext), \
             mock.patch.object(sa, "load_policy", return_value=POLICY), \
             mock.patch.object(sa, "reload_rules"), mock.patch.object(sa, "load_record", return_value=record), \
             mock.patch.object(sa, "write_json") as written, \
             mock.patch.object(sa.os, "open", return_value=9), mock.patch.object(sa.os, "close"), \
             mock.patch.object(sa, "restore_owned", side_effect=RuntimeError("FRAMEBUFFER_ACL_CHANGED_REVIEW_REQUIRED")), \
             mock.patch.object(Path, "unlink") as removed:
            with self.assertRaisesRegex(RuntimeError, "ACL_CHANGED_REVIEW"):
                sa.disable()
            self.assertFalse(written.call_args.args[1]["enabled"])
            removed.assert_not_called()


class ProbeAndStatusTests(unittest.TestCase):
    def test_read_probe_drops_all_privileges_without_pam_or_shell(self):
        user = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="fbtest")
        with mock.patch.object(sa.pwd, "getpwnam", return_value=user), \
             mock.patch.object(sa.os, "getgrouplist", return_value=[1000, 27]), \
             mock.patch.object(sa.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b'{"read_open":true,"write_access":false}')) as run:
            sa.probe_user(POLICY)
            self.assertEqual(run.call_args.kwargs["user"], 1000)
            self.assertEqual(run.call_args.kwargs["group"], 1000)
            self.assertEqual(run.call_args.kwargs["extra_groups"], [1000, 27])
            self.assertEqual(run.call_args.args[0][:3], ["/usr/bin/python3", "-I", "-S"])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_probe_rejects_write_permission(self):
        user = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="fbtest")
        with mock.patch.object(sa.pwd, "getpwnam", return_value=user), \
             mock.patch.object(sa.os, "getgrouplist", return_value=[1000]), \
             mock.patch.object(sa.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b'{"read_open":true,"write_access":true}')):
            with self.assertRaisesRegex(RuntimeError, "ACCESS_NOT_READ_ONLY"):
                sa.probe_user(POLICY)

    def test_status_never_locks_writes_or_changes_permissions(self):
        with mock.patch.object(sa, "load_policy", return_value=POLICY), \
             mock.patch.object(sa, "locked") as lock, \
             mock.patch.object(sa, "write_json") as write, \
             mock.patch.object(sa.os, "setxattr") as aclwrite:
            result = sa.status()
            self.assertTrue(result["enabled"])
            lock.assert_not_called()
            write.assert_not_called()
            aclwrite.assert_not_called()

    def test_invalid_device_is_refused_before_reading_acl(self):
        for mode, dev in ((stat.S_IFREG | 0o660, 0), (stat.S_IFCHR | 0o660, sa.os.makedev(29, 1))):
            info = SimpleNamespace(st_mode=mode, st_rdev=dev, st_uid=0)
            with mock.patch.object(sa.os, "fstat", return_value=info), \
                 mock.patch.object(sa.os, "getxattr") as get:
                with self.assertRaisesRegex(RuntimeError, "FRAMEBUFFER_DEVICE_INVALID"):
                    sa.device_snapshot(9)
                get.assert_not_called()


class CliAndRuleTests(unittest.TestCase):
    def test_status_dispatch_ignores_user_profile_state(self):
        with mock.patch.object(cli, "resolve_state", side_effect=AssertionError("must not resolve profile")), \
             mock.patch.object(cli, "_module", return_value=SimpleNamespace(command=lambda args: {"enabled": False})), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["screen-access", "status"]), 0)

    def test_enable_requires_explicit_account_and_parser_has_no_owner_or_pam_flow(self):
        args = cli.build_parser().parse_args(["screen-access", "enable", "--user", "fbtest"])
        self.assertEqual((args.screen_access_command, args.user), ("enable", "fbtest"))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["screen-access", "enable"])

    def test_rule_matches_only_fb0_and_is_inactive_without_policy(self):
        source = Path(__file__).resolve().parents[1]
        text = (source / "packaging/70-framebridge-screen.rules").read_text()
        lines = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn('SUBSYSTEM=="graphics"', line)
            self.assertIn('KERNEL=="fb0"', line)
            self.assertIn('TEST=="/etc/framebridge-expanded/screen-access.json"', line)
            self.assertIn("framebridge-screen-access apply-device", line)
        self.assertNotIn("uaccess", text)
        self.assertNotIn("video", text)


if __name__ == "__main__":
    unittest.main()
