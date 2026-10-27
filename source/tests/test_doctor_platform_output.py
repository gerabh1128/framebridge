import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "expanded"))
from framebridge_expanded import cli

class DoctorPlatformOutputTests(unittest.TestCase):
    def render(self, compatibility, ok):
        result = {"ok": ok, "compatible_debian_13_amd64": False,
                  "platform_compatibility": compatibility, "display": "unavailable"}
        runtime = mock.Mock()
        runtime.doctor.return_value = result
        output = io.StringIO()
        with mock.patch.object(cli, "_module", return_value=runtime), contextlib.redirect_stdout(output):
            code = cli._doctor_command(argparse.Namespace(json=False), Path("/unused-test-state"))
        return code, output.getvalue()

    def test_supported_ubuntu_uses_current_profile_not_legacy_debian_flag(self):
        code, text = self.render({"ok": True, "profile_id": "ubuntu24.04-amd64-py312", "issues": []}, True)
        self.assertEqual(code, 0)
        self.assertIn("Platform: compatible", text)
        self.assertIn("ubuntu24.04-amd64-py312", text)
        self.assertNotIn("Debian 13 amd64:", text)

    def test_incompatible_abi_is_explained_in_human_diagnostics(self):
        code, text = self.render({"ok": False, "profile_id": "mint22-amd64-py312",
                                 "issues": ["Installed artifact profile/ABI does not match this operating system and interpreter."]}, False)
        self.assertNotEqual(code, 0)
        self.assertIn("not verified compatible", text)
        self.assertIn("profile/ABI does not match", text)

if __name__ == "__main__":
    unittest.main()
