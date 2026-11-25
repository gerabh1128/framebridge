"""Execute maintscript branches with cleanup commands intercepted in a tempdir."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from build_expanded import INSTALLATION, maintainer_preinst, maintainer_prerm, maintenance_hook, PRIVILEGED_HELPER_FLOOR
from build_profiles import CANDIDATE_VERSION


class MaintainerScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.trace = self.root / "calls.jsonl"
        self.stub = self.root / "cleanup-stub"
        self.stub.write_text(
            "#!" + sys.executable + "\n"
            "import json,os,sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "raise SystemExit(int(os.environ.get('TEST_CLEANUP_RC','0')))\n"
        )
        self.stub.chmod(0o755)
        self.screen = self.root / "screen-access.json"
        self.gate = self.root / "maintenance-stub"
        self.gate.write_text(
            "#!" + sys.executable + "\n"
            "import json,os,sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as stream: stream.write(json.dumps(['maintenance','begin'])+'\\n')\n"
            "raise SystemExit(int(os.environ.get('TEST_MAINTENANCE_RC','0')))\n"
        )
        self.gate.chmod(0o755)

    def run_script(self, args, *, fail=0, screen=False, gate_fail=0):
        script = maintainer_prerm(PRIVILEGED_HELPER_FLOOR).replace(maintenance_hook("begin"), shlex.quote(str(self.gate)) + "\n")
        # Only side-effecting commands and config probes are redirected. The
        # real dpkg comparisons are read-only and verify Debian version ordering.
        script = script.replace(INSTALLATION, ":")
        script = script.replace("/usr/bin/python3", shlex.quote(str(self.stub)))
        script = script.replace("/usr/lib/framebridge-expanded/bin/framebridge-screen-access", shlex.quote(str(self.stub)))
        script = script.replace("/etc/framebridge-expanded/screen-access.json", shlex.quote(str(self.screen)))
        if screen:
            self.screen.write_text("{}")
        env = dict(os.environ, TEST_CALLS=str(self.trace), TEST_CLEANUP_RC=str(fail), TEST_MAINTENANCE_RC=str(gate_fail))
        result = subprocess.run(["/bin/sh", "-s", "--", *args], input=script, text=True,
                                capture_output=True, env=env, timeout=10)
        calls = [json.loads(row) for row in self.trace.read_text().splitlines()] if self.trace.exists() else []
        return result, calls

    def test_downgrade_revokes_only_helper_before_replacement(self):
        result, calls = self.run_script(["upgrade", "0.3.1"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls.pop(0), ["maintenance", "begin"])
        self.assertEqual(calls[0][:3], ["-I", "-S", "-c"])
        self.assertEqual(calls[0][3], 'import runpy; runpy.run_path("/usr/lib/framebridge-expanded/expanded/framebridge_expanded/uninstall.py")["purge_credential_helpers"]()')
        self.assertNotIn("--all-users", calls[0])

    def test_forward_upgrade_retains_helper_state(self):
        result, calls = self.run_script(["upgrade", "0.3.2"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["maintenance", "begin"]])

    def test_same_version_reinstall_retains_helper_state(self):
        result, calls = self.run_script(["upgrade", CANDIDATE_VERSION])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["maintenance", "begin"]])

    def test_remove_and_deconfigure_keep_existing_cleanup(self):
        for operation in ("remove", "deconfigure"):
            if self.trace.exists():
                self.trace.unlink()
            result, calls = self.run_script([operation], screen=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, [["-I", "-S", "/usr/lib/framebridge-expanded/expanded/framebridge_expanded/uninstall.py", "--all-users"], ["disable"]])

    def test_failed_helper_cleanup_blocks_downgrade(self):
        result, calls = self.run_script(["upgrade", "0.3.1"], fail=17)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ["maintenance", "begin"])

    def test_busy_refusal_precedes_any_downgrade_cleanup(self):
        result, calls = self.run_script(["upgrade", "0.3.1"], gate_fail=17)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(calls, [["maintenance", "begin"]])

    def test_failed_upgrade_path_still_checks_gate(self):
        result, calls = self.run_script(["failed-upgrade", "0.3.1", CANDIDATE_VERSION], gate_fail=17)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(calls, [["maintenance", "begin"]])

    def test_invalid_target_does_not_execute_or_interpolate(self):
        marker = self.root / "must-not-exist"
        result, calls = self.run_script(["upgrade", "0.3.1;$(touch " + str(marker) + ")"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertFalse(marker.exists())

    def test_missing_target_fails_closed(self):
        result, calls = self.run_script(["upgrade"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_current_version_cannot_inject_script_paths(self):
        with self.assertRaises(ValueError):
            maintainer_prerm("0.3.2'; /tmp/untrusted-script; '")

    def run_preinst(self, arguments, *, version="0.3.4~dev.20261029.4", fail=0):
        script = maintainer_preinst(version).replace(maintenance_hook("begin"), shlex.quote(str(self.gate)) + "\n")
        result = subprocess.run(["/bin/sh", "-s", "--", *arguments], input=script,
                                text=True, capture_output=True, timeout=10,
                                env=dict(os.environ, TEST_CALLS=str(self.trace), TEST_MAINTENANCE_RC=str(fail)))
        calls = [json.loads(row) for row in self.trace.read_text().splitlines()] if self.trace.exists() else []
        return result, calls

    def test_same_version_reinstall_requires_cold_maintenance(self):
        result, calls = self.run_preinst(["upgrade", "0.3.4~dev.20261029.4"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["maintenance", "begin"]])

    def test_forward_upgrade_requires_cold_maintenance(self):
        result, calls = self.run_preinst(["upgrade", "0.3.4~dev.20261029.3"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["maintenance", "begin"]])

    def test_fresh_install_and_downgrade_require_maintenance(self):
        for arguments in (["install"], ["upgrade", "0.3.5"]):
            with self.subTest(arguments=arguments):
                self.trace.unlink(missing_ok=True)
                result, calls = self.run_preinst(arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [["maintenance", "begin"]])

    def test_037_compatible_rc_lineage_requires_maintenance(self):
        for previous in ("0.4.0~rc1", "0.4.0~rc2", "0.4.0~rc3"):
            with self.subTest(previous=previous):
                self.trace.unlink(missing_ok=True)
                result, calls = self.run_preinst(["upgrade", previous], version="0.3.7")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [["maintenance", "begin"]])

    def test_037_other_downgrades_also_require_maintenance(self):
        result, calls = self.run_preinst(["upgrade", "0.4.0~rc4"], version="0.3.7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["maintenance", "begin"]])

    def test_failed_maintenance_blocks_package_replacement(self):
        result, calls = self.run_preinst(["upgrade", "0.3.4~dev.20261029.4"], fail=17)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(calls, [["maintenance", "begin"]])


if __name__ == "__main__":
    unittest.main()
