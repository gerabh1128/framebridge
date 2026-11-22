#!/usr/bin/env bash
set -euo pipefail
SESSION="framebridge"
tmux has-session -t "$SESSION" 2>/dev/null || tmux new-session -d -s "$SESSION"

if command -v gnome-terminal >/dev/null 2>&1; then
  exec gnome-terminal -- bash -lc "tmux attach -t '$SESSION'"
elif command -v xfce4-terminal >/dev/null 2>&1; then
  exec xfce4-terminal --command="bash -lc \"tmux attach -t '$SESSION'\""
elif command -v konsole >/dev/null 2>&1; then
  exec konsole -e bash -lc "tmux attach -t '$SESSION'"
elif command -v xterm >/dev/null 2>&1; then
  exec xterm -e tmux attach -t "$SESSION"
else
  echo "No encontré terminal gráfica compatible."
  echo "Ejecuta manualmente: tmux attach -t $SESSION"
  exit 1
fi
