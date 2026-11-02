#!/usr/bin/python3.11
"""claude-dash: one tmux pane that watches and drives every Claude Code pane in its session.

Run it from inside the session you want to watch (re-selects an existing dash window):
    tmux new-window -S -n dash /exports/para-lipg-hpc/mdmanurung/lucid-dashboard/claude_dash.py
or as a popup, which closes after Enter jumps to an agent. A popup has no pane to tell it its
session, so pass the client; display-popup does not expand formats, run-shell does (tmux.conf):
    bind G run-shell 'tmux display-popup -c "#{client_name}" -E -w 90% -h 90% "/exports/para-lipg-hpc/mdmanurung/lucid-dashboard/claude_dash.py #{client_name}"'
An argument may also be a session target, e.g. claude_dash.py '$3'.
run-shell cannot host it (no tty).

Keys
    j/k, arrows   select agent          Enter   jump to its pane
    i             reply in a box (Alt-Enter or a paste adds lines) 0-9 y n answer a waiting prompt
    I             message every listed agent (filter first to pick a group)
    r / R         quick reply (QUICK_REPLIES below) to the selected / every listed agent
    Space         Enter on a waiting prompt
    e             send Esc (interrupt)  N       new claude window
    X             kill pane (asks)      q       quit
    /             filter: type words, Enter jumps to the top match, Esc clears
    Tab           select the next agent that needs you (waiting first, then idle)

While it runs, the session option @claude_need holds how many agents wait on you, for the tmux
status bar: #{?@claude_need,#{@claude_need} NEED YOU,} (tmux-modern.conf shows it as a red badge).
"""
import atexit
import collections
import curses
import json
import shutil
import os
import re
import subprocess
import sys
import threading
import time

RANK = {"waiting": 0, "?": 1, "idle": 2, "shell": 3, "busy": 3}
LABEL = {"waiting": "NEEDS YOU", "idle": "● idle", "busy": "◐ busy", "shell": "◌ shell", "?": "○ ?"}
# r / R then a number sends one of these; edit to taste (at most 9)
QUICK_REPLIES = ["continue", "yes, go ahead", "run the tests and report what fails",
                 "commit this with a clear message", "stop and summarise where you are and what is left"]
KEYS = [("Tab", "next"), ("i", "reply"), ("I", "all"), ("r", "quick"), ("/", "filter"), ("0-9 y n ␣", "answer"),
        ("e", "esc"), ("⏎", "jump"), ("N", "new"), ("X", "kill"), ("q", "quit")]
# Tokyo Night as xterm-256 numbers: name -> (fg, bg, 8-colour fg, 8-colour bg); -1 = terminal default
PALETTE = {
    "alert": (231, 88, curses.COLOR_WHITE, curses.COLOR_RED),        # waiting row: a red bar
    "alert_sel": (231, 160, curses.COLOR_WHITE, curses.COLOR_RED),
    "busy": (179, -1, curses.COLOR_YELLOW, -1),
    "idle": (114, -1, curses.COLOR_GREEN, -1),
    "unknown": (117, -1, curses.COLOR_CYAN, -1),
    "accent": (111, -1, curses.COLOR_BLUE, -1),
    "dim": (244, -1, -1, -1),
    "pill": (234, 111, curses.COLOR_BLACK, curses.COLOR_BLUE),
    "pill_red": (231, 160, curses.COLOR_WHITE, curses.COLOR_RED),
    "pill_idle": (234, 114, curses.COLOR_BLACK, curses.COLOR_GREEN),
    "pill_busy": (252, 238, curses.COLOR_BLACK, curses.COLOR_WHITE),
    "pill_warn": (234, 179, curses.COLOR_BLACK, curses.COLOR_YELLOW),
    "input": (231, 237, curses.COLOR_BLACK, curses.COLOR_WHITE),     # message box field
    "hot": (210, -1, curses.COLOR_RED, -1),                          # context nearly full
}
EDIT_KEYS = [("⏎", "send"), ("Alt-⏎", "new line"), ("Esc", "cancel"), ("Ctrl-U", "clear")]
QUICK_KEYS = [("1-%d" % len(QUICK_REPLIES), "send"), ("Esc", "cancel")]
BOX_LINES = 4  # message lines shown in the box; older lines scroll out of view
COL = {}  # name -> curses attribute, filled once curses has started
SEEN = {}  # pane id -> (time read, progress line, context %)


def sh(*args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, errors="replace", timeout=20).stdout


def ppid(pid):
    # comm may contain spaces or ")", so read the field after the last ")"
    try:
        with open("/proc/%d/stat" % pid) as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def pane_for(pid, ppid_of, pane_pids):
    """Walk up the process tree from pid; return the first ancestor that is a pane's pid."""
    while pid > 1:
        if pid in pane_pids:
            return pid
        pid = ppid_of(pid)
    return None


def assign(agents, ppid_of, pane_pids):
    """Map pane pid -> agent. Oldest agent wins, because a nested agent starts after its parent."""
    out = {}
    for a in sorted(agents, key=lambda a: a.get("startedAt", 0)):
        pp = pane_for(a["pid"], ppid_of, pane_pids)
        if pp and pp not in out:
            out[pp] = a
    return out


def since(pid):
    """Epoch ms of the agent's last status change, from ~/.claude/sessions/<pid>.json; 0 if unreadable."""
    try:
        with open(os.path.expanduser("~/.claude/sessions/%d.json" % pid)) as f:
            return json.load(f).get("statusUpdatedAt", 0)
    except (OSError, ValueError):
        return 0


def age(ms):
    if not ms:
        return ""
    s = max(0, int(time.time() - ms / 1000))
    return "%ds" % s if s < 60 else "%dm" % (s // 60) if s < 3600 else "%dh" % (s // 3600)


class Agents(threading.Thread):
    """Polls `claude agents --json` every 5 s (each call costs ~0.4 s CPU); keeps last good result."""
    daemon = True
    rows = []
    err = "starting"

    def run(self):
        while True:
            try:
                rows = json.loads(sh("claude", "agents", "--json"))
                rows = [a for a in rows if a.get("kind") == "interactive" and "pid" in a]
                for a in rows:
                    a["since"] = since(a["pid"])
                self.rows = rows
                self.err = ""
            except Exception as e:  # claude missing, timeout, bad JSON: show it, keep polling
                self.err = type(e).__name__
            time.sleep(5)


def progress(screen):
    """Last top-level "● " line on a Claude Code screen: the agent's latest step or message."""
    steps = [l[2:].strip() for l in screen.splitlines() if l.startswith("● ")]
    return steps[-1] if steps else ""


def ctx(screen):
    """Context use from a status line like "| ctx: 20%" (the last one on screen); None if absent."""
    found = re.findall(r"ctx:\s*(\d+)%", screen)
    return int(found[-1]) if found else None


class Poller(threading.Thread):
    """All tmux reads happen here, so a keypress never waits on a subprocess.
    The UI sets .want to the selected pane and calls .wake.set() for an immediate refresh."""
    daemon = True
    rows = []            # sorted agent rows, replaced whole each poll
    preview = ("", [])   # (pane id, screen lines) for .want

    def __init__(self, session, me, agents):
        super().__init__()
        self.session, self.me, self.agents = session, me, agents
        self.want, self.wake, self.need = None, threading.Event(), None

    def run(self):
        while True:
            self.wake.clear()
            want = self.want  # capture first: a selection change mid-poll wakes us again
            if want:
                lines = sh("tmux", "capture-pane", "-p", "-t", want).rstrip("\n").splitlines()
                self.preview = (want, lines)
            self.rows = panes(self.session, self.me, self.agents.rows)
            need = sum(r["status"] == "waiting" for r in self.rows)
            if need != self.need:  # status-bar badge; unset at 0 so #{?@claude_need,...} is false
                # ponytail: one dashboard per session; a second one's exit clears the badge until next change
                sh("tmux", "set", "-t", self.session, *(["@claude_need", str(need)] if need else ["-u", "@claude_need"]))
                self.need = need
            self.wake.wait(0.5)


def panes(session, me, agents):
    # window names cannot hold tabs; the path goes last so a tab in it stays in the path
    fmt = "\t".join(["#{pane_id}", "#{pane_pid}", "#{window_index}.#{pane_index}",
                     "#{pane_current_command}", "#{window_name}", "#{session_name}",
                     "#{pane_current_path}"])
    rows = {}
    for line in sh("tmux", "list-panes", "-s", "-t", session, "-F", fmt).splitlines():
        pid_, ppid_, target, cmd, wname, sname, cwd = line.split("\t", 6)
        if pid_ != me:
            rows[int(ppid_)] = dict(id=pid_, target=target, window=wname, cwd=cwd, cmd=cmd, session=sname,
                                    status="?", name="", waiting="", since=0)
    for pp, a in assign(agents, ppid, rows).items():
        rows[pp].update(status=a.get("status", "?"), name=a.get("name", ""),
                        waiting=a.get("waitingFor", ""), since=a.get("since", 0), agent=True)
    out = [r for r in rows.values() if r.get("agent") or r["cmd"] == "claude"]
    # re-read a screen when its progress is over 3 s old, oldest first and at most 2 per tick, so
    # no tick stalls on all of them (the first tick reads all); -J keeps a wrapped line whole
    read_at = lambda r: SEEN.get(r["id"], (0,))[0]
    stale = sorted((r for r in out if time.time() - read_at(r) > 3), key=read_at)
    for r in stale if not SEEN else stale[:2]:
        screen = sh("tmux", "capture-pane", "-p", "-J", "-t", r["id"])
        SEEN[r["id"]] = (time.time(), progress(screen), ctx(screen))
    for r in out:
        _, r["progress"], r["ctx"] = SEEN.get(r["id"], (0, "", None))
    return sorted(out, key=lambda r: (RANK.get(r["status"], 1),
                                      tuple(int(n) for n in r["target"].split("."))))


def next_needing(rows, sel):
    """Index of the next waiting or idle row after sel, wrapping; sel if none needs you."""
    order = list(range(sel + 1, len(rows))) + list(range(sel + 1))
    return next((i for i in order if rows[i]["status"] in ("waiting", "idle")), sel)


def matches(query, r):
    """Every word of the filter appears somewhere in the row (any case)."""
    hay = " ".join([r["target"], r["status"], r["name"], r["window"], r["cwd"], r["progress"]]).lower()
    return all(word in hay for word in query.lower().split())


def send_text(pane, text):
    if "\n" in text:  # paste as one block: a typed newline would submit the first line alone
        subprocess.run(["tmux", "load-buffer", "-b", "claude-dash", "-"], input=text, text=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        sh("tmux", "paste-buffer", "-p", "-d", "-b", "claude-dash", "-t", pane)
    else:
        if text.endswith(";"):  # tmux reads a trailing ";" as a command separator
            text = text[:-1] + "\\;"
        sh("tmux", "send-keys", "-t", pane, "-l", "--", text)
    time.sleep(0.1)  # one call with Enter reads as a paste and Claude Code does not submit
    sh("tmux", "send-keys", "-t", pane, "Enter")


def escape(scr):
    """After Esc, read what came with it: "alt-enter", "paste-start", "paste-end", or "" for a lone Esc."""
    scr.nodelay(True)
    try:
        nxt = scr.get_wch()
        if nxt in ("\n", "\r"):
            return "alt-enter"
        if nxt == "[":  # bracketed paste wraps pasted text in ESC[200~ ... ESC[201~
            seq = ""
            while len(seq) < 8 and seq[-1:] != "~":
                seq += scr.get_wch()
            return {"200~": "paste-start", "201~": "paste-end"}.get(seq, "")
        curses.unget_wch(nxt) if isinstance(nxt, str) else curses.ungetch(nxt)  # Esc, then a key
        return ""
    except curses.error:  # nothing followed: a lone Esc
        return ""
    finally:
        scr.timeout(150)


def box_body(edit, room):
    """Lines inside the message box: the quick-reply menu, or the last BOX_LINES of the message."""
    if edit.get("quick"):
        return ["%d  %s" % (i, q) for i, q in enumerate(QUICK_REPLIES, 1)]
    lines = edit["buf"].split("\n")
    shown = lines[-BOX_LINES:]
    out = [("❯ " if j == 0 and len(lines) <= BOX_LINES else "… " if j == 0 else "  ") + l
           for j, l in enumerate(shown)]
    out[-1] += "█"
    return [l if len(l) <= room else "…" + l[-(room - 1):] for l in out]  # keep the typing end in view


def ask(scr, prompt):
    curses.endwin()
    try:
        return input("\n" + prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    finally:
        scr.refresh()


def put(scr, y, x, s, attr=0):
    """Write s clipped to the screen; return the column after it, so segments can chain."""
    h, w = scr.getmaxyx()
    if not (0 <= y < h and x < w - 1):
        return x
    s = s[: w - 1 - x]
    try:  # wide glyphs can still overflow the last cell
        scr.addstr(y, x, s, attr)
    except curses.error:
        pass
    return x + len(s)


def colours():
    deep = curses.COLORS >= 256
    for n, (key, (fg, bg, fg8, bg8)) in enumerate(PALETTE.items(), 1):
        curses.init_pair(n, fg if deep else fg8, bg if deep else bg8)
        COL[key] = curses.color_pair(n)
    if not deep:
        COL["dim"] = curses.A_DIM


def hints(scr, y, keys):
    x = 1
    for key, what in keys:  # key in blue, what it does in grey
        x = put(scr, y, put(scr, y, x, key, COL["accent"] | curses.A_BOLD), " %s   " % what, COL["dim"])


def draw(scr, name, rows, sel, note, err, counts, edit, preview):
    scr.erase()
    h, w = scr.getmaxyx()
    c, B = COL, curses.A_BOLD
    total = sum(counts.values())

    # header: title pill, session, then one pill per state (over all agents, filter or not)
    x = put(scr, 0, 0, " ◆ claude-dash ", c["pill"] | B)
    shown_n = total if len(rows) == total else "%d/%d" % (len(rows), total)
    x = put(scr, 0, x, "  %s · %s agents  " % (name, shown_n), c["accent"] | B)
    for n, label, key in [(counts["waiting"], "NEED YOU", "pill_red"), (counts["idle"], "idle", "pill_idle"),
                          (counts["busy"] + counts["shell"], "busy", "pill_busy")]:
        if n:
            x = put(scr, 0, x, " %d %s " % (n, label), c[key] | B) + 1
    if err:
        put(scr, 0, max(x, w - 28), " agents unavailable: %s " % err[:10], c["pill_warn"])

    nw = min(max([len(r["name"] or r["window"]) for r in rows] + [5]), 26, max(8, w // 4))
    put(scr, 2, 0, "  %-6s%-12s%-5s%-5s%-*s  %s" % ("WIN", "STATE", "FOR", "CTX", nw, "AGENT", "PROGRESS"),
        c["dim"])
    shown = max(1, min(len(rows), (h - 6) // 2))  # list takes at most half; the preview the rest
    off = min(max(0, sel - shown + 1), max(0, len(rows) - shown))
    for i, r in enumerate(rows[off:off + shown], off):
        y, st, here = 3 + i - off, r["status"], i == sel
        label = LABEL.get(st, st)
        agent = (r["name"] or r["window"])[:nw]
        used = "" if r.get("ctx") is None else "%d%%" % r["ctx"]
        if st == "waiting":  # one solid red bar, full width
            text = "%s %-6s%-12s%-5s%-5s%-*s  %s · %s" % ("▌" if here else " ", r["target"], label, age(r["since"]),
                                                        used, nw, agent, r["waiting"], r["progress"])
            put(scr, y, 0, text.ljust(w), c["alert_sel" if here else "alert"] | B)
            continue
        x = put(scr, y, 0, "▌ " if here else "  ", c["accent"] | B)
        x = put(scr, y, x, "%-6s" % r["target"], c["dim"])
        x = put(scr, y, x, "%-12s" % label, c.get(st, c["unknown"]) | (B if st == "idle" else 0))
        x = put(scr, y, x, "%-5s" % age(r["since"]), c["dim"])
        hot = c["hot"] | B if (r.get("ctx") or 0) >= 80 else c["busy"] if (r.get("ctx") or 0) >= 60 else c["dim"]
        x = put(scr, y, x, "%-5s" % used, hot)
        x = put(scr, y, x, "%-*s  " % (nw, agent), B if here else 0)
        put(scr, y, x, r["progress"], 0 if here else c["dim"])

    # preview of the selected agent's screen
    top = shown + 4
    body = box_body(edit, w - 6) if edit else []
    foot_rows = len(body) + 3 if edit else 1  # the box: border, its lines, border, then a hint row
    if rows and h - top - foot_rows > 2:
        r = rows[sel]
        put(scr, top, 0, "─" * w, c["dim"])
        put(scr, top, 2, " %s · %s · %s " % (r["target"], r["name"] or r["window"],
                                            r["cwd"].replace(os.path.expanduser("~"), "~", 1)), c["accent"])
        for j, line in enumerate(preview[-(h - top - 1 - foot_rows):]):
            put(scr, top + 1 + j, 1, line)

    # footer: a boxed message field with its keys on their own row, a notice, or the key hints
    if edit:
        y0, title = h - len(body) - 3, " %s " % edit["title"]
        put(scr, y0, 0, "╭─" + title + "─" * max(0, w - 4 - len(title)) + "╮", c["accent"])
        for j, line in enumerate(body, y0 + 1):
            put(scr, j, 0, "│", c["accent"])
            put(scr, j, 1, (" " + line).ljust(w - 3), c["input"] | B)
            put(scr, j, w - 2, "│", c["accent"])
        put(scr, h - 2, 0, "╰" + "─" * (w - 3) + "╯", c["accent"])
        hints(scr, h - 1, QUICK_KEYS if edit.get("quick") else EDIT_KEYS)
    elif note:
        put(scr, h - 1, put(scr, h - 1, 0, " ● ", c["busy"] | B), note, c["busy"])
    else:
        hints(scr, h - 1, KEYS)
    scr.refresh()


def main(scr):
    curses.use_default_colors()
    colours()
    curses.curs_set(0)
    scr.timeout(150)  # redraw from the poller's snapshot; drawing makes no subprocess calls
    # a popup's tty belongs to no pane; there TMUX_PANE is unset or stale, so ignore it
    popup = os.ttyname(0) not in sh("tmux", "list-panes", "-a", "-F", "#{pane_tty}").split()
    me = "" if popup else os.environ.get("TMUX_PANE", "")
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if popup and not arg:  # without a pane, tmux would guess the most recent client
        sys.exit("claude-dash: in a popup pass the client: %s '#{client_name}' (see --help)" % sys.argv[0])
    # target by id: "-t NAME" is parsed as a window target and breaks on names with "." or ":"
    where = (["-c", arg] if arg.startswith("/dev/") else ["-t", arg]) if arg else ["-t", me] if me else []
    session = sh("tmux", "display", "-p", *where, "#{session_id}").strip()
    if not session:  # e.g. /usr/bin/tmux 2.7 first on PATH cannot talk to a 3.7b server
        sys.exit("claude-dash: 'tmux display' failed; tmux on PATH is %s" % shutil.which("tmux"))
    atexit.register(sh, "tmux", "set", "-t", session, "-u", "@claude_need")  # no stale badge after exit
    sys.stdout.write("\033[?2004h")  # bracketed paste: a pasted newline must not send the message
    sys.stdout.flush()
    atexit.register(lambda: (sys.stdout.write("\033[?2004l"), sys.stdout.flush()))
    agents = Agents()
    agents.start()
    poll = Poller(session, me, agents)
    poll.start()
    sel, sel_id, note, was_waiting = 0, None, "", None
    query, edit = "", None  # edit: footer line editor; edit["to"] is None for the filter

    def deliver(text, to):
        """Send text to panes `to`; skip any now waiting, as Enter would accept its dialog."""
        now = poll.rows
        skipped = [r["target"] for r in now if r["id"] in to and r["status"] == "waiting"]
        live = [r["id"] for r in now if r["id"] in to and r["status"] != "waiting"]
        for pane in live:
            send_text(pane, text)
        return "sent to %d agent%s" % (len(live), "" if len(live) == 1 else "s") + (
            "; skipped waiting %s" % " ".join(skipped) if skipped else "")

    while True:
        if edit and edit["to"] is None:
            query = edit["buf"]
        everyone = poll.rows
        rows = [r for r in everyone if matches(query, r)]
        ids = [r["id"] for r in rows]
        # follow the agent, not the row: rows re-sort whenever a status changes
        sel = ids.index(sel_id) if sel_id in ids else max(0, min(sel, len(rows) - 1))
        sel_id = rows[sel]["id"] if rows else None
        if sel_id != poll.want:  # new selection: fetch its screen now, not at the next poll
            poll.want = sel_id
            poll.wake.set()
        waiting_now = {r["id"] for r in everyone if r["status"] == "waiting"}
        if was_waiting is not None and waiting_now - was_waiting:
            curses.beep()
        was_waiting = waiting_now
        name = everyone[0]["session"] if everyone else ""
        foot = note or ("filter /%s   Esc clear" % query if query else "")
        counts = collections.Counter(r["status"] for r in everyone)
        draw(scr, name, rows, sel, foot, agents.err if agents.err != "starting" else "",
             counts, edit, poll.preview[1])
        try:
            k = scr.get_wch()  # str for typed characters (any script), int for special keys
        except curses.error:
            continue
        note = ""
        ch = k if isinstance(k, str) else ""
        code = ord(k) if ch else k
        kind = escape(scr) if code == 27 else ""
        if kind == "paste-start":  # a paste outside the box opens a reply to the selected agent
            if not edit and rows:
                edit = dict(title="reply to %s · %s" % (rows[sel]["target"], rows[sel]["name"] or rows[sel]["window"]),
                            buf="", to=[rows[sel]["id"]])
            if edit:
                edit["pasting"] = True
            continue
        if kind == "paste-end":
            if edit:
                edit["pasting"] = False
            continue
        if kind == "alt-enter":
            if edit and edit["to"] is not None and not edit.get("quick"):
                edit["buf"] += "\n"
            continue
        if edit and edit.get("quick"):
            if code == 27:
                edit = None
            elif ch in "123456789" and ch and int(ch) <= len(QUICK_REPLIES):
                note, edit = deliver(QUICK_REPLIES[int(ch) - 1], edit["to"]), None
            continue
        if edit and code not in (curses.KEY_UP, curses.KEY_DOWN):
            enter = code in (10, 13, curses.KEY_ENTER)
            filtering = edit["to"] is None
            if enter and edit.get("pasting"):  # newline inside a paste: keep it, do not send
                edit["buf"] += " " if filtering else "\n"
                continue
            if code == 27:  # Esc cancels; on the filter it also clears it
                query = "" if filtering else query
                edit = None
            elif code in (curses.KEY_BACKSPACE, 127, 8):
                edit["buf"] = edit["buf"][:-1]
            elif code == 21:  # Ctrl-U
                edit["buf"] = ""
            elif enter and not filtering:
                text = edit["buf"].strip()
                note = deliver(text, edit["to"]) if text else "not sent: empty message"
                edit = None
            elif enter:
                edit = None  # filter stays on; Enter falls through and jumps to the top match
            elif ch == "\t":
                edit["buf"] += "    "
            elif ch.isprintable():
                edit["buf"] += ch
            if not (enter and filtering):
                continue
        if ch == "/":
            edit = dict(title="filter: words to match", buf=query, to=None)
            continue
        if code == 27:  # Esc clears the filter; "e" sends Esc to an agent
            query = ""
            continue
        if ch == "q":
            return
        if (code == curses.KEY_DOWN or ch == "j") and sel < len(rows) - 1:
            sel_id = rows[sel + 1]["id"]
        elif (code == curses.KEY_UP or ch == "k") and sel > 0:
            sel_id = rows[sel - 1]["id"]
        elif code == 9 and rows:  # Tab
            sel_id = rows[next_needing(rows, sel)]["id"]
        elif ch == "N":
            d = ask(scr, "new claude in dir [%s]: " % (rows[sel]["cwd"] if rows else "~"))
            d = os.path.expanduser(d or (rows[sel]["cwd"] if rows else "~"))
            if not os.path.isdir(d):
                note = "no such dir: %s" % d
                continue
            sh("tmux", "new-window", "-d", "-t", session + ":", "-c", d, "claude")
        if ch == "I" and rows:  # every listed agent; waiting ones are skipped at send time
            edit = dict(title="message all %d listed agents" % len(rows), buf="", to=[r["id"] for r in rows])
            continue
        if ch == "R" and rows:
            edit = dict(title="quick reply to all %d listed agents" % len(rows), buf="",
                        to=[r["id"] for r in rows], quick=True)
            continue
        if not rows:
            continue
        r = rows[sel]
        waiting = r["status"] == "waiting"
        if code in (10, 13, curses.KEY_ENTER):
            sh("tmux", "select-window", "-t", r["id"])
            sh("tmux", "select-pane", "-t", r["id"])
            if popup:  # leave, so the popup closes and the jump is visible
                return
        elif ch in ("i", "r") and waiting:
            note = "%s is waiting on a dialog: answer with 0-9/y/n/Space first" % r["target"]
        elif ch == "r":
            edit = dict(title="quick reply to %s · %s" % (r["target"], r["name"] or r["window"]), buf="",
                        to=[r["id"]], quick=True)
        elif ch == "i":  # typed in the footer, so its screen stays visible in the preview
            edit = dict(title="reply to %s · %s" % (r["target"], r["name"] or r["window"]), buf="", to=[r["id"]])
        elif ch and ch in "0123456789yn " and not waiting:
            note = "%s is %s, not waiting: key not sent" % (r["target"], r["status"])
        elif ch and ch in "0123456789yn":
            sh("tmux", "send-keys", "-t", r["id"], ch)
        elif ch == " ":
            sh("tmux", "send-keys", "-t", r["id"], "Enter")
        elif ch == "e":
            sh("tmux", "send-keys", "-t", r["id"], "Escape")
        elif ch == "X" and ask(scr, "kill %s %s? [y/N] " % (r["target"], r["name"])) == "y":
            sh("tmux", "kill-pane", "-t", r["id"])


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):
        sys.exit(__doc__)
    if not os.environ.get("TMUX"):
        sys.exit("claude-dash: run inside tmux, e.g. tmux new-window -S -n dash %s" % sys.argv[0])
    os.environ.setdefault("ESCDELAY", "25")  # Esc clears the filter without a 1 s lag
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
