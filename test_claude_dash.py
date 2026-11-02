from claude_dash import assign, pane_for

# claude 30 -> bash 20 -> pane shell 10 -> tmux server 1; nested agent 40 under claude 30
tree = {40: 30, 30: 20, 20: 10, 10: 1, 99: 1}.get
assert pane_for(30, tree, {10}) == 10           # nested child maps to its pane
assert pane_for(99, tree, {10}) is None         # claude outside tmux maps to nothing
assert pane_for(10, tree, {10, 20}) == 10       # pane pid itself
assert pane_for(30, tree, {10, 20}) == 20       # nearest ancestor wins

# two agents in one pane: the pane's own (older) claude wins, whatever the JSON order
own, nested = {"pid": 30, "startedAt": 1, "name": "own"}, {"pid": 40, "startedAt": 2, "name": "eval"}
assert assign([nested, own], tree, {10})[10]["name"] == "own"
assert assign([own, nested], tree, {10})[10]["name"] == "own"
print("ok")

from claude_dash import progress
screen = "● Fixing #152 now\n  ⎿  $ pytest\n  ● main\n● Running tests with 2 fake CPUs · 31s\n\n✻ Harmonizing… (2m)\n❯ \n"
assert progress(screen) == "Running tests with 2 fake CPUs · 31s"   # last top-level step, not the indented one
assert progress("❯ \n") == ""                                         # fresh session: empty column
print("ok progress")

from claude_dash import matches
row = dict(target="5.1", status="waiting", name="annplyr-ergonomics", window="claude",
           cwd="/x/_hobby/annplyr", progress="Bash(Rscript check.R)")
assert matches("", row)                       # empty filter keeps everything
assert matches("ANN wait", row)               # all words, any case, any column
assert not matches("ann viralscan", row)      # one missing word drops the row
print("ok filter")

from claude_dash import next_needing
st = lambda *ss: [dict(status=x) for x in ss]
assert next_needing(st("waiting", "idle", "busy", "busy"), 0) == 1       # next one that needs you
assert next_needing(st("waiting", "idle", "busy", "busy"), 1) == 0       # wraps to the top
assert next_needing(st("busy", "busy"), 1) == 1                         # nobody needs you: stay
assert next_needing(st("idle"), 0) == 0                                 # only itself
print("ok next")

from claude_dash import BOX_LINES, box_body, ctx
assert ctx("x\n  ViralScan (main) | Opus 5.5 (medium) | ctx: 20%\n") == 20    # the status-line value
assert ctx("ctx: 9%\n...\nctx: 81%") == 81                                    # the newest one wins
assert ctx("no status line") is None
one = box_body(dict(buf="hi"), 40)
assert one == ["❯ hi█"]                                                        # cursor at the end
many = box_body(dict(buf="\n".join("l%d" % i for i in range(6))), 40)
assert len(many) == BOX_LINES and many[0].startswith("… ") and many[-1] == "  l5█"   # older lines scroll out
assert box_body(dict(buf="x" * 50), 10)[0].startswith("…")                   # long line keeps its end
print("ok box")

import claude_dash, os, tempfile
claude_dash.REPLIES_FILE = os.path.join(tempfile.mkdtemp(), "replies.txt")
assert claude_dash.quick_replies() == claude_dash.QUICK_REPLIES[:9]          # no file: built-in replies
open(claude_dash.REPLIES_FILE, "w").write("# mine\nship it\n\n  rebase on main  \n")
assert claude_dash.quick_replies() == ["ship it", "rebase on main"]          # comments and blanks skipped
print("ok replies")

from claude_dash import cells, fit_end
assert ctx("log says ctx: 99%\n" + "x\n" * 5 + "| ctx: 12%\n") == 12          # agent output cannot fake it
assert ctx("ctx: 97% printed by the agent\n" + "line\n" * 6) is None         # only the bottom lines count
open(claude_dash.REPLIES_FILE, "wb").write(b"ok\n\xff bad\n")
assert claude_dash.quick_replies()[0] == "ok"                                # a bad byte is not a crash
assert assign([{"pid": 30, "startedAt": None}, {"pid": 40, "startedAt": 2}], tree, {10})[10]["pid"] == 30
assert cells("ab") == 2 and cells("日本") == 4                                # wide glyphs take two cells
assert cells(fit_end("日本語" * 20, 11)) <= 11 and fit_end("日本語" * 20, 11).startswith("…")
print("ok hardening")
