import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = TEST_ROOT.parent
PACKAGE_ROOT = SOURCE_ROOT / 'expanded/framebridge_expanded'
FIXTURES = TEST_ROOT / 'fixtures/r4-035'
PACKAGE = 'r4_fleet_busy_test'
package = types.ModuleType(PACKAGE); package.__path__ = []
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + '.broker', PACKAGE_ROOT / 'broker.py')
broker = importlib.util.module_from_spec(spec); spec.loader.exec_module(broker)
sys.modules[PACKAGE + '.broker'] = broker; package.broker = broker
spec = importlib.util.spec_from_file_location(PACKAGE + '.fleet', PACKAGE_ROOT / 'fleet.py')
fleet = importlib.util.module_from_spec(spec); spec.loader.exec_module(fleet)

# The existing screen schema/validation is unchanged by this patch. A mock here
# isolates whether the typed route propagates liveness errors before dispatch.
screen = types.ModuleType(PACKAGE + '.real_screen'); screen.__path__ = []
transport = types.ModuleType(PACKAGE + '.real_screen.screen_transport')
transport.REQUEST_TYPE = 'screen_rpc'; transport.validate_payload = lambda p: None
transport.ScreenTransportError = type('ScreenTransportError', (ValueError,), {})
sys.modules[PACKAGE + '.real_screen'] = screen
sys.modules[PACKAGE + '.real_screen.screen_transport'] = transport

NODE_ID = '11111111-1111-4111-8111-111111111111'


class FleetBusyTests(unittest.TestCase):
    def descriptor(self, ready, busy):
        return {'alias': 'TESTNODE', 'node_id': NODE_ID, 'hostname': 'test', 'transport': 'broker_outbound',
                'operational_transport_ready': ready, 'operational_transport_busy': busy}

    def route(self, name, ready, busy, broker_error=None):
        def request(node_id, operation, payload, **kwargs):
            if broker_error is not None: raise broker_error
            return {'ok': True, 'job_id': payload.get('job_id'), 'command_sha256': payload.get('sha256')}
        with patch.object(fleet, 'resolve_node', return_value=self.descriptor(ready, busy)), patch.object(broker, 'request', side_effect=request) as dispatch, patch.object(fleet, '_normalize_read', side_effect=lambda cap, args: args):
            if name == 'exec': response = fleet.execute_node('TESTNODE', 'printf public', True, job_id='test')
            elif name == 'screen': response = fleet.execute_screen_node('TESTNODE', 'public-canonical-placeholder', True, job_id='test', include_stdout=True)
            elif name == 'cua': response = fleet.cua_node('TESTNODE', {'action': 'OBSERVE'}, True, job_id='test')
            else: response = fleet.node_read('TESTNODE', 'system.status', {})
            return response, dispatch.call_count

    def test_descriptor_propagates_busy_and_freshness(self):
        raw = {'node_id': NODE_ID, 'alias': 'TESTNODE', 'ready': False, 'busy': True,
               'heartbeat_fresh': False, 'activity_state': 'busy-heartbeat-stale', 'last_seen_age_seconds': 7.2}
        with patch.object(broker, 'list_confirmed_nodes', return_value=[raw]):
            value = fleet._remote_descriptors({'role': 'central'})[0]
        self.assertFalse(value['operational_transport_ready'])
        self.assertTrue(value['operational_transport_busy'])
        self.assertEqual(value['activity_state'], 'busy-heartbeat-stale')
        self.assertEqual(value['last_seen_age_seconds'], 7.2)

    def test_frozen_metadata_defaults_to_existing_ready_policy(self):
        with patch.object(broker, 'list_confirmed_nodes', return_value=[{'node_id': NODE_ID, 'alias': 'TESTNODE', 'ready': True}]):
            value = fleet._remote_descriptors({'role': 'central'})[0]
        self.assertTrue(value['operational_transport_ready'])
        self.assertFalse(value['operational_transport_busy'])
        self.assertEqual(value['activity_state'], 'idle')

    def test_all_remote_routes_report_stale_busy_without_dispatch(self):
        for route in ['read', 'exec', 'screen', 'cua']:
            with self.subTest(route=route):
                response, count = self.route(route, False, True)
                self.assertEqual(response['error'], 'NODE_BUSY_HEARTBEAT_STALE')
                self.assertEqual(count, 0)
                self.assertEqual(response['node_id'], NODE_ID)

    def test_all_remote_routes_still_reject_actually_offline_node(self):
        for route in ['read', 'exec', 'screen', 'cua']:
            with self.subTest(route=route):
                response, count = self.route(route, False, False)
                self.assertEqual(response['error'], 'NODE_OFFLINE'); self.assertEqual(count, 0)

    def test_all_remote_routes_preserve_queue_when_fresh_and_busy(self):
        for route in ['read', 'exec', 'screen', 'cua']:
            with self.subTest(route=route):
                response, count = self.route(route, True, True)
                self.assertTrue(response['ok']); self.assertEqual(count, 1)

    def test_race_to_stale_busy_between_descriptor_and_broker_is_not_masked(self):
        for route in ['read', 'exec', 'screen', 'cua']:
            with self.subTest(route=route):
                response, count = self.route(route, True, False, broker.BrokerError('NODE_BUSY_HEARTBEAT_STALE'))
                self.assertEqual(response['error'], 'NODE_BUSY_HEARTBEAT_STALE'); self.assertEqual(count, 1)

    def test_arbitrary_broker_or_runtime_exception_text_is_not_published(self):
        for error in [broker.BrokerError('private diagnostic'), RuntimeError('private diagnostic')]:
            response, count = self.route('exec', True, False, error)
            self.assertEqual(response['error'], 'NODE_EXEC_FAILED')

    def test_authorization_gate_is_not_relaxed_for_busy_node(self):
        response = fleet.execute_node('TESTNODE', 'printf public', False)
        self.assertEqual(response['error'], 'AUTHORIZED_TRUE_REQUIRED')


if __name__ == '__main__': unittest.main(verbosity=2)
