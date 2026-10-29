"""Per-console ordinary-sudo capability; no privilege grant or custom authentication.

Only transient user services created by this controller are stopped. Each new
console has its own tmux server and cgroup. NoNewPrivileges is checked on both
the actual server and Bash process. Callers must hold their terminal-manager
lock while changing mode so keyboard input cannot race the transition.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import pwd
import re
import signal
import shutil
import shlex
import stat
import subprocess
import threading
import time
import uuid

class SudoControlError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def _fail(code):
    raise SudoControlError(code)

def _proc(pid):
    try:
        root = Path("/proc") / str(int(pid))
        raw = (root / "stat").read_text().rsplit(") ", 1)[1].split()
        fields = {}
        for line in (root / "status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"Uid", "NoNewPrivs"}:
                fields[key] = value.split()
        try:
            executable = os.readlink(root / "exe")
        except OSError:
            executable = ""
        return {"pid": int(pid), "state": raw[0], "ppid": int(raw[1]),
                "pgrp": int(raw[2]), "sid": int(raw[3]), "tty": int(raw[4]),
                "tpgid": int(raw[5]), "birth": raw[19], "uids": tuple(map(int, fields["Uid"])),
                "nnp": int(fields["NoNewPrivs"][0]), "exe": executable}
    except (OSError, ValueError, KeyError, IndexError):
        return None

def _processes():
    result = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            item = _proc(int(path.name))
            if item and item["state"] not in {"Z", "X"}:
                result[item["pid"]] = item
    return result

def _descendants(processes, pid):
    found = set()
    while True:
        added = {p for p, info in processes.items()
                 if info["ppid"] == pid or info["ppid"] in found} - found
        if not added:
            return found
        found.update(added)

SESSION_ENVIRONMENT_KEYS = (
    "DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    "LD_LIBRARY_PATH", "LIBGL_DRIVERS_PATH", "GBM_BACKENDS_PATH",
    "__EGL_VENDOR_LIBRARY_FILENAMES", "__GLX_VENDOR_LIBRARY_NAME",
    "GALLIUM_DRIVER", "MESA_LOADER_DRIVER_OVERRIDE", "VK_DRIVER_FILES",
    "VK_ICD_FILENAMES", "DRI_PRIME",
)


class SudoConsoleController:
    def __init__(self, runtime_dir, home=None, timeout=6.0, *, min_rows=2, max_rows=500, min_cols=10, max_cols=1000):
        if os.geteuid() == 0:
            _fail("TTY_SUDO_USER_SESSION_REQUIRED")
        self.uid = os.getuid()
        dimensions = (min_rows, max_rows, min_cols, max_cols)
        if (not all(type(value) is int for value in dimensions)
                or not 1 <= min_rows <= max_rows or not 1 <= min_cols <= max_cols):
            _fail("TTY_SUDO_SIZE_LIMITS_INVALID")
        self.min_rows, self.max_rows = min_rows, max_rows
        self.min_cols, self.max_cols = min_cols, max_cols
        # The package may run inside a profile-scoped private desktop bus.
        # sudo-mode units belong to the actual logged-in user's systemd manager.
        self.user_runtime = Path("/run/user") / str(self.uid)
        try:
            runtime_info = self.user_runtime.lstat()
            bus_info = (self.user_runtime / "bus").lstat()
        except OSError:
            _fail("TTY_SUDO_USER_MANAGER_UNAVAILABLE")
        if (not stat.S_ISDIR(runtime_info.st_mode) or runtime_info.st_uid != self.uid
                or stat.S_IMODE(runtime_info.st_mode) != 0o700
                or not stat.S_ISSOCK(bus_info.st_mode) or bus_info.st_uid != self.uid):
            _fail("TTY_SUDO_USER_MANAGER_UNAVAILABLE")
        self.user_bus_address = "unix:path=" + str(self.user_runtime / "bus")
        # Preserve only this profile's explicit graphical context in the shell
        # unit. These are paths and driver selections, never authority cookies.
        self.session_environment = {
            key: os.environ[key] for key in SESSION_ENVIRONMENT_KEYS if key in os.environ
        }
        self.home = Path(home or pwd.getpwuid(self.uid).pw_dir).resolve()
        self.base = Path(runtime_dir).absolute() / "sudo"
        self.base.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.base.mkdir(mode=0o700, exist_ok=True)
        for path in (self.base.parent, self.base):
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.uid
                    or stat.S_IMODE(info.st_mode) != 0o700):
                _fail("TTY_SUDO_RUNTIME_NOT_PRIVATE")
        if len(os.fsencode(self.base / ("s-" + "a" * 26 + ".sock"))) >= 104:
            _fail("TTY_SUDO_SOCKET_PATH_TOO_LONG")
        self.tmux = shutil.which("tmux")
        self.systemd_run = shutil.which("systemd-run")
        self.systemctl = shutil.which("systemctl")
        if not all((self.tmux, self.systemd_run, self.systemctl)):
            _fail("TTY_SUDO_BACKEND_NOT_AVAILABLE")
        self.timeout = float(timeout)
        self.prefix = "framebridge-tty-" + hashlib.sha256(str(self.base).encode()).hexdigest()[:12] + "-"
        self._records = {}
        self._uncertain_launches = set()
        self._lock = threading.RLock()

    def _run(self, command, *, data=None, optional=False):
        env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
        env["XDG_RUNTIME_DIR"] = str(self.user_runtime)
        env["DBUS_SESSION_BUS_ADDRESS"] = self.user_bus_address
        try:
            result = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _fail("TTY_SUDO_BACKEND_TIMEOUT")
        except OSError:
            _fail("TTY_SUDO_BACKEND_UNAVAILABLE")
        if result.returncode and not optional:
            _fail("TTY_SUDO_BACKEND_FAILED")
        return result

    def _unit(self, name):
        if not re.fullmatch(re.escape(self.prefix) + r"[a-f0-9]{32}-[a-f0-9]{8}\.service", name):
            _fail("TTY_SUDO_UNIT_NOT_OWNED")
        result = self._run([self.systemctl, "--user", "show", name, "--no-pager",
                            "--property=MainPID,ActiveState,SubState,NoNewPrivileges,ControlGroup"], optional=True)
        return dict(line.split("=", 1) for line in result.stdout.decode("utf-8", "replace").splitlines()
                    if "=" in line)

    def _tmux(self, record, *args, optional=False, data=None):
        socket = record.get("tmux_socket") or record.get("legacy_socket")
        if not isinstance(socket, str) or not socket.startswith("/") or "\0" in socket:
            _fail("TTY_SUDO_SOCKET_INVALID")
        return self._run([self.tmux, "-S", socket, "-f", "/dev/null", *map(str, args)],
                         optional=optional, data=data)

    def _resolve(self, session):
        if isinstance(session, str):
            session = {"session_id": session}
        elif not isinstance(session, dict):
            value = dict(vars(session))
            value.update(value.pop("_sudo_record", value.pop("sudo_record", {})) or {})
            session = value
        sid = session.get("session_id")
        if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
            _fail("TTY_SUDO_SESSION_INVALID")
        record = dict(self._records.get(sid) or session)
        if not record.get("pane") or not (record.get("tmux_socket") or record.get("legacy_socket")):
            _fail("TTY_SUDO_SESSION_NOT_FOUND")
        return record

    def _pane(self, record):
        values = {}
        for field in ("pane_pid", "pane_width", "pane_height", "pane_dead"):
            value = self._tmux(record, "display-message", "-p", "-t", record["pane"],
                               "#{" + field + "}").stdout.decode().strip()
            if not value.isdigit():
                _fail("TTY_SUDO_PANE_METADATA_INVALID")
            values[field] = int(value)
        if values["pane_dead"]:
            _fail("TTY_SUDO_CONSOLE_EXITED")
        proc = _proc(values["pane_pid"])
        if proc is None:
            _fail("TTY_SUDO_PROCESS_NOT_FOUND")
        if record.get("pid") and (record["pid"] != proc["pid"] or record.get("birth") != proc["birth"]):
            _fail("TTY_SUDO_PROCESS_IDENTITY_CHANGED")
        try:
            cwd = os.readlink(Path("/proc") / str(proc["pid"]) / "cwd")
        except OSError:
            _fail("TTY_SUDO_CWD_UNAVAILABLE")
        return proc, cwd, values["pane_height"], values["pane_width"]

    def _scope(self, group):
        # cgroup v2 cgroup.procs plus nested cgroups includes reparented descendants.
        if not group or not group.startswith("/") or ".." in Path(group).parts:
            _fail("TTY_SUDO_CGROUP_UNAVAILABLE")
        root = Path("/sys/fs/cgroup") / group.lstrip("/")
        try:
            files = [root / "cgroup.procs", *root.glob("**/cgroup.procs")]
            return {int(line) for file in files for line in file.read_text().splitlines() if line.isdigit()}
        except OSError:
            _fail("TTY_SUDO_CGROUP_UNAVAILABLE")

    def _pane_group(self, pid):
        try:
            entries = Path("/proc", str(pid), "cgroup").read_text().splitlines()
            return next(line[3:] for line in entries if line.startswith("0::"))
        except (OSError, StopIteration):
            _fail("TTY_SUDO_CGROUP_UNAVAILABLE")

    def _pane_scope(self, group, server_group):
        if group == server_group:
            return None
        name = Path(group).name
        if not re.fullmatch(r"tmux-spawn-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\.scope", name):
            _fail("TTY_SUDO_CGROUP_UNEXPECTED")
        if Path(group).parent != Path(server_group).parent:
            _fail("TTY_SUDO_CGROUP_UNEXPECTED")
        return name

    def _inspect(self, record):
        proc, cwd, rows, cols = self._pane(record)
        all_processes = _processes()
        shell_pid = proc["pid"]
        candidates = _descendants(all_processes, shell_pid)
        candidates.update(p for p, info in all_processes.items() if p != shell_pid and (
            info["sid"] == proc["sid"] or (proc["tty"] and info["tty"] == proc["tty"])))
        unit_name = record.get("unit_name")
        server = None
        group = None
        pane_group = None
        pane_scope = None
        if unit_name:
            unit = self._unit(unit_name)
            try:
                main_pid = int(unit["MainPID"])
            except (KeyError, ValueError):
                _fail("TTY_SUDO_UNIT_NOT_RUNNING")
            server = _proc(main_pid)
            if (unit.get("ActiveState") != "active" or server is None
                    or Path(server["exe"]).name != Path(self.tmux).name
                    or (record.get("server_pid") and record["server_pid"] != main_pid)
                    or (record.get("server_birth") and record["server_birth"] != server["birth"])):
                _fail("TTY_SUDO_UNIT_IDENTITY_CHANGED")
            group = unit.get("ControlGroup")
            pane_group = self._pane_group(shell_pid)
            pane_scope = self._pane_scope(pane_group, group)
            if record.get("pane_control_group") and record["pane_control_group"] != pane_group:
                _fail("TTY_SUDO_CGROUP_IDENTITY_CHANGED")
            scoped = self._scope(group) | self._scope(pane_group)
            candidates.update(scoped - {main_pid, shell_pid})
            expected = "yes" if proc["nnp"] else "no"
            if unit.get("NoNewPrivileges") != expected or server["nnp"] != proc["nnp"]:
                _fail("TTY_SUDO_PRIVILEGE_STATE_MISMATCH")
        elevated = (any(uid != self.uid for uid in proc["uids"])
                    or any(p in all_processes and any(u != self.uid for u in all_processes[p]["uids"])
                           for p in candidates))
        unknown = candidates - all_processes.keys()
        idle_shell = (Path(proc["exe"]).name == "bash" and proc["state"] == "S"
                      and proc["tpgid"] == proc["pgrp"] == proc["pid"])
        marker = record.get("prompt_marker")
        try:
            prompt_idle = bool(marker and Path(marker).read_text().strip() == record.get("generation", "") + ":idle")
        except OSError:
            prompt_idle = False
        reason = "TTY_SUDO_ELEVATED_PROCESS_PRESENT" if elevated else (
            "TTY_SUDO_PROCESS_STATE_UNVERIFIED" if unknown else (
                "TTY_SUDO_CONSOLE_BUSY" if candidates or not idle_shell else (
                    "TTY_SUDO_PROMPT_UNVERIFIED" if not prompt_idle else None)))
        return {**record, "ok": True, "verified": True, "pid": proc["pid"], "birth": proc["birth"],
                "mode": "off" if proc["nnp"] else "on", "no_new_privs": proc["nnp"],
                "server_pid": server["pid"] if server else None,
                "server_no_new_privs": server["nnp"] if server else None,
                "server_birth": server["birth"] if server else None, "control_group": group,
                "pane_control_group": pane_group, "pane_scope_unit": pane_scope,
                "cwd": cwd, "rows": rows, "cols": cols, "busy": bool(reason),
                "busy_reason": reason, "busy_pids": sorted(candidates),
                "managed": bool(unit_name), "sudo_authentication": "system_policy",
                "sudo_authorization_granted": False,
                "legacy_detached_scope_complete": bool(unit_name), "prompt_verified": prompt_idle}

    def _old_cleanup_complete(self, record):
        try:
            old = _proc(record["pid"])
            if old and old["birth"] == record["birth"] and old["state"] not in {"Z", "X"}:
                return False
            unit = self._unit(record["unit_name"])
            if int(unit.get("MainPID", "0")) or unit.get("ActiveState") in {"active", "activating", "deactivating"}:
                return False
            for group in {record.get("control_group"), record.get("pane_control_group")}:
                if not group:
                    return False
                if (Path("/sys/fs/cgroup") / group.lstrip("/")).exists() and self._scope(group):
                    return False
            return True
        except (SudoControlError, KeyError, ValueError):
            return False

    def _refresh_pending(self, record):
        old = record.get("pending_old_record")
        if old and self._old_cleanup_complete(old):
            record = dict(record)
            record.pop("pending_old_record", None)
            record.update(cleanup_pending=False, transition_complete=True, cleanup_error=None)
            self._records[record["session_id"]] = record
        return record

    def status(self, session):
        with self._lock:
            record = self._refresh_pending(self._resolve(session))
            return self._inspect(record)

    def _idle(self, record):
        current = self._inspect(record)
        if current["busy"]:
            _fail(current["busy_reason"])
        return current

    def _launch(self, session_id, cwd, rows, cols, mode):
        if mode not in {"on", "off"}:
            _fail("TTY_SUDO_MODE_INVALID")
        if not re.fullmatch(r"[a-f0-9]{32}", str(session_id)):
            _fail("TTY_SUDO_SESSION_INVALID")
        if not (type(rows) is int and self.min_rows <= rows <= self.max_rows and type(cols) is int and self.min_cols <= cols <= self.max_cols):
            _fail("TTY_SUDO_SIZE_INVALID")
        cwd = str(Path(cwd).resolve())
        if not Path(cwd).is_dir():
            _fail("TTY_SUDO_CWD_INVALID")
        nonce = uuid.uuid4().hex[:8]
        socket = self.base / ("s-" + hashlib.sha256((session_id + nonce).encode()).hexdigest()[:26] + ".sock")
        unit_name = self.prefix + session_id + "-" + nonce + ".service"
        marker = self.base / ("p-" + nonce + ".state")
        rcfile = self.base / ("p-" + nonce + ".bashrc")
        record = {"session_id": session_id, "name": "fb-real-" + session_id, "pane": "",
                  "tmux_socket": str(socket), "unit_name": unit_name, "mode": mode,
                  "prompt_marker": str(marker), "rcfile": str(rcfile), "generation": nonce}
        def write_private(path, text):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
        quoted = shlex.quote(str(marker))
        rc = ("__fb_tty_busy() { builtin printf '%s\\n' '" + nonce + ":busy' > " + quoted + "; }\n"
              "__fb_tty_prompt() { if [[ $(builtin trap -p DEBUG) == \"trap -- '__fb_tty_busy' DEBUG\" ]]; then "
              "builtin printf '%s\\n' '" + nonce + ":idle' > " + quoted + "; else "
              "builtin printf '%s\\n' '" + nonce + ":busy' > " + quoted + "; fi; }\n"
              "set -o functrace\ntrap '__fb_tty_busy' DEBUG\nPROMPT_COMMAND=__fb_tty_prompt\nPS1='\\s-\\v\\$ '\n")
        write_private(marker, nonce + ":busy\n")
        write_private(rcfile, rc)
        identity = pwd.getpwuid(self.uid).pw_name
        command = [self.systemd_run, "--user", "--quiet", "--collect", "--unit=" + unit_name,
                   "--property=Type=exec", "--property=NoNewPrivileges=" + ("yes" if mode == "off" else "no"),
                   "--property=KillMode=control-group", "--property=TimeoutStopSec=3",
                   "--working-directory=" + str(self.home)]
        for key, value in {"HOME": str(self.home), "USER": identity, "LOGNAME": identity,
                           "HISTFILE": "/dev/null", "SHELL": "/usr/bin/bash",
                           "LANG": "C.UTF-8", "TERM": "xterm-256color"}.items():
            command.append("--setenv=" + key + "=" + value)
        for key, value in self.session_environment.items():
            command.append("--setenv=" + key + "=" + value)
        command += [self.tmux, "-D", "-S", str(socket), "-f", "/dev/null"]
        launched = False
        verified_record = None
        try:
            launched = True
            # A failed partial launch must never disappear from shutdown
            # evidence, even if its ordinary rollback also fails.
            self._uncertain_launches.add(unit_name)
            self._run(command)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if socket.exists():
                    break
                time.sleep(0.05)
            else:
                _fail("TTY_SUDO_TMUX_NOT_READY")
            pane = self._tmux(record, "new-session", "-d", "-s", record["name"],
                              "-x", cols, "-y", rows, "-c", cwd, "-P", "-F", "#{pane_id}",
                              shlex.join(["/usr/bin/bash", "--noprofile", "--rcfile", str(rcfile), "-i"])).stdout.decode().strip()
            if not re.fullmatch(r"%[0-9]+", pane):
                _fail("TTY_SUDO_PANE_METADATA_INVALID")
            record["pane"] = pane
            self._tmux(record, "set-option", "-p", "-t", pane, "remain-on-exit", "on")
            self._tmux(record, "set-option", "-t", record["name"], "status", "off")
            # Verify the real process, not merely the requested unit property.
            expected = 1 if mode == "off" else 0
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                current = self._inspect(record)
                verified_record = current
                if current["no_new_privs"] != expected:
                    _fail("TTY_SUDO_PRIVILEGE_STATE_MISMATCH")
                if not current["busy"]:
                    self._uncertain_launches.discard(unit_name)
                    return current
                time.sleep(0.05)
            _fail("TTY_SUDO_CONSOLE_NOT_IDLE")
        except BaseException:
            # Only a fully inspected record can prove rollback. Incomplete
            # launches retain uncertainty even if an ordinary stop was sent.
            if verified_record is not None:
                try:
                    self._close(verified_record)
                    self._uncertain_launches.discard(unit_name)
                except SudoControlError:
                    pass
            elif launched:
                try:
                    unit = self._unit(unit_name)
                    proc, _, _, _ = self._pane(record)
                    scope = self._pane_scope(self._pane_group(proc["pid"]), unit.get("ControlGroup"))
                    if scope:
                        self._run([self.systemctl, "--user", "--no-block", "stop", scope], optional=True)
                except SudoControlError:
                    pass
                self._run([self.systemctl, "--user", "--no-block", "stop", unit_name], optional=True)
            if unit_name not in self._uncertain_launches:
                socket.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                rcfile.unlink(missing_ok=True)
            raise

    def launch(self, session_id, cwd=None, rows=24, cols=80, mode="off"):
        with self._lock:
            if session_id in self._records:
                _fail("TTY_SUDO_SESSION_EXISTS")
            record = self._launch(session_id, cwd or self.home, rows, cols, mode)
            self._records[session_id] = record
            return dict(record)

    def _terminated(self, record):
        # A dead pane still owns its tmux service and may retain detached jobs.
        unit = self._unit(record["unit_name"])
        server = _proc(int(unit.get("MainPID", "0")))
        if server and (server["pid"] != record.get("server_pid")
                       or server["birth"] != record.get("server_birth")):
            _fail("TTY_SUDO_UNIT_IDENTITY_CHANGED")
        groups = {record.get("control_group"), record.get("pane_control_group")}
        if None in groups:
            _fail("TTY_SUDO_CGROUP_UNAVAILABLE")
        processes = _processes()
        members = set()
        for group in groups:
            if (Path("/sys/fs/cgroup") / group.lstrip("/")).exists():
                members.update(self._scope(group))
        if members - processes.keys():
            _fail("TTY_SUDO_PROCESS_STATE_UNVERIFIED")
        if any(any(uid != self.uid for uid in processes[pid]["uids"]) for pid in members):
            _fail("TTY_SUDO_ELEVATED_PROCESS_PRESENT")
        return {**record, "busy_reason": None}

    def _close_members(self, record):
        members = self._scope_members_existing(record)
        result = {}
        for pid in members:
            process = _proc(pid)
            if process is None:
                # An exited member can disappear between cgroup and /proc reads.
                if pid in self._scope_members_existing(record):
                    _fail("TTY_SUDO_PROCESS_STATE_UNVERIFIED")
                continue
            if any(uid != self.uid for uid in process["uids"]):
                _fail("TTY_SUDO_ELEVATED_PROCESS_PRESENT")
            if process["state"] not in {"Z", "X"}:
                result[pid] = process
        return result

    def _scope_members_existing(self, record):
        members = set()
        for group in {record["control_group"], record["pane_control_group"]}:
            path = Path("/sys/fs/cgroup") / group.lstrip("/")
            if path.exists():
                try:
                    members.update(self._scope(group))
                except SudoControlError:
                    if path.exists():
                        raise
        return members

    def _close(self, record, require_idle=False):
        try:
            current = self._idle(record) if require_idle else self._inspect(record)
        except SudoControlError as error:
            if (require_idle or not record.get("unit_name")
                    or error.code not in {"TTY_SUDO_CONSOLE_EXITED", "TTY_SUDO_PROCESS_NOT_FOUND"}):
                raise
            current = self._terminated(record)
        if current["busy_reason"] in {"TTY_SUDO_ELEVATED_PROCESS_PRESENT", "TTY_SUDO_PROCESS_STATE_UNVERIFIED"}:
            _fail(current["busy_reason"])
        if record.get("unit_name"):
            # Close this private PTY before stopping its sibling tmux-spawn
            # scope: interactive Bash ignores TERM and that scope may otherwise
            # wait the user's default 90-second systemd stop timeout.
            self._tmux(record, "kill-session", "-t", record["name"], optional=True)
            if current.get("pane_scope_unit"):
                self._run([self.systemctl, "--user", "--no-block", "stop",
                           current["pane_scope_unit"]], optional=True)
            self._run([self.systemctl, "--user", "--no-block", "stop",
                       record["unit_name"]], optional=True)
            deadline = time.monotonic() + self.timeout
            escalate_at = time.monotonic() + min(1.0, self.timeout / 3)
            escalated = False
            while time.monotonic() < deadline:
                remaining = self._close_members(current)
                shell = _proc(current["pid"])
                server = _proc(current["server_pid"])
                shell_alive = (shell and shell["birth"] == current["birth"]
                               and shell["state"] not in {"Z", "X"})
                server_alive = (server and server["birth"] == current["server_birth"]
                                and server["state"] not in {"Z", "X"})
                if not remaining and not shell_alive and not server_alive:
                    break
                if not escalated and time.monotonic() >= escalate_at:
                    # Revalidate the private service identity and all recorded
                    # cgroup members before escalating. Never signal root jobs,
                    # unverified processes or a reused PID.
                    self._terminated(current)
                    for identity in remaining.values():
                        try:
                            fd = os.pidfd_open(identity["pid"])
                        except ProcessLookupError:
                            continue
                        except (AttributeError, OSError):
                            _fail("TTY_SUDO_PIDFD_UNAVAILABLE")
                        try:
                            live = _proc(identity["pid"])
                            if (not live or live["birth"] != identity["birth"]
                                    or live["state"] in {"Z", "X"}):
                                continue
                            if any(uid != self.uid for uid in live["uids"]):
                                _fail("TTY_SUDO_ELEVATED_PROCESS_PRESENT")
                            group = self._pane_group(live["pid"])
                            if not any(group == owned or group.startswith(owned + "/")
                                       for owned in {current["control_group"], current["pane_control_group"]}):
                                _fail("TTY_SUDO_CGROUP_UNEXPECTED")
                            try:
                                signal.pidfd_send_signal(fd, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            except (AttributeError, OSError):
                                _fail("TTY_SUDO_PIDFD_SIGNAL_FAILED")
                        finally:
                            os.close(fd)
                    escalated = True
                time.sleep(0.05)
            else:
                _fail("TTY_SUDO_SCOPE_STILL_RUNNING")
            Path(record["tmux_socket"]).unlink(missing_ok=True)
        else:
            self._tmux(record, "kill-session", "-t", record["name"])
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                proc = _proc(current["pid"])
                if proc is None or proc["birth"] != current["birth"]:
                    break
                time.sleep(0.05)
            else:
                _fail("TTY_SUDO_OLD_CONSOLE_STILL_RUNNING")
        for key in ("prompt_marker", "rcfile"):
            if record.get(key):
                path = Path(record[key])
                if path.parent == self.base:
                    path.unlink(missing_ok=True)
        return {"ok": True, "session_id": record["session_id"], "closed": True,
                "root_process_cleanup_claimed": False}

    def close(self, session):
        with self._lock:
            record = self._refresh_pending(self._resolve(session))
            if record.get("cleanup_pending"):
                _fail("TTY_SUDO_TRANSITION_CLEANUP_PENDING")
            result = self._close(record)
            self._records.pop(record["session_id"], None)
            return result

    def set_mode(self, session, mode):
        if mode not in {"on", "off"}:
            _fail("TTY_SUDO_MODE_INVALID")
        with self._lock:
            old = self._refresh_pending(self._resolve(session))
            if old.get("cleanup_pending"):
                _fail("TTY_SUDO_TRANSITION_CLEANUP_PENDING")
            current = self._idle(old)
            if old.get("unit_name") and current["mode"] == mode:
                return {**current, "recreated": False}
            candidate = self._launch(current["session_id"], current["cwd"],
                                     current["rows"], current["cols"], mode)
            try:
                # The old console remains alive until its replacement is verified.
                self._idle(old)
                self._close(old, require_idle=True)
            except BaseException as error:
                previous = _proc(current["pid"])
                if previous and previous["birth"] == current["birth"] and previous["state"] not in {"Z", "X"}:
                    try:
                        self._close(candidate)
                    except BaseException:
                        self._uncertain_launches.add(candidate["unit_name"])
                        raise
                    raise
                # The former Bash is gone. Keep the verified replacement and
                # retain old ownership metadata until all old scopes are empty.
                candidate = {**candidate, "cleanup_pending": True, "transition_complete": False,
                             "cleanup_error": getattr(error, "code", "TTY_SUDO_OLD_CLEANUP_FAILED"),
                             "pending_old_record": dict(current)}
                self._records[current["session_id"]] = candidate
                return {**candidate, "recreated": True, "previous_pid": current["pid"]}
            candidate.update(cleanup_pending=False, transition_complete=True)
            self._records[current["session_id"]] = candidate
            return {**candidate, "recreated": True, "previous_pid": current["pid"]}
