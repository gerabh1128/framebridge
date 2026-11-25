"""Local CLI routing for the profile-specific Original viewer controls."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from framebridge_expanded import cli, viewer


class ViewerControl(unittest.TestCase):
    def test_explicit_cli_control_never_starts_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "profile"
            root = state / "home/.local/share/framebridge/fbterm"
            root.mkdir(parents=True, mode=0o700)
            state.chmod(0o700)
            with patch.object(cli, "resolve_state", return_value=state):
                for action, expected in (("status", False), ("pause", True), ("status", True), ("resume", False)):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        rc = cli.main(["--state-dir", str(state), "--json", "viewer", action])
                    self.assertEqual(rc, 0, output.getvalue())
                    envelope = json.loads(output.getvalue())
                    self.assertTrue(envelope["ok"])
                    result = envelope["result"]
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["paused"], expected)
                    self.assertTrue(result["execution_unchanged"])
            self.assertFalse((root / "queue-v2").exists())


if __name__ == "__main__":
    unittest.main()
