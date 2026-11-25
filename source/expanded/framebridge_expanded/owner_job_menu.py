"""Read-only GTK context menu bound to one exact recorded local job."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from . import activity_data as data, owner_jobs as owner

def identity(row):
    return row["id"],row["digest"],row["request"]

def selected(profile,expected):
    row=owner.row(profile,expected[0])
    data.require(identity(row)==expected,"JOB_IDENTITY_CHANGED")
    return row

def interval(row):
    # SQLite started is the scheduler claim, not an independently measured
    # command start. Do not infer elapsed time for active/unknown jobs.
    if row.get("state")!="completed":return None
    a,b=data.timestamp(row.get("started")),data.timestamp(row.get("finished"))
    if a is None or b is None or b<a:return None
    return b-a

def duration(row):
    v=interval(row)
    if v is None:
        return "N/A" if row.get("state") in ("not_started","expired_before_start") else "-"
    if v<1:return "<1s"
    s=int(v)
    if s<60:return str(s)+"s"
    if s<3600:return f"{s//60}m {s%60:02d}s"
    if s<86400:return f"{s//3600}h {(s%3600)//60:02d}m"
    return f"{s//86400}d {(s%86400)//3600:02d}h"

def clock(value):
    t=data.timestamp(value)
    return datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if t is not None else "Unverified"

def detail_text(r):
    from .unified_viewer import job_state
    st=job_state(dict(r,exit_code=str(r["exit_code"]) if r.get("exit_code") is not None else None))
    code=r.get("exit_code")
    state=r.get("state")
    exit_label=("N/A" if state in ("not_started","expired_before_start") else "-" if state in ("queued","running","in_flight") else str(code) if state=="completed" and code is not None else "Unconfirmed")
    values=[("Job",r["id"]),("State",st),("Exit",exit_label),
            ("Recorded duration",duration(r)),("Created",clock(r.get("created"))),
            ("Claimed",clock(r.get("started"))),("Finished",clock(r.get("finished"))),
            ("Capture",r.get("capture_status") or "Unconfirmed"),
            ("Command SHA-256",r["digest"])]
    if r.get("owner_cancel_signal_sent"):
        values.append(("Historical cancellation","Signal sent to owned command group; effects not reverted"))
    if r.get("owner_cancel_before_exec"):
        values.append(("Historical cancellation","Cancelled before command execution"))
    if r.get("cancellation_requested") and not (r.get("owner_cancel_signal_sent") or r.get("owner_cancel_before_exec")):
        values.append(("Historical cancellation request","Recorded; outcome not established by the request"))
    if r.get("error"):values.append(("Receipt",r["error"]))
    if state=="outcome_unknown":
        values.append(("Caution","Outcome unconfirmed. Do not replay. Ask the authorized administrator to inspect independent evidence."))
    elif state=="completed" and code is not None and str(code)!="0":
        values.append(("Caution","Exit code alone does not establish the cause or external effects."))
    return "\n".join(k+": "+str(v) for k,v in values)+"\n\nDuration is the recorded claim-to-finish interval, when available. This viewer does not execute or modify jobs."

def read_details(profile,expected):
    r=owner.details(profile,expected[0])
    data.require(identity(r)==expected,"JOB_IDENTITY_CHANGED")
    return detail_text(r)

def read_output(profile,expected):
    r=selected(profile,expected)
    data.require(r["state"]=="completed","SAFE_OUTPUT_UNAVAILABLE")
    v=data.job_output(profile,expected[0],expected_digest=expected[1],expected_request=expected[2])
    selected(profile,expected)
    return v

def install(window,history,state,profile,Gtk,Gdk,GLib,refresh):
    active_menus=[];pending=set()
    observations=ThreadPoolExecutor(max_workers=1)
    window.connect("destroy",lambda *_:observations.shutdown(wait=False,cancel_futures=True))
    def alert(title,detail,kind=None):
        d=Gtk.MessageDialog(transient_for=window,modal=True,
            message_type=kind or Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.OK,text=title)
        d.format_secondary_text(detail);d.run();d.destroy()
    def report_failure():
        alert("Information unavailable","The exact job or verified output is unavailable. No job was changed.",
              Gtk.MessageType.WARNING)
    def background(key,fn,done):
        if key in pending:return
        pending.add(key)
        future=observations.submit(fn)
        def deliver():
            pending.discard(key)
            if state.get("closed"):return False
            try:v=future.result()
            except Exception:report_failure()
            else:done(v)
            return False
        future.add_done_callback(lambda _:GLib.idle_add(deliver))
    def display_output(v):
        d=Gtk.Dialog(title="Verified output",transient_for=window,modal=True)
        d.set_default_size(780,460);d.add_button("Close",Gtk.ResponseType.CLOSE)
        text=Gtk.TextView();text.set_editable(False);text.set_monospace(True)
        text.get_buffer().set_text(v["text"]+("\n[Output preview truncated]" if v.get("truncated") else ""))
        sc=Gtk.ScrolledWindow();sc.add(text);d.get_content_area().pack_start(sc,True,True,0)
        d.show_all();d.run();d.destroy()
    def open_menu(event):
        if event.button!=3:return False
        bx,by=history.window_to_buffer_coords(Gtk.TextWindowType.TEXT,int(event.x),int(event.y))
        it,top=history.get_line_at_y(by);line=it.get_line()
        rows=state.get("rows",[])
        if line>=len(rows):return True
        y,height=history.get_line_yrange(it)
        if not y<=by<y+height:return True
        jid=rows[line]["id"]
        try:
            r=owner.row(profile,jid);expected=identity(r)
        except Exception:
            report_failure();return True
        b=history.get_buffer();start=b.get_iter_at_line(line);end=start.copy();end.forward_to_line_end()
        b.select_range(start,end)
        menu=Gtk.Menu()
        def item(label,fn,sensitive=True):
            i=Gtk.MenuItem(label=label);i.set_sensitive(sensitive)
            i.connect("activate",lambda *_:fn());menu.append(i)
        item(jid,lambda:None,False)
        menu.append(Gtk.SeparatorMenuItem())
        item("View job details…",lambda:background(("details",expected),lambda:read_details(profile,expected),lambda v:alert("Job details",v)))
        item("View verified output…",lambda:background(("output",expected),lambda:read_output(profile,expected),display_output),
             r["state"]=="completed")
        menu.append(Gtk.SeparatorMenuItem())
        item("Copy job ID",lambda:Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(jid,-1))
        menu.show_all();state["last_owner_menu"]=menu;state["last_owner_job"]=jid
        active_menus.append(menu)
        def dismissed(*_):
            if menu in active_menus:active_menus.remove(menu)
        menu.connect("deactivate",dismissed)
        menu.popup_at_pointer(event)
        return True
    history.connect("button-press-event",lambda _w,e:open_menu(e))
    state["owner_context_menu"]=open_menu
