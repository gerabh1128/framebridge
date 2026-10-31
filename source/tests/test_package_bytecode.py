"""Regress equal-size reproducible upgrades without traversing package links."""
import json
import os
from pathlib import Path
import py_compile
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from build_expanded import bytecode_cleanup_source, maintainer_postinst


class PackageBytecodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = self.root / "installed"
        self.package = self.prefix / "expanded/framebridge_expanded"
        self.package.mkdir(parents=True)
        self.module = self.package / "__init__.py"
        self.module.write_text("__version__ = '0.3.4'\n")
        self.epoch = 1793059200
        os.utime(self.module, (self.epoch, self.epoch))
        self.cache = Path(py_compile.compile(str(self.module), doraise=True))

    def interpreter_version(self):
        code = ("import sys; sys.path.insert(0, "
                + repr(str(self.prefix / "expanded"))
                + "); import framebridge_expanded; print(framebridge_expanded.__version__)")
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def replace_source_with_same_size_and_time(self):
        before = self.module.stat()
        self.module.write_text("__version__ = '0.3.5'\n")
        os.utime(self.module, (before.st_atime, before.st_mtime))
        self.assertEqual(self.module.stat().st_size, before.st_size)
        self.assertEqual(self.module.stat().st_mtime_ns, before.st_mtime_ns)

    def cleanup(self, prefix=None):
        return subprocess.run([sys.executable, "-I", "-S", "-"],
                              input=bytecode_cleanup_source(prefix or self.prefix),
                              capture_output=True, text=True, timeout=10)

    def test_equal_size_timestamp_cache_is_really_stale_then_invalidated(self):
        self.assertEqual(self.interpreter_version(), "0.3.4")
        self.replace_source_with_same_size_and_time()
        # This is the real pre-fix failure, in an independent interpreter.
        self.assertEqual(self.interpreter_version(), "0.3.4")
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FRAMEBRIDGE_BYTECODE_INVALIDATED=1", result.stdout)
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.interpreter_version(), "0.3.5")

    def test_source_native_payload_and_user_profile_are_untouched(self):
        native = self.prefix / "native-extension.so"
        native.write_bytes(b"native-payload")
        profile = self.root / "user-profile/__pycache__"
        profile.mkdir(parents=True)
        outside = profile / "identity.pyc"
        outside.write_bytes(b"user-bytecode")
        source = self.module.read_bytes()
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.module.read_bytes(), source)
        self.assertEqual(native.read_bytes(), b"native-payload")
        self.assertEqual(outside.read_bytes(), b"user-bytecode")

    def test_regular_legacy_pyc_and_pyo_are_invalidated(self):
        legacy = self.prefix / "old.pyc"
        optimized = self.prefix / "old.pyo"
        legacy.write_bytes(b"legacy")
        optimized.write_bytes(b"optimized")
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(legacy.exists())
        self.assertFalse(optimized.exists())

    def test_internal_directory_link_never_traverses_outside_package(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "must-remain.pyc"
        marker.write_bytes(b"outside")
        (self.prefix / "linked-cache").symlink_to(outside, target_is_directory=True)
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_bytes(), b"outside")
        self.assertTrue((self.prefix / "linked-cache").is_symlink())
        self.assertFalse(self.cache.exists())

    def test_cache_file_link_is_not_followed_or_removed(self):
        target = self.root / "outside.pyc"
        target.write_bytes(b"outside")
        link = self.prefix / "cache.pyc"
        link.symlink_to(target)
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"outside")

    def test_prefix_link_is_rejected(self):
        link = self.root / "prefix-link"
        link.symlink_to(self.prefix, target_is_directory=True)
        result = self.cleanup(link)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.cache.exists())

    def test_linked_ancestor_is_rejected(self):
        link = self.root / "ancestor-link"
        link.symlink_to(self.root, target_is_directory=True)
        result = self.cleanup(link / "installed")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.cache.exists())

    def test_group_writable_package_directory_is_rejected(self):
        self.package.chmod(0o775)
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BYTECODE_DIRECTORY_UNTRUSTED", result.stderr)
        self.assertTrue(self.cache.exists())

    def hook(self, prefix):
        trace = self.root / "schedule.json"
        stub = self.root / "schedule-stub"
        stub.write_text(
            "#!" + sys.executable + "\n"
            "import json,sys\nfrom pathlib import Path\n"
            "sys.path.insert(0," + repr(str(self.prefix / "expanded")) + ")\n"
            "import framebridge_expanded\n"
            "Path(" + repr(str(trace)) + ").write_text(json.dumps({"
            "'version': framebridge_expanded.__version__}))\n"
        )
        stub.chmod(0o755)
        script = maintainer_postinst().replace(bytecode_cleanup_source(),
                                              bytecode_cleanup_source(prefix))
        script = script.replace(
            "/usr/bin/python3 -I -S /usr/lib/framebridge-expanded/expanded/framebridge_expanded/package_upgrade.py schedule",
            shlex.quote(str(stub)))
        return script, trace

    def test_configure_invalidates_before_importing_upgrade_scheduler(self):
        self.replace_source_with_same_size_and_time()
        self.assertEqual(self.interpreter_version(), "0.3.4")
        script, trace = self.hook(self.prefix)
        result = subprocess.run(["/bin/sh", "-s", "--", "configure", "0.3.4"],
                                input=script, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(trace.read_text()), {"version": "0.3.5"})

    def test_cleanup_failure_prevents_importing_upgrade_scheduler(self):
        link = self.root / "prefix-link"
        link.symlink_to(self.prefix, target_is_directory=True)
        script, trace = self.hook(link)
        result = subprocess.run(["/bin/sh", "-s", "--", "configure", "0.3.4"],
                                input=script, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(trace.exists())

    def test_non_configure_actions_neither_cleanup_nor_schedule(self):
        script, trace = self.hook(self.prefix)
        for action in ("abort-upgrade", "abort-remove", "triggered"):
            with self.subTest(action=action):
                result = subprocess.run(["/bin/sh", "-s", "--", action], input=script,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.cache.exists())
                self.assertFalse(trace.exists())

    def test_no_root_or_relative_prefix_is_accepted(self):
        for prefix in ("/", "relative/path", "/usr/lib/../elsewhere"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                bytecode_cleanup_source(prefix)


if __name__ == "__main__":
    unittest.main()
