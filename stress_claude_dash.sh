#!/usr/bin/env bash
# Stress test claude_dash.py in throwaway tmux sessions with fake agents; never touches other sessions.
# Run: bash stress_claude_dash.sh   (prints "ok" or the first failed check)
#
# Fake agents: a copy of sleep named `claude` (so tmux shows the pane as claude), plus a fake
# `claude agents --json` on the dashboard's PATH that lists them with the status in $FAKE/status/<pid>
# (idle unless the file says otherwise), so a test can make an agent wait.
set -euo pipefail
DASH="$(cd "$(dirname "$0")" && pwd)/claude_dash.py"
S="dash.selftest"                      # a "." in the name broke "-t NAME" targeting
FAKE="$(mktemp -d)"
mkdir -p "$FAKE/bin" "$FAKE/shim" "$FAKE/status"
cp /bin/sleep "$FAKE/bin/claude"
cat > "$FAKE/shim/claude" <<'SHIM'
#!/usr/bin/env bash
[ "$1" = agents ] || exit 1
tmux list-panes -s -t "$DASH_SID" -F '#{pane_pid} #{pane_current_command} #{window_index}.#{pane_index}' |
while read -r pid cmd target; do
  [ "$cmd" = claude ] || continue
  st=$(cat "$FAKE/status/$pid" 2>/dev/null || echo idle)
  why=$([ "$st" = waiting ] && echo ',"waitingFor":"permission prompt"' || true)
  echo "{\"pid\":$pid,\"kind\":\"interactive\",\"status\":\"$st\",\"name\":\"fake-$target\",\"startedAt\":1$why}"
done | paste -sd, | sed 's/^/[/; s/$/]/'
SHIM
chmod +x "$FAKE/shim/claude"
SID=""; SID2=""                        # target sessions by $id: "-t =dash.selftest" is parsed as a window
trap 'for s in $SID $SID2; do tmux kill-session -t "$s" 2>/dev/null; done; rm -rf "$FAKE"' EXIT

fail() { echo "FAIL: $1"; tmux capture-pane -p -t "$D" 2>/dev/null | head -12; exit 1; }
until_screen() { for _ in $(seq 40); do tmux capture-pane -p -t "$D" | grep -qF -- "$1" && return 0; sleep 0.25; done; fail "$2"; }
on_pane() { for _ in $(seq 20); do tmux capture-pane -p -t "$SID:$1" | grep -qF -- "$2" && return 0; sleep 0.25; done; fail "$3"; }
pid_of() { tmux display -p -t "$SID:$1" '#{pane_pid}'; }
# session ids start with "$": quote them, or the pane's shell expands "$70" to "$7" "0" (session 0!)
dash() { tmux new-window -d -P -F '#{pane_id}' -t "$1:" -n dash \
  "env PATH=$FAKE/shim:\$PATH DASH_SID='$SID' FAKE=$FAKE $DASH '$2'"; }

SID=$(tmux new-session -d -P -F '#{session_id}' -s "$S" -x 120 -y 30 "$FAKE/bin/claude 1000")
for _ in 1 2; do tmux new-window -d -t "$SID:" "$FAKE/bin/claude 1000"; done
D=$(dash "$SID" "")

until_screen "$S · 3 agents" "3 fake agents not listed under the dotted session name"
until_screen "3 idle" "fake agents not idle"
tmux send-keys -t "$D" j j j k                       # move past the end and back
tmux send-keys -t "$D" 1                             # idle agents get no prompt keys
until_screen "not waiting: key not sent" "digit was not refused for an idle agent"

tmux send-keys -t "$D" / 2 . 1                       # filter down to one agent
until_screen "1/3 agents" "filter /2.1 did not narrow the list"
tmux send-keys -t "$D" Escape
until_screen "$S · 3 agents" "Esc did not clear the filter"

tmux send-keys -t "$D" i; sleep 0.4                  # selection stayed on 2.1 through the filter
tmux send-keys -t "$D" -l "hello one"; tmux send-keys -t "$D" Enter
on_pane 2.1 "hello one" "i did not deliver the reply to the selected pane 2.1"
tmux send-keys -t "$D" I; sleep 0.4                  # reply to every listed agent
tmux send-keys -t "$D" -l "hello all"; tmux send-keys -t "$D" Enter
until_screen "sent to 3 agents" "I did not report sending to 3 agents"
on_pane 3.1 "hello all" "I did not reach pane 3.1"

tmux send-keys -t "$D" r; sleep 0.4; tmux send-keys -t "$D" 1   # reply 1 = "continue" prefills the box
until_screen "❯ continue█" "r 1 did not prefill the box"
tmux capture-pane -p -t "$SID:2.1" | grep -q "continue" && fail "prefilled reply sent before Enter"
tmux send-keys -t "$D" -l " please"; tmux send-keys -t "$D" Enter   # edit it, then send
on_pane 2.1 "continue please" "edited prefilled reply did not reach pane 2.1"

tmux send-keys -t "$D" i; sleep 0.4                  # a bracketed paste with a newline stays in the box
tmux set-buffer -b dashstress "line one
line two"; tmux paste-buffer -p -d -b dashstress -t "$D"; sleep 0.5
until_screen "line two" "pasted second line not shown in the box"
tmux capture-pane -p -t "$SID:2.1" | grep -q "line one" && fail "paste sent before Enter"
tmux send-keys -t "$D" Enter
on_pane 2.1 "line two" "multi-line message did not reach pane 2.1"

# adversarial input: none of these may run a command or send anything
tmux send-keys -t "$D" r; sleep 0.4                  # a paste into the reply menu must not pick a reply
tmux set-buffer -b dashstress "2
x"; tmux paste-buffer -p -d -b dashstress -t "$D"; sleep 1
tmux capture-pane -p -t "$SID:2.1" | grep -q "go ahead" && fail "paste into the reply menu sent a reply"
tmux send-keys -t "$D" Escape; sleep 0.3
tmux send-keys -t "$D" M-X; sleep 0.8                # Alt-X must not open the kill prompt
tmux capture-pane -p -t "$D" | grep -qF "[y/N]" && fail "Alt-X ran X"
tmux send-keys -t "$D" -H 1b 5b 1b 4f 41; sleep 0.8  # ESC [ then an arrow key crashed escape()
until_screen "$S · 3 agents" "ESC [ + arrow crashed the dashboard"

# a waiting agent: badge, prompt keys, double-tap guard, messages skip it
echo waiting > "$FAKE/status/$(pid_of 3.1)"
until_screen "1 NEED YOU" "waiting agent not shown"
[ "$(tmux show -t "$SID" -v @claude_need)" = 1 ] || fail "badge option @claude_need not 1"
tmux send-keys -t "$D" Tab; sleep 0.4; tmux send-keys -t "$D" 1   # Tab selects the waiting agent
on_pane 3.1 "1" "digit did not reach the waiting agent"
tmux send-keys -t "$D" 1
until_screen "pressed twice" "a repeated prompt key was not refused"
tmux send-keys -t "$D" I; sleep 0.4; tmux send-keys -t "$D" -l "to all"; tmux send-keys -t "$D" Enter
until_screen "skipped 3.1" "a message was not held back from the waiting agent"
tmux capture-pane -p -t "$SID:3.1" | grep -q "to all" && fail "message reached the waiting agent"

# a newline in a pane's path killed the tmux reader; the list must keep updating
mkdir -p "$FAKE/nl
dir"
tmux new-window -d -t "$SID:" -c "$FAKE/nl
dir" "$FAKE/bin/claude 1000"
until_screen "4 agents" "pane with a newline in its path froze the list"

tmux send-keys -t "$D" X; sleep 0.5; tmux send-keys -t "$D" n Enter   # answer "no" to kill
sleep 1; until_screen "4 agents" "kill went ahead without y"
tmux send-keys -t "$D" X; sleep 0.5; tmux send-keys -t "$D" y Enter
until_screen "3 agents" "X then y did not kill a pane"

tmux resize-window -t "$SID:dash" -x 40 -y 6; sleep 1   # tiny terminal must not crash
tmux resize-window -t "$SID:dash" -x 120 -y 30
until_screen "3 agents" "dashboard died after resize"

# SIGTERM (a closed pane or popup) must still clear the badge
echo waiting > "$FAKE/status/$(pid_of 1.1)"         # the kill above removed the waiting 3.1
until_screen "1 NEED YOU" "1.1 not shown waiting"
[ "$(tmux show -t "$SID" -v @claude_need 2>/dev/null)" = 1 ] || fail "badge not set before SIGTERM"
kill -TERM "$(tmux display -p -t "$D" '#{pane_pid}')"; sleep 1.5
[ -z "$(tmux show -t "$SID" -v @claude_need 2>/dev/null)" ] || fail "SIGTERM left the badge set"

# a dashboard watching another session exits when that session dies
SID2=$(tmux new-session -d -P -F '#{session_id}' -s dashwatch -x 100 -y 20 "sleep 60")
D=$(dash "$SID2" "$SID")
until_screen "$S · 3 agents" "dashboard given a session id did not watch it"
tmux kill-session -t "$SID"; SID=""
for _ in $(seq 20); do tmux list-panes -t "$D" >/dev/null 2>&1 || break; sleep 0.25; done
tmux list-panes -t "$D" >/dev/null 2>&1 && fail "dashboard kept running after its session died"
echo ok
