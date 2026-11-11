import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = TEST_ROOT.parent
PACKAGE_ROOT = SOURCE_ROOT / 'expanded/framebridge_expanded'
FIXTURES = TEST_ROOT / 'fixtures/r4-035'

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

BASE = load('_audit_baseline', FIXTURES / 'fbterm_engine.py')
CAND = load('_audit_candidate', SOURCE_ROOT / 'sources/original-asus/fbterm/bin/fbterm_engine.py')

def audit_function(path, module):
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'FBTerm')
    execute = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'execute')
    target = next(n for n in ast.walk(execute) if isinstance(n, ast.Try)
                  and any(isinstance(v, ast.Name) and v.id == 'incomplete_jobs'
                          for body in n.body for v in ast.walk(body))
                  and any(isinstance(h.type, ast.Name) and h.type.id == 'OSError' for h in n.handlers))
    fn = ast.FunctionDef(name='audit', args=ast.arguments(posonlyargs=[], args=[ast.arg(arg='self')],
                          vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]),
                         body=[ast.Assign(targets=[ast.Name(id='incomplete_jobs', ctx=ast.Store())],
                              value=ast.List(elts=[], ctx=ast.Load())), target,
                              ast.Return(value=ast.Name(id='incomplete_jobs', ctx=ast.Load()))],
                         decorator_list=[])
    output = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    ns = {'os': os, 'FBTermError': module.FBTermError}
    exec(compile(output, str(path), 'exec'), ns)
    return ns['audit']

OLD_AUDIT = audit_function(FIXTURES / 'fbterm_engine.py', BASE)
NEW_AUDIT = audit_function(SOURCE_ROOT / 'sources/original-asus/fbterm/bin/fbterm_engine.py', CAND)

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.jobs = self.root / 'jobs'; self.jobs.mkdir()
        self.term = SimpleNamespace(jobs=self.jobs)

    def tearDown(self):
        self.temporary.cleanup()

    def job(self, name, done=True, result=True):
        p = self.jobs / name; p.mkdir()
        if done: (p / 'DONE').write_bytes(b'')
        if result: (p / 'result.env').write_text('EXIT=0\n')
        return p

    def equal(self, expected):
        self.assertEqual(sorted(OLD_AUDIT(self.term)), sorted(expected))
        self.assertEqual(sorted(NEW_AUDIT(self.term)), sorted(expected))

    def test_empty_and_completed_history(self):
        self.equal([])
        for i in range(100): self.job(str(i))
        self.equal([])

    def test_every_kind_of_incomplete_evidence_is_retained(self):
        self.job('complete'); self.job('missing-done', done=False)
        self.job('missing-result', result=False); self.job('missing-both', done=False, result=False)
        self.equal(['missing-done', 'missing-result', 'missing-both'])

    def test_files_and_broken_directory_links_are_ignored_as_before(self):
        (self.jobs / 'plain-file').write_text('x')
        (self.jobs / 'broken-link').symlink_to(self.root / 'absent', target_is_directory=True)
        self.equal([])

    def test_directory_links_preserve_existing_follow_behavior(self):
        target = self.root / 'target'; target.mkdir(); (target / 'DONE').touch()
        (self.jobs / 'directory-link').symlink_to(target, target_is_directory=True)
        self.equal(['directory-link'])
        (target / 'result.env').touch(); self.equal([])

    def test_broken_evidence_links_remain_incomplete(self):
        p = self.job('broken-evidence', done=False)
        (p / 'DONE').symlink_to(self.root / 'absent')
        self.equal(['broken-evidence'])

    def test_evidence_directories_still_count_as_existing_exactly_as_before(self):
        p = self.job('existing-directories', done=False, result=False)
        (p / 'DONE').mkdir(); (p / 'result.env').mkdir()
        self.equal([])

    def test_permission_or_io_failure_remains_fail_closed(self):
        with patch.object(os, 'scandir', side_effect=OSError('io')):
            with self.assertRaisesRegex(CAND.FBTermError, 'no se pudo auditar jobs previos'):
                NEW_AUDIT(self.term)

    def test_scandir_is_closed_when_iteration_fails(self):
        closed = []
        class Broken:
            def __enter__(self): return self
            def __exit__(self, *a): closed.append(True)
            def __iter__(self): raise OSError('io')
        with patch.object(os, 'scandir', return_value=Broken()):
            with self.assertRaises(CAND.FBTermError): NEW_AUDIT(self.term)
        self.assertEqual(closed, [True])

    def test_execute_refuses_incomplete_before_creating_or_sending_new_job(self):
        self.job('prior-incomplete', result=False)
        for module in [BASE, CAND]:
            term = module.FBTerm(self.root, 'test-session', 'test-title')
            term.ensure = lambda **kw: None
            def do_not_send(*a, **kw): self.fail('command sent before audit rejected')
            term._run = do_not_send
            with self.assertRaises(module.FBTermBusy): term.execute('never-created', 'printf x\n', 1)
            self.assertFalse((self.jobs / 'never-created').exists())

    def test_execute_complete_flow_keeps_hash_and_prompt_contract(self):
        self.job('past-complete')
        term = CAND.FBTerm(self.root, 'test-session', 'test-title')
        term.ensure = lambda **kw: None
        ready = self.root / 'prompt.ready'; ready.touch()
        def send(argv, **kw):
            self.assertEqual(argv[0:2], ['tmux', 'send-keys'])
            p = self.jobs / 'fresh'
            h = hashlib.sha256((p / 'command.bash').read_bytes()).hexdigest()
            (p / 'result.env').write_text('EXIT=0\nCOMMAND_SHA256=' + h + '\n')
            (p / 'DONE').touch(); ready.touch()
            done_time = (p / 'DONE').stat().st_mtime_ns
            os.utime(ready, ns=(done_time + 1000000, done_time + 1000000))
            return SimpleNamespace(returncode=0, stderr='')
        term._run = send
        result = term.execute('fresh', 'printf x\n', 1)
        self.assertEqual(result['COMMAND_HASH_GATE'], 'PASS')
        self.assertEqual(result['EXIT'], '0')

    def test_only_the_history_iteration_changes(self):
        old = (FIXTURES / 'fbterm_engine.py').read_text()
        root = SOURCE_ROOT.parent
        review = json.loads((root / 'provenance/FBTERM-VIEWER-HEADER-0.3.7-REVIEW.json').read_text())
        self.assertEqual(hashlib.sha256((root / review['path']).read_bytes()).hexdigest(),
                         review['reviewed_sha256'])
        preserved = (root / review['preserved_original']).read_bytes()
        self.assertEqual(hashlib.sha256(preserved).hexdigest(), review['preserved_original_sha256'])
        new = preserved.decode('utf-8')
        start = old.index('                for prior in self.jobs.iterdir():')
        end = old.index('            except OSError as exc:', start)
        nstart = new.index('                # DirEntry reuses')
        nend = new.index('            except OSError as exc:', nstart)
        self.assertEqual(old[:start] + old[end:], new[:nstart] + new[nend:])

if __name__ == '__main__': unittest.main(verbosity=2)
