#!/usr/bin/env python3
# Modified for FrameBridge 0.3.5 on 2026-10-31:
# select the validated graphical session for the read-only FBTERM viewer.
# Original FrameBridge material remains GPL-3.0-only; see the project LICENSE.

import errno
import fcntl
import hashlib
import os
import pwd
import stat
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid


class FBTermError(RuntimeError):
    pass


class FBTermBusy(FBTermError):
    def __init__(self, message, blocker=None):
        super().__init__(message)
        self.blocker = blocker


class FBTermNotStarted(FBTermError):
    pass


class FBTermOutcomeUnknown(FBTermError):
    pass


class FBTerm:
    JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

    def __init__(self, root, session, title):
        self.root = Path(root)
        self.session = session
        self.title = title

        self.bin = self.root / "bin"
        self.jobs = self.root / "jobs"

        self.runner = self.bin / "fbterm-run"
        self.shell = self.bin / "fbterm-shell"
        self.lock_path = self.root / "controller.lock"
        self.dispatch = self.root / "dispatch.fifo"

    @staticmethod
    def _run(args, **kwargs):
        return subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )

    def has_session(self):
        p = self._run(
            ["tmux", "has-session", "-t", self.session]
        )
        return p.returncode == 0

    @staticmethod
    def _viewer_environment():
        """Select the current desktop for the read-only viewer, never for jobs."""
        env = os.environ.copy()
        try:
            from framebridge_expanded import display_context as display
        except ImportError:
            # Preserve standalone Original behavior outside the expanded package.
            if env.get("FRAMEBRIDGE_EXPANDED_STATE"):
                return None
            return env if env.get("DISPLAY") else None
        try:
            # A service may start before graphical login. Refresh only this copy;
            # the adapter's CUA binding and existing tmux shell stay unchanged.
            display.prepare(env)
            context = display.check(env)
            if not isinstance(context, dict):
                return None
            if context.get("kind") == "virtual-x11":
                return env if context.get("explicit") is True else None
            uid = os.getuid()
            if (context.get("kind") != "physical-x11"
                    or type(context.get("uid")) is not int
                    or context["uid"] != uid):
                return None
            runtime = Path("/run/user") / str(uid)
            info = runtime.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                    or info.st_mode & 0o077):
                return None
            bus = runtime / "bus"
            info = bus.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid:
                return None
            env.update({
                "HOME": pwd.getpwuid(uid).pw_dir,
                "XDG_RUNTIME_DIR": str(runtime),
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(bus),
            })
            return env
        except (OSError, ValueError, TypeError, KeyError, display.DisplayContextError):
            # No verified graphical session: retain text execution without a
            # guessed display, another user's bus, or a fabricated visible state.
            return None

    def _set_viewer_header(self):
        """Place the release identity in the original viewer's pane border."""
        try:
            from framebridge_expanded import __version__
        except ImportError:
            return
        target = self.session + ":0"
        header = (
            f" #[bold,fg=colour25]FrameBridge {__version__}#[default] | "
            "#{?#{e|>=:#{pane_width},56},GNU GPL v3 only | Copyleft,GPL-3.0-only} "
        )
        try:
            self._run(
                ["tmux", "set-option", "-w", "-t", target, "pane-border-format", header,
                 ";", "set-option", "-w", "-t", target, "pane-border-status", "top"],
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            # The viewer is usable even when tmux cannot set its pane border.
            pass

    def ensure(self, visible=True):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.jobs.mkdir(mode=0o700, parents=True, exist_ok=True)

        if not self.has_session():
            env = os.environ.copy()
            env["FBTERM_ROOT"] = str(self.root)
            env["FBTERM_SESSION"] = self.session
            env["FBTERM_TITLE"] = self.title
            env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

            p = subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    self.session,
                    "/usr/bin/env",
                    "FBTERM_ROOT=" + env["FBTERM_ROOT"],
                    "FBTERM_SESSION=" + env["FBTERM_SESSION"],
                    "FBTERM_TITLE=" + env["FBTERM_TITLE"],
                    "PATH=" + env["PATH"],
                    str(self.shell),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if p.returncode != 0:
                raise FBTermError(
                    "tmux new-session fallo: " + p.stderr.strip()
                )

            subprocess.run(
                [
                    "tmux",
                    "set-environment",
                    "-t",
                    self.session,
                    "FBTERM_ROOT",
                    str(self.root),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            subprocess.run(
                [
                    "tmux",
                    "set-environment",
                    "-t",
                    self.session,
                    "FBTERM_SESSION",
                    self.session,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            subprocess.run(
                [
                    "tmux",
                    "set-option",
                    "-t",
                    self.session,
                    "allow-rename",
                    "off",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            subprocess.run(
                [
                    "tmux",
                    "set-option",
                    "-t",
                    self.session,
                    "set-titles",
                    "off",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        viewer_env = self._viewer_environment() if visible else None
        if viewer_env is not None and not self.visible_window(viewer_env):
            self._set_viewer_header()
            try:
                subprocess.Popen(
                    [
                        "gnome-terminal",
                        "--title=" + self.title,
                        "--",
                        "bash",
                        "-lc",
                        (
                            "exec tmux attach-session -r -t "
                            + subprocess.list2cmdline([self.session])
                        ),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=viewer_env,
                )
            except OSError:
                return self.status()

            for _ in range(40):
                if self.visible_window(viewer_env):
                    break
                time.sleep(0.1)

        return self.status()

    def visible_window(self, viewer_env=None):
        env = self._viewer_environment() if viewer_env is None else viewer_env
        if env is None:
            return False
        try:
            p = self._run(["wmctrl", "-l"], env=env)
        except OSError:
            return False
        return (
            p.returncode == 0
            and self.title in p.stdout
        )

    def status(self):
        if not self.has_session():
            return {
                "session": False,
                "visible": False,
                "pane_pid": None,
                "pane_tty": None,
                "pane_command": None,
                "clients": 0,
                "readonly_clients": 0,
            }

        fmt = (
            "#{pane_pid}|#{pane_tty}|"
            "#{pane_current_command}"
        )

        p = self._run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                self.session + ":0.0",
                fmt,
            ]
        )

        parts = p.stdout.strip().split("|", 2)

        pane_pid = parts[0] if len(parts) > 0 else ""
        pane_tty = parts[1] if len(parts) > 1 else ""
        pane_command = parts[2] if len(parts) > 2 else ""

        clients = self._run(
            [
                "tmux",
                "list-clients",
                "-t",
                self.session,
                "-F",
                "#{client_readonly}",
            ]
        )

        values = [
            x.strip()
            for x in clients.stdout.splitlines()
            if x.strip()
        ]

        readonly = sum(
            1
            for x in values
            if x in ("1", "on", "true")
        )

        return {
            "session": True,
            "visible": self.visible_window(),
            "pane_pid": pane_pid,
            "pane_tty": pane_tty,
            "pane_command": pane_command,
            "clients": len(values),
            "readonly_clients": readonly,
        }

    @staticmethod
    def _sync_directory(directory):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _write_job_file(path, value):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())

    def _prepare_job(self, job, command, timeout_s, token):
        # Only complete preparations participate in the durable jobs audit.
        with tempfile.TemporaryDirectory(prefix=".job-", dir=self.root) as temporary:
            prepared = Path(temporary) / "prepared"
            prepared.mkdir(mode=0o700)
            if deadline := os.environ.get("FBTERM_START_BEFORE"):
                self._write_job_file(prepared / "start.before", deadline)
            self._write_job_file(prepared / "command.bash", command)
            self._write_job_file(prepared / "timeout.seconds", str(timeout_s) + "\n")
            self._write_job_file(prepared / "dispatch.pending", token)
            self._sync_directory(prepared)
            prepared.rename(job)
            self._sync_directory(self.jobs)

    def _open_fifo(self, flags):
        fd = os.open(self.dispatch, flags | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise FBTermError("FBTERM invalid dispatch FIFO")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_dispatch(self):
        deadline = time.monotonic() + 3
        while True:
            try:
                return self._open_fifo(os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ENXIO):
                    raise FBTermNotStarted("FBTERM dispatch unavailable") from exc
                if time.monotonic() >= deadline:
                    raise FBTermNotStarted("FBTERM worker unavailable") from exc
                time.sleep(0.02)
            except FBTermError as exc:
                raise FBTermNotStarted(str(exc)) from exc

    def _transition_pending(self, job, state):
        # The launch and the cancellation contend for the same inode. Once
        # either rename wins, the other cannot succeed, even after a timeout.
        try:
            (job / "dispatch.pending").rename(job / state)
        except FileNotFoundError:
            return False
        self._sync_directory(job)
        return True

    def _cancel_unstarted(self, job):
        if any((job / name).exists() for name in
               ("dispatch.started", "started.at", "DONE", "result.env")):
            return False
        if self._transition_pending(job, "dispatch.cancelled"):
            return True
        return (job / "dispatch.cancelled").is_file()

    def _run_dispatched_job(self, job_id, token):
        if not self.JOB_RE.fullmatch(job_id) or job_id in (".", ".."):
            return
        job = self.jobs / job_id
        try:
            pending = (job / "dispatch.pending").read_text(encoding="ascii")
        except FileNotFoundError:
            return
        if pending != token:
            return
        deadline = job / "start.before"
        if deadline.exists() and time.time() >= float(deadline.read_text()):
            self._cancel_unstarted(job)
            return
        if not self._transition_pending(job, "dispatch.started"):
            return
        # From this point an interruption leaves an uncertain job, never a
        # retryable one. Only the original runner produces a completed receipt.
        subprocess.run([str(self.runner), job_id], check=False)

    def serve(self):
        lock = self.root / "worker.lock"
        with lock.open("ab") as worker_lock:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.mkfifo(self.dispatch, 0o600)
            except FileExistsError:
                pass
            # Linux permits an O_RDWR FIFO open. Hold it while idle so there is
            # no polling and disconnecting writers do not terminate the worker.
            with os.fdopen(self._open_fifo(os.O_RDWR), "rb") as inbox:
                while record := inbox.readline(164):
                    if not record.endswith(b"\n"):
                        while record and not record.endswith(b"\n"):
                            record = inbox.readline(164)
                        continue
                    fields = record.split()
                    if len(fields) != 2 or not record.isascii():
                        continue
                    job_id, token = (field.decode("ascii") for field in fields)
                    try:
                        self._run_dispatched_job(job_id, token)
                    except (OSError, UnicodeError):
                        print("FBTERM_DISPATCH_ERROR", flush=True)

    @staticmethod
    def _wait_for_completion(job, timeout_s):
        deadline = time.monotonic() + timeout_s + 8
        while not (job / "DONE").exists():
            if (job / "dispatch.cancelled").exists() or time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def execute(self, job_id, command, timeout_s=120):
        if not self.JOB_RE.fullmatch(job_id) or job_id in (".", ".."):
            raise FBTermError("JOB_ID invalido")

        if not isinstance(command, str) or not command:
            raise FBTermError("command vacio")

        timeout_s = int(timeout_s)

        if timeout_s < 1 or timeout_s > 3600:
            raise FBTermError("timeout fuera de rango")

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.jobs.mkdir(mode=0o700, exist_ok=True)

        lock_fd = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )

        try:
            try:
                fcntl.flock(
                    lock_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                raise FBTermBusy("FBTERM ocupado")

            # R2: fail-closed si existe evidencia durable incompleta.
            # La reconciliacion conserva esos directorios fuera de jobs.
            incomplete_jobs = []

            try:
                # DirEntry reuses the directory type from readdir. Preserve
                # the full fail-closed audit and both durable-file checks;
                # avoid a separate stat and Path allocations for every job.
                with os.scandir(self.jobs) as prior_jobs:
                    for prior in prior_jobs:
                        if (
                            prior.is_dir()
                            and (
                                not os.path.exists(os.path.join(prior.path, "DONE"))
                                or not os.path.exists(os.path.join(prior.path, "result.env"))
                            )
                        ):
                            if not self._cancel_unstarted(Path(prior.path)):
                                incomplete_jobs.append(prior.name)
            except OSError as exc:
                raise FBTermError(
                    "no se pudo auditar jobs previos: " + str(exc)
                )

            if incomplete_jobs:
                sample = ", ".join(sorted(incomplete_jobs)[:3])
                raise FBTermBusy(
                    "FBTERM tiene job incompleto: " + sample, sorted(incomplete_jobs)[0]
                )

            job = self.jobs / job_id

            if job.exists():
                raise FBTermError("JOB_ID ya existe")

            try:
                self.ensure(visible=os.environ.get("FBTERM_JOB_VIEWER") != "0")
            except (FBTermError, OSError, subprocess.TimeoutExpired) as exc:
                raise FBTermNotStarted("FBTERM session unavailable") from exc
            try:
                with os.fdopen(self._open_dispatch(), "wb", buffering=0) as dispatch:
                    token = uuid.uuid4().hex
                    try:
                        self._prepare_job(job, command, timeout_s, token)
                    except (OSError, UnicodeError) as exc:
                        raise FBTermNotStarted("FBTERM job preparation failed") from exc
                    record = f"{job_id} {token}\n".encode("ascii")
                    try:
                        if dispatch.write(record) != len(record):
                            raise OSError("short dispatch write")
                    except OSError as exc:
                        if self._cancel_unstarted(job):
                            raise FBTermNotStarted("FBTERM dispatch failed before start") from exc
                        raise FBTermOutcomeUnknown("FBTERM dispatch result unknown") from exc
                if not self._wait_for_completion(job, timeout_s):
                    if self._cancel_unstarted(job):
                        raise FBTermNotStarted("FBTERM dispatch expired before start")
                    if not (job / "DONE").exists():
                        raise FBTermOutcomeUnknown("resultado durable no aparecio")
                lines = (job / "result.env").read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise FBTermOutcomeUnknown("no se pudo verificar el resultado durable") from exc

            expected_command_sha = hashlib.sha256(command.encode("utf-8")).hexdigest()

            result = {}

            for line in lines:
                if "=" in line:
                    k, v = line.split("=", 1)
                    result[k] = v

            result["EXPECTED_COMMAND_SHA256"] = (
                expected_command_sha
            )

            result["COMMAND_HASH_GATE"] = (
                "PASS"
                if result.get("COMMAND_SHA256")
                == expected_command_sha
                else "FAIL"
            )

            result["STDOUT_PATH"] = str(
                job / "stdout.bin"
            )

            result["STDERR_PATH"] = str(
                job / "stderr.bin"
            )

            return result

        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass

            os.close(lock_fd)


def file_text(path):
    return Path(path).read_text(
        encoding="utf-8",
        errors="replace",
    )


if __name__ == "__main__":
    import json
    import sys

    root = os.environ["FBTERM_ROOT"]
    session = os.environ["FBTERM_SESSION"]
    title = os.environ["FBTERM_TITLE"]

    term = FBTerm(root, session, title)

    action = sys.argv[1]

    if action == "ensure":
        print(json.dumps(term.ensure(), sort_keys=True))

    elif action == "status":
        print(json.dumps(term.status(), sort_keys=True))

    elif action == "serve":
        term.serve()

    elif action == "exec":
        job_id = sys.argv[2]
        timeout_s = int(sys.argv[3])
        command_path = sys.argv[4]

        try:
            r = term.execute(
                job_id,
                file_text(command_path),
                timeout_s,
            )
            print(json.dumps(r, sort_keys=True))
        except FBTermNotStarted as e:
            print(json.dumps({"ERROR": "NOT_STARTED", "MESSAGE": str(e)}))
            sys.exit(70)
        except FBTermOutcomeUnknown as e:
            print(json.dumps({"ERROR": "OUTCOME_UNKNOWN", "MESSAGE": str(e)}))
            sys.exit(74)
        except FBTermBusy as e:
            print(json.dumps({
                "ERROR": "BUSY",
                "REASON": "unresolved_job" if e.blocker else "controller_busy",
                "BLOCKING_JOB_ID": e.blocker,
                "MESSAGE": str(e),
            }))
            sys.exit(75)

    else:
        raise SystemExit(64)
