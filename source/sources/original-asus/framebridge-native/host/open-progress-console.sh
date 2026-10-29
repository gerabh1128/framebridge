#!/usr/bin/env bash
set +e

ACTION="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
[ -n "$ACTION" ] || ACTION="ENSURE"

case "$ACTION" in
  ENSURE|FOCUS|STATUS) ;;
  *)
    echo "PROGRESS_CONSOLE_GATE=FAIL"
    echo "ERROR=INVALID_ACTION"
    exit 2
    ;;
esac

TITLE="FrameBridge - Progreso DIG"
LOG_DIR="${FRAMEBRIDGE_PROGRESS_DIR:-$HOME/.local/state/framebridge/progress}"
LOG="${FRAMEBRIDGE_PROGRESS_LOG:-$LOG_DIR/framebridge-progress.log}"
DISPLAY_VALUE="$(printenv DISPLAY 2>/dev/null)"
[ -n "$DISPLAY_VALUE" ] || DISPLAY_VALUE=":0"
XAUTH_VALUE="$(printenv XAUTHORITY 2>/dev/null)"
[ -n "$XAUTH_VALUE" ] || XAUTH_VALUE="$HOME/.Xauthority"

mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR" 2>/dev/null || true
touch "$LOG"
chmod 600 "$LOG" 2>/dev/null || true

if [ ! -s "$LOG" ]; then
  cat > "$LOG" <<'HEADER'
============================================================
 FRAMEBRIDGE ASUS // DIG PROGRESS
 MODO: SOLO LECTURA
============================================================
HEADER
fi

find_window()
{
  DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"     xdotool search --name "^$TITLE$" 2>/dev/null | tail -n1
}

W="$(find_window)"

if [ "$ACTION" = "STATUS" ]; then
  echo "PROGRESS_CONSOLE_GATE=PASS"
  if [ -n "$W" ]; then
    echo "STATE=ON"
    echo "WINDOW_ID=$W"
  else
    echo "STATE=OFF"
    echo "WINDOW_ID="
  fi
  echo "LOG=$LOG"
  echo "READ_ONLY=YES"
  echo "STYLE=GREEN_MONOCHROME_DIG"
  exit 0
fi

if [ -z "$W" ]; then
  export FB_PROGRESS_LOG="$LOG"
  export FB_PROGRESS_TITLE="$TITLE"

  if command -v gnome-terminal >/dev/null 2>&1; then
    DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       gnome-terminal --title="$TITLE" --       bash -lc '
        printf "\033]0;%s\007" "$FB_PROGRESS_TITLE"
        printf "\033]10;#a8ffb5\007"
        printf "\033]11;#010402\007"
        printf "\033[1;38;2;168;255;181m"
        printf "============================================================\n"
        printf " FRAMEBRIDGE // DIG PROGRESS // SOLO LECTURA\n"
        printf "============================================================\n"
        exec tail -n 200 -F "$FB_PROGRESS_LOG"
      ' >/dev/null 2>&1 &
  elif command -v xterm >/dev/null 2>&1; then
    DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       xterm -T "$TITLE" -fg "#a8ffb5" -bg "#010402"       -e bash -lc 'exec tail -n 200 -F "$FB_PROGRESS_LOG"'       >/dev/null 2>&1 &
  else
    echo "PROGRESS_CONSOLE_GATE=FAIL"
    echo "ERROR=NO_SUPPORTED_TERMINAL"
    exit 3
  fi

  for _ in $(seq 1 50); do
    W="$(find_window)"
    [ -n "$W" ] && break
    sleep 0.1
  done
fi

if [ -z "$W" ]; then
  echo "PROGRESS_CONSOLE_GATE=FAIL"
  echo "ERROR=WINDOW_NOT_FOUND"
  exit 4
fi

if command -v wmctrl >/dev/null 2>&1; then
  GEOM="$(DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE" xdotool getdisplaygeometry 2>/dev/null)"
  SW="$(printf '%s' "$GEOM" | awk '{print $1}')"
  SH="$(printf '%s' "$GEOM" | awk '{print $2}')"
  if [ -n "$SW" ] && [ -n "$SH" ]; then
    H=330
    Y=$((SH - H - 28))
    [ "$Y" -lt 0 ] && Y=0
    DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"       wmctrl -ir "$W" -e "0,0,$Y,$SW,$H" >/dev/null 2>&1 || true
  fi
fi

if [ "$ACTION" = "FOCUS" ]; then
  DISPLAY="$DISPLAY_VALUE" XAUTHORITY="$XAUTH_VALUE"     xdotool windowactivate --sync "$W" >/dev/null 2>&1 || true
fi

echo "PROGRESS_CONSOLE_GATE=PASS"
echo "ACTION=$ACTION"
echo "STATE=ON"
echo "WINDOW_ID=$W"
echo "LOG=$LOG"
echo "READ_ONLY=YES"
echo "STYLE=GREEN_MONOCHROME_DIG"
exit 0
