# Original FBTERM viewer — profile isolation and pause

Each Expanded profile keeps one graphical FBTERM window titled exactly
\`>framebridge_\`. The visible Profile ID is a stable digest of the normalized
local state path, not a node or job identifier. Its X11 window class and
owned process identify the profile. Other profiles are not mistaken for it.

The Original VTE terminal occupies most of the window and retains the
classic dark background, light-gray text and neutral gray status header.
Its scrollbar remains available; terminal input is disabled and the client
attaches to the Original tmux socket with `attach-session -r`. Recorded jobs
are available in a collapsed-by-default history panel below the terminal,
with independent scrolling. Job IDs remain exact. Non-started and expired
jobs show EXIT: N/A, active and queued jobs show EXIT: -, and uncertain
outcomes are distinguished from jobs that did not execute. Ordinary successful
jobs use neutral text, not saturated green or cyan. No request text is inferred
from a job ID. The embedded tmux header is disabled before attachment to avoid
duplicating the outer header. On a running profile, closing the viewer allows
the supervisor to reopen it; minimizing it does not raise it. The tray opens
or minimizes the same viewer explicitly.

A still-attached older GNOME Terminal viewer is recognized and is not
duplicated. Close it during the normal cold-upgrade procedure. The
historical Original engine retains its old title-based visibility probe;
use the profile-bound Expanded viewer observation for the new layout.

Use these commands as the regular profile owner, with the intended profile
selected or explicitly supplied via --state-dir:

- fb viewer status — inspect automatic reopening policy.
- fb viewer pause — persistently suppress automatic opening and idle viewer
  bootstrap. Existing windows are not closed; close one manually if desired.
- fb viewer resume — allow automatic opening again when the original runtime
  and display are ready. No task is submitted or repeated.

These commands do not stop the runtime, change authentication, create a job,
release an uncertain reservation, close another profile's window, or alter
the queue. Pause applies across normal runtime restart and reboot. If the
private marker is unsafe, fail closed. The native Original worker and its
execution receipt handling are unchanged.

## Migration from an externally installed legacy viewer keeper

The modern profile supervisor alone owns header visibility. Refreshing the
header text without an explicit layout decision preserves the existing tmux
status setting. Only one verified legacy window and one verified read-only
client permit the legacy status bar; mixed or incomplete observations do not.

Before a cold upgrade, inventory the profile owner's systemd user timers and
their exact ExecStart. Older separately installed viewer keepers may still
force a second tmux header even when the new package is correct. If such a
keeper exists for this exact profile, verify and back up its unit files,
script and enablement state. With the owner's approval, retire only that
obsolete timer and its associated oneshot service. Preserve the files for
rollback. Do not stop the managed profile service, disable unrelated timers,
or modify authentication, queue records or the Original tmux session.
The package does not automatically disable external user units. Validate
absence of a competing writer during real execution before accepting the
upgrade. A return to a legacy configuration requires an explicit rollback
decision, not automatic restoration of a competing header writer.
