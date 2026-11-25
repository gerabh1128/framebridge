"""Regression tests for non-destructive, locked profile-layout preparation.

Only temporary directories and synthetic role names are used.
"""
import errno
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "packaging/framebridge-original"
LINKS = (
    ("home/.local/share/framebridge/fbterm/bin", "fbterm/bin"),
    ("home/.local/share/framebridge/tool-adapter/bin", "tool-adapter/bin"),
    ("home/.local/lib/framebridge-native/host", "framebridge-native/host"),
    ("home/Descargas/framebridge_v0_1/host", "framebridge-native/host"),
)
SENTINEL = b"ordinary test file, must survive unchanged"


class LayoutMigration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fb-layout-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / "package"
        (self.prefix / "bin").mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.prefix / "bin/framebridge-original")
        (self.prefix / "layout.json").write_text(json.dumps({
            "fbterm_bin": "original/workstation/fbterm/bin",
            "adapter_bin": "original/workstation/tool-adapter/bin",
            "native_host_dir": "original/workstation/framebridge-native/host",
            "native_host": "original/workstation/framebridge-native/host/native",
        }))
        for _, suffix in LINKS:
            (self.prefix / "original/workstation" / suffix).mkdir(parents=True, exist_ok=True)
        loader = importlib.machinery.SourceFileLoader(
            "fb_layout_regression", str(self.prefix / "bin/framebridge-original"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.module = importlib.util.module_from_spec(spec)
        loader.exec_module(self.module)
        self.state = self.root / "profile"
        self.home = self.state / "home"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.module.settings = lambda args: (
            self.state, self.home, {"XDG_RUNTIME_DIR": str(self.runtime)})
        self.args = types.SimpleNamespace()

    def old_links(self):
        for rel, suffix in LINKS:
            path = self.state / rel
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.symlink_to(self.prefix / "original/retired-role" / suffix)

    def prepare(self):
        return self.module.prepare(self.args)

    def journals(self):
        return sorted(p for p in self.state.glob("layout-migration-*") if p.is_dir())

    def assert_current_links(self):
        for rel, suffix in LINKS:
            p = self.state / rel
            self.assertTrue(p.is_symlink())
            self.assertEqual(os.readlink(p), str(self.prefix / "original/workstation" / suffix))

    def assert_old_links(self):
        for rel, suffix in LINKS:
            p = self.state / rel
            self.assertTrue(p.is_symlink())
            self.assertEqual(os.readlink(p), str(self.prefix / "original/retired-role" / suffix))

    def test_upgrade_preserves_all_old_entries_and_idempotence(self):
        self.old_links()
        self.prepare()
        self.assert_current_links()
        [journal] = self.journals()
        self.assertTrue((journal / "committed").is_file())
        self.assertEqual(journal.stat().st_mode & 0o777, 0o700)
        manifest = json.loads((journal / "manifest.json").read_text())
        self.assertEqual(len(manifest["entries"]), 4)
        for item in manifest["entries"]:
            saved = journal / item["backup"]
            self.assertTrue(saved.is_symlink())
            self.assertEqual(os.readlink(saved), item["previous"])
        self.prepare()
        self.assertEqual(self.journals(), [journal])

    def test_fresh_install_and_repeat(self):
        self.prepare()
        self.assert_current_links()
        before = self.journals()
        self.prepare()
        self.assertEqual(self.journals(), before)

    def test_partial_legacy_current_layout(self):
        self.old_links()
        rel, suffix = LINKS[0]
        p = self.state / rel
        p.unlink()
        p.symlink_to(self.prefix / "original/workstation" / suffix)
        self.prepare()
        self.assert_current_links()
        [journal] = self.journals()
        self.assertEqual(len(json.loads((journal / "manifest.json").read_text())["entries"]), 3)

    def test_refuses_regular_file_at_preflight(self):
        self.old_links()
        p = self.state / LINKS[0][0]
        p.unlink()
        p.write_bytes(SENTINEL)
        with self.assertRaisesRegex(RuntimeError, "Unexpected runtime entry"):
            self.prepare()
        self.assertEqual(p.read_bytes(), SENTINEL)
        self.assertFalse(p.is_symlink())

    def test_refuses_custom_and_live_legacy_link(self):
        self.old_links()
        p = self.state / LINKS[0][0]
        p.unlink()
        outside = self.root / "outside"
        p.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "Unexpected link"):
            self.prepare()
        self.assertEqual(os.readlink(p), str(outside))
        p.unlink()
        previous = self.prefix / "original/retired-role/fbterm/bin"
        previous.mkdir(parents=True)
        p.symlink_to(previous)
        with self.assertRaisesRegex(RuntimeError, "Unexpected link"):
            self.prepare()
        self.assertEqual(os.readlink(p), str(previous))

    def test_rejects_symlinked_internal_parent(self):
        self.old_links()
        folder = (self.state / LINKS[0][0]).parent
        outside = self.root / "external"
        folder.rename(outside)
        folder.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((RuntimeError, OSError)):
            self.prepare()
        self.assertTrue(folder.is_symlink())
        self.assertTrue((outside / "bin").is_symlink())

    def test_failure_on_second_install_restores_all_links(self):
        self.old_links()
        real = self.module._move_noreplace
        failed = False

        def inject(sfd, src, dfd, dst):
            nonlocal failed
            if src == "new-1" and not failed:
                failed = True
                raise OSError(errno.EIO, "simulated install failure")
            return real(sfd, src, dfd, dst)

        with mock.patch.object(self.module, "_move_noreplace", side_effect=inject):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertTrue(failed)
        self.assert_old_links()
        [journal] = self.journals()
        self.assertTrue((journal / "rolled-back").is_file())
        self.assertFalse((journal / "committed").exists())
        self.prepare()
        self.assert_current_links()

    def test_concurrent_file_before_displacement_is_preserved(self):
        self.old_links()
        p = self.state / LINKS[0][0]
        real = self.module._move_noreplace
        injected = False

        def inject(sfd, src, dfd, dst):
            nonlocal injected
            if dst == "old-0" and not injected:
                injected = True
                p.unlink()
                p.write_bytes(SENTINEL)
            return real(sfd, src, dfd, dst)

        with mock.patch.object(self.module, "_move_noreplace", side_effect=inject):
            with self.assertRaisesRegex(RuntimeError, "Unexpected concurrent"):
                self.prepare()
        self.assertTrue(injected)
        self.assertFalse(p.is_symlink())
        self.assertEqual(p.read_bytes(), SENTINEL)

    def test_concurrent_file_after_displacement_is_preserved_and_blocks(self):
        self.old_links()
        p = self.state / LINKS[0][0]
        old = os.readlink(p)
        real = self.module._move_noreplace
        injected = False

        def inject(sfd, src, dfd, dst):
            nonlocal injected
            if src == "new-0" and not injected:
                injected = True
                p.write_bytes(SENTINEL)
            return real(sfd, src, dfd, dst)

        with mock.patch.object(self.module, "_move_noreplace", side_effect=inject):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertTrue(injected)
        self.assertFalse(p.is_symlink())
        self.assertEqual(p.read_bytes(), SENTINEL)
        [journal] = self.journals()
        self.assertEqual(os.readlink(journal / "old-0"), old)
        self.assertFalse((journal / "committed").exists())
        self.assertFalse((journal / "rolled-back").exists())
        with self.assertRaisesRegex(RuntimeError, "Unfinished layout migration"):
            self.prepare()

    def test_interrupted_journal_blocks_without_destroying_evidence(self):
        self.old_links()
        journal = self.state / ("layout-migration-" + "1" * 32)
        journal.mkdir(mode=0o700)
        (journal / "manifest.json").write_text("{}")
        p = self.state / LINKS[0][0]
        p.rename(journal / "old-0")
        with self.assertRaisesRegex(RuntimeError, "Unfinished layout migration"):
            self.prepare()
        self.assertFalse(p.exists())
        self.assertTrue((journal / "old-0").is_symlink())

    def test_invalid_or_conflicting_completion_marker_blocks(self):
        self.old_links()
        journal = self.state / ("layout-migration-" + "2" * 32)
        journal.mkdir(mode=0o700)
        marker = journal / "committed"
        marker.write_bytes(b"")
        marker.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "Invalid layout completion marker"):
            self.prepare()
        self.assert_old_links()
        marker.write_bytes(b"complete\n")
        other = journal / "rolled-back"
        other.write_bytes(b"restored\n")
        other.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "Unfinished layout migration"):
            self.prepare()
        self.assert_old_links()

    def test_failed_commit_marker_never_destroys_saved_links(self):
        self.old_links()
        real = self.module._write_new

        def fail_marker(fd, name, data):
            if name == "committed":
                raise OSError(errno.EIO, "simulated durable commit failure")
            return real(fd, name, data)

        with mock.patch.object(self.module, "_write_new", side_effect=fail_marker):
            with self.assertRaises(OSError):
                self.prepare()
        self.assert_current_links()
        [journal] = self.journals()
        self.assertTrue((journal / "old-0").is_symlink())
        self.assertFalse((journal / "committed").exists())
        self.assertFalse((journal / "rolled-back").exists())
        with self.assertRaisesRegex(RuntimeError, "Unfinished layout migration"):
            self.prepare()

    def test_exception_after_successful_displacement_restores_original(self):
        self.old_links()
        real = self.module._move_noreplace
        injected = False

        def inject(sfd, src, dfd, dst):
            nonlocal injected
            result = real(sfd, src, dfd, dst)
            if dst == "old-0" and not injected:
                injected = True
                raise OSError(errno.EIO, "failure after successful displacement")
            return result

        with mock.patch.object(self.module, "_move_noreplace", side_effect=inject):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertTrue(injected)
        self.assert_old_links()
        self.assertTrue((self.journals()[0] / "rolled-back").is_file())

    def test_exception_after_successful_install_restores_original(self):
        self.old_links()
        real = self.module._move_noreplace
        injected = False

        def inject(sfd, src, dfd, dst):
            nonlocal injected
            result = real(sfd, src, dfd, dst)
            if src == "new-0" and not injected:
                injected = True
                raise OSError(errno.EIO, "failure after successful install")
            return result

        with mock.patch.object(self.module, "_move_noreplace", side_effect=inject):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertTrue(injected)
        self.assert_old_links()
        self.assertTrue((self.journals()[0] / "rolled-back").is_file())

    def test_profile_lock_blocks_simultaneous_preparation(self):
        self.state.mkdir(mode=0o700)
        fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.module._layout_lock(fd):
            # Advance the timeout deterministically without waiting 10 seconds.
            with mock.patch.object(self.module.time, "monotonic", side_effect=[0, 11]):
                with self.assertRaisesRegex(RuntimeError, "Another layout preparation"):
                    self.prepare()
        self.prepare()
        self.assert_current_links()


if __name__ == "__main__":
    unittest.main()
