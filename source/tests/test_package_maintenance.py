"""Disposable maintenance leases and real interprocess exclusion.

No test calls the production maintainer entrypoint or opens its real state.
"""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from framebridge_expanded import package_maintenance as gate


class PackageMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fb-package-gate-test-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / "gate"
        self.proc = self.parent / "proc"
        self.proc.mkdir()
        self.pending = self.parent / "legacy-pending.json"
        self.kw = dict(root=self.root, owner=os.getuid(), proc_root=self.proc, pending=self.pending)

    def transition(self, action):
        gate.transition(action, **self.kw)

    def runtime(self):
        return gate.runtime_lease(root=self.root, owner=os.getuid())

    def ready(self):
        self.transition("begin")
        self.transition("finish")

    def state(self):
        return (self.root / "state").read_bytes()

    def process(self, args):
        fake = self.proc / "987654"
        fake.mkdir(exist_ok=True)
        (fake / "cmdline").write_bytes(b"\0".join(args) + b"\0")

    def test_missing_marker_fails_closed_without_creation(self):
        with self.assertRaises(gate.MaintenanceError), self.runtime():
            self.fail("Runtime entered without a package marker")
        self.assertFalse(self.root.exists())

    def test_begin_persists_across_hook_process_lifetimes(self):
        self.transition("begin")
        self.assertEqual(self.state(), gate.MAINTENANCE)
        inode = (self.root / "state").stat().st_ino
        for _ in range(2):
            with self.assertRaisesRegex(gate.MaintenanceError, "MAINTENANCE_ACTIVE"), self.runtime():
                self.fail("Runtime entered maintenance")
        self.transition("begin")  # retry of interrupted package configuration
        self.transition("finish")
        self.assertEqual(self.state(), gate.READY)
        self.assertEqual((self.root / "state").stat().st_ino, inode)
        with self.runtime():
            pass

    def test_existing_runtime_refuses_replacement_without_mutating_marker(self):
        self.ready()
        with self.runtime():
            with self.assertRaisesRegex(gate.MaintenanceError, "MAINTENANCE_BUSY"):
                self.transition("begin")
            self.assertEqual(self.state(), gate.READY)

    def test_exclusive_lock_refuses_concurrent_start(self):
        self.ready()
        with gate.lease(self.root, os.getuid(), writer=True):
            with self.assertRaisesRegex(gate.MaintenanceError, "MAINTENANCE_BUSY"), self.runtime():
                self.fail("Runtime crossed exclusive lease")
        with self.runtime():
            pass

    def test_finish_without_begin_refused(self):
        self.ready()
        with self.assertRaisesRegex(gate.MaintenanceError, "BEGIN_REQUIRED"):
            self.transition("finish")
        self.assertEqual(self.state(), gate.READY)

    def test_legacy_manager_is_not_silently_ignored(self):
        self.ready()
        self.process([b"/usr/bin/python3", b"-m", b"framebridge_expanded.runtime", b"_manager"])
        with self.assertRaisesRegex(gate.MaintenanceError, "ACTIVE_COMPONENTS"):
            self.transition("begin")
        self.assertEqual(self.state(), gate.READY)

    def test_legacy_detached_original_worker_refuses_replacement(self):
        self.ready()
        self.process([b"python3", b"/home/example/.local/share/framebridge/fbterm/bin/fbterm_engine.py", b"serve"])
        with self.assertRaisesRegex(gate.MaintenanceError, "ACTIVE_COMPONENTS"):
            self.transition("begin")
        self.assertEqual(self.state(), gate.READY)

    def test_unrelated_process_does_not_block(self):
        self.process([b"/usr/bin/python3", b"/opt/unrelated/worker.py"])
        self.ready()
        self.assertEqual(self.state(), gate.READY)

    def test_legacy_deferred_restart_is_refused(self):
        self.ready()
        self.pending.write_text(json.dumps({"schema":1,"phase":"SCHEDULED","profiles":[]}))
        self.pending.chmod(0o600)
        with self.assertRaisesRegex(gate.MaintenanceError, "LEGACY_TRANSACTION_PENDING"):
            self.transition("begin")
        self.assertEqual(self.state(), gate.READY)

    def test_captured_empty_legacy_record_allowed(self):
        self.pending.write_text(json.dumps({"schema":1,"phase":"CAPTURED","profiles":[]}))
        self.pending.chmod(0o600)
        self.ready()

    def test_invalid_state_does_not_allow_runtime(self):
        self.ready()
        (self.root / "state").write_bytes(b"READY\nBROKEN")
        with self.assertRaises(gate.MaintenanceError), self.runtime():
            self.fail("Runtime accepted an invalid marker")
        self.transition("begin")
        self.transition("finish")
        with self.runtime():
            pass

    def test_state_symlink_never_followed(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        outside = self.parent / "outside"
        outside.write_bytes(b"unchanged")
        (self.root / "state").symlink_to(outside)
        with self.assertRaises(gate.MaintenanceError):
            self.transition("begin")
        self.assertEqual(outside.read_bytes(), b"unchanged")
        self.assertTrue((self.root / "state").is_symlink())

    def test_wrong_state_mode_is_refused_not_repaired(self):
        self.ready()
        (self.root / "state").chmod(0o666)
        with self.assertRaisesRegex(gate.MaintenanceError, "FILE_UNTRUSTED"):
            self.transition("begin")
        self.assertEqual((self.root / "state").stat().st_mode & 0o777, 0o666)

    def test_hardlinked_marker_refused(self):
        self.ready()
        other = self.parent / "hardlink"
        os.link(self.root / "state", other)
        with self.assertRaisesRegex(gate.MaintenanceError, "FILE_UNTRUSTED"), self.runtime():
            self.fail("Runtime accepted a hardlink")

    def test_directory_symlink_refused(self):
        outside = self.parent / "outside"
        outside.mkdir()
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(gate.MaintenanceError):
            self.transition("begin")
        self.assertFalse((outside / "state").exists())

    def test_inaccessible_process_inventory_refuses_upgrade(self):
        self.ready()
        with patch.object(gate, "active_roles", side_effect=PermissionError("fixture")):
            with self.assertRaises(gate.MaintenanceError):
                self.transition("begin")
        self.assertEqual(self.state(), gate.READY)

    def test_actual_child_process_shared_lease_blocks_begin(self):
        self.ready()
        code = ("from pathlib import Path\n"
                "from framebridge_expanded import package_maintenance as g\n"
                "import os,sys\n"
                "with g.runtime_lease(root=Path(sys.argv[1]),owner=os.getuid()):\n"
                " print('HELD',flush=True)\n"
                " sys.stdin.readline()\n")
        env = dict(os.environ, PYTHONPATH=str(Path(gate.__file__).resolve().parents[1]))
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.root)], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            import select
            self.assertTrue(select.select([child.stdout], [], [], 5)[0])
            self.assertEqual(child.stdout.readline().strip(), "HELD")
            with self.assertRaisesRegex(gate.MaintenanceError, "MAINTENANCE_BUSY"):
                self.transition("begin")
            child.communicate("\n", timeout=5)
            self.assertEqual(child.returncode, 0)
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)
        self.transition("begin")
        self.transition("finish")

    def test_unprivileged_maintainer_entrypoint_never_changes_state(self):
        with patch.object(gate.os,"getuid",return_value=1000), \
             patch.object(gate.os,"geteuid",return_value=1000), \
             patch.object(gate,"transition") as apply, redirect_stderr(io.StringIO()):
            self.assertEqual(gate.main(["begin"]),1)
            apply.assert_not_called()

    def test_runtime_error_is_not_relabelled_as_lock_failure(self):
        self.ready()
        with self.assertRaisesRegex(PermissionError, "synthetic runtime error"):
            with self.runtime():
                raise PermissionError("synthetic runtime error")

    def test_extracted_code_does_not_touch_production_guard(self):
        with patch.object(gate,"installed_runtime",return_value=False), \
             patch.object(gate,"runtime_lease") as actual:
            with gate.runtime_guard():
                pass
            actual.assert_not_called()


if __name__ == "__main__":
    unittest.main()
