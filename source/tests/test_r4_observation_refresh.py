"""Local candidate regression tests; all desktop/process operations are mocked."""
import ast
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch
from r4_ast import semantic_ast_digest

TEST_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = TEST_ROOT.parent
PACKAGE_ROOT = SOURCE_ROOT / 'expanded/framebridge_expanded'
FIXTURES = TEST_ROOT / 'fixtures/r4-035'
PROFILE = Path('/profile/NODE_A')


def load(area):
    name = '_fb_public_r4_' + area
    package = types.ModuleType(name)
    package.__path__ = [str(FIXTURES if area == 'baseline' else PACKAGE_ROOT), str(PACKAGE_ROOT)]
    sys.modules[name] = package
    runtime = importlib.import_module(name + '.runtime')
    display = importlib.import_module(name + '.display_context')
    compatibility = types.ModuleType(name + '.compatibility')
    compatibility.assess = lambda prefix: {
        'ok': True, 'profile_id': 'debian13-amd64-py313', 'issues': []}
    compatibility.display_support = lambda env, context: {
        'physical_x11_verified': context.get('kind') == 'physical-x11',
        'native_wayland_cua': False}
    network = types.ModuleType(name + '.network')
    network.inspect = lambda: {}
    sys.modules[name + '.compatibility'] = compatibility
    sys.modules[name + '.network'] = network
    return runtime, display


class ObservationRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline, cls.baseline_display = load('baseline')
        cls.candidate, cls.display = load('candidate')

    def setUp(self):
        self.old_context = {
            'kind': 'text', 'physical': False, 'explicit': False,
            'selection': 'auto', 'profile': str(PROFILE), 'display': '',
            'reason': 'PHYSICAL_X11_UNAVAILABLE_TEXT_TOOLS_ACTIVE'}
        self.shared = {
            'PATH': '/usr/bin', 'XDG_SESSION_TYPE': 'tty',
            'FRAMEBRIDGE_DISPLAY_BINDING': json.dumps(self.old_context)}
        self.initial = copy.deepcopy(self.shared)
        self.config = {'headless': 'auto', 'role': 'node',
                       'name': 'NODE_A', 'node_id': 'test-node',
                       'ports': {'mcp': 8865}}
        self.ready_context = copy.deepcopy(self.old_context)
        self.discoveries = []
        self.process_queries = []
        self.root_query_rc = 0
        self.manager_alive = True
        self.physical = {
            'kind': 'physical-x11', 'physical': True, 'session_id': '3',
            'uid': 1000, 'leader': 9001, 'active_vt': 'tty7', 'seat': 'seat0',
            'vtnr': 7, 'display': ':0', 'authority': '/private/test/Xauthority'}

        def discover():
            self.discoveries.append(True)
            return dict(self.physical)

        def query(args, **kwargs):
            self.process_queries.append((args, kwargs))
            self.assertEqual(args, ['xwininfo', '-root'])
            return subprocess.CompletedProcess(args, self.root_query_rc, b'', b'')

        def read(path):
            return {
                'manager.json': {'pid': 1},
                'worker.json': {'processes': [{'pid': 2}]},
                'ready.json': {'ready': True, 'display_context': self.ready_context},
                'last-error.json': {},
            }.get(path.name, {})

        for runtime, display in [(self.baseline, self.baseline_display),
                                 (self.candidate, self.display)]:
            for target, name, value in [
                (runtime, 'environment', lambda *a, **kw: self.shared),
                (runtime, 'state_path', lambda *a: PROFILE),
                (runtime, 'load_config', lambda *a: dict(self.config)),
                (runtime, '_read', read),
                (runtime, '_alive', lambda *a: self.manager_alive),
                (display, '_physical', discover),
                (display, 'missing_graphical_commands', lambda env: []),
            ]:
                p = patch.object(target, name, value)
                p.start()
                self.addCleanup(p.stop)
        p = patch.object(self.candidate.subprocess, 'run', query)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(self.candidate.shutil, 'which', lambda *a, **k: '/usr/bin/stub')
        p.start()
        self.addCleanup(p.stop)

    def test_reproduces_baseline_stale_text_then_candidate_observes_current_gui(self):
        before = self.baseline.status(PROFILE)
        self.assertFalse(before['capabilities']['graphical_cua'])
        self.assertEqual(self.discoveries, [])
        after = self.candidate.status(PROFILE)
        self.assertTrue(after['capabilities']['graphical_cua'])
        self.assertEqual(after['display_context']['session_id'], '3')
        self.assertEqual(after['display_context']['display'], ':0')
        self.assertEqual(len(self.discoveries), 1)
        self.assertEqual(len(self.process_queries), 1)

    def test_refresh_changes_only_copy_not_worker_binding_or_process_environment(self):
        host_environment = dict(os.environ)
        view = self.candidate._observation_environment(PROFILE, self.config)
        self.assertIsNot(view, self.shared)
        self.assertEqual(self.shared, self.initial)
        self.assertEqual(dict(os.environ), host_environment)
        self.assertNotIn('authority', self.display.describe(view))
        self.assertEqual(self.display.describe(self.shared)['kind'], 'text')

    def test_doctor_uses_fresh_observation_without_capturing_or_sending_input(self):
        result = self.candidate.doctor(PROFILE)
        self.assertTrue(result['capabilities']['graphical_cua'])
        self.assertEqual(result['display_context']['kind'], 'physical-x11')
        self.assertEqual(self.shared, self.initial)
        self.assertEqual([q[0] for q in self.process_queries], [['xwininfo', '-root']])

    def test_missing_gui_is_false_not_inherited_success(self):
        def missing():
            raise self.display.DisplayContextError('PHYSICAL_X11_SESSION_UNAVAILABLE')
        with patch.object(self.display, '_physical', missing):
            result = self.candidate.status(PROFILE)
        self.assertFalse(result['capabilities']['graphical_cua'])
        self.assertEqual(result['display_context']['kind'], 'text')
        self.assertEqual(result['display_context']['reason'], 'PHYSICAL_X11_SESSION_UNAVAILABLE')
        self.assertEqual(self.process_queries, [])

    def test_unusable_x11_connection_remains_unavailable(self):
        self.root_query_rc = 1
        result = self.candidate.status(PROFILE)
        self.assertFalse(result['capabilities']['graphical_cua'])
        self.assertEqual(result['display_context']['reason'],
                         'PHYSICAL_X11_UNAVAILABLE_TEXT_TOOLS_ACTIVE')
        self.assertEqual(self.shared, self.initial)

    def test_missing_graphical_tools_preserves_specific_reason(self):
        with patch.object(self.display, 'missing_graphical_commands', lambda env: ['xclip']):
            result = self.candidate.status(PROFILE)
        self.assertFalse(result['capabilities']['graphical_cua'])
        self.assertEqual(result['display_context']['reason'], 'GRAPHICAL_CUA_TOOLS_UNAVAILABLE')
        self.assertEqual(self.process_queries, [])

    def test_explicit_virtual_selection_is_not_rebound_to_physical(self):
        virtual = {'kind': 'virtual-x11', 'physical': False, 'explicit': True,
                   'selection': 'yes', 'profile': str(PROFILE), 'display': ':140'}
        self.config['headless'] = 'yes'
        self.shared['FRAMEBRIDGE_DISPLAY_BINDING'] = json.dumps(virtual)
        self.shared['DISPLAY'] = ':140'
        self.ready_context = virtual
        view = self.candidate._observation_environment(PROFILE, self.config)
        self.assertEqual(self.display.describe(view), virtual)
        result = self.candidate.status(PROFILE)
        self.assertTrue(result['capabilities']['graphical_cua'])
        self.assertEqual(self.discoveries, [])
        self.assertEqual(self.process_queries, [])

    def test_explicit_physical_selection_is_not_silently_replaced(self):
        self.config['headless'] = 'no'
        self.candidate._observation_environment(PROFILE, self.config)
        self.assertEqual(self.discoveries, [])
        self.assertEqual(self.process_queries, [])
        self.assertEqual(self.shared, self.initial)

    def test_stopped_runtime_does_not_claim_active_graphical_cua(self):
        self.manager_alive = False
        result = self.candidate.status(PROFILE)
        self.assertFalse(result['capabilities']['graphical_cua'])
        self.assertEqual(self.discoveries, [])

    def test_observer_failure_is_reported_unavailable(self):
        with patch.object(self.candidate, '_observation_environment',
                          side_effect=OSError('test discovery failure')):
            result = self.candidate.status(PROFILE)
        self.assertFalse(result['capabilities']['graphical_cua'])
        self.assertEqual(result['display_context']['reason'], 'DISPLAY_DETECTION_UNAVAILABLE')

    def test_worker_authority_and_input_guard_sources_unchanged(self):
        import hashlib
        guards = json.loads((FIXTURES / 'STRUCTURAL-GUARDS.json').read_text())
        for name, expected in guards['unchanged_helpers'].items():
            self.assertEqual(hashlib.sha256((PACKAGE_ROOT / name).read_bytes()).hexdigest(), expected)
        text = (PACKAGE_ROOT / 'runtime.py').read_text()
        function = next(n for n in ast.parse(text).body
                        if isinstance(n, ast.FunctionDef) and n.name == 'adapter_request')
        actual = semantic_ast_digest(function)
        self.assertEqual(actual, guards['runtime']['adapter_request'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
