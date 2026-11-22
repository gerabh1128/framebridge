"""Local-only JSON-lines Unix socket prototype. No TCP listener, no shell."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import struct
from .core import Manager, TerminalDenied

MAX_REQUEST = 65536


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        # On Linux: SO_PEERCRED is the connecting process's kernel UID.
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.server.owner_uid:
                return
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
                self.send({'ok': False, 'error': 'REQUEST_TOO_LARGE_OR_TRUNCATED'})
                return
            req = json.loads(raw)
            if type(req) is not dict or type(req.get('op')) is not str:
                self.send({'ok': False, 'error': 'INVALID_REQUEST'})
                return
            # Only specific RPC shapes. The caller cannot supply binary argv,
            # host path, a shell, an approval bool, or an OwnerDevice verifier.
            op = req['op']
            # Enforce exact wire shape for every operation. Untrusted clients
            # may not attach arbitrary authority or additional arguments.
            permitted = {
                'status': ({'op'},),
                'open': ({'op'}, {'op','program'}),
                'observe': ({'op','session_id'},),
                'resize': ({'op','session_id','rows','cols'},),
                'abort': ({'op','session_id'},),
                'key.send': ({'op','session_id','key','owner_assertion'},),
                'text.type': ({'op','session_id','text','owner_assertion'},),
            }
            accepted = permitted.get(op)
            if accepted is None:
                raise TerminalDenied('UNKNOWN_OPERATION')
            if set(req) not in accepted:
                raise TerminalDenied('REQUEST_INVALID')
            m = self.server.manager
            if op == 'status': data = m.status()
            elif op == 'open': data = m.open(req.get('program', 'demo-readonly'))
            elif op == 'observe': data = m.observe(req['session_id'])
            elif op == 'resize': data = m.resize(req['session_id'], req['rows'], req['cols'])
            elif op == 'abort': data = m.abort(req['session_id'])
            elif op in ('key.send', 'text.type'):
                # Server default remains Manager(owner_verifier=None) -> DENY.
                # A separately deployed trusted bootstrap must bind a real
                # WebAuthn/Owner verifier. Neither AUTHORIZED nor an RPC bool
                # can alter the verifier. Never accept arbitrary shell/argv.
                if m._gate is None:
                    raise TerminalDenied('OWNER_DEVICE_NOT_CONNECTED')
                if type(req.get('owner_assertion')) is not dict:
                    raise TerminalDenied('OWNER_APPROVAL_REQUIRED')
                required = ({'op', 'session_id', 'key', 'owner_assertion'}
                            if op == 'key.send' else
                            {'op', 'session_id', 'text', 'owner_assertion'})
                if set(req) != required:
                    raise TerminalDenied('REQUEST_INVALID')
                if op == 'key.send':
                    data = m.key(req['session_id'], req['key'],
                                 approved_context=req['owner_assertion'])
                else:
                    data = m.text(req['session_id'], req['text'],
                                  approved_context=req['owner_assertion'])
            else: raise TerminalDenied('UNKNOWN_OPERATION')
            self.send({'ok': True, 'result': data})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self.send({'ok': False, 'error': 'REQUEST_INVALID'})
        except TerminalDenied as exc:
            self.send({'ok': False, 'error': str(exc)})
        except Exception:
            # Never echo arbitrary error content, terminal output or secrets.
            self.send({'ok': False, 'error': 'INTERNAL_ERROR'})

    def send(self, data):
        self.wfile.write((json.dumps(data, ensure_ascii=True, separators=(',', ':')) + '\n').encode())


class LocalServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    def __init__(self, path, manager=None):
        if os.geteuid() == 0:
            raise TerminalDenied('ROOT_SERVER_FORBIDDEN')
        p = Path(path).absolute()
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not operate in a shared or symlinked parent directory.
        if p.parent.is_symlink() or stat.S_IMODE(p.parent.stat().st_mode) != 0o700:
            raise TerminalDenied('SOCKET_PARENT_NOT_PRIVATE')
        if p.exists() or p.is_symlink():
            raise TerminalDenied('SOCKET_ALREADY_EXISTS')
        self.owner_uid = os.geteuid()
        self.manager = manager or Manager()
        self._socket_path = p
        old = os.umask(0o077)
        try: super().__init__(str(p), Handler)
        finally: os.umask(old)
        os.chmod(p, 0o600)

    def server_close(self):
        self.manager.shutdown()
        super().server_close()
        try: self._socket_path.unlink(missing_ok=True)
        except OSError: pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='FrameBridge CUA-TTY staging demo, local socket only')
    parser.add_argument('--socket', default=str(Path.home()/'.local/share/framebridge-cua-tty-r1/agent.sock'))
    args = parser.parse_args(argv)
    with LocalServer(args.socket) as server:
        print('CUA_TTY_R1_LOCAL_ONLY_SOCKET_READY', flush=True)
        try: server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt: pass


if __name__ == '__main__':
    main()
