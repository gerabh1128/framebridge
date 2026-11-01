import hashlib
from contextlib import nullcontext
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from framebridge_expanded import package_upgrade as upgrade


class PackageUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original_read = upgrade._read
        self.state = self.root / 'profile'
        self.state.mkdir(mode=0o700)
        self.config = {'role': 'node', 'node_id': 'test-node'}
        self.write(self.state / 'config.json', self.config)
        self.proc = self.root / '1234'
        self.proc.mkdir()
        self.identity = {'pid': 1234, 'birth': '99', 'uid': os.getuid()}
        self.write(self.state / 'manager.json', self.identity)
        self.args = ['/usr/bin/python3', '-m', 'framebridge_expanded.runtime',
                     '_manager', '--state', str(self.state)]
        (self.proc / 'cmdline').write_bytes(b'\0'.join(x.encode() for x in self.args) + b'\0')
        self.environ()
        (self.proc / 'stat').write_text('1234 (python3) S ' + ' '.join(['0'] * 18 + ['99']))
        (self.proc / 'cgroup').write_text('0::/user.slice/user-1000.slice/user@1000.service/app.slice/test.scope\n')

    def write(self, path, data):
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def environ(self, prefix=None):
        prefix = upgrade.PREFIX if prefix is None else prefix
        (self.proc / 'environ').write_bytes(b'\0'.join(x.encode() for x in [
            'FRAMEBRIDGE_EXPANDED_PREFIX=' + prefix,
            'FRAMEBRIDGE_EXPANDED_STATE=' + str(self.state),
            'PYTHONPATH=' + prefix + '/expanded:' + prefix + '/portable']) + b'\0')

    def identify(self):
        if os.getuid() == 0:
            self.skipTest('Identity fixtures run as the ordinary build owner, not root')
        return upgrade.identify(self.proc)

    def test_identifies_only_installed_manager_and_exact_owned_record(self):
        row = self.identify()
        self.assertEqual(row['pid'], 1234)
        self.assertEqual(row['state'], str(self.state))
        self.assertEqual(row['birth'], '99')
        self.assertIsNone(row['unit'])
        self.assertNotIn('config', row)

    def test_development_clone_is_not_an_installed_service(self):
        self.environ('/home/example/source')
        self.assertIsNone(self.identify())

    def test_arbitrary_python_command_is_ignored(self):
        (self.proc / 'cmdline').write_bytes(b'/usr/bin/python3\0-c\0print(1)\0')
        self.assertIsNone(upgrade.identify(self.proc))

    def test_pid_reuse_is_rejected(self):
        self.write(self.state / 'manager.json', dict(self.identity, birth='98'))
        if os.getuid() == 0:
            self.skipTest('Ordinary-owner fixture required')
        with self.assertRaisesRegex(RuntimeError, 'RECORD_MISMATCH'):
            upgrade.identify(self.proc)

    def test_recognizes_qa_unit_without_selecting_unrelated_services(self):
        (self.proc / 'cgroup').write_text('0::/user.slice/user@1000.service/app.slice/framebridge-qa-mint223-private2-c3418304.service\n')
        self.assertEqual(self.identify()['unit'], 'framebridge-qa-mint223-private2-c3418304.service')

    def test_unrelated_unit_is_not_restarted(self):
        (self.proc / 'cgroup').write_text('0::/user.slice/user@1000.service/app.slice/owner-important.service\n')
        self.assertIsNone(self.identify()['unit'])

    def test_world_readable_metadata_is_refused(self):
        path = self.state / 'config.json'
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'FILE_UNTRUSTED'):
            upgrade._read(path, os.getuid())

    def test_metadata_symlink_is_refused(self):
        path = self.state / 'alias.json'
        path.symlink_to(self.state / 'config.json')
        with self.assertRaises(OSError):
            upgrade._read(path, os.getuid())

    def test_shared_profile_directory_is_refused(self):
        self.state.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, 'DIRECTORY_UNTRUSTED'):
            upgrade._private(self.state, os.getuid())

    def root_read(self, path, uid, maximum=upgrade.MAXIMUM):
        # Root-owned production intent is represented by the test owner's file.
        return self.original_read(path, os.getuid(), maximum)

    def pending(self, profiles=None):
        value = {'schema': 1, 'transaction': '1' * 16, 'phase': 'CAPTURED', 'profiles': profiles or []}
        self.write(self.root / 'pending.json', value)
        return value

    def test_fresh_install_does_not_schedule_or_start_profiles(self):
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade.subprocess, 'run') as run:
            self.assertEqual(upgrade.schedule()['phase'], 'NO_UPGRADE_SNAPSHOT')
        run.assert_not_called()

    def test_empty_upgrade_stays_stopped(self):
        self.pending()
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade.subprocess, 'run') as run:
            self.assertEqual(upgrade.schedule()['phase'], 'COMPLETE')
        run.assert_not_called()

    def test_restart_is_detached_and_delayed_past_package_transaction(self):
        self.pending([{'state': str(self.state)}])
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(upgrade.schedule()['phase'], 'SCHEDULED')
        args = run.call_args.args[0]
        self.assertEqual(args[0], '/usr/bin/systemd-run')
        self.assertIn('--on-active=2s', args)
        self.assertEqual(args[-2:], [upgrade.MODULE, 'resume'])

    def test_scheduling_failure_keeps_captured_intent(self):
        self.pending([{'state': str(self.state)}])
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'SCHEDULE_FAILED'):
                upgrade.schedule()
        self.assertEqual(json.loads((self.root / 'pending.json').read_text())['phase'], 'CAPTURED')

    def test_repeated_configure_does_not_schedule_second_restart(self):
        self.write(self.root / 'pending.json', {'phase': 'SCHEDULED'})
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade.subprocess, 'run') as run:
            self.assertEqual(upgrade.schedule()['phase'], 'SCHEDULED')
        run.assert_not_called()

    def test_changed_manager_is_not_stopped_and_failure_is_recorded(self):
        row = {'uid': 1000, 'user': 'owner', 'home': '/home/owner',
               'state': str(self.state), 'pid': 1234, 'birth': '99',
               'config_sha256': 'a' * 64, 'unit': None, 'phase': 'CAPTURED'}
        self.write(self.root / 'pending.json', {'schema': 1, 'phase': 'SCHEDULED', 'profiles': [row]})
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade, '_wait_dpkg', return_value=nullcontext()), \
             mock.patch.object(upgrade, '_resume_lock', return_value=nullcontext()), \
             mock.patch.object(upgrade, 'identify', return_value=dict(row, birth='100')), \
             mock.patch.object(upgrade, '_owner') as owner:
            with self.assertRaisesRegex(RuntimeError, 'PROFILE_RESTART_FAILED'):
                upgrade.resume()
        owner.assert_not_called()
        value = json.loads((self.root / 'pending.json').read_text())
        self.assertEqual(value['phase'], 'FAILED')
        self.assertIn('IDENTITY_CHANGED', value['profiles'][0]['error'])

    def test_manual_profile_restart_never_enables_service(self):
        row = {'uid': 1000, 'user': 'owner', 'home': '/home/owner',
               'state': str(self.state), 'pid': 1234, 'birth': '99',
               'config_sha256': 'a' * 64, 'unit': None, 'phase': 'CAPTURED'}
        self.write(self.root / 'pending.json', {'schema': 1, 'phase': 'SCHEDULED', 'profiles': [row]})
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade, '_wait_dpkg', return_value=nullcontext()), \
             mock.patch.object(upgrade, '_resume_lock', return_value=nullcontext()), \
             mock.patch.object(upgrade, 'identify', return_value=row), \
             mock.patch.object(upgrade, '_owner', return_value={'ok': True, 'ready': True}) as owner, \
             mock.patch.object(upgrade, '_start_manual', return_value='framebridge-profile-' + 'a' * 20 + '.service'), \
             mock.patch.object(upgrade.subprocess, 'run') as run:
            self.assertEqual(upgrade.resume()['phase'], 'COMPLETE')
        self.assertEqual([c.args[1] for c in owner.call_args_list], ['user-profile', 'user-desktop', 'user-stop', 'user-ready'])
        run.assert_not_called()


    def test_snapshot_does_not_stop_the_process_running_apt(self):
        row = {'uid': os.getuid(), 'state': str(self.state), 'pid': 1234}
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade, 'identify', return_value=row), \
             mock.patch.object(upgrade.subprocess, 'run') as run:
            value = upgrade.snapshot(self.root)
        self.assertEqual(value['phase'], 'CAPTURED')
        run.assert_not_called()

    def test_reinstall_after_failure_preserves_receipt_and_keeps_stopped_profiles_stopped(self):
        failed = {'schema': 1, 'transaction': 'a' * 16, 'phase': 'FAILED',
                  'profiles': [{'state': str(self.state), 'phase': 'FAILED',
                                'error': 'UPGRADE_OWNER_OPERATION_FAILED:user-ready'}]}
        self.write(self.root / 'pending.json', failed)
        processes = self.root / 'no-running-managers'
        processes.mkdir()
        marker = self.state / 'manual-stop.json'
        marker.write_bytes(b'{"manual_stop": true}\n')
        before_marker = marker.read_bytes()
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade.subprocess, 'run') as run:
            value = upgrade.snapshot(processes)
            self.assertEqual(value['profiles'], [])
            self.assertEqual(upgrade.schedule()['phase'], 'COMPLETE')
        self.assertEqual(json.loads((self.root / 'previous.json').read_text()), failed)
        self.assertEqual(marker.read_bytes(), before_marker)
        run.assert_not_called()

    def test_reinstall_after_failure_captures_only_current_managers_without_replaying_old_intent(self):
        failed = {'schema': 1, 'phase': 'FAILED',
                  'profiles': [{'state': '/home/owner/previous-profile', 'phase': 'FAILED'}]}
        self.write(self.root / 'pending.json', failed)
        row = {'uid': 1000, 'state': str(self.state), 'pid': 1234, 'phase': 'CAPTURED'}
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade, 'identify', return_value=row), \
             mock.patch.object(upgrade.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            value = upgrade.snapshot(self.root)
            self.assertEqual(value['profiles'], [row])
            self.assertEqual(upgrade.schedule()['phase'], 'SCHEDULED')
        self.assertEqual(json.loads((self.root / 'previous.json').read_text()), failed)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-2:], [upgrade.MODULE, 'resume'])

    def test_reinstall_does_not_replace_a_snapshot_with_a_live_upgrade_worker(self):
        for phase in ('SCHEDULED', 'RUNNING'):
            with self.subTest(phase=phase):
                self.write(self.root / 'pending.json', {'schema': 1, 'phase': phase})
                before = (self.root / 'pending.json').read_bytes()
                with mock.patch.object(upgrade, 'ROOT', self.root), \
                     mock.patch.object(upgrade, '_root'), \
                     mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
                     mock.patch.object(upgrade, 'identify', return_value=None):
                    with self.assertRaisesRegex(RuntimeError, 'PREVIOUS_WORKER_PENDING'):
                        upgrade.snapshot(self.root)
                self.assertEqual((self.root / 'pending.json').read_bytes(), before)
                self.assertFalse((self.root / 'previous.json').exists())

    def test_optional_desktop_migration_failure_does_not_stop_runtime_recovery(self):
        row = {'uid': 1000, 'user': 'owner', 'home': '/home/owner',
               'state': str(self.state), 'pid': 1234, 'birth': '99',
               'config_sha256': 'a' * 64, 'unit': None, 'phase': 'CAPTURED'}
        self.write(self.root / 'pending.json', {'schema': 1, 'phase': 'SCHEDULED', 'profiles': [row]})
        def owner(profile, operation):
            if operation == 'user-desktop':
                raise RuntimeError('Legacy console is closed')
            return {'ok': True, 'ready': True}
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade, '_wait_dpkg', return_value=nullcontext()), \
             mock.patch.object(upgrade, '_resume_lock', return_value=nullcontext()), \
             mock.patch.object(upgrade, 'identify', return_value=row), \
             mock.patch.object(upgrade, '_owner', side_effect=owner), \
             mock.patch.object(upgrade, '_start_manual', return_value='framebridge-profile-' + 'a' * 20 + '.service'):
            value = upgrade.resume()
        self.assertEqual(value['phase'], 'COMPLETE')
        self.assertEqual(value['profiles'][0]['phase'], 'READY')
        self.assertFalse(value['profiles'][0]['desktop']['ok'])

    def check_stop_failure_retains_marker(self, before):
        if os.getuid() == 0:
            self.skipTest('Ordinary-owner fixture required')
        from framebridge_expanded import runtime, autostart
        directory = self.state / 'autostart'
        directory.mkdir(mode=0o700)
        marker = directory / 'manual-stop.json'
        if before is not None:
            marker.write_bytes(before)
            marker.chmod(0o600)
        def fail(state, **kwargs):
            marker.write_text('{"temporary":true}')
            marker.chmod(0o600)
            raise RuntimeError('simulated bounded stop failure')
        with mock.patch.object(runtime, 'stop', side_effect=fail), \
             mock.patch.object(upgrade, '_birth', return_value='99'), \
             mock.patch.object(upgrade, '_background', return_value=None), \
             mock.patch.object(runtime, 'start') as start, \
             mock.patch.object(autostart, '_configuration_action', side_effect=lambda state, action: action(state)):
            with self.assertRaisesRegex(RuntimeError, 'bounded stop failure'):
                upgrade.user_operation('user-stop', str(self.state),
                    hashlib.sha256((self.state / 'config.json').read_bytes()).hexdigest(),
                    manager_pid=1234, manager_birth='99')
        start.assert_not_called()
        if before is None:
            self.assertFalse(marker.exists())
        else:
            self.assertEqual(marker.read_bytes(), before)

    def test_stop_failure_retains_absent_manual_stop_policy(self):
        self.check_stop_failure_retains_marker(None)

    def test_stop_failure_retains_existing_manual_stop_bytes(self):
        self.check_stop_failure_retains_marker(b'{"manual_stop": true}\n')



    def test_user_unit_restart_rejects_account_reassignment(self):
        row = {'uid': 1000, 'user': 'owner', 'home': '/home/owner'}
        account = SimpleNamespace(pw_name='different', pw_dir='/home/different')
        with mock.patch.object(upgrade.pwd, 'getpwuid', return_value=account):
            with self.assertRaisesRegex(RuntimeError, 'ACCOUNT_CHANGED'):
                upgrade._unit(row)



    def test_manual_start_uses_an_independent_user_service_cgroup(self):
        row = {'uid': os.getuid(), 'user': 'owner', 'home': '/home/owner', 'state': str(self.state)}
        account = SimpleNamespace(pw_name='owner', pw_dir='/home/owner')
        with mock.patch.object(upgrade.pwd, 'getpwuid', return_value=account), \
             mock.patch.object(upgrade.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            if os.getuid() == 0:
                self.skipTest('Ordinary-owner fixture required')
            unit = upgrade._start_manual(row)
        args = run.call_args.args[0]
        self.assertIn('/usr/bin/systemd-run', args)
        self.assertIn('--property=ExitType=cgroup', args)
        self.assertIn('--property=KillMode=control-group', args)
        self.assertIn('--unit=' + unit, args)
        self.assertEqual(args[-5:], ['/usr/bin/fb', '--state-dir', str(self.state), '--json', 'start'])
        self.assertNotIn('enable', args)

    def test_background_identity_failure_is_refused_without_signaling(self):
        from framebridge_expanded import connection_background as background
        alive_pid = 54321
        original_exists = Path.exists
        with mock.patch.object(background, '_directory', return_value=self.state), \
             mock.patch.object(background, '_read', return_value={'pid': alive_pid}), \
             mock.patch.object(background, '_valid_identity', return_value=False), \
             mock.patch.object(Path, 'exists', autospec=True,
                 side_effect=lambda path: True if str(path) == '/proc/' + str(alive_pid) else original_exists(path)), \
             mock.patch.object(background, 'disconnect') as disconnect:
            with self.assertRaisesRegex(RuntimeError, 'BACKGROUND_IDENTITY_UNVERIFIED'):
                upgrade._background(self.state)
        disconnect.assert_not_called()

    def test_upgrade_migrates_one_reference_per_owner_and_marks_multiple_captures(self):
        rows = []
        for number in (1234, 1235):
            rows.append({'uid': 1000, 'user': 'owner', 'home': '/home/owner',
                         'state': str(self.state) + '-' + str(number),
                         'pid': number, 'birth': '99', 'config_sha256': 'a' * 64,
                         'unit': 'framebridge-autostart-' + format(number, '016x') + '.service',
                         'phase': 'CAPTURED'})
        self.write(self.root / 'pending.json', {'schema': 1, 'phase': 'SCHEDULED', 'profiles': rows})
        by_pid = {str(row['pid']): row for row in rows}
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade, '_wait_dpkg', return_value=nullcontext()), \
             mock.patch.object(upgrade, '_resume_lock', return_value=nullcontext()), \
             mock.patch.object(upgrade, 'identify', side_effect=lambda proc: by_pid[proc.name]), \
             mock.patch.object(upgrade, '_owner', return_value={'ok': True, 'ready': True}) as owner, \
             mock.patch.object(upgrade, '_unit', return_value=['/usr/bin/systemctl', '--user']), \
             mock.patch.object(upgrade.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            value = upgrade.resume()
        self.assertEqual(value['phase'], 'COMPLETE')
        selections = [call for call in owner.call_args_list if call.args[1] == 'user-profile']
        self.assertEqual(len(selections), 1)
        self.assertTrue(selections[0].args[0]['default_profile_ambiguous'])
        self.assertEqual([row['phase'] for row in value['profiles']], ['READY', 'READY'])

    def test_default_reference_failure_is_recorded_without_blocking_runtime_recovery(self):
        row = {'uid': 1000, 'user': 'owner', 'home': '/home/owner',
               'state': str(self.state), 'pid': 1234, 'birth': '99',
               'config_sha256': 'a' * 64, 'unit': None, 'phase': 'CAPTURED'}
        self.write(self.root / 'pending.json', {'schema': 1, 'phase': 'SCHEDULED', 'profiles': [row]})
        def owner(profile, operation):
            if operation == 'user-profile':
                raise RuntimeError('Default profile directory is unsafe')
            return {'ok': True, 'ready': True}
        with mock.patch.object(upgrade, 'ROOT', self.root), \
             mock.patch.object(upgrade, '_root'), \
             mock.patch.object(upgrade, '_read', side_effect=self.root_read), \
             mock.patch.object(upgrade, '_wait_dpkg', return_value=nullcontext()), \
             mock.patch.object(upgrade, '_resume_lock', return_value=nullcontext()), \
             mock.patch.object(upgrade, 'identify', return_value=row), \
             mock.patch.object(upgrade, '_owner', side_effect=owner), \
             mock.patch.object(upgrade, '_start_manual', return_value='framebridge-profile-' + 'a' * 20 + '.service'):
            value = upgrade.resume()
        self.assertEqual(value['phase'], 'COMPLETE')
        self.assertEqual(value['profiles'][0]['phase'], 'READY')
        self.assertFalse(value['profiles'][0]['default_profile']['ok'])

    def test_verified_background_connection_is_retained_without_reading_credentials(self):
        from framebridge_expanded import connection_background as background
        record = {'pid': 1234, 'start_ticks': 88}
        with mock.patch.object(background, '_directory', return_value=self.state), \
             mock.patch.object(background, '_read', return_value=record), \
             mock.patch.object(background, '_valid_identity', return_value=True), \
             mock.patch.object(background, 'status', return_value={'verified': True}):
            value = upgrade._background(self.state)
        self.assertTrue(value['preserved_in_memory'])
        self.assertFalse(value['process_reloaded'])
        self.assertEqual(value['start_ticks'], 88)

    def check_runtime_stop_connection(self, preserve):
        from framebridge_expanded import runtime, connection_background, tty, autostart
        with mock.patch.object(runtime, 'load_config', return_value=self.config), \
             mock.patch.object(runtime, '_read', return_value={}), \
             mock.patch.object(runtime, '_terminate'), \
             mock.patch.object(runtime, '_atomic'), \
             mock.patch.object(runtime, 'status', return_value={'running': False}), \
             mock.patch.object(tty, 'stop', return_value={'ok': True, 'cleanup_verified': True}), \
             mock.patch.object(autostart, 'mark_manual_stop'), \
             mock.patch.object(connection_background, 'disconnect', return_value={'ok': True}) as disconnect, \
             mock.patch.object(connection_background, 'status', return_value={'ok': True, 'alive': True}) as status:
            if preserve:
                runtime.stop(self.state, preserve_connection=True)
                disconnect.assert_not_called()
                status.assert_called_once_with(self.state)
            else:
                runtime.stop(self.state)
                disconnect.assert_called_once_with(self.state)
                status.assert_not_called()

    def test_normal_runtime_stop_still_disconnects(self):
        self.check_runtime_stop_connection(False)

    def test_upgrade_runtime_stop_retains_existing_connection(self):
        self.check_runtime_stop_connection(True)


if __name__ == '__main__':
    unittest.main()
