"""Synthetic-pixel and race regressions. Never uses a real X11 display/input."""
import ast
import base64
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from PIL import Image
from r4_ast import semantic_ast_digest

TEST_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = TEST_ROOT.parent
PACKAGE_ROOT = SOURCE_ROOT / 'expanded/framebridge_expanded'
FIXTURES = TEST_ROOT / 'fixtures/r4-035'
SPEC = importlib.util.spec_from_file_location('window_candidate', SOURCE_ROOT/'sources/original-asus/framebridge-native/host/framebridge_cua_linux.py')
cua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cua)


class WindowCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name)/'root.png'
        # Unique pixels encode their original absolute ROOT position. This is
        # synthetic unit-test input, never presented as real desktop evidence.
        rgb = bytes(v for y in range(10) for x in range(12)
                    for v in (x*18, y*23, (x+y)*10))
        ppm = b'P6\n12 10\n255\n'+rgb
        made = subprocess.run(['convert','ppm:-',str(self.fixture)], input=ppm,
                              capture_output=True, timeout=5)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.geometry = (12,10)
        self.window = 500
        self.rect = dict(x=2,y=3,width=8,height=6)
        self.calls = []
        self.crop_pixels = None
        self.mutate_after = None
        self.crop_failure = False
        self.encode_failure = False
        self.bad_identify = None
        self.originals = {name:getattr(cua,name) for name in
                          ['_run','_require_x11','_geometry','_cursor',
                           '_active_window','_window_geometry']}
        cua._run = self.run_backend
        cua._require_x11 = lambda: None
        cua._geometry = lambda: self.geometry
        cua._cursor = lambda: (4,5)
        cua._active_window = lambda: (self.window,'NEUTRAL UNIT FIXTURE')
        cua._window_geometry = self.window_geometry
        self.addCleanup(self.restore)

    def restore(self):
        for name,value in self.originals.items(): setattr(cua,name,value)

    def window_geometry(self, window):
        self.calls.append(('window_geometry',window))
        return dict(self.rect)

    def run_backend(self, args, timeout=15):
        self.calls.append(tuple(args))
        if args[0]=='import':
            self.assertEqual(args[1:3],['-window','root'])
            shutil.copyfile(self.fixture,args[-1])
            if self.mutate_after=='import_focus': self.window=501
            elif self.mutate_after=='import_rect': self.rect['x']=3
            elif self.mutate_after=='import_display': self.geometry=(13,10)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        if args[0]=='identify' and self.bad_identify:
            if self.bad_identify=='root_size' and Path(args[-1]).name=='screen.png':
                return SimpleNamespace(returncode=0,stdout='11 10')
            if self.bad_identify=='crop_size' and Path(args[-1]).name=='window.png':
                return SimpleNamespace(returncode=0,stdout='7 6')
            if self.bad_identify=='malformed':
                return SimpleNamespace(returncode=0,stdout='not geometry')
        if args[0]=='convert' and '-crop' in args:
            if self.crop_failure: return SimpleNamespace(returncode=1,stdout='')
            result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
            if result.returncode==0:
                with Image.open(args[-1]) as image:
                    rgb=image.convert('RGB')
                    self.crop_pixels=(image.size,[rgb.getpixel((x,y)) for y in range(rgb.height) for x in range(rgb.width)])
            if self.mutate_after=='crop_rect': self.rect['height']=5
            return result
        if args[0]=='convert' and '-quality' in args:
            if self.encode_failure: return SimpleNamespace(returncode=1,stdout='')
            result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
            if self.mutate_after=='encode_focus': self.window=502
            return result
        return subprocess.run(args,capture_output=True,text=True,timeout=timeout)

    def capture(self,scope='WINDOW',**extra):
        message=dict(capture_scope=scope,expected_window_id=500,
                     expected_source_width=8 if scope=='WINDOW' else 12,
                     expected_source_height=6 if scope=='WINDOW' else 10)
        message.update(extra)
        return cua._capture_visual_jpeg(message)

    def test_exact_root_crop_preserves_absolute_marker_pixels(self):
        result=self.capture()
        self.assertTrue(result['ok'])
        size,pixels=self.crop_pixels
        self.assertEqual(size,(8,6))
        self.assertEqual(pixels[0],(36,69,50))  # absolute ROOT (2,3)
        self.assertEqual(pixels[-1],(162,184,170))  # absolute ROOT (9,8)
        frame=result['_frame_output']
        self.assertIn('SOURCE_X=2\nSOURCE_Y=3\nSOURCE_WIDTH=8\nSOURCE_HEIGHT=6',frame)
        self.assertIn('VISUAL_CAPTURE_BACKEND=X11_ROOT_CROP',frame)
        data=base64.b64decode(frame.split('BASE64=',1)[1],validate=True)
        with Image.open(io.BytesIO(data)) as image: self.assertEqual(image.size,(8,6))
        self.assertFalse(any(call[:3]==('import','-window','500') for call in self.calls))

    def test_crop_never_requests_hidden_window_pixels(self):
        self.capture()
        imports=[call for call in self.calls if call[0]=='import']
        self.assertEqual(len(imports),1)
        self.assertEqual(imports[0][2],'root')
        # Thus any real occluder in ROOT stays visible in the crop.

    def test_root_regression_uses_no_window_geometry_or_crop(self):
        result=self.capture('ROOT')
        self.assertTrue(result['ok'])
        self.assertIsNone(self.crop_pixels)
        self.assertFalse(any(call[0]=='window_geometry' for call in self.calls))
        self.assertIn('SOURCE_WIDTH=12\nSOURCE_HEIGHT=10',result['_frame_output'])

    def test_root_malformed_backend_dimensions_still_rejected(self):
        self.bad_identify='root_size'
        with self.assertRaisesRegex(cua.CuaTransportError,'^VISUAL_CAPTURE_GEOMETRY_MISMATCH$'):
            self.capture('ROOT')

    def test_window_root_dimensions_mismatch_rejected_before_crop(self):
        self.bad_identify='root_size'
        with self.assertRaisesRegex(cua.CuaTransportError,'^VISUAL_CAPTURE_GEOMETRY_MISMATCH$'):
            self.capture()
        self.assertIsNone(self.crop_pixels)

    def test_crop_dimensions_mismatch_still_rejected(self):
        self.bad_identify='crop_size'
        with self.assertRaisesRegex(cua.CuaTransportError,'^VISUAL_CAPTURE_GEOMETRY_MISMATCH$'):
            self.capture()
        self.assertFalse(any('-quality' in call for call in self.calls))

    def test_malformed_identify_rejected(self):
        self.bad_identify='malformed'
        with self.assertRaisesRegex(cua.CuaTransportError,'^VISUAL_CAPTURE_GEOMETRY_INVALID$'):
            self.capture()

    def test_partial_offscreen_edges_and_corners_use_explicit_visible_intersection(self):
        cases=[((-2,3,8,6),(0,3,6,6)), ((8,3,8,6),(8,3,4,6)),
               ((2,-2,8,6),(2,0,8,4)), ((2,8,8,6),(2,8,8,2)),
               ((-2,-2,8,6),(0,0,6,4)), ((8,-2,8,6),(8,0,4,4)),
               ((-2,8,8,6),(0,8,6,2)), ((8,8,8,6),(8,8,4,2))]
        for full,visible in cases:
            with self.subTest(full=full):
                self.rect=dict(zip(('x','y','width','height'),full))
                x,y,width,height=visible
                result=self.capture(expected_source_width=width,expected_source_height=height)
                frame=result['_frame_output']
                self.assertIn('SOURCE_X=%d\nSOURCE_Y=%d\nSOURCE_WIDTH=%d\nSOURCE_HEIGHT=%d'%visible,frame)
                self.assertIn('WINDOW_CAPTURE_CLIPPED=true',frame)
                for field,value in zip(('X','Y','WIDTH','HEIGHT'),full):
                    self.assertIn('WINDOW_FULL_%s=%d'%(field,value),frame)
                size,pixels=self.crop_pixels
                self.assertEqual(size,(width,height))
                self.assertEqual(pixels[0],(x*18,y*23,(x+y)*10))
                self.assertEqual(pixels[-1],((x+width-1)*18,(y+height-1)*23,
                                             (x+y+width+height-2)*10))

    def test_fully_offscreen_rejected_without_capture_or_padding(self):
        for rect in [dict(x=-8,y=3,width=8,height=6),
                     dict(x=2,y=-6,width=8,height=6),
                     dict(x=12,y=3,width=8,height=6),
                     dict(x=2,y=10,width=8,height=6)]:
            self.rect=rect;self.calls.clear()
            with self.subTest(rect=rect):
                with self.assertRaisesRegex(cua.CuaTransportError,'^WINDOW_CAPTURE_OUTSIDE_DISPLAY$'):
                    self.capture()
                self.assertFalse(any(call[0]=='import' for call in self.calls))

    def test_window_input_mapping_uses_the_same_visible_origin_and_bounds(self):
        self.rect=dict(x=-2,y=-2,width=8,height=6)
        old=cua._require_chat_authorization
        cua._require_chat_authorization=lambda *unused: None
        try:
            msg=dict(coord_space='WINDOW',expected_window_id=500,
                     expected_source_width=6,expected_source_height=4,x=0,y=0)
            context=cua._action_context(msg)
            self.assertEqual(context['window_rect'],dict(x=0,y=0,width=6,height=4))
            self.assertEqual(context['window_full_rect'],self.rect)
            self.assertEqual(cua._resolve_point(msg,12,10)[2:4],(0,0))
            msg.update(x=5,y=3)
            self.assertEqual(cua._resolve_point(msg,12,10)[2:4],(5,3))
            msg.update(x=6,y=3)
            with self.assertRaisesRegex(cua.CuaTransportError,'^WINDOW_COORDINATES_OUT_OF_BOUNDS$'):
                cua._resolve_point(msg,12,10)
            msg.update(x=-1,y=0)
            with self.assertRaisesRegex(cua.CuaTransportError,'^WINDOW_COORDINATES_OUT_OF_BOUNDS$'):
                cua._resolve_point(msg,12,10)
        finally:
            cua._require_chat_authorization=old

    def test_window_preview_mapping_uses_visible_crop_geometry(self):
        self.rect=dict(x=8,y=8,width=8,height=6)
        old=cua._require_chat_authorization
        cua._require_chat_authorization=lambda *unused: None
        try:
            msg=dict(coord_space='WINDOW_PREVIEW',expected_window_id=500,
                     expected_source_width=4,expected_source_height=2,
                     preview_width=2,preview_height=1,x=1,y=0)
            context=cua._action_context(msg)
            self.assertEqual(context['window_rect'],dict(x=8,y=8,width=4,height=2))
            self.assertEqual(cua._resolve_point(msg,12,10)[2:4],(10,8))
            lines='\n'.join(cua._evidence_lines(context))
            self.assertIn('WINDOW_FULL_X=8',lines)
            self.assertIn('WINDOW_FULL_WIDTH=8',lines)
            self.assertIn('WINDOW_WIDTH=4',lines)
            self.assertIn('WINDOW_CAPTURE_CLIPPED=true',lines)
        finally:
            cua._require_chat_authorization=old

    def test_full_window_dimensions_not_accepted_for_clipped_source(self):
        self.rect=dict(x=-2,y=-2,width=8,height=6)
        with self.assertRaisesRegex(cua.CuaTransportError,'^DISPLAY_GEOMETRY_CHANGED$'):
            self.capture(expected_source_width=8,expected_source_height=6)
        self.assertFalse(any(call[0]=='import' for call in self.calls))

    def test_invalid_full_rectangle_is_typed(self):
        for rect in [dict(x=0,y=0,width=0,height=2),dict(x=0,y=0,width=2,height=-1),
                     dict(x='0',y=0,width=2,height=2),dict(x=0,y=0,width=True,height=2)]:
            self.rect=rect
            with self.subTest(rect=rect):
                with self.assertRaisesRegex(cua.CuaTransportError,'^WINDOW_GEOMETRY_INVALID$'):
                    self.capture()

    def test_focus_change_after_root_capture_rejected(self):
        self.mutate_after='import_focus'
        with self.assertRaisesRegex(cua.CuaTransportError,'^ACTIVE_WINDOW_CHANGED$'):
            self.capture()
        self.assertIsNone(self.crop_pixels)

    def test_window_move_after_root_capture_rejected(self):
        self.mutate_after='import_rect'
        with self.assertRaisesRegex(cua.CuaTransportError,'^WINDOW_GEOMETRY_CHANGED$'):
            self.capture()
        self.assertIsNone(self.crop_pixels)

    def test_display_change_after_root_capture_rejected(self):
        self.mutate_after='import_display'
        with self.assertRaisesRegex(cua.CuaTransportError,'^VISUAL_CAPTURE_CONTEXT_CHANGED$'):
            self.capture()

    def test_window_resize_during_crop_rejected(self):
        self.mutate_after='crop_rect'
        with self.assertRaisesRegex(cua.CuaTransportError,'^WINDOW_GEOMETRY_CHANGED$'):
            self.capture()
        self.assertFalse(any('-quality' in call for call in self.calls))

    def test_focus_change_during_jpeg_encoding_rejected_before_publication(self):
        self.mutate_after='encode_focus'
        with self.assertRaisesRegex(cua.CuaTransportError,'^ACTIVE_WINDOW_CHANGED$'):
            self.capture()

    def test_wrong_expected_window_rejected_before_capture(self):
        with self.assertRaisesRegex(cua.CuaTransportError,'^ACTIVE_WINDOW_CHANGED$'):
            self.capture(expected_window_id=999)
        self.assertFalse(any(call[0]=='import' for call in self.calls))

    def test_wrong_expected_source_dimensions_rejected_before_capture(self):
        with self.assertRaisesRegex(cua.CuaTransportError,'^DISPLAY_GEOMETRY_CHANGED$'):
            self.capture(expected_source_width=7)
        self.assertFalse(any(call[0]=='import' for call in self.calls))

    def test_crop_backend_failure_is_typed(self):
        self.crop_failure=True
        with self.assertRaisesRegex(cua.CuaTransportError,'^VISUAL_WINDOW_CROP_FAILED$'):
            self.capture()

    def test_jpeg_backend_failure_is_typed(self):
        self.encode_failure=True
        with self.assertRaisesRegex(cua.CuaTransportError,'^VISUAL_JPEG_ENCODE_FAILED$'):
            self.capture()

    def test_action_context_keeps_real_native_authorization_checks(self):
        self.rect=dict(x=-2,y=-2,width=8,height=6)
        msg=dict(coord_space='WINDOW',expected_window_id=500,
                 expected_source_width=6,expected_source_height=4)
        with self.assertRaisesRegex(cua.CuaTransportError,'^CHAT_AUTHORIZATION_REQUIRED$'):
            cua._action_context(msg)
        self.assertEqual(self.calls,[])
        good=cua._action_context(dict(msg,authorized='YES'))
        self.assertEqual(good['window_rect'],dict(x=0,y=0,width=6,height=4))
        with self.assertRaisesRegex(cua.CuaTransportError,'^SECRET_ENTRY_FORBIDDEN$'):
            cua._action_context(dict(msg,authorized='YES',semantic_action='TOKEN'))
        with self.assertRaisesRegex(cua.CuaTransportError,'^SPECIFIC_CONFIRMATION_REQUIRED$'):
            cua._action_context(dict(msg,authorized='YES',semantic_action='SYSTEM_CHANGE'))

    def test_all_other_existing_functions_and_top_level_nodes_unchanged(self):
        import hashlib, json
        tree = ast.parse((SOURCE_ROOT/'sources/original-asus/framebridge-native/host/framebridge_cua_linux.py').read_text())
        retained = [node for node in tree.body
                    if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name in {'_capture_visual_jpeg', '_visual_png_geometry',
                                              '_check_window_capture_state', '_visible_window_rect',
                                              '_action_context', '_evidence_lines'})]
        actual = semantic_ast_digest(retained)
        expected = json.loads((FIXTURES/'STRUCTURAL-GUARDS.json').read_text())['window_retained_sha256']
        self.assertEqual(actual, expected)


if __name__=='__main__': unittest.main()
