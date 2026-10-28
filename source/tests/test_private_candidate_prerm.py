"""Private package versions retain the fixed privileged-helper rollback floor.

Only a temporary maintainer script is run. Cleanup Python is replaced with a
recording stub; no installed helper, profile or package transaction is touched.
"""
import ast
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

WORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORK / "packaging"))
import build_expanded

PRIVATE_VERSION = "0.3.2+private.20261028.1"


class PrivateCandidatePrermTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = self.root / "DEBIAN"
        self.control.mkdir()
        self.log = self.root / "cleanup-calls.jsonl"
        self.python_stub = self.root / "record-cleanup"
        self.python_stub.write_text(
            "#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\n"
            + "with Path(" + repr(str(self.log)) + ").open('a') as stream:\n"
            + " stream.write(json.dumps(sys.argv[1:])+'\\n')\n")
        self.python_stub.chmod(0o755)

    def candidate_script(self, package_version=PRIVATE_VERSION):
        # Execute the real production write expression with an arbitrary package
        # VERSION, rather than reconstructing its floor selection in the test.
        tree = ast.parse(Path(build_expanded.__file__).read_text())
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        writers = [node for node in ast.walk(main)
                   if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                   and isinstance(node.value.func, ast.Attribute)
                   and node.value.func.attr == "write_text"
                   and node.value.args and isinstance(node.value.args[0], ast.Call)
                   and isinstance(node.value.args[0].func, ast.Name)
                   and node.value.args[0].func.id == "maintainer_prerm"]
        self.assertEqual(len(writers), 1)
        namespace = dict(control=self.control, VERSION=package_version,
                         PRIVILEGED_HELPER_FLOOR=build_expanded.PRIVILEGED_HELPER_FLOOR,
                         maintainer_prerm=build_expanded.maintainer_prerm)
        exec(compile(ast.Module(body=writers, type_ignores=[]), build_expanded.__file__, "exec"), namespace)
        script = self.control / "prerm"
        text = script.read_text()
        self.assertIn('"$2" lt 0.3.2;', text)
        self.assertNotIn(package_version, text)
        # The real dpkg is used only for its read-only version validator and
        # comparator. Every privileged cleanup command is a recording stub.
        script.write_text(text.replace("/usr/bin/python3", shlex.quote(str(self.python_stub))))
        return script

    def upgrade_target(self, target, package_version=PRIVATE_VERSION):
        script = self.candidate_script(package_version)
        result = subprocess.run(["/bin/sh", str(script), "upgrade", target],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_private_to_official_032_preserves_helper_grants(self):
        self.assertEqual(self.upgrade_target("0.3.2"), [])

    def test_target_below_032_revokes_helper_grants(self):
        calls = self.upgrade_target("0.3.1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ["-I", "-S", "-c"])
        self.assertIn('["purge_credential_helpers"]()', calls[0][3])

    def test_same_private_version_preserves_helper_grants(self):
        self.assertEqual(self.upgrade_target(PRIVATE_VERSION), [])

    def test_forward_upgrade_preserves_helper_grants(self):
        self.assertEqual(self.upgrade_target("0.3.3"), [])

    def test_arbitrary_future_package_version_keeps_the_032_floor(self):
        self.assertEqual(self.upgrade_target("0.3.2", package_version="9.9.9+private.1"), [])


if __name__ == "__main__":
    unittest.main()
