"""Owned real-console cleanup, daemon outcome and fail-closed evidence regressions."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "expanded"))
os.environ["FRAMEBRIDGE_EXPANDED_PREFIX"] = str(ROOT)
from framebridge_expanded import tty, tty_cleanup, tty_sudo_control as control


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fb-cleanup-qa-", dir=Path("/run/user") / str(os.getuid()))
        self.state = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(tty.stop, self.state)

    def dead_daemon(self):
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"])
        identity = tty._identity(child.pid)
        child.terminate()
        child.wait()
        return tty_cleanup.daemon_identity(identity)

    def pidfile(self, daemon):
        tty._write_json(self.state / "tty-process.json", {key: daemon[key] for key in ("pid", "birth", "uid", "boot")})

    def assert_gone(self, record):
        for pid, birth in ((record["pid"], record["birth"]), (record["server_pid"], record["server_birth"])):
            process = control._proc(pid)
            self.assertFalse(process and process["birth"] == birth and process["state"] not in {"Z", "X"})
        controller = control.SudoConsoleController(tty.runtime_path(self.state), timeout=5)
        self.assertEqual(controller._scope_members_existing(record), set())

    def test_real_close_and_stop_verify_owned_process_death(self):
        self.assertTrue(tty.start(self.state)["ok"])
        for close_first in (True, False):
            opened = tty.request(self.state, {"authorized": True, "op": "open"})
            self.assertTrue(opened["ok"], opened)
            receipt = tty_cleanup.read(self.state)
            self.assertEqual(len(receipt["sessions"]), 1)
            record = receipt["sessions"][0]
            if close_first:
                closed = tty.request(self.state, {"authorized": True, "op": "close", "session_id": record["session_id"]})
                self.assertTrue(closed["ok"], closed)
                self.assertTrue(closed["result"]["cleanup_verified"])
            stopped = tty.stop(self.state)
            self.assertTrue(stopped["ok"], stopped)
            self.assertTrue(stopped["cleanup_verified"])
            self.assert_gone(record)
            self.assertTrue(tty.stop(self.state)["cleanup_verified"])
            if close_first:
                self.assertTrue(tty.start(self.state)["ok"])

    def test_failed_shutdown_cannot_become_success_by_stop_or_restart(self):
        limits = tty.Limits()
        home = self.state / "home"; home.mkdir()
        manager = tty.PortableManager(self.state, limits, home)
        manager.cleanup_daemon = self.dead_daemon()
        manager._record_cleanup()
        self.addCleanup(manager.shutdown)
        manager.open()
        self.pidfile(manager.cleanup_daemon)
        with mock.patch.object(manager._sudo, "close", side_effect=control.SudoControlError("TTY_SUDO_SCOPE_STILL_RUNNING")):
            result = manager.shutdown()
        self.assertFalse(result["cleanup_verified"])
        before = (self.state / tty_cleanup.NAME).read_bytes()
        for _ in range(2):
            result = tty.stop(self.state)
            self.assertFalse(result["ok"], result)
            self.assertFalse(result["cleanup_verified"])
            self.assertEqual(result["error"], "TTY_CLEANUP_UNVERIFIED")
        self.assertEqual(tty.start(self.state)["error"], "TTY_CLEANUP_PENDING")
        self.assertEqual((self.state / tty_cleanup.NAME).read_bytes(), before)
        self.assertTrue(control._proc(tty_cleanup.read(self.state)["sessions"][0]["pid"]))
        self.assertTrue(manager.shutdown()["cleanup_verified"])
        self.assertTrue(tty.stop(self.state)["cleanup_verified"])

    def test_missing_or_other_daemon_receipt_does_not_verify_stop(self):
        expected = self.dead_daemon(); self.pidfile(expected)
        self.assertFalse(tty.stop(self.state)["cleanup_verified"])
        other = self.dead_daemon()
        tty_cleanup.write(self.state, other, [], verified=True)
        self.assertFalse(tty.stop(self.state)["cleanup_verified"])

    def test_untrusted_or_tampered_receipt_fails_closed(self):
        daemon = self.dead_daemon(); self.pidfile(daemon)
        path = self.state / tty_cleanup.NAME
        tty_cleanup.write(self.state, daemon, [], verified=True)
        path.chmod(0o644)
        self.assertFalse(tty.stop(self.state)["cleanup_verified"])
        path.chmod(0o600)
        value = json.loads(path.read_text()); value["daemon"]["uid"] += 1
        tty._write_json(path, value)
        self.assertFalse(tty.stop(self.state)["cleanup_verified"])
        value["daemon"] = daemon; value["state_dir"] += "-other"
        tty._write_json(path, value)
        self.assertFalse(tty.stop(self.state)["cleanup_verified"])
        path.unlink(); path.symlink_to(self.state / "tty-process.json")
        self.assertFalse(tty.stop(self.state)["cleanup_verified"])
        self.assertEqual(tty.start(self.state)["error"], "TTY_CLEANUP_PENDING")

    def test_inflight_operation_receipt_is_preserved_on_restart(self):
        daemon = self.dead_daemon()
        tty_cleanup.write(self.state, daemon, [], operation_pending=True)
        before = (self.state / tty_cleanup.NAME).read_bytes()
        self.assertEqual(tty.start(self.state)["error"], "TTY_CLEANUP_PENDING")
        self.assertEqual((self.state / tty_cleanup.NAME).read_bytes(), before)

    def test_pending_receipt_allows_restart_only_after_natural_identity_death(self):
        daemon = self.dead_daemon()
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"])
        identity = tty._identity(child.pid)
        sid = uuid.uuid4().hex
        unit = "framebridge-tty-" + "a" * 12 + "-" + sid + "-" + "b" * 8 + ".service"
        record = {"session_id": sid, "unit_name": unit, "pid": child.pid, "birth": identity["birth"],
                  "server_pid": daemon["pid"], "server_birth": daemon["birth"],
                  "control_group": "/absent-fb-cleanup-qa/" + unit,
                  "pane_control_group": "/absent-fb-cleanup-qa/" + unit}
        tty_cleanup.write(self.state, daemon, [record], errors=[{"error": "TTY_SUDO_SCOPE_STILL_RUNNING"}])
        try:
            self.assertEqual(tty.start(self.state)["error"], "TTY_CLEANUP_PENDING")
        finally:
            child.terminate(); child.wait()
        self.assertTrue(tty.start(self.state)["ok"])
        self.assertTrue(tty.stop(self.state)["cleanup_verified"])

    def test_prelaunch_invalid_arguments_do_not_poison_profile(self):
        home = self.state / "home"; home.mkdir()
        manager = tty.PortableManager(self.state, tty.Limits(), home)
        manager.cleanup_daemon = self.dead_daemon()
        manager._record_cleanup()
        self.addCleanup(manager.shutdown)
        with mock.patch.object(manager._sudo, "_run") as run:
            with self.assertRaises(control.SudoControlError):
                manager._sudo.launch(uuid.uuid4().hex, cwd=self.state / "absent")
            with self.assertRaises(tty.TerminalDenied):
                manager.open(rows=0)
            run.assert_not_called()
        self.assertFalse(manager._cleanup_uncertain())
        self.assertTrue(manager.shutdown()["cleanup_verified"])
        self.assertTrue(tty.start(self.state)["ok"])
        self.assertTrue(tty.stop(self.state)["cleanup_verified"])

    def test_previous_boot_receipts_recover_without_signaling_old_pid_numbers(self):
        daemon = tty_cleanup.daemon_identity(tty._identity(os.getpid()))
        daemon["boot"] = str(uuid.uuid4())
        for verified, pending in ((True, False), (False, False), (False, True)):
            with self.subTest(verified=verified, pending=pending):
                tty_cleanup.write(self.state, daemon, [], verified=verified, operation_pending=pending)
                self.pidfile(daemon)
                with mock.patch.object(os, "pidfd_open") as opening:
                    self.assertTrue(tty.stop(self.state)["cleanup_verified"])
                    opening.assert_not_called()
                self.assertTrue(tty.start(self.state)["ok"])
                self.assertTrue(tty.stop(self.state)["cleanup_verified"])

    def test_verified_launch_rollback_does_not_poison_profile(self):
        home = self.state / "home"; home.mkdir()
        manager = tty.PortableManager(self.state, tty.Limits(), home)
        manager.cleanup_daemon = self.dead_daemon(); manager._record_cleanup()
        self.addCleanup(manager.shutdown)
        original = manager._sudo._inspect
        injected = False
        def mismatch(record):
            nonlocal injected
            current = original(record)
            if not injected:
                injected = True
                return {**current, "no_new_privs": 0}
            return current
        with mock.patch.object(manager._sudo, "_inspect", side_effect=mismatch):
            with self.assertRaises(control.SudoControlError) as error:
                manager.open()
        self.assertEqual(error.exception.code, "TTY_SUDO_PRIVILEGE_STATE_MISMATCH")
        self.assertFalse(manager._cleanup_uncertain())
        self.assertTrue(manager.shutdown()["cleanup_verified"])
        self.assertTrue(manager.open()["session_id"])
        self.assertTrue(manager.shutdown()["cleanup_verified"])

    def test_partial_launch_and_failed_rollback_remain_unverified(self):
        home = self.state / "home"; home.mkdir()
        manager = tty.PortableManager(self.state, tty.Limits(), home)
        manager.cleanup_daemon = self.dead_daemon()
        manager._record_cleanup(); self.pidfile(manager.cleanup_daemon)
        original = manager._sudo._run
        def failed(command, **kwargs):
            if command[0] == manager._sudo.systemd_run:
                original(command, **kwargs)
                raise control.SudoControlError("TTY_SUDO_BACKEND_TIMEOUT")
            if command[0] == manager._sudo.systemctl and "stop" in command:
                raise control.SudoControlError("TTY_SUDO_BACKEND_TIMEOUT")
            return original(command, **kwargs)
        try:
            with mock.patch.object(manager._sudo, "_run", side_effect=failed):
                with self.assertRaises(control.SudoControlError):
                    manager.open()
            self.assertTrue(manager._sudo._uncertain_launches)
            self.assertFalse(manager.shutdown()["cleanup_verified"])
            self.assertTrue(tty_cleanup.read(self.state)["operation_pending"])
            self.assertFalse(tty.stop(self.state)["cleanup_verified"])
            self.assertEqual(tty.start(self.state)["error"], "TTY_CLEANUP_PENDING")
        finally:
            for unit in manager._sudo._uncertain_launches:
                manager._sudo._unit(unit)  # Narrow ownership validation first.
                original([manager._sudo.systemctl, "--user", "stop", unit], optional=True)

    def test_real_close_kills_hup_and_term_ignoring_owned_job(self):
        controller = control.SudoConsoleController(tty.runtime_path(self.state), timeout=5)
        record = controller.launch(uuid.uuid4().hex, cwd=self.state)
        self.addCleanup(lambda: controller.close(record) if record["session_id"] in controller._records else None)
        marker = self.state / "child.pid"
        script = "import os,signal,time;signal.signal(signal.SIGHUP,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);open(" + repr(str(marker)) + ",'w').write(str(os.getpid()));time.sleep(30)"
        import shlex
        command = "nohup " + shlex.quote(sys.executable) + " -c " + shlex.quote(script) + " >/dev/null 2>&1 & disown"
        controller._tmux(record, "send-keys", "-t", record["pane"], "-l", command)
        controller._tmux(record, "send-keys", "-t", record["pane"], "Enter")
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertTrue(marker.exists())
        child = control._proc(int(marker.read_text()))
        calls = []
        original = controller._run
        def run(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)
        started = time.monotonic()
        with mock.patch.object(controller, "_run", side_effect=run):
            self.assertTrue(controller.close(record)["closed"])
        self.assertLess(time.monotonic() - started, 5)
        killed = control._proc(child["pid"])
        self.assertFalse(killed and killed["birth"] == child["birth"] and killed["state"] not in {"Z", "X"})
        self.assert_gone(record)
        kill_index = next(i for i, call in enumerate(calls) if "kill-session" in call)
        stops = [(i, call) for i, call in enumerate(calls) if "stop" in call]
        self.assertTrue(stops)
        self.assertTrue(all(i > kill_index and "--no-block" in call for i, call in stops))


class PidfdGuardTests(unittest.TestCase):
    def test_reused_pid_after_pidfd_open_is_not_signaled(self):
        controller = control.SudoConsoleController.__new__(control.SudoConsoleController)
        controller.uid = os.getuid()
        controller.timeout = 0.06
        controller.systemctl = "systemctl"
        record = {"session_id": "a" * 32, "unit_name": "owned.service", "name": "private-session",
                  "pid": 123456, "birth": "1", "server_pid": 123457, "server_birth": "2",
                  "control_group": "/private/owned.service", "pane_control_group": "/private/own.scope",
                  "pane_scope_unit": "own.scope", "busy_reason": None}
        member = {"pid": 123458, "birth": "3", "uids": (os.getuid(),) * 4, "state": "S"}
        opened = False
        def pidfd_open(pid):
            nonlocal opened
            self.assertEqual(pid, member["pid"])
            opened = True
            return 987
        def process(pid):
            if pid == member["pid"]:
                return {**member, "birth": "4" if opened else "3"}
            return None
        with (mock.patch.object(controller, "_inspect", return_value=record),
              mock.patch.object(controller, "_tmux"),
              mock.patch.object(controller, "_run"),
              mock.patch.object(controller, "_close_members", return_value={member["pid"]: member}),
              mock.patch.object(controller, "_terminated", return_value=record),
              mock.patch.object(controller, "_pane_group") as group,
              mock.patch.object(control, "_proc", side_effect=process),
              mock.patch.object(os, "pidfd_open", side_effect=pidfd_open),
              mock.patch.object(os, "close") as closing,
              mock.patch.object(signal, "pidfd_send_signal") as sending):
            with self.assertRaises(control.SudoControlError):
                controller._close(record)
            self.assertTrue(opened)
            sending.assert_not_called()
            group.assert_not_called()
            closing.assert_called_once_with(987)


if __name__ == "__main__":
    unittest.main()
