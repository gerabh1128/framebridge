#!/usr/bin/env python3
"""Original FrameBridge real-screen RPC: actual pixels and bound visible input."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from .screen_capture import CaptureError, capture_screen, read_chunk
from .screen_control import ControlError, act, bind_screen, snapshot_binding

MAX_REQUEST = 16384
CAPTURE_ID = re.compile(r"[0-9a-f]{32}\Z")
HEX_SHA = re.compile(r"[0-9a-f]{64}\Z")


class ScreenRpcError(RuntimeError):
    def __init__(self, code: str, **details):
        self.code = code
        self.details = details
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise ScreenRpcError(code)


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def state_directory() -> Path:
    parent = Path('/run/user') / str(os.getuid())
    st = parent.lstat()
    require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077 == 0,
            'RUNTIME_DIRECTORY_UNTRUSTED')
    profile = Path(os.environ.get('FRAMEBRIDGE_EXPANDED_STATE', str(Path.home() / '.local/share/framebridge-expanded'))).expanduser().absolute()
    path = parent / ('framebridge-screen-' + hashlib.sha256(str(profile).encode()).hexdigest()[:16])
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = path.lstat()
    require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077 == 0,
            'SCREEN_DIRECTORY_UNTRUSTED')
    return path


def checked_read(path: Path, max_bytes=32768):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077 == 0,
                'SCREEN_RECORD_UNTRUSTED')
        require(st.st_size <= max_bytes, 'SCREEN_RECORD_TOO_LARGE')
        data = os.read(fd, max_bytes + 1)
        require(len(data) == st.st_size, 'SCREEN_RECORD_CHANGED')
        return json.loads(data)
    finally:
        os.close(fd)


def write_record(path: Path, value, *, replace=False):
    data = encode(value)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if not replace:
            require(not path.exists(), 'SCREEN_RECORD_EXISTS')
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def public_manifest(manifest):
    # Internal path is unnecessary to use or understand a captured screen.
    return {k: v for k, v in manifest.items() if k not in {'path', '_action_consumed'}}


def observe(directory: Path, requested_session_id=None):
    before = None
    before_error = None
    try:
        before = snapshot_binding()
    except ControlError as exc:
        before_error = exc.code
    manifest = capture_screen(directory)
    manifest['full_screen'] = True
    manifest['authentic_pixels'] = True
    manifest['mime'] = 'image/png'
    if manifest.get('screen_type') == 'tty' and before is not None:
        manifest = bind_screen(manifest)
        require(manifest.get('binding') == before, 'VISIBLE_TARGET_CHANGED_DURING_CAPTURE')
        manifest['control_available'] = True
    else:
        manifest['control_available'] = False
        manifest['control_error'] = before_error or 'USE_GRAPHICAL_CUA_FOR_INPUT'
    identity = manifest.get('binding') or {
        'node': manifest['node'], 'uid': manifest['uid'],
        'active_vt': manifest.get('active_vt'), 'session': manifest.get('session'),
        'screen_type': manifest.get('screen_type'),
    }
    manifest['session_id'] = 'screen_' + hashlib.sha256(encode(identity)).hexdigest()[:32]
    if requested_session_id:
        manifest['requested_session_id'] = requested_session_id
        manifest['requested_session_visible'] = requested_session_id == manifest['session_id']
    manifest['_action_consumed'] = False
    write_record(directory / (manifest['capture_id'] + '.bound.json'), manifest)
    return manifest


def load_capture(directory, capture_id):
    require(type(capture_id) is str and CAPTURE_ID.fullmatch(capture_id), 'CAPTURE_ID_INVALID')
    result = checked_read(directory / (capture_id + '.bound.json'))
    require(type(result) is dict, 'CAPTURE_RECORD_INVALID')
    require(result.get('capture_id') == capture_id, 'CAPTURE_RECORD_MISMATCH')
    return result


def validate_request(request):
    require(type(request) is dict, 'REQUEST_INVALID')
    op = request.get('op')
    allowed = {
        'observe': {'op', 'session_id'},
        'status': {'op'},
        'read_chunk': {'op', 'capture_id', 'index', 'expected_sha256'},
        'read_capture': {'op', 'capture_id'},
        'key': {'op', 'capture_id', 'session_id', 'key'},
        'text': {'op', 'capture_id', 'session_id', 'text'},
    }
    require(type(op) is str and op in allowed, 'OPERATION_INVALID')
    require(not set(request).difference(allowed[op]), 'UNKNOWN_REQUEST_FIELDS')
    if 'session_id' in request:
        sid = request['session_id']
        require(type(sid) is str and len(sid) <= 96 and re.fullmatch(r'[A-Za-z0-9_-]+', sid), 'SESSION_ID_INVALID')
    if op == 'read_chunk':
        require(type(request.get('index')) is int and request['index'] >= 0, 'CHUNK_INDEX_INVALID')
        require(type(request.get('expected_sha256')) is str and HEX_SHA.fullmatch(request['expected_sha256']), 'EXPECTED_SHA256_REQUIRED')
    if op == 'key':
        require(type(request.get('key')) is str and 0 < len(request['key']) <= 32, 'KEY_INVALID')
    if op == 'text':
        require(type(request.get('text')) is str and 0 < len(request['text'].encode('utf-8')) <= 4096, 'TEXT_INVALID')
        require('\x00' not in request['text'], 'TEXT_INVALID')
    return op


def dispatch(request, directory=None):
    op = validate_request(request)
    directory = directory or state_directory()
    if op == 'status':
        return {'ok': True, 'result': {'node': os.uname().nodename, 'uid': os.getuid(),
                    'full_screen': True, 'authentic_pixels': True,
                    'input_binding': 'foreground-visible-tmux-pane'}}
    if op == 'observe':
        return {'ok': True, 'result': public_manifest(observe(directory, request.get('session_id')))}
    manifest = load_capture(directory, request.get('capture_id'))
    if op == 'read_capture':
        # Verify the existing artifact without observing or consuming input control.
        read_chunk(directory, manifest['capture_id'], 0, expected_sha256=manifest['sha256'])
        historical = public_manifest(manifest)
        historical.update(historical_capture=True, control_available=False,
                          control_error='HISTORICAL_CAPTURE_OBSERVE_AGAIN_FOR_INPUT')
        return {'ok': True, 'result': historical}
    if op == 'read_chunk':
        require(manifest['sha256'] == request['expected_sha256'], 'CAPTURE_SHA256_MISMATCH')
        chunk = read_chunk(directory, manifest['capture_id'], request['index'],
                           chunk_bytes=2100, expected_sha256=request['expected_sha256'])
        return {'ok': True, 'result': chunk}
    if manifest.get('screen_type') == 'x11':
        raise ScreenRpcError('USE_GRAPHICAL_CUA_FOR_X11_INPUT')
    require(manifest.get('control_available') is True, 'VISIBLE_INPUT_UNAVAILABLE')
    require(not manifest.get('_action_consumed'), 'CAPTURE_ALREADY_USED_OBSERVE_AGAIN')
    if request.get('session_id'):
        require(request['session_id'] == manifest['session_id'], 'SESSION_NOT_VISIBLE')
    # Read and verify the immutable PNG before relying on its binding.
    read_chunk(directory, manifest['capture_id'], 0, expected_sha256=manifest['sha256'])
    # Mark the observation used before input: never replay after an uncertain send.
    manifest['_action_consumed'] = True
    write_record(directory / (manifest['capture_id'] + '.bound.json'), manifest, replace=True)
    try:
        result = act(manifest, op, text=request.get('text'), key=request.get('key'))
    except ControlError as exc:
        raise ScreenRpcError(exc.code, capture_id=manifest['capture_id'],
                             capture_consumed=True, observe_again=True,
                             performed=exc.details.get('performed')) from exc
    time.sleep(0.08)
    try:
        after = observe(directory)
    except (CaptureError, ControlError, ScreenRpcError, OSError) as exc:
        raise ScreenRpcError('INPUT_SENT_OBSERVE_FAILED', performed=True,
                             capture_id=manifest['capture_id'], observe_again=True,
                             cause=getattr(exc, 'code', 'SCREEN_IO_FAILED')) from exc
    summary = {k: v for k, v in result.items() if k in {'ok', 'performed', 'action', 'input_bytes', 'key', 'input_target'}}
    after['action'] = {'kind': op, 'previous_capture_id': manifest['capture_id'], 'result': summary}
    return {'ok': True, 'result': public_manifest(after)}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        require(len(argv) == 1 and len(argv[0]) <= MAX_REQUEST * 2, 'ENCODED_REQUEST_INVALID')
        raw = base64.b64decode(argv[0], validate=True)
        require(len(raw) <= MAX_REQUEST, 'REQUEST_TOO_LARGE')
        request = json.loads(raw)
        directory = state_directory()
        lock_fd = os.open(directory / '.lock', os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(lock_fd)
            require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077 == 0, 'SCREEN_LOCK_UNTRUSTED')
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            reply = dispatch(request, directory)
        finally:
            os.close(lock_fd)
        print(encode(reply).decode('ascii'))
        return 0
    except (ScreenRpcError, CaptureError, ControlError) as exc:
        details = {k: v for k, v in getattr(exc, 'details', {}).items()
                   if k in {'performed', 'capture_id', 'capture_consumed', 'observe_again', 'cause'}}
        print(encode({'ok': False, 'error': {'code': exc.code, **details}}).decode('ascii'))
        return 1
    except (ValueError, OSError, KeyError, TypeError):
        print(encode({'ok': False, 'error': {'code': 'SCREEN_REQUEST_FAILED'}}).decode('ascii'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
