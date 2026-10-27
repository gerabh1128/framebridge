"""Pure guide/catalog checks; no server, connection or node is started."""
import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from framebridge_expanded import __version__
from framebridge_expanded import guide

SOURCE=Path(__file__).resolve().parents[1]
FRONT=SOURCE/'portable/front_door_mcp.py'

class FakeMCP:
    def __init__(self,name,**kwargs):
        self.name=name;self.options=kwargs
        self._mcp_server=SimpleNamespace(version=None)
        self._tool_manager=SimpleNamespace(_tools={})
        self.annotations={}
    def tool(self,**kwargs):
        def register(fn):
            self._tool_manager._tools[fn.__name__]=fn
            self.annotations[fn.__name__]=kwargs.get('annotations')
            return fn
        return register

def guide_surface():
    tree=ast.parse(FRONT.read_text())
    names={'_registered_operations','framebridge_help','framebridge_status'}
    keep=[]
    for node in tree.body:
        if isinstance(node,ast.Assign) and (
            any(isinstance(t,ast.Name) and t.id=='mcp' for t in node.targets)
            or any(isinstance(t,ast.Attribute) and t.attr=='version' for t in node.targets)):
            keep.append(copy.deepcopy(node))
        elif isinstance(node,ast.FunctionDef) and node.name in names:
            keep.append(copy.deepcopy(node))
    ns={'FastMCP':FakeMCP,'ToolAnnotations':lambda **kw:kw,'PRODUCT_VERSION':__version__,
        'GUIDE_SCHEMA':guide.GUIDE_SCHEMA,'get_guide':guide.get_guide,
        'socket':SimpleNamespace(gethostname=lambda:'fixture-receiver'),'HOST':'127.0.0.1','PORT':20865,
        'ADAPTER_SOCKET':SimpleNamespace(is_socket=lambda:False)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=keep,type_ignores=[])),str(FRONT),'exec'),ns)
    return ns

class GuideTests(unittest.TestCase):
    def test_guide_versions_topics_and_bounds(self):
        self.assertEqual(len(guide.topics()),10)
        for topic in (*guide.topics(),'all'):
            value=guide.get_guide(topic)
            self.assertTrue(value['ok'])
            self.assertEqual(value['version'],__version__)
            self.assertTrue(value['read_only'])
            self.assertFalse(value['capabilities_verified'])
            self.assertLess(len(json.dumps(value).encode()),65536)
            self.assertRegex(value['guide_sha256'],r'^[a-f0-9]{64}$')
    def test_guide_reads_no_profile_or_network(self):
        with mock.patch('builtins.open',side_effect=AssertionError('no file reads')), \
             mock.patch('subprocess.run',side_effect=AssertionError('no processes')), \
             mock.patch('socket.socket',side_effect=AssertionError('no sockets')):
            self.assertTrue(guide.get_guide('start')['ok'])
    def test_invalid_topic_is_bounded_actionable(self):
        self.assertEqual(guide.get_guide('missing')['error'],'HELP_TOPIC_UNKNOWN')
        self.assertEqual(guide.get_guide('x'*65)['error'],'HELP_TOPIC_INVALID')
        with self.assertRaises(ValueError):guide.render_text('missing')
    def test_guides_have_no_fixed_owner_paths_or_account(self):
        text=json.dumps(guide.export_guide())
        for marker in ['/home/gerabh','/home/gera/','gerabh1128@','GERAMEG','ASUS','GERANAS']:
            self.assertNotIn(marker,text)
    def test_human_agent_and_transcript_boundary_are_explicit(self):
        text=guide.render_text('all')
        self.assertIn('human operators and authorized agents',text)
        self.assertIn('terminal transcript',text)
        self.assertIn('not an authentic full-screen image',text)
        self.assertIn('normal operating-system authentication',text)
        self.assertIn('does not restrict GNU GPL freedoms',text)
    def test_export_matches_installed_web_inputs(self):
        data=json.loads((SOURCE/'docs/COMMAND-GUIDE.json').read_text())
        self.assertEqual(data,guide.export_guide())
        self.assertIn('FrameBridge '+__version__,(SOURCE/'docs/framebridge.1').read_text())
        self.assertNotIn('Reconnecting requires entering the runtime key again',
                         (SOURCE/'docs/framebridge.1').read_text())
    def test_development_guidance_preserves_license_freedom(self):
        text=guide.render_text('security')
        self.assertIn('separate source clone',text)
        self.assertIn('do not present a modified build as an official release',text)
        self.assertIn('GNU GPLv3-only',text)

class MCPGuideSurfaceTests(unittest.TestCase):
    def test_init_invites_learning_and_has_product_version(self):
        ns=guide_surface();mcp=ns['mcp']
        self.assertIn("framebridge_help(topic='start')",mcp.options['instructions'])
        self.assertEqual(mcp._mcp_server.version,__version__)
        self.assertEqual(mcp.name,'FrameBridge')
    def test_help_is_registered_readonly_and_bounded(self):
        ns=guide_surface();value=ns['framebridge_help']('start')
        self.assertTrue(value['ok']);self.assertEqual(value['receiver'],'fixture-receiver')
        self.assertIn('framebridge_help',value['registered_operations'])
        self.assertTrue(ns['mcp'].annotations['framebridge_help']['readOnlyHint'])
        self.assertNotIn('adapter_socket',value)
    def test_status_query_success_is_not_adapter_readiness(self):
        ns=guide_surface();value=ns['framebridge_status']()
        self.assertTrue(value['query_success'])
        self.assertFalse(value['adapter_socket_present'])
        self.assertEqual(value['adapter_ready'],'not_checked')
        self.assertEqual(value['version'],__version__)
        self.assertNotIn('adapter_socket',value)
    def test_historical_lab_tools_are_not_publicly_decorated(self):
        tree=ast.parse(FRONT.read_text())
        byname={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
        for name in ['framebridge_exec_geranas','framebridge_cloud_dsm']:
            self.assertFalse(byname[name].decorator_list)
        self.assertTrue(byname['framebridge_exec'].decorator_list)
        self.assertTrue(byname['framebridge_help'].decorator_list)

if __name__=='__main__':unittest.main()
