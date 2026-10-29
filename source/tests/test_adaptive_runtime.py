"""Adaptive text/graphical selection; inert CUA stubs never send user input."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from framebridge_expanded import adapter
from framebridge_expanded import config
from framebridge_expanded import display_context as dc
from framebridge_expanded import runtime


def physical(**changes):
    data=dict(kind='physical-x11',physical=True,session_id='fixture-session',uid=os.getuid(),
              leader=123,active_vt='tty7',seat='seat0',vtnr=7,display=':0',authority='/private/fixture.Xauthority')
    data.update(changes)
    return data


def capture_output(valid=True):
    if not valid: return 'VISUAL_CAPTURE_GATE=PASS\nMIME=image/jpeg\nBASE64=invalid'
    pixels=b'\xff\xd8synthetic-unit-fixture\xff\xd9'
    return '\n'.join(['RESULTADO_FINAL=CUA_VISUAL_OBSERVE_PASS','VISUAL_CAPTURE_GATE=PASS','MIME=image/jpeg','BYTES='+str(len(pixels)),
                     'SHA256='+hashlib.sha256(pixels).hexdigest(),'BASE64='+base64.b64encode(pixels).decode()])


class AdaptiveDisplayTests(unittest.TestCase):
    def test_auto_missing_graphical_tools_selects_text_and_clears_inherited_display(self):
        env={'DISPLAY':':99','XAUTHORITY':'/unrelated'}
        with patch.object(dc,'_physical',return_value=physical()), patch.object(dc,'missing_graphical_commands',return_value=['xdotool']):
            dc.configure(env,'auto','/profile')
        self.assertEqual(dc.describe(env)['kind'],'text')
        self.assertNotIn('DISPLAY',env)
        self.assertNotIn('XAUTHORITY',env)
        self.assertFalse(dc.private_display_required(env,'auto',False))

    def test_text_to_graphical_and_back_preserves_profile_and_requires_new_observation(self):
        env={};dc.bind_text(env,'/profile')
        with patch.object(dc,'_physical',return_value=physical()),patch.object(dc,'missing_graphical_commands',return_value=[]):
            dc.prepare(env)
            self.assertEqual(dc.check(env)['kind'],'physical-x11')
            self.assertTrue(dc.describe(env)['needs_observation'])
            dc.prepare(env)
            self.assertTrue(dc.describe(env)['needs_observation'])
        with patch.object(dc,'_physical',side_effect=dc.DisplayContextError('NO_SESSION')):
            dc.prepare(env)
        self.assertEqual(dc.describe(env)['kind'],'text')
        self.assertEqual(dc.describe(env)['profile'],'/profile')
        with self.assertRaisesRegex(dc.DisplayContextError,'CUA_DISPLAY_UNAVAILABLE'):
            dc.check(env)

    def stub(self,env,valid=True):
        calls=[]
        engine=SimpleNamespace()
        def native(payload,timeout_s=60):
            calls.append(payload)
            return {'executed':True,'output':capture_output(valid)}
        engine.native_cua_roundtrip=native
        def handle(request,job):
            response=engine.native_cua_roundtrip(request.get('cua',{}))
            return {'ok':response['executed']}
        engine.handle_cua_request=handle
        adapter.install_adapter_guard(engine,env=env)
        return engine,calls

    def test_transition_blocks_input_until_successful_observation_then_allows_input(self):
        env={};dc.bind_text(env,'/profile');engine,calls=self.stub(env)
        with patch.object(dc,'_physical',return_value=physical()),patch.object(dc,'missing_graphical_commands',return_value=[]):
            blocked=engine.handle_cua_request({'cua':{'action':'CLICK'}},'job1')
            self.assertFalse(blocked['performed']);self.assertEqual(calls,[])
            self.assertEqual(blocked['error'],'CUA_DISPLAY_CHANGED_OBSERVE_REQUIRED')
            observed=engine.handle_cua_request({'cua':{'action':'OBSERVE'}},'job2')
            self.assertTrue(observed['ok']);self.assertFalse(dc.describe(env).get('needs_observation',False))
            self.assertTrue(engine.handle_cua_request({'cua':{'action':'CLICK'}},'job3')['ok'])
            self.assertEqual(len(calls),2)

    def test_invalid_observation_does_not_unlock_input_after_transition(self):
        env={};dc.bind_text(env,'/profile');engine,calls=self.stub(env,valid=False)
        with patch.object(dc,'_physical',return_value=physical()),patch.object(dc,'missing_graphical_commands',return_value=[]):
            engine.handle_cua_request({'cua':{'action':'OBSERVE'}},'job1')
            self.assertTrue(dc.describe(env)['needs_observation'])
            blocked=engine.handle_cua_request({'cua':{'action':'CLICK'}},'job2')
            self.assertFalse(blocked['performed']);self.assertEqual(len(calls),1)

    def test_session_changes_during_observation_keep_input_locked(self):
        env={};dc.bind_text(env,'/profile');engine,calls=self.stub(env)
        with patch.object(dc,'_physical',side_effect=[physical(),physical(),physical(session_id='different')]),patch.object(dc,'missing_graphical_commands',return_value=[]):
            result=engine.handle_cua_request({'cua':{'action':'OBSERVE'}},'job1')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'],'CUA_FOREGROUND_SESSION_CHANGED')
        self.assertTrue(dc.describe(env)['needs_observation'])

    def test_session_change_at_observation_release_returns_safe_block_and_keeps_input_locked(self):
        env={};dc.bind_text(env,'/profile');engine,calls=self.stub(env)
        with patch.object(dc,'_physical',side_effect=[physical(),physical(),physical(),physical(session_id='different')]),patch.object(dc,'missing_graphical_commands',return_value=[]):
            result=engine.handle_cua_request({'cua':{'action':'OBSERVE'}},'job1')
        self.assertFalse(result['ok'])
        self.assertFalse(result['performed'])
        self.assertFalse(result['input_may_have_been_sent'])
        self.assertEqual(result['error'],'CUA_FOREGROUND_SESSION_CHANGED')
        self.assertTrue(dc.describe(env)['needs_observation'])
        with patch.object(dc,'_physical',return_value=physical(session_id='different')),patch.object(dc,'missing_graphical_commands',return_value=[]):
            blocked=engine.handle_cua_request({'cua':{'action':'CLICK'}},'job2')
        self.assertFalse(blocked['performed'])
        self.assertEqual(blocked['error'],'CUA_DISPLAY_CHANGED_OBSERVE_REQUIRED')
        self.assertEqual(len(calls),1)


class AdaptiveCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fb-adaptive-test-')
        self.state=Path(self.temp.name)
        config.save_config(self.state,config.default_config())
        self.env={'PATH':'/fixture/bin'}
        dc.bind_text(self.env,self.state)

    def tearDown(self): self.temp.cleanup()

    def doctor(self,missing,*,compatibility=None):
        def which(name,path=None): return None if name in missing else '/fixture/bin/'+name
        # Graphical-capability tests use an eligible artifact fixture. Source
        # checkouts do not contain the installed vendor layout or manifest.
        eligibility = compatibility if compatibility is not None else {
            'ok': True, 'profile_id': 'debian13-amd64-py313', 'issues': [], 'warnings': []}
        with patch.object(runtime,'environment',return_value=dict(self.env)),patch.object(runtime.shutil,'which',side_effect=which),patch('framebridge_expanded.network.inspect',return_value={}),patch('framebridge_expanded.compatibility.assess',return_value=eligibility):
            return runtime.doctor(self.state)

    def test_optional_gui_dependencies_do_not_block_text_tools(self):
        result=self.doctor(set(dc.GRAPHICAL_COMMANDS+dc.VIRTUAL_COMMANDS+('magick','convert','gnome-terminal','xterm')))
        self.assertTrue(result['ok'])
        self.assertEqual(result['missing_commands'],[])
        self.assertTrue(result['capabilities']['fbexec'])
        self.assertTrue(result['capabilities']['fbterm'])
        self.assertTrue(result['capabilities']['cua_tty_module'])
        self.assertFalse(result['capabilities']['graphical_cua'])
        self.assertTrue(result['optional_missing_commands']['graphical_cua'])

    def test_text_mode_does_not_bypass_incompatible_artifact(self):
        rejected = {'ok': False, 'profile_id': None, 'issues': ['Fixture artifact ABI mismatch'], 'warnings': []}
        result = self.doctor(set(dc.GRAPHICAL_COMMANDS + dc.VIRTUAL_COMMANDS), compatibility=rejected)
        self.assertFalse(result['ok'])
        self.assertIn('Fixture artifact ABI mismatch', result['issues'])
        self.assertEqual([], result['missing_commands'])
        self.assertFalse(result['capabilities']['graphical_cua'])

    def test_missing_core_command_still_blocks_startup(self):
        result=self.doctor({'bash'})
        self.assertFalse(result['ok']);self.assertIn('bash',result['missing_commands'])

    def test_explicit_private_desktop_requires_its_optional_components(self):
        current=config.load_config(self.state);current['headless']='yes';config.save_config(self.state,current)
        result=self.doctor({'Xvfb'})
        self.assertFalse(result['ok']);self.assertIn('Xvfb',result['missing_commands'])

    def test_text_manager_starts_core_session_without_xvfb_or_window_manager(self):
        spawned=[]
        def spawn(command,env,state,role):
            spawned.append((command,role))
            return SimpleNamespace(poll=lambda:None),{'role':role}
        with patch.object(runtime,'environment',return_value=dict(self.env)),patch.object(runtime,'_display_ok',return_value=False),patch.object(runtime,'_spawn',side_effect=spawn),patch.object(runtime,'_atomic'),patch.object(runtime,'_terminate'),patch.object(runtime.signal,'signal'),patch.object(runtime,'_STOP',True):
            runtime._manager(self.state)
        self.assertEqual([role for _,role in spawned],['private-session'])
        self.assertEqual(spawned[0][0][0],'dbus-run-session')


if __name__=='__main__': unittest.main()
