# claude-dash

A tmux dashboard for Claude Code. You run it in one pane, and it lists every Claude Code agent in the same tmux session. For each one it shows the live status, how long it has been in that state, its context use and its latest step. From the dashboard you can reply, answer prompts, jump to an agent, spawn one or kill one, so you never have to click through panes one at a time.

```
 ◆ claude-dash   0 · 10 agents   1 NEED YOU   1 idle   8 busy
  WIN   STATE       FOR  CTX  AGENT                       PROGRESS
  5.1   NEEDS YOU   1m   24%  annplyr-ergonomics          permission prompt · Bash(Rscript check.R)
  9.1   ● idle      24s  26%  mycelium-extra-59           All three changes are on main and pass…
▌ 1.1   ◐ busy      5m   25%  fix-adversarial-review-fin  Land #154, #155, #153, #151 in order
── 1.1 · fix-adversarial-review-findings · ~/ViralScan ─────────────────────
 (live preview of the selected agent's screen)
 Tab next   i reply   I all   r quick   / filter   0-9 y n ␣ answer   ⏎ jump   N new   X kill   q quit
```

An agent waiting on you gets a full-width red row, a red `N NEED YOU` badge appears in the header, and the terminal bell rings.

## Requirements

- tmux 3.2 or later (tested on 3.7b).
- Claude Code with `claude agents --json`, which needs 2.1.139 or later.
- Python 3.7 or later. The shebang is `/usr/bin/python3.11`; change it to match your system.
- Linux, because the dashboard maps agents to panes through `/proc`.

It has no Python dependencies and needs no Claude Code hooks.

## Run

From inside the tmux session you want to watch:

```sh
tmux new-window -S -n dash /path/to/claude_dash.py
```

`-S` jumps to an existing `dash` window instead of opening a second dashboard. It also works as a split pane:

```sh
tmux split-window -h -l 45% /path/to/claude_dash.py
```

To open it as a popup, add this to `~/.tmux.conf`. The popup closes after `Enter` jumps to an agent.

```tmux
bind G run-shell 'tmux display-popup -c "#{client_name}" -E -w 90% -h 90% "/path/to/claude_dash.py #{client_name}"'
```

A popup has no pane of its own, so the binding passes the client name. `display-popup` does not expand `#{...}` formats but `run-shell` does, which is why the binding goes through `run-shell`.

## Keys

| Key | Action |
|---|---|
| `j` `k` / arrows | select an agent |
| `Tab` | select the next agent that needs you, waiting agents first, then idle ones |
| `Enter` | jump to the agent's pane |
| `i` | reply in the message box; `Alt-Enter` or a paste adds lines, `Enter` sends |
| `I` | message every listed agent; filter first to pick a group |
| `r` / `R` | prefilled reply to the selected / every listed agent: pick 1-9, edit it, `Enter` sends |
| `0`-`9` `y` `n` `Space` | answer a waiting prompt (`Space` sends Enter) |
| `e` | send Esc to interrupt the agent |
| `/` | filter by any words; `Enter` jumps to the top match, `Esc` clears |
| `N` | open a new Claude window in a directory you choose |
| `X` | kill the pane, after a `y/N` confirmation |
| `q` | quit |

The dashboard has safety rules for keys sent to agents:

- Prompt keys only go to agents that are **waiting**, because a stray `1` would otherwise land in a busy agent's prompt.
- Messages skip waiting agents, because `Enter` would accept the open dialog.
- The selection follows the agent, not the row, so a re-sort can't send your key to a different agent.

## Prefilled replies

Put your own replies in `~/.config/claude-dash/replies.txt`, one per line and at most 9. Lines starting with `#` are skipped.

```
# claude-dash replies
continue
run the tests and report what fails, with the exact error
stop and summarise where you are, what is done and what is left
```

The file is re-read every time you open the menu. Without it, the built-in `QUICK_REPLIES` in `claude_dash.py` are used.

## Status-bar badge and theme

While the dashboard runs, the session option `@claude_need` holds the number of waiting agents. Show it in any status line with:

```tmux
#{?@claude_need,#{@claude_need} NEED YOU,}
```

`tmux-modern.conf` is an optional Tokyo Night theme that shows this as a red badge. It also adds pane-border labels with each agent's task name and rounded popups. To try it live, or to keep it, add this line to `~/.tmux.conf` above any TPM lines:

```sh
tmux source-file /path/to/tmux-modern.conf
```

## Where the data comes from

- **Status and name:** `claude agents --json`, polled every 5 s.
- **Time in state:** `statusUpdatedAt` in `~/.claude/sessions/<pid>.json`.
- **Agent to pane:** the parent-process chain in `/proc`, up to a tmux pane's pid.
- **Progress and context:** the agent's last `●` line and the `ctx: N%` in its status line, read with `tmux capture-pane`.

A background thread does every tmux read, so keypresses never wait on a subprocess. One frame draws in about 0.3 ms.

## Tests

```sh
python3.11 test_claude_dash.py   # pure functions: pane mapping, parsing, filter, message box, replies
bash stress_claude_dash.sh       # end to end in a throwaway tmux session with fake agents
```

The stress test never touches your other sessions.

## Limits

- `run-shell` cannot host the dashboard, because it has no tty.
- Without bracketed paste in your terminal, a pasted newline sends the message early.
- With two dashboards on one session, closing one clears the badge until the next status change.
- The `CTX` column needs a status line that prints `ctx: N%`.
