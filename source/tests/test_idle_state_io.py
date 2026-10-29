"""Liveness stays fresh without periodically syncing the durable profile."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from framebridge_expanded import autostart, connection_background as background


class IdleStateIOTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "profile"
        self.directory = background._directory(self.state)
        self.runtime_parent = self.root / "runtime"
        self.runtime_parent.mkdir(mode=0o700)
        (self.runtime_parent / str(os.getuid())).mkdir(mode=0o700)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(background, "_RUNTIME_PARENT", self.runtime_parent).start()
        self.identity = dict(pid=12345, uid=os.getuid(), start_ticks=99,
                             boot_id="boot-one", session_id=12345,
                             process_group=12345, state_dir=str(self.state), lock_fd=8)

    def sink(self):
        runtime = background._heartbeat_directory(self.state, create=True)
        return background._StatusOutput(self.directory / "status.json", self.identity,
                                        runtime / "status.json")

    def status(self):
        with mock.patch.object(background, "_valid_identity", return_value=True):
            return background.status(self.state)

    def test_ready_heartbeat_fresh_beyond_timeout_without_durable_writes(self):
        clock = [100.0]
        with mock.patch.object(background.time, "time", side_effect=lambda: clock[0]), mock.patch.object(background.time, "monotonic", side_effect=lambda: clock[0]):
            sink = self.sink()
            sink.observe("ready")
            sink.change("ready", "[OK] Connected.")
            durable = sink.path.stat()
            original = sink.path.read_bytes()
            for instant in range(101, 141):
                clock[0] = float(instant)
                sink.observe("ready")
                self.assertTrue(self.status()["verified"])
            self.assertEqual(sink.path.read_bytes(), original)
            self.assertEqual(sink.path.stat().st_ino, durable.st_ino)
            self.assertEqual(sink.path.stat().st_mtime_ns, durable.st_mtime_ns)
            clock[0] = 156.0
            self.assertFalse(self.status()["verified"])
            self.assertEqual(self.status()["state"], "retrying")
            sink.observe("ready")
            self.assertTrue(self.status()["verified"])

    def test_failure_and_recovery_publish_transitions_immediately(self):
        sink = self.sink()
        sink.observe("ready")
        sink.change("ready", "[OK] Connected.")
        self.assertTrue(self.status()["verified"])
        sink.observe("access")
        self.assertFalse(self.status()["verified"])
        sink.change("retrying", "[RETRYING] Lost readiness.")
        self.assertEqual(json.loads(sink.path.read_text())["state"], "retrying")
        sink.observe("ready")
        sink.change("ready", "[OK] Connected.")
        self.assertTrue(self.status()["verified"])

    def test_missing_or_old_worker_heartbeat_never_uses_durable_timestamp(self):
        sink = self.sink()
        sink.observe("ready")
        sink.change("ready", "[OK] Connected.")
        heartbeat = background._read(sink.heartbeat_path)
        for field, value in (("pid", 99999), ("boot_id", "older-boot"), ("start_ticks", 44), ("state_dir", "/another-profile")):
            wrong = dict(heartbeat, **{field: value})
            background._write(sink.heartbeat_path, wrong)
            self.assertFalse(self.status()["verified"], field)
        sink.heartbeat_path.unlink()
        self.assertFalse(self.status()["verified"])

    def test_no_runtime_falls_back_to_bounded_fresh_durable_heartbeat(self):
        (self.runtime_parent / str(os.getuid())).rmdir()
        self.assertIsNone(background._heartbeat_directory(self.state, create=True))
        clock = [100.0]
        with mock.patch.object(background.time, "time", side_effect=lambda: clock[0]), mock.patch.object(background.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(background, "_write", wraps=background._write) as write:
            sink = background._StatusOutput(self.directory / "status.json", self.identity)
            sink.observe("ready")
            sink.change("ready", "[OK] Connected.")
            write.reset_mock()
            for instant in range(101, 121):
                clock[0] = float(instant)
                sink.observe("ready")
                self.assertTrue(self.status()["verified"])
            self.assertEqual(write.call_count, 4)
            sink.observe("access")
            self.assertFalse(self.status()["verified"])

    def test_legacy_durable_heartbeat_remains_readable_with_runtime_available(self):
        background._write(self.directory / "status.json", dict(self.identity, state="ready", verified_at=100.0))
        with mock.patch.object(background.time, "time", return_value=110.0):
            self.assertTrue(self.status()["verified"])
        with mock.patch.object(background.time, "time", return_value=116.0):
            self.assertFalse(self.status()["verified"])

    def test_runtime_permissions_and_symlinks_rejected_without_changes(self):
        root = self.runtime_parent / str(os.getuid())
        root.chmod(0o755)
        with self.assertRaises(RuntimeError):
            background._heartbeat_directory(self.state, create=True)
        self.assertEqual(root.stat().st_mode & 0o777, 0o755)
        root.chmod(0o700)
        directory = background._heartbeat_directory(self.state, create=True)
        directory.rmdir()
        directory.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            background._heartbeat_directory(self.state, create=True)
        with self.assertRaises(RuntimeError):
            background._heartbeat_directory(self.state)

    def test_runtime_parent_symlink_rejected(self):
        alias = self.root / "runtime-alias"
        alias.symlink_to(self.runtime_parent, target_is_directory=True)
        with mock.patch.object(background, "_RUNTIME_PARENT", alias), self.assertRaises(RuntimeError):
            background._heartbeat_directory(self.state, create=True)

    def test_heartbeat_file_symlink_and_hardlink_rejected(self):
        sink = self.sink()
        sink.heartbeat_path.symlink_to(sink.path)
        with self.assertRaises(OSError):
            sink.observe("ready")
        sink.heartbeat_path.unlink()
        os.link(sink.path, sink.heartbeat_path)
        with self.assertRaises(RuntimeError):
            sink.observe("ready")

    def test_worker_state_only_writes_on_transition(self):
        with mock.patch.object(autostart, "_json", wraps=autostart._json) as write:
            autostart._worker_status(self.directory, "connected")
            first = (self.directory / "worker-status.json").read_bytes()
            for _ in range(20):
                autostart._worker_status(self.directory, "connected")
            self.assertEqual(write.call_count, 1)
            self.assertEqual((self.directory / "worker-status.json").read_bytes(), first)
            autostart._worker_status(self.directory, "reconnecting")
            self.assertEqual(write.call_count, 2)
            self.assertEqual(json.loads((self.directory / "worker-status.json").read_text())["state"], "reconnecting")


if __name__ == "__main__":
    unittest.main()
