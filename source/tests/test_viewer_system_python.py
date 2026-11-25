"""Graphical viewer must use system GI even when the supervisor is portable."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"expanded"))
from framebridge_expanded import viewer

class SystemUIInterpreter(unittest.TestCase):
    def test_private_supervisor_launches_system_ui_without_vendor_abi(self):
        desktop={"FRAMEBRIDGE_EXPANDED_STATE":"/synthetic/profile","DISPLAY":":0",
                 "HOME":"/synthetic/home","XDG_RUNTIME_DIR":"/synthetic/runtime",
                 "DBUS_SESSION_BUS_ADDRESS":"unix:path=/synthetic/bus",
                 "PYTHONPATH":"/synthetic/vendor-cp313","PYTHONHOME":"/synthetic/runtime",
                 "PYTHONEXECUTABLE":"/synthetic/bin/python","PYTHONUSERBASE":"/synthetic/user",
                 "LD_LIBRARY_PATH":"/synthetic/lib"}
        original=dict(desktop)
        with patch.object(sys,"executable","/usr/lib/framebridge-expanded/python/bin/python3.13"),patch.object(subprocess,"Popen") as run:
            viewer._launch_unified(desktop)
        argv=run.call_args.args[0];env=run.call_args.kwargs["env"]
        self.assertEqual(argv,["/usr/bin/python3","-B","-s","-m","framebridge_expanded.unified_viewer","--profile","/synthetic/profile"])
        self.assertEqual(env["PYTHONPATH"],str(Path(viewer.__file__).resolve().parents[1]))
        for k in ("PYTHONHOME","PYTHONEXECUTABLE","PYTHONUSERBASE","LD_LIBRARY_PATH"):
            self.assertNotIn(k,env)
        for k in ("FRAMEBRIDGE_EXPANDED_STATE","DISPLAY","HOME","XDG_RUNTIME_DIR","DBUS_SESSION_BUS_ADDRESS"):
            self.assertEqual(env[k],desktop[k])
        self.assertEqual(env["PYTHONNOUSERSITE"],"1")
        self.assertTrue(run.call_args.kwargs["start_new_session"])
        self.assertEqual(desktop,original)
    def test_missing_system_ui_fails_closed(self):
        with patch.object(subprocess,"Popen",side_effect=OSError):
            with self.assertRaisesRegex(viewer.Waiting,"viewer_launch_failed"):
                viewer._launch_unified({"FRAMEBRIDGE_EXPANDED_STATE":"/synthetic/profile"})
if __name__=="__main__":unittest.main()
