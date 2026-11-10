# Screen procedure for ChatGPT and coding assistants

Documentation branch **0.3.7.10**, based on FrameBridge **0.3.7**. This branch
clarifies route selection. It does not install an update or certify new hardware,
desktop sessions or input capabilities.

## Choose the route from the actual session

| Object or operation | Correct interpretation and route |
| --- | --- |
| Active physical VT, /dev/ttyN | Visible TTY control needs a tmux client directly attached to that VT and a fresh capture binding. |
| Cinnamon or another supported X11 desktop | Use graphical `framebridge_cua` for observation and input on the confirmed target. |
| GNOME Terminal or another terminal emulator in X11 | Its shell and tmux client normally use /dev/pts/N. Type through graphical CUA when interacting with the visible window. |
| FBEXEC or a CLI tty transcript | Command execution or terminal text; neither is authentic screen evidence. |
| Virtual X11 display | Label as virtual. Its image does not prove access to the physical desktop. |
| Native Wayland | Full desktop capture/input is not validated by this release. XWayland alone is insufficient. |

The desktop's VT number and X11 DISPLAY identify different layers. Neither
tty7 nor DISPLAY=:0 is a universal requirement; inspect the current session.

## Supported graphical X11 procedure

1. Read `framebridge_help(topic="start")`, then topics `screens` and `nodes`.
   Confirm the receiver, installed version and enrolled target. `LOCAL` means
   the receiver. Preserve the same explicit target throughout the operation.
2. Call `framebridge_cua(action="OBSERVE", authorized=true,
   target="<confirmed alias>", capture_scope="ROOT")`. This selects graphical
   observation. The legacy `capture_scope="node:ALIAS"` is observation only and
   uses a different real-screen route; do not use it to select graphical input.
3. Inspect the operation result and actual image. For physical desktop work
   require `display_context.kind="physical-x11"` and `physical=true`. A
   registered tool, loaded module or successful outer wrapper does not prove
   capture. Stop and inspect the precise graphical error if observation fails.
4. For input into the active application, get a fresh `OBSERVE` with
   `capture_scope="WINDOW"`. Check the intended window and use its
   `ACTIVE_WINDOW_ID`, `SOURCE_WIDTH` and `SOURCE_HEIGHT` as
   `expected_window_id`, `expected_source_width` and
   `expected_source_height` on the input call. Keep the target, capture scope
   and coordinate space consistent. Pass `coord_space="WINDOW"` on every input
   call, including TYPE and KEY, or `coord_space="WINDOW_PREVIEW"` for preview
   coordinates. The default is ROOT: `capture_scope="WINDOW"` alone does not
   select window input geometry. `WIDTH/HEIGHT` describe the preview; do not
   substitute them for source dimensions. Omission can produce
   `DISPLAY_GEOMETRY_CHANGED` despite a valid window observation.
5. Execute only the authorized action. Use `observe_after=true` or take a new
   observation and inspect the result. If post-observation fails, do not infer
   that input failed. If `input_may_have_been_sent` or `do_not_replay` is true,
   reobserve and do not automatically replay the action. Report uncertain
   effects explicitly. One controller at a time shares the desktop.

A ROOT observation proves only the observed pixels and context. Keyboard,
typing, clicks and other actions require their own verified effects.

## Interpret the TTY error narrowly

`real_screen/screen_control.py:snapshot_binding()` reads the active VT and
looks for a tmux client whose `client_tty` equals `/dev/<active VT>`. A tmux
client on /dev/pts/N inside an X11 terminal correctly does not match it.
`SCREEN_VISIBLE_TMUX_NOT_FOUND` means no matching visible TTY client was
found by that backend. It does not establish a defect in graphical CUA.

The real-screen observation path can return authentic X11 pixels while reporting
`control_available=false` and that TTY error. Keep pixel evidence and TTY
control readiness separate. `framebridge_screen_key/text` and
`cua_tty_key_send/text_type` require visible TTY binding; they are not the
graphical input procedure for an X11 terminal.

Do not switch to tty1 or another VT, attach tmux to /dev/tty7, change permissions,
restart services, alter sessions or edit installed code simply to make that TTY
check succeed during graphical work. Diagnose an actual error through the
graphical route first. Propose changes only with evidence and authorization.

## Source references

- `source/expanded/framebridge_expanded/real_screen/screen_control.py`:
  `snapshot_binding`, active VT and visible tmux client matching.
- `source/expanded/framebridge_expanded/real_screen/screen_rpc.py`:
  observation and separate TTY control availability.
- `source/expanded/framebridge_expanded/real_screen/screen_frontdoor.py`:
  capture-linked visible TTY key/text operations.
- `source/expanded/framebridge_expanded/display_context.py` and
  `native_host.py`: graphical context and uncertain-input metadata.
- `source/portable/front_door_mcp.py`: explicit graphical target and scope.
