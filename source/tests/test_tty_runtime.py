"""Real headless Bash/tmux integration tests; only test-owned profiles are closed."""
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import time
import unittest

WORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORK / "expanded"))
os.environ["PYTHONPATH"] = str(WORK / "expanded")
os.environ["FRAMEBRIDGE_EXPANDED_PREFIX"] = str(WORK)
from framebridge_expanded import tty


class RealTtyTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="tty-test-", dir=WORK / "qa"))
        self.sessions = []
        self.previous_display = os.environ.pop("DISPLAY", None)
        self.previous_x11 = os.environ.pop("XDG_SESSION_TYPE", None)
        self.assertTrue(tty.start(self.state).get("ok"))

    def tearDown(self):
        for sid in self.sessions:
            tty.request(self.state, {"op": "close", "authorized": True, "session_id": sid})
        self.assertTrue(tty.stop(self.state).get("ok"))
        self.assertFalse((tty.runtime_path(self.state) / "agent.sock").exists())
        if self.previous_display is not None:
            os.environ["DISPLAY"] = self.previous_display
        if self.previous_x11 is not None:
            os.environ["XDG_SESSION_TYPE"] = self.previous_x11

    def call(self, op, **args):
        return tty.request(self.state, {"op": op, "authorized": True, **args})

    def open(self, **args):
        value = self.call("open", **args)
        self.assertTrue(value.get("ok"), value)
        self.sessions.append(value["result"]["session_id"])
        return value["result"]

    def enter(self, sid, text):
        self.assertTrue(self.call("text.type", session_id=sid, text=text).get("ok"))
        self.assertTrue(self.call("key.send", session_id=sid, key="ENTER").get("ok"))

    def await_file(self, path):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(path.is_file(), str(path))
        return path.read_text()

    def test_real_persistent_bash_and_unicode_without_x11(self):
        self.assertNotIn("DISPLAY", os.environ)
        opened = self.open(rows=30, cols=100)
        self.assertTrue(opened["real_terminal"])
        self.assertEqual(opened["backend"], "tmux")
        self.assertTrue(Path(opened["tty"]).exists())
        self.assertGreater(opened["pid"], 1)
        sid = opened["session_id"]
        directory = self.state / "working-directory"
        directory.mkdir()
        self.enter(sid, "export FRAMEBRIDGE_TTY_VALUE=" + shlex.quote("persistent ñ 漢字")
                   + "; cd " + shlex.quote(str(directory)))
        marker = self.state / "unicode-and-state.txt"
        self.enter(sid, 'printf %s "$FRAMEBRIDGE_TTY_VALUE" > ' + shlex.quote(str(marker)))
        self.assertEqual(self.await_file(marker), "persistent ñ 漢字")
        observed = self.call("observe", session_id=sid)
        self.assertTrue(observed["ok"])
        self.assertEqual(observed["result"]["pid"], opened["pid"])
        self.assertEqual(observed["result"]["cwd"], str(directory))
        self.assertGreaterEqual(observed["result"]["sequence"], 4)

    def test_control_c_interrupts_a_real_foreground_process(self):
        sid = self.open()["session_id"]
        self.enter(sid, "sleep 30")
        deadline = time.monotonic() + 2
        sleeping = False
        while time.monotonic() < deadline:
            value = self.call("observe", session_id=sid)
            if value["result"]["current_command"] == "sleep":
                sleeping = True
                break
            time.sleep(0.05)
        self.assertTrue(sleeping)
        self.assertTrue(self.call("key.send", session_id=sid, key="CTRL_C")["ok"])
        marker = self.state / "interrupted.txt"
        self.enter(sid, "printf interrupted > " + shlex.quote(str(marker)))
        self.assertEqual(self.await_file(marker), "interrupted")

    def test_real_resize_can_exceed_the_old_machine_specific_bounds(self):
        sid = self.open()["session_id"]
        self.assertTrue(self.call("resize", session_id=sid, rows=110, cols=240)["ok"])
        observed = self.call("observe", session_id=sid)["result"]
        self.assertEqual((observed["screen"]["rows"], observed["screen"]["cols"]), (110, 240))

    def test_authorization_and_socket_identity_are_explicit(self):
        before = self.call("status")["result"]["active_sessions"]
        denied = tty.request(self.state, {"op": "open", "authorized": False})
        self.assertEqual(denied.get("error"), "AUTHORIZED_TRUE_REQUIRED")
        denied = tty.request(self.state, {"op": "open", "authorized": 1})
        self.assertEqual(denied.get("error"), "AUTHORIZED_TRUE_REQUIRED")
        self.assertEqual(self.call("status")["result"]["active_sessions"], before)
        path = tty.runtime_path(self.state) / "agent.sock"
        st = path.stat()
        self.assertTrue(stat.S_ISSOCK(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(st.st_uid, os.geteuid())
        status = self.call("status")["result"]
        self.assertEqual(status["input_policy"], "same-uid-authorized")
        self.assertNotIn("owner_device_connected", status)
        self.assertNotIn("owner_signature_required", status)

    def test_errors_do_not_expose_arbitrary_payloads_or_paths(self):
        self.assertEqual(self.call("open", program="demo-readonly").get("error"),
                         "PROGRAM_NOT_SUPPORTED")
        self.assertEqual(self.call("observe", session_id="../../outside").get("error"),
                         "SESSION_ID_INVALID")
        sid = self.open()["session_id"]
        self.assertEqual(self.call("key.send", session_id=sid, key="INVALID_PRIVATE_TEXT").get("error"),
                         "KEY_NOT_ALLOWLISTED")
        closed = self.call("close", session_id=sid)
        self.assertTrue(closed["result"]["cleanup_verified"])
        self.sessions.remove(sid)
        self.assertEqual(self.call("observe", session_id=sid).get("error"), "SESSION_NOT_FOUND")

    def test_configurable_session_limit_replaces_the_old_constructor_cap(self):
        self.assertTrue(tty.stop(self.state)["ok"])
        (self.state / "tty.json").write_text(json.dumps({"limits": {"max_sessions": 5}}))
        self.assertTrue(tty.start(self.state)["ok"])
        for _ in range(5):
            self.open(rows=10, cols=40)
        self.assertEqual(self.call("status")["result"]["active_sessions"], 5)
        self.assertEqual(self.call("open").get("error"), "SESSION_LIMIT")

    def test_configured_input_limit_is_reported_and_enforced(self):
        self.assertTrue(tty.stop(self.state)["ok"])
        (self.state / "tty.json").write_text(json.dumps({"limits": {"max_text_bytes": 100}}))
        self.assertTrue(tty.start(self.state)["ok"])
        sid = self.open()["session_id"]
        self.assertEqual(self.call("status")["result"]["limits"]["max_text_bytes"], 100)
        self.assertEqual(self.call("text.type", session_id=sid, text="x" * 101).get("error"),
                         "TEXT_INVALID_OR_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
