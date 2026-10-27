import unittest
from unittest.mock import patch
from framebridge_expanded import fleet
from framebridge_expanded.real_screen.screen_frontdoor import resolve_profile_node, ScreenError

class CanonicalScreenNodeTests(unittest.TestCase):
    def test_remote_uuid_is_bound_to_confirmed_executor_alias(self):
        uid = "12345678-1234-1234-1234-123456789abc"
        with patch.object(fleet, "resolve_node", return_value={"transport": "broker_outbound", "alias": "worker-1", "node_id": uid}) as resolve:
            self.assertEqual(resolve_profile_node(uid), "worker-1")
        resolve.assert_called_once_with(uid)

    def test_receiver_identity_is_canonical_local(self):
        with patch.object(fleet, "resolve_node", return_value={"transport": "local", "alias": "central"}):
            self.assertEqual(resolve_profile_node("central"), "LOCAL")

    def test_unknown_alias_has_no_hostname_or_ssh_fallback(self):
        with patch.object(fleet, "resolve_node", side_effect=fleet.FleetError("NODE_NOT_FOUND")):
            with self.assertRaisesRegex(ScreenError, "NODE_NOT_FOUND"):
                resolve_profile_node("unconfirmed-host")

if __name__ == "__main__":
    unittest.main()
