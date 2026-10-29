import copy
from datetime import datetime, timezone, timedelta
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from framebridge_expanded.real_screen import screen_control as m


class ScreenControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "screen.png"
        self.path.write_bytes(b"immutable-observed-frame-fixture")
        self.binding = {"version": 1, "backend": "visible-tmux", "node": "TESTNODE",
                        "uid": os.getuid(), "active_vt": "tty1", "client_tty": "/dev/tty1",
                        "socket": {"path": "/run/user/1000/console.sock", "inode": 12},
                        "server": {"pid": 100, "start_ticks": 20},
                        "client": {"pid": 101, "start_ticks": 21},
                        "session_id": "$0", "window_id": "@0", "pane_id": "%1",
                        "pane": {"pid": 102, "start_ticks": 22},
                        "pane_geometry": [120, 0, 120, 66], "pane_in_mode": False}
        self.manifest = {"capture_id": "a" * 32, "source": "linux-framebuffer",
                         "screen_type": "tty", "active_vt": "tty1", "node": "TESTNODE",
                         "uid": os.getuid(), "synthetic": False, "pixel_source_verified": True,
                         "path": str(self.path), "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                         "completed_at": datetime.now(timezone.utc).isoformat(),
                         "binding": copy.deepcopy(self.binding)}

    def tearDown(self):
        self.temp.cleanup()

    def refused(self, change, code="SCREEN_BINDING_CHANGED"):
        different = copy.deepcopy(self.binding)
        change(different)
        with patch.object(m, "snapshot_binding", return_value=different), patch.object(m, "_tmux") as send:
            with self.assertRaises(m.ControlError) as e:
                m.act(self.manifest, "key.send", key="ENTER")
            self.assertEqual(e.exception.code, code)
            send.assert_not_called()

    def test_bind_returns_copy(self):
        with patch.object(m, "snapshot_binding", return_value=self.binding):
            result = m.bind_screen(self.manifest)
        self.assertIsNot(result, self.manifest)
        self.assertEqual(result["binding"], self.binding)

    def test_bind_vt_mismatch(self):
        self.manifest["active_vt"] = "tty2"
        with patch.object(m, "snapshot_binding", return_value=self.binding):
            with self.assertRaises(m.ControlError) as e:
                m.bind_screen(self.manifest)
        self.assertEqual(e.exception.code, "SCREEN_CAPTURE_VT_MISMATCH")

    def test_bind_node_mismatch(self):
        self.manifest["node"] = "OTHER"
        with patch.object(m, "snapshot_binding", return_value=self.binding):
            with self.assertRaises(m.ControlError) as e:
                m.bind_screen(self.manifest)
        self.assertEqual(e.exception.code, "SCREEN_CAPTURE_NODE_MISMATCH")

    def test_wrong_source(self):
        self.manifest["source"] = "rendered-text"
        with self.assertRaises(m.ControlError) as e:
            m.bind_screen(self.manifest)
        self.assertEqual(e.exception.code, "SCREEN_CONTROL_MODE_UNSUPPORTED")

    def test_literal_text_no_shell_or_enter(self):
        text = "printf '%s' '$(touch never)'"
        with patch.object(m, "snapshot_binding", return_value=self.binding), patch.object(m, "_tmux", return_value="") as send:
            result = m.act(self.manifest, "text.type", text=text)
        self.assertTrue(result["performed"])
        send.assert_called_once_with(self.binding["socket"]["path"], "send-keys", "-t", "%1", "-l", "--", text)

    def test_explicit_key(self):
        with patch.object(m, "snapshot_binding", return_value=self.binding), patch.object(m, "_tmux", return_value="") as send:
            result = m.act(self.manifest, "key.send", key="ENTER")
        self.assertTrue(result["performed"])
        send.assert_called_once_with(self.binding["socket"]["path"], "send-keys", "-t", "%1", "Enter")

    def test_text_newline_rejected(self):
        with patch.object(m, "_tmux") as send:
            with self.assertRaises(m.ControlError) as e:
                m.act(self.manifest, "text.type", text="pwd\n")
            self.assertEqual(e.exception.code, "SCREEN_TEXT_CONTROL_CHARACTER")
            send.assert_not_called()

    def test_unknown_key_rejected(self):
        with patch.object(m, "_tmux") as send:
            with self.assertRaises(m.ControlError):
                m.act(self.manifest, "key.send", key="ALT_F2")
            send.assert_not_called()

    def test_changed_vt_no_input(self):
        self.refused(lambda b: b.update(active_vt="tty2"))

    def test_changed_pane_no_input(self):
        self.refused(lambda b: b["pane"].update(pid=222))

    def test_pid_reuse_no_input(self):
        self.refused(lambda b: b["pane"].update(start_ticks=23))

    def test_changed_socket_no_input(self):
        self.refused(lambda b: b["socket"].update(inode=13))

    def test_changed_geometry_no_input(self):
        self.refused(lambda b: b.update(pane_geometry=[0, 0, 240, 66]))

    def test_hidden_client_no_fallback(self):
        identity = {"path": "/run/user/1000/console.sock", "inode": 12}
        client_row = "101|/dev/tty2|$0|240|67|0\n"
        with patch.object(m, "_active_vt", return_value="tty1"), patch.object(m, "_server_sockets", return_value=[(identity, {"pid": 100})]), patch.object(m, "_tmux", return_value=client_row) as run:
            with self.assertRaises(m.ControlError) as e:
                m.snapshot_binding()
            self.assertEqual(e.exception.code, "SCREEN_VISIBLE_TMUX_NOT_FOUND")
            self.assertEqual(run.call_count, 1)

    def test_postflight_change_reports_performed(self):
        other = copy.deepcopy(self.binding)
        other["pane_id"] = "%2"
        with patch.object(m, "snapshot_binding", side_effect=[self.binding, self.binding, other]), patch.object(m, "_tmux", return_value="") as send:
            with self.assertRaises(m.ControlError) as e:
                m.act(self.manifest, "key.send", key="ENTER")
            self.assertEqual(e.exception.code, "SCREEN_CHANGED_AFTER_INPUT")
            self.assertTrue(e.exception.details["performed"])
            send.assert_called_once()

    def test_tampered_capture_no_input(self):
        self.path.write_bytes(b"changed")
        with patch.object(m, "_tmux") as send:
            with self.assertRaises(m.ControlError) as e:
                m.act(self.manifest, "key.send", key="ENTER")
            self.assertEqual(e.exception.code, "SCREEN_CAPTURE_SHA256_MISMATCH")
            send.assert_not_called()

    def test_capture_id_required(self):
        self.manifest.pop("capture_id")
        with self.assertRaises(m.ControlError) as e:
            m.act(self.manifest, "key.send", key="ENTER")
        self.assertEqual(e.exception.code, "SCREEN_CAPTURE_ID_REQUIRED")

    def test_copy_mode_does_not_type_into_hidden_application(self):
        self.binding["pane_in_mode"] = True
        self.manifest["binding"] = copy.deepcopy(self.binding)
        self.refused(lambda b: None, "SCREEN_TMUX_MODE_INPUT_UNSUPPORTED")

    def test_optional_explicit_age_limit(self):
        self.manifest["completed_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self.assertRaises(m.ControlError) as e:
            m.act(self.manifest, "key.send", key="ENTER", max_age_s=30)
        self.assertEqual(e.exception.code, "SCREEN_CAPTURE_EXPIRED")

    def test_no_arbitrary_age_expiry_by_default(self):
        self.manifest["completed_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch.object(m, "snapshot_binding", return_value=self.binding), patch.object(m, "_tmux", return_value=""):
            self.assertTrue(m.act(self.manifest, "key.send", key="ENTER")["performed"])


if __name__ == "__main__":
    unittest.main()
