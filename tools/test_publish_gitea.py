#!/usr/bin/env python3
"""Check the memory-only credential transport without real credentials or networking."""
import importlib.util
import base64
from unittest import mock
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("publisher", Path(__file__).with_name("publish_gitea.py"))
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)

class AskpassTests(unittest.TestCase):
    def test_transport_keeps_disposable_token_out_of_script_and_environment(self):
        with tempfile.TemporaryDirectory(prefix="framebridge-askpass-test-") as tmp:
            token = "a" * 40  # Disposable test data, not a credential.
            server, stop, thread, client = publisher.askpass_server(Path(tmp), token)
            try:
                self.assertEqual(stat.S_IMODE(client.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((Path(tmp) / "credential.sock").stat().st_mode), 0o600)
                self.assertNotIn(token, client.read_text())
                for prompt, expected in [("Username for 'http://127.0.0.1:3005':", b"gerardo\n"),
                                         ("Password for 'http://gerardo@127.0.0.1:3005':", (token + "\n").encode())]:
                    result = subprocess.run([str(client), prompt], capture_output=True, check=True, timeout=5)
                    self.assertEqual(result.stdout, expected)
                    self.assertEqual(result.stderr, b"")
            finally:
                stop.set()
                thread.join(timeout=2)
                server.close()

    def test_unknown_prompt_returns_no_token(self):
        with tempfile.TemporaryDirectory(prefix="framebridge-askpass-test-") as tmp:
            server, stop, thread, client = publisher.askpass_server(Path(tmp), "b" * 40)
            try:
                result = subprocess.run([str(client), "Unexpected prompt"], capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
            finally:
                stop.set()
                thread.join(timeout=2)
                server.close()

    def test_token_management_uses_normal_basic_auth_in_memory(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 204
        response.__enter__.return_value.read.return_value = b""
        disposable = "c" * 40
        with mock.patch.object(publisher.urllib.request, "urlopen", return_value=response) as opener:
            status, _ = publisher.api_request("DELETE", "/users/gerardo/tokens/disposable", disposable)
        self.assertEqual(status, 204)
        request = opener.call_args.args[0]
        expected = "Basic " + base64.b64encode(("gerardo:" + disposable).encode()).decode()
        self.assertEqual(request.get_header("Authorization"), expected)
        self.assertNotIn(disposable, request.full_url)

    def test_incomplete_journal_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="framebridge-journal-test-") as tmp:
            target = Path(tmp) / "transaction.json"
            partial = target.with_suffix(".partial")
            partial.write_text("unreviewed incomplete state")
            with self.assertRaises(FileExistsError):
                publisher.record(target, {"phase": "NEW"})
            self.assertFalse(target.exists())
            self.assertEqual(partial.read_text(), "unreviewed incomplete state")

if __name__ == "__main__":
    unittest.main(verbosity=2)
