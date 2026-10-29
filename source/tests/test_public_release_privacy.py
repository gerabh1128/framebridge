"""Regression checks for portable helpers and removed implicit lab routing."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
FRONT_DOORS = (
    SOURCE / 'portable/gerameg_cua_frontdoor.py',
    SOURCE / 'sources/original-geranas/FrameBridge-Front-Door/bin/gerameg_cua_frontdoor.py',
)
RPC = SOURCE / 'sources/original-gerameg/cua_tty_rpc.py'
HOST = SOURCE / 'sources/original-asus/framebridge-native/host/framebridge_host.py'
LAUNCHER = SOURCE / 'packaging/framebridge-original'
PROGRESS_CONSOLE = SOURCE / 'sources/original-asus/framebridge-native/host/open-progress-console.sh'


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicReleasePrivacyTests(unittest.TestCase):
    def test_captured_journal_uses_account_state_without_payload_contents(self):
        # Invoke only the journal writer with synthetic metadata in a temporary HOME.
        tree = ast.parse(HOST.read_text())
        journal = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_cua_runtime_journal')
        namespace = {'Path': Path, 'json': json}
        exec(compile(ast.Module(body=[journal], type_ignores=[]), str(HOST), 'exec'), namespace)
        with tempfile.TemporaryDirectory(prefix='framebridge-journal-') as temporary:
            home = Path(temporary) / 'different account'
            with mock.patch.object(Path, 'home', return_value=home):
                namespace['_cua_runtime_journal']('synthetic_event',
                    {'id': 'example-request', 'action': 'STATUS',
                        'script': 'private-example-input-not-to-log'}, rc=0)
            log = home / '.local/state/framebridge/cua-runtime/cua-runtime.log'
            records = log.read_text().splitlines()
            self.assertEqual(len(records), 1)
            record = json.loads(records[0])
            self.assertEqual(record['event'], 'synthetic_event')
            self.assertEqual(record['id'], 'example-request')
            self.assertNotIn('private-example-input-not-to-log', records[0])
            self.assertEqual([path.name for path in home.iterdir()], ['.local'])

    def test_captured_launcher_keeps_progress_inside_generic_profile_state(self):
        # settings() computes paths; no process or UI is started by this check.
        import hashlib
        tree = ast.parse(LAUNCHER.read_text())
        settings = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'settings')
        with tempfile.TemporaryDirectory(prefix='framebridge-profile-') as temporary:
            home = Path(temporary) / 'different account'
            state = home / 'selected profile'
            namespace = {'Path': Path, 'os': os, 'hashlib': hashlib,
                'REAL_HOME': home, 'PREFIX': Path('/opt/framebridge-original'),
                'LAYOUT': {'fbterm_bin': 'fbterm', 'native_host': 'host.py'}}
            exec(compile(ast.Module(body=[settings], type_ignores=[]), str(LAUNCHER), 'exec'), namespace)
            args = types.SimpleNamespace(state_dir=str(state), port=12345, hub_port=12346, broker_port=12347)
            with mock.patch.dict(os.environ, {'HOME': str(home)}, clear=True):
                actual_state, profile_home, environment = namespace['settings'](args)
            self.assertEqual(actual_state, state)
            self.assertEqual(profile_home, state / 'home')
            progress = Path(environment['FRAMEBRIDGE_PROGRESS_DIR'])
            self.assertEqual(progress, profile_home / '.local/state/framebridge/progress')
            self.assertEqual(Path(environment['FRAMEBRIDGE_PROGRESS_LOG']), progress / 'framebridge-progress.log')
            self.assertFalse(state.exists())

    def test_progress_console_uses_account_state_and_honors_explicit_overrides(self):
        # Execute the two variable assignments only, never the graphical launcher.
        assignments = [line for line in PROGRESS_CONSOLE.read_text().splitlines()
            if line.startswith(('LOG_DIR=', 'LOG='))]
        self.assertEqual(len(assignments), 2)
        script = '\n'.join(assignments) + '\nprintf "%s\\0%s" "$LOG_DIR" "$LOG"\n'
        with tempfile.TemporaryDirectory(prefix='framebridge-progress-') as temporary:
            home = Path(temporary) / 'different account'
            cases = [({}, home / '.local/state/framebridge/progress', None),
                ({'FRAMEBRIDGE_PROGRESS_DIR': str(home / 'custom directory')}, home / 'custom directory', None),
                ({'FRAMEBRIDGE_PROGRESS_LOG': str(home / 'specific file.log')},
                    home / '.local/state/framebridge/progress', home / 'specific file.log')]
            for overrides, expected_directory, expected_file in cases:
                with self.subTest(overrides=tuple(overrides)):
                    completed = subprocess.run(['/bin/bash', '-c', script],
                        env={'HOME': str(home), **overrides}, capture_output=True, timeout=10, check=True)
                    directory, log = completed.stdout.decode().split('\0')
                    self.assertEqual(Path(directory), expected_directory)
                    self.assertEqual(Path(log), expected_file or expected_directory / 'framebridge-progress.log')
            self.assertFalse(home.exists())

    def test_legacy_rpc_resolves_home_in_execution_account_and_quotes_arguments(self):
        # Execute only our synthetic stub, not the captured CUA client or server.
        with tempfile.TemporaryDirectory(prefix='framebridge-public-check-') as temporary:
            remote_home = Path(temporary) / 'different user home'
            stub = remote_home / '.local/lib/framebridge-gerameg-original/cua_tty_rpc.py'
            stub.parent.mkdir(parents=True)
            stub.write_text('import json, os, sys\nprint(json.dumps({"home": os.environ["HOME"], "args": sys.argv[1:]}))\n')
            marker = Path(temporary) / 'must-not-exist'
            arguments = ['request', 'spaces and quotes \' "', '$(touch ' + str(marker) + ')', '; false']
            for index, path in enumerate(FRONT_DOORS):
                with self.subTest(front_door=index):
                    module = load_module(path, 'public_front_door_' + str(index))
                    with mock.patch.object(Path, 'home', return_value=Path('/unrelated/central/home')):
                        command = module._remote_script(arguments)
                    completed = subprocess.run(['/bin/sh', '-c', command],
                        env={**os.environ, 'HOME': str(remote_home)},
                        text=True, capture_output=True, timeout=10, check=True)
                    result = json.loads(completed.stdout)
                    self.assertEqual(result['home'], str(remote_home))
                    self.assertEqual(result['args'], arguments)
                    self.assertFalse(marker.exists())

    def test_legacy_rpc_requires_a_remote_home(self):
        for index, path in enumerate(FRONT_DOORS):
            module = load_module(path, 'public_front_door_no_home_' + str(index))
            environment = dict(os.environ)
            environment.pop('HOME', None)
            completed = subprocess.run(['/bin/sh', '-c', module._remote_script(['request'])],
                env=environment, text=True, capture_output=True, timeout=10)
            self.assertNotEqual(completed.returncode, 0)

    def test_captured_rpc_uses_accounts_home_and_retains_identity_guard(self):
        with tempfile.TemporaryDirectory(prefix='framebridge-owner-home-') as temporary:
            home = Path(temporary)
            with mock.patch.object(Path, 'home', return_value=home):
                module = load_module(RPC, 'public_captured_rpc')
                self.assertEqual(module.HOME, home)
                with mock.patch.object(module.os, 'getuid', return_value=module.UID), \
                     mock.patch.object(module.os, 'geteuid', return_value=module.UID), \
                     mock.patch.object(module.socket, 'gethostname', return_value=module.HOSTNAME):
                    module.guard_runtime()
                    with mock.patch.object(module.os, 'geteuid', return_value=module.UID + 1):
                        with self.assertRaisesRegex(module.RpcError, 'UID_MISMATCH'):
                            module.guard_runtime()
                    with mock.patch.object(Path, 'home', return_value=home / 'other'):
                        with self.assertRaisesRegex(module.RpcError, 'HOME_MISMATCH'):
                            module.guard_runtime()

    def test_no_implicit_lab_node_is_reachable_through_classic_routes(self):
        # Compile the early-routing functions only: no native host launch or CUA.
        tree = ast.parse(HOST.read_text())
        selected = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CLASSIC_NODE_TARGETS' for t in node.targets):
                selected.append(node)
            if isinstance(node, ast.FunctionDef) and node.name in ('route_classic_node', 'route_classic_plan'):
                selected.append(node)
        namespace = {'NODE_TARGET_RE': re.compile(r'[A-Z0-9_-]+')}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(HOST), 'exec'), namespace)
        self.assertEqual(namespace['CLASSIC_NODE_TARGETS'], {})
        for target in ('FRESH-MANUAL', 'USER-WORKER'):
            message = {'target': target, 'mode': 'EXEC', 'lang': 'BASH', 'script': 'printf example'}
            result = namespace['route_classic_node'](message)
            self.assertFalse(result['executed'])
            self.assertEqual(result['error'], 'CLASSIC_NODE_TARGET_UNKNOWN')
            result = namespace['route_classic_plan'](message)
            self.assertFalse(result['executed'])
            self.assertEqual(result['error'], 'NODEPLAN_TARGET_UNKNOWN')


if __name__ == '__main__':
    unittest.main()
