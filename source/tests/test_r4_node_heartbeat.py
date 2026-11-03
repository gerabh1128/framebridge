"""Protocol/control-flow regressions; no network, shell, GUI or private keys."""
import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from r4_ast import semantic_ast_digest

TEST_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = TEST_ROOT.parent
PACKAGE_ROOT = SOURCE_ROOT / 'expanded/framebridge_expanded'
FIXTURES = TEST_ROOT / 'fixtures/r4-035'
PACKAGE = '_r4_node_candidate'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(PACKAGE_ROOT)]
sys.modules[PACKAGE] = package
broker = types.ModuleType(PACKAGE + '.broker')
broker.BrokerError = type('BrokerError', (ValueError,), {})
broker.validate_endpoint = lambda value: value
sys.modules[broker.__name__] = broker
spec = importlib.util.spec_from_file_location(PACKAGE + '.node', PACKAGE_ROOT / 'node.py')
node = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = node
spec.loader.exec_module(node)


class Signer:
    def __init__(self):
        self.calls = []

    def _signed_envelope(self, ctx, operation, payload):
        envelope = {'kind': operation, 'payload': dict(payload),
                    'nonce': str(len(self.calls) + 1)}
        self.calls.append((threading.get_ident(), envelope))
        return envelope


class HeartbeatFlow(unittest.TestCase):
    def setUp(self):
        self.state = Path('/profile')
        self.req = {'request_id': 'request-1'}
        self.main_thread = threading.get_ident()
        self.signer = Signer()
        self.release = threading.Event()
        self.dispatches = []
        self.posts = []
        self.interval = patch.object(node, '_HEARTBEAT_INTERVAL_SECONDS', 0.003)
        self.interval.start()
        self.addCleanup(self.interval.stop)
        self.addCleanup(self.release.set)

    def dispatch(self, request, **kwargs):
        self.dispatches.append((threading.get_ident(), request))
        if not self.release.wait(1):
            raise AssertionError('test failed to release owned worker')
        return {'ok': True, 'performed': True, 'receipt': 'actual-operation-result'}

    def call(self, post, dispatch=None, contact=None):
        with patch.object(node, 'dispatch_request', dispatch or self.dispatch), \
                patch.object(broker, 'post_json', post, create=True):
            return node._dispatch_with_heartbeat(self.req, url='https://broker',
                ctx=object(), state=self.state, outbound=self.signer, contact=contact)

    def valid_post(self, url, envelope, **kwargs):
        self.posts.append((threading.get_ident(), url, envelope, kwargs))
        if len(self.posts) == 3:
            self.release.set()
        return {'ok': True, 'busy': True, 'request_id': 'request-1'}

    def test_long_dispatch_is_once_and_only_main_thread_signs_without_poll(self):
        contact = types.SimpleNamespace(accepted=lambda *a, **kw: None)
        value = self.call(self.valid_post, contact=contact)
        self.assertTrue(value['performed'])
        self.assertEqual(len(self.dispatches), 1)
        self.assertNotEqual(self.dispatches[0][0], self.main_thread)
        self.assertEqual(len(self.posts), 3)
        self.assertTrue(all(row[0] == self.main_thread for row in self.posts))
        self.assertTrue(all(row[1].endswith('/node/heartbeat') for row in self.posts))
        self.assertTrue(all(row[2]['payload'] == {'request_id': 'request-1'} for row in self.posts))
        self.assertTrue(all(row[3]['timeout'] == 2 for row in self.posts))
        self.assertTrue(all(tid == self.main_thread for tid, _ in self.signer.calls))
        self.assertEqual(len({e['nonce'] for _, e in self.signer.calls}), 3)

    def test_transient_failure_recovers_with_new_nonce_without_replaying_input(self):
        def post(url, envelope, **kwargs):
            self.posts.append(envelope)
            if len(self.posts) == 1:
                raise broker.BrokerError('BROKER_HTTP_REJECTED_503')
            if len(self.posts) == 3:
                self.release.set()
            return {'ok': True, 'busy': True, 'request_id': 'request-1'}
        value = self.call(post)
        self.assertTrue(value['performed'])
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual([v['nonce'] for v in self.posts], ['1', '2', '3'])

    def test_invalid_heartbeat_reply_stops_heartbeat_but_keeps_actual_result(self):
        def post(url, envelope, **kwargs):
            self.posts.append(envelope)
            threading.Timer(0.02, self.release.set).start()
            return {'ok': True, 'busy': True, 'request_id': 'another-request'}
        value = self.call(post)
        self.assertEqual(value['receipt'], 'actual-operation-result')
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.dispatches), 1)

    def test_fatal_transport_error_does_not_abort_or_replay_execution(self):
        def post(*args, **kwargs):
            self.posts.append(True)
            threading.Timer(0.02, self.release.set).start()
            raise broker.BrokerError('BROKER_HTTP_REJECTED_403')
        self.assertTrue(self.call(post)['performed'])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.dispatches), 1)

    def test_lease_expiry_after_accepted_heartbeat_keeps_eventual_result_once(self):
        def post(*args, **kwargs):
            self.posts.append(True)
            if len(self.posts)==1:
                return {'ok': True, 'busy': True, 'request_id': 'request-1'}
            threading.Timer(0.02, self.release.set).start()
            raise broker.BrokerError('BROKER_HTTP_REJECTED_403')
        value=self.call(post)
        self.assertEqual(value['receipt'], 'actual-operation-result')
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(len(self.dispatches), 1)

    def test_signing_error_is_redacted_and_does_not_discard_action_result(self):
        def sign(*args, **kwargs):
            threading.Timer(0.02, self.release.set).start()
            raise ValueError('MUST-NOT-APPEAR-IN-LOG')
        self.signer._signed_envelope = sign
        with self.assertLogs(node._LOG, logging.WARNING) as logs:
            self.assertTrue(self.call(self.valid_post)['performed'])
        self.assertNotIn('MUST-NOT-APPEAR', '\n'.join(logs.output))
        self.assertEqual(len(self.dispatches), 1)

    def test_worker_exception_is_propagated_including_cancellation(self):
        def stop(*args, **kwargs):
            raise SystemExit(7)
        with self.assertRaises(SystemExit) as raised:
            self.call(self.valid_post, dispatch=stop)
        self.assertEqual(raised.exception.code, 7)
        self.assertEqual(self.signer.calls, [])

    def test_fast_job_does_not_add_one_second_delay_or_extra_heartbeat(self):
        self.release.set()
        value = self.call(self.valid_post)
        self.assertTrue(value['performed'])
        self.assertEqual(self.posts, [])

    def test_feature_is_explicit_and_frozen_reply_has_no_heartbeat(self):
        for value in [None, True, 'node-heartbeat-v1', ('node-heartbeat-v1',), {}]:
            self.assertFalse(node._heartbeat_supported({'protocol_features': value}))
        self.assertTrue(node._heartbeat_supported({'protocol_features': ['node-heartbeat-v1']}))


class RunProtocol(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.ctx = types.SimpleNamespace(state_dir=self.state / 'node')
        self.identity = {'node_id': 'node-1'}
        self.signer = Signer()
        pairing = {'state': 'paired', 'node_id': 'node-1', 'broker_base_url': 'https://broker'}
        pairing_module = types.SimpleNamespace(PairingStore=lambda *a: types.SimpleNamespace(load=lambda: pairing))
        self.original = lambda name: {'pairing': pairing_module, 'outbound_agent': self.signer}[name]
        for obj, name, value in [(node, '_context', lambda *a: (self.state, {'broker_url':'https://broker'}, self.ctx, self.identity)),
                                 (node, '_original', self.original), (node, '_HEARTBEAT_INTERVAL_SECONDS', 0.003)]:
            p = patch.object(obj, name, value);p.start();self.addCleanup(p.stop)

    def test_frozen_server_dispatch_is_same_main_thread_and_result_posted_once(self):
        calls, dispatched = [], []
        def post(url, envelope, **kwargs):
            calls.append(envelope['kind'])
            return ({'ok': True, 'request': {'request_id': 'r'}} if envelope['kind']=='node.poll' else {'ok': True})
        def dispatch(request, **kwargs):
            dispatched.append(threading.get_ident());return {'ok': True, 'performed': True}
        with patch.object(broker, 'post_json', post, create=True), patch.object(node, 'dispatch_request', dispatch):
            result = node.run(once=True)
        self.assertEqual(dispatched, [threading.get_ident()])
        self.assertEqual(calls, ['node.poll', 'node.result'])
        self.assertFalse(result['idle'])
        row = json.loads((self.state/'node-runtime-status.json').read_text())
        self.assertEqual(row['worker_pid'], os.getpid())
        self.assertFalse(row['heartbeat_v1'])

    def test_rejected_poll_never_dispatches_or_writes_accepted_contact(self):
        with patch.object(broker, 'post_json', lambda *a, **kw: {'ok':False}, create=True), \
                patch.object(node, 'dispatch_request') as dispatch:
            with self.assertRaisesRegex(node.NodeError, 'NODE_POLL_REJECTED'):
                node.run(once=True)
        dispatch.assert_not_called()
        self.assertFalse((self.state/'node-runtime-status.json').exists())

    def test_negotiated_run_keeps_heartbeats_but_never_polls_during_dispatch(self):
        released = threading.Event()
        self.addCleanup(released.set)
        operations, dispatches, heartbeats = [], [], []
        def post(url, envelope, **kwargs):
            operations.append(envelope['kind'])
            if envelope['kind']=='node.poll':
                return {'ok': True, 'protocol_features': ['node-heartbeat-v1'],
                        'request': {'request_id': 'r'}}
            if envelope['kind']=='node.heartbeat':
                heartbeats.append(envelope['nonce'])
                if len(heartbeats)==2: released.set()
                return {'ok': True, 'busy': True, 'request_id': 'r'}
            self.assertTrue(released.is_set())
            return {'ok': True}
        def dispatch(*a, **kw):
            dispatches.append(threading.get_ident())
            self.assertTrue(released.wait(1))
            return {'ok': True, 'performed': True}
        with patch.object(broker, 'post_json', post, create=True), \
                patch.object(node, 'dispatch_request', dispatch):
            result = node.run(once=True)
        self.assertFalse(result['idle'])
        self.assertEqual(len(dispatches), 1)
        self.assertNotEqual(dispatches[0], threading.get_ident())
        self.assertEqual(operations, ['node.poll','node.heartbeat','node.heartbeat','node.result'])
        self.assertTrue(all(tid == threading.get_ident() for tid,_ in self.signer.calls))

    def test_status_write_failure_does_not_discard_result_or_reexecute(self):
        def post(url, envelope, **kwargs):
            return {'ok': True, 'request': {'request_id':'r'}} if envelope['kind']=='node.poll' else {'ok':True}
        with patch.object(broker, 'post_json', post, create=True), \
                patch.object(node, 'dispatch_request', return_value={'ok':True,'performed':True}) as dispatch, \
                patch.object(node, '_write_json', side_effect=OSError('test full disk')):
            result = node.run(once=True)
        self.assertFalse(result['idle'])
        self.assertEqual(dispatch.call_count, 1)

    def test_result_delivery_retry_never_returns_to_dispatch(self):
        calls, result_count, dispatched = [], 0, []
        def post(url, envelope, **kwargs):
            nonlocal result_count
            calls.append(envelope)
            if envelope['kind']=='node.poll':
                if len(calls)>1: raise StopIteration('end fixture after acknowledged result')
                return {'ok':True, 'request': {'request_id':'r'}}
            result_count += 1
            if result_count == 1: raise broker.BrokerError('BROKER_HTTP_REJECTED_503')
            return {'ok':True}
        with patch.object(broker, 'post_json', post, create=True), \
                patch.object(node, 'dispatch_request', side_effect=lambda *a, **kw: dispatched.append(True) or {'ok':True}), \
                patch.object(node.time, 'sleep', lambda *a: None):
            with self.assertRaises(StopIteration): node.run()
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(result_count, 2)
        self.assertEqual(len({v['nonce'] for v in calls}), len(calls))

    def test_contact_file_is_throttled_and_contains_no_envelope_or_credentials(self):
        writer = node._BrokerContact(self.state, 'node-1')
        with patch.object(node.time, 'monotonic', side_effect=[100,101,105]), \
                patch.object(node, '_write_json') as write:
            writer.accepted('node.poll', heartbeat_v1=True)
            writer.accepted('node.heartbeat', heartbeat_v1=True)
            writer.accepted('node.heartbeat', heartbeat_v1=True)
        self.assertEqual(write.call_count, 2)
        row=write.call_args.args[1]
        self.assertEqual(set(row), {'schema','node_id','worker_pid','worker_birth',
            'last_broker_acceptance_unix','last_accepted_operation','heartbeat_v1'})

    def test_existing_dispatch_authority_and_ack_recovery_code_are_unchanged(self):
        import hashlib
        guards = json.loads((FIXTURES / 'STRUCTURAL-GUARDS.json').read_text())['node']
        tree = ast.parse((PACKAGE_ROOT / 'node.py').read_text())
        for name, expected in guards.items():
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            actual = semantic_ast_digest(function)
            self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
