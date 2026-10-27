import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from framebridge_expanded.real_screen import screen_rpc as rpc


class ScreenRpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.counter = 0
        self.binding = {'backend': 'visible-tmux', 'pane_id': '%1', 'node': 'TEST', 'uid': os.getuid()}
        self.actions = []
        self.patches = [
            patch.object(rpc, 'snapshot_binding', return_value=self.binding),
            patch.object(rpc, 'capture_screen', side_effect=self.capture),
            patch.object(rpc, 'bind_screen', side_effect=lambda m: {**m, 'binding': self.binding}),
            patch.object(rpc, 'read_chunk', side_effect=self.chunk),
            patch.object(rpc, 'act', side_effect=self.action),
            patch.object(rpc.time, 'sleep'),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, directory):
        self.counter += 1
        capture_id = f'{self.counter:032x}'
        data = b'actual-pixel-test-fixture-' + capture_id.encode()
        path = directory / (capture_id + '.png')
        path.write_bytes(data)
        return {'capture_id': capture_id, 'source': 'linux-framebuffer', 'screen_type': 'tty',
                'node': 'TEST', 'uid': os.getuid(), 'width': 1920, 'height': 1080,
                'path': str(path), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                'active_vt': 'tty1', 'synthetic': False, 'pixel_source_verified': True}

    def chunk(self, directory, capture_id, index, **kwargs):
        data = (directory / (capture_id + '.png')).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != kwargs['expected_sha256']:
            raise rpc.CaptureError('CAPTURE_SHA256_MISMATCH')
        return {'capture_id': capture_id, 'index': index, 'bytes': len(data), 'sha256': digest,
                'total_chunks': 1, 'encoding': 'base64', 'data': base64.b64encode(data).decode(), 'more': False}

    def action(self, manifest, kind, **kwargs):
        self.actions.append((manifest['capture_id'], kind, kwargs))
        return {'performed': True}

    def observe(self, **kwargs):
        return rpc.dispatch({'op': 'observe', **kwargs}, self.directory)['result']

    def test_observation_uses_capture_pixels_not_terminal_text(self):
        m = self.observe(session_id='hidden_terminal')
        self.assertTrue(m['full_screen'] and m['authentic_pixels'])
        self.assertFalse(m['requested_session_visible'])
        self.assertNotIn('path', m)
        self.assertTrue(m['control_available'])
        self.assertEqual(m['width'], 1920)

    def test_hidden_terminal_id_cannot_receive_input_for_visible_image(self):
        m = self.observe()
        with self.assertRaisesRegex(rpc.ScreenRpcError, 'SESSION_NOT_VISIBLE'):
            rpc.dispatch({'op': 'key', 'capture_id': m['capture_id'], 'session_id': 'hidden_terminal', 'key': 'ENTER'}, self.directory)
        self.assertEqual(self.actions, [])

    def test_input_returns_new_observation_and_cannot_replay_old_capture(self):
        m = self.observe()
        after = rpc.dispatch({'op': 'text', 'capture_id': m['capture_id'], 'session_id': m['session_id'], 'text': 'visible marker'}, self.directory)['result']
        self.assertNotEqual(after['capture_id'], m['capture_id'])
        self.assertEqual(len(self.actions), 1)
        with self.assertRaisesRegex(rpc.ScreenRpcError, 'CAPTURE_ALREADY_USED'):
            rpc.dispatch({'op': 'key', 'capture_id': m['capture_id'], 'key': 'ENTER'}, self.directory)
        self.assertEqual(len(self.actions), 1)

    def test_unknown_send_result_does_not_replay_input(self):
        m = self.observe()
        with patch.object(rpc, 'act', side_effect=rpc.ControlError('SCREEN_BINDING_CHANGED')):
            with self.assertRaisesRegex(rpc.ScreenRpcError, 'SCREEN_BINDING_CHANGED'):
                rpc.dispatch({'op': 'key', 'capture_id': m['capture_id'], 'key': 'ENTER'}, self.directory)
        with self.assertRaisesRegex(rpc.ScreenRpcError, 'CAPTURE_ALREADY_USED'):
            rpc.dispatch({'op': 'key', 'capture_id': m['capture_id'], 'key': 'ENTER'}, self.directory)

    def test_target_change_during_capture_returns_no_bound_image(self):
        with patch.object(rpc, 'bind_screen', side_effect=lambda m: {**m, 'binding': {**self.binding, 'pane_id': '%0'}}):
            with self.assertRaisesRegex(rpc.ScreenRpcError, 'VISIBLE_TARGET_CHANGED_DURING_CAPTURE'):
                self.observe()
        self.assertEqual(list(self.directory.glob('*.bound.json')), [])

    def test_sent_input_is_reported_if_following_capture_fails(self):
        m = self.observe()
        with patch.object(rpc, 'capture_screen', side_effect=rpc.CaptureError('ACTIVE_VT_CHANGED')):
            with self.assertRaises(rpc.ScreenRpcError) as caught:
                rpc.dispatch({'op': 'key', 'capture_id': m['capture_id'], 'key': 'ENTER'}, self.directory)
        self.assertEqual(caught.exception.code, 'INPUT_SENT_OBSERVE_FAILED')
        self.assertTrue(caught.exception.details['performed'])
        self.assertEqual(len(self.actions), 1)

    def test_view_can_succeed_without_safe_input_target(self):
        with patch.object(rpc, 'snapshot_binding', side_effect=rpc.ControlError('SCREEN_VISIBLE_TMUX_NOT_FOUND')):
            m = self.observe()
        self.assertFalse(m['control_available'])
        with self.assertRaisesRegex(rpc.ScreenRpcError, 'VISIBLE_INPUT_UNAVAILABLE'):
            rpc.dispatch({'op': 'key', 'capture_id': m['capture_id'], 'key': 'ENTER'}, self.directory)
        self.assertEqual(self.actions, [])

    def test_changed_png_cannot_drive_input(self):
        m = self.observe()
        (self.directory / (m['capture_id'] + '.png')).write_bytes(b'different pixels')
        with self.assertRaisesRegex(rpc.CaptureError, 'CAPTURE_SHA256_MISMATCH'):
            rpc.dispatch({'op': 'key', 'capture_id': m['capture_id'], 'key': 'ENTER'}, self.directory)
        self.assertEqual(self.actions, [])

    def test_read_requires_expected_image_hash(self):
        m = self.observe()
        with self.assertRaisesRegex(rpc.ScreenRpcError, 'CAPTURE_SHA256_MISMATCH'):
            rpc.dispatch({'op': 'read_chunk', 'capture_id': m['capture_id'], 'index': 0, 'expected_sha256': '0' * 64}, self.directory)


if __name__ == '__main__':
    unittest.main()
