"""Local behavioral checks; actual desktop/tmux certification is separate."""
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
PREFIX = Path(os.environ.get("FRAMEBRIDGE_TEST_PREFIX", BASE / "package9/usr/lib/framebridge-expanded"))
BASELINE = Path(os.environ.get("FRAMEBRIDGE_TEST_BASELINE", BASE / "package8/usr/lib/framebridge-expanded"))
if not (PREFIX / "expanded/framebridge_expanded").is_dir():
    PREFIX = BASE / "source" if (BASE / "source/expanded/framebridge_expanded").is_dir() else BASE
sys.path.insert(0, str(PREFIX / "expanded"))
from framebridge_expanded import viewer


class Profile(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="framebridge-viewer-qa-")
        self.state = Path(self.temporary.name) / "profile"
        self.root = self.state / "home/.local/share/framebridge/fbterm"
        self.root.mkdir(parents=True, mode=0o700)
        self.state.chmod(0o700)
        runtime_name = "fbo-" + str(os.getuid()) + "-" + hashlib.sha256(str(self.state).encode()).hexdigest()[:16]
        self.runtime = Path("/tmp") / runtime_name
        (self.runtime / "tmux").mkdir(parents=True, mode=0o700)
        self.runtime.chmod(0o700)
        self.env = dict(os.environ, FRAMEBRIDGE_EXPANDED_STATE=str(self.state),
                        FRAMEBRIDGE_EXPANDED_PREFIX=str(PREFIX), FBTERM_ROOT=str(self.root),
                        FBTERM_SESSION=viewer.SESSION, FBTERM_TITLE=viewer.TITLE,
                        TMUX_TMPDIR=str(self.runtime / "tmux"),
                        FRAMEBRIDGE_FBTERM_ENGINE=str(PREFIX / "original/workstation/fbterm/bin/fbterm_engine.py"))
        (self.root / "bin").symlink_to(PREFIX / "original/workstation/fbterm/bin", target_is_directory=True)
        (self.root / "queue-v2").mkdir(mode=0o700)
        self.db = self.root / "queue-v2/jobs.sqlite3"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT, state TEXT)")
        self.db.chmod(0o600)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
        shutil.rmtree(self.runtime)
        self.temporary.cleanup()

    def insert(self, *states):
        with sqlite3.connect(self.db) as conn:
            conn.executemany("INSERT INTO jobs VALUES (?, ?)", [(str(i), state) for i, state in enumerate(states)])

    def endpoint_path(self):
        path = self.runtime / "tmux" / ("tmux-" + str(os.getuid()))
        path.mkdir(mode=0o700)
        return path / "default"

    def worker(self):
        process = subprocess.Popen(["bash", str(self.root / "bin/fbterm-shell")], env=self.env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 3
        while not (self.root / "dispatch.fifo").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(process.poll(), "Original idle worker must remain alive")
        self.assertTrue((self.root / "dispatch.fifo").exists())
        return process

    def mock_session(self, stack, *, windows=0, readonly=0, commands=None):
        tmux = ["tmux", "-S", str(self.endpoint_path())]
        stack.enter_context(patch.object(viewer, "_tmux", return_value=tmux))
        stack.enter_context(patch.object(viewer, "_session", return_value=True))
        stack.enter_context(patch.object(viewer, "_worker_present", return_value=True))
        stack.enter_context(patch.object(viewer, "_viewer_state", return_value=(windows, readonly, False)))
        def run(arguments, *args, **kwargs):
            if commands is not None:
                commands.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        stack.enter_context(patch.object(viewer, "_run", side_effect=run))
        def launch(desktop):
            if commands is not None:
                commands.append(["unified-viewer", desktop["FRAMEBRIDGE_EXPANDED_STATE"]])
        stack.enter_context(patch.object(viewer, "_launch_unified", side_effect=launch))
        return tmux

    def test_branded_window_recognizes_only_same_profile_legacy_title(self):
        old=viewer.profile_title(self.state, viewer.LEGACY_TITLE)
        foreign=viewer.profile_title(self.state.with_name("other-profile"),viewer.LEGACY_TITLE)
        new=viewer.profile_title(self.state,viewer.TITLE)
        env=dict(self.env,FBTERM_TITLE=new)
        inventory="0x1 0 1 gnome-terminal-server.Gnome-terminal host "+old+"\n0x2 0 2 gnome-terminal-server.Gnome-terminal host "+foreign+"\n"
        with patch.object(viewer,"_owned_window_pid",return_value=[b"gnome-terminal"]), patch.object(viewer,"_run",side_effect=[
            subprocess.CompletedProcess([],0,inventory,""),
            subprocess.CompletedProcess([],0,"",""),
        ]):
            self.assertEqual(viewer._viewer_state(["tmux"],env,env),(1,0,True))
        self.assertTrue(new.startswith(">framebridge_"))

    def test_same_base_title_is_scoped_to_profile(self):
        other = dict(self.env, FRAMEBRIDGE_EXPANDED_STATE=str(self.state.with_name("other-profile")))
        title = viewer._title(self.env)
        self.assertNotEqual(title, viewer._title(other))
        self.assertEqual(title, viewer.profile_title(self.state, title))
        self.assertNotIn(str(self.state), title)
        self.assertTrue(title.startswith(viewer.TITLE + " ["))
        # A foreign profile's window is not counted; both may coexist.
        inventory = "0x1 0 1 gnome-terminal-server.Gnome-terminal host " + viewer._title(other) + "\n"
        with patch.object(viewer, "_owned_window_pid", return_value=[b"gnome-terminal"]), patch.object(viewer, "_run", side_effect=[
            subprocess.CompletedProcess([], 0, inventory, ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]):
            self.assertEqual(viewer._viewer_state(["tmux"], self.env, self.env), (0, 0, False))
        inventory += "0x2 0 1 gnome-terminal-server.Gnome-terminal host " + title + "\n"
        with patch.object(viewer, "_owned_window_pid", return_value=[b"gnome-terminal"]), patch.object(viewer, "_run", side_effect=[
            subprocess.CompletedProcess([], 0, inventory, ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]):
            self.assertEqual(viewer._viewer_state(["tmux"], self.env, self.env), (1, 0, True))

    def test_exact_title_has_profile_bound_window_class_and_process(self):
        from framebridge_expanded.unified_viewer import window_instance
        own = window_instance(self.state)
        other = window_instance(self.state.with_name("other-profile"))
        inventory = (
            "0x1 0 11 " + own + ".FrameBridgeFBTERM host >framebridge_\n"
            "0x2 0 12 " + other + ".FrameBridgeFBTERM host >framebridge_\n"
            "0x3 0 13 " + own + ".FrameBridgeFBTERM host >framebridge_\n")
        args = [b"python3", b"-B", b"-m", b"framebridge_expanded.unified_viewer",
                b"--profile", os.fsencode(self.state)]
        with patch.object(viewer, "_owned_window_pid", side_effect=[args, [b"unrelated"]]), \
                patch.object(viewer, "_run", return_value=subprocess.CompletedProcess([],0,inventory,"")):
            self.assertEqual(viewer.window_inventory(self.env,self.env),[("0x1","unified")])

    def test_pause_persists_and_prevents_both_launch_and_bootstrap(self):
        self.insert("completed", "outcome_unknown")
        before = self.db.read_bytes()
        self.assertFalse(viewer.pause_control(self.state, "status")["paused"])
        self.assertTrue(viewer.pause_control(self.state, "pause")["paused"])
        self.assertTrue(viewer.pause_control(self.state, "pause")["paused"])
        self.assertEqual((self.root / "viewer-paused").stat().st_mode & 0o777, 0o600)
        new = viewer.Supervisor(self.state, self.env)
        with patch.object(viewer, "_tmux") as tmux, patch.object(viewer, "desktop_environment", return_value=self.env):
            self.assertEqual(new.tick(), {"code": "viewer_paused"})
            self.assertEqual(viewer.open_existing(self.root, viewer.SESSION, self.env, desktop=self.env),
                             {"code": "viewer_paused"})
            with self.assertRaisesRegex(viewer.Waiting, "viewer_paused"):
                viewer.bootstrap(self.env)
            tmux.assert_not_called()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(viewer.pause_control(self.state, "resume")["paused"])
        self.assertFalse(viewer.pause_control(self.state, "resume")["paused"])
        self.assertFalse((self.root / "viewer-paused").exists())
        self.assertEqual(self.db.read_bytes(), before)

    def test_unsafe_pause_marker_fails_closed_without_following_link(self):
        outside = self.state / "outside-marker"
        outside.write_text("untouched")
        marker = self.root / "viewer-paused"
        marker.symlink_to(outside)
        with self.assertRaisesRegex(viewer.Waiting, "unsafe_viewer_pause_record"):
            viewer.pause_control(self.state, "resume")
        with self.assertRaisesRegex(viewer.Waiting, "unsafe_viewer_pause_record"):
            viewer.open_existing(self.root, viewer.SESSION, self.env, desktop=self.env)
        self.assertEqual(outside.read_text(), "untouched")
        self.assertTrue(marker.is_symlink())

    def test_queue_observation_is_read_only_and_does_not_create_missing_database(self):
        self.insert("outcome_unknown", "queued", "in_flight", "completed")
        before = self.db.read_bytes()
        self.assertEqual(viewer.queue_counts(self.root), {"outcome_unknown": 1, "queued": 1, "in_flight": 1})
        self.assertEqual(before, self.db.read_bytes())
        self.db.unlink()
        self.assertIsNone(viewer.queue_counts(self.root))
        self.assertFalse(self.db.exists())

    def test_header_reports_observations_without_claiming_unknown_is_blocked(self):
        self.insert("outcome_unknown", "in_flight", "queued")
        commands = []
        with contextlib.ExitStack() as stack:
            tmux = self.mock_session(stack, windows=1, readonly=1, commands=commands)
            result = viewer.configure_header(self.root, viewer.SESSION, self.env, tmux)
        self.assertTrue(result["execution_state_verified"])
        self.assertEqual(len(commands), 1, "Header refresh is one bounded tmux operation")
        text = " ".join(commands[0])
        self.assertIn("Running: 1 | Queued: 1 | Unconfirmed outcomes: 1", text)
        self.assertIn("Read only", text)
        self.assertNotIn("blocked", text.lower())
        self.assertNotIn("=" + viewer.SESSION, text, "tmux option target does not accept equals prefix")

    def test_default_header_refresh_does_not_change_visibility(self):
        commands = []
        with contextlib.ExitStack() as stack:
            tmux = self.mock_session(stack, windows=1, readonly=1, commands=commands)
            viewer.configure_header(self.root, viewer.SESSION, self.env, tmux)
        self.assertEqual(len(commands), 1)
        self.assertNotIn("status", commands[0])
        self.assertIn("status-format[0]", commands[0])

    def test_explicit_legacy_and_embedded_header_modes(self):
        endpoint = self.endpoint_path()
        for embedded, expected in ((False, "3"), (True, "off")):
            with self.subTest(embedded=embedded):
                commands = []
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(self, "endpoint_path", return_value=endpoint))
                    tmux = self.mock_session(stack, commands=commands)
                    viewer.configure_header(self.root, viewer.SESSION, self.env, tmux, embedded=embedded)
                args = commands[0]
                self.assertEqual(args[args.index("status") + 1], expected)

    def test_only_verified_single_legacy_viewer_enables_status(self):
        endpoint = self.endpoint_path()
        for windows, readonly, legacy, expected in (
            (1, 1, True, "3"), (1, 1, False, "off"),
            (2, 2, True, "off"), (1, 0, True, "off"),
            (0, 1, False, "off"),
        ):
            with self.subTest(windows=windows, readonly=readonly, legacy=legacy):
                commands = []
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(self, "endpoint_path", return_value=endpoint))
                    self.mock_session(stack, windows=windows, readonly=readonly, commands=commands)
                    stack.enter_context(patch.object(viewer, "_viewer_state", return_value=(windows, readonly, legacy)))
                    if not windows or not readonly:
                        with self.assertRaisesRegex(viewer.Waiting, "incomplete_no_duplicate"):
                            viewer.open_existing(self.root, viewer.SESSION, self.env, desktop=self.env)
                    else:
                        viewer.open_existing(self.root, viewer.SESSION, self.env, desktop=self.env)
                args = commands[0]
                self.assertEqual(args[args.index("status") + 1], expected)

    def test_minimized_present_window_is_never_raised_replaced_or_relaunched(self):
        commands = []
        with contextlib.ExitStack() as stack:
            self.mock_session(stack, windows=1, readonly=1, commands=commands)
            result = viewer.open_existing(self.root, viewer.SESSION, self.env, desktop=self.env)
        self.assertEqual(result["code"], "viewer_present")
        self.assertFalse(any(command[0] in ("unified-viewer", "gnome-terminal", "wmctrl", "xdotool") for command in commands))

    def test_readonly_explicit_socket_and_shared_pending_reservation_prevent_duplicate(self):
        commands = []
        with contextlib.ExitStack() as stack:
            tmux = self.mock_session(stack, commands=commands)
            first = viewer.open_existing(self.root, viewer.SESSION, self.env, desktop=self.env)
            second = viewer.open_existing(self.root, viewer.SESSION, self.env, desktop=self.env)
        launches = [command for command in commands if command[0] == "unified-viewer"]
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0], ["unified-viewer", str(self.state)])
        self.assertNotIn("bash", launches[0])
        self.assertEqual(first["code"], "viewer_launch_pending")
        self.assertEqual(second["code"], "viewer_launch_pending")

    def test_partial_viewer_state_never_opens_duplicate(self):
        commands = []
        with contextlib.ExitStack() as stack:
            self.mock_session(stack, windows=1, readonly=0, commands=commands)
            with self.assertRaisesRegex(viewer.Waiting, "incomplete_no_duplicate"):
                viewer.open_existing(self.root, viewer.SESSION, self.env, desktop=self.env)
        self.assertFalse(any(command[0] in ("unified-viewer", "gnome-terminal") for command in commands))

    def test_symlink_socket_and_wrong_profile_runtime_are_rejected(self):
        socket_path = self.endpoint_path()
        socket_path.symlink_to("/dev/null")
        with self.assertRaisesRegex(viewer.Waiting, "unsafe_tmux_socket"):
            viewer._tmux(self.env)
        changed = dict(self.env, TMUX_TMPDIR=str(self.runtime.parent / "unrelated-profile"))
        with self.assertRaisesRegex(viewer.Waiting, "unexpected_profile_runtime"):
            viewer._tmux(changed)

    def test_login_after_headless_boot_creates_worker_once_and_later_loss_waits(self):
        supervisor = viewer.Supervisor(self.state, self.env)
        with patch.object(viewer, "desktop_environment", return_value=None), patch.object(viewer, "_run") as run:
            with self.assertRaisesRegex(viewer.Waiting, "validated_desktop_absent"):
                supervisor.tick()
            run.assert_not_called()
        self.assertFalse(supervisor.bootstrap_attempted)
        with patch.object(viewer, "desktop_environment", return_value=self.env), \
                patch.object(viewer, "_tmux", side_effect=viewer.Waiting("session_socket_absent")), \
                patch.object(viewer, "_run", return_value=subprocess.CompletedProcess([], 0, '{"attempted":true}', "")) as run, \
                patch.object(viewer, "open_existing", return_value={"code": "viewer_opened"}):
            self.assertEqual(supervisor.tick()["code"], "viewer_opened")
            with self.assertRaisesRegex(viewer.Waiting, "recovery_required"):
                supervisor.tick()
            self.assertEqual(run.call_count, 1)

    def test_recreated_profile_stops_supervision_before_any_attach(self):
        supervisor = viewer.Supervisor(self.state, self.env)
        self.state.rename(self.state.with_name("retired-profile"))
        self.root.mkdir(parents=True, mode=0o700)
        self.state.chmod(0o700)
        with patch.object(viewer, "desktop_environment") as desktop:
            with self.assertRaisesRegex(viewer.Waiting, "profile_replaced"):
                supervisor.tick()
            desktop.assert_not_called()
        self.assertTrue(supervisor.stopped.is_set())

    def test_bootstrap_defers_for_queued_work_controller_and_worker_owners(self):
        with patch.object(viewer, "desktop_environment", return_value=self.env):
            for name in ("controller.lock", "worker.lock"):
                with self.subTest(lock=name), viewer._lock(self.root / name):
                    with self.assertRaisesRegex(viewer.Waiting, "busy"):
                        viewer.bootstrap(self.env)
            self.insert("queued")
            with self.assertRaisesRegex(viewer.Waiting, "execution_pending_no_bootstrap"):
                viewer.bootstrap(self.env)

    def test_fifo_live_owner_prevents_bootstrap_without_reading_or_draining(self):
        fifo = self.root / "dispatch.fifo"
        os.mkfifo(fifo, 0o600)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(fd, b"existing-record\n")
            with self.assertRaisesRegex(viewer.Waiting, "dispatch_fifo_has_owner"):
                viewer._idle_fifo(self.root)
            self.assertEqual(os.read(fd, 100), b"existing-record\n")
        finally:
            os.close(fd)
        viewer._idle_fifo(self.root)

    def test_kernel_fifo_probe_rejects_writer_without_reader_and_preserves_buffered_bytes(self):
        fifo = self.root / "dispatch.fifo"
        os.mkfifo(fifo, 0o600)
        original = fifo.stat()
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(reader)
        try:
            with self.assertRaisesRegex(viewer.Waiting, "dispatch_fifo_has_owner_or_data"):
                viewer._idle_fifo(self.root)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            try:
                os.write(writer, b"preserve-existing-record\n")
            finally:
                os.close(reader)
            with self.assertRaisesRegex(viewer.Waiting, "dispatch_fifo_has_pending_data"):
                viewer._idle_fifo(self.root)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            try:
                self.assertEqual(os.read(reader, 100), b"preserve-existing-record\n")
            finally:
                os.close(reader)
        finally:
            os.close(writer)
        viewer._idle_fifo(self.root)
        current = fifo.stat()
        self.assertEqual((original.st_dev, original.st_ino), (current.st_dev, current.st_ino))

    def test_kernel_fifo_probe_rejects_inode_substitution_after_open(self):
        fifo = self.root / "dispatch.fifo"
        other = self.root / "unrelated.fifo"
        os.mkfifo(fifo, 0o600)
        os.mkfifo(other, 0o600)
        actual_open = os.open
        def substituted(path, flags, *args, **kwargs):
            if Path(path) == fifo and flags & os.O_ACCMODE == os.O_RDONLY:
                return actual_open(other, flags, *args, **kwargs)
            return actual_open(path, flags, *args, **kwargs)
        with patch.object(viewer.os, "open", side_effect=substituted):
            with self.assertRaisesRegex(viewer.Waiting, "dispatch_fifo_changed"):
                viewer._idle_fifo(self.root)

    def test_guarded_bootstrap_starts_original_idle_worker_without_replaying_unknown_job(self):
        self.insert("outcome_unknown")
        jobs = self.root / "jobs/old-uncertain-task"
        jobs.mkdir(parents=True)
        marker = self.state / "must-not-exist"
        (jobs / "command.bash").write_text("touch " + str(marker) + "\n")
        (jobs / "dispatch.pending").write_text("old-dispatch-token")
        evidence = {file.name: file.read_bytes() for file in jobs.iterdir()}
        database_before = self.db.read_bytes()
        test = self
        class Original:
            def __init__(self, root, session, title):
                test.assertEqual((root, session, title), (test.root, viewer.SESSION, viewer.profile_title(test.state)))
            def ensure(self, visible):
                test.assertFalse(visible)
                with test.assertRaises(viewer.Waiting), viewer._lock(test.root / "controller.lock"):
                    pass
                with viewer._lock(test.root / "worker.lock"):
                    pass
                test.worker()  # actual unchanged Original serve(), isolated FIFO
        module = types.SimpleNamespace(FBTerm=Original)
        specification = types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda module: None))
        with patch.object(viewer, "desktop_environment", return_value=self.env), \
                patch.object(viewer.importlib.util, "spec_from_file_location", return_value=specification), \
                patch.object(viewer.importlib.util, "module_from_spec", return_value=module):
            self.assertEqual(viewer.bootstrap(self.env), {"code": "original_idle_worker_started", "attempted": True})
        time.sleep(0.1)
        self.assertFalse(marker.exists())
        self.assertEqual(database_before, self.db.read_bytes())
        self.assertEqual(evidence, {file.name: file.read_bytes() for file in jobs.iterdir()})
        with self.assertRaises(viewer.Waiting), viewer._lock(self.root / "worker.lock"):
            pass

    def test_original_worker_identity_is_verified_from_its_real_process(self):
        process = self.worker()
        response = subprocess.CompletedProcess([], 0, f"0\t{process.pid}\n", "")
        with patch.object(viewer, "_run", return_value=response):
            self.assertTrue(viewer._worker_present(self.root, ["tmux"], self.env))
            self.assertFalse(viewer._worker_present(self.root / "other-profile", ["tmux"], self.env))

    def test_supervisor_failure_is_isolated_and_event_interrupts_idle_wait(self):
        supervisor = viewer.Supervisor(self.state, self.env, interval=60)
        output = io.StringIO()
        with patch.object(supervisor, "tick", side_effect=RuntimeError("private-data-not-for-output")), \
                contextlib.redirect_stdout(output):
            supervisor.start()
            deadline = time.monotonic() + 2
            while not output.getvalue() and time.monotonic() < deadline:
                time.sleep(0.01)
            before = time.monotonic()
            supervisor.close()
        self.assertFalse(supervisor.thread.is_alive())
        self.assertLess(time.monotonic() - before, 1)
        self.assertIn("viewer_unavailable", output.getvalue())
        self.assertNotIn("private-data", output.getvalue())

    def test_display_refresh_changes_only_its_copy_and_rejects_other_user(self):
        from framebridge_expanded import display_context
        before = dict(self.env)
        def prepare(env):
            env["DISPLAY"] = ":new-test-display"
        with patch.object(display_context, "prepare", side_effect=prepare), \
                patch.object(display_context, "check", return_value={"kind": "virtual-x11", "explicit": True}):
            selected = viewer.desktop_environment(self.env)
        self.assertEqual(self.env, before)
        self.assertEqual(selected["DISPLAY"], ":new-test-display")
        with patch.object(display_context, "prepare"), patch.object(display_context, "check", return_value={"kind": "physical-x11", "uid": os.getuid() + 1}):
            self.assertIsNone(viewer.desktop_environment(self.env))

    def test_original_dispatch_logic_is_unchanged_except_english_error_literals(self):
        paths = [prefix / "original/workstation/fbterm/bin/fbterm_engine.py" for prefix in (BASELINE, PREFIX)]
        if not paths[0].is_file():
            self.skipTest("Set FRAMEBRIDGE_TEST_BASELINE to verify the unchanged Original dispatch source")
        def methods(path):
            tree = ast.parse(path.read_text())
            class EraseMessages(ast.NodeTransformer):
                def visit_Constant(self, node):
                    if isinstance(node.value, str):
                        node.value = {"no se pudo auditar jobs previos: ": "could not audit previous jobs: ",
                                      "resultado durable no aparecio": "durable result did not appear",
                                      "no se pudo verificar el resultado durable": "could not verify the durable result"}.get(node.value, node.value)
                    return node
            tree = EraseMessages().visit(tree)
            cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FBTerm")
            return {node.name: ast.dump(node, include_attributes=False) for node in cls.body
                    if isinstance(node, ast.FunctionDef) and node.name not in ("ensure", "_set_viewer_header")}
        self.assertEqual(methods(paths[0]), methods(paths[1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
