#!/bin/zsh
# ctl.sh runs London Commuter scripts from the runtime worktree, never the main checkout (JB decision 1, 2026-10-31).
# Usage: zsh mac/test_ctl_runtime.sh
f="${0:A:h}/ctl.sh"; rc=0
grep -q '^R="$HOME/Claude_London Commuter/.worktrees/RUNTIME"; ' "$f" && print "PASS ctl.sh R= is .worktrees/RUNTIME" || { print "FAIL ctl.sh R= is not .worktrees/RUNTIME"; rc=1; }
grep -nE '^[^#]*R="\$HOME/Claude_London Commuter"([^/]|$)' "$f" && { print "FAIL ctl.sh still points at the main checkout"; rc=1; } || print "PASS no main-checkout R= left"
zsh -n "$f" && print "PASS ctl.sh parses" || rc=1
exit $rc
