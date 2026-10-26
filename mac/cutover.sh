#!/bin/zsh
# Cut-over (JB 2026-10-26 14:22): interactive Code sessions close; their work continues as lean headless runs.
# Each closing session writes <notes>/cutover/<LETTER>.md (brief: worktree, branch, state, exact next step, model).
# This script starts ONE lean headless run per brief (max 3 at a time), in the worktree the brief names, and records it.
#   mac/cutover.sh            list briefs and what would start
#   mac/cutover.sh --go       start them
set -u
N="$HOME/Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"
C="$N/cutover"; EV="$N/w1/evidence"; R="$HOME/Claude_London Commuter"
CL=$(ls -t "$HOME"/.local/share/claude/versions/* | head -1)
mkdir -p "$C"
if [[ ${1:-} == --go ]]; then
  [[ -d "$C/.lock" ]] && (( $(date +%s) - $(stat -f %m "$C/.lock") > 900 )) && rmdir "$C/.lock"
  mkdir "$C/.lock" 2>/dev/null || exit 0
  trap 'rmdir "$C/.lock" 2>/dev/null' EXIT INT TERM
fi
running() { pgrep -f "lc-cutover-lane" | wc -l | tr -d ' '; }
for b in "$C"/*.md(N); do
  L=${b:t:r}; [[ $L == COORDINATOR ]] && continue
  [[ -f "$C/$L.started" ]] && { echo "$L: already started"; continue; }
  W=$(grep -oE '/Users/[^ `]*\.worktrees/[^ `/]+' "$b" | head -1); [[ -d ${W:-/nonexistent} ]] || W="$R"
  M=$(grep -oiE 'claude-(haiku|sonnet|opus)-5-5' "$b" | head -1); M=${M:-claude-sonnet-5-5}
  echo "$L: worktree $W model $M"
  [[ ${1:-} == --go ]] || continue
  while (( $(running) >= 3 )); do sleep 20; done
  ( cd "$W" && LC_CUTOVER=lc-cutover-lane nohup "$CL" -p "You are lane $L, continuing headless after the cut-over (lc-cutover-lane). Read CLAUDE.md and your brief $b only, then do the exact next step(s) it names. Lean lane thresholds: 175k start hand-over, 200k complete, 225k stop (the fuel guard tells you). Heavy tests only via scripts/test-submit.sh; never wait in a turn for a job: when you need a result, write your status to $N/status/$L-status.md and end. Never merge, never push to main, never deploy. Decisions for JB go in a decision card file (w1/decisions/D-<n>.json, schema in the Control Room DECISIONS.md, programme V2). Final output one line: DONE $L or HANDOFF $L <brief path>." \
      --model "$M" --settings "$EV/lane-settings-v2.json" --strict-mcp-config --mcp-config "$R/docs/handover/v3/sp1/lane-mcp.json" \
      --output-format json > "$C/$L.json" 2> "$C/$L.log" < /dev/null & )
  date '+%F %T' > "$C/$L.started"
done
