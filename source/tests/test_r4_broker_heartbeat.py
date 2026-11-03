"""Protocol tests with the real unchanged Store verifier and disposable SSHSIG keys."""
import ast
import collections
import copy
import hashlib
import importlib.util
import ipaddress
import json
import secrets
import socket
import sys
import tempfile
import threading
import types
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = TEST_ROOT.parent
PACKAGE_ROOT = SOURCE_ROOT / 'expanded/framebridge_expanded'
FIXTURES = TEST_ROOT / 'fixtures/r4-035'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


crypto = load('test_real_node_identity', SOURCE_ROOT / 'sources/original-geranas/FrameBridge-Broker-RC1/src/framebridge_linux_chatgpt/node_identity.py')
broker = load('test_broker_candidate', PACKAGE_ROOT / 'broker.py')


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')


clock = types.SimpleNamespace(now=2000000000.0)
fake_time = types.SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now, sleep=lambda n: None)
tree = ast.parse((SOURCE_ROOT / 'sources/original-geranas/FrameBridge-Broker-RC1/dev/fbpair_dev_broker.py').read_text())
store_ast = next(v for v in tree.body if isinstance(v, ast.ClassDef) and v.name == 'Store')
namespace = {'Path': Path, 'threading': threading, 'collections': collections,
             'json': json, 'time': fake_time, 'os': __import__('os'),
             'canonical': canonical, 'verify_pairing_payload': crypto.verify_pairing_payload,
             'ipaddress': ipaddress, 'BaseHTTPRequestHandler': BaseHTTPRequestHandler,
             'DEFAULT_CONTROL_PEERS': frozenset({'127.0.0.1'})}
exec(compile(ast.Module(body=[store_ast], type_ignores=[]), 'original-Store', 'exec'), namespace)
handler_parts = [v for v in tree.body if isinstance(v, ast.FunctionDef) and
                 v.name in {'_control_peer_literal', 'parse_control_peers', 'control_peer_allowed', 'make_handler'}]
exec(compile(ast.Module(body=handler_parts, type_ignores=[]), 'original-Handler', 'exec'), namespace)


class Store(broker._NodeHeartbeatStore, namespace['Store']):
    pass


class HeartbeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix='fb-test-only-signers-')
        cls.addClassCleanup(cls.keys.cleanup)
        cls.key = crypto.ensure_node_keypair(Path(cls.keys.name) / 'node-a', comment='disposable-test-a')
        cls.other = crypto.ensure_node_keypair(Path(cls.keys.name) / 'node-b', comment='disposable-test-b')

    def setUp(self):
        clock.now = 2000000000.0
        self.patch_time = patch.object(broker, 'time', fake_time)
        self.patch_time.start(); self.addCleanup(self.patch_time.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='fb-test-store-')
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'paired.json'
        path.write_text(json.dumps({'schema': 1, 'nodes': [
            {'source': 'fbpair', 'node_id': 'node-a', 'node_host_id': 'host-a', 'state': 'paired',
             'public_key_ssh': self.key['public_key_ssh']},
            {'source': 'fbpair', 'node_id': 'node-b', 'node_host_id': 'host-b', 'state': 'paired',
             'public_key_ssh': self.other['public_key_ssh']}]}))
        self.store = Store(path)
        self.enqueue('work-a', 'node-a')
        self.assertEqual(self.store.next_read('node-a')['request_id'], 'work-a')

    def enqueue(self, rid, node='node-a', expiry=None):
        self.store.control_requests[rid] = {'request_id': rid, 'node_id': node,
            'expires_at': clock.now + 60 if expiry is None else expiry}
        self.store.control_results[rid] = {'state': 'pending', 'node_id': node}
        self.store.control_queue[node].append(rid)

    def message(self, kind='node.heartbeat', rid='work-a', node='node-a', **changes):
        payload = {} if kind == 'node.poll' else {'request_id': rid}
        if kind == 'node.result': payload['result'] = {'ok': True, 'public': 'done'}
        value = {'schema': 1, 'kind': kind, 'node_id': node,
                 'node_host_id': 'host-a' if node == 'node-a' else 'host-b',
                 'timestamp': clock.now, 'nonce': secrets.token_hex(16), 'payload': payload}
        value.update(changes)
        key = self.key if node == 'node-a' else self.other
        signature = crypto.sign_pairing_payload(Path(key['private_key_path']), canonical(value))
        return dict(value, node_proof={'format': 'sshsig', 'namespace': 'framebridge-pairing',
                                       'signature_b64': signature})

    def call(self, kind='heartbeat', message=None):
        return broker._node_message_response(self.store, '/fbpair/v1/node/' + kind,
                                              self.message() if message is None else message)

    def test_real_signature_refreshes_liveness_without_dequeue_or_extending_lease(self):
        self.enqueue('queued-a')
        expiry = self.store.control_requests['work-a']['expires_at']
        code, response = self.call()
        self.assertEqual((code, response), (200, {'ok': True, 'request_id': 'work-a', 'busy': True}))
        self.assertEqual(list(self.store.control_queue['node-a']), ['queued-a'])
        self.assertEqual(self.store.control_requests['work-a']['expires_at'], expiry)
        status = self.store.node_runtime_status('node-a')
        self.assertTrue(status['ready'] and status['heartbeat_fresh'] and status['busy'])
        self.assertEqual(status['activity_state'], 'busy')

    def test_signature_tamper_cannot_refresh_liveness(self):
        value = self.message(); value['node_proof']['signature_b64'] = 'AAAA'
        self.assertEqual(self.call(message=value)[0], 403)
        self.assertNotIn('node-a', self.store.node_last_seen)

    def test_host_binding_is_unchanged(self):
        self.assertEqual(self.call(message=self.message(node_host_id='other-host'))[1]['error'], 'host_id_mismatch')
        self.assertNotIn('node-a', self.store.node_last_seen)

    def test_timestamp_window_is_unchanged(self):
        self.assertEqual(self.call(message=self.message(timestamp=clock.now - 61))[1]['error'], 'timestamp_window')
        self.assertNotIn('node-a', self.store.node_last_seen)

    def test_future_timestamp_window_is_unchanged(self):
        self.assertEqual(self.call(message=self.message(timestamp=clock.now + 61))[0], 403)

    def test_nonce_replay_is_rejected(self):
        value = self.message(); self.assertEqual(self.call(message=value)[0], 200)
        self.assertEqual(self.call(message=value)[1]['error'], 'replay')

    def test_job_from_another_node_is_rejected_before_verification(self):
        self.assertEqual(self.call(message=self.message(node='node-b'))[1]['error'], 'heartbeat_request_not_active')
        self.assertNotIn('node-b', self.store.node_last_seen)

    def test_pending_job_does_not_invent_busy_or_heartbeat(self):
        self.enqueue('not-dispatched')
        self.assertEqual(self.call(message=self.message(rid='not-dispatched'))[0], 403)
        self.assertNotIn('node-a', self.store.node_last_seen)

    def test_completed_job_releases_busy_and_cannot_heartbeat(self):
        self.call(); self.assertTrue(self.store.put_result('node-a', 'work-a', {'ok': True}))
        self.assertFalse(self.store.node_runtime_status('node-a')['busy'])
        self.assertEqual(self.call()[0], 403)

    def test_expired_lease_cannot_refresh_or_extend_liveness(self):
        expiry = self.store.control_requests['work-a']['expires_at']; clock.now = expiry + 1
        self.assertEqual(self.call()[0], 403)
        self.assertFalse(self.store.node_runtime_status('node-a')['busy'])
        self.assertEqual(self.store.control_requests['work-a']['expires_at'], expiry)

    def test_extra_payload_fields_are_rejected(self):
        self.assertEqual(self.call(message=self.message(payload={'request_id': 'work-a', 'script': 'ignored'}))[0], 403)
        self.assertNotIn('node-a', self.store.node_last_seen)

    def test_wrong_heartbeat_kind_is_rejected(self):
        self.assertEqual(self.call(message=self.message(kind='node.poll'))[0], 403)

    def test_nonfinite_timestamp_is_rejected(self):
        self.assertEqual(self.call(message=self.message(timestamp=float('nan')))[0], 403)

    def test_poll_advertises_capability_and_preserves_legacy_dispatch(self):
        self.enqueue('queued-a')
        code, response = self.call('poll', self.message(kind='node.poll'))
        self.assertEqual(code, 200)
        self.assertEqual(response['request']['request_id'], 'queued-a')
        self.assertEqual(response['protocol_features'], ['node-heartbeat-v1'])

    def test_legacy_result_acknowledges_once_without_executing(self):
        code, response = self.call('result', self.message(kind='node.result'))
        self.assertEqual((code, response), (200, {'ok': True}))
        self.assertEqual(self.store.get_result('work-a')['state'], 'complete')
        self.assertFalse(self.store.node_runtime_status('node-a')['busy'])

    def test_heartbeat_cannot_dequeue_through_poll_endpoint(self):
        self.enqueue('queued-a')
        self.assertEqual(self.call('poll')[0], 403)
        self.assertEqual(list(self.store.control_queue['node-a']), ['queued-a'])
        self.assertNotIn('node-a', self.store.node_last_seen)

    def test_heartbeat_cannot_ack_through_result_endpoint(self):
        self.assertEqual(self.call('result')[0], 403)
        self.assertEqual(self.store.get_result('work-a')['state'], 'dispatched')

    def test_stale_active_lease_is_distinguished_from_offline(self):
        self.call(); clock.now += 6
        value = self.store.node_runtime_status('node-a')
        self.assertFalse(value['ready']); self.assertTrue(value['busy'])
        self.assertEqual(value['activity_state'], 'busy-heartbeat-stale')

    def test_stale_idle_node_is_offline(self):
        self.call(); self.store.put_result('node-a', 'work-a', {'ok': True}); clock.now += 6
        value = self.store.node_runtime_status('node-a')
        self.assertFalse(value['ready'] or value['busy'])
        self.assertEqual(value['activity_state'], 'offline')

    def test_unpaired_status_remains_absent(self):
        self.assertIsNone(self.store.node_runtime_status('unknown-node'))

    def test_status_does_not_scan_historical_result_dictionary(self):
        class NoScan(dict):
            def items(self): raise AssertionError('historical scan')
            def __iter__(self): raise AssertionError('historical scan')
        self.store.control_results = NoScan(self.store.control_results)
        self.assertEqual(self.store.node_runtime_status('node-a')['active_request_count'], 1)

    def test_changed_lease_is_not_silently_extended(self):
        self.store.control_requests['work-a']['expires_at'] += 600
        self.assertEqual(self.call()[0], 403)

    def test_request_reports_stale_busy_without_claiming_offline(self):
        with patch.object(broker, 'list_confirmed_nodes', return_value=[{'node_id': 'node-a', 'ready': False, 'busy': True}]):
            with self.assertRaisesRegex(broker.BrokerError, 'NODE_BUSY_HEARTBEAT_STALE'):
                broker.request('node-a', 'admin.exec', {}, timeout_s=10)

    def test_fresh_busy_preserves_existing_bounded_queue_policy(self):
        def control(route, value):
            if route == 'admin-exec': return {'ok': True, 'request_id': 'new-rid'}
            return {'node_id': 'node-a', 'state': 'complete', 'result': {'ok': True}}
        with patch.object(broker, 'list_confirmed_nodes', return_value=[{'node_id': 'node-a', 'ready': True, 'busy': True}]), patch.object(broker, '_control', side_effect=control):
            self.assertEqual(broker.request('node-a', 'admin.exec', {}, timeout_s=10)['request_id'], 'new-rid')

    def test_expiry_crossed_during_real_signature_verification_cannot_refresh(self):
        previous = clock.now - 4; self.store.node_last_seen['node-a'] = previous
        real = crypto.verify_pairing_payload
        def crossing(*args):
            result = real(*args); clock.now += 61; return result
        with patch.dict(namespace, {'verify_pairing_payload': crossing}):
            self.assertEqual(self.call()[0], 403)
        self.assertEqual(self.store.node_last_seen['node-a'], previous)

    def test_completion_during_real_signature_verification_cannot_refresh(self):
        previous = clock.now - 4; self.store.node_last_seen['node-a'] = previous
        real = crypto.verify_pairing_payload
        def completing(*args):
            result = real(*args); self.store.put_result('node-a', 'work-a', {'ok': True}); return result
        with patch.dict(namespace, {'verify_pairing_payload': completing}):
            self.assertEqual(self.call()[0], 403)
        self.assertEqual(self.store.node_last_seen['node-a'], previous)
        self.assertEqual(self.store.get_result('work-a')['state'], 'complete')

    def test_concurrent_identical_nonce_can_only_be_verified_once(self):
        first_crypto = threading.Event(); release = threading.Event(); second_attempted = threading.Event()
        real = crypto.verify_pairing_payload; signatures = []
        class ObservedLock:
            def __init__(inner): inner.lock = threading.Lock()
            def __enter__(inner):
                if threading.current_thread().name == 'second-verifier': second_attempted.set()
                inner.lock.acquire()
            def __exit__(inner, *unused): inner.lock.release()
        self.store._node_verifier_locks = {'node-a': ObservedLock()}
        def blocking(*args):
            signatures.append(1); first_crypto.set(); release.wait(2); return real(*args)
        envelope = self.message(); results = []
        def call(): results.append(self.call(message=envelope))
        one = threading.Thread(target=call, name='first-verifier', daemon=True)
        two = threading.Thread(target=call, name='second-verifier', daemon=True)
        with patch.dict(namespace, {'verify_pairing_payload': blocking}):
            try:
                one.start(); self.assertTrue(first_crypto.wait(1))
                two.start(); self.assertTrue(second_attempted.wait(1))
            finally: release.set(); one.join(2); two.join(2)
        self.assertFalse(one.is_alive() or two.is_alive())
        self.assertEqual(sorted(code for code, _ in results), [200, 403])
        self.assertEqual([body['error'] for code, body in results if code == 403], ['replay'])
        self.assertEqual(len(signatures), 1)

    def test_different_nodes_can_verify_without_global_crypto_lock(self):
        self.enqueue('work-b', node='node-b'); self.store.next_read('node-b')
        entered = threading.Event(); release = threading.Event(); finished_b = threading.Event()
        real = crypto.verify_pairing_payload; results = []
        a, b = self.message(), self.message(node='node-b', rid='work-b')
        def blocking(public, payload, signature):
            if json.loads(payload)['node_id'] == 'node-a': entered.set(); release.wait(2)
            return real(public, payload, signature)
        def run_a(): results.append(self.call(message=a))
        def run_b(): results.append(self.call(message=b)); finished_b.set()
        one = threading.Thread(target=run_a, daemon=True); two = threading.Thread(target=run_b, daemon=True)
        with patch.dict(namespace, {'verify_pairing_payload': blocking}):
            try:
                one.start(); self.assertTrue(entered.wait(1)); two.start()
                completed_before_release = finished_b.wait(1)
            finally: release.set(); one.join(2); two.join(2)
        self.assertTrue(completed_before_release)
        self.assertEqual([code for code, _ in results], [200, 200])

    def test_unknown_public_nodes_do_not_grow_verification_lock_cache(self):
        for index in range(50):
            body = {'node_id': 'unknown-%d' % index}
            self.assertFalse(self.store.verify_node_envelope(body)[0])
        self.assertFalse(getattr(self.store, '_node_verifier_locks', {}))

    def test_missing_heartbeat_verification_guard_never_falls_through_to_success(self):
        with patch.object(self.store, '_node_verification_lock', return_value=None), patch.object(self.store, 'verify_node_envelope', return_value=(True, {'node_id': 'node-a'})) as ordinary:
            self.assertEqual(self.call()[0], 403)
            ordinary.assert_not_called()
        self.assertNotIn('node-a', self.store.node_last_seen)


class HttpContractTests(unittest.TestCase):
    """Exercise the real candidate HTTP router on a disposable loopback port."""

    setUpClass = classmethod(HeartbeatTests.setUpClass.__func__)
    enqueue = HeartbeatTests.enqueue
    message = HeartbeatTests.message

    def setUp(self):
        HeartbeatTests.setUp(self)
        original = types.SimpleNamespace(Store=namespace['Store'], make_handler=namespace['make_handler'])
        with socket.socket() as selected:
            selected.bind(('127.0.0.1', 0)); port = selected.getsockname()[1]
        state = Path(self.temp.name)
        paired = state / 'paired.json'
        records = json.loads(paired.read_text())
        (state / 'broker').mkdir(mode=0o700)
        (state / 'broker/paired-nodes.json').write_text(json.dumps(records))
        with patch.object(broker, 'state_path', side_effect=lambda p=None: Path(p)), patch.object(broker, '_config', return_value={}), patch.object(broker, 'original_modules', return_value=original):
            self.server = broker.make_server('127.0.0.1', port, state_dir=state)
        # Replace the pure Store fixture with the actual HTTP server's Store.
        self.store = self.server.expanded_store
        self.enqueue('work-a'); self.store.next_read('node-a')
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:%d' % port
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def http(self, path, message):
        request = urllib.request.Request(self.base_url + path, data=json.dumps(message).encode(),
                                         headers={'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as response:
            return response.code, json.loads(response.read())

    def test_http_public_unsigned_heartbeat_is_not_authorized(self):
        self.assertEqual(self.http('/fbpair/v1/node/heartbeat', {'request_id': 'work-a'})[0], 403)
        self.assertNotIn('node-a', self.store.node_last_seen)

    def test_http_signed_heartbeat_reaches_new_route_without_dequeue(self):
        self.enqueue('queued-a')
        code, value = self.http('/fbpair/v1/node/heartbeat', self.message())
        self.assertEqual((code, value.get('busy')), (200, True))
        self.assertEqual(list(self.store.control_queue['node-a']), ['queued-a'])

    def test_http_frozen_poll_and_result_contract_remains_usable(self):
        code, value = self.http('/fbpair/v1/node/poll', self.message(kind='node.poll'))
        self.assertEqual((code, value.get('ok')), (200, True))
        self.assertIn('node-heartbeat-v1', value['protocol_features'])
        code, value = self.http('/fbpair/v1/node/result', self.message(kind='node.result'))
        self.assertEqual((code, value), (200, {'ok': True}))

    def test_http_operator_control_still_requires_bearer(self):
        code, value = self.http('/fbpair/v1/control/node-status', {'node_id': 'node-a'})
        self.assertEqual((code, value.get('error')), (403, 'CONTROL_AUTH_REQUIRED'))

    def test_http_heartbeat_cannot_be_used_to_ack_command(self):
        self.assertEqual(self.http('/fbpair/v1/node/result', self.message())[0], 403)
        self.assertEqual(self.store.get_result('work-a')['state'], 'dispatched')

    def test_full_node_run_real_signed_http_stays_online_past_five_seconds(self):
        package_name = 'r4_isolated_protocol_test'
        package = types.ModuleType(package_name); package.__path__ = []
        package.broker = broker
        sys.modules[package_name] = package
        self.addCleanup(sys.modules.pop, package_name, None)
        spec = importlib.util.spec_from_file_location(package_name + '.node', PACKAGE_ROOT / 'node.py')
        node = importlib.util.module_from_spec(spec); spec.loader.exec_module(node)
        self.store.control_queue.clear(); self.store.control_requests.clear(); self.store.control_results.clear()
        self.store._heartbeat_dispatched.clear(); self.enqueue('work-main')
        state = Path(self.temp.name); ctx = types.SimpleNamespace(state_dir=state / 'node')
        identity = {'node_id': 'node-a'}
        pair = {'state': 'paired', 'node_id': 'node-a', 'broker_base_url': self.base_url}
        signing_threads, kinds, dispatches, observed = [], [], [], []
        started = threading.Event()
        finished = threading.Event()
        outcome = []

        def signed(ctx, kind, payload):
            signing_threads.append(threading.get_ident()); kinds.append(kind)
            return self.message(kind=kind, payload=payload)

        def original(name):
            if name == 'outbound_agent': return types.SimpleNamespace(_signed_envelope=signed)
            if name == 'pairing': return types.SimpleNamespace(PairingStore=lambda _: types.SimpleNamespace(load=lambda: pair))
            raise AssertionError('unexpected original import')

        def dispatch(request, state_dir=None):
            dispatches.append(request['request_id']); started.set()
            # Exceed the unchanged five-second liveness threshold.
            finished.wait(6.2)
            return {'ok': True, 'public': 'one execution only'}

        def run():
            try: outcome.append(node.run(state_dir=state, once=True))
            except BaseException as error: outcome.append(error)

        with patch.object(node, '_context', return_value=(state, {'broker_url': self.base_url}, ctx, identity)), patch.object(node, '_original', side_effect=original), patch.object(node, 'dispatch_request', side_effect=dispatch), patch.object(broker, '_config', return_value={}):
            main = threading.Thread(target=run, name='test-node-main', daemon=True)
            beginning = time.monotonic(); main.start()
            self.assertTrue(started.wait(2))
            while main.is_alive() and time.monotonic() - beginning < 9:
                clock.now = 2000000000.0 + time.monotonic() - beginning
                status = self.store.node_runtime_status('node-a')
                if status['busy']: observed.append((clock.now, status['ready']))
                main.join(0.1)
            finished.set(); main.join(2)
        self.assertFalse(main.is_alive())
        self.assertTrue(outcome and isinstance(outcome[0], dict), type(outcome[0]).__name__ if outcome else 'no outcome')
        self.assertEqual(outcome[0]['request_id'], 'work-main')
        self.assertEqual(dispatches, ['work-main'])
        self.assertGreaterEqual(kinds.count('node.heartbeat'), 4)
        self.assertEqual(kinds.count('node.poll'), 1)
        self.assertEqual(kinds.count('node.result'), 1)
        self.assertEqual(len(set(signing_threads)), 1)
        self.assertTrue(any(moment > 2000000005 for moment, _ in observed))
        self.assertTrue(all(ready for _, ready in observed))
        self.assertEqual(self.store.get_result('work-main')['state'], 'complete')
        self.assertFalse(self.store.node_runtime_status('node-a')['busy'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
