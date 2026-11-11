"""An operation still waiting at the node adapter cannot start after its deadline."""
from types import SimpleNamespace
import unittest
from unittest import mock

from framebridge_expanded.adapter import install_start_deadline_guard


class AdapterStartDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.original = mock.Mock(return_value={"ok": True})
        self.adapter = SimpleNamespace(handle=self.original)
        install_start_deadline_guard(self.adapter)

    def test_expired_socket_request_never_reaches_any_adapter_route(self):
        for request_type in ("screen_rpc", "cua", None):
            with self.subTest(request_type=request_type), mock.patch(
                    "framebridge_expanded.adapter.time.time", return_value=100):
                result = self.adapter.handle({"start_before": 100, "request_type": request_type})
            self.assertEqual(result["execution_state"], "expired_before_start")
            self.assertEqual(result["error"], "REQUEST_EXPIRED")
        self.original.assert_not_called()

    def test_valid_deadline_does_not_change_the_original_wire_payload(self):
        request = {"job_id": "a", "start_before": 101}
        with mock.patch("framebridge_expanded.adapter.time.time", return_value=100):
            self.assertEqual(self.adapter.handle(request), {"ok": True})
        self.original.assert_called_once_with({"job_id": "a"})
        self.assertEqual(request["start_before"], 101)

    def test_non_finite_deadline_is_rejected(self):
        for deadline in (float("nan"), float("inf"), True, "101"):
            with self.subTest(deadline=deadline):
                result = self.adapter.handle({"start_before": deadline})
            self.assertEqual(result["error"], "INVALID_START_DEADLINE")
        self.original.assert_not_called()
