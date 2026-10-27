import unittest
from pathlib import Path
import tempfile
from unittest import mock
from framebridge_expanded import network

def interface(name="eth0", *, address=True, flags=None, scope="global"):
    return {"ifname": name, "flags": ["UP", "LOWER_UP"] if flags is None else flags,
            "operstate": "UP", "addr_info": [
                {"family": "inet", "local": "192.0.2.20", "prefixlen": 24, "scope": scope}
            ] if address else []}

def route(name="eth0"):
    return {"dst": "default", "dev": name, "gateway": "192.0.2.1"}

class NetworkTests(unittest.TestCase):
    def test_working_ifupdown_is_not_misreported_from_nm_unmanaged(self):
        value = network.summarize([interface()], {"ipv4": [route()]},
            {"eth0": {"managed": False, "state": "10 (unmanaged)"}}, {"eth0"})
        self.assertEqual(value["connectivity"], "LINK_ADDRESS_DEFAULT_ROUTE")
        self.assertEqual(value["interfaces"][0]["manager"], "ifupdown")
        self.assertFalse(value["internet_verified"])
        self.assertEqual(value["warnings"][0]["code"], "CONNECTED_INTERFACE_NOT_MANAGED_BY_NETWORKMANAGER")

    def test_manager_connected_state_does_not_substitute_for_an_address(self):
        value = network.summarize([interface(address=False)], {"ipv4": [route()]},
            {"eth0": {"managed": True, "state": "100 (connected)"}})
        self.assertEqual(value["connectivity"], "NOT_ESTABLISHED")

    def test_address_without_default_route_is_not_internet_claim(self):
        value = network.summarize([interface()], {"ipv4": []})
        self.assertEqual(value["connectivity"], "NOT_ESTABLISHED")
        self.assertFalse(value["internet_verified"])

    def test_loopback_does_not_establish_external_connectivity(self):
        value = network.summarize([interface("lo")], {"ipv4": [route("lo")]},
            {"lo": {"managed": True, "state": "100 (connected (externally))"}})
        self.assertEqual(value["connectivity"], "NOT_ESTABLISHED")

    def test_interface_down_is_not_ready_even_with_residual_routes(self):
        value = network.summarize([interface(flags=[])], {"ipv4": [route()]})
        self.assertFalse(value["interfaces"][0]["local_route_ready"])

    def test_ipv6_global_address_and_route_are_supported(self):
        row = interface()
        row["addr_info"] = [{"family": "inet6", "local": "2001:db8::20", "prefixlen": 64, "scope": "global"}]
        value = network.summarize([row], {"ipv6": [{"dst": "default", "dev": "eth0", "gateway": "fe80::1"}]})
        self.assertEqual(value["connectivity"], "LINK_ADDRESS_DEFAULT_ROUTE")

    def test_unmanaged_is_not_automatically_named_ifupdown(self):
        value = network.summarize([interface()], {"ipv4": [route()]},
            {"eth0": {"managed": False, "state": "10 (unmanaged)"}})
        self.assertEqual(value["interfaces"][0]["manager"], "unverified")

    def test_missing_kernel_data_returns_explicit_failure(self):
        with mock.patch.object(network, "_json", return_value=None), \
             mock.patch.object(network.shutil, "which", return_value=None), \
             mock.patch.object(network, "_active", return_value=False):
            value = network.inspect()
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"], "NETWORK_KERNEL_STATUS_UNAVAILABLE")
        self.assertFalse(value["internet_verified"])

    def test_ifupdown_state_contains_names_only_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as path:
            state = Path(path) / "ifstate"
            state.write_text("lo=lo\neth0=eth0\nunsafe command=value\n")
            state.chmod(0o600)
            self.assertEqual(network._ifupdown(state), {"lo", "eth0"})
            link = Path(path) / "link"
            link.symlink_to(state)
            self.assertEqual(network._ifupdown(link), set())

    def test_nm_parser_accepts_only_device_metadata(self):
        output = ("GENERAL.DEVICE:eth0\nGENERAL.STATE:10 (unmanaged)\nGENERAL.NM-MANAGED:no\n"
                  "GENERAL.DEVICE:eth\\:bad\nGENERAL.STATE:100 (connected)\nGENERAL.NM-MANAGED:yes\n")
        with mock.patch.object(network, "_command", return_value=(0, output)) as command:
            devices = network._nm_devices()
        self.assertEqual(devices, {"eth0": {"managed": False, "state": "10 (unmanaged)"}})
        self.assertNotIn("connection", command.call_args.args[0])

if __name__ == "__main__":
    unittest.main()
