import io
import json
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from framebridge_expanded import connection_background as background
from framebridge_expanded import tunnel

ID = "tunnel_background_fixture"
MCP = "http://127.0.0.1:20865/mcp"
FAKE = "SYNTHETIC_BACKGROUND_ONLY_0123456789"
CLIENT = r'''
import datetime,http.server,json,os,sys,time
from pathlib import Path
started=datetime.datetime.now(datetime.timezone.utc).isoformat()
identifier=sys.argv[sys.argv.index('--control-plane.tunnel-id')+1]
mcp=sys.argv[sys.argv.index('--mcp.server-url')+1].removeprefix('url=')
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  if self.path=='/readyz': body=b'ready'
  elif self.path=='/healthz': body=b'live'
  elif self.path=='/api/status': body=json.dumps({'control_plane_tunnel_id':identifier,'mcp_server_url':mcp,'started_at':started,'tunnel_metadata':{'ID':identifier}}).encode()
  elif self.path=='/metrics': body=('commands_poll_last_successful_timestamp_seconds '+str(time.time())+'\n').encode()
  else: self.send_response(404);self.end_headers();return
  self.send_response(200);self.end_headers();self.wfile.write(body)
server=http.server.HTTPServer(('127.0.0.1',0),Handler)
Path(sys.argv[sys.argv.index('--health.url-file')+1]).write_text('http://127.0.0.1:'+str(server.server_port))
print('TECHNICAL_FIXTURE '+os.environ['FRAMEBRIDGE_EXPANDED_RUNTIME_KEY'],flush=True)
server.serve_forever()
'''


def command(code=CLIENT):
    return [sys.executable, "-u", "-c", code, "--control-plane.tunnel-id", ID,
            "--mcp.server-url", "url=" + MCP]


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fb-background-test-")
        self.state = Path(self.temp.name)

    def tearDown(self):
        background.disconnect(self.state)
        self.temp.cleanup()

    def start(self, wait=10):
        output = io.StringIO()
        rc = background.start(command(), FAKE, self.state, output=output, wait_timeout=wait)
        self.assertEqual(rc, 0, output.getvalue())
        return output.getvalue()

    def test_real_detached_worker_ready_private_logs_and_no_credential_on_disk(self):
        output = self.start()
        current = background.status(self.state)
        self.assertTrue(current["alive"])
        self.assertTrue(current["verified"])
        self.assertIn("terminal can be closed", output)
        self.assertNotIn(FAKE, output)
        self.assertNotIn("TECHNICAL_FIXTURE", output)
        worker = Path('/proc') / str(current['pid'])
        self.assertNotIn(FAKE.encode(), (worker / 'cmdline').read_bytes())
        self.assertNotIn(FAKE.encode(), (worker / 'environ').read_bytes())
        log = self.state / 'logs/chatgpt/connection.log'
        deadline = time.monotonic()+3
        while 'TECHNICAL_FIXTURE' not in log.read_text() and time.monotonic()<deadline:
            time.sleep(.05)
        self.assertIn('TECHNICAL_FIXTURE [REDACTED]', log.read_text())
        for path in self.state.rglob('*'):
            if path.is_file():
                self.assertNotIn(FAKE.encode(), path.read_bytes(), str(path))
        self.assertEqual((self.state / 'connection').stat().st_mode & 0o777, 0o700)
        for name in ('owner.lock','status.json'):
            self.assertEqual((self.state / 'connection' / name).stat().st_mode & 0o777, 0o600)
        stopped = background.disconnect(self.state)
        self.assertTrue(stopped['ok'])
        self.assertFalse(background.status(self.state)['alive'])
        self.assertTrue(background.disconnect(self.state)['ok'])

    def test_second_connect_reuses_owned_profile_without_duplicate_worker(self):
        self.start()
        pid = background.status(self.state)['pid']
        output = io.StringIO()
        self.assertEqual(background.start(command(), FAKE, self.state, output=output, wait_timeout=2),0)
        self.assertEqual(background.status(self.state)['pid'],pid)
        self.assertIn('already owns this profile',output.getvalue())

    def test_startup_failure_never_reports_ready_and_releases_lock(self):
        output = io.StringIO()
        rc = background.start(command('raise SystemExit(7)'), FAKE, self.state, output=output, wait_timeout=5)
        self.assertEqual(rc,1)
        self.assertNotIn('[OK]',output.getvalue())
        self.assertFalse(background.status(self.state)['alive'])
        self.start()

    def test_pending_is_explicit_and_keeps_background_worker(self):
        output=io.StringIO()
        rc=background.start(command('import time;time.sleep(60)'),FAKE,self.state,output=output,wait_timeout=.7)
        self.assertEqual(rc,1)
        self.assertIn('continues retrying in the background',output.getvalue())
        self.assertTrue(background.status(self.state)['alive'])
        self.assertFalse(background.status(self.state)['verified'])

    def test_closed_controlling_terminal_does_not_stop_verified_tunnel(self):
        master,slave=pty.openpty()
        root=Path(background.__file__).resolve().parents[1]
        code=("import os,fcntl,termios,sys,signal;os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0);"
              +"sys.path.insert(0,"+repr(str(root))+");from framebridge_expanded import connection_background as b;"
              +"rc=b.start("+repr(command())+","+repr(FAKE)+","+repr(str(self.state))+",wait_timeout=10);"
              +"signal.pause()")
        parent=subprocess.Popen([sys.executable,'-c',code],stdin=slave,stdout=slave,stderr=slave,close_fds=True)
        os.close(slave)
        try:
            deadline=time.monotonic()+12
            while time.monotonic()<deadline and not background.status(self.state)['verified']:
                self.assertIsNone(parent.poll())
                time.sleep(.1)
            self.assertTrue(background.status(self.state)['verified'])
            os.close(master);master=None
            parent.wait(timeout=3)
            time.sleep(.3)
            self.assertTrue(background.status(self.state)['verified'])
        finally:
            if master is not None: os.close(master)
            if parent.poll() is None: parent.terminate();parent.wait(timeout=3)

    def test_stale_heartbeat_is_not_verified_and_forged_message_is_not_printed(self):
        self.start()
        pid=background.status(self.state)['pid']
        os.kill(pid,signal.SIGSTOP)
        try:
            path=self.state/'connection/status.json'
            record=json.loads(path.read_text());record['verified_at']=time.time()-30
            record['message']='\x1b[31m'+FAKE
            background._write(path,record)
            if record.get('heartbeat_backend') == background._HEARTBEAT_BACKEND:
                heartbeat_path=background._heartbeat_directory(self.state)/'status.json'
                heartbeat=background._read(heartbeat_path)
                heartbeat['verified_at']=time.time()-30
                background._write(heartbeat_path,heartbeat)
            current=background.status(self.state)
            self.assertFalse(current['verified'])
            self.assertEqual(current['state'],'retrying')
            self.assertNotIn(FAKE,current['message'])
            self.assertNotIn('\x1b',current['message'])
        finally:
            os.kill(pid,signal.SIGCONT)

    def test_forged_identity_does_not_signal_unrelated_process(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            directory=background._directory(self.state)
            lock=background._checked_fd(directory/'owner.lock',os.O_RDWR,create=True)
            try:
                record=background._identity(child.pid,self.state,lock)
                record.update(state='ready',verified_at=time.time())
                background._write(directory/'status.json',record)
                with mock.patch.object(signal,'pidfd_send_signal') as send:
                    self.assertTrue(background.disconnect(self.state)['ok'])
                    send.assert_not_called()
                self.assertIsNone(child.poll())
                self.assertFalse(background.status(self.state)['verified'])
            finally: os.close(lock)
        finally:
            child.terminate();child.wait(timeout=3)

    def test_foreground_mode_cannot_overlap_owned_background_connection(self):
        self.start()
        with self.assertRaisesRegex(RuntimeError, 'already owns'):
            background.run_foreground(command(),FAKE,self.state,output=io.StringIO())
        self.assertTrue(background.status(self.state)['verified'])

    def test_ctrl_c_before_published_state_reaps_owned_supervisor(self):
        real_spawn=subprocess.Popen
        seen=[]
        def spawn(*args,**kwargs):
            child=real_spawn(*args,**kwargs)
            seen.append(child)
            return child
        real_sleep=time.sleep
        interrupted=[]
        def interrupt_once(seconds):
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return real_sleep(seconds)
        with mock.patch.object(background.subprocess,'Popen',side_effect=spawn), mock.patch.object(background.time,'sleep',side_effect=interrupt_once):
            rc=background.start(command(),FAKE,self.state,output=io.StringIO(),wait_timeout=5)
        self.assertEqual(rc,130)
        self.assertTrue(seen)
        self.assertIsNotNone(seen[0].poll())
        self.assertFalse(background.status(self.state)['alive'])

    def test_interactive_background_does_not_prompt_when_already_running(self):
        self.start()
        with mock.patch.object(tunnel,'_hidden') as prompt:
            self.assertEqual(tunnel.connect_chatgpt('/unused',self.state,MCP,output=io.StringIO()),0)
            prompt.assert_not_called()


if __name__=='__main__': unittest.main()
