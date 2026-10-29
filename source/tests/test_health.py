import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "expanded"))
from framebridge_expanded import health


NODE_ID = "51f0acf5-3633-41f6-aea7-4fed5cffcb01"


def snapshots():
    return ({"ok": True, "configured": True, "role": "central", "name": "gateway",
             "runtime": {"running": True, "ready": True, "node_id": NODE_ID,
                         "display_context": {"kind": "text"},
                         "capabilities": {"fbexec": True, "fbterm": True, "cua_tty_module": True, "graphical_cua": False}},
             "connection": {"verified": True, "state": "ready"},
             "boot": {"configured": True, "enabled": False, "service_state": "active"},
             "desired_connection": "connected"},
            {"ok": True, "platform_compatibility": {"ok": True, "profile_id": "debian13-amd64-py313", "validation_status": "validated", "issues": []},
             "issues": [], "missing_commands": [], "capabilities": {"fbexec": True, "fbterm": True, "cua_tty_module": True, "graphical_cua": False}})


class HealthTests(unittest.TestCase):
    def test_release_build_identity_is_retained_without_relabelling_it_validated(self):
        status, doctor = snapshots()
        doctor["platform_compatibility"]["validation_status"] = "release_build"
        value = health.build_report(status, doctor)
        self.assertTrue(value["ok"])
        self.assertEqual(value["prerequisites"]["validation_status"], "release_build")
        self.assertNotEqual(value["prerequisites"]["validation_status"], "validated")

    def test_healthy_text_mode_does_not_require_graphical_tools_or_boot_enabled(self):
        status, doctor = snapshots()
        value = health.build_report(status, doctor, observed_at="2026-10-28T12:00:00+00:00", host="gateway")
        self.assertTrue(value["ok"])
        self.assertEqual(value["health"], {"state": "healthy", "healthy": True, "issues": []})
        self.assertEqual(value["boot"]["policy"], "disabled")
        self.assertEqual(value["effective_target"]["node_id"], NODE_ID)
        self.assertFalse(value["desktop"]["capture_verified_by_this_report"])
        self.assertIsNone(value["software"]["runtime_version"])

    def test_query_success_is_not_readiness(self):
        status, doctor = snapshots()
        status["runtime"].update(running=False, ready=False)
        status["connection"].update(verified=False, state="stopped")
        value = health.build_report(status, doctor)
        self.assertTrue(value["ok"])
        self.assertEqual(value["health"]["state"], "degraded")
        self.assertFalse(value["health"]["healthy"])
        self.assertIn("LOCAL_TOOLS_NOT_READY", value["health"]["issues"])

    def test_paused_connection_is_distinct_from_failure(self):
        status, doctor = snapshots()
        status["desired_connection"] = "disconnected"
        status["connection"].update(verified=False, state="stopped")
        value = health.build_report(status, doctor)
        self.assertEqual(value["health"]["state"], "paused")
        self.assertTrue(value["health"]["healthy"])
        self.assertEqual(value["health"]["issues"], [])

    def test_node_does_not_claim_central_connection_verified(self):
        status, doctor = snapshots()
        status.update(role="node", desired_connection="not_applicable")
        value = health.build_report(status, doctor)
        self.assertEqual(value["connection"]["state"], "managed_by_central")
        self.assertIsNone(value["connection"]["verified"])
        self.assertEqual(value["health"]["state"], "healthy")

    def test_requested_pause_does_not_hide_a_still_running_tunnel(self):
        status, doctor = snapshots()
        status["desired_connection"] = "disconnected"
        value = health.build_report(status, doctor)
        self.assertEqual(value["health"]["state"], "degraded")
        self.assertIn("CONNECTION_INTENT_MISMATCH", value["health"]["issues"])
        status["connection"].update(verified=False, state="stopped", alive=True)
        value = health.build_report(status, doctor)
        self.assertFalse(value["health"]["healthy"])

    def test_failed_connection_query_cannot_certify_a_healthy_profile(self):
        status, doctor = snapshots()
        status["connection"]["ok"] = False
        value = health.build_report(status, doctor)
        self.assertFalse(value["ok"])
        self.assertIsNone(value["health"]["healthy"])
        self.assertIn("CONNECTION_STATUS_UNAVAILABLE", value["health"]["issues"])

    def test_missing_boot_policy_is_unknown_rather_than_healthy(self):
        status, doctor = snapshots()
        status["boot"] = {}
        value = health.build_report(status, doctor)
        self.assertFalse(value["ok"])
        self.assertEqual(value["health"]["state"], "unavailable")

    def test_missing_runtime_status_is_not_a_stopped_runtime(self):
        status, doctor = snapshots()
        status["runtime"] = {"status_unavailable": True, "running": False, "ready": False}
        value = health.build_report(status, doctor)
        self.assertFalse(value["ok"])
        self.assertEqual(value["runtime"]["state"], "unavailable")
        self.assertEqual(value["health"]["state"], "unavailable")
        self.assertIsNone(value["health"]["healthy"])

    def test_failed_runtime_query_cannot_certify_ready(self):
        status, doctor = snapshots()
        status["runtime"]["ok"] = False
        value = health.build_report(status, doctor)
        self.assertFalse(value["ok"])
        self.assertEqual(value["health"]["state"], "unavailable")

    def test_failed_profile_query_cannot_claim_unconfigured(self):
        value = health.build_report({"ok": False, "configured": False}, None)
        self.assertFalse(value["ok"])
        self.assertEqual(value["health"]["state"], "unavailable")

    def test_missing_diagnostic_result_is_unavailable_not_passed(self):
        status, _ = snapshots()
        value = health.build_report(status, {})
        self.assertFalse(value["ok"])
        self.assertEqual(value["health"]["state"], "unavailable")

    def test_unconfigured_profile_is_observed_without_running_doctor_or_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "not-created"
            with mock.patch.object(health.runtime, "doctor", side_effect=AssertionError("doctor must not run")):
                value = health.collect(state)
            self.assertFalse(state.exists())
            self.assertTrue(value["ok"])
            self.assertEqual(value["health"]["state"], "unconfigured")
            self.assertEqual(value["health"]["issues"], [])
            self.assertEqual(value["prerequisites"]["state"], "not_run")

    def test_diagnostics_failure_retains_status_without_leaking_exception(self):
        status, _ = snapshots()
        with mock.patch.object(health.lifecycle, "status", return_value=status), \
             mock.patch.object(health.runtime, "doctor", side_effect=RuntimeError("secret-access-key")):
            value = health.collect(Path("/unused"))
        self.assertFalse(value["ok"])
        self.assertEqual(value["runtime"]["state"], "ready")
        self.assertEqual(value["health"]["state"], "unavailable")
        self.assertNotIn("secret-access-key", json.dumps(value))

    def test_partial_invalid_profile_does_not_guess_it_is_unconfigured(self):
        with mock.patch.object(health.lifecycle, "status", side_effect=ValueError("private-input")), \
             mock.patch.object(health.runtime, "doctor") as doctor:
            value = health.collect(Path("/unused"))
        doctor.assert_not_called()
        self.assertFalse(value["ok"])
        self.assertEqual(value["health"]["state"], "unavailable")
        self.assertNotIn("private-input", json.dumps(value))

    def test_allowlist_excludes_connection_material_paths_and_untrusted_free_text(self):
        status, doctor = snapshots()
        status["tunnel_id"] = "secret-tunnel"
        status["runtime"].update(state_dir="/private/secret-path", error="secret-error", access_key="secret-key")
        status["runtime"]["display_context"]["authority"] = "/private/secret-authority"
        status["connection"].update(message="secret-message", log="/private/secret-log", token="secret-token")
        doctor.update(network={"token": "secret-network"}, issues=["secret-issue"])
        doctor["platform_compatibility"]["issues"] = ["secret-compatibility"]
        result = health.build_report(status, doctor, host="host")
        self.assertNotIn("secret-", json.dumps(result))
        self.assertEqual(result["prerequisites"]["issue_count"], 1)
        self.assertEqual(result["prerequisites"]["compatibility_issue_count"], 1)

    def test_prerequisite_failure_is_degraded_without_claiming_no_runtime(self):
        status, doctor = snapshots()
        doctor.update(ok=False, missing_commands=["tmux"])
        value = health.build_report(status, doctor)
        self.assertTrue(value["ok"])
        self.assertEqual(value["health"]["state"], "degraded")
        self.assertEqual(value["runtime"]["state"], "ready")
        self.assertEqual(value["prerequisites"]["missing_commands"], ["tmux"])

    def test_reports_do_not_modify_snapshots(self):
        status, doctor = snapshots()
        before = copy.deepcopy((status, doctor))
        health.build_report(status, doctor)
        self.assertEqual((status, doctor), before)

    def test_terminal_output_is_explicit_about_target_and_observation_limit(self):
        status, doctor = snapshots()
        value = health.build_report(status, doctor, host="gateway")
        text = health.render(value)
        self.assertIn("Target: LOCAL = this computer (gateway)", text)
        self.assertIn(NODE_ID, text)
        self.assertIn("capture/input not tested", text)
        self.assertIn("not proof of the running version", text)
        self.assertIn("not a fleet scan", text)


if __name__ == "__main__":
    unittest.main()
