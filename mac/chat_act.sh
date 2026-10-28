#!/bin/bash
# chat_act.sh <target> <cid>: the ONE action run behind a Control Room chat message that asked for something to be done.
# Started in the background by chat_reply.sh (or by gen/chat_sync.py act-run's queue hand-over). It runs the LC repo's
# scripts/lane.sh as the coord role (its allowed tools and deny rules are unchanged; never --dangerously-skip-permissions),
# waits for it, posts the DONE/BLOCKED follow-up from "Claude (acting)" into the thread, starts a queued one, and publishes.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOTES="$HOME/Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"
STATE="$NOTES/w1/control-room"
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3; [ -x "$PY" ] || PY=python3
T="$1"; C="$2"
echo "$T" | grep -qE '^(COORD|V3|V2|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})$' && echo "$C" | grep -qE '^[A-Za-z0-9]{6,32}$' || { echo "bad args"; exit 2; }
echo "$(date '+%F %T') action run for $T $C"
"$PY" "$ROOT/gen/chat_sync.py" act-run "$T" "$C"
echo "$(date '+%F %T') action run ended"
[ -n "${LC_CHAT_NOPUSH:-}" ] && exit 0
START=$(date +%s)
for i in $(seq 1 18); do
  if [ ! -d "$STATE/run.lock" ]; then
    /bin/zsh "$ROOT/mac/push.sh"
    if [ -f "$STATE/last_push" ] && [ "$(stat -f %m "$STATE/last_push")" -ge "$START" ]; then echo "published"; break; fi
  fi
  sleep 5
done
