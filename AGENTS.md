# FrameBridge instructions for coding assistants

Read [the agent start guide](docs/FRAMEBRIDGE-AGENT-START.md) before operating and
[the screen procedure](docs/FRAMEBRIDGE-SCREEN-PROCEDURE.md) before screen work.
Use the installed versioned help: first topic `start`, then `screens` and `nodes`
as relevant. Confirm the receiver, selected target, installed version and actual
capabilities. Instructions do not grant authorization.

## Select the screen route before diagnosing a failure

- A physical VT, an X11 desktop, a terminal emulator PTY and a tmux session are
  different objects. An X11 desktop on tty7 can normally have a terminal and
  tmux client on /dev/pts/N. Do not treat the PTY as a defect.
- For supported graphical X11 use `framebridge_cua` with an explicit confirmed
  target. Observe ROOT, inspect authentic pixels and the display context; for
  physical desktop work require `kind=physical-x11` and `physical=true`.
- Before graphical window input observe WINDOW and bind the expected window
  and source dimensions to the returned values. On every input call, including
  TYPE and KEY, pass coord_space=WINDOW (or WINDOW_PREVIEW for preview
  coordinates); capture_scope=WINDOW alone does not select input geometry.
  Keep the target consistent. Observe afterwards and verify the intended effect.
- `screen_control.py` visible-tmux control requires a client directly on the
  active /dev/ttyN. `SCREEN_VISIBLE_TMUX_NOT_FOUND` and `control_available=false`
  from that route are not proof of a graphical CUA failure.
- `framebridge_screen_observe` may still capture valid X11 pixels. Its TTY
  control metadata does not certify graphical input. `framebridge_screen_key`,
  `framebridge_screen_text` and CUA-TTY key/text tools require visible TTY
  binding. Use graphical CUA to type in a desktop terminal.
- Do not switch VT, create or reattach tmux on a physical VT, escalate
  permissions, restart services or edit installed software merely to satisfy a
  TTY check for an X11 task. Inspect the actual graphical error before proposing
  changes. Reuse the user's authorization; request approval for new risky scope.
- Reobserve uncertain input. If `input_may_have_been_sent` or `do_not_replay`
  is true, do not automatically replay it. Capture success does not prove input.
- Do not hardcode tty7 or DISPLAY=:0 as universal requirements. A virtual X11
  display is not the physical desktop; native Wayland control is not validated.

## Development scope

Use a separate source checkout for requested changes. Preserve installed
services, profiles, sessions and credentials. Test affected surfaces and report
what was actually verified. A branch name is not a built or signed release.
