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
