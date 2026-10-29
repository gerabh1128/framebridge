#!/usr/bin/env python3

import fcntl
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time


class FBTermError(RuntimeError):
    pass


class FBTermBusy(FBTermError):
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

    def ensure(self, visible=True):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.jobs.mkdir(mode=0o700, parents=True, exist_ok=True)

        if not self.has_session():
            env = os.environ.copy()
            env["FBTERM_ROOT"] = str(self.root)
            env["FBTERM_SESSION"] = self.session
            env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")

            p = subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    self.session,
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

            subprocess.run(
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    self.session + ":0.0",
                    (
                        "export FBTERM_ROOT="
                        + subprocess.list2cmdline([str(self.root)])
                        + "; export FBTERM_SESSION="
                        + subprocess.list2cmdline([self.session])
                        + "; export PATH="
                        + subprocess.list2cmdline(
                            [str(self.bin) + ":$PATH"]
                        )
                    ),
                    "C-m",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            time.sleep(0.5)

        if visible and not self.visible_window():
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
            )

            for _ in range(40):
                if self.visible_window():
                    break
                time.sleep(0.1)

        return self.status()

    def visible_window(self):
        p = self._run(["wmctrl", "-l"])
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

    def execute(self, job_id, command, timeout_s=120):
        if not self.JOB_RE.fullmatch(job_id):
            raise FBTermError("JOB_ID invalido")

        if not isinstance(command, str) or not command:
            raise FBTermError("command vacio")

        timeout_s = int(timeout_s)

        if timeout_s < 1 or timeout_s > 3600:
            raise FBTermError("timeout fuera de rango")

        self.ensure(visible=True)

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
                for prior in self.jobs.iterdir():
                    if (
                        prior.is_dir()
                        and (
                            not (prior / "DONE").exists()
                            or not (prior / "result.env").exists()
                        )
                    ):
                        incomplete_jobs.append(prior.name)
            except OSError as exc:
                raise FBTermError(
                    "no se pudo auditar jobs previos: " + str(exc)
                )

            if incomplete_jobs:
                sample = ", ".join(sorted(incomplete_jobs)[:3])
                raise FBTermBusy(
                    "FBTERM tiene job incompleto: " + sample
                )

            job = self.jobs / job_id

            if job.exists():
                raise FBTermError("JOB_ID ya existe")

            job.mkdir(mode=0o700)

            command_file = job / "command.bash"
            command_file.write_text(
                command,
                encoding="utf-8",
            )
            os.chmod(command_file, 0o600)

            (job / "timeout.seconds").write_text(
                str(timeout_s) + "\n",
                encoding="ascii",
            )

            expected_command_sha = hashlib.sha256(
                command.encode("utf-8")
            ).hexdigest()

            # R2 idle gate: prompt.ready existe solo cuando el shell
            # persistente esta realmente esperando el siguiente comando.
            ready_path = self.jobs.parent / "prompt.ready"
            ready_deadline = time.monotonic() + 3.0

            while time.monotonic() < ready_deadline:
                if ready_path.exists():
                    break
                time.sleep(0.02)
            else:
                raise FBTermBusy(
                    "FBTERM pane no esta en prompt"
                )

            try:
                ready_path.unlink()
            except FileNotFoundError:
                raise FBTermBusy(
                    "FBTERM prompt cambio antes del envio"
                )
            except OSError as exc:
                raise FBTermError(
                    "no se pudo reservar prompt: " + str(exc)
                )

            send = self._run(
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    self.session + ":0.0",
                    "fbterm-run " + job_id,
                    "C-m",
                ]
            )

            if send.returncode != 0:
                raise FBTermError(
                    "tmux send-keys fallo: "
                    + send.stderr.strip()
                )

            deadline = time.monotonic() + timeout_s + 8

            while time.monotonic() < deadline:
                if (job / "DONE").exists():
                    break
                time.sleep(0.05)

            if not (job / "DONE").exists():
                raise FBTermError(
                    "resultado durable no aparecio"
                )

            # R2: no liberar el lock hasta que bash haya vuelto realmente
            # al prompt. fbterm-shell actualiza prompt.ready mediante
            # PROMPT_COMMAND despues de que fbterm-run haya terminado.
            done_path = job / "DONE"
            prompt_deadline = time.monotonic() + 5.0
            prompt_ready = False

            while time.monotonic() < prompt_deadline:
                try:
                    if (
                        ready_path.exists()
                        and ready_path.stat().st_mtime_ns
                        > done_path.stat().st_mtime_ns
                    ):
                        prompt_ready = True
                        break
                except OSError:
                    pass

                time.sleep(0.02)

            if not prompt_ready:
                raise FBTermError(
                    "prompt durable no aparecio"
                )

            result = {}

            for line in (job / "result.env").read_text(
                encoding="utf-8"
            ).splitlines():
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
        except FBTermBusy as e:
            print(json.dumps({
                "ERROR": "BUSY",
                "MESSAGE": str(e),
            }))
            sys.exit(75)

    else:
        raise SystemExit(64)
