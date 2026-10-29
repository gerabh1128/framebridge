"""Deterministic protocol fixtures only: never observe or control a desktop."""
import base64
import concurrent.futures
import contextlib
import hashlib
import gzip
import io
import json
import math
import os
from pathlib import Path
import shlex
import sys
import types
import tempfile
import threading
import unittest
from unittest.mock import patch

from framebridge_expanded.real_screen import screen_rpc as rpc
from framebridge_expanded.real_screen import screen_transport as transport
from framebridge_expanded.real_screen.screen_frontdoor import (
    CHUNK_BYTES, EXPANDED_CHUNK_BYTES, EXPANDED_STDOUT_BYTES,
    MAX_STDOUT_BYTES, RPC_COMMAND, ScreenClient, ScreenError,
)
from test_screen_frontdoor import CAPTURE_ID, NODE_ID, Image, png, Executor


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class ProtocolFixture:
    """Saved generated PNG fixture through actual producer, RPC and typed transport.

    The private records model a previously captured artifact for protocol tests;
    they are generated test pixels and do not certify any real screen observation.
    """
    def __init__(self, directory, width=384, height=256):
        self.directory = directory
        self.data = png(width, height)
        self.manifest = {
            'capture_id': CAPTURE_ID, 'node': 'test-fixture-host', 'uid': os.getuid(),
            'source': 'linux-framebuffer', 'screen_type': 'tty', 'active_vt': 'tty1',
            'bytes': len(self.data), 'sha256': hashlib.sha256(self.data).hexdigest(),
            'width': width, 'height': height, 'synthetic': False,
            'pixel_source_verified': True, 'full_screen': True, 'authentic_pixels': True,
            'session_id': 'screen_fixture', 'requested_session_visible': False,
            'control_available': True, '_action_consumed': False,
            'binding': {'window_id': 'fixture-window', 'geometry': [0, 0, width, height]},
        }
        path = directory / (CAPTURE_ID + '.png')
        path.write_bytes(self.data)
        path.chmod(0o600)
        for suffix in ('.json', '.bound.json'):
            rpc.write_record(directory / (CAPTURE_ID + suffix), self.manifest)
        self.calls, self.reply_sizes = [], []
        self.rpc, self.transport = rpc, transport
        self.variant = None
        self.clock, self.latency = None, 0

    def __call__(self, **kwargs):
        request = json.loads(base64.b64decode(shlex.split(kwargs['script'])[-1]))
        self.calls.append((request, kwargs['timeout_s']))
        if self.clock is not None:
            self.clock.now += self.latency
        payload = {key: kwargs[key] for key in (
            'script', 'authorized', 'timeout_s', 'job_id', 'sha256', 'include_stdout')}
        payload.update(version=1, mode='EXEC', request_type='screen_rpc')

        def runner(argument, timeout_s):
            # No real observe/input functions are invoked by this fixture executor.
            if request['op'] in ('key', 'text'):
                manifest = self.rpc.public_manifest(self.manifest)
                manifest['action'] = {'kind': request['op'], 'previous_capture_id': request['capture_id'],
                                      'result': {'performed': True}}
                envelope = {'ok': True, 'result': manifest}
            else:
                envelope = self.rpc.dispatch(request, self.directory)
            if request['op'] == 'read_chunk' and self.variant == 'corrupt':
                encoded = envelope['result']['data']
                data = base64.b64decode(encoded)
                envelope['result']['data'] = base64.b64encode(bytes([data[0] ^ 1]) + data[1:]).decode()
            return 0, self.rpc.encode(envelope) + b'\n', b''

        outer = self.transport.handle_request(payload, runner=runner)
        outer.update(target=kwargs['target'], node_id=NODE_ID,
                     mcp_job_id=kwargs['job_id'], mcp_script_sha256=kwargs['sha256'])
        self.reply_sizes.append(len(outer['stdout'].encode()))
        if self.variant == 'truncated' and request['op'] == 'read_chunk':
            outer['stdout_truncated'] = True
        return outer


@contextlib.contextmanager
def frozen_receiver(testcase):
    """Load the hashed base receiver fixture, including in exported source archives.

    The capsule contains only the five original real-screen modules from the
    stated immutable revision. Its source hashes and outer digest are verified;
    no Git checkout, installed receiver, or live desktop is used by this test.
    """
    revision = 'f6b14c52109e0d55b919f179a38c8a4c8f8868f9'
    path = Path(__file__).resolve().parent / 'fixtures/frozen_screen_receiver_f6b14c52.json.gz'
    raw = path.read_bytes()
    testcase.assertEqual(hashlib.sha256(raw).hexdigest(), '47670e1325c0f77fabeea9c69c09923c3fd0ed2463a8300a4940bb5e4bafffc5')
    capsule = json.loads(gzip.decompress(raw))
    testcase.assertEqual(capsule['revision'], revision)
    names = ('screen_frontdoor', 'screen_capture', 'screen_control', 'screen_rpc', 'screen_transport')
    testcase.assertEqual(set(capsule['files']), set(names))
    package_name = '_framebridge_frozen_screen_fixture'
    package = types.ModuleType(package_name)
    package.__path__ = []
    with patch.dict(sys.modules, {package_name: package}):
        modules = {}
        for name in names:
            entry = capsule['files'][name]
            source = entry['source']
            testcase.assertEqual(hashlib.sha256(source.encode()).hexdigest(), entry['sha256'])
            full_name = package_name + '.' + name
            module = types.ModuleType(full_name)
            module.__package__ = package_name
            sys.modules[full_name] = module
            exec(compile(source, revision + ':' + name + '.py', 'exec'), module.__dict__)
            modules[name] = module
        yield modules['screen_rpc'], modules['screen_transport']


class ScreenTransferBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fixture = ProtocolFixture(self.directory)

    def client(self, **kwargs):
        return ScreenClient(self.fixture, Image, **kwargs)

    def read(self, client):
        return client.call('read_capture', 'GERAMEG', True, capture_id=CAPTURE_ID)

    def test_expanded_complete_original_through_producer_rpc_and_transport(self):
        result = self.read(self.client(stdout_limit_bytes=EXPANDED_STDOUT_BYTES))
        self.assertEqual(result[1].data, self.fixture.data)
        self.assertEqual(hashlib.sha256(result[1].data).hexdigest(), self.fixture.manifest['sha256'])
        self.assertEqual(len(self.fixture.calls), 1 + math.ceil(len(self.fixture.data) / EXPANDED_CHUNK_BYTES))
        self.assertTrue(all(r['chunk_bytes'] == EXPANDED_CHUNK_BYTES for r, _ in self.fixture.calls[1:]))
        self.assertLessEqual(max(self.fixture.reply_sizes), EXPANDED_STDOUT_BYTES)
        self.assertGreater(max(self.fixture.reply_sizes), MAX_STDOUT_BYTES)
        # A full-sized serialized reply is measured, not estimated from base64 alone.
        self.assertGreater(max(self.fixture.reply_sizes), 60000)
        self.assertGreater(EXPANDED_STDOUT_BYTES - max(self.fixture.reply_sizes), 4096)
        for key in ('capture_id', 'node', 'session_id', 'width', 'height', 'binding', 'requested_session_visible'):
            self.assertEqual(result[0]['result'][key], self.fixture.manifest[key])
        self.assertEqual(result[0]['node_id'], NODE_ID)
        legacy_calls = 1 + math.ceil(len(self.fixture.data) / CHUNK_BYTES)
        self.assertGreater(legacy_calls, 10 * len(self.fixture.calls))

    def test_legacy_omits_extension_and_stays_within_4096_bytes(self):
        result = self.read(self.client())
        self.assertEqual(result[1].data, self.fixture.data)
        self.assertEqual(len(self.fixture.calls), 1 + math.ceil(len(self.fixture.data) / CHUNK_BYTES))
        self.assertTrue(all('chunk_bytes' not in r for r, _ in self.fixture.calls))
        self.assertLessEqual(max(self.fixture.reply_sizes), MAX_STDOUT_BYTES)

    def test_new_central_with_actual_frozen_receiver_uses_legacy_schema(self):
        with frozen_receiver(self) as (frozen_rpc, frozen_transport):
            self.fixture.rpc, self.fixture.transport = frozen_rpc, frozen_transport
            self.assertNotIn('max_chunk_bytes', frozen_rpc.public_manifest(self.fixture.manifest))
            # Only the capture itself is mocked: all frozen protocol and PNG reads
            # execute the actual base code. Never access a live desktop in tests.
            with patch.object(frozen_rpc, 'observe', return_value=self.fixture.manifest):
                for op, arguments in [('observe', {}), ('read_capture', {'capture_id': CAPTURE_ID})]:
                    with self.subTest(op=op):
                        self.fixture.calls.clear()
                        result = self.client(stdout_limit_bytes=EXPANDED_STDOUT_BYTES).call(
                            op, 'GERAMEG', True, **arguments)
                        self.assertEqual(result[1].data, self.fixture.data)
                        self.assertEqual(result[0]['result']['sha256'], self.fixture.manifest['sha256'])
                        self.assertNotIn('max_chunk_bytes', result[0]['result'])
                        self.assertTrue(all('chunk_bytes' not in request for request, _ in self.fixture.calls))
                        self.assertEqual(len(self.fixture.calls), 1 + math.ceil(len(self.fixture.data) / CHUNK_BYTES))
                        self.assertLessEqual(max(self.fixture.reply_sizes), MAX_STDOUT_BYTES)

    def test_capability_is_exact_bounded_int_and_cannot_expand_transport_budget(self):
        original = self.fixture.rpc.public_manifest
        for capability in (True, 0, -1, EXPANDED_CHUNK_BYTES + 1, 1.5, '45000'):
            with self.subTest(capability=capability), patch.object(
                    self.fixture.rpc, 'public_manifest',
                    side_effect=lambda value: dict(original(value), max_chunk_bytes=capability)):
                self.fixture.calls.clear()
                result = self.read(self.client(stdout_limit_bytes=EXPANDED_STDOUT_BYTES))
                self.assertEqual(result['error'], 'SCREEN_TRANSFER_CAPABILITY_INVALID')
                self.assertEqual(len(self.fixture.calls), 1)
        self.fixture.calls.clear()
        result = self.read(self.client())
        self.assertEqual(result[1].data, self.fixture.data)
        self.assertTrue(all('chunk_bytes' not in request for request, _ in self.fixture.calls))
        self.assertLessEqual(max(self.fixture.reply_sizes), MAX_STDOUT_BYTES)

    def test_ambiguous_submitted_input_failures_require_observe_without_claiming_performed(self):
        variants = ('non_ok', 'truncated', 'malformed', 'binding')
        for op, arguments in [('key', {'key': 'ENTER'}), ('text', {'text': 'fixture-only'})]:
            for variant in variants:
                with self.subTest(op=op, variant=variant):
                    calls = []
                    fixture = Executor()
                    def execute(**kwargs):
                        calls.append(kwargs)
                        outer = fixture(**kwargs)
                        if variant == 'non_ok':
                            return {'ok': False, 'error': 'SCREEN_RPC_FAILED'}
                        if variant == 'truncated':
                            outer['stdout_truncated'] = True
                        elif variant == 'malformed':
                            outer['stdout'] = '{invalid fixture'
                        elif variant == 'binding':
                            outer['mcp_script_sha256'] = '0' * 64
                        return outer
                    result = ScreenClient(execute, Image).call(
                        op, 'GERAMEG', True, capture_id=CAPTURE_ID, **arguments)
                    self.assertFalse(result['ok'])
                    self.assertTrue(result['observe_again'])
                    self.assertEqual(result['capture_id'], CAPTURE_ID)
                    self.assertNotIn('performed', result)
                    self.assertNotIn('capture_consumed', result)
                    self.assertEqual(len(calls), 1)

    def test_unconfirmed_input_image_integrity_failure_does_not_assert_performed(self):
        for variant in ('dimensions', 'chunk_hash', 'image_hash'):
            with self.subTest(variant=variant):
                fixture = Executor(variant)
                result = ScreenClient(fixture, Image).call(
                    'key', 'GERAMEG', True, capture_id=CAPTURE_ID, key='ENTER')
                self.assertFalse(result['ok'])
                self.assertTrue(result['observe_again'])
                self.assertNotIn('performed', result)
                self.assertNotIn('capture_consumed', result)
                self.assertEqual(sum(request['op'] == 'key' for request in fixture.requests), 1)

    def test_generic_rpc_exception_after_consumption_remains_uncertain(self):
        attempts = []
        def execute(**kwargs):
            request = shlex.split(kwargs['script'])[-1]
            output = io.StringIO()
            def interrupted(*args, **arguments):
                attempts.append('fixture-input-attempt')
                raise OSError('fixture interruption after consuming capture')
            with patch.object(rpc, 'state_directory', return_value=self.directory), \
                 patch.object(rpc, 'act', side_effect=interrupted), contextlib.redirect_stdout(output):
                code = rpc.main([request])
            return {'ok': False, 'exit_code': str(code), 'target': kwargs['target'], 'node_id': NODE_ID,
                    'mcp_job_id': kwargs['job_id'], 'mcp_script_sha256': kwargs['sha256'],
                    'stdout': output.getvalue(), 'stdout_truncated': False}
        result = ScreenClient(execute, Image).call(
            'key', 'GERAMEG', True, capture_id=CAPTURE_ID, key='ENTER')
        self.assertEqual(result['error'], 'SCREEN_REQUEST_FAILED')
        self.assertTrue(result['observe_again'])
        self.assertNotIn('performed', result)
        self.assertNotIn('capture_consumed', result)
        self.assertEqual(attempts, ['fixture-input-attempt'])
        self.assertTrue(rpc.load_capture(self.directory, CAPTURE_ID)['_action_consumed'])

    def test_chunk_rejection_after_unconfirmed_input_still_requires_observation(self):
        fixture = Executor()
        def execute(**kwargs):
            result = fixture(**kwargs)
            if fixture.requests[-1]['op'] == 'read_chunk':
                result.update(ok=False, exit_code='1', stdout=json.dumps(
                    {'ok': False, 'error': {'code': 'UNKNOWN_REQUEST_FIELDS'}}))
            return result
        result = ScreenClient(execute, Image).call(
            'key', 'GERAMEG', True, capture_id=CAPTURE_ID, key='ENTER')
        self.assertEqual(result['error'], 'UNKNOWN_REQUEST_FIELDS')
        self.assertTrue(result['observe_again'])
        self.assertNotIn('performed', result)
        self.assertNotIn('capture_consumed', result)
        self.assertEqual([request['op'] for request in fixture.requests], ['key', 'read_chunk'])

    def test_known_backend_rejection_keeps_original_meaning_without_uncertainty(self):
        for op, arguments in [('key', {'key': 'ENTER'}), ('text', {'text': 'fixture-only'})]:
            with self.subTest(op=op):
                fixture = Executor('error_exit1')
                result = ScreenClient(fixture, Image).call(
                    op, 'GERAMEG', True, capture_id=CAPTURE_ID, **arguments)
                self.assertEqual(result['error'], 'SESSION_NOT_VISIBLE')
                self.assertNotIn('observe_again', result)
                self.assertNotIn('performed', result)
                self.assertEqual(len(fixture.requests), 1)

    def test_truncation_and_corruption_never_return_partial_success(self):
        for variant, error in [('truncated', 'SCREEN_STDOUT_INVALID'), ('corrupt', 'SCREEN_IMAGE_HASH_MISMATCH')]:
            with self.subTest(variant=variant):
                self.fixture.variant = variant
                result = self.read(self.client(stdout_limit_bytes=EXPANDED_STDOUT_BYTES))
                self.assertIs(result['ok'], False)
                self.assertEqual(result['error'], error)
                self.assertNotIn('result', result)

    def test_monotonic_total_deadline_propagates_remaining_whole_seconds(self):
        clock = Clock()
        self.fixture.clock, self.fixture.latency = clock, 1.2
        with patch('framebridge_expanded.real_screen.screen_frontdoor.time.monotonic', clock):
            result = self.read(self.client(stdout_limit_bytes=EXPANDED_STDOUT_BYTES, transfer_timeout_s=4))
        self.assertEqual(result['error'], 'SCREEN_TRANSFER_TIMEOUT')
        self.assertEqual([t for _, t in self.fixture.calls], [4, 2, 1])
        self.assertLess(clock.now, 104)
        self.assertNotIn('result', result)

    def test_late_executor_response_cannot_publish_stale_success(self):
        clock = Clock()
        self.fixture.clock, self.fixture.latency = clock, 6
        with patch('framebridge_expanded.real_screen.screen_frontdoor.time.monotonic', clock):
            result = self.read(self.client(transfer_timeout_s=5))
        self.assertEqual(result['error'], 'SCREEN_TRANSFER_TIMEOUT')
        self.assertEqual(len(self.fixture.calls), 1)

    def test_deadline_after_confirmed_input_preserves_no_replay_warning(self):
        clock = Clock()
        self.fixture.clock, self.fixture.latency = clock, 1.2
        with patch('framebridge_expanded.real_screen.screen_frontdoor.time.monotonic', clock):
            result = self.client(transfer_timeout_s=3).call(
                'key', 'GERAMEG', True, capture_id=CAPTURE_ID, key='ENTER')
        self.assertEqual(result['error'], 'SCREEN_TRANSFER_TIMEOUT')
        self.assertTrue(result['performed'])
        self.assertTrue(result['capture_consumed'])
        self.assertTrue(result['observe_again'])
        self.assertEqual(result['capture_id'], CAPTURE_ID)

    def test_executor_exception_during_input_requires_fresh_observation(self):
        def failed(**kwargs):
            raise TimeoutError('test fixture timeout')
        result = ScreenClient(failed, Image).call(
            'key', 'GERAMEG', True, capture_id=CAPTURE_ID, key='ENTER')
        self.assertFalse(result['ok'])
        self.assertTrue(result['observe_again'])
        self.assertNotIn('performed', result)
        self.assertEqual(result['capture_id'], CAPTURE_ID)

    def test_chunk_extension_is_bounded_strict_and_legacy_default_remains(self):
        request = {'op': 'read_chunk', 'capture_id': CAPTURE_ID, 'index': 0,
                   'expected_sha256': self.fixture.manifest['sha256']}
        self.assertEqual(rpc.dispatch(request, self.directory)['result']['chunk_bytes'], CHUNK_BYTES)
        for value in (True, 0, -1, EXPANDED_CHUNK_BYTES + 1, 1.5, '45000'):
            with self.subTest(value=value), self.assertRaisesRegex(rpc.ScreenRpcError, 'CHUNK_BOUNDS_INVALID'):
                rpc.validate_request(dict(request, chunk_bytes=value))
        with self.assertRaisesRegex(rpc.ScreenRpcError, 'UNKNOWN_REQUEST_FIELDS'):
            rpc.validate_request(dict(request, extra='invalid'))

    def test_client_stdout_budgets_reject_one_byte_over_exact_boundary(self):
        for budget in (MAX_STDOUT_BYTES, EXPANDED_STDOUT_BYTES):
            for over in (0, 1):
                with self.subTest(budget=budget, over=over):
                    def execute(**kwargs):
                        envelope = {'ok': True, 'result': {'padding': ''}}
                        envelope['result']['padding'] = 'x' * (budget + over - len(rpc.encode(envelope)))
                        return {'ok': True, 'exit_code': '0', 'target': kwargs['target'], 'node_id': NODE_ID,
                                'mcp_job_id': kwargs['job_id'], 'mcp_script_sha256': kwargs['sha256'],
                                'stdout': rpc.encode(envelope).decode(), 'stdout_truncated': False}
                    client = ScreenClient(execute, Image, stdout_limit_bytes=budget)
                    if over:
                        with self.assertRaisesRegex(ScreenError, 'SCREEN_STDOUT_INVALID'):
                            client.remote('GERAMEG', {'op': 'status'})
                    else:
                        self.assertIn('padding', client.remote('GERAMEG', {'op': 'status'}))

    def test_typed_transport_exact_serialized_boundary(self):
        script = RPC_COMMAND + shlex.quote(base64.b64encode(rpc.encode({'op': 'status'})).decode()) + '\n'
        payload = {'version': 1, 'mode': 'EXEC', 'authorized': True, 'script': script,
                   'timeout_s': 30, 'job_id': 'test-screen-boundary', 'sha256': hashlib.sha256(script.encode()).hexdigest(),
                   'include_stdout': True, 'request_type': 'screen_rpc'}
        for over in (0, 1):
            envelope = {'ok': True, 'result': {'padding': ''}}
            envelope['result']['padding'] = 'x' * (EXPANDED_STDOUT_BYTES + over - len(rpc.encode(envelope)) - 1)
            stdout = rpc.encode(envelope) + b'\n'
            self.assertEqual(len(stdout), EXPANDED_STDOUT_BYTES + over)
            result = transport.handle_request(payload, runner=lambda *args: (0, stdout, b''))
            self.assertEqual(result['ok'], not over)
            if over:
                self.assertIn('SCREEN_RPC_OUTPUT_LIMIT', result['stdout'])

    def test_main_serializes_competing_inputs_and_consumes_capture_once(self):
        request = base64.b64encode(rpc.encode({'op': 'key', 'capture_id': CAPTURE_ID, 'key': 'ENTER'})).decode()
        barrier = threading.Barrier(2)
        def invoke():
            barrier.wait(timeout=2)
            return rpc.main([request])
        with patch.object(rpc, 'state_directory', return_value=self.directory), \
             patch.object(rpc, 'act', return_value={'ok': True, 'performed': True}) as act, \
             patch.object(rpc, 'observe', return_value=dict(self.fixture.manifest)), \
             patch('builtins.print'), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: invoke(), range(2)))
        self.assertEqual(sorted(outcomes), [0, 1])
        self.assertEqual(act.call_count, 1)
        self.assertTrue(rpc.load_capture(self.directory, CAPTURE_ID)['_action_consumed'])

    def test_main_lock_covers_dispatch_in_competing_rpc_entries(self):
        first_entered, second_entered, release = threading.Event(), threading.Event(), threading.Event()
        count = 0
        mutex = threading.Lock()
        def dispatch(*args):
            nonlocal count
            with mutex:
                count += 1
                current = count
            if current == 1:
                first_entered.set()
                self.assertTrue(release.wait(2))
            else:
                second_entered.set()
            return {'ok': True, 'result': {}}
        request = base64.b64encode(rpc.encode({'op': 'status'})).decode()
        with patch.object(rpc, 'state_directory', return_value=self.directory), \
             patch.object(rpc, 'dispatch', side_effect=dispatch), patch('builtins.print'), \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(rpc.main, [request])
            try:
                self.assertTrue(first_entered.wait(2))
                second = pool.submit(rpc.main, [request])
                self.assertFalse(second_entered.wait(0.1))
            finally:
                release.set()
            self.assertEqual((first.result(timeout=2), second.result(timeout=2)), (0, 0))
        self.assertTrue(second_entered.is_set())


if __name__ == '__main__':
    unittest.main()
