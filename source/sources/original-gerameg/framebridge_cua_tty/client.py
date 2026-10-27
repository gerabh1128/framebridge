"""Local staging inspection client; input operations cannot bypass OwnerDevice."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import socket


def rpc(op, *, path, **kwargs):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(4)
        sock.connect(str(path))
        sock.sendall((json.dumps({'op':op, **kwargs})+'\n').encode())
        reply = bytearray()
        while len(reply) < 65536:
            chunk = sock.recv(min(8192, 65536-len(reply)))
            if not chunk: break
            reply.extend(chunk)
            if b'\n' in chunk: break
        if len(reply) >= 65536 or b'\n' not in reply: raise RuntimeError('RESPONSE_TOO_LARGE')
        return json.loads(reply.split(b'\n',1)[0])


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('op', choices=('status','open','observe','resize','abort','key.send','text.type'))
    p.add_argument('--session-id',default='')
    p.add_argument('--socket',default=str(Path.home()/'.local/share/framebridge-cua-tty-r1/agent.sock'))
    p.add_argument('--key',default='')
    p.add_argument('--text',default='')
    args=p.parse_args(argv)
    req={}
    if args.session_id: req['session_id']=args.session_id
    if args.key: req['key']=args.key
    if args.text: req['text']=args.text
    print(json.dumps(rpc(args.op,path=args.socket,**req),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
