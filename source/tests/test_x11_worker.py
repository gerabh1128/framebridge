"""Exercise bounded worker pipes with real Python-only child processes.

The worker command is replaced at the Popen boundary. No native backend or
display is instantiated; request/permission/deadline behavior uses real pipes.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

try:
    from framebridge_expanded import x11_text as subject
except ModuleNotFoundError as error:
    if error.name != "framebridge_expanded":
        raise
    import x11_text as subject


STUB = r'''
import json, signal, sys, time
mode = sys.argv[1]
if mode == "hang_unread":
    time.sleep(10)
    raise SystemExit(2)
request = json.loads(sys.stdin.buffer.readline())
if request["window"] != 101 or request["geometry"] != [1920,1080]:
    print("ERROR TYPE_FIXTURE_CONTEXT_INVALID", flush=True)
    raise SystemExit(2)
if mode == "preflight":
    print("ERROR TYPE_TEXT_UNREPRESENTABLE_IN_CURRENT_GROUP", flush=True)
elif mode == "exit":
    raise SystemExit(2)
elif mode == "bad":
    print("NOT A VALID MESSAGE", flush=True)
elif mode == "oversize":
    print("x" * 5000, flush=True)
elif mode == "hang_before":
    time.sleep(10)
else:
    for index in range(2 if mode == "twice" else 1):
        print("INPUT", flush=True)
        if sys.stdin.buffer.readline() != b"GO\n":
            raise SystemExit(3)
    if mode == "hang_after":
        time.sleep(10)
    elif mode == "ignore_terminate":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(10)
    elif mode == "error_after":
        print("ERROR TYPE_KEYMAP_CHANGED", flush=True)
    else:
        print("OK", flush=True)
'''


class BoundedWorkerTests(unittest.TestCase):
    def test_selector_failure_does_not_spawn_worker(self):
        with patch.object(subject.selectors, "DefaultSelector", side_effect=OSError("fixture")), \
             patch.object(subject.subprocess, "Popen") as spawn:
            with self.assertRaises(OSError):
                subject.bounded_type_text("/", expected_window=101, expected_geometry=(1920, 1080),
                                          before_input=lambda: None)
        spawn.assert_not_called()

    def test_spawn_failure_closes_selector(self):
        selector = Mock()
        with patch.object(subject.selectors, "DefaultSelector", return_value=selector), \
             patch.object(subject.subprocess, "Popen", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                subject.bounded_type_text("/", expected_window=101, expected_geometry=(1920, 1080),
                                          before_input=lambda: None)
        selector.close.assert_called_once_with()

    @contextmanager
    def child(self, mode):
        real_popen = subprocess.Popen
        children = []

        def spawn(argv, **kwargs):
            self.assertEqual(argv, [sys.executable, "-I", "-S", "-B", str(Path(subject.__file__).resolve()), "--worker"])
            child = real_popen([sys.executable, "-u", "-c", STUB, mode], **kwargs)
            children.append(child)
            return child

        try:
            with patch.object(subject.subprocess, "Popen", side_effect=spawn):
                yield children
        finally:
            for process in children:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                self.assertIsNotNone(process.returncode)
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.stdout.closed)

    def invoke(self, guard, *, text="-S /run/fixture", timeout=.5):
        return subject.bounded_type_text(text, expected_window=101,
                                         expected_geometry=(1920, 1080),
                                         before_input=guard, timeout=timeout)

    def test_preflight_failure_does_not_request_any_permission(self):
        guard = Mock()
        with self.child("preflight"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_TEXT_UNREPRESENTABLE_IN_CURRENT_GROUP$"):
                self.invoke(guard)
        guard.assert_not_called()

    def test_one_input_batch_requires_one_guard_and_ack(self):
        guard = Mock()
        with self.child("success"):
            self.assertIsNone(self.invoke(guard))
        guard.assert_called_once_with()

    def test_each_input_batch_requires_a_fresh_guard(self):
        guard = Mock()
        with self.child("twice"):
            self.invoke(guard)
        self.assertEqual(guard.call_count, 2)

    def test_timeout_before_permission_has_zero_guard_calls(self):
        guard = Mock()
        started = time.monotonic()
        with self.child("hang_before"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_WORKER_TIMEOUT$"):
                self.invoke(guard, timeout=.15)
        self.assertLess(time.monotonic() - started, 2)
        guard.assert_not_called()

    def test_timeout_after_permission_retains_possible_input_evidence(self):
        guard = Mock()
        started = time.monotonic()
        with self.child("hang_after"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_WORKER_TIMEOUT$"):
                self.invoke(guard, timeout=.15)
        self.assertLess(time.monotonic() - started, 2)
        guard.assert_called_once_with()

    def test_guard_failure_terminates_child_waiting_for_ack(self):
        guard = Mock(side_effect=RuntimeError("AUTHORITY_EXPIRED"))
        started = time.monotonic()
        with self.child("success"):
            with self.assertRaisesRegex(RuntimeError, "^AUTHORITY_EXPIRED$"):
                self.invoke(guard)
        self.assertLess(time.monotonic() - started, 2)
        guard.assert_called_once_with()

    def test_stalled_child_that_ignores_termination_is_killed(self):
        guard = Mock()
        started = time.monotonic()
        with self.child("ignore_terminate") as children:
            with self.assertRaisesRegex(subject.TextError, "^TYPE_WORKER_TIMEOUT$"):
                self.invoke(guard, timeout=.15)
        self.assertLess(time.monotonic() - started, 2)
        guard.assert_called_once_with()
        self.assertLess(children[0].returncode, 0)

    def test_partial_input_error_preserves_permission_evidence(self):
        guard = Mock()
        with self.child("error_after"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_KEYMAP_CHANGED$"):
                self.invoke(guard)
        guard.assert_called_once_with()

    def test_unknown_protocol_line_fails_without_permission(self):
        guard = Mock()
        with self.child("bad"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_WORKER_PROTOCOL_INVALID$"):
                self.invoke(guard)
        guard.assert_not_called()

    def test_oversized_worker_line_fails_without_permission(self):
        guard = Mock()
        with self.child("oversize"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_WORKER_PROTOCOL_INVALID$"):
                self.invoke(guard)
        guard.assert_not_called()

    def test_worker_exit_is_not_mistaken_for_success(self):
        guard = Mock()
        with self.child("exit"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_WORKER_EXIT_UNCERTAIN$"):
                self.invoke(guard)
        guard.assert_not_called()

    def test_large_unicode_request_cannot_block_on_unread_pipe(self):
        guard = Mock()
        started = time.monotonic()
        with self.child("hang_unread"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_WORKER_TIMEOUT$"):
                self.invoke(guard, text="\U0001f642" * 4096, timeout=.15)
        self.assertLess(time.monotonic() - started, 2)
        guard.assert_not_called()

    def test_main_dispatch_runs_only_after_backend_definition(self):
        # Replace only the NativeBackend AST in memory. This executes the real
        # module/worker order with a constructor that cannot call native code.
        wrapper = r'''
import ast, pathlib, sys
source = pathlib.Path(sys.argv[1]).read_text()
tree = ast.parse(source)
replacement = ast.parse("class NativeBackend:\n    def __init__(self):\n        raise TextError('TYPE_FIXTURE_NATIVE_BOUNDARY')\n").body[0]
for index, node in enumerate(tree.body):
    if isinstance(node, ast.ClassDef) and node.name == 'NativeBackend':
        tree.body[index] = replacement
ast.fix_missing_locations(tree)
sys.argv = [sys.argv[1], '--worker']
namespace = {'__name__': '__main__', '__file__': sys.argv[0]}
exec(compile(tree, sys.argv[0], 'exec'), namespace)
'''
        request = json.dumps({"text": "a", "window": 101,
                              "geometry": [1920, 1080], "timeout": 1}) + "\n"
        result = subprocess.run([sys.executable, "-c", wrapper, str(Path(subject.__file__))],
                                input=request, text=True, capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ERROR TYPE_FIXTURE_NATIVE_BOUNDARY\n")

    def test_real_isolated_worker_bootstrap_reaches_safe_display_preflight(self):
        guard = Mock()
        # This display number cannot refer to the user's real session. Native
        # preflight can read libraries but cannot emit a GUI event.
        with patch.dict(os.environ, {"DISPLAY": ":99999"}):
            with self.assertRaises(subject.TextError) as caught:
                self.invoke(guard, timeout=1)
        self.assertIn(str(caught.exception), {"TYPE_DISPLAY_UNAVAILABLE",
                                             "TYPE_NATIVE_LIBRARY_UNAVAILABLE",
                                             "TYPE_NATIVE_ABI_UNSUPPORTED"})
        guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
