#!/usr/bin/env bash
set +e

ACTION="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
[ -n "$ACTION" ] || ACTION="ENSURE"
[ "$ACTION" = "OPEN" ] && ACTION="ENSURE"

case "$ACTION" in
  ENSURE|FOCUS|STATUS|CLOSE|TOGGLE) ;;
  *)
    echo "INTERACTIVE_TERMINAL_GATE=FAIL"
    echo "ERROR=INVALID_ACTION"
    exit 2
    ;;
esac

TITLE="FrameBridge - Terminal"
STATE_DIR="${FRAMEBRIDGE_UI_STATE_DIR:-$HOME/.local/share/framebridge/ui-state}"
STATE_FILE="$STATE_DIR/interactive-terminal.window"
DISPLAY_VALUE="$(printenv DISPLAY 2>/dev/null)"
[ -n "$DISPLAY_VALUE" ] || DISPLAY_VALUE=":0"
XAUTH_VALUE="$(printenv XAUTHORITY 2>/dev/null)"
[ -n "$XAUTH_VALUE" ] || XAUTH_VALUE="$HOME/.Xauthority"

TERM_FG="#a8ffb5"
TERM_BG="#010402"
TERM_CURSOR="#c8ffd1"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

window_exists()
{
  W="$1"
  [ -n "$W" ] || return 1
  DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"     xdotool getwindowname "$W" >/dev/null 2>&1
}

read_state_window()
{
  if [ -r "$STATE_FILE" ]; then
    head -n1 "$STATE_FILE" 2>/dev/null | tr -cd '0-9'
  fi
}

write_state_window()
{
  W="$1"
  printf '%s\n' "$W" > "$STATE_FILE"
  chmod 600 "$STATE_FILE" 2>/dev/null || true
}

clear_state()
{
  rm -f "$STATE_FILE"
}

W="$(read_state_window)"
if ! window_exists "$W"; then
  W=""
  clear_state
fi

if [ "$ACTION" = "STATUS" ]; then
  echo "INTERACTIVE_TERMINAL_GATE=PASS"
  if [ -n "$W" ]; then
    echo "STATE=ON"
    echo "WINDOW_ID=$W"
  else
    echo "STATE=OFF"
    echo "WINDOW_ID="
  fi
  echo "INTERACTIVE=YES"
  echo "STYLE=GREEN_MONOCHROME"
  exit 0
fi

if [ "$ACTION" = "TOGGLE" ]; then
  if [ -n "$W" ]; then
    ACTION="CLOSE"
  else
    ACTION="ENSURE"
  fi
fi

if [ "$ACTION" = "CLOSE" ]; then
  if [ -n "$W" ]; then
    DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       xdotool windowclose "$W" >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
      if ! window_exists "$W"; then
        break
      fi
      sleep 0.1
    done
  fi
  clear_state
  echo "INTERACTIVE_TERMINAL_GATE=PASS"
  echo "ACTION=CLOSE"
  echo "STATE=OFF"
  echo "WINDOW_ID="
  echo "INTERACTIVE=YES"
  echo "STYLE=GREEN_MONOCHROME"
  exit 0
fi

if [ -z "$W" ]; then
  BEFORE="$(mktemp)"
  AFTER="$(mktemp)"
  trap 'rm -f "$BEFORE" "$AFTER"' EXIT

  DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"     wmctrl -lx 2>/dev/null     | awk '$3 ~ /gnome-terminal-server.Gnome-terminal/ {print $1}'     | sort -u > "$BEFORE"

  export FB_TERM_TITLE="$TITLE"
  export FB_TERM_FG="$TERM_FG"
  export FB_TERM_BG="$TERM_BG"
  export FB_TERM_CURSOR="$TERM_CURSOR"

  if command -v gnome-terminal >/dev/null 2>&1; then
    DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       gnome-terminal --title="$TITLE" -- bash -lc '
        printf "\033]0;%s\007" "$FB_TERM_TITLE"
        printf "\033]10;%s\007" "$FB_TERM_FG"
        printf "\033]11;%s\007" "$FB_TERM_BG"
        printf "\033]12;%s\007" "$FB_TERM_CURSOR"
        exec bash -l
      ' >/dev/null 2>&1 &
  elif command -v xfce4-terminal >/dev/null 2>&1; then
    DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       xfce4-terminal --title="$TITLE"       --command="bash -lc 'printf \"\\033]10;$TERM_FG\\007\\033]11;$TERM_BG\\007\"; exec bash -l'"       >/dev/null 2>&1 &
  elif command -v xterm >/dev/null 2>&1; then
    DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       xterm -T "$TITLE" -fg "$TERM_FG" -bg "$TERM_BG" -e bash -l       >/dev/null 2>&1 &
  else
    echo "INTERACTIVE_TERMINAL_GATE=FAIL"
    echo "ERROR=NO_SUPPORTED_TERMINAL"
    exit 3
  fi

  for _ in $(seq 1 50); do
    TITLE_W="$(DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       xdotool search --name "^$TITLE$" 2>/dev/null | tail -n1)"
    if window_exists "$TITLE_W"; then
      W="$TITLE_W"
      break
    fi

    DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       wmctrl -lx 2>/dev/null       | awk '$3 ~ /gnome-terminal-server.Gnome-terminal/ {print $1}'       | sort -u > "$AFTER"

    NEW_HEX="$(comm -13 "$BEFORE" "$AFTER" | tail -n1)"
    if [ -n "$NEW_HEX" ]; then
      W="$((NEW_HEX))"
      if window_exists "$W"; then
        break
      fi
      W=""
    fi
    sleep 0.1
  done

  if [ -z "$W" ]; then
    echo "INTERACTIVE_TERMINAL_GATE=FAIL"
    echo "ERROR=WINDOW_NOT_FOUND"
    exit 4
  fi

  write_state_window "$W"
fi

DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"   xdotool windowactivate --sync "$W" >/dev/null 2>&1 || true

echo "INTERACTIVE_TERMINAL_GATE=PASS"
echo "ACTION=$ACTION"
echo "STATE=ON"
echo "WINDOW_ID=$W"
echo "INTERACTIVE=YES"
echo "STYLE=GREEN_MONOCHROME"
echo "TERM_FG=$TERM_FG"
echo "TERM_BG=$TERM_BG"
exit 0
