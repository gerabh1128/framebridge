"""Resume only previously running installed profiles after an APT upgrade.

The pre-install snapshot never stops the process carrying APT. The configure
hook schedules an isolated system service; it waits for dpkg to release its
lock before restarting the captured profiles as their original Linux account.
No credentials, boot policy or unrelated services are changed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path('/var/lib/framebridge-expanded/package-upgrade')
MODULE = '/usr/lib/framebridge-expanded/expanded/framebridge_expanded/package_upgrade.py'
PREFIX = '/usr/lib/framebridge-expanded'
PYTHONS = {'/usr/bin/python3', '/usr/bin/python3.12', '/usr/bin/python3.13', '/usr/bin/python3.14'}
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
UNIT = re.compile(r'framebridge-(?:autostart-[0-9a-f]{16}|profile-[0-9a-f]{20}|qa-[A-Za-z0-9-]{1,100})\.service\Z')
MAXIMUM = 2 * 1024 * 1024


def _need(value, message):
    if not value:
        raise RuntimeError(message)


def _read(path, uid, maximum=MAXIMUM):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == uid
              and info.st_nlink == 1 and not info.st_mode & 0o077
              and info.st_size <= maximum, 'UPGRADE_FILE_UNTRUSTED')
        data = os.read(fd, maximum + 1)
        _need(len(data) <= maximum, 'UPGRADE_FILE_TOO_LARGE')
        return data
    finally:
        os.close(fd)


def _private(path, uid):
    info = path.lstat()
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
          and stat.S_IMODE(info.st_mode) == 0o700, 'UPGRADE_DIRECTORY_UNTRUSTED')


def _root():
    _need(os.getuid() == 0 and os.geteuid() == 0, 'UPGRADE_ROOT_REQUIRED')
    for parent in [Path('/var/lib'), Path('/var/lib/framebridge-expanded')]:
        if not parent.exists():
            parent.mkdir(mode=0o700)
        info = parent.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
              and not info.st_mode & 0o022, 'UPGRADE_ROOT_PARENT_UNTRUSTED')
    ROOT.mkdir(mode=0o700, exist_ok=True)
    _private(ROOT, 0)


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix='.upgrade-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((json.dumps(value, indent=2, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _birth(proc):
    fields = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()
    return fields[19] if fields[0] not in {'Z', 'X'} else None


def identify(proc):
    args = [part.decode() for part in (proc / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')]
    if (len(args) != 6 or args[0] not in PYTHONS
            or args[1:5] != ['-m', 'framebridge_expanded.runtime', '_manager', '--state']):
        return None
    env = dict(x.split(b'=', 1) for x in (proc / 'environ').read_bytes().split(b'\0') if b'=' in x)
    if (env.get(b'FRAMEBRIDGE_EXPANDED_PREFIX') != PREFIX.encode()
            or env.get(b'FRAMEBRIDGE_EXPANDED_STATE') != args[5].encode()
            or env.get(b'PYTHONPATH', b'').split(b':')[0] != (PREFIX + '/expanded').encode()):
        return None
    uid = proc.stat().st_uid
    _need(uid != 0, 'UPGRADE_ROOT_PROFILE_REFUSED')
    state = Path(args[5])
    _need(state.is_absolute() and str(state) == os.path.normpath(str(state))
          and not any(ord(c) < 32 for c in str(state)), 'UPGRADE_PROFILE_PATH_INVALID')
    for parent in [state, *state.parents]:
        _need(not parent.is_symlink(), 'UPGRADE_PROFILE_SYMLINK_REFUSED')
    _private(state, uid)
    birth = _birth(proc)
    _need(birth is not None, 'UPGRADE_MANAGER_NOT_ALIVE')
    record = json.loads(_read(state / 'manager.json', uid))
    _need(record.get('pid') == int(proc.name) and record.get('birth') == birth
          and record.get('uid') == uid, 'UPGRADE_MANAGER_RECORD_MISMATCH')
    account = pwd.getpwuid(uid)
    config = json.loads(_read(state / 'config.json', uid))
    _need(config.get('role') in {'node', 'central', 'standalone'}, 'UPGRADE_CONFIG_INVALID')
    group = (proc / 'cgroup').read_text().splitlines()
    units = [part for line in group for part in line.split('/') if UNIT.fullmatch(part)]
    _need(len(set(units)) <= 1, 'UPGRADE_MANAGER_UNIT_AMBIGUOUS')
    return {'uid': uid, 'user': account.pw_name, 'home': account.pw_dir,
            'state': str(state), 'pid': int(proc.name), 'birth': birth,
            'config_sha256': hashlib.sha256(_read(state / 'config.json', uid)).hexdigest(),
            'node_id': config.get('node_id'), 'unit': next(iter(set(units)), None),
            'phase': 'CAPTURED'}


def snapshot(proc_root=Path('/proc')):
    _root()
    rows = []
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            row = identify(proc)
        except (FileNotFoundError, ProcessLookupError, PermissionError, UnicodeError, IndexError):
            continue
        if row:
            rows.append(row)
    keys = [(r['uid'], r['state']) for r in rows]
    _need(len(keys) == len(set(keys)), 'UPGRADE_DUPLICATE_PROFILE_MANAGERS')
    value = {'schema': 1, 'phase': 'CAPTURED', 'profiles': rows,
             'transaction': os.urandom(8).hex(),
             'captured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
             'boot_policy_changed': False, 'credentials_changed': False}
    pending = ROOT / 'pending.json'
    if pending.exists() or pending.is_symlink():
        previous = json.loads(_read(pending, 0))
        _need(previous.get('phase') not in {'SCHEDULED', 'RUNNING'}, 'UPGRADE_PREVIOUS_WORKER_PENDING')
        _write(ROOT / 'previous.json', previous)
    _write(pending, value)
    return value


def schedule():
    _root()
    pending = ROOT / 'pending.json'
    if not pending.exists():
        return {'phase': 'NO_UPGRADE_SNAPSHOT'}
    value = json.loads(_read(pending, 0))
    if value.get('phase') in {'COMPLETE', 'SCHEDULED', 'RUNNING'}:
        return value
    _need(value.get('phase') == 'CAPTURED', 'UPGRADE_REVIEW_PREVIOUS_FAILURE')
    if not value['profiles']:
        value['phase'] = 'COMPLETE'
        _write(pending, value)
        return value
    transaction = value.get('transaction', '')
    _need(re.fullmatch(r'[a-f0-9]{16}', transaction), 'UPGRADE_TRANSACTION_INVALID')
    value['phase'] = 'SCHEDULED'
    _write(pending, value)
    try:
        result = subprocess.run([
            '/usr/bin/systemd-run', '--quiet', '--collect',
            '--unit=framebridge-package-upgrade-' + transaction, '--on-active=2s',
            '--timer-property=RemainAfterElapse=no',
            '--property=Type=oneshot', '--property=TimeoutStartSec=600',
            '/usr/bin/python3', '-I', '-S', MODULE, 'resume'],
            env=ENV, capture_output=True, timeout=15)
        _need(result.returncode == 0, 'UPGRADE_WORKER_SCHEDULE_FAILED')
    except BaseException:
        value['phase'] = 'CAPTURED'
        _write(pending, value)
        raise
    return value


@contextmanager
def _wait_dpkg(timeout=180):
    """Hold APT/dpkg locks throughout bounded runtime replacement."""
    deadline = time.monotonic() + timeout
    descriptors = []
    held = []
    try:
        for name in ('lock-frontend', 'lock'):
            descriptors.append(os.open('/var/lib/dpkg/' + name, os.O_RDWR | os.O_NOFOLLOW))
        while True:
            try:
                for fd in descriptors:
                    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(fd)
                break
            except BlockingIOError:
                for fd in held:
                    fcntl.lockf(fd, fcntl.LOCK_UN)
                held = []
                _need(time.monotonic() < deadline, 'UPGRADE_DPKG_LOCK_TIMEOUT')
                time.sleep(0.25)
        yield
    finally:
        for fd in held:
            fcntl.lockf(fd, fcntl.LOCK_UN)
        for fd in descriptors:
            os.close(fd)


def _owner(row, operation):
    account = pwd.getpwuid(row['uid'])
    _need(row['uid'] != 0 and account.pw_name == row['user']
          and account.pw_dir == row['home'], 'UPGRADE_ACCOUNT_CHANGED')
    args = ['/usr/sbin/runuser', '-u', account.pw_name, '--', '/usr/bin/env', '-i',
            'HOME=' + account.pw_dir, 'USER=' + account.pw_name, 'LOGNAME=' + account.pw_name,
            'PATH=/usr/bin:/bin', 'LANG=C.UTF-8', 'LC_ALL=C.UTF-8',
            'XDG_RUNTIME_DIR=/run/user/' + str(row['uid']),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(row['uid']) + '/bus',
            '/usr/bin/python3', '-I', '-S', MODULE, operation,
            '--state', row['state'], '--config-sha256', row['config_sha256'],
            '--manager-pid', str(row['pid']), '--manager-birth', row['birth']]
    background = row.get('background')
    if background:
        args += ['--connection-pid', str(background['pid']),
                 '--connection-birth', str(background['start_ticks'])]
    if operation == 'user-profile' and row.get('default_profile_ambiguous'):
        args += ['--ambiguous-profiles']
    result = subprocess.run(args, env=ENV, capture_output=True, text=True, timeout=120)
    _need(result.returncode == 0, 'UPGRADE_OWNER_OPERATION_FAILED:' + operation)
    value = json.loads(result.stdout)
    _need(value.get('ok') is True, 'UPGRADE_OWNER_NOT_READY:' + operation)
    return value


def _start_manual(row):
    unit = 'framebridge-profile-' + hashlib.sha256(row['state'].encode()).hexdigest()[:20] + '.service'
    base = _unit(row)[:-2]  # runuser/env for the owner's actual user bus
    result = subprocess.run([*base, '/usr/bin/systemd-run', '--user', '--quiet', '--collect',
        '--unit=' + unit, '--property=Type=exec', '--property=ExitType=cgroup',
        '--property=KillMode=control-group',
        '--setenv=FRAMEBRIDGE_EXPANDED_PREFIX=' + PREFIX,
        '--setenv=FRAMEBRIDGE_EXPANDED_STATE=' + row['state'],
        '--setenv=HOME=' + row['home'], '--setenv=PATH=/usr/bin:/bin', '/usr/bin/fb', '--state-dir', row['state'], '--json', 'start'],
        env=ENV, capture_output=True, timeout=20)
    _need(result.returncode == 0, 'UPGRADE_NATIVE_USER_UNIT_START_FAILED')
    return unit


def _unit(row):
    account = pwd.getpwuid(row['uid'])
    _need(row['uid'] != 0 and account.pw_name == row['user']
          and account.pw_dir == row['home'], 'UPGRADE_ACCOUNT_CHANGED')
    return ['/usr/sbin/runuser', '-u', account.pw_name, '--', '/usr/bin/env', '-i',
            'HOME=' + account.pw_dir, 'PATH=/usr/bin:/bin',
            'XDG_RUNTIME_DIR=/run/user/' + str(row['uid']),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(row['uid']) + '/bus',
            '/usr/bin/systemctl', '--user']


def _resume():
    _root()
    pending = ROOT / 'pending.json'
    value = json.loads(_read(pending, 0))
    _need(value.get('schema') == 1 and value.get('phase') == 'SCHEDULED', 'UPGRADE_INTENT_INVALID')
    value['phase'] = 'RUNNING'
    _write(pending, value)
    failures = []
    captured_by_uid = {}
    for captured in value['profiles']:
        captured_by_uid.setdefault(captured['uid'], set()).add(captured['state'])
    default_reviewed = set()
    for row in value['profiles']:
        try:
            current = identify(Path('/proc') / str(row['pid']))
            _need(current and all(current[k] == row[k] for k in
                  ('uid', 'user', 'home', 'state', 'pid', 'birth', 'config_sha256', 'unit')),
                  'UPGRADE_MANAGER_IDENTITY_CHANGED')
            # Only profiles captured from a verified installed manager are
            # eligible. Never scan homes or pick the first of several paths.
            if row['uid'] not in default_reviewed:
                row['default_profile_ambiguous'] = len(captured_by_uid[row['uid']]) != 1
                try:
                    row['default_profile'] = _owner(row, 'user-profile')
                except Exception as profile_error:
                    row['default_profile'] = {'ok': False, 'warning': str(profile_error)[:160]}
                default_reviewed.add(row['uid'])
            # Migration runs as the profile owner and only recognizes the old
            # FrameBridge desktop entry; no third-party launcher is overwritten.
            try:
                row['desktop'] = _owner(row, 'user-desktop')
            except Exception as desktop_error:
                row['desktop'] = {'ok': False, 'warning': str(desktop_error)[:160]}
            if row['unit']:
                current = identify(Path('/proc') / str(row['pid']))
                _need(current and all(current[k] == row[k] for k in
                      ('uid', 'state', 'pid', 'birth', 'config_sha256', 'unit')),
                      'UPGRADE_MANAGER_IDENTITY_CHANGED')
                _need(UNIT.fullmatch(row['unit']), 'UPGRADE_UNIT_INVALID')
                result = subprocess.run([*_unit(row), 'restart', row['unit']],
                                        env=ENV, capture_output=True, timeout=100)
                _need(result.returncode == 0, 'UPGRADE_UNIT_RESTART_FAILED')
                row['runtime'] = _owner(row, 'user-ready')
            else:
                stopped = _owner(row, 'user-stop')
                row['background'] = stopped.get('background')
                row['unit'] = _start_manual(row)
                row['runtime'] = _owner(row, 'user-ready')
            row['phase'] = 'READY'
        except Exception as error:
            row['phase'] = 'FAILED'
            row['error'] = str(error)[:160]
            failures.append(row['state'])
        _write(pending, value)
    value['phase'] = 'FAILED' if failures else 'COMPLETE'
    value['completed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    _write(pending, value)
    _need(not failures, 'UPGRADE_PROFILE_RESTART_FAILED: inspect /var/lib/framebridge-expanded/package-upgrade/pending.json')
    return value


@contextmanager
def _resume_lock():
    fd = os.open(ROOT / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == 0
              and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
              'UPGRADE_LOCK_UNTRUSTED')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def resume():
    _root()
    with _resume_lock(), _wait_dpkg():
        return _resume()


def _background(state):
    from framebridge_expanded import connection_background as background
    directory = background._directory(state, False)
    record = background._read(directory / 'status.json') if directory else {}
    if not record:
        return None
    valid = background._valid_identity(record, state)
    pid = record.get('pid')
    if not valid:
        _need(type(pid) is not int or not Path('/proc', str(pid)).exists(),
              'UPGRADE_BACKGROUND_IDENTITY_UNVERIFIED')
        return None
    return {'pid': pid, 'start_ticks': record['start_ticks'],
            'verified': background.status(state)['verified'],
            'preserved_in_memory': True, 'process_reloaded': False}


def user_operation(operation, state, config_sha256, *, manager_pid=None, manager_birth=None,
                   connection_pid=None, connection_birth=None, ambiguous_profiles=False):
    _need(os.getuid() != 0 and os.getuid() == os.geteuid(), 'UPGRADE_OWNER_REQUIRED')
    state = Path(state)
    _private(state, os.getuid())
    data = _read(state / 'config.json', os.getuid())
    _need(hashlib.sha256(data).hexdigest() == config_sha256, 'UPGRADE_CONFIG_CHANGED')
    sys.path.insert(0, PREFIX + '/expanded')
    if operation == 'user-profile':
        from framebridge_expanded.profiles import migrate
        return migrate(state, ambiguous=ambiguous_profiles)
    if operation == 'user-desktop':
        from framebridge_expanded.console_desktop import migrate_legacy
        return migrate_legacy(state)
    from framebridge_expanded import runtime
    if operation == 'user-stop':
        from framebridge_expanded import autostart

        def restart(value):
            # runtime.stop writes a manual-stop marker. Retain its former bytes
            # even when stopping fails; upgrading must not change boot intent.
            marker = state / 'autostart/manual-stop.json'
            before = _read(marker, os.getuid()) if os.path.lexists(marker) else None
            manager = json.loads(_read(state / 'manager.json', os.getuid()))
            _need(manager.get('pid') == manager_pid and manager.get('birth') == manager_birth
                  and _birth(Path('/proc') / str(manager_pid)) == manager_birth,
                  'UPGRADE_MANAGER_IDENTITY_CHANGED')
            background = _background(state)
            try:
                current = runtime.stop(state, preserve_connection=True)
                deadline = time.monotonic() + 20
                while current.get('running') or any(x.get('alive') for x in current.get('processes', [])):
                    _need(time.monotonic() < deadline, 'UPGRADE_OLD_RUNTIME_STOP_TIMEOUT')
                    time.sleep(0.1)
                    current = runtime.status(state)
            finally:
                if before is None:
                    marker.unlink(missing_ok=True)
                else:
                    fd, temporary = tempfile.mkstemp(prefix='.upgrade-stop-', dir=marker.parent)
                    with os.fdopen(fd, 'wb') as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(before)
                    os.replace(temporary, marker)
            return {'ok': True, 'stopped': True, 'background': background,
                    'boot_policy_changed': False}

        return autostart._configuration_action(state, restart)
    else:
        deadline = time.monotonic() + 60
        while True:
            result = runtime.status(state)
            manager = json.loads(_read(state / 'manager.json', os.getuid()))
            if (result.get('running') and result.get('ready')
                    and (manager.get('pid'), manager.get('birth')) != (manager_pid, manager_birth)):
                break
            _need(time.monotonic() < deadline, 'UPGRADE_NEW_RUNTIME_NOT_READY')
            time.sleep(0.25)
    _need(result.get('running') is True and result.get('ready') is True, 'UPGRADE_NEW_RUNTIME_NOT_READY')
    background = _background(state)
    if connection_pid:
        _need(background and background['pid'] == connection_pid
              and background['start_ticks'] == connection_birth, 'UPGRADE_BACKGROUND_IDENTITY_CHANGED')
    return {'ok': True, 'ready': True, 'running': True, 'background': background,
            'manager': json.loads(_read(state / 'manager.json', os.getuid())),
            'boot_policy_changed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['snapshot', 'schedule', 'resume',
                        'user-stop', 'user-ready', 'user-desktop', 'user-profile'])
    parser.add_argument('--state')
    parser.add_argument('--config-sha256')
    parser.add_argument('--manager-pid', type=int)
    parser.add_argument('--manager-birth')
    parser.add_argument('--connection-pid', type=int)
    parser.add_argument('--connection-birth', type=int)
    parser.add_argument('--ambiguous-profiles', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.operation.startswith('user-'):
            value = user_operation(args.operation, args.state, args.config_sha256,
                manager_pid=args.manager_pid, manager_birth=args.manager_birth,
                connection_pid=args.connection_pid, connection_birth=args.connection_birth,
                ambiguous_profiles=args.ambiguous_profiles)
        else:
            value = globals()[args.operation]()
        print(json.dumps(value))
        return 0
    except Exception as error:
        print('FrameBridge package upgrade: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

