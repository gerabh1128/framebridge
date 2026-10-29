"""Exercise the public health command without starting a profile or transport."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from framebridge_expanded import cli, health, __version__


class HealthCliTests(unittest.TestCase):
    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(arguments)
        return code, output.getvalue(), errors.getvalue()

    def test_absent_profile_is_reported_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "absent-profile"
            code, text, _ = self.invoke(["--state-dir", str(profile), "--json", "health"])
            self.assertFalse(profile.exists())
        value = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual(value["version"], __version__)
        self.assertEqual(value["command"], "health")
        self.assertTrue(value["ok"])
        self.assertEqual(value["result"]["health"]["state"], "unconfigured")
        self.assertFalse(value["result"]["health"]["healthy"])
        self.assertTrue(value["result"]["read_only"])

    def test_query_success_does_not_make_exit_status_healthy(self):
        for state, healthy, expected in (("healthy", True, 0), ("paused", True, 0),
                                         ("degraded", False, 1), ("unavailable", None, 1)):
            with self.subTest(state=state):
                report = {"ok": True, "health": {"state": state, "healthy": healthy}}
                module = SimpleNamespace(collect=mock.Mock(return_value=report))
                with mock.patch.object(cli, "_module", return_value=module):
                    code, text, _ = self.invoke(["health", "--json"])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(text)["result"], report)

    def test_failed_observation_cannot_pass_even_with_a_healthy_flag(self):
        module = SimpleNamespace(collect=mock.Mock(return_value={"ok": False, "health": {"healthy": True}}))
        with mock.patch.object(cli, "_module", return_value=module):
            code, text, _ = self.invoke(["--json", "health"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(text)["ok"])

    def test_human_summary_names_the_local_machine_and_its_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, text, _ = self.invoke(["health", "--state-dir", str(Path(temporary) / "absent")])
        self.assertEqual(code, 1)
        self.assertIn("Target: LOCAL = this computer", text)
        self.assertIn("unconfigured", text)
        self.assertIn("not a fleet scan", text)

    def test_json_output_never_prompts_for_authentication(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch("builtins.input") as ask:
            code, text, _ = self.invoke(["health", "--json", "--state-dir", str(Path(temporary) / "absent")])
        ask.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIsNone(json.loads(text)["result"]["software"]["runtime_version"])


if __name__ == "__main__":
    unittest.main()
