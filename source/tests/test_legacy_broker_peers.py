"""Legacy broker peer policy checks without sockets, desktops or live profiles."""
from __future__ import annotations

import contextlib
import io
from http.server import BaseHTTPRequestHandler
import tempfile
import unittest
from unittest import mock

from framebridge_expanded import broker


CONTROL_PATHS = (
    '/fbpair/v1/control/node-status',
    '/fbpair/v1/control/request',
    '/fbpair/v1/control/admin-exec',
    '/fbpair/v1/control/admin-cua',
    '/fbpair/v1/control/operation',
    '/fbpair/v1/control/result',
    '/dev/authenticate-and-approve',
    '/dev/approve-authenticated',
)


class BodyReadReached(Exception):
    pass


class NoStoreCalls:
    def __getattr__(self, name):
        raise AssertionError('The peer gate must run before Store access')


class LegacyBrokerPeerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = broker.original_modules()

    def request_fixture(self, handler, path, peer):
        request = object.__new__(handler)
        request.path = path
        request.client_address = (peer, 12345)
        # Forwarding headers must never replace the actual transport peer.
        request.headers = {'X-Forwarded-For': '127.0.0.1', 'X-Real-IP': '127.0.0.1'}
        request.read_json = mock.Mock(side_effect=BodyReadReached)
        request.send_json = mock.Mock()
        return request

    def test_default_policy_preserves_only_the_original_loopback_peer(self):
        allowed = self.original.parse_control_peers()
        self.assertEqual(len(allowed), 1)
        self.assertTrue(self.original.control_peer_allowed('127.0.0.1', allowed))
        for peer in ('192.0.2.15', '198.51.100.18', 'localhost', '', None):
            with self.subTest(kind=type(peer).__name__):
                self.assertFalse(self.original.control_peer_allowed(peer, allowed))

    def test_explicit_literal_peers_are_canonical_and_narrow(self):
        allowed = self.original.parse_control_peers(['192.0.2.15', '2001:0db8:0:0::15'])
        self.assertEqual(len(allowed), 3)
        self.assertTrue(self.original.control_peer_allowed('192.0.2.15', allowed))
        self.assertTrue(self.original.control_peer_allowed('2001:db8::15', allowed))
        self.assertTrue(self.original.control_peer_allowed('127.0.0.1', allowed))
        self.assertFalse(self.original.control_peer_allowed('192.0.2.16', allowed))

    def test_malformed_or_implicit_peer_configuration_is_rejected(self):
        invalid = ('192.0.2.15', ['', '192.0.2.15'], ['*'], ['0.0.0.0/0'], ['0.0.0.0'], ['::'],
            ['example.com'], ['[::1]'], ['fe80::1%eth0'], [' 192.0.2.15'], [None], [123])
        for value in invalid:
            with self.subTest(configuration_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, '^CONTROL_PEER_CONFIG_INVALID$'):
                    self.original.parse_control_peers(value)

    def test_cli_rejects_invalid_configuration_before_store_or_listener(self):
        with mock.patch.object(self.original, 'Store') as store, \
             mock.patch.object(self.original, 'ThreadingHTTPServer') as server, \
             mock.patch('sys.argv', ['legacy-broker', '--control-peer', 'example.com']), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as caught:
                self.original.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('CONTROL_PEER_CONFIG_INVALID', errors.getvalue())
        self.assertNotIn('example.com', errors.getvalue())
        store.assert_not_called()
        server.assert_not_called()

    def test_repeated_cli_peers_reach_the_handler_only_when_explicit(self):
        with mock.patch.object(self.original, 'Store'), \
             mock.patch.object(self.original, 'ThreadingHTTPServer') as server, \
             mock.patch.object(self.original, 'make_handler') as make_handler, \
             mock.patch('sys.argv', ['legacy-broker', '--control-peer', '192.0.2.15',
                 '--control-peer', '2001:db8::15']), contextlib.redirect_stdout(io.StringIO()):
            self.original.main()
        allowed = make_handler.call_args.kwargs['control_peers']
        self.assertEqual(len(allowed), 3)
        self.assertTrue(self.original.control_peer_allowed('192.0.2.15', allowed))
        self.assertTrue(self.original.control_peer_allowed('2001:db8::15', allowed))
        server.return_value.serve_forever.assert_called_once()

    def test_all_eight_routes_deny_an_unconfigured_peer_before_body_or_store(self):
        handler = self.original.make_handler(NoStoreCalls(), 'http://localhost:18765')
        for path in CONTROL_PATHS:
            with self.subTest(path=path):
                request = self.request_fixture(handler, path, '192.0.2.15')
                handler.do_POST(request)
                request.read_json.assert_not_called()
                request.send_json.assert_called_once_with(403, {'ok': False, 'error': 'local_only'})

    def test_loopback_and_explicit_peers_pass_the_gate_for_all_eight_routes(self):
        handler = self.original.make_handler(NoStoreCalls(), 'http://localhost:18765',
            control_peers=['192.0.2.15'])
        for path in CONTROL_PATHS:
            for peer in ('127.0.0.1', '192.0.2.15'):
                with self.subTest(path=path, source_kind='loopback' if peer.startswith('127.') else 'explicit'):
                    request = self.request_fixture(handler, path, peer)
                    with self.assertRaises(BodyReadReached):
                        handler.do_POST(request)
                    request.read_json.assert_called_once()

    def test_expanded_control_keeps_authentication_and_blocks_development_routes(self):
        def no_socket_init(server, address, handler):
            # Keep the real server base class so the bounded TLS subclass is
            # constructed normally. Replacing that base with a MagicMock would
            # make class construction/RequestHandlerClass itself a mock.
            server.server_address = address
            server.RequestHandlerClass = handler

        with tempfile.TemporaryDirectory(prefix='framebridge-broker-policy-') as state, \
             mock.patch.object(broker, '_config', return_value={}), \
             mock.patch.object(broker, '_control_secret', return_value='fixture-control'), \
             mock.patch.object(broker.ThreadingHTTPServer, '__init__', new=no_socket_init), \
             mock.patch.object(self.original, 'control_peer_allowed', side_effect=AssertionError('legacy gate reached')):
            server = broker.make_server(host='localhost', state_dir=state)
            handler = server.RequestHandlerClass
            self.assertIsInstance(handler, type)
            self.assertTrue(issubclass(handler, BaseHTTPRequestHandler))
            for path, status, error in (
                ('/fbpair/v1/control/nodes', 403, 'CONTROL_AUTH_REQUIRED'),
                ('/dev/authenticate-and-approve', 404, 'ENDPOINT_NOT_AVAILABLE'),
                ('/dev/approve-authenticated', 404, 'ENDPOINT_NOT_AVAILABLE'),
            ):
                with self.subTest(path=path):
                    request = self.request_fixture(handler, path, '127.0.0.1')
                    handler.do_POST(request)
                    request.read_json.assert_not_called()
                    request.send_json.assert_called_once_with(status, {'ok': False, 'error': error})


if __name__ == '__main__':
    unittest.main()
