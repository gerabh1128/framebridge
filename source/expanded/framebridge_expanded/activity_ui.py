"""Local tray controls only. FBTERM is the sole informational window."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import os
import sys

from . import activity_data as data, viewer, update_control

MESSAGES = {
    "PROFILE_NOT_CONFIGURED": "No configured local profile",
    "WORK_ACTIVE_OR_UNCONFIRMED": "Active, queued or unconfirmed work",
    "INITIAL_SETUP_REQUIRES_TERMINAL": "Initial setup requires the owner terminal",
    "STOP_NOT_CONFIRMED": "Stop not confirmed",
    "QUERY_UNAVAILABLE": "Observation unavailable",
    "COMMAND_REJECTED_OR_UNAVAILABLE": "Operation unavailable",
}


def message(exc):
    if isinstance(exc, data.ObservationError):
        return MESSAGES.get(str(exc), "Local operation not verified")
    if isinstance(exc, viewer.Waiting):
        return "FBTERM unavailable; check the local service"
    return "Local operation not verified"


def main():
    if os.getuid() == 0 or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return 0
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gio, GLib
    ready, _ = Gtk.init_check([])
    if not ready:
        return 0
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as indicators
    except (ImportError, ValueError):
        indicators = None

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="org.framebridge.Tray",
                             flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
            self.initialized = False
            self.p = None
            self.status = None
            self.local = None
            self.update_state = None
            self.pending = set()
            self.pool = ThreadPoolExecutor(max_workers=2)
            self.stopped = False
            self.indicator = None
            self.legacy = None
            self.watch = 0
            self.timer = 0
            self.error = None

        def do_command_line(self, cmd):
            args = cmd.get_arguments()[1:]
            if args not in ([], ["--show"], ["--autostart"]):
                return 2
            self.activate()
            if args != ["--autostart"]:
                self.open_viewer()
            return 0

        def do_activate(self):
            if self.initialized:
                return
            self.initialized = True
            self.hold()
            try:
                self.p = data.profile()
            except Exception as e:
                self.error = message(e)
            self.build_menu()
            self.watch = Gio.bus_watch_name(Gio.BusType.SESSION, "org.kde.StatusNotifierWatcher",
                Gio.BusNameWatcherFlags.NONE, self.watcher_appeared, self.watcher_vanished)
            self.refresh_local()
            self.timer = GLib.timeout_add_seconds(5, self.tick)

        def task(self, key, fn, done, failed=None):
            if self.stopped or key in self.pending:
                return
            self.pending.add(key)
            future = self.pool.submit(fn)
            def deliver():
                self.pending.discard(key)
                if self.stopped:
                    return False
                try:
                    result = future.result()
                except Exception as e:
                    self.error = message(e)
                    if failed:
                        failed(e)
                else:
                    done(result)
                self.update_menu()
                return False
            future.add_done_callback(lambda _: GLib.idle_add(deliver))
            self.update_menu()

        def item(self, label, callback):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", callback)
            self.menu.append(item)
            return item

        def build_menu(self):
            self.menu = Gtk.Menu()
            self.menu_status = Gtk.MenuItem(label=">framebridge_ | Verifying")
            self.menu_status.set_sensitive(False)
            self.menu.append(self.menu_status)
            self.open_item = self.item("Open FBTERM", self.open_viewer)
            self.minimize_item = self.item("Minimize FBTERM", self.minimize_viewer)
            self.menu.append(Gtk.SeparatorMenuItem())
            self.start_item = self.item("Start service", lambda *_: self.admin("start"))
            self.restart_item = self.item("Restart service…", lambda *_: self.admin("restart"))
            self.stop_item = self.item("Stop service…", lambda *_: self.admin("stop"))
            self.prepare_item = self.item("Prepare APT update…", lambda *_: self.update_admission("prepare"))
            self.resume_item = self.item("Resume accepting jobs…", lambda *_: self.update_admission("resume"))
            self.menu.append(Gtk.SeparatorMenuItem())
            self.item("Refresh local status", lambda *_: self.refresh_local())
            self.reload_item = self.item("Reload selected profile", self.reload_profile)
            self.menu.show_all()
            self.update_menu()

        def update_menu(self):
            if not self.initialized or not hasattr(self, "menu_status"):
                return
            st = self.status or {}
            usable = self.p is not None and self.status is not None
            running = st.get("running") is True
            managed = st.get("managed") is True
            clear = self.local is not None and not self.local.get("other") and not any(
                self.local["counts"].get(s, 0) for s in ("queued", "running", "in_flight", "outcome_unknown"))
            state, txt = data.activity_indicator(self.status, self.local)
            gate=self.update_state
            if gate and gate["admission_paused"]:
                txt += " | " + ("Queue drained" if gate["queue_drained"] else "Update preparation")
                if state != "busy":state="attention"
            self.menu_status.set_label((self.p.name if self.p else ">framebridge_") + " | " + (self.error or txt))
            self.prepare_item.set_sensitive(usable and managed and gate is not None and not gate["admission_paused"] and "update" not in self.pending)
            self.resume_item.set_sensitive(usable and gate is not None and gate["admission_paused"] and "update" not in self.pending)
            self.start_item.set_sensitive(usable and managed and not running and "admin" not in self.pending)
            self.restart_item.set_sensitive(usable and managed and running and clear and "admin" not in self.pending)
            self.stop_item.set_sensitive(usable and managed and running and clear and "admin" not in self.pending)
            self.open_item.set_sensitive(self.p is not None and "viewer" not in self.pending)
            self.minimize_item.set_sensitive(self.p is not None and "viewer" not in self.pending)
            self.reload_item.set_sensitive(not self.pending)
            icon = "framebridge-tray-" + state
            if self.legacy is not None:
                self.legacy.set_tooltip_text(">framebridge_ | " + txt)
                self.legacy.set_from_icon_name(icon)
            if self.indicator is not None:
                self.indicator.set_icon_full(icon, ">framebridge_ | " + txt)

        def watcher_appeared(self, *_):
            if indicators is None:
                self.watcher_vanished()
                return
            if self.indicator is None:
                self.indicator = indicators.Indicator.new("framebridge-tray", "framebridge-tray",
                    indicators.IndicatorCategory.APPLICATION_STATUS)
                self.indicator.set_icon_theme_path("/usr/share/icons/hicolor/scalable/apps")
                self.indicator.set_title("FrameBridge")
                self.indicator.set_menu(self.menu)
            if self.legacy is not None:
                self.legacy.set_visible(False)
            self.indicator.set_status(indicators.IndicatorStatus.ACTIVE)
            self.update_menu()

        def watcher_vanished(self, *_):
            if self.indicator is not None:
                self.indicator.set_status(indicators.IndicatorStatus.PASSIVE)
            if self.legacy is None:
                self.legacy = Gtk.StatusIcon.new_from_icon_name("framebridge-tray")
                self.legacy.set_title("FrameBridge")
                self.legacy.connect("popup-menu", lambda icon,b,t: self.menu.popup(
                    None, None, Gtk.StatusIcon.position_menu, icon, b, t))
                self.legacy.connect("activate", self.open_viewer)
            self.legacy.set_visible(True)
            self.update_menu()

        def refresh_local(self):
            if self.p is None:
                return
            p = self.p
            def read():
                st = data.local_status(p)
                try:
                    history = data.local_jobs(p)
                except (OSError, data.ObservationError):
                    history = None
                try:
                    gate = update_control.control(p,"status")
                except (OSError, data.ObservationError):
                    gate = None
                return st, history, gate
            def done(value):
                self.status, self.local, self.update_state = value
                self.error = None
            def failed(e):
                self.status = self.local = self.update_state = None
            self.task("local", read, done, failed)

        def tick(self):
            self.refresh_local()
            return not self.stopped

        def view(self, action):
            if not self.p:
                return
            p = self.p
            self.task("viewer", lambda: viewer.window_action(p.path, action),
                      lambda _: setattr(self, "error", None))

        def open_viewer(self, *_):
            self.view("open")

        def minimize_viewer(self, *_):
            self.view("minimize")

        def reload_profile(self, *_):
            if self.pending:
                return
            try:
                self.p = data.profile()
                self.status = self.local = self.update_state = None
                self.error = None
            except Exception as e:
                self.error = message(e)
            self.update_menu()
            self.refresh_local()

        def update_admission(self,action):
            if self.p is None or self.update_state is None:return
            p=self.p
            d=Gtk.MessageDialog(title=">framebridge_",modal=True,message_type=Gtk.MessageType.QUESTION,
                 buttons=Gtk.ButtonsType.OK_CANCEL,text="Prepare local APT update?" if action=="prepare" else "Resume accepting new jobs?")
            d.format_secondary_text("Existing jobs may finish. No job is cancelled or repeated. Review the queue, detached effects and all profiles before stopping the service and running the system updater. This does not run APT." if action=="prepare" else "Resume new local EXEC admissions only after maintenance and verification. Existing receipts remain intact.")
            d.set_default_response(Gtk.ResponseType.CANCEL)
            answer=d.run();d.destroy()
            if answer!=Gtk.ResponseType.OK:return
            def done(v):
                self.update_state=v
                self.error=None
                self.refresh_local()
            self.task("update",lambda:update_control.control(p,action),done)

        def admin(self, action):
            if not self.p or not self.status or not self.status.get("managed"):
                return
            p = self.p
            if action in ("stop", "restart"):
                dialog = Gtk.MessageDialog(title=">framebridge_", modal=True,
                    message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL,
                    text=("Restart" if action == "restart" else "Stop") + " the local FrameBridge service?")
                dialog.format_secondary_text("Only the selected local profile is affected. Active, queued and unconfirmed work must be reviewed. Do not submit more work during maintenance. The queue check is not an atomic drain. Boot policy and existing job receipts are preserved.")
                result = dialog.run()
                dialog.destroy()
                if result != Gtk.ResponseType.OK:
                    return
            def done(value):
                self.error = None
                self.refresh_local()
            def failed(e):
                self.refresh_local()
            self.task("admin", lambda: data.service(p, action), done, failed)

        def do_shutdown(self):
            self.stopped = True
            if self.timer:
                GLib.source_remove(self.timer)
            if self.watch:
                Gio.bus_unwatch_name(self.watch)
            if self.legacy is not None:
                self.legacy.set_visible(False)
            if self.indicator is not None:
                self.indicator.set_status(indicators.IndicatorStatus.PASSIVE)
            self.pool.shutdown(wait=False, cancel_futures=True)
            Gtk.Application.do_shutdown(self)

    return App().run(sys.argv)
