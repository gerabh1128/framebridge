"""Deterministic worker initialization races, using only in-memory fixtures."""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

WORK = Path(__file__).resolve().parents[1]
EXPANDED = WORK / "expanded/framebridge_expanded"


def definitions(module, names, namespace):
    tree = ast.parse((EXPANDED / module).read_text())
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(EXPANDED / module), "exec"), namespace)
    return namespace


class ObservedRLock:
    """Signal when the competing worker reaches the lock, without timing guesses."""
    def __init__(self):
        self.lock = threading.RLock()
        self.waiting = threading.Event()
        self.watch = None

    def __enter__(self):
        if threading.get_ident() == self.watch:
            self.waiting.set()
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()

    def watched(self, function):
        self.watch = threading.get_ident()
        return function()


class WorkerInitializationTests(unittest.TestCase):
    def test_runtime_publishes_complete_module_once_to_competing_workers(self):
        started, release = threading.Event(), threading.Event()
        lock, module = ObservedRLock(), SimpleNamespace()
        loads = []

        def initialize(target):
            loads.append(target)
            started.set()
            if not release.wait(2):
                raise AssertionError("fixture load was not released")
            target.settings = "complete"

        loader = SimpleNamespace(name="fixture", exec_module=initialize)
        namespace = definitions("runtime.py", {"_legacy"}, dict(
            _LEGACY=None, _LEGACY_LOCK=lock, PREFIX=Path("/fixture"),
            importlib=SimpleNamespace(
                machinery=SimpleNamespace(SourceFileLoader=lambda *args: loader),
                util=SimpleNamespace(spec_from_loader=lambda *args: object(),
                                     module_from_spec=lambda spec: module))))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(namespace["_legacy"])
            try:
                self.assertTrue(started.wait(1))
                self.assertIsNone(namespace["_LEGACY"])
                second = pool.submit(lock.watched, namespace["_legacy"])
                self.assertTrue(lock.waiting.wait(1))
                self.assertFalse(second.done())
            finally:
                release.set()
            self.assertIs(first.result(1), module)
            self.assertIs(second.result(1), module)
        self.assertEqual(module.settings, "complete")
        self.assertEqual(loads, [module])

    def test_runtime_failed_initialization_remains_retryable(self):
        attempts = []
        module = SimpleNamespace()

        def initialize(target):
            attempts.append(target)
            if len(attempts) == 1:
                raise RuntimeError("fixture failure")
            target.settings = "complete"

        loader = SimpleNamespace(name="fixture", exec_module=initialize)
        namespace = definitions("runtime.py", {"_legacy"}, dict(
            _LEGACY=None, _LEGACY_LOCK=threading.RLock(), PREFIX=Path("/fixture"),
            importlib=SimpleNamespace(
                machinery=SimpleNamespace(SourceFileLoader=lambda *args: loader),
                util=SimpleNamespace(spec_from_loader=lambda *args: object(),
                                     module_from_spec=lambda spec: module))))
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            namespace["_legacy"]()
        self.assertIsNone(namespace["_LEGACY"])
        self.assertIs(namespace["_legacy"](), module)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(module.settings, "complete")

    def test_fleet_bootstrap_serializes_shared_path_check_remove_and_append(self):
        started, release = threading.Event(), threading.Event()
        lock = ObservedRLock()
        broker, fleet = Path("/fixture/broker"), Path("/fixture/fleet")

        class Paths(list):
            def remove(self, value):
                if not started.is_set():
                    started.set()
                    if not release.wait(2):
                        raise AssertionError("fixture path update was not released")
                super().remove(value)

        paths = Paths([str(broker), str(fleet)])
        package = SimpleNamespace(__file__=str(broker / "framebridge_linux_chatgpt/__init__.py"),
                                  __path__=[])
        namespace = definitions("fleet.py", {"bootstrap_original"}, dict(
            _BOOTSTRAP_LOCK=lock, original_paths=lambda: (broker, fleet), Path=Path,
            sys=SimpleNamespace(path=paths),
            _require=lambda valid, code: self.assertTrue(valid, code)))
        with mock.patch.dict(sys.modules, {"framebridge_linux_chatgpt": package}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(namespace["bootstrap_original"])
                try:
                    self.assertTrue(started.wait(1))
                    second = pool.submit(lock.watched, namespace["bootstrap_original"])
                    self.assertTrue(lock.waiting.wait(1))
                    self.assertFalse(second.done())
                finally:
                    release.set()
                first.result(1)
                second.result(1)
        self.assertEqual(paths.count(str(broker)), 1)
        self.assertEqual(paths.count(str(fleet)), 1)
        self.assertCountEqual(package.__path__, [str(root / "framebridge_linux_chatgpt")
                                                 for root in (broker, fleet)])


    def broker_namespace(self, config):
        def validate(url):
            if url == "invalid-fixture":
                raise ValueError("fixture endpoint rejected")
            return url

        return definitions("broker.py", {"configure", "_settings"}, dict(
            _state=None, _url=None, _control_path=None,
            _SETTINGS_LOCK=ObservedRLock(), Path=Path,
            state_path=lambda state=None: Path(state or "/fixture/default"),
            _config=config, validate_endpoint=validate))

    def test_broker_initial_settings_are_published_as_one_complete_profile(self):
        started, release = threading.Event(), threading.Event()
        loads = []

        def config(state):
            loads.append(state)
            started.set()
            if not release.wait(2):
                raise AssertionError("fixture config was not released")
            return {"ports": {"broker": 23456}}

        namespace = self.broker_namespace(config)
        lock = namespace["_SETTINGS_LOCK"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(namespace["_settings"])
            try:
                self.assertTrue(started.wait(1))
                self.assertEqual(tuple(namespace[key] for key in ("_state", "_url", "_control_path")),
                                 (None, None, None))
                second = pool.submit(lock.watched, namespace["_settings"])
                self.assertTrue(lock.waiting.wait(1))
                self.assertFalse(second.done())
            finally:
                release.set()
            expected = (Path("/fixture/default"), "http://127.0.0.1:23456",
                        Path("/fixture/default/broker/control.key"))
            self.assertEqual(first.result(1), expected)
            self.assertEqual(second.result(1), expected)
        self.assertEqual(loads, [Path("/fixture/default")])

    def test_broker_explicit_profile_change_cannot_expose_mixed_endpoint_or_key_path(self):
        started, release = threading.Event(), threading.Event()

        def config(state):
            if state == Path("/fixture/next"):
                started.set()
                if not release.wait(2):
                    raise AssertionError("fixture config was not released")
            return {"ports": {"broker": 23456}}

        namespace = self.broker_namespace(config)
        previous = namespace["_settings"]()
        lock = namespace["_SETTINGS_LOCK"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(namespace["configure"], "https://fixture.example:24443",
                                "/fixture/next", "/fixture/next/explicit-control.key")
            try:
                self.assertTrue(started.wait(1))
                self.assertEqual(tuple(namespace[key] for key in ("_state", "_url", "_control_path")),
                                 previous)
                second = pool.submit(lock.watched, namespace["_settings"])
                self.assertTrue(lock.waiting.wait(1))
                self.assertFalse(second.done())
            finally:
                release.set()
            first.result(1)
            self.assertEqual(second.result(1), (Path("/fixture/next"),
                             "https://fixture.example:24443", Path("/fixture/next/explicit-control.key")))

    def test_broker_failed_validation_publishes_nothing_and_can_retry(self):
        namespace = self.broker_namespace(lambda state: {})
        with self.assertRaisesRegex(ValueError, "fixture endpoint rejected"):
            namespace["configure"]("invalid-fixture", "/fixture/rejected")
        self.assertEqual(tuple(namespace[key] for key in ("_state", "_url", "_control_path")),
                         (None, None, None))
        previous = namespace["_settings"]()
        with self.assertRaisesRegex(ValueError, "fixture endpoint rejected"):
            namespace["configure"]("invalid-fixture", "/fixture/rejected")
        self.assertEqual(namespace["_settings"](), previous)


if __name__ == "__main__":
    unittest.main()
