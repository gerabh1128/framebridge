"""Verify single-package LAN roles and saved role records."""
import contextlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE / "expanded"), str(SOURCE / "packaging")]
from framebridge_expanded import cli, installation, package_upgrade


ID = "6d88c5ae343e4413a202ccae2ae7694a"
SERVICE = installation.SERVICE_TYPE
HOST = {"id": ID, "role": "central", "host": "hub.local", "address": "192.168.16.10", "port": 20878}


class InstallationRoleTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch(installation, "DIRECTORY", self.root)
        self.patch(installation, "_installer_lock", contextlib.nullcontext)
        self.patch(installation, "_checked", side_effect=lambda path, **kw: path.lstat())
        self.patch(installation, "_lan_interfaces", return_value=("lan0", {"192.168.16.8"}))
        self.reservation = self.patch(installation, "_reservation", return_value=None)
        self.browse = self.patch(installation, "_browse")
        self.announce = self.patch(installation, "_announce")
        self.withdraw = self.patch(installation, "_withdraw")

    def patch(self, obj, name, value=mock.DEFAULT, **kwargs):
        patcher = mock.patch.object(obj, name, value, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_present_central_installs_node_only(self):
        self.browse.return_value = [HOST]
        result = installation.configure()
        self.assertEqual((result["role"], result["central_hint"]), ("node", "hub.local"))
        self.assertEqual({item.name for item in self.root.iterdir()}, {"role.json"})
        self.announce.assert_not_called()
        self.assertEqual(cli._new_profile()["role"], "node")

    def test_new_central_installs_only_after_observing_its_claim(self):
        advertised = []
        self.announce.side_effect = lambda identity, role, port: advertised.append(
            dict(HOST, id=identity, role=role, address="192.168.16.8", port=port))
        self.browse.side_effect = lambda interface: advertised[-1:]
        result = installation.configure()
        self.assertEqual(result["role"], "central")
        self.assertEqual([row["role"] for row in advertised], ["claim", "central", "central"])
        self.assertEqual({item.name for item in self.root.iterdir()}, {"role.json"})
        self.assertEqual(cli._new_profile()["role"], "central")
        self.assertEqual(installation.status()["source"], "lan_bootstrap")

    def test_competing_claim_keeps_role_pending(self):
        self.browse.side_effect = [[], [dict(HOST, role="claim")]]
        result = installation.configure()
        self.assertEqual(result["reason"], "LAN_CENTRAL_CLAIM_UNCONFIRMED")
        self.assertEqual({item.name for item in self.root.iterdir()}, {"role.json"})
        self.assertEqual(self.withdraw.call_count, 2)
        with self.assertRaisesRegex(RuntimeError, "pending"):
            cli._new_profile()

    def test_lan_failure_keeps_role_pending_until_retry(self):
        self.browse.side_effect = installation.DiscoveryPending("LAN_DISCOVERY_UNAVAILABLE")
        self.assertEqual(installation.configure()["role"], "pending")
        self.browse.side_effect = None
        self.browse.return_value = [HOST]
        self.assertEqual(installation.configure()["role"], "node")
        self.announce.assert_not_called()

    def test_legacy_central_reservation_prevents_promotion_while_broker_is_down(self):
        self.reservation.return_value = "hub.home.arpa"
        result = installation.configure()
        self.assertEqual(result["source"], "local_dns_reservation")
        self.assertEqual(cli._new_profile()["broker_url"], "https://hub.home.arpa:20878")
        self.browse.assert_not_called()

    def test_upgrade_reuses_previous_running_roles_and_never_browses_lan(self):
        self.patch(package_upgrade, "ROOT", self.root)
        self.patch(package_upgrade, "_private")
        read = self.patch(package_upgrade, "_read", return_value=b'{"profiles":[{"role":"node"}]}')
        self.assertEqual(installation.configure("0.3.6")["role"], "node")
        self.browse.assert_not_called()
        read.assert_called_once()
        self.announce.assert_not_called()

    def test_existing_central_keeps_port_offline(self):
        self.patch(package_upgrade, "ROOT", self.root)
        self.patch(package_upgrade, "_private")
        self.patch(package_upgrade, "_read", return_value=b'{"profiles":[{"role":"central","broker_port":22000}]}')
        self.announce.side_effect = installation.DiscoveryPending("LAN_DISCOVERY_UNAVAILABLE")
        result = installation.configure("0.3.6")
        self.assertEqual((result["role"], result["broker_port"], result["advertisement"]),
                         ("central", 22000, "daemon_unavailable"))
        self.assertEqual({item.name for item in self.root.iterdir()}, {"role.json"})
        self.browse.assert_not_called()

    def test_inactive_legacy_profiles_are_never_reclassified(self):
        self.patch(package_upgrade, "ROOT", self.root)
        self.patch(package_upgrade, "_private")
        self.patch(package_upgrade, "_read", side_effect=FileNotFoundError)
        self.assertEqual(installation.configure("0.3.6")["role"], "legacy")
        self.browse.assert_not_called()
        self.announce.assert_not_called()

    def test_rc3_role_record_retains_identity_and_port(self):
        state = installation._decision("central", "lan_bootstrap", identity=ID, broker_port=22000,
                                       panel_installed=True)
        path = self.root / "role.json"
        path.write_text(json.dumps(state))
        result = installation.configure("0.4.0~rc3")
        self.assertEqual((result["role"], result["identity"], result["broker_port"]), ("central", ID, 22000))
        self.assertNotIn("panel_installed", json.loads(path.read_text()))
        self.browse.assert_not_called()
        self.announce.assert_called_once_with(ID, "central", 22000)

    def test_parse_only_resolved_records_on_selected_link(self):
        suffix = f"FrameBridge on hub;{SERVICE};local"
        parsed = installation._parse_browse(
            f"+;lan1;IPv4;{suffix}\n+;lan0;IPv4;{suffix}\n"
            f'=;lan0;IPv4;{suffix};hub.local;192.168.16.10;20878;"role=central" "id={ID}" "v=1"\n',
            "lan0")
        self.assertEqual(parsed, [HOST])
        with self.assertRaisesRegex(installation.DiscoveryPending, "UNRESOLVED"):
            installation._parse_browse(f"+;lan0;IPv4;{suffix}\n", "lan0")
