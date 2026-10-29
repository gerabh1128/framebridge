"""Optional private desktops never change normal installation/readiness."""
import contextlib
import io
import json
import re
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging'))
from build_expanded import package_relationships
from build_profiles import PROFILES, target_profile
from framebridge_expanded import cli, config, display_context as dc, runtime


class DesktopPackageRelationships(unittest.TestCase):
    def test_no_automatic_virtual_desktop_in_any_target(self):
        for target in PROFILES:
            with self.subTest(target=target):
                fields = package_relationships(target_profile(target))
                def packages(value):
                    return {re.match(r'[a-z0-9+.-]+', item.strip()).group()
                            for item in re.split(r'[,|]', value) if item.strip()}
                required = packages(fields['Depends'])
                recommended = packages(fields.get('Recommends', ''))
                self.assertTrue({'openbox', 'xvfb'}.isdisjoint(required | recommended))
                self.assertEqual({'openbox', 'xvfb'}, set(fields['Suggests'].split(', ')))
                self.assertIn('xauth', required)
                self.assertIn('tmux', required)


class OptionalPrivateDesktop(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='fb-optional-desktop-')
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        config.save_config(self.state, config.default_config())
        self.env = {'PATH': '/fixture/bin'}
        dc.bind_text(self.env, self.state)

    @contextlib.contextmanager
    def prerequisites(self, missing, physical=False):
        def which(name, path=None):
            return None if name in missing else '/fixture/bin/' + name
        compatible = {'ok': True, 'profile_id': 'debian13-amd64-py313',
                      'issues': [], 'warnings': []}
        context = {'kind': 'physical-x11', 'physical': True} if physical else {'kind': 'text', 'physical': False}
        with patch.object(runtime, 'environment', return_value=dict(self.env)), \
             patch.object(runtime.shutil, 'which', side_effect=which), \
             patch.object(runtime, '_display_ok', return_value=physical), \
             patch.object(dc, 'describe', return_value=context), \
             patch('framebridge_expanded.network.inspect', return_value={}), \
             patch('framebridge_expanded.compatibility.assess', return_value=compatible):
            yield

    def mode(self, value):
        settings = config.load_config(self.state)
        settings['headless'] = value
        config.save_config(self.state, settings)

    def test_normal_text_mode_ready_without_optional_tools(self):
        with self.prerequisites({'openbox', 'Xvfb'}):
            result = runtime.doctor(self.state)
        self.assertTrue(result['ok'])
        self.assertEqual([], result['missing_commands'])
        self.assertTrue(result['capabilities']['fbexec'])
        self.assertTrue(result['capabilities']['fbterm'])
        self.assertFalse(result['capabilities']['private_desktop'])
        self.assertEqual(['Xvfb', 'openbox'], result['optional_missing_commands']['private_desktop'])
        self.assertEqual(dc.VIRTUAL_INSTALL_COMMAND, result['optional_install_commands']['private_desktop'])

    def test_existing_physical_x11_does_not_require_virtual_tools(self):
        self.mode('no')
        with self.prerequisites({'openbox', 'Xvfb'}, physical=True):
            result = runtime.doctor(self.state)
        self.assertTrue(result['ok'])
        self.assertTrue(result['capabilities']['graphical_cua'])
        self.assertFalse(result['capabilities']['private_desktop'])
        self.assertEqual([], result['missing_commands'])

    def test_explicit_virtual_start_fails_before_spawning_with_install_hint(self):
        self.mode('yes')
        with self.prerequisites({'openbox'}), \
             patch.object(runtime, 'status', return_value={'running': False}), \
             patch.object(runtime, '_spawn') as spawn:
            result = runtime.start(self.state)
        self.assertFalse(result['ok'])
        self.assertIn('openbox', result['doctor']['missing_commands'])
        self.assertIn(dc.VIRTUAL_INSTALL_COMMAND, result['error'])
        spawn.assert_not_called()

    def test_manager_rechecks_missing_tool_before_creating_any_child(self):
        self.mode('yes')
        with self.prerequisites({'openbox'}), \
             patch.object(runtime.signal, 'signal'), \
             patch.object(runtime, '_spawn') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'Private virtual desktop tools are missing: openbox'):
                runtime._manager(self.state)
        spawn.assert_not_called()
        error = json.loads((self.state/'last-error.json').read_text())['error']
        self.assertIn(dc.VIRTUAL_INSTALL_COMMAND, error)

    def test_virtual_tools_available_with_no_optional_install_hint(self):
        with self.prerequisites(set()):
            result = runtime.doctor(self.state)
        self.assertTrue(result['capabilities']['private_desktop'])
        self.assertIsNone(result['optional_install_commands']['private_desktop'])

    def test_human_doctor_explains_optional_install_without_failing(self):
        fake_runtime = SimpleNamespace(doctor=Mock(return_value={
            'ok': True, 'display': 'text tools', 'missing_commands': [], 'issues': [],
            'platform_compatibility': {'ok': True},
            'optional_missing_commands': {'private_desktop': ['openbox']},
            'optional_install_commands': {'private_desktop': dc.VIRTUAL_INSTALL_COMMAND}}))
        stdout = io.StringIO()
        with patch.object(cli, '_module', return_value=fake_runtime), \
             contextlib.redirect_stdout(stdout):
            code = cli.main(['--state-dir', str(self.state), 'doctor'])
        self.assertEqual(0, code)
        self.assertIn('Optional private desktop tools missing: openbox', stdout.getvalue())
        self.assertIn(dc.VIRTUAL_INSTALL_COMMAND, stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
