#!/bin/zsh
# ctl.sh <command> [args]: the Controller's toolkit (JB 28 Oct 11:12 "I expect action"). Every command is a fixed,
# reviewed operation, so the Controller's permission list can allow this one script instead of open-ended shell.
# Never: secrets, gcloud, ios.yml / paid CI, gh pr merge (the train merges), force push, phone settings.
#
#   digest                         lanes, slots, PRs, queue, FLAGS (v2/sup/digest.py)
#   queue <L> <task> <brief> [model] [effort] [continue...]   write <notes>/v2/queue/<L>-<task>.req (lc_watch starts it)
#   hold <L>                       move a lane's queued .req to queue/held (pause non-P0 work)
#   unhold <L>                     move it back
#   train                          the last train/merge events and the newest TRN result lines
#   job <job-id>                   one test result (the .txt summary, never the .log)
#   fails <job-id>                 the error lines from a failed job's logs (grep, max 20 lines)
#   full                           submit a FULL job on origin/main (the install gate installs on PASS)
#   install-tick                   run the install gate now (lock, submit install when a full PASS exists)
#   phone                          devicectl device list + Appium server status + phone lock + phone-free answer
#   phone-free <HH:MM> <note...>   record JB's phone-free answer until HH:MM today (only when JB said the phone is free)
#   appium-start                   start the Appium server detached (127.0.0.1:4723) if it is not running
#   appium-check                   take the phone lock, open one Appium session (noReset, no app launch), report, close it
#   review <pr>                    start a review of a PR now (detached)
#   asks <args>                    the ask tracker (gen/asks.py): list | set <id> --state S --eta HH:MM --note "..." | add ...
#   eta <L> <HH:MM> [note...]      set a lane's ETC (JB 28 Oct 15:52: every state except STAGED/parked has one; digest flags NO-ETC)
#   log <text>                     append one line to v2/sup/COORD_LOG.md
set -u
N="$HOME/Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"
R="$HOME/Claude_London Commuter"; ROOT="${0:A:h:h}"
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3; [[ -x $PY ]] || PY=python3
D=00008110-0009399002FA801E
cmd="${1:-}"; shift 2>/dev/null
log() { print -r -- "$(date '+%F %H:%M') coord(ctl): $*" >> "${LC_COORD_LOG:-$N/v2/sup/COORD_LOG.md}"; }
# simulations (gen/sim_controller.py sets LC_CTL_SIM=1): only read-only commands run; anything that changes lanes, jobs or the phone is refused
if [[ -n "${LC_CTL_SIM:-}" && "$cmd" =~ '^(queue|hold|unhold|full|install-tick|phone-free|appium-start|appium-check|review|eta)$' ]]; then
  echo "SIM: '$cmd' refused in a simulation (read-only commands only)"; log "SIM refused $cmd $*"; exit 0; fi
case "$cmd" in
  digest) $PY "$N/v2/sup/digest.py" | grep -v 'install gate: no current' ;;
  queue)
    L="$1"; T="$2"; B="$3"; M="${4:-sonnet}"; E="${5:-high}"; shift 5 2>/dev/null; C="$*"
    [[ "$L" =~ '^[A-Z][A-Za-z0-9]{0,7}$' && "$T" =~ '^[a-z0-9][a-z0-9-]{0,30}$' ]] || { echo "bad lane or task id"; exit 2; }
    [[ -f "$B" ]] || { echo "brief not found: $B"; exit 2; }
    [[ $(wc -c < "$B") -le 8000 ]] || { echo "brief over 8000 bytes: shorten it"; exit 2; }
    [[ "$M" =~ '^(haiku|sonnet|opus)$' ]] || { echo "model must be haiku|sonnet|opus"; exit 2; }
    pct=$(tail -1 "$N/w1/evidence/usage_pct.csv" | cut -d, -f2); if (( ${pct%.*} >= 85 )); then echo "usage ${pct}% >= 85%: no new lanes"; exit 3; fi
    printf '%s\n' "$L" "$T" "$B" "$M" "$E" lane "$C" > "$N/v2/queue/$L-$T.req.tmp" && mv "$N/v2/queue/$L-$T.req.tmp" "$N/v2/queue/$L-$T.req"
    log "queued $L/$T ($M/$E) brief $B"; echo "queued $L/$T: lc_watch starts it within a minute (or parks it until a slot frees)" ;;
  hold) for f in "$N"/v2/queue/"$1"-*.req(N); do mv "$f" "$N/v2/queue/held/"; echo "held ${f:t}"; log "held ${f:t}"; done ;;
  unhold) for f in "$N"/v2/queue/held/"$1"-*.req(N); do mv "$f" "$N/v2/queue/"; echo "unheld ${f:t}"; log "unheld ${f:t}"; done ;;
  train) grep -a -E 'train/|merging|merged #' "$N/announce.log" | tail -8 | cut -c1-220
         for f in ${(f)"$(ls -t "$N"/testq/results/*TRN*.txt 2>/dev/null | head -3)"}; do echo "== ${f:t}"; grep -E '^(overall|\[)' "$f" | cut -c1-160; done
         ls "$N/testq/running" 2>/dev/null | sed 's/^/running: /' ;;
  job) f="$N/testq/results/$1.txt"; [[ -f $f ]] && cat "$f" | cut -c1-200 || { ls "$N/testq/running" "$N/testq/pending" 2>/dev/null | grep -F "$1" && echo "(still queued/running)" || echo "no such job"; } ;;
  fails) for f in "$N"/testq/results/"$1".*.log(N); do grep -a -E 'error:|✘ Test|FAILED|Fatal' "$f" | head -8 | cut -c1-240 | sed "s|^|${f:t:r:e}: |"; done | head -20 ;;
  full) cd "$R" && git fetch -q origin && S=$(git rev-parse origin/main) && \
        scripts/test-submit.sh --lane FULL --sha "$S" --branch main --base "$S~1" --targets "all go app watch" --priority 1 && log "FULL job on main ${S:0:8}" ;;
  install-tick) cd "$R" && scripts/install_gate.sh tick; scripts/install_gate.sh holder ;;
  phone) xcrun devicectl list devices 2>/dev/null | grep -E 'iPhone|Watch' | grep -v simulated
         curl -s -m 3 localhost:4723/status | grep -q '"ready":true' && echo "Appium server: up on :4723" || echo "Appium server: down"
         curl -s -m 3 localhost:8100/status | grep -q sessionId && echo "WDA: listening on :8100" || echo "WDA: not listening"
         cd "$R" && scripts/install_gate.sh holder; cat "$N/w1/phone_free.json" 2>/dev/null ;;
  phone-free) [[ "${1:-}" =~ '^[0-2][0-9]:[0-5][0-9]$' ]] || { echo "usage: phone-free HH:MM note"; exit 2; }; u="$(date +%Y-%m-%d)T$1"; shift
        $PY -c 'import json,sys,datetime;json.dump({"at":datetime.datetime.now().strftime("%Y-%m-%dT%H:%M"),"by":"JB","until":sys.argv[2],"note":" ".join(sys.argv[3:])[:300]},open(sys.argv[1],"w"))' "$N/w1/phone_free.json" "$u" "$@" && log "phone-free until $u" && cat "$N/w1/phone_free.json" ;;
  appium-start) curl -s -m 3 localhost:4723/status | grep -q '"ready":true' && { echo "Appium already up"; exit 0; }
        mkdir -p ~/.appium/logs; (nohup appium --address 127.0.0.1 --port 4723 > ~/.appium/logs/appium-$(date +%m%d-%H%M).log 2>&1 &)
        for i in {1..20}; do curl -s -m 2 localhost:4723/status | grep -q '"ready":true' && { echo "Appium up"; log "appium started"; exit 0; }; sleep 1; done; echo "Appium did not start"; exit 1 ;;
  appium-check)
        cd "$R" && scripts/install_gate.sh lock appium-check 15 || { echo "phone lock held: $(scripts/install_gate.sh holder)"; exit 1; }
        "$0" appium-start >/dev/null
        out=$(curl -s -m 360 -X POST localhost:4723/session -H 'content-type: application/json' -d '{"capabilities":{"alwaysMatch":{"platformName":"iOS","appium:automationName":"XCUITest","appium:udid":"'$D'","appium:xcodeOrgId":"HXPCQ5MYQ7","appium:updatedWDABundleId":"com.coresf.WebDriverAgentRunner","appium:noReset":true,"appium:fullReset":false,"appium:autoLaunch":false,"appium:wdaLaunchTimeout":240000}}}')
        sid=$(print -r -- "$out" | $PY -c 'import json,sys;print((json.load(sys.stdin).get("value") or {}).get("sessionId") or "")' 2>/dev/null)
        if [[ -n $sid ]]; then echo "APPIUM OK: session $sid on $D"; curl -s -m 20 -X DELETE "localhost:4723/session/$sid" >/dev/null; log "appium-check OK"
        else msg=$(print -r -- "$out" | $PY -c 'import json,sys;print(str((json.load(sys.stdin).get("value") or {}).get("message"))[:300])' 2>/dev/null)
             echo "APPIUM FAILED: $msg"; print -r -- "$msg" | grep -q -i 'automation mode\|code 65' && echo "Likely cause: iPhone locked, or Settings > Developer > Enable UI Automation is off. JB: unlock the phone and keep it awake, then run appium-check again."; log "appium-check FAILED"; fi
        scripts/install_gate.sh unlock appium-check >/dev/null ;;
  review) cd "$R" && [[ "$1" =~ '^[0-9]+$' ]] && { (LC_MAX_LANES=6 nohup scripts/review.sh "$1" >> "$N/v2/bg.log" 2>&1 &); echo "review #$1 started"; log "review #$1 started"; } ;;
  asks) $PY "$ROOT/gen/asks.py" "$@" ;;
  eta) [[ "${1:-}" =~ '^[A-Z][A-Za-z0-9]{0,7}$' && "${2:-}" =~ '^[0-2][0-9]:[0-5][0-9]$' ]] || { echo "usage: eta <L> HH:MM [note]"; exit 2; }
       $PY -c 'import json,sys,os,datetime as d
p,L,hm=sys.argv[1:4]; n=d.datetime.now(); t=n.replace(hour=int(hm[:2]),minute=int(hm[3:]),second=0,microsecond=0)
t=t+d.timedelta(days=1) if t<n-d.timedelta(hours=1) else t
try: e=json.load(open(p))
except (OSError,ValueError): e={}
o=e.get(L) or {}; why=" ".join(sys.argv[4:])[:200]; new=t.strftime("%Y-%m-%dT%H:%M")
if o.get("eta") and o["eta"]!=new and not why: sys.exit("ETC %s -> %s needs a reason (JB A99: no unexplained ETC changes): eta %s HH:MM <why>"%(o["eta"][11:],new[11:],L))
h=(o.get("hist") or [])+([{k:o[k] for k in ("eta","at","note") if k in o}] if o.get("eta") and o["eta"]!=new else [])
e[L]={"eta":new,"at":n.strftime("%Y-%m-%dT%H:%M"),"note":why,"hist":h[-5:]}
json.dump(e,open(p+".tmp","w"),indent=1); os.replace(p+".tmp",p); print("lane %s ETC %s"%(L,e[L]["eta"]))' "$N/v2/sup/lane_eta.json" "$@" && log "lane $1 ETC $2 ${*:3}" ;;
  log) log "$*"; echo logged ;;
  *) sed -n 2,26p "$0"; exit 2 ;;
esac
