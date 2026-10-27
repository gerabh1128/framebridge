"""Synthetic, unprivileged tests; never run sudo, visudo or systemd-creds."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from framebridge_expanded import credential_helper as helper

KEY = b"SYNTHETIC-FRAMEBRIDGE-TEST-NOT-A-REAL-KEY"
PROFILE = "a" * 64


class CredentialHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.policies = self.root / "sudoers"
        self.registry.mkdir(mode=0o700)
        self.policies.mkdir(mode=0o700)
        self.owner = SimpleNamespace(pw_uid=1234, pw_gid=1234, pw_name="testowner", pw_dir="/home/testowner")
        self.calls = []
        self.generation = 0
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(helper, "ROOT", self.registry).start()
        mock.patch.object(helper, "SUDOERS", self.policies).start()
        # Functional tests run as an ordinary user. Trust-path guards have
        # dedicated stat-driven tests below rather than requiring real root.
        mock.patch.object(helper, "_root_path", side_effect=lambda p, **kw: Path(p)).start()
        original_fstat = os.fstat
        def root_stat(fd):
            values = list(original_fstat(fd)); values[4] = 0
            return os.stat_result(values)
        mock.patch.object(helper.os, "fstat", side_effect=root_stat).start()
        self.run_patcher = mock.patch.object(helper, "_run", side_effect=self.fake_run)
        self.run = self.run_patcher.start()

    def fake_run(self, args, data=None):
        self.calls.append((args, data))
        if args[1] == "encrypt":
            self.generation += 1
            return ("SYNTHETIC-CIPHERTEXT-%03d-" % self.generation).encode() * 6
        if args[1] == "decrypt":
            return KEY
        return b""

    def protect(self):
        cipher = helper.protect(self.owner, PROFILE, KEY)
        return cipher, hashlib.sha256(cipher).hexdigest()

    def test_protect_has_literal_owner_rule_and_no_plaintext_in_files_or_argv(self):
        cipher, digest = self.protect()
        record, policy = helper._paths(self.owner.pw_uid, PROFILE, digest)
        self.assertEqual(0o600, stat.S_IMODE(record.stat().st_mode))
        self.assertEqual(0o440, stat.S_IMODE(policy.stat().st_mode))
        text = policy.read_text()
        self.assertIn("testowner ALL=(root) NOLOG_INPUT: NOLOG_OUTPUT: NOSETENV: NOPASSWD:", text)
        self.assertTrue(text.endswith(" decrypt " + PROFILE + " " + digest + "\n"))
        self.assertNotIn("*", text)
        self.assertNotIn("systemd-creds", text)
        for path in (record, policy):
            self.assertNotIn(KEY, path.read_bytes())
        encryption = self.calls[0]
        self.assertEqual(helper.CREDS, encryption[0][0])
        self.assertEqual(KEY, encryption[1])
        self.assertIn("--with-key=host", encryption[0])
        self.assertFalse(any(a.startswith("--uid") or a == "--user" for a in encryption[0]))
        self.assertNotIn(KEY.decode(), " ".join(encryption[0]))
        self.assertEqual(KEY, helper.decrypt(self.owner, PROFILE, digest))

    def test_wrong_owner_or_profile_never_decrypts(self):
        _, digest = self.protect()
        self.calls.clear()
        other = SimpleNamespace(pw_uid=5678, pw_gid=5678, pw_name="another", pw_dir="/home/another")
        for owner, profile in ((other, PROFILE), (self.owner, "b" * 64)):
            with self.subTest(owner=owner.pw_uid, profile=profile):
                with self.assertRaises(Exception):
                    helper.decrypt(owner, profile, digest)
        self.assertEqual([], self.calls)

    def test_reused_uid_with_different_username_or_home_is_rejected(self):
        _, digest = self.protect()
        for field in ("pw_name", "pw_dir"):
            values = vars(self.owner).copy(); values[field] = "changed"
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                helper.decrypt(SimpleNamespace(**values), PROFILE, digest)

    def test_cipher_or_name_or_principal_tampering_is_rejected_before_decrypt(self):
        _, digest = self.protect()
        record, _ = helper._paths(self.owner.pw_uid, PROFILE, digest)
        original = json.loads(record.read_bytes())
        for field, value in (("cipher", "AAAA"), ("name", "another-secret"), ("uid", 0), ("profile", "b" * 64)):
            changed = dict(original); changed[field] = value
            record.write_text(json.dumps(changed)); self.calls.clear()
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                helper.decrypt(self.owner, PROFILE, digest)
            self.assertEqual([], self.calls)

    def test_symlink_hardlink_and_broad_permissions_are_rejected(self):
        _, digest = self.protect()
        record, _ = helper._paths(self.owner.pw_uid, PROFILE, digest)
        backup = record.with_suffix(".backup"); record.rename(backup)
        record.symlink_to(backup)
        with self.assertRaises(OSError): helper.decrypt(self.owner, PROFILE, digest)
        record.unlink(); os.link(backup, record)
        with self.assertRaises(RuntimeError): helper.decrypt(self.owner, PROFILE, digest)
        record.unlink(); backup.rename(record); record.chmod(0o644)
        with self.assertRaises(RuntimeError): helper.decrypt(self.owner, PROFILE, digest)

    def test_invalid_policy_does_not_grant_or_leave_a_record(self):
        def fail_visudo(args, data=None):
            if args[0] == helper.VISUDO: raise RuntimeError("invalid policy")
            return self.fake_run(args, data)
        self.run.side_effect = fail_visudo
        with self.assertRaises(RuntimeError): self.protect()
        self.assertEqual([], list(self.registry.iterdir()))
        self.assertEqual([], list(self.policies.iterdir()))

    def test_rotation_revoke_and_idempotent_partial_cleanup(self):
        _, first = self.protect(); _, second = self.protect()
        self.assertNotEqual(first, second)
        helper.revoke(self.owner, PROFILE, first)
        helper.revoke(self.owner, PROFILE, first)
        self.assertEqual(KEY, helper.decrypt(self.owner, PROFILE, second))
        _, policy = helper._paths(self.owner.pw_uid, PROFILE, second)
        policy.unlink()  # simulate interruption after authorization removal
        helper.revoke(self.owner, PROFILE, second)
        self.assertEqual([], list(self.registry.iterdir()))
        self.assertEqual([], list(self.policies.iterdir()))

    def test_bootstrap_has_only_literal_nolog_commands_and_host_grant_is_one_use(self):
        for kind in ("host", "user"):
            helper.authorize(self.owner, PROFILE, kind)
            path, expected = helper._authorization(self.owner, PROFILE, kind)
            self.assertEqual(expected, path.read_bytes())
            self.assertIn(b"NOLOG_INPUT: NOLOG_OUTPUT: NOSETENV: NOPASSWD:", expected)
            self.assertNotIn(b"*", expected)
            self.assertNotIn(KEY, expected)
            if kind == "user":
                self.assertIn(b"encrypt --uid=1234 --with-key=host --name=chatgpt-runtime-key - -", expected)
            helper.unauthorize(self.owner, PROFILE, kind, required=True)
            with self.assertRaises(RuntimeError):
                helper.unauthorize(self.owner, PROFILE, kind, required=True)

    def test_prune_and_purge_remove_interrupted_authorizations(self):
        helper.authorize(self.owner, PROFILE, "host")
        helper.authorize(self.owner, PROFILE, "user")
        helper.prune(self.owner, PROFILE, set())
        self.assertEqual([], list(self.policies.iterdir()))
        helper.authorize(self.owner, PROFILE, "user")
        helper.purge_all()
        self.assertEqual([], list(self.policies.iterdir()))

    def test_purge_prevalidates_all_policies_before_deleting_any(self):
        _, first = self.protect(); _, second = self.protect()
        record, policy = helper._paths(self.owner.pw_uid, PROFILE, second)
        policy.chmod(0o600); policy.write_text("unrelated rule\n")
        with self.assertRaises(RuntimeError): helper.purge_all()
        self.assertTrue(helper._paths(self.owner.pw_uid, PROFILE, first)[0].exists())
        self.assertTrue(record.exists())

    def test_purge_handles_orphan_rule_after_interrupted_revoke(self):
        _, digest = self.protect()
        record, policy = helper._paths(self.owner.pw_uid, PROFILE, digest)
        record.unlink()
        helper.purge_all()
        self.assertFalse(policy.exists())

    def test_prune_recovers_lost_protect_receipt_and_preserves_committed_cipher(self):
        _, current = self.protect(); _, lost_receipt = self.protect()
        helper.prune(self.owner, PROFILE, {current})
        self.assertEqual(KEY, helper.decrypt(self.owner, PROFILE, current))
        self.assertFalse(helper._paths(self.owner.pw_uid, PROFILE, lost_receipt)[0].exists())
        helper.prune(self.owner, PROFILE, set())
        self.assertEqual([], list(self.registry.iterdir()))
        self.assertEqual([], list(self.policies.iterdir()))

    def test_revoke_persists_authorization_removal_before_deleting_cipher(self):
        _, digest = self.protect()
        record, policy = helper._paths(self.owner.pw_uid, PROFILE, digest)
        observed = []
        with mock.patch.object(helper, "_sync", side_effect=lambda p: observed.append((p, record.exists(), policy.exists()))):
            helper.revoke(self.owner, PROFILE, digest)
        self.assertEqual([(self.policies, True, False), (self.registry, False, False)], observed)

    def test_purge_recovers_only_recognized_root_owned_staging_files(self):
        _, digest = self.protect()
        record, _ = helper._paths(self.owner.pw_uid, PROFILE, digest)
        temporary = record.with_name("." + record.name + "." + "d" * 24)
        temporary.write_bytes(b"incomplete-staging-json"); temporary.chmod(0o600)
        helper.purge_all()
        self.assertFalse(temporary.exists())
        self.assertEqual([], list(self.registry.iterdir()))

    def test_staging_hardlink_after_publish_is_recovered_before_reading_record(self):
        _, digest = self.protect()
        record, _ = helper._paths(self.owner.pw_uid, PROFILE, digest)
        temporary = record.with_name("." + record.name + "." + "d" * 24)
        os.link(record, temporary)
        original_lstat = Path.lstat
        def root_lstat(path):
            values = list(original_lstat(path)); values[4] = 0
            return os.stat_result(values)
        with mock.patch.object(Path, "lstat", root_lstat):
            helper._cleanup_temporaries()
        self.assertFalse(temporary.exists())
        self.assertEqual(KEY, helper.decrypt(self.owner, PROFILE, digest))

    def test_broken_receipt_pipe_rolls_back_published_protect_grant(self):
        helper.authorize(self.owner, PROFILE, "host")
        stdout = mock.Mock(); stdout.buffer.write.side_effect = BrokenPipeError("synthetic closed pipe")
        with mock.patch.object(helper, "_installation"), mock.patch.object(helper, "_caller", return_value=self.owner), mock.patch.object(helper, "_prepare_root"), mock.patch.object(helper.resource, "setrlimit"), mock.patch.object(helper.os, "umask"), mock.patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(KEY))), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(1, helper.main(["protect", PROFILE]))
            self.assertNotIn(KEY.decode(), stderr.getvalue())
        self.assertEqual([".lock"], [p.name for p in self.registry.iterdir()])
        self.assertEqual([], list(self.policies.iterdir()))

    def test_ids_reject_paths_flags_and_extra_arguments(self):
        for value in ("../secret", "--uid=0", "A" * 64, "a" * 63, PROFILE + "\n"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                helper.protect(self.owner, value, KEY)
        for key in (b"", b"space key", b"\x00", b"a" * (helper.MAX_KEY + 1)):
            with self.subTest(size=len(key)), self.assertRaises(RuntimeError):
                helper.protect(self.owner, PROFILE, key)
        self.assertEqual([], self.calls)

    def test_caller_is_derived_from_sudo_identity_not_a_uid_argument(self):
        with mock.patch.object(helper.pwd, "getpwuid", return_value=self.owner):
            with mock.patch.dict(os.environ, {"SUDO_UID": "1234", "SUDO_GID": "1234", "SUDO_USER": "testowner"}, clear=True):
                self.assertEqual(self.owner, helper._caller())
            for bad in ({}, {"SUDO_UID": "0"}, {"SUDO_UID": "01234"}, {"SUDO_UID": "1234", "SUDO_GID": "1234", "SUDO_USER": "another"}):
                with mock.patch.dict(os.environ, bad, clear=True), self.assertRaises(RuntimeError):
                    helper._caller()

    def test_subprocess_has_fixed_environment_and_no_shell(self):
        self.run_patcher.stop()
        with mock.patch.object(helper.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"cipher", b"")) as run:
            self.assertEqual(b"cipher", helper._run([helper.CREDS, "encrypt", "-", "-"], KEY))
        _, kwargs = run.call_args
        self.assertEqual(helper.ENV, kwargs["env"])
        self.assertEqual("/", kwargs["cwd"])
        self.assertTrue(kwargs["close_fds"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn(KEY.decode(), repr(kwargs["env"]))

    def test_main_redacts_failures_and_rejects_extra_arguments(self):
        with mock.patch.object(helper, "_installation"), mock.patch.object(helper.resource, "setrlimit"), mock.patch.object(helper.os, "umask"), mock.patch("sys.stderr", new_callable=io.StringIO) as stderr, mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(1, helper.main(["decrypt", PROFILE, "b" * 64, "--uid=0"]))
            self.assertEqual("", stdout.getvalue())
            self.assertNotIn(KEY.decode(), stderr.getvalue())


class CredentialTrustGuardTests(unittest.TestCase):
    def test_root_path_rejects_each_untrusted_component(self):
        target = Path("/rootowned/helper")
        def info(mode, uid=0, links=1):
            return SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=links)
        for bad in (info(stat.S_IFDIR | 0o777), info(stat.S_IFDIR | 0o755, uid=1000), info(stat.S_IFLNK | 0o777)):
            def fake(path):
                if path == Path("/rootowned"): return bad
                return info((stat.S_IFREG | 0o755) if path == target else (stat.S_IFDIR | 0o755))
            with mock.patch.object(Path, "lstat", fake), self.assertRaises(RuntimeError):
                helper._root_path(target)

    def test_installed_helper_requires_isolated_no_site_interpreter(self):
        with mock.patch.object(helper.os, "geteuid", return_value=0):
            for isolated, no_site in ((0, 1), (1, 0)):
                with mock.patch.object(helper.sys, "flags", SimpleNamespace(isolated=isolated, no_site=no_site)), self.assertRaises(RuntimeError):
                    helper._installation()


if __name__ == "__main__":
    unittest.main()
