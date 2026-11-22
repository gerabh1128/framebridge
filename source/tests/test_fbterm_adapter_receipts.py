"""Preserve FBTERM dispatch states through the adapter and Fleet."""
import hashlib
import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from framebridge_expanded import broker, fleet, runtime


ADAPTER = Path(__file__).resolve().parents[1] / (
    "sources/original-workstation/tool-adapter/bin/framebridge_tool_adapter.py"
)
SPEC = importlib.util.spec_from_file_location("test_original_fbterm_adapter", ADAPTER)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class AdapterReceipts(unittest.TestCase):
    SCRIPT = "printf ready"
    JOB_ID = "fbterm-receipt-test"

    def setUp(self):
        inbox = TemporaryDirectory()
        self.addCleanup(inbox.cleanup)
        patcher = patch.object(adapter, "INBOX", Path(inbox.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def handle(self, rc, stdout):
        request = {
            "version": 1, "authorized": True, "mode": "EXEC",
            "job_id": self.JOB_ID, "script": self.SCRIPT,
        }
        process = CompletedProcess([], rc, stdout=stdout, stderr="private stderr")
        with patch.object(adapter.subprocess, "run", return_value=process):
            return adapter.handle(request)

    def test_confirmed_failures_report_state_without_private_diagnostic(self):
        for rc, failure, state, do_not_replay in (
            (70, "NOT_STARTED", "not_started", False),
            (74, "OUTCOME_UNKNOWN", "outcome_unknown", True),
        ):
            with self.subTest(rc=rc):
                result = self.handle(rc, json.dumps({
                    "ERROR": failure, "MESSAGE": "private diagnostic",
                }) + "\n")
                self.assertFalse(result["ok"])
                self.assertEqual(result["job_id"], self.JOB_ID)
                self.assertEqual(result["engine_rc"], rc)
                self.assertEqual(result["error"], "FBTERM_" + failure)
                self.assertEqual(result["adapter_error"], result["error"])
                self.assertEqual(result["execution_state"], state)
                self.assertEqual(result.get("do_not_replay", False), do_not_replay)
                self.assertNotIn("exit_code", result)
                self.assertNotIn("stdout", result)
                self.assertNotIn("private", str(result))

    def test_status_requires_one_receipt_matching_the_engine_return_code(self):
        for rc, stdout in (
            (75, '{"ERROR":"NOT_STARTED","MESSAGE":"unmatched"}'),
            (70, '{"ERROR":"OUTCOME_UNKNOWN","MESSAGE":"unmatched"}'),
            (74, '{"ERROR":"NOT_STARTED","MESSAGE":"unmatched"}'),
            (70, '{"ERROR":"NOT_STARTED"}'),
            (74, '{"ERROR":"OUTCOME_UNKNOWN","MESSAGE":1}'),
            (70, '{}\n{"ERROR":"NOT_STARTED","MESSAGE":"extra record"}'),
        ):
            with self.subTest(rc=rc, stdout=stdout):
                result = self.handle(rc, stdout)
                self.assertEqual(result["engine_rc"], rc)
                self.assertEqual(result["execution_state"], "outcome_unknown")
                self.assertTrue(result["do_not_replay"])

    def test_ordinary_command_exit_and_busy_engine_keep_existing_fields(self):
        command = self.handle(0, json.dumps({"JOB_ID":self.JOB_ID, "EXIT":"70",
                              "COMMAND_HASH_GATE":"PASS", "COMMAND_SHA256": hashlib.sha256(self.SCRIPT.encode()).hexdigest()}))
        self.assertTrue(command["ok"])
        self.assertEqual(command["exit_code"], "70")
        self.assertEqual(command["command_hash_gate"], "PASS")

        busy = self.handle(75, '{"ERROR":"BUSY","MESSAGE":"FBTERM ocupado"}')
        self.assertEqual(busy["engine_rc"], 75)
        self.assertIsNone(busy.get("exit_code"))
        self.assertFalse(busy["ok"])
        self.assertEqual(busy["execution_state"], "not_started")

    def test_fleet_dispatch_and_status_preserve_the_adapter_state(self):
        node = {
            "alias": "TESTNODE", "node_id": "11111111-1111-4111-8111-111111111111",
            "operational_transport_ready": True,
        }
        for rc, failure in adapter.ENGINE_FAILURES.items():
            result = self.handle(rc, json.dumps({"ERROR": failure, "MESSAGE": "test"}))
            state = failure.lower()
            for transport in ("local", "broker_outbound"):
                with self.subTest(rc=rc, transport=transport):
                    descriptor = dict(node, transport=transport)
                    with (
                        patch.object(fleet, "resolve_node", return_value=descriptor),
                        patch.object(runtime, "adapter_request", return_value=result),
                        patch.object(broker, "request", return_value=result),
                        patch.object(broker, "request_status", return_value={
                            "state": "complete", "result": result, "job_id": self.JOB_ID,
                        }),
                    ):
                        response = fleet.execute_node("TESTNODE", self.SCRIPT, True, job_id=self.JOB_ID)
                        self.assertFalse(response["ok"])
                        self.assertEqual(response["execution_state"], state)
                        self.assertEqual(response["mcp_job_id"], self.JOB_ID)
                        self.assertNotIn("exit_code", response)
                        self.assertEqual(response.get("do_not_replay", False), rc == 74)

                        if transport == "broker_outbound":
                            status = fleet.execution_status("TESTNODE", job_id=self.JOB_ID)
                            self.assertTrue(status["ok"])
                            self.assertEqual(status["execution_state"], state)
                            self.assertEqual(status["mcp_job_id"], self.JOB_ID)
                            self.assertNotIn("exit_code", status)
                            self.assertEqual(status["do_not_replay"], rc == 74)
