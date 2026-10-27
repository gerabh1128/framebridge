"""Actual HTTP MCP startup is independent of ambient WebSocket libraries."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from framebridge_expanded import __version__

SOURCE = Path(__file__).resolve().parents[1]


class HttpMcpTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='fb-http-transport-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.ambient = self.directory/'ambient'
        package = self.ambient/'websockets'
        package.mkdir(parents=True)
        self.marker = self.directory/'unwanted-websocket-import'
        (package/'__init__.py').write_text(
            "import os\nfrom pathlib import Path\n"
            "Path(os.environ['FB_TEST_WS_MARKER']).write_text('imported')\n"
            "raise RuntimeError('INCOMPATIBLE_AMBIENT_WEBSOCKETS')\n")
        home = self.directory/'home'
        home.mkdir(mode=0o700)
        self.env = dict(os.environ, HOME=str(home), PYTHONNOUSERSITE='1',
                        PYTHONDONTWRITEBYTECODE='1',
                        FB_TEST_WS_MARKER=str(self.marker),
                        FRAMEBRIDGE_EXPANDED_STATE=str(home/'state'),
                        FRAMEBRIDGE_STATE_DIR=str(home/'state'),
                        FRAMEBRIDGE_TOOL_ROOT=str(home/'adapter'),
                        FRAMEBRIDGE_BROKER_SOURCE=str(SOURCE/'sources/original-geranas/FrameBridge-Broker-RC1/src'),
                        FRAMEBRIDGE_FLEET_SOURCE=str(SOURCE/'sources/original-geranas/FrameBridge-Fleet-Hub/app'))
        # Match the child search path exported by runtime.environment(), with an
        # intentionally incompatible optional library taking precedence.
        self.env['PYTHONPATH'] = os.pathsep.join(map(str, [
            self.ambient, SOURCE/'expanded', SOURCE/'portable',
            SOURCE/'sources/original-geranas/FrameBridge-Fleet-Hub/vendor']))

    def test_default_websocket_detection_would_load_the_incompatible_library(self):
        code = "from mcp.server.fastmcp import FastMCP; import uvicorn; uvicorn.Config(FastMCP('negative-control').streamable_http_app()).load()"
        result = subprocess.run([sys.executable, '-c', code], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('INCOMPATIBLE_AMBIENT_WEBSOCKETS', result.stderr)
        self.assertTrue(self.marker.exists())

    def start(self, name):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            self.port = reservation.getsockname()[1]
        if name == 'fleet':
            command = [sys.executable, '-m', 'framebridge_expanded.fleet']
            self.env['FRAMEBRIDGE_FLEET_HUB_PORT'] = str(self.port)
        else:
            command = [sys.executable, str(SOURCE/'portable/front_door_mcp.py')]
            self.env['FRAMEBRIDGE_FRONT_DOOR_PORT'] = str(self.port)
        log = (self.directory/(name+'.log')).open('w')
        self.process = subprocess.Popen(command, env=self.env, cwd=self.directory,
                                        stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=log, start_new_session=True)
        log.close()
        self.addCleanup(self.stop)
        self.session = None
        self.protocol = None
        deadline = time.monotonic()+12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail('HTTP component exited: '+(self.directory/(name+'.log')).read_text()[-2000:])
            try:
                reply, headers, _ = self.rpc({'jsonrpc':'2.0', 'id':1,
                    'method':'initialize', 'params':{'protocolVersion':'2025-11-25',
                    'capabilities':{}, 'clientInfo':{'name':'framebridge-http-qa','version':'1'}}})
                break
            except urllib.error.URLError:
                time.sleep(.05)
        else:
            self.fail('HTTP component did not become ready')
        self.assertIn('result', reply)
        self.assertIn('serverInfo', reply['result'])
        self.assertTrue(headers.get('Content-Type', '').startswith('text/event-stream'))
        self.session = headers.get('Mcp-Session-Id')
        self.assertTrue(self.session)
        self.protocol = reply['result']['protocolVersion']
        _, _, code = self.rpc({'jsonrpc':'2.0', 'method':'notifications/initialized'})
        self.assertEqual(202, code)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def rpc(self, value):
        headers = {'Content-Type':'application/json',
                   'Accept':'application/json, text/event-stream'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        if self.protocol:
            headers['Mcp-Protocol-Version'] = self.protocol
        request = urllib.request.Request('http://127.0.0.1:'+str(self.port)+'/mcp',
                    data=json.dumps(value).encode(), headers=headers, method='POST')
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read().decode()
            if raw and response.headers.get('Content-Type','').startswith('text/event-stream'):
                raw = next(line[5:].strip() for line in raw.splitlines() if line.startswith('data:'))
            return json.loads(raw) if raw else None, response.headers, response.status

    def test_fleet_initializes_and_lists_tools_without_websocket_import(self):
        self.start('fleet')
        result, _, status = self.rpc({'jsonrpc':'2.0','id':2,'method':'tools/list'})
        self.assertEqual(200, status)
        self.assertEqual(6, len(result['result']['tools']))
        self.assertFalse(self.marker.exists())

    def test_actual_frontdoor_help_and_execution_denial_over_streamable_http(self):
        self.start('frontdoor')
        result, _, status = self.rpc({'jsonrpc':'2.0','id':2,'method':'tools/list'})
        self.assertEqual(200, status)
        names = {tool['name'] for tool in result['result']['tools']}
        self.assertTrue({'framebridge_help','framebridge_exec'}.issubset(names))
        reply, _, _ = self.rpc({'jsonrpc':'2.0','id':3,'method':'tools/call',
                    'params':{'name':'framebridge_help','arguments':{'topic':'start'}}})
        self.assertIn(__version__, json.dumps(reply['result']))
        denied, _, _ = self.rpc({'jsonrpc':'2.0','id':4,'method':'tools/call',
                    'params':{'name':'framebridge_exec',
                              'arguments':{'script':'true','authorized':False}}})
        self.assertIn('AUTHORIZED_TRUE_REQUIRED', json.dumps(denied['result']))
        self.assertFalse((Path(self.env['FRAMEBRIDGE_TOOL_ROOT'])/'adapter.sock').exists())
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
