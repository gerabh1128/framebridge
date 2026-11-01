#!/usr/bin/env python3
"""Isolated viewer launch tests; no desktop, jobs, or real user state. Set FBTERM_TEST_REAL_BUS=1 for temporary real UNIX sockets."""
from pathlib import Path
import ast
import hashlib
import importlib.util
import json
import os
import socket
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch,Mock

ENGINE_SOURCE=Path(__file__).resolve().parents[1]/'sources/original-asus/fbterm/bin/fbterm_engine.py'
spec=importlib.util.spec_from_file_location('viewer_engine_release',ENGINE_SOURCE)
engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)

def semantic_ast(node):
    """Serialize code semantics without CPython dump-format/optional-field noise."""
    if isinstance(node, ast.AST):
        # Python 3.13 ast.dump omits optional empty fields by default. Normalize
        # those fields ourselves, including an attribute absent on one ABI.
        fields = []
        for name in sorted(node._fields):
            value = getattr(node, name, None)
            if value is None or isinstance(value, list) and not value:
                continue
            fields.append([name, semantic_ast(value)])
        return [type(node).__name__, fields]
    if isinstance(node, list):
        # None placeholders inside a nonempty list (e.g. kw_defaults) matter.
        return [semantic_ast(value) for value in node]
    if isinstance(node, bytes):
        return ["bytes", node.hex()]
    if isinstance(node, complex):
        return ["complex", repr(node)]
    return node


def semantic_ast_digest(node):
    data = json.dumps(semantic_ast(node), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


class ViewerEnvironment(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='fbterm-viewer-test-')
        self.base=Path(self.tmp.name);self.uid=os.getuid()
        self.runtime=self.base/'run-user'/str(self.uid)
        self.runtime.mkdir(mode=0o700,parents=True)
        self.addCleanup(self.tmp.cleanup)
        if os.environ.get('FBTERM_TEST_REAL_BUS')=='1':
            self.bus=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            self.bus.bind(str(self.runtime/'bus'))
            self.addCleanup(self.bus.close)
        else:
            # This executor forbids socket(2). Simulate only the socket inode's
            # file type; paths, ownership, modes and symlink tests stay real.
            bus_path=self.runtime/'bus';bus_path.write_bytes(b'fake bus metadata')
            held=bus_path.open('rb');self.addCleanup(held.close)
            bus_inode=bus_path.lstat().st_ino;real_lstat=Path.lstat
            def socket_metadata(path,*args,**kwargs):
                info=real_lstat(path,*args,**kwargs)
                if info.st_ino==bus_inode and not stat.S_ISLNK(info.st_mode):
                    return types.SimpleNamespace(st_mode=stat.S_IFSOCK|0o666,st_uid=info.st_uid)
                return info
            socket_patch=patch.object(Path,'lstat',autospec=True,side_effect=socket_metadata)
            socket_patch.start();self.addCleanup(socket_patch.stop)
        self.original_env={
            'PATH':'/usr/bin:/bin','HOME':'/isolated/home',
            'XDG_RUNTIME_DIR':'/isolated/runtime',
            'DBUS_SESSION_BUS_ADDRESS':'unix:path=/private/framebridge-bus',
            'TMUX_TMPDIR':'/private/tmux','FBTERM_ROOT':'/private/fbterm',
            'FBTERM_SESSION':'framebridge-original-fbterm','FBTERM_TITLE':'FrameBridge Expanded',
            'FRAMEBRIDGE_EXPANDED_STATE':'/private/profile',
            'FRAMEBRIDGE_DISPLAY_BINDING':json.dumps({'kind':'text','selection':'auto'}),
        }
        self.environment=patch.dict(os.environ,self.original_env,clear=True)
        self.environment.start();self.addCleanup(self.environment.stop)
        self.context={'kind':'physical-x11','physical':True,'uid':self.uid,'display':':0','selection':'auto'}
        self.display=types.ModuleType('framebridge_expanded.display_context')
        self.display.DisplayContextError=type('DisplayContextError',(RuntimeError,),{})
        def prepare(env):
            env.update(DISPLAY=':0',XAUTHORITY='/validated/.Xauthority')
            env['FRAMEBRIDGE_DISPLAY_BINDING']=json.dumps(self.context)
        self.display.prepare=Mock(side_effect=prepare)
        self.display.check=Mock(side_effect=lambda env:dict(self.context))
        package=types.ModuleType('framebridge_expanded');package.display_context=self.display
        self.modules=patch.dict(sys.modules,{'framebridge_expanded':package,'framebridge_expanded.display_context':self.display})
        self.modules.start();self.addCleanup(self.modules.stop)
        def mapped(value):
            return self.base/'run-user' if str(value)=='/run/user' else Path(value)
        self.pathpatch=patch.object(engine,'Path',side_effect=mapped)
        self.pathpatch.start();self.addCleanup(self.pathpatch.stop)
        self.term=engine.FBTerm(self.base/'fbterm','framebridge-original-fbterm','FrameBridge Expanded')

    def test_physical_discovery_recovers_desktop_and_uses_real_bus(self):
        env=self.term._viewer_environment()
        self.assertEqual(env['DISPLAY'],':0');self.assertEqual(env['XAUTHORITY'],'/validated/.Xauthority')
        self.assertEqual(env['HOME'],engine.pwd.getpwuid(self.uid).pw_dir)
        self.assertEqual(env['XDG_RUNTIME_DIR'],str(self.runtime))
        self.assertEqual(env['DBUS_SESSION_BUS_ADDRESS'],'unix:path='+str(self.runtime/'bus'))
        self.display.prepare.assert_called_once();self.display.check.assert_called_once()

    def test_discovery_never_mutates_process_or_job_environment(self):
        before=dict(os.environ);env=self.term._viewer_environment()
        self.assertEqual(dict(os.environ),before)
        self.assertIsNot(env,os.environ)
        for key in ('TMUX_TMPDIR','FBTERM_ROOT','FBTERM_SESSION','FBTERM_TITLE','FRAMEBRIDGE_EXPANDED_STATE'):
            self.assertEqual(env[key],before[key])
        self.assertEqual(os.environ['DBUS_SESSION_BUS_ADDRESS'],'unix:path=/private/framebridge-bus')

    def test_headless_session_keeps_text_operation(self):
        self.display.check.side_effect=self.display.DisplayContextError('CUA_DISPLAY_UNAVAILABLE')
        self.assertIsNone(self.term._viewer_environment())
        with patch.object(engine.subprocess,'Popen') as popen,patch.object(self.term,'has_session',return_value=True),patch.object(self.term,'status',return_value={'session':True,'visible':False}):
            result=self.term.ensure()
        self.assertTrue(result['session']);self.assertFalse(result['visible']);popen.assert_not_called()

    def test_explicit_virtual_desktop_preserves_existing_environment(self):
        self.context={'kind':'virtual-x11','explicit':True,'display':':140'}
        self.display.prepare.side_effect=None
        os.environ['DISPLAY']=':140'
        os.environ['FRAMEBRIDGE_DISPLAY_BINDING']=json.dumps({'kind':'virtual-x11','selection':'yes','explicit':True})
        before=dict(os.environ)
        self.assertEqual(self.term._viewer_environment(),before)

    def test_unrequested_virtual_desktop_is_rejected(self):
        self.context={'kind':'virtual-x11','explicit':False,'display':':140'}
        self.assertIsNone(self.term._viewer_environment())

    def test_foreign_session_uid_is_rejected(self):
        self.context['uid']=self.uid+1
        self.assertIsNone(self.term._viewer_environment())

    def test_boolean_uid_is_rejected(self):
        self.context['uid']=False
        self.assertIsNone(self.term._viewer_environment())

    def test_missing_runtime_is_rejected(self):
        actual=self.runtime.with_name(str(self.uid)+'-old');self.runtime.rename(actual)
        self.assertIsNone(self.term._viewer_environment())

    def test_runtime_with_group_access_is_rejected_without_permission_changes(self):
        self.runtime.chmod(0o750)
        self.assertIsNone(self.term._viewer_environment())
        self.assertEqual(stat.S_IMODE(self.runtime.lstat().st_mode),0o750)

    def test_runtime_symlink_is_rejected(self):
        actual=self.runtime.with_name(str(self.uid)+'-actual');self.runtime.rename(actual);self.runtime.symlink_to(actual,target_is_directory=True)
        self.assertIsNone(self.term._viewer_environment())

    def test_foreign_runtime_owner_is_rejected(self):
        real=Path.lstat
        def fake(path,*args,**kwargs):
            result=real(path,*args,**kwargs)
            if path==self.runtime:return types.SimpleNamespace(st_mode=result.st_mode,st_uid=self.uid+1)
            return result
        with patch.object(Path,'lstat',new=fake):self.assertIsNone(self.term._viewer_environment())

    def test_missing_bus_is_rejected(self):
        (self.runtime/'bus').unlink()
        self.assertIsNone(self.term._viewer_environment())

    def test_bus_regular_file_is_rejected(self):
        (self.runtime/'bus').unlink();(self.runtime/'bus').write_text('not a bus')
        self.assertIsNone(self.term._viewer_environment())

    def test_bus_symlink_is_rejected(self):
        bus=self.runtime/'bus';real=self.runtime/'real-bus';bus.rename(real);bus.symlink_to(real)
        self.assertIsNone(self.term._viewer_environment())

    def test_foreign_bus_owner_is_rejected(self):
        real=Path.lstat
        def fake(path,*args,**kwargs):
            result=real(path,*args,**kwargs)
            if path==self.runtime/'bus':return types.SimpleNamespace(st_mode=result.st_mode,st_uid=self.uid+1)
            return result
        with patch.object(Path,'lstat',new=fake):self.assertIsNone(self.term._viewer_environment())

    def test_visibility_check_uses_same_validated_environment(self):
        env=self.term._viewer_environment()
        with patch.object(self.term,'_run',return_value=types.SimpleNamespace(returncode=0,stdout='0x123 FrameBridge Expanded\n')) as run:
            self.assertTrue(self.term.visible_window(env))
        run.assert_called_once_with(['wmctrl','-l'],env=env)

    def test_visible_flag_false_does_not_attempt_graphical_discovery(self):
        with patch.object(self.term,'has_session',return_value=True),patch.object(self.term,'status',return_value={'visible':False}),patch.object(self.term,'_viewer_environment') as discovery:
            self.term.ensure(visible=False)
        discovery.assert_not_called()

    def test_ensure_launches_only_readonly_viewer_with_copy_environment(self):
        env=self.term._viewer_environment();before=dict(os.environ)
        with patch.object(self.term,'has_session',return_value=True),patch.object(self.term,'_viewer_environment',return_value=env),patch.object(self.term,'visible_window',side_effect=[False,True]) as visible,patch.object(self.term,'status',return_value={'visible':True,'session':True}),patch.object(engine.subprocess,'Popen') as popen:
            self.assertTrue(self.term.ensure()['visible'])
        args,kw=popen.call_args
        self.assertEqual(args[0][0],'gnome-terminal')
        self.assertIn('tmux attach-session -r -t',args[0][-1])
        self.assertIn(self.term.session,args[0][-1]);self.assertIs(kw['env'],env)
        self.assertEqual(visible.call_args_list[0].args,(env,));self.assertEqual(visible.call_args_list[1].args,(env,))
        self.assertEqual(dict(os.environ),before)

    def test_viewer_launch_failure_does_not_destroy_or_fake_session(self):
        env=self.term._viewer_environment()
        with patch.object(self.term,'has_session',return_value=True),patch.object(self.term,'_viewer_environment',return_value=env),patch.object(self.term,'visible_window',return_value=False),patch.object(self.term,'status',return_value={'session':True,'visible':False}),patch.object(engine.subprocess,'Popen',side_effect=OSError('launch failed')):
            result=self.term.ensure()
        self.assertTrue(result['session']);self.assertFalse(result['visible'])

    def test_wmctrl_failure_does_not_fake_visibility(self):
        env=self.term._viewer_environment()
        with patch.object(self.term,'_run',side_effect=OSError('wmctrl failed')):self.assertFalse(self.term.visible_window(env))

    def test_semantic_ast_is_independent_of_optional_empty_fields_and_locations(self):
        first = ast.parse("def example(value):\n    return value\n")
        second = ast.parse("\n\ndef example(value):\n    return value\n")
        function = second.body[0]
        for name in ("returns", "type_comment", "type_params", "decorator_list"):
            if hasattr(function, name):
                delattr(function, name)
        self.assertEqual(semantic_ast_digest(first), semantic_ast_digest(second))
        changed = ast.parse("def example(value):\n    return value + 1\n")
        self.assertNotEqual(semantic_ast_digest(first), semantic_ast_digest(changed))
        # Preserve argument-default placeholders and actual literal changes.
        required = ast.parse("def example(*, value): pass")
        default_none = ast.parse("def example(*, value=None): pass")
        self.assertNotEqual(semantic_ast_digest(required), semantic_ast_digest(default_none))
        self.assertNotEqual(semantic_ast_digest(ast.parse("None")),
                            semantic_ast_digest(ast.parse("False")))

    def test_viewer_patch_preserves_preexisting_job_and_shell_methods(self):
        # Fingerprints of semantic 0.3.4 ASTs bind this viewer-only change to
        # existing execution semantics without shipping a duplicate engine.
        # Our serializer, rather than ast.dump's version-dependent defaults,
        # handles the two supported CPython ABIs (3.12 and 3.13).
        expected = {'__init__': 'ec60f92a1d737961d23e1250bd04df0683978db55385a019fdbe67391a76cb89', '_run': '1200a06bb7c164d34464fe884aca06d16d257d780f37124ea4a3a2492a7f63ea', 'has_session': 'd938d8aac50a140a2838b1223ab14ff8e6a69847a7c1b1d1346b2aedb0dfde48', 'status': 'a4f3185627fbc1fb36174d055dd4421254776ebcdc73a087fb4124e086bcafa6', 'execute': '4a562e7a0cb75d3c7a84b5945044b63df16296da6e766f24c0430bde2684313c'}
        tree = ast.parse(ENGINE_SOURCE.read_text())
        klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'FBTerm')
        methods = {n.name: n for n in klass.body if isinstance(n, ast.FunctionDef)}
        for name, expected_hash in expected.items():
            self.assertEqual(semantic_ast_digest(methods[name]), expected_hash, name)
        # Creation/initialization of tmux and Bash precedes the viewer branch.
        self.assertEqual(semantic_ast_digest(methods['ensure'].body[2]), '0aecdc60e1a1647fa9ce02ff56d21c3fe082148227029112d259d99de42ef150')

if __name__ == '__main__':
    unittest.main(verbosity=2)
