"""Actual framebridge_cua function with fake transport and opaque image fixtures.
These tests send no GUI input and make no authentic-pixel claim.
"""
import ast
import base64
import copy
import hashlib
from pathlib import Path
import re
import unittest
import uuid
from r4_ast import semantic_ast_digest
from framebridge_expanded.fleet import FleetError, select_target

TEST_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = TEST_ROOT.parent
PACKAGE_ROOT = SOURCE_ROOT / 'expanded/framebridge_expanded'
FIXTURES = TEST_ROOT / 'fixtures/r4-035'
class Image:
    def __init__(self,*,data,format): self.data,self.format=data,format

class PostMetadata(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.before_data=b'BEFORE_OPAQUE_IMAGE_TEST_BYTES'
        self.after_data=b'AFTER_OPAQUE_IMAGE_TEST_BYTES'
        self.before=dict(ok=True,action='CLICK',performed=True,resultado_final='CUA_CLICK_PASS',
            input_may_have_been_sent=True,do_not_replay=False,job_id='action-id',mcp_job_id='action-id',
            request_id='action-request',expected_window_gate='PASS',expected_source_geometry_gate='PASS',
            root_x='358',root_y='240',coord_space='WINDOW',coord_space_width='538',coord_space_height='353',
            image_base64=base64.b64encode(self.before_data).decode(),mime='image/jpeg',image_mime='image/jpeg',
            width='538',height='353',source_x='0',source_y='0',source_width='538',source_height='353',
            display_width='1920',display_height='1080',capture_scope='WINDOW',coord_space_default='WINDOW_PREVIEW',
            coord_space_available='WINDOW,WINDOW_PREVIEW,ROOT',window_x='0',window_y='0',window_width='538',
            window_height='353',window_full_x='-24',window_full_y='-18',window_full_width='562',
            window_full_height='371',window_capture_clipped='true',visual_capture_backend='X11_ROOT_CROP',
            active_window_id='501',window_title='OWN BEFORE',cursor_x='358',cursor_y='240',
            sha256=hashlib.sha256(self.before_data).hexdigest(),bytes=str(len(self.before_data)),
            capture_id='before-capture',session_id='session-3',session={'id':'3'},display_context={'kind':'physical-x11','display':':0','phase':'before'},
            timestamp='before-time',visual_capture_gate='PASS',cua_protocol_version='3',
            frame_output='RESULTADO_FINAL=CUA_CLICK_PASS\nWINDOW_WIDTH=538\nWINDOW_FULL_WIDTH=562',
            node='NODE_A',node_id='11111111-1111-4111-8111-111111111111',hostname='NODE_A',
            requested_target='NODE_A',resolved_target='NODE_A',target='NODE_A',transport='broker_outbound')
        self.after=dict(ok=True,action='OBSERVE',resultado_final='CUA_VISUAL_OBSERVE_PASS',
            image_base64=base64.b64encode(self.after_data).decode(),mime='image/jpeg',image_mime='image/jpeg',
            width='654',height='328',source_x='0',source_y='0',source_width='654',source_height='328',
            display_width='1920',display_height='1080',capture_scope='WINDOW',coord_space_default='WINDOW_PREVIEW',
            coord_space_available='WINDOW,WINDOW_PREVIEW,ROOT',window_x='0',window_y='0',window_width='654',
            window_height='328',window_full_x='-24',window_full_y='-18',window_full_width='678',
            window_full_height='346',window_capture_clipped='true',visual_capture_backend='X11_ROOT_CROP',
            active_window_id='502',window_title='OWN RESULT',cursor_x='400',cursor_y='260',
            sha256=hashlib.sha256(self.after_data).hexdigest(),bytes=str(len(self.after_data)),
            capture_id='after-capture',session_id='session-3',session={'id':'3','phase':'after'},
            display_context={'kind':'physical-x11','display':':0','phase':'after'},timestamp='after-time',
            visual_capture_gate='PASS',cua_protocol_version='3',
            frame_output='RESULTADO_FINAL=CUA_VISUAL_OBSERVE_PASS\nWINDOW_WIDTH=654\nWINDOW_FULL_WIDTH=678',
            node='NODE_A',node_id='11111111-1111-4111-8111-111111111111',hostname='NODE_A',
            requested_target='NODE_A',resolved_target='NODE_A',target='NODE_A',transport='broker_outbound')
        self.raise_post=False
        self.ns=dict(uuid=uuid,hashlib=hashlib,re=re,base64=base64,Image=Image,
                     FleetError=FleetError,select_target=select_target,
                     sanitize_text=str,dispatch_cua_request=self.dispatch)
        module=ast.parse((SOURCE_ROOT/'portable/front_door_mcp.py').read_text())
        function=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='framebridge_cua')
        function.decorator_list=[]
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),
                     str(SOURCE_ROOT/'portable/front_door_mcp.py'),'exec'),self.ns)
        self.function=self.ns['framebridge_cua']

    def dispatch(self,payload,authorized,timeout_s,job_id,target):
        self.calls.append(dict(payload=copy.deepcopy(payload),authorized=authorized,
                               timeout_s=timeout_s,job_id=job_id,target=target))
        if len(self.calls)==1: return copy.deepcopy(self.before)
        if self.raise_post: raise RuntimeError('TEST TRANSPORT FAILURE')
        return copy.deepcopy(self.after)

    def call(self,**kwargs):
        args=dict(action='CLICK',authorized=True,x=358,y=240,coord_space='WINDOW',capture_scope='WINDOW',
                  expected_window_id='501',expected_source_width=538,expected_source_height=353,
                  observe_after=True,observe_scope='WINDOW',job_id='action-id',target='NODE_A')
        args.update(kwargs)
        response=self.function(**args)
        return (response[0],response[1]) if isinstance(response,list) else (response,None)

    def test_clipped_window_post_metadata_is_one_new_snapshot(self):
        meta,image=self.call()
        for key in ('source_x','source_y','source_width','source_height','window_x','window_y',
                    'window_width','window_height','window_full_x','window_full_y','window_full_width',
                    'window_full_height','window_capture_clipped','visual_capture_backend',
                    'coord_space_default','coord_space_available','cursor_x','cursor_y','frame_output',
                    'capture_id','active_window_id','window_title','display_context','session','node_id'):
            self.assertEqual(meta[key],self.after[key],key)
            self.assertEqual(meta['action_capture'][key],self.before[key],key)
        self.assertEqual(image.data,self.after_data)
        self.assertEqual(meta['sha256'],hashlib.sha256(image.data).hexdigest())
        self.assertEqual(meta['bytes'],str(len(image.data)))
        self.assertNotIn('image_base64',meta)
        self.assertNotIn('image_base64',meta['action_capture'])

    def test_post_inside_window_replaces_clipped_flag_and_full_geometry(self):
        self.after.update(source_x='296',source_y='202',source_width='678',source_height='346',
                          window_x='296',window_y='202',window_width='678',window_height='346',
                          window_full_x='296',window_full_y='202',window_full_width='678',
                          window_full_height='346',window_capture_clipped='false')
        meta,_=self.call()
        self.assertEqual(meta['window_capture_clipped'],'false')
        self.assertEqual(meta['source_x'],meta['window_x'])
        self.assertEqual(meta['window_x'],meta['window_full_x'])
        self.assertEqual(meta['action_capture']['window_capture_clipped'],'true')

    def test_window_to_root_clears_every_old_window_field(self):
        for key in tuple(self.after):
            if key.startswith('window_'): self.after.pop(key)
        self.after.update(source_x='0',source_y='0',source_width='1920',source_height='1080',
                          width='960',height='540',capture_scope='ROOT',visual_capture_backend='X11_ROOT',
                          coord_space_default='ROOT',coord_space_available='ROOT,PREVIEW',
                          frame_output='RESULTADO_FINAL=CUA_VISUAL_OBSERVE_PASS\nCAPTURE_SCOPE=ROOT')
        meta,image=self.call(observe_scope='ROOT')
        self.assertFalse(any(key.startswith('window_') for key in meta))
        self.assertEqual(meta['source_width'],'1920')
        self.assertEqual(meta['capture_scope'],'ROOT')
        self.assertEqual(meta['coord_space_default'],'ROOT')
        self.assertIn('window_full_width',meta['action_capture'])
        self.assertEqual(meta['action_capture']['capture_scope'],'WINDOW')
        self.assertEqual(meta['sha256'],hashlib.sha256(image.data).hexdigest())

    def test_optional_snapshot_fields_missing_from_post_are_not_stale(self):
        for key in ('source_x','source_y','cursor_x','cursor_y','session','capture_id','frame_output'):
            self.after.pop(key)
        meta,_=self.call()
        for key in ('source_x','source_y','cursor_x','cursor_y','session','capture_id','frame_output'):
            self.assertNotIn(key,meta)
            self.assertIn(key,meta['action_capture'])

    def test_action_result_identity_and_input_evidence_stay_unchanged(self):
        meta,_=self.call()
        for key in ('action','resultado_final','performed','input_may_have_been_sent','do_not_replay',
                    'job_id','mcp_job_id','request_id','expected_window_gate','expected_source_geometry_gate',
                    'root_x','root_y','coord_space','coord_space_width','coord_space_height'):
            self.assertEqual(meta[key],self.before[key],key)
        self.assertEqual(meta['post_job_id'],'cua-post-'+hashlib.sha256(b'action-id').hexdigest())
        self.assertEqual(meta['post_resultado_final'],'CUA_VISUAL_OBSERVE_PASS')
        self.assertEqual([c['payload']['action'] for c in self.calls],['CLICK','OBSERVE'])

    def test_post_failure_does_not_replay_or_hoist_failed_snapshot(self):
        self.after=dict(ok=False,error='VISUAL_CAPTURE_CONTEXT_CHANGED',window_width='999')
        meta,image=self.call()
        self.assertTrue(meta['performed'])
        self.assertFalse(meta['post_ok'])
        self.assertEqual(meta['post_error'],'VISUAL_CAPTURE_CONTEXT_CHANGED')
        self.assertEqual(meta['window_width'],'538')
        self.assertEqual(image.data,self.before_data)
        self.assertNotIn('action_capture',meta)
        self.assertEqual(len(self.calls),2)

    def test_post_transport_exception_does_not_replay_action(self):
        self.raise_post=True
        meta,_=self.call()
        self.assertTrue(meta['performed']);self.assertFalse(meta['post_ok'])
        self.assertEqual(meta['post_error'],'POST_OBSERVATION_TRANSPORT_FAILED')
        self.assertEqual(len(self.calls),2)

    def test_unauthorized_request_never_dispatches(self):
        meta,image=self.call(authorized=False)
        self.assertEqual(meta['error'],'AUTHORIZED_TRUE_REQUIRED')
        self.assertEqual(self.calls,[]);self.assertIsNone(image)

    def test_no_followup_requested_leaves_action_snapshot_untouched(self):
        meta,image=self.call(observe_after=False)
        self.assertNotIn('action_capture',meta)
        self.assertNotIn('post_ok',meta)
        self.assertEqual(meta['window_width'],'538')
        self.assertEqual(image.data,self.before_data)
        self.assertEqual(len(self.calls),1)

    def test_capture_key_tuple_has_no_duplicates(self):
        tree = ast.parse((SOURCE_ROOT/'portable/front_door_mcp.py').read_text())
        assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'capture_keys' for t in node.targets))
        values = [v.value for v in assignment.value.elts]
        self.assertEqual(len(values), len(set(values)))

    def test_authenticated_dispatch_functions_unchanged_from_public_035(self):
        import json
        guards = json.loads((FIXTURES/'STRUCTURAL-GUARDS.json').read_text())['frontdoor']
        # FBEXEC's public envelope changes in 0.4. Its authorization and
        # confirmed-target behavior are exercised through MCP dispatch tests.
        guards.pop("framebridge_exec")
        tree = ast.parse((SOURCE_ROOT/'portable/front_door_mcp.py').read_text())
        for name, expected in guards.items():
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            actual = semantic_ast_digest(function)
            self.assertEqual(actual, expected)

if __name__=='__main__': unittest.main(verbosity=2)
