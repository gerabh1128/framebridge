"""Owned transient-unit tests; never invokes sudo or changes system policy."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "expanded"))
os.environ["FRAMEBRIDGE_EXPANDED_PREFIX"] = str(ROOT)
from framebridge_expanded import tty_sudo_control as control


class SudoControlTests(unittest.TestCase):
    def setUp(self):
        runtime = Path("/run/user") / str(os.getuid())
        self.temp = tempfile.TemporaryDirectory(prefix="fb-sudo-qa-", dir=runtime)
        self.base = Path(self.temp.name)
        self.controller = control.SudoConsoleController(self.base, timeout=5)
        self.records = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for record in reversed(self.records):
            try:
                self.controller.close(record)
            except control.SudoControlError as error:
                if error.code not in {"TTY_SUDO_BACKEND_FAILED", "TTY_SUDO_PROCESS_NOT_FOUND",
                                      "TTY_SUDO_CONSOLE_EXITED", "TTY_SUDO_UNIT_NOT_RUNNING"}:
                    raise
        self.temp.cleanup()

    def launch(self, **kwargs):
        record = self.controller.launch(uuid.uuid4().hex, **kwargs)
        self.records.append(record)
        return record

    def wait(self, record, predicate, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = self.controller.status(record)
            if predicate(value):
                return value
            time.sleep(0.03)
        self.fail("Expected controlled console state was not reached.")

    def send(self, record, command):
        self.controller._tmux(record, "send-keys", "-t", record["pane"], "-l", command)
        self.controller._tmux(record, "send-keys", "-t", record["pane"], "Enter")

    def test_roundtrip_real_nnp_and_recreation_preserves_cwd_size(self):
        cwd = self.base / "directory"
        cwd.mkdir()
        off = self.launch(cwd=cwd, rows=31, cols=97)
        self.assertEqual((off["no_new_privs"], off["server_no_new_privs"]), (1, 1))
        self.assertTrue(off["prompt_verified"])
        on = self.controller.set_mode(off, "on")
        self.assertEqual((on["no_new_privs"], on["server_no_new_privs"]), (0, 0))
        self.assertNotEqual(on["pid"], off["pid"])
        self.assertEqual((on["cwd"], on["rows"], on["cols"]), (str(cwd), 31, 97))
        self.assertIsNone(control._proc(off["pid"]))
        blocked = self.controller.set_mode(on, "off")
        self.assertEqual((blocked["no_new_privs"], blocked["server_no_new_privs"]), (1, 1))
        self.assertNotEqual(blocked["pid"], on["pid"])
        self.assertFalse(blocked["sudo_authorization_granted"])
        self.assertEqual(blocked["sudo_authentication"], "system_policy")

    def test_same_mode_is_idempotent_for_managed_console(self):
        record = self.launch()
        same = self.controller.set_mode(record, "off")
        self.assertFalse(same["recreated"])
        self.assertEqual(record["pid"], same["pid"])

    def test_builtin_read_is_busy_without_a_child_process(self):
        record = self.launch()
        self.send(record, "read -r __fb_public_test")
        busy = self.wait(record, lambda v: v["busy"] and not v["prompt_verified"])
        self.assertFalse(busy["busy_pids"])
        with self.assertRaises(control.SudoControlError) as error:
            self.controller.set_mode(record, "on")
        self.assertEqual(error.exception.code, "TTY_SUDO_PROMPT_UNVERIFIED")
        self.controller._tmux(record, "send-keys", "-t", record["pane"], "C-c")
        self.wait(record, lambda v: not v["busy"])

    def test_background_job_blocks_transition(self):
        record = self.launch()
        self.send(record, "sleep 30 &")
        self.wait(record, lambda v: bool(v["busy_pids"]))
        with self.assertRaises(control.SudoControlError) as error:
            self.controller.set_mode(record, "on")
        self.assertEqual(error.exception.code, "TTY_SUDO_CONSOLE_BUSY")

    def test_reparented_setsid_disowned_job_is_found_by_cgroup(self):
        record = self.launch()
        self.send(record, "setsid -f /bin/sleep 30 </dev/null >/dev/null 2>&1 & disown")
        busy = self.wait(record, lambda v: bool(v["busy_pids"]))
        processes = control._processes()
        descendants = control._descendants(processes, record["pid"])
        self.assertTrue(set(busy["busy_pids"]) - descendants)
        with self.assertRaises(control.SudoControlError):
            self.controller.set_mode(record, "on")

    def test_stopped_job_blocks_transition(self):
        record = self.launch()
        self.send(record, "sleep 30")
        self.wait(record, lambda v: bool(v["busy_pids"]))
        self.controller._tmux(record, "send-keys", "-t", record["pane"], "C-z")
        busy = self.wait(record, lambda v: any(
            control._proc(p) and control._proc(p)["state"] == "T" for p in v["busy_pids"]))
        with self.assertRaises(control.SudoControlError):
            self.controller.set_mode(record, "on")
        self.assertTrue(busy["busy"])

    def test_candidate_is_rolled_back_if_original_becomes_busy(self):
        record = self.launch()
        original = self.controller._idle
        calls = 0
        def idle(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise control.SudoControlError("TTY_SUDO_CONSOLE_BUSY")
            return original(value)
        with mock.patch.object(self.controller, "_idle", side_effect=idle):
            with self.assertRaises(control.SudoControlError):
                self.controller.set_mode(record, "on")
        current = self.controller.status(record)
        self.assertEqual((current["pid"], current["mode"]), (record["pid"], "off"))
        self.assertEqual(len(list((self.base / "sudo").glob("*.sock"))), 1)

    def test_postkill_cleanup_failure_keeps_verified_replacement(self):
        record = self.launch()
        original = self.controller._close
        def close(value, require_idle=False):
            result = original(value, require_idle=require_idle)
            if value["pid"] == record["pid"]:
                raise control.SudoControlError("TTY_SUDO_BACKEND_TIMEOUT")
            return result
        with mock.patch.object(self.controller, "_close", side_effect=close):
            replacement = self.controller.set_mode(record, "on")
        self.assertTrue(replacement["cleanup_pending"])
        self.assertFalse(replacement["transition_complete"])
        self.assertEqual(replacement["cleanup_error"], "TTY_SUDO_BACKEND_TIMEOUT")
        self.assertIsNone(control._proc(record["pid"]))
        self.assertIsNotNone(control._proc(replacement["pid"]))
        self.assertEqual(replacement["no_new_privs"], 0)
        with mock.patch.object(self.controller, "_old_cleanup_complete", return_value=False):
            state = self.controller.status(record)
            self.assertTrue(state["cleanup_pending"])
            self.assertEqual(state["pid"], replacement["pid"])
            with self.assertRaises(control.SudoControlError) as error:
                self.controller.set_mode(record, "off")
            self.assertEqual(error.exception.code, "TTY_SUDO_TRANSITION_CLEANUP_PENDING")
            with self.assertRaises(control.SudoControlError):
                self.controller.close(record)
        state = self.controller.status(record)
        self.assertFalse(state["cleanup_pending"])
        self.assertTrue(state["transition_complete"])
        self.assertNotIn("pending_old_record", state)

    def test_legacy_status_does_not_fake_prompt_verification(self):
        record = self.launch()
        legacy = {key: record[key] for key in ("name", "pane")}
        legacy.update(session_id=uuid.uuid4().hex, legacy_socket=record["tmux_socket"])
        state = self.controller.status(legacy)
        self.assertEqual(state["no_new_privs"], 1)
        self.assertFalse(state["prompt_verified"])
        with self.assertRaises(control.SudoControlError) as error:
            self.controller.set_mode(legacy, "on")
        self.assertEqual(error.exception.code, "TTY_SUDO_PROMPT_UNVERIFIED")

    def test_only_own_unit_names_are_accepted(self):
        with self.assertRaises(control.SudoControlError) as error:
            self.controller._unit("unrelated-existing.service")
        self.assertEqual(error.exception.code, "TTY_SUDO_UNIT_NOT_OWNED")

    def test_pid_birth_change_fails_closed(self):
        record = self.launch()
        self.controller._records[record["session_id"]]["birth"] = "not-the-birth"
        try:
            with self.assertRaises(control.SudoControlError) as error:
                self.controller.status(record)
            self.assertEqual(error.exception.code, "TTY_SUDO_PROCESS_IDENTITY_CHANGED")
        finally:
            self.controller._records[record["session_id"]]["birth"] = record["birth"]

    def test_elevated_process_metadata_refuses_change_and_close(self):
        record = self.launch()
        original = control._processes
        def processes():
            data = original()
            data[987654321] = {"pid": 987654321, "ppid": record["pid"],
                              "sid": 987654321, "tty": 0, "uids": (0, 0, 0, 0)}
            return data
        with mock.patch.object(control, "_processes", side_effect=processes):
            with self.assertRaises(control.SudoControlError) as error:
                self.controller.set_mode(record, "on")
            self.assertEqual(error.exception.code, "TTY_SUDO_ELEVATED_PROCESS_PRESENT")
            with self.assertRaises(control.SudoControlError):
                self.controller.close(record)

    def test_marker_contains_only_generation_and_state(self):
        record = self.launch()
        self.send(record, "printf '%s\\n' PUBLIC_INPUT_MARKER")
        self.wait(record, lambda v: not v["busy"])
        value = Path(record["prompt_marker"]).read_text()
        self.assertEqual(value, record["generation"] + ":idle\n")
        rc = Path(record["rcfile"]).read_text()
        self.assertNotIn("PUBLIC_INPUT_MARKER", rc)
        self.assertNotIn("BASH_COMMAND", rc)
        self.assertEqual(Path(record["rcfile"]).stat().st_mode & 0o777, 0o600)

    def test_exit_still_cleans_up_owned_server_and_scope(self):
        record = self.launch()
        self.send(record, "exit")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process = control._proc(record["pid"])
            if process is None or process["state"] in {"Z", "X"}:
                break
            time.sleep(0.03)
        result = self.controller.close(record)
        self.assertTrue(result["closed"])
        self.assertFalse(Path(record["tmux_socket"]).exists())
        self.assertIsNone(control._proc(record["server_pid"]))
        self.records.remove(record)

    def test_invalid_mode_does_not_launch(self):
        with self.assertRaises(control.SudoControlError):
            self.controller.launch(uuid.uuid4().hex, mode="automatic-root")
        self.assertFalse(list((self.base / "sudo").glob("*.sock")))



class PortableSudoLimitsTests(SudoControlTests):
    # Reuse lifecycle helpers, but do not repeat the inherited test methods.
    def test_expanded_large_geometry_survives_mode_change(self):
        record = self.launch(rows=500, cols=1000)
        self.assertEqual((record["rows"], record["cols"]), (500, 1000))
        enabled = self.controller.set_mode(record, "on")
        self.assertEqual((enabled["rows"], enabled["cols"], enabled["no_new_privs"]), (500, 1000, 0))

    def test_profile_limits_reject_before_starting_a_unit(self):
        controller = control.SudoConsoleController(
            self.base / "profile", min_rows=3, max_rows=8, min_cols=11, max_cols=30)
        with mock.patch.object(controller, "_run", side_effect=AssertionError("unit must not start")):
            for rows, cols in [(2, 20), (9, 20), (4, 10), (4, 31), (True, 20)]:
                with self.assertRaises(control.SudoControlError) as error:
                    controller.launch(uuid.uuid4().hex, rows=rows, cols=cols)
                self.assertEqual(error.exception.code, "TTY_SUDO_SIZE_INVALID")

    def test_actual_nnp_mismatch_rolls_back_candidate(self):
        with mock.patch.object(self.controller, "_inspect",
                               return_value={"no_new_privs": 0, "busy": False}):
            with self.assertRaises(control.SudoControlError) as error:
                self.controller.launch(uuid.uuid4().hex, mode="off")
        self.assertEqual(error.exception.code, "TTY_SUDO_PRIVILEGE_STATE_MISMATCH")
        self.assertFalse(list((self.base / "sudo").glob("*.sock")))
        self.assertFalse(self.controller._records)

    def test_profile_runtime_isolation(self):
        other = control.SudoConsoleController(self.base / "other")
        self.assertNotEqual(other.base, self.controller.base)
        self.assertNotEqual(other.prefix, self.controller.prefix)


    def test_private_profile_bus_does_not_redirect_user_manager(self):
        private = self.base / "private-runtime"
        private.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, {
                "XDG_RUNTIME_DIR": str(private),
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(private / "not-the-user-manager")}):
            record = self.launch(mode="off")
            self.assertEqual(record["no_new_privs"], 1)
            self.assertEqual(record["server_no_new_privs"], 1)
            self.assertEqual(self.controller.user_runtime, Path("/run/user") / str(os.getuid()))



    def test_profile_graphical_context_is_explicitly_forwarded_to_unit(self):
        import shlex
        private = self.base / "profile-desktop"
        private.mkdir(mode=0o700)
        expected = {
            "DISPLAY": ":193",
            "XAUTHORITY": str(private / "authority-not-used"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(private / "private-bus-not-used"),
            "XDG_RUNTIME_DIR": str(private),
            "LD_LIBRARY_PATH": str(private / "libraries"),
            "LIBGL_DRIVERS_PATH": str(private / "drivers"),
            "GBM_BACKENDS_PATH": str(private / "gbm"),
            "__EGL_VENDOR_LIBRARY_FILENAMES": str(private / "egl-not-used.json"),
            "__GLX_VENDOR_LIBRARY_NAME": "mesa",
            "GALLIUM_DRIVER": "llvmpipe",
            "MESA_LOADER_DRIVER_OVERRIDE": "zink",
            "VK_DRIVER_FILES": str(private / "vk-not-used.json"),
            "VK_ICD_FILENAMES": str(private / "vk-compat-not-used.json"),
            "DRI_PRIME": "0",
        }
        forbidden = "FRAMEBRIDGE_TEST_NOT_FORWARDED"
        with mock.patch.dict(os.environ, dict(expected, **{forbidden: "MUST_NOT_FORWARD"})):
            controller = control.SudoConsoleController(self.base / "graphical-controller")
            record = controller.launch(uuid.uuid4().hex, mode="off")
            self.addCleanup(controller.close, record)
            self.assertEqual(record["no_new_privs"], 1)
            self.assertEqual(record["server_no_new_privs"], 1)
            self.assertEqual(controller.session_environment, expected)
            result = controller._run([
                controller.systemctl, "--user", "show", record["unit_name"],
                "--property=Environment", "--value"])
            unit_environment = dict(value.split("=", 1)
                                    for value in shlex.split(result.stdout.decode()))
            for name, value in expected.items():
                self.assertEqual(unit_environment.get(name), value)
            self.assertNotIn(forbidden, unit_environment)
            self.assertEqual(controller.user_bus_address,
                             "unix:path=/run/user/" + str(os.getuid()) + "/bus")



# Do not run inherited acceptance cases twice.
for _name in tuple(name for name in dir(SudoControlTests) if name.startswith("test_")):
    setattr(PortableSudoLimitsTests, _name, None)


class PortableSudoRpcTests(unittest.TestCase):
    def setUp(self):
        import threading
        from framebridge_expanded import tty
        self.tty = tty
        class FakeManager:
            _lock = threading.RLock()
            def reap(self): pass
            def touch(self, sid): self.last_touch = sid
            def sudo(self, sid, mode):
                return {"session_id": sid, "mode": mode, "no_new_privs": 1 if mode == "off" else 0}
        self.manager = FakeManager()
        self.sid = "a" * 32
        self.payload = {"op": "sudo", "mode": "on", "session_id": self.sid, "authorized": True}

    def test_authorized_typed_sudo_dispatch(self):
        reply = self.tty.dispatch(self.manager, self.payload)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["session_id"], self.sid)
        self.assertEqual(reply["result"]["mode"], "on")
        self.assertEqual(self.manager.last_touch, self.sid)

    def test_missing_authorization_and_extra_keys_are_rejected(self):
        for payload in [dict(self.payload, authorized=False), dict(self.payload, command="sudo id")]:
            with self.assertRaises(self.tty.TerminalDenied):
                self.tty.dispatch(self.manager, payload)

    def test_cleanup_pending_keeps_verified_replacement_in_failure_reply(self):
        self.manager.sudo = lambda sid, mode: {"session_id": sid, "cleanup_pending": True, "mode": mode}
        reply = self.tty.dispatch(self.manager, self.payload)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "TTY_SUDO_TRANSITION_CLEANUP_PENDING")
        self.assertEqual(reply["result"]["session_id"], self.sid)
        status = self.tty.dispatch(self.manager, dict(self.payload, mode="status"))
        self.assertTrue(status["ok"])

    def test_controller_error_is_public_fixed_code(self):
        error = control.SudoControlError("TTY_SUDO_CONSOLE_BUSY")
        self.assertEqual(self.tty._safe_error(error), "TTY_SUDO_CONSOLE_BUSY")


if __name__ == "__main__":
    unittest.main()
