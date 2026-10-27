"""Local authenticated Bash/FBTERM console, shared across desktop and TTY clients."""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys

from .config import load_config, private_dir, state_path

_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")

def _node(state, value):
    value = value or "LOCAL"
    if type(value) is not str or _NODE.fullmatch(value) is None:
        raise ValueError("CONSOLE_NODE_INVALID")
    host = socket.gethostname()
    names = {"local", "localhost", host.lower(), host.split(".")[0].lower()}
    try:
        config = load_config(state)
    except (ValueError, OSError):
        config = {}
    name = config.get("name")
    if isinstance(name, str):
        names.add(name.lower())
    if value.lower() not in names:
        raise RuntimeError(
            "REMOTE_CONSOLE_SSH_CONFIGURATION_REQUIRED: run fb console on the target node, "
            "or use your explicitly configured SSH destination. Remote EXEC, CUA and TTY "
            "operations remain available through the enrolled broker.")
    return re.sub(r"[^A-Za-z0-9_-]", "_", host)[:64] or "NODE"

def _private_runtime(path):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError("CONSOLE_RUNTIME_DIRECTORY_UNTRUSTED")
    return path

def context(state, node="LOCAL", *, create=False):
    state = state_path(state)
    label = _node(state, node)
    key = hashlib.sha256(str(state).encode()).hexdigest()[:20]
    runtime = _private_runtime(Path("/run/user") / str(os.getuid()))
    root = runtime / "framebridge-expanded-console"
    profile = root / key
    if create:
        private_dir(state)
        for path in (root, profile):
            path.mkdir(mode=0o700, exist_ok=True)
            _private_runtime(path)
    else:
        for path in (root, profile):
            if path.exists() or path.is_symlink():
                _private_runtime(path)
    return {
        "state": state, "node": label, "runtime": runtime,
        "socket": profile / "console.sock",
        "session": "FB-" + label + "-" + key[:10],
        "jobs_root": state / "home/.local/share/framebridge/fbterm",
    }

def _properties(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

def _active_graphical_display(env=None):
    """Auto GUI requires the actual active local X11 session, never a guessed Xvfb."""
    env = os.environ if env is None else env
    display = env.get("DISPLAY", "")
    sid = env.get("XDG_SESSION_ID", "")
    if (re.fullmatch(r":[0-9]+(?:\.[0-9]+)?", display) is None
            or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sid) is None):
        return False
    try:
        result = subprocess.run([
            "loginctl", "show-session", sid, "--no-pager",
            "-p", "User", "-p", "Type", "-p", "Active", "-p", "Remote",
            "-p", "Display", "-p", "VTNr",
        ], capture_output=True, text=True, timeout=5)
        if result.returncode:
            return False
        fields = _properties(result.stdout)
        vt = fields.get("VTNr", "")
        if not vt.isdigit() or int(vt) < 1:
            return False
        active = Path("/sys/class/tty/tty0/active").read_text().strip()
        return (fields.get("User") == str(os.getuid())
                and fields.get("Type") == "x11"
                and fields.get("Active") == "yes"
                and fields.get("Remote") == "no"
                and fields.get("Display", "").split(".")[0] == display.split(".")[0]
                and active == "tty" + str(int(vt)))
    except (OSError, subprocess.SubprocessError):
        return False

def _environment(ctx):
    env = os.environ.copy()
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    env.update({
        "HOME": real_home,
        "XDG_RUNTIME_DIR": str(ctx["runtime"]),
        "FRAMEBRIDGE_EXPANDED_STATE": str(ctx["state"]),
        "FRAMEBRIDGE_FBTERM_ROOT": str(ctx["jobs_root"]),
        "FRAMEBRIDGE_FBTERM_ROOT_OVERRIDE": str(ctx["jobs_root"]),
        "FBTERM_ROOT": str(ctx["jobs_root"]),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
    })
    # The installed command adjusts sys.path only in its own process. Make the
    # new graphical-terminal process resolve this same installed module.
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(
        [package_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env

def _socket_exists(sock):
    try:
        info = sock.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISSOCK(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError("CONSOLE_SOCKET_UNTRUSTED")
    return True

def status(state, node="LOCAL"):
    ctx = context(state, node)
    sock = ctx["socket"]
    value = {"ok": True, "node": ctx["node"], "socket": str(sock),
             "session": ctx["session"], "jobs_root": str(ctx["jobs_root"]),
             "exists": False, "panes": []}
    if not _socket_exists(sock):
        return value
    try:
        result = subprocess.run([
            "/usr/bin/tmux", "-S", str(sock), "list-panes",
            "-t", "=" + ctx["session"],
            "-F", "#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_width}|#{pane_height}|#{@framebridge_role}",
        ], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        value.update(ok=False, error="CONSOLE_TMUX_STATUS_FAILED")
        return value
    if result.returncode:
        value.update(ok=False, error="CONSOLE_TMUX_STATUS_FAILED")
        return value
    value["exists"] = True
    for line in result.stdout.splitlines():
        fields = line.split("|")
        if len(fields) != 6:
            raise RuntimeError("CONSOLE_TMUX_STATUS_INVALID")
        pane, pid, command, width, height, role = fields
        if not re.fullmatch(r"%[0-9]+", pane) or not all(x.isdigit() for x in [pid, width, height]):
            raise RuntimeError("CONSOLE_TMUX_STATUS_INVALID")
        value["panes"].append({"pane": pane, "pid": int(pid), "command": command,
                               "cols": int(width), "rows": int(height), "role": role})
    return value


def _prevent_recursive_attach(ctx, env):
    """Reject attaching a console from one of its own panes before reflow."""
    marker = os.environ.get("TMUX", "")
    if not marker:
        return
    parts = marker.rsplit(",", 2)
    try:
        same_socket = Path(parts[0]).resolve() == ctx["socket"].resolve()
    except (OSError, RuntimeError):
        raise RuntimeError("CONSOLE_TMUX_CONTEXT_UNVERIFIED") from None
    if not same_socket:
        return
    pane = os.environ.get("TMUX_PANE", "")
    if (len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit()
            or re.fullmatch(r"%[0-9]+", pane) is None):
        raise RuntimeError("CONSOLE_TMUX_CONTEXT_UNVERIFIED")
    try:
        result = subprocess.run([
            "/usr/bin/tmux", "-S", str(ctx["socket"]), "list-panes", "-s",
            "-t", "=" + ctx["session"], "-F", "#{pane_id}",
        ], env=env, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("CONSOLE_TMUX_CONTEXT_UNVERIFIED") from None
    panes = result.stdout.splitlines() if isinstance(result.stdout, str) else []
    if (result.returncode or not panes
            or any(re.fullmatch(r"%[0-9]+", value) is None for value in panes)):
        raise RuntimeError("CONSOLE_TMUX_CONTEXT_UNVERIFIED")
    if pane in panes:
        raise RuntimeError(
            "CONSOLE_ALREADY_ATTACHED: This terminal is already inside this "
            "FrameBridge console. Use its existing shell or open a separate terminal.")

def run(state, node="LOCAL", *, inside=False, mode="auto"):
    if mode not in {"auto", "gui", "tty"}:
        raise ValueError("CONSOLE_MODE_INVALID")
    ctx = context(state, node, create=True)
    env = _environment(ctx)
    _socket_exists(ctx["socket"])
    ui = Path(__file__).resolve().parent / "console_ui/framebridge_console.py"
    if not ui.is_file():
        raise RuntimeError("CONSOLE_COMPONENT_NOT_INSTALLED")
    graphical = _active_graphical_display(env)
    if not inside and mode != "tty" and (mode == "gui" or graphical):
        if not graphical:
            raise RuntimeError("CONSOLE_ACTIVE_X11_DESKTOP_REQUIRED")
        terminal = shutil.which("gnome-terminal")
        if not terminal:
            raise RuntimeError("CONSOLE_GRAPHICAL_TERMINAL_UNAVAILABLE")
        try:
            result = subprocess.run([
                terminal, "--window", "--maximize",
                "--title=FrameBridge Console | " + ctx["node"], "--",
                sys.executable, "-m", "framebridge_expanded.console",
                "--state-dir", str(ctx["state"]), "--node", node, "--inside", "--mode", "gui",
            ], env=env, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("CONSOLE_GRAPHICAL_TERMINAL_FAILED") from None
        if result.returncode:
            raise RuntimeError("CONSOLE_GRAPHICAL_TERMINAL_FAILED")
        return 0
    if not os.isatty(0):
        raise RuntimeError("CONSOLE_INTERACTIVE_TERMINAL_REQUIRED")
    _prevent_recursive_attach(ctx, env)
    try:
        result = subprocess.run([
            sys.executable, str(ui), "--node", ctx["node"],
            "--socket", str(ctx["socket"]), "--session", ctx["session"] ,
            "--layout", "horizontal" if inside else "vertical", "--apply",
        ], env=env, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("CONSOLE_LAYOUT_FAILED") from None
    if result.returncode:
        raise RuntimeError("CONSOLE_LAYOUT_FAILED")
    os.execve("/usr/bin/tmux", [
        "tmux", "-S", str(ctx["socket"]), "attach-session", "-f", "ignore-size",
        "-t", "=" + ctx["session"],
    ], env)
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir")
    parser.add_argument("--node", default="LOCAL")
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--mode", choices=["auto", "gui", "tty"], default="auto")
    args = parser.parse_args(argv)
    return run(state_path(args.state_dir), args.node, inside=args.inside, mode=args.mode)

if __name__ == "__main__":
    raise SystemExit(main())
