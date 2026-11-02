#!/usr/bin/env bash
# Stress test claude_dash.py in a throwaway tmux session; never touches other sessions.
# Run: bash stress_claude_dash.sh   (prints "ok" or the first failed check)
set -euo pipefail
DASH="$(cd "$(dirname "$0")" && pwd)/claude_dash.py"
S="dash.selftest"                      # a "." in the name broke "-t NAME" targeting
FAKE="$(mktemp -d)"; ln -s /bin/sleep "$FAKE/claude"   # pane_current_command reads "claude"
SID=""                                 # target by $id: "-t =dash.selftest" is parsed as a window
trap '[ -n "$SID" ] && tmux kill-session -t "$SID" 2>/dev/null; rm -rf "$FAKE"' EXIT

fail() { echo "FAIL: $1"; tmux capture-pane -p -t "$D" | head -8; exit 1; }
until_screen() { for _ in $(seq 40); do tmux capture-pane -p -t "$D" | grep -q -- "$1" && return 0; sleep 0.25; done; fail "$2"; }

SID=$(tmux new-session -d -P -F '#{session_id}' -s "$S" -x 120 -y 30 "$FAKE/claude 1000")
for _ in 1 2; do tmux new-window -d -t "$SID:" "$FAKE/claude 1000"; done
D=$(tmux new-window -d -P -F '#{pane_id}' -t "$SID:" -n dash "$DASH")

until_screen "$S · 3 agents" "3 fake claude panes not listed under the dotted session name"
tmux send-keys -t "$D" j j j k                       # move past the end and back
tmux send-keys -t "$D" 1                             # fake panes are not waiting
until_screen "not waiting: key not sent" "digit was not refused for a non-waiting pane"

tmux send-keys -t "$D" / 2 . 1                       # filter down to one agent
until_screen "1/3 agents" "filter /2.1 did not narrow the list"
tmux send-keys -t "$D" Escape
until_screen "$S · 3 agents" "Esc did not clear the filter"

on_pane() { for _ in $(seq 20); do tmux capture-pane -p -t "$SID:$1" | grep -q -- "$2" && return 0; sleep 0.25; done; fail "$3"; }
tmux send-keys -t "$D" i; sleep 0.5                  # selection stayed on 2.1 through the filter
tmux send-keys -t "$D" -l "hello one"; tmux send-keys -t "$D" Enter
on_pane 2.1 "hello one" "i did not deliver the reply to the selected pane 2.1"
tmux send-keys -t "$D" I; sleep 0.5                  # reply to every listed agent
tmux send-keys -t "$D" -l "hello all"; tmux send-keys -t "$D" Enter
until_screen "sent to 3 agents" "I did not report sending to 3 agents"
on_pane 3.1 "hello all" "I did not reach pane 3.1"

tmux send-keys -t "$D" X; sleep 0.5; tmux send-keys -t "$D" n Enter   # answer "no" to kill
sleep 1; until_screen "3 agents" "kill went ahead without y"
tmux send-keys -t "$D" X; sleep 0.5; tmux send-keys -t "$D" y Enter
until_screen "2 agents" "X then y did not kill a pane"

tmux resize-window -t "$SID:dash" -x 40 -y 6; sleep 1   # tiny terminal must not crash
tmux resize-window -t "$SID:dash" -x 120 -y 30
until_screen "2 agents" "dashboard died after resize"

tmux send-keys -t "$D" C-c; sleep 1                    # clean exit, no traceback
tmux list-panes -t "$SID:dash" >/dev/null 2>&1 && fail "Ctrl-C did not exit"
echo ok
