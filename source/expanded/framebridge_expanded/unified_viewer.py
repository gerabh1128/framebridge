"""One minimal FBTERM window: read-only history and the Original tmux client.

The history uses the local profile's bounded database observation. The lower
terminal is a real VTE terminal running an explicit read-only tmux attachment.
No shell, job submission, replay, remote target, or credential prompt.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import os
from pathlib import Path
import socket
import sys

from . import __version__, activity_data as data

TITLE = ">framebridge_"
LABELS = {"queued": "Queued", "in_flight": "Running", "running": "Running",
          "completed": "Completed", "not_started": "Not started",
          "expired_before_start": "Expired before start",
          "outcome_unknown": "Outcome unconfirmed", "unverified": "Unverified"}
CSS = b"""
window.fbterm { background-color: #1c1c1c; color: #d0d0d0; }
.fbterm label { color: #d0d0d0; font-family: monospace; font-size: 11pt; font-weight: normal; }
.fbterm .status-header { background-color: #303030; }
.fbterm .history text { background-color: #242424; color: #d0d0d0; }
.fbterm .history { font-family: monospace; font-size: 10pt; font-weight: normal; }
.fbterm separator { background-color: #494949; min-height: 1px; }
.fbterm expander { background-color: #292929; color: #d0d0d0; padding: 5px 7px; }
"""


def profile_id(path):
    return hashlib.sha256(os.fsencode(Path(path))).hexdigest()[:16]


def window_instance(path):
    return "framebridge-fbterm-" + profile_id(path)


def job_state(row):
    status = LABELS.get(row["state"], "Unverified")
    code = row["exit_code"]
    if row["state"] == "completed":
        if code is None:
            status = "Completed (exit code unverified)"
        elif code != "0":
            status = "Completed with error"
    if row.get("owner_cancel_before_exec"):
        return "Cancelled before execution"
    if row.get("owner_cancel_signal_sent"):
        return "Cancel signal sent; command ended"
    if row.get("capture_status") == "partial" and row["state"]=="completed":
        return status + " (partial capture)"
    return status


def job_text(row):
    state = row["state"]
    code = row["exit_code"]
    if state in ("not_started", "expired_before_start"):
        value = "N/A"
    elif state in ("queued", "in_flight", "running"):
        value = "-"
    elif state in ("outcome_unknown", "unverified"):
        value = "Unconfirmed"
    else:
        value = code if code is not None else "Unconfirmed"
    from .owner_job_menu import duration
    return "JOB: " + row["id"] + " | STATE: " + job_state(row) + " | DUR: " + duration(row) + " | EXIT: " + value


def job_colour(row):
    if row["state"] == "completed":
        return "success" if row["exit_code"] == "0" else "error" if row["exit_code"] is not None else "warning"
    if row["state"] in ("running", "in_flight"):
        return "active"
    if row["state"] in ("queued", "outcome_unknown", "unverified"):
        return "warning"
    return "neutral"


def history_text(rows):
    return "\n".join(map(job_text, rows)) or "No recorded jobs."


def summary(result):
    if result is None:
        return "Execution state not verified"
    c = result["counts"]
    running = c.get("running", 0) + c.get("in_flight", 0)
    return (f"Running: {running} | Queued: {c.get('queued', 0)} | "
            f"Unconfirmed outcomes: {c.get('outcome_unknown', 0)} | "
            f"Completed: {c.get('completed', 0)}" +
            (" | Other states: " + str(result["other"]) if result.get("other") else ""))


def create_window(profile, tmux, terminal_env, observe=None):
    """Create one window; test callers may inject a bounded synthetic observer."""
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Vte", "2.91")
    from gi.repository import Gtk, Gdk, GLib, Vte, Pango
    from .viewer import SESSION

    if observe is None:
        observe = lambda offset: data.local_jobs(profile, offset)
    css = Gtk.CssProvider()
    css.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    window = Gtk.Window(title=TITLE)
    window.set_wmclass(window_instance(profile.path), "FrameBridgeFBTERM")
    window.set_default_size(1060, 610)
    window.get_style_context().add_class("fbterm")
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    window.add(root)
    header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    header.set_border_width(6)
    header.get_style_context().add_class("status-header")
    root.pack_start(header, False, False, 0)
    identity = Gtk.Label(xalign=0, label=f"FrameBridge Expanded | {data.safe_text(socket.gethostname())} | {__version__} | Profile ID: {profile_id(profile.path)}")
    identity.set_ellipsize(Pango.EllipsizeMode.END)
    header.pack_start(identity, False, False, 0)
    counts = Gtk.Label(xalign=0, label="Execution state not verified")
    counts.set_ellipsize(Pango.EllipsizeMode.END)
    header.pack_start(counts, False, False, 0)
    stamp = Gtk.Label(xalign=0, label="Read-only output | Right-click a job for actions | GPL-3.0-only | Copyleft")
    stamp.set_ellipsize(Pango.EllipsizeMode.END)
    header.pack_start(stamp, False, False, 0)
    root.pack_start(Gtk.Separator(), False, False, 0)
    # The original terminal owns all available space by default.
    lower = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    root.pack_start(lower, True, True, 0)
    root.pack_start(Gtk.Separator(), False, False, 0)
    expander = Gtk.Expander(label="Recorded jobs / history (right-click for actions)")
    expander.set_expanded(False)
    root.pack_start(expander, False, False, 0)
    history = Gtk.TextView()
    history.get_style_context().add_class("history")
    history.set_editable(False)
    history.set_cursor_visible(False)
    history.set_monospace(True)
    buffer = history.get_buffer()
    for name, colour in (("success", "#d0d0d0"), ("active", "#ededed"),
                         ("warning", "#c7b28d"), ("error", "#d79b96"), ("neutral", "#bfbfbf")):
        buffer.create_tag(name, foreground=colour, weight=Pango.Weight.NORMAL)
    history.set_wrap_mode(Gtk.WrapMode.NONE)
    history.set_left_margin(8)
    history.set_right_margin(8)
    history.set_top_margin(7)
    history.set_bottom_margin(7)
    scroll = Gtk.ScrolledWindow()
    scroll.set_overlay_scrolling(False)
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(120)
    scroll.set_size_request(-1, 135)
    scroll.add(history)
    expander.add(scroll)
    terminal = Vte.Terminal()
    terminal.set_input_enabled(False)
    terminal.set_scrollback_lines(10000)
    terminal.set_font(Pango.FontDescription("Monospace 11"))
    terminal.set_scroll_on_output(False)
    terminal.set_scroll_on_keystroke(False)
    bg = Gdk.RGBA(); bg.parse("#1c1c1c")
    fg = Gdk.RGBA(); fg.parse("#d0d0d0")
    terminal.set_color_background(bg)
    terminal.set_color_foreground(fg)
    terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.OFF)
    lower.pack_start(terminal, True, True, 0)
    scrollbar = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL,
                               adjustment=terminal.get_vadjustment())
    lower.pack_start(scrollbar, False, False, 0)
    pool = ThreadPoolExecutor(max_workers=1)
    state = {"closed": False, "pending": False, "rows": [], "offset": 0, "total": 0,
             "source": 0, "first": True, "child": None, "last_page": None}
    adjust = scroll.get_vadjustment()

    def apply(result, older=False):
        if state["closed"]:
            return False
        state["pending"] = False
        if result is None:
            counts.set_text("Execution state not verified")
            stamp.set_text("Read-only output | Last observation unavailable | GPL-3.0-only | Copyleft")
            return False
        c = result["counts"]
        active = c.get("running", 0) + c.get("in_flight", 0)
        # Retain the classic monochrome palette. Only exceptional states are
        # subtly distinguished; idle and successful work are not coloured green.
        accent = "#c7b28d" if result.get("other") or c.get("outcome_unknown", 0) else "#ededed" if active else "#d0d0d0"
        counts.set_markup('<span foreground="' + accent + '">' +
                          GLib.markup_escape_text(summary(result)) + '</span>')
        updated = datetime.fromtimestamp(result["updated"]).astimezone()
        stamp.set_text(f"Right-click a job for details | Updated {updated:%H:%M:%S %Z} | GPL-3.0-only | Copyleft")
        state["total"] = result["total"]
        position = adjust.get_value()
        if older:
            # Key-based de-duplication protects against concurrent additions.
            seen = {row["id"] for row in state["rows"]}
            state["rows"].extend(row for row in result["jobs"] if row["id"] not in seen)
            state["offset"] = result["offset"]
        else:
            # Do not replace older pages while the operator is reading them.
            if state["offset"] or position > 2:
                return False
            state["rows"] = result["jobs"]
        text = history_text(state["rows"])
        if text != state["last_page"]:
            state["redrawing"] = True
            buffer.set_text("")
            for row in state["rows"]:
                buffer.insert_with_tags_by_name(buffer.get_end_iter(), job_text(row) + "\n", job_colour(row))
            if not state["rows"]:
                buffer.set_text("No recorded jobs.")
            state["last_page"] = text
            state["redrawing"] = False
            def restore():
                adjust.set_value(min(position, max(0, adjust.get_upper() - adjust.get_page_size())))
                return False
            GLib.idle_add(restore)
        return False

    def refresh(older=False):
        if state["closed"] or state["pending"]:
            return not state["closed"]
        if older and not expander.get_expanded():
            return True
        offset = state["offset"] + data.HISTORY_PAGE if older else 0
        if older and offset >= state["total"]:
            return True
        state["pending"] = True
        future = pool.submit(observe, offset)
        def done(future):
            try:
                result = future.result()
            except Exception:
                result = None
            GLib.idle_add(apply, result, older)
        future.add_done_callback(done)
        return True

    def scroll_changed(adjustment):
        if state.get("redrawing") or not expander.get_expanded():
            return
        if (state["rows"] and adjustment.get_value() > 0
                and adjustment.get_value() + adjustment.get_page_size() >= adjustment.get_upper() - 3
                and state["offset"] + data.HISTORY_PAGE < state["total"]):
            refresh(older=True)
        elif adjustment.get_value() <= 2 and state["offset"] and not state["pending"]:
            state["offset"] = 0
            refresh()

    def close(*_):
        state["closed"] = True
        if state["source"]:
            GLib.source_remove(state["source"])
        pool.shutdown(wait=False, cancel_futures=True)
        Gtk.main_quit()

    def spawned(term, pid, error, *_):
        if error is not None:
            if not state["closed"]:
                window.destroy()
        else:
            state["child"] = pid

    def exited(*_):
        if not state["closed"]:
            window.destroy()

    from . import owner_job_menu
    owner_job_menu.install(window,history,state,profile,Gtk,Gdk,GLib,refresh)
    window.connect("destroy", close)
    terminal.connect("child-exited", exited)
    adjust.connect("value-changed", scroll_changed)
    window.show_all()
    expander.set_expanded(False)
    state["history_expander"] = expander
    argv = [*tmux, "attach-session", "-r", "-t", "=" + SESSION]
    terminal.spawn_async(Vte.PtyFlags.DEFAULT, None, argv,
                         [key + "=" + value for key, value in terminal_env.items()],
                         GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, spawned, None)
    refresh()
    state["source"] = GLib.timeout_add_seconds(3, refresh)
    return window, terminal, history, state

def main():
    """Launched only by the existing local viewer supervisor."""
    from . import viewer
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    if os.getuid() == 0 or sys.argv[1:2] != ["--profile"] or len(sys.argv) != 3:
        return 2
    env = dict(os.environ)
    try:
        path, root, _, identity = viewer._profile(env)
        if str(path) != sys.argv[2]:
            return 2
        desktop = viewer.desktop_environment(env)
        if desktop is None:
            return 2
        with viewer._lock(root / "unified-viewer.lock"):
            profile = data.profile_at(path)
            if viewer._profile(env)[3] != identity or viewer._paused(root):
                return 2
            tmux = viewer._tmux(env)
            if not viewer._session(tmux, env) or not viewer._worker_present(root, tmux, env):
                return 2
            ready, _ = Gtk.init_check([])
            if not ready:
                return 2
            # Explicit socket/session; no shell and no editable terminal.
            # Set the embedded presentation before VTE attaches. Prevent the
            # transient second tmux header when the supervisor starts the window.
            viewer.configure_header(root, viewer.SESSION, env, tmux, embedded=True)
            create_window(profile, tmux, desktop)
            Gtk.main()
        return 0
    except Exception:
        # A UI failure must never stop or replay an execution job.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
