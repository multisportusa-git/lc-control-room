#!/bin/zsh
# Runs on JB's Mac every minute (launchd). Each run records any answers JB gave on the page (and starts the action a
# decision names). The full build and publish runs when something changed or every 10 minutes. No model tokens.
set -u
ROOT="${0:A:h:h}"
NOTES="$HOME/Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"
STATE="$NOTES/w1/control-room"
KEY="$HOME/.config/lc-control-room/pass"
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3; [[ -x $PY ]] || PY=python3
GH=/opt/homebrew/bin/gh; [[ -x $GH ]] || GH=gh
mkdir -p "$STATE/pub"
LOG="$STATE/push.log"
[[ -f $LOG ]] && [[ $(wc -l < "$LOG") -gt 600 ]] && tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exec >>"$LOG" 2>&1
LOCK="$STATE/run.lock"; mkdir "$LOCK" 2>/dev/null || { [[ $(( $(date +%s) - $(stat -f %m "$LOCK") )) -gt 600 ]] && rm -rf "$LOCK" && mkdir "$LOCK" || exit 0; }
trap 'rm -rf "$LOCK"' EXIT
[[ -s $KEY ]] || { echo "no passphrase file at $KEY: run mac/install.sh"; exit 1; }
STAMP="$STATE/last_push"; DLOG="$NOTES/w1/decisions/decisions.log"
fp() { stat -f %m "$NOTES/w1/state.json" "$DLOG" "$NOTES/w1/decisions" "$NOTES/w1/evidence" "$NOTES/testq/results" "$NOTES/w1/chat" 2>/dev/null | tr '\n' ' '; }
T="$HOME/.config/lc-control-room/ntfy_topic"
[[ -s $T ]] || { echo "lc-$(/usr/bin/openssl rand -hex 12)" > "$T"; chmod 600 "$T"; }
"$PY" "$ROOT/gen/decisions_sync.py" || echo "decisions sync failed"
[[ -f "$ROOT/gen/chat_sync.py" ]] && { "$PY" "$ROOT/gen/chat_sync.py" || echo "chat sync failed"; }
[[ ! -f $HOME/.config/lc-wake.off ]] && { "$PY" "$ROOT/gen/wake.py" || echo "wake failed"; }
# cut-over: any new closing-session brief in <notes>/cutover/ gets its lean headless successor (max 3 at once; backgrounded)
( zsh "$ROOT/mac/cutover.sh" --go >> "$STATE/cutover.log" 2>&1 & )
age=$(( $(date +%s) - $( [[ -f $STAMP ]] && stat -f %m "$STAMP" || echo 0 ) ))
[[ "$(fp)" == "$(cat "$STATE/last_fp" 2>/dev/null)" && $age -lt 570 ]] && exit 0
echo "$(date '+%F %T') build (last publish ${age}s ago)"
git -C "$ROOT" pull -q --ff-only origin main || echo "code pull failed; kept the current code"
# session fuel: the guard's per-session state, adaptive compaction points and the newest events (gen embeds a fuel block)
FU="$STATE/fuel"; rm -rf "$FU.tmp"; mkdir -p "$FU.tmp/state"
find "$HOME/.config/lc-fuel/state" -name '*.json' -mmin -1440 -exec cp {} "$FU.tmp/state/" \; 2>/dev/null; cp "$HOME/.config/lc-fuel/compact_points.json" "$FU.tmp/" 2>/dev/null
tail -n 200 "$NOTES/w1/fuel/events.jsonl" > "$FU.tmp/events.jsonl" 2>/dev/null; rm -rf "$FU"; mv "$FU.tmp" "$FU"
nice -n 15 "$PY" "$ROOT/gen/w1_dashboard.py" --json "$STATE/w1.json" --out "$NOTES/w1/W1_DASHBOARD.html" || { echo "build failed"; exit 1; }
gzip -9nc "$STATE/w1.json" | /usr/bin/openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass file:"$KEY" -base64 -A > "$STATE/w1.enc.tmp" \
  && [[ -s "$STATE/w1.enc.tmp" ]] && mv "$STATE/w1.enc.tmp" "$STATE/pub/w1.enc" || { echo "encrypt failed"; exit 1; }
cd "$STATE/pub"
if [[ ! -d .git ]]; then
  git init -q -b data && git remote add origin https://github.com/multisportusa-git/lc-control-room.git
  git config user.name "lc-control-room (JB's Mac)"; git config user.email "multisportusa-git@users.noreply.github.com"
fi
git add -A
# one commit only: amend, then force-push, so the data branch never grows
if git rev-parse -q --verify HEAD >/dev/null; then git commit -q --amend -m "data $(date -u +%FT%TZ)"; else git commit -q -m "data $(date -u +%FT%TZ)"; fi
git -c credential.helper= -c credential.helper="!$GH auth git-credential" push -q -f origin data:data && { echo "pushed"; touch "$STAMP"; fp > "$STATE/last_fp"; } || echo "push failed (offline?)"
git reflog expire --expire=now --all && git gc -q --prune=now
