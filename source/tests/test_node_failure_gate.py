"""Runtime guard for nodes whose existing managed systemd units predate the limit."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from framebridge_expanded import autostart, config, runtime, connection_background


class NodeFailureGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "profile"
        config.save_config(self.profile, config.default_config(role="node"))
        _, self.directory, self.unit = autostart._paths(self.profile)

    def test_four_runtime_failures_stop_restart_loop_for_legacy_unit(self):
        with mock.patch.object(runtime, "start", return_value={"running": True,"ready": True}) as start, \
                mock.patch.object(runtime, "status", return_value={"running": False}), \
                mock.patch.object(runtime, "stop", return_value={"ok":True,"running":False}):
            self.assertEqual([autostart.worker(self.profile) for _ in range(4)], [1,1,1,0])
            self.assertEqual(autostart.worker(self.profile), 0)
        self.assertEqual(start.call_count,4)
        self.assertTrue(autostart._node_failure_gate(self.directory))
        import json
        self.assertEqual(json.loads((self.directory/"worker-status.json").read_text())["state"], "node_restart_limit_reached")

    def test_four_failed_headless_initializations_are_bounded(self):
        with mock.patch.object(runtime, "start", return_value={"running":False,"ready":False,"error":"synthetic startup failure"}) as start, \
                mock.patch.object(runtime, "stop", return_value={"ok":True,"running":False}), \
                mock.patch.object(autostart.time, "sleep"):
            self.assertEqual(autostart.worker(self.profile),0)
        self.assertEqual(start.call_count,4)
        self.assertTrue(autostart._node_failure_gate(self.directory))

    def test_operator_reset_only_when_inactive(self):
        for _ in range(4):
            autostart._node_failure_gate(self.directory, action="failure")
        active = subprocess.CompletedProcess([],0,b"ActiveState=active\nMainPID=123\n",b"")
        stopped = subprocess.CompletedProcess([],0,b"ActiveState=inactive\nMainPID=0\n",b"")
        with mock.patch.object(autostart, "_systemctl", return_value=active) as ctl:
            autostart.reset_node_failure_gate_for_start(self.directory,self.unit)
            self.assertTrue(autostart._node_failure_gate(self.directory))
            self.assertEqual(ctl.call_count,1)
        with mock.patch.object(autostart, "_systemctl", return_value=stopped) as ctl:
            autostart.reset_node_failure_gate_for_start(self.directory,self.unit)
            ctl.assert_any_call("reset-failed",self.unit.name)
            self.assertFalse(autostart._node_failure_gate(self.directory))

    def test_invalid_gate_does_not_restart_or_start_runtime(self):
        p = self.directory/"node-failure-gate.json"
        p.write_text('{"schema":1}')
        p.chmod(0o600)
        with mock.patch.object(runtime,"start") as start, mock.patch.object(runtime,"stop",return_value={"ok":True}):
            self.assertEqual(autostart.worker(self.profile),0)
        start.assert_not_called()
