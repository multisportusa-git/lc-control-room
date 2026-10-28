#!/bin/zsh
# chat_reply.sh <target> <cid>: ONE fresh, short, read-only headless Claude run answers one Control Room chat message,
# writes the answer into <notes>/w1/chat/<target>.json and publishes. Started in the background by gen/chat_sync.py.
# With INTENT: act it then starts mac/chat_act.sh (one lean action run via lane.sh, follow-up posted to the thread).
# Profile: empty working dir (no CLAUDE.md), only project/local setting sources (none there) plus a generated settings
# file with deny rules for secrets and every write tool, --strict-mcp-config with no servers, explicit --allowedTools.
set -u
ROOT="${0:A:h:h}"
NOTES="$HOME/Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"
STATE="$NOTES/w1/control-room"
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3; [[ -x $PY ]] || PY=python3
T="$1"; C="$2"
[[ "$T" =~ '^(COORD|V3|V2|A[0-9]{1,4}|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})$' && "$C" =~ '^[A-Za-z0-9]{6,32}$' ]] || { echo "bad args"; exit 2; }
W="$NOTES/w1/chat/run/$C"; mkdir -p "$W/cwd"
CL=$(ls -d "$HOME"/.local/share/claude/versions/* 2>/dev/null | sort -V | tail -1)
[[ -x "$CL" ]] || CL="$HOME/.local/bin/claude"
"$PY" "$ROOT/gen/chat_sync.py" plan "$T" "$C" "$W" || exit 1
MODEL=$(<"$W/model.txt")
echo "$(date '+%F %T') answering $T $C with $MODEL ($CL)"
cd "$W/cwd" || exit 1
# 5-minute ceiling (macOS has no timeout(1)); stream-json so the first request's context (start cost) is measured
/usr/bin/perl -e 'alarm 300; exec @ARGV' "$CL" -p "$(<"$W/prompt.txt")" --model "$MODEL" \
  --setting-sources project,local --settings "$W/settings.json" --strict-mcp-config --mcp-config "$W/mcp.json" \
  --tools "Read,Grep,Glob,Bash" --disable-slash-commands --allowedTools "$(<"$W/tools.txt")" --permission-mode default --no-session-persistence --output-format stream-json --verbose \
  > "$W/out.json" 2> "$W/err.txt"
echo "$(date '+%F %T') claude exit $?"
"$PY" "$ROOT/gen/chat_sync.py" store "$T" "$C" "$W/out.json"
# the read-only run decided "INTENT: act": store wrote act.txt (lane id) only when it started the one action run for this target
# (a second request while one runs is queued in the thread instead). Start it in the background through the LC lane script.
if [[ -s "$W/act.txt" ]]; then
  echo "$(date '+%F %T') starting action run $(<"$W/act.txt") for $T $C"
  nohup /bin/bash "$ROOT/mac/chat_act.sh" "$T" "$C" > "$W/act.log" 2>&1 < /dev/null &
  disown
fi
# publish now: push.sh holds a lock while it runs, so retry for up to 90 s until a publish lands after the answer
START=$(date +%s)
for i in {1..18}; do
  if [[ ! -d "$STATE/run.lock" ]]; then
    /bin/zsh "$ROOT/mac/push.sh"
    [[ -f "$STATE/last_push" && $(stat -f %m "$STATE/last_push") -ge $START ]] && { echo "published"; break; }
  fi
  sleep 5
done
# keep the last 30 run folders
ls -dt "$NOTES/w1/chat/run"/*/ 2>/dev/null | tail -n +31 | while read -r d; do rm -rf "$d"; done
