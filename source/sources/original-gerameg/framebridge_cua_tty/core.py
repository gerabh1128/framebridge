"""Unprivileged local PTY session engine with mandatory owner gate for inputs.

Staging only: only fixed, no-side-effects demo program may be opened. Never
use an LLM-provided shell/argv or `authorized=True` as authority.
"""
from __future__ import annotations
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import pty
import secrets
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from typing import Callable
from .screen import Screen


class TerminalDenied(PermissionError):
    pass


KEYS = {'UP': b'\x1b[A', 'DOWN': b'\x1b[B', 'LEFT': b'\x1b[D',
        'RIGHT': b'\x1b[C', 'ENTER': b'\r', 'TAB': b'\t', 'ESC': b'\x1b',
        'BACKSPACE': b'\x7f', 'CTRL_C': b'\x03'}


@dataclass
class Session:
    session_id: str
    proc: subprocess.Popen
    fd: int
    screen: Screen
    born: float
    last_access: float
    program: str
    sequence: int = 0


class Manager:
    """A trusted OwnerDevice verifier must implement callable(context)->bool.

    IMPORTANT: no grants can be passed in via RPC, environment, or `authorized`.
    An absent verifier rejects ALL input. A production verifier must validate
    signed challenge, owner identity, exact payload hash, nonce, TTL and one-use.
    """
    def __init__(self, owner_verifier: Callable | None = None, max_sessions: int = 2,
                 idle_timeout_s: int = 600):
        if not 1 <= max_sessions <= 4: raise ValueError('MAX_SESSIONS_INVALID')
        self._gate = owner_verifier
        self._sessions = {}
        self._lock = threading.RLock()
        self.max_sessions, self.idle_timeout_s = max_sessions, idle_timeout_s

    def _get(self, sid):
        s = self._sessions.get(sid)
        if not s: raise KeyError('SESSION_NOT_FOUND')
        if time.monotonic() - s.last_access > self.idle_timeout_s:
            self._terminate(s)
            del self._sessions[sid]
            raise TerminalDenied('SESSION_EXPIRED')
        s.last_access = time.monotonic()
        return s

    @staticmethod
    def _terminate(s):
        if s.proc.poll() is None:
            try: os.killpg(s.proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: s.proc.wait(timeout=0.7)
            except subprocess.TimeoutExpired:
                try: os.killpg(s.proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: s.proc.wait(timeout=1)
                except subprocess.TimeoutExpired: pass
        try: os.close(s.fd)
        except OSError: pass

    @staticmethod
    def _child_ctty():
        # CUA does not use setuid. Child has same UID as daemon.
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    def open(self, program='demo-readonly', rows=24, cols=80):
        if program != 'demo-readonly':
            raise TerminalDenied('PROGRAM_NOT_ALLOWLISTED')
        if os.geteuid() == 0:
            raise TerminalDenied('ROOT_DAEMON_FORBIDDEN')
        with self._lock:
            self.reap()
            if len(self._sessions) >= self.max_sessions:
                raise TerminalDenied('SESSION_LIMIT')
            screen = Screen(rows, cols)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            env = {'PATH': '/usr/bin:/bin', 'TERM': 'xterm', 'LANG': 'C.UTF-8',
                   'HOME': str(Path.home())}
            try:
                proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('demo.py'))],
                        stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
                        start_new_session=True, preexec_fn=self._child_ctty)
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
            os.set_blocking(master, False)
            sid = secrets.token_hex(16)
            now = time.monotonic()
            self._sessions[sid] = Session(sid, proc, master, screen, now, now, program)
            return {'session_id': sid, 'program': program, 'state': 'OPEN'}

    def _drain(self, s):
        total = 0
        while total < 65536:
            ready, _, _ = select.select([s.fd], [], [], 0)
            if not ready: break
            try: data = os.read(s.fd, min(4096, 65536-total))
            except (BlockingIOError, OSError): break
            if not data: break
            total += len(data)
            s.screen.feed(data)
        return total

    def observe(self, sid):
        with self._lock:
            s = self._get(sid)
            n = self._drain(s)
            s.sequence += 1
            return {'session_id': sid, 'program': s.program, 'running': s.proc.poll() is None,
                    'sequence': s.sequence, 'drained_bytes': n, 'screen': s.screen.snapshot()}

    def status(self):
        with self._lock:
            self.reap()
            return {'active_sessions': len(self._sessions), 'max_sessions': self.max_sessions,
                    'owner_device_connected': self._gate is not None,
                    'input_enabled': False if self._gate is None else 'per-call-only'}

    def _authorize(self, s, operation, payload: bytes, approved_context=None):
        if self._gate is None:
            raise TerminalDenied('OWNER_DEVICE_NOT_CONNECTED')
        if approved_context is None:
            raise TerminalDenied('OWNER_APPROVAL_REQUIRED')
        context = {'session_id': s.session_id, 'program': s.program, 'operation': operation,
                   'payload_sha256': hashlib.sha256(payload).hexdigest(),
                   'owner_assertion': approved_context}
        if self._gate(context) is not True:
            raise TerminalDenied('OWNER_APPROVAL_INVALID_OR_REPLAYED')

    def key(self, sid, key, approved_context=None):
        if key not in KEYS:
            raise TerminalDenied('KEY_NOT_ALLOWLISTED')
        with self._lock:
            s = self._get(sid)
            data = KEYS[key]
            self._authorize(s, 'key.send', data, approved_context)
            if s.proc.poll() is not None: raise TerminalDenied('SESSION_CLOSED')
            os.write(s.fd, data)
            return {'session_id': sid, 'submitted': key, 'state': 'SENT'}

    def text(self, sid, text, approved_context=None):
        if not isinstance(text, str) or not 1 <= len(text) <= 64 or any(ord(c)<32 or ord(c)==127 for c in text):
            raise TerminalDenied('TEXT_NOT_ALLOWED')
        with self._lock:
            s = self._get(sid)
            data = text.encode('utf-8')
            self._authorize(s, 'text.type', data, approved_context)
            if s.proc.poll() is not None: raise TerminalDenied('SESSION_CLOSED')
            os.write(s.fd, data)
            return {'session_id': sid, 'submitted_chars': len(text), 'state': 'SENT'}

    def resize(self, sid, rows, cols):
        with self._lock:
            s = self._get(sid)
            s.screen.resize(rows, cols)
            fcntl.ioctl(s.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            return {'session_id': sid, 'rows': rows, 'cols': cols}

    def abort(self, sid):
        # Emergency stop always works, but only against its own demo session.
        with self._lock:
            s = self._get(sid)
            self._terminate(s)
            del self._sessions[sid]
            return {'session_id': sid, 'state': 'ABORTED'}

    def reap(self):
        for sid, s in list(self._sessions.items()):
            if time.monotonic() - s.last_access > self.idle_timeout_s:
                self._terminate(s)
                self._sessions.pop(sid, None)

    def shutdown(self):
        with self._lock:
            for s in self._sessions.values(): self._terminate(s)
            self._sessions.clear()
