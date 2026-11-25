"""Profile-owned supervision of the Original, read-only FBTERM viewer.

The viewer never submits, repeats, reconciles or updates a job. A single guarded
bootstrap may create the Original idle FIFO worker after graphical login. Later
loss of that worker requires recovery by the execution layer or an operator.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import array
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import select
import socket
import sqlite3
import stat
import subprocess
import sys
import termios
import threading
import time

TITLE = ">framebridge_ · FrameBridge Expanded"
LEGACY_TITLE = "FrameBridge Expanded"
SESSION = "framebridge-original-fbterm"


class Waiting(Exception):
    """A viewer prerequisite is absent; execution services remain independent."""


def profile_title(state, title=TITLE):
    """Stable display identity, not the profile path or an execution identity."""
    state = Path(state)
    if not state.is_absolute() or str(state) != os.path.normpath(str(state)):
        raise Waiting("unexpected_profile")
    suffix = " [" + hashlib.sha256(os.fsencode(state)).hexdigest()[:16] + "]"
    if not isinstance(title, str) or not title.isprintable():
        raise Waiting("invalid_viewer_title")
    if not title.endswith(suffix):
        title += suffix
    if len(title) > 128:
        raise Waiting("invalid_viewer_title")
    return title


def _title(env):
    return profile_title(env["FRAMEBRIDGE_EXPANDED_STATE"], env.get("FBTERM_TITLE") or TITLE)


def _run(arguments, env, timeout=2):
    return subprocess.run(arguments, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=timeout)


def _directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise Waiting("unsafe_profile_directory")
    return info.st_dev, info.st_ino


def _profile(env):
    state = Path(env["FRAMEBRIDGE_EXPANDED_STATE"])
    root = Path(env["FBTERM_ROOT"])
    temporary = Path(env["TMUX_TMPDIR"])
    if (not state.is_absolute() or not root.is_absolute()
            or root != state / "home/.local/share/framebridge/fbterm"):
        raise Waiting("unexpected_profile")
    runtime_name = "fbo-" + str(os.getuid()) + "-" + hashlib.sha256(str(state).encode()).hexdigest()[:16]
    allowed = (Path("/run/user") / str(os.getuid()) / runtime_name / "tmux",
               Path("/tmp") / runtime_name / "tmux")
    if temporary not in allowed or env.get("FBTERM_SESSION") != SESSION:
        raise Waiting("unexpected_profile_runtime")
    identity = tuple(_directory(path) for path in (state, root, temporary.parent, temporary))
    return state, root, temporary, identity


@contextmanager
def _lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise Waiting("unsafe_viewer_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Waiting("viewer_or_worker_busy") from None
        yield fd
    finally:
        os.close(fd)


def _paused(root):
    path = Path(root) / "viewer-paused"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise Waiting("unsafe_viewer_pause_record")
    return True


def pause_control(state, action):
    """Explicit local control of automatic viewer reopening; no job effects."""
    state = Path(state)
    root = state / "home/.local/share/framebridge/fbterm"
    if not state.is_absolute() or str(state) != os.path.normpath(str(state)):
        raise Waiting("unexpected_profile")
    for path in (state, root):
        _directory(path)
    if action not in ("status", "pause", "resume"):
        raise ValueError("invalid_viewer_action")
    if action == "status":
        return {"ok": True, "paused": _paused(root), "execution_unchanged": True}
    with _lock(root / "viewer.lock"):
        paused = _paused(root)
        path = root / "viewer-paused"
        if action == "pause" and not paused:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            finally:
                os.close(fd)
        elif action == "resume" and paused:
            path.unlink()
        # Persist directory entry across normal restart.
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"ok": True, "paused": _paused(root), "execution_unchanged": True}


def desktop_environment(environment):
    """Validate the current desktop without changing the runtime environment."""
    from . import display_context as display
    env = dict(environment)
    try:
        display.prepare(env)
        context = display.check(env)
        if context.get("kind") == "virtual-x11":
            return env if context.get("explicit") is True else None
        uid = os.getuid()
        if (context.get("kind") != "physical-x11"
                or type(context.get("uid")) is not int or context["uid"] != uid):
            return None
        runtime = Path("/run/user") / str(uid)
        _directory(runtime)
        bus = runtime / "bus"
        info = bus.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid:
            return None
        env.update(HOME=pwd.getpwuid(uid).pw_dir, XDG_RUNTIME_DIR=str(runtime),
                   DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(bus))
        return env
    except (OSError, ValueError, TypeError, KeyError, Waiting, display.DisplayContextError):
        return None


def _tmux(env):
    _, _, temporary, _ = _profile(env)
    directory = temporary / ("tmux-" + str(os.getuid()))
    try:
        _directory(directory)
        endpoint = directory / "default"
        info = endpoint.lstat()
    except FileNotFoundError:
        raise Waiting("session_socket_absent") from None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise Waiting("unsafe_tmux_socket")
    return ["tmux", "-S", str(endpoint)]


def _session(tmux, env):
    return _run(tmux + ["has-session", "-t", "=" + SESSION], env).returncode == 0


def queue_counts(root):
    """Read the existing queue without creating a database or changing rows."""
    database = root / "queue-v2/jobs.sqlite3"
    try:
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            connection.execute("PRAGMA query_only=ON")
            return dict(connection.execute(
                "SELECT state, COUNT(*) FROM jobs WHERE state IN "
                "('in_flight','running','queued','outcome_unknown') GROUP BY state"))
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return None


def configure_header(root, session, env, tmux=None, *, embedded=None):
    """Refresh header text. Only an explicit layout decision may change visibility.

    The Original controller uses the default and must never re-enable an
    embedded status bar. The validated viewer supervisor owns that decision.
    """
    from . import __version__
    if session != SESSION:
        raise Waiting("unexpected_viewer_session")
    tmux = _tmux(env) if tmux is None else tmux
    counts = queue_counts(Path(root))
    if counts is None:
        activity = "Execution state not verified"
    else:
        active = counts.get("in_flight", 0) + counts.get("running", 0)
        activity = (f"Running: {active} | Queued: {counts.get('queued', 0)} | "
                    f"Unconfirmed outcomes: {counts.get('outcome_unknown', 0)}")
    # Escape tmux format markers and terminal controls in externally set names.
    clean = lambda text: "".join(c for c in text if c.isprintable()).replace("#", "##")
    display_title = _title(env)
    if not display_title.startswith(">framebridge_"):
        display_title = ">framebridge_ · " + display_title
    lines = [f"{clean(display_title)} | {clean(socket.gethostname())} | {clean(__version__)}",
             activity,
             f"Read only | Updated {datetime.now().astimezone():%H:%M:%S %Z} | GPL-3.0-only | Copyleft"]
    commands = [["set-option", "-t", session, f"status-format[{i}]", value]
                for i, value in enumerate(lines)]
    if embedded is not None:
        if type(embedded) is not bool:
            raise ValueError("invalid_embedded_layout")
        commands += [["set-option", "-t", session, "status", "off" if embedded else "3"]]
    commands += [["set-option", "-t", session, name, value] for name, value in
                 (("status-position", "top"), ("status-style", "bg=colour236,fg=colour252"))]
    commands += [["set-option", "-w", "-t", session + ":0", name, value] for name, value in
                 (("pane-border-status", "off"), ("window-style", "bg=colour234,fg=colour252"),
                  ("window-active-style", "bg=colour234,fg=colour252"))]
    arguments = list(tmux)
    for command in commands:
        if len(arguments) > len(tmux):
            arguments.append(";")
        arguments.extend(command)
    if _run(arguments, env).returncode:
        raise Waiting("header_update_failed")
    return {"execution_state_verified": counts is not None, "counts": counts}


def _owned_window_pid(pid):
    if not pid.isdigit():
        return None
    try:
        process = Path("/proc") / pid
        if process.stat().st_uid != os.getuid():
            return None
        return (process / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None


def window_inventory(env, desktop):
    """Match the exact profile, not a common window title or another desktop."""
    from .unified_viewer import window_instance
    windows = _run(["wmctrl", "-lpx"], desktop)
    if windows.returncode:
        raise Waiting("window_inventory_failed")
    state = env["FRAMEBRIDGE_EXPANDED_STATE"]
    title = _title(env)
    legacy_titles = {title, profile_title(state, TITLE), profile_title(state, LEGACY_TITLE)}
    wmclass = window_instance(state) + ".FrameBridgeFBTERM"
    matching = []
    for line in windows.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) != 6:
            continue
        wid, _, pid, cls, _, name = parts
        if not wid.startswith("0x") or not all(c in "0123456789abcdefABCDEF" for c in wid[2:]):
            continue
        if name == ">framebridge_" and cls == wmclass:
            argv = _owned_window_pid(pid)
            expected = [b"-m", b"framebridge_expanded.unified_viewer",
                        b"--profile", os.fsencode(state)]
            if argv and [x for x in argv if x][-4:] == expected:
                matching.append((wid, "unified"))
        elif name in legacy_titles and cls.lower().endswith(".gnome-terminal"):
            if _owned_window_pid(pid):
                matching.append((wid, "legacy"))
    return matching


def _viewer_state(tmux, env, desktop):
    inventory = window_inventory(env, desktop)
    matching = len(inventory)
    legacy = any(kind == "legacy" for _, kind in inventory)
    clients = _run(tmux + ["list-clients", "-F", "#{client_session}\t#{client_readonly}\t#{client_pid}"], env)
    if clients.returncode:
        raise Waiting("client_inventory_failed")
    readonly = 0
    for line in clients.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[:2] == [SESSION, "1"] and parts[2].isdigit():
            try:
                readonly += (Path("/proc") / parts[2]).stat().st_uid == os.getuid()
            except FileNotFoundError:
                pass
    return matching, readonly, legacy


def observe_window(state):
    """Read-only profile-scoped GUI observation; no launch or focus request."""
    from . import runtime, config
    try:
        env = runtime._observation_environment(state, config.load_config(state))
        desktop = desktop_environment(env)
        if desktop is None:
            return {"window_state": "desktop_unavailable", "window_count": None}
        items = window_inventory(env, desktop)
        return {"window_state": "present" if items else "absent",
                "window_count": len(items),
                "layout": items[0][1] if len(items) == 1 else None}
    except Exception:
        return {"window_state": "unverified", "window_count": None}


def _worker_present(root, tmux, env):
    panes = _run(tmux + ["list-panes", "-t", "=" + SESSION, "-F", "#{pane_dead}\t#{pane_pid}"], env)
    if panes.returncode:
        return False
    alive = False
    for line in panes.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[0] == "0" and fields[1].isdigit():
            try:
                process = Path("/proc") / fields[1]
                argv = (process / "cmdline").read_bytes().split(b"\0")
                alive |= (process.stat().st_uid == os.getuid()
                          and os.fsencode(root / "bin/fbterm_engine.py") in argv
                          and b"serve" in argv)
            except (OSError, ValueError):
                pass
    return alive


def _launch_pending(lock_fd, *, reserve=False):
    """Share a short launch grace period with per-job ensure() invocations."""
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    now = time.monotonic()
    try:
        saved = json.loads(os.pread(lock_fd, 512, 0) or b"{}")
        pending = (saved.get("boot") == boot
                   and 0 <= now - float(saved.get("started", -30)) < 20)
    except (ValueError, TypeError, AttributeError):
        pending = False
    if reserve:
        data = json.dumps({"boot": boot, "started": now}).encode("ascii")
        os.ftruncate(lock_fd, 0)
        os.pwrite(lock_fd, data, 0)
    return pending


def _launch_unified(desktop):
    # GTK/GI and VTE belong to the distribution Python ABI, not the private
    # execution interpreter. Do not import cp313 vendor extensions into the
    # GUI process on Ubuntu/Mint. Keep the exact profile and desktop binding.
    ui_env = dict(desktop)
    ui_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    ui_env["PYTHONDONTWRITEBYTECODE"] = "1"
    ui_env["PYTHONNOUSERSITE"] = "1"
    for key in ("PYTHONHOME", "PYTHONEXECUTABLE", "PYTHONUSERBASE", "LD_LIBRARY_PATH"):
        ui_env.pop(key, None)
    try:
        subprocess.Popen(["/usr/bin/python3", "-B", "-s", "-m", "framebridge_expanded.unified_viewer",
                          "--profile", desktop["FRAMEBRIDGE_EXPANDED_STATE"]],
                         env=ui_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise Waiting("viewer_launch_failed") from None


def window_action(state, action):
    """Explicit local tray action. No remote target and no execution start."""
    from . import runtime, config
    env = runtime._observation_environment(state, config.load_config(state))
    desktop = desktop_environment(env)
    if desktop is None:
        raise Waiting("validated_desktop_absent")
    if action == "open":
        pause_control(state, "resume")
        root = _profile(env)[1]
        open_existing(root, SESSION, env, desktop=desktop)
    elif action != "minimize":
        raise ValueError("invalid_viewer_action")
    inventory = window_inventory(env, desktop)
    if not inventory and action == "open":
        return "Opening FBTERM"
    if len(inventory) != 1:
        raise Waiting("viewer_window_not_unique")
    wid = inventory[0][0]
    args = ["wmctrl", "-ia", wid] if action == "open" else ["xdotool", "windowminimize", wid]
    if _run(args, desktop).returncode:
        raise Waiting("viewer_window_action_failed")
    return "FBTERM available"



def open_existing(root, session, env, *, desktop=None, cancelled=None):
    """Attach read-only under the shared lock; never create a session here."""
    _, expected_root, _, _ = _profile(env)
    if Path(root) != expected_root or session != SESSION:
        raise Waiting("unexpected_viewer_session")
    desktop = desktop_environment(env) if desktop is None else desktop
    if desktop is None:
        raise Waiting("validated_desktop_absent")
    with _lock(Path(root) / "viewer.lock") as guard:
        if _paused(root):
            return {"code": "viewer_paused"}
        tmux = _tmux(env)
        if not _session(tmux, env):
            raise Waiting("session_unavailable")
        if not _worker_present(Path(root), tmux, env):
            raise Waiting("original_worker_unavailable")
        windows, readonly, legacy = _viewer_state(tmux, env, desktop)
        # Only a single, positively identified legacy viewer may display tmux's
        # own header. A mixed or incomplete inventory cannot enable it.
        header = configure_header(root, session, env, tmux,
                                  embedded=not (legacy and windows == readonly == 1))
        if windows and readonly:
            os.ftruncate(guard, 0)
            code = "viewer_present" if windows == readonly == 1 else "viewer_duplicates_detected"
            return {"code": code, "windows": windows, "readonly_clients": readonly, **header}
        if windows or readonly:
            raise Waiting("viewer_state_incomplete_no_duplicate")
        if _launch_pending(guard):
            return {"code": "viewer_launch_pending", "windows": 0, "readonly_clients": 0, **header}
        if cancelled is not None and cancelled.is_set():
            raise Waiting("viewer_stopped")
        _launch_pending(guard, reserve=True)
        # The child validates the same profile and attaches read-only itself.
        # No shell, input, focus request or second informational window.
        _launch_unified(desktop)
        windows, readonly, legacy = _viewer_state(tmux, env, desktop)
        if windows and readonly:
            os.ftruncate(guard, 0)
        return {"code": "viewer_opened" if windows and readonly else "viewer_launch_pending",
                "windows": windows, "readonly_clients": readonly, **header}


def _idle_fifo(root):
    """Check kernel FIFO state while both cooperating protocol locks are held.

    Original execute owns controller.lock through writer open/write/close;
    Original serve owns worker.lock throughout its reader lifetime. Those locks
    exclude new protocol participants during this probe and worker handoff.
    Arbitrary same-UID code bypassing those locks is outside that guarantee.
    No bytes are read/written and the FIFO inode is never replaced or removed.
    """
    path = root / "dispatch.fifo"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise Waiting("unsafe_dispatch_fifo")
    identity = info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)

    def verify(current):
        if (not stat.S_ISFIFO(current.st_mode)
                or (current.st_dev, current.st_ino, current.st_uid, stat.S_IMODE(current.st_mode)) != identity):
            raise Waiting("dispatch_fifo_changed")

    flags = os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    # With no reader, a nonblocking write-only open must fail with ENXIO.
    # Success proves an existing reader; close our unwritten probe and defer.
    try:
        writer = os.open(path, os.O_WRONLY | flags)
    except OSError as exc:
        if exc.errno != errno.ENXIO:
            raise
        verify(path.lstat())
    else:
        try:
            verify(os.fstat(writer))
            raise Waiting("dispatch_fifo_has_owner")
        finally:
            os.close(writer)

    reader = os.open(path, os.O_RDONLY | flags)
    try:
        verify(os.fstat(reader))
        unread = array.array("i", [0])
        fcntl.ioctl(reader, termios.FIONREAD, unread, True)
        if unread[0] != 0:
            raise Waiting("dispatch_fifo_has_pending_data")
        # Linux suppresses HUP until this reader has seen a writer. Opening and
        # closing our own unwritten endpoint enables the kernel's HUP test.
        writer = os.open(path, os.O_WRONLY | flags)
        try:
            verify(os.fstat(writer))
        finally:
            os.close(writer)
        poller = select.poll()
        poller.register(reader, select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLNVAL)
        events = poller.poll(0)
        mask = 0
        for descriptor, event in events:
            if descriptor == reader:
                mask |= event
        if (not mask & select.POLLHUP
                or mask & (select.POLLIN | select.POLLERR | select.POLLNVAL)):
            raise Waiting("dispatch_fifo_has_owner_or_data")
        fcntl.ioctl(reader, termios.FIONREAD, unread, True)
        if unread[0] != 0:
            raise Waiting("dispatch_fifo_has_pending_data")
        verify(path.lstat())
    finally:
        os.close(reader)


def bootstrap(environment):
    """Run only in a short-lived child with its own copied environment."""
    _, root, _, _ = _profile(environment)
    if desktop_environment(environment) is None:
        raise Waiting("validated_desktop_absent")
    with _lock(root / "viewer.lock"), _lock(root / "controller.lock"):
        if _paused(root):
            raise Waiting("viewer_paused")
        try:
            present = _session(_tmux(environment), environment)
        except Waiting as exc:
            if str(exc) != "session_socket_absent":
                raise
            present = False
        if present:
            return {"code": "session_already_present", "attempted": False}
        counts = queue_counts(root)
        if counts is None:
            raise Waiting("execution_state_not_verified")
        if any(counts.get(name, 0) for name in ("queued", "running", "in_flight")):
            raise Waiting("execution_pending_no_bootstrap")
        # A live worker must keep its lock. Release this probe before starting
        # Original serve; the controller lock still excludes new dispatches.
        with _lock(root / "worker.lock"):
            _idle_fifo(root)
        prefix = Path(environment["FRAMEBRIDGE_EXPANDED_PREFIX"])
        engine_path = Path(environment["FRAMEBRIDGE_FBTERM_ENGINE"])
        if (not engine_path.resolve().is_relative_to((prefix / "original").resolve())
                or not engine_path.samefile(root / "bin/fbterm_engine.py")):
            raise Waiting("unexpected_original_engine")
        specification = importlib.util.spec_from_file_location("framebridge_viewer_original", engine_path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        engine = module.FBTerm(root, SESSION, _title(environment))
        # Original serve reads only newly written FIFO records. ensure(False)
        # does not enumerate job directories or repeat unfinished jobs.
        engine.ensure(visible=False)
        return {"code": "original_idle_worker_started", "attempted": True}


class Supervisor:
    """A best-effort UI lifecycle owned by exactly one profile runtime."""
    def __init__(self, state, environment, interval=5):
        self.state = Path(state)
        self.environment = dict(environment)
        actual_state, self.root, _, self.identity = _profile(self.environment)
        if self.state != actual_state:
            raise Waiting("unexpected_profile")
        self.interval = interval
        self.stopped = threading.Event()
        self.bootstrap_attempted = False
        self.thread = threading.Thread(target=self._loop, name="fbterm-viewer", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=12)

    def tick(self):
        if _profile(self.environment)[3] != self.identity:
            self.stopped.set()
            raise Waiting("profile_replaced_viewer_stopped")
        if _paused(self.root):
            return {"code": "viewer_paused"}
        desktop = desktop_environment(self.environment)
        if desktop is None:
            raise Waiting("validated_desktop_absent")
        if self.stopped.is_set():
            raise Waiting("viewer_stopped")
        try:
            present = _session(_tmux(self.environment), self.environment)
        except Waiting as exc:
            if str(exc) != "session_socket_absent":
                raise
            present = False
        if present:
            # Existing workers also consume the single bootstrap allowance.
            self.bootstrap_attempted = True
        elif not self.bootstrap_attempted:
            # Mark before spawning: timeout/crash must never trigger an automatic
            # second attempt. Proven precondition deferrals can safely retry.
            self.bootstrap_attempted = True
            result = _run([sys.executable, "-m", "framebridge_expanded.viewer", "_bootstrap"],
                          self.environment, timeout=8)
            response = json.loads(result.stdout)
            if response.get("attempted") is False:
                self.bootstrap_attempted = False
            if result.returncode:
                raise Waiting(response.get("code", "original_bootstrap_failed"))
        else:
            raise Waiting("original_session_missing_recovery_required")
        if self.stopped.is_set():
            raise Waiting("viewer_stopped")
        return open_existing(self.root, SESSION, self.environment, desktop=desktop, cancelled=self.stopped)

    def _loop(self):
        previous = None
        while not self.stopped.is_set():
            try:
                result = self.tick()
            except Waiting as exc:
                result = {"code": str(exc)}
            except Exception as exc:
                result = {"code": "viewer_unavailable", "error_type": type(exc).__name__}
            # Concise structured evidence in the profile's existing worker log.
            # Never serialize environment variables or exception text with paths.
            if result != previous:
                print("FBTERM_VIEWER " + json.dumps(result, sort_keys=True), flush=True)
                previous = result
            self.stopped.wait(self.interval)


def main():
    if sys.argv[1:] != ["_bootstrap"]:
        raise SystemExit("This module is managed by the FrameBridge profile runtime.")
    try:
        response, code = bootstrap(os.environ.copy()), 0
    except Waiting as exc:
        response, code = {"code": str(exc), "attempted": False}, 1
    except Exception as exc:
        response, code = {"code": "original_bootstrap_failed", "error_type": type(exc).__name__,
                          "attempted": True}, 1
    print(json.dumps(response, sort_keys=True), flush=True)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
