"""Watchdog (0 model tokens), run every minute by mac/push.sh after the chat sync. JB 28 Oct 10:24: the machinery must keep
moving without JB messaging a session. It does only mechanical moves and wakes the Controller (chat target COORD) on
judgement events:

1. review CHANGES  : a lane in state "pr" whose PR has a <notes>/reviews/<pr>-<sha>.changes newer than the lane's last run
                     -> requeue the lane (<notes>/v2/queue/<L>-<T>.req) with a short brief pointing at the review result.
                     At most 2 automatic rounds per PR; a 3rd CHANGES wakes the Controller instead.
2. dead relaunch   : a lane left in "relaunching" (or "waiting" with its test result present) with no lane lock for 5+ min
                     (lane.sh died, e.g. "brief over 8000 bytes") -> requeue with a short brief (always < 8 KB).
3. Controller wake : judgement events (3rd CHANGES, a BLOCKED lane, a requeue that failed twice) post one message into the
                     COORD thread and start one Controller run. Caps: LC_WAKE_MAX_DAY (default 6) per day, one per event,
                     none while weekly usage >= 85% (the newest row of usage_pct.csv).
Never runs lane.sh itself (lc_watch starts .req files), never touches git. Usage: wake.py [--dry-run]
"""
import glob, json, os, re, subprocess, sys, time, datetime as dt

DRY = "--dry-run" in sys.argv
HOME = os.path.expanduser("~")
NOTES = os.environ.get("LC_WAKE_NOTES") or os.path.join(HOME, "Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08")
V2 = os.path.join(NOTES, "v2"); TQ = os.path.join(NOTES, "testq"); REV = os.path.join(NOTES, "reviews")
SUP = os.path.join(V2, "sup"); MK = os.path.join(SUP, "wake"); BR = os.path.join(V2, "briefs", "auto")
USAGE = os.path.join(NOTES, "w1", "evidence", "usage_pct.csv")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOP_PCT = 85.0
MAX_DAY = int(os.environ.get("LC_WAKE_MAX_DAY", "6"))
DEAD_MIN = 5
END = ("End with: WAITING job=<id> | PR #<n> ready head <sha> | blocked: <why>. Red test first; heavy tests only via "
       "scripts/test-submit.sh.\nIf push/gh refused: commit and end \"BLOCKED: push needed, commit <sha> on <branch>\".\n")

def now(): return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def announce(msg):
    print(msg)
    if not DRY:
        with open(os.path.join(NOTES, "announce.log"), "a") as f: f.write("%s watchdog: %s\n" % (now(), msg))
def js(p, d=None):
    try: return json.load(open(p))
    except (OSError, ValueError): return d
def mtime(p):
    try: return os.path.getmtime(p)
    except OSError: return 0
def marked(name): return os.path.exists(os.path.join(MK, name))
def mark(name, text=""):
    if DRY: return
    os.makedirs(MK, exist_ok=True); open(os.path.join(MK, name), "w").write(text or now())

def usage_pct():
    try: rows = [l.split(",") for l in open(USAGE).read().splitlines() if re.match(r"\d{4}-", l)]
    except OSError: return None
    try: return float(rows[-1][1])
    except (IndexError, ValueError): return None

EPOCH_F = os.path.join(MK, "epoch")   # events older than the watchdog's first run are history, never acted on
def epoch():
    if not os.path.exists(EPOCH_F) and not DRY:
        os.makedirs(MK, exist_ok=True); open(EPOCH_F, "w").write(now())
    return mtime(EPOCH_F) or time.time()

def req_exists(L): return bool(glob.glob(os.path.join(V2, "queue", L + "-*.req")))
def locked(L): return os.path.isdir(os.path.join(V2, "locks", "lane-" + L))

def write_req(L, reg, brief_text, cont, why):
    T = reg.get("task") or "task"
    os.makedirs(BR, exist_ok=True)
    bf = os.path.join(BR, "%s-%s.md" % (L, dt.datetime.now().strftime("%m%d-%H%M")))
    q = os.path.join(V2, "queue", "%s-%s.req" % (L, T))
    if DRY: print("DRY would queue %s: %s" % (L, why)); return True
    open(bf, "w").write(brief_text.encode()[:7800].decode("utf-8", "ignore"))
    lines = [L, T, bf, reg.get("model") or "sonnet", reg.get("effort") or "high", reg.get("role") or "lane", cont.replace("\n", " ")[:400]]
    tmp = q + ".tmp"; open(tmp, "w").write("\n".join(lines) + "\n"); os.replace(tmp, q)
    announce("queued %s/%s (%s)" % (L, T, why))
    return True

def wakes_today():
    d = dt.date.today().isoformat()
    return len([f for f in glob.glob(os.path.join(MK, "coord-*")) if open(f).read()[:10] == d])

def wake_coord(key, text):
    """One Controller run for one judgement event."""
    if marked("coord-" + key): return
    pct = usage_pct()
    if pct is not None and pct >= STOP_PCT:
        if not marked("coord-quota-" + key): announce("Controller wake skipped (usage %.0f%% >= %.0f%%): %s" % (pct, STOP_PCT, text[:120])); mark("coord-quota-" + key)
        return
    if wakes_today() >= MAX_DAY:
        if not marked("coord-cap-" + key): announce("Controller wake skipped (daily cap %d): %s" % (MAX_DAY, text[:120])); mark("coord-cap-" + key)
        return
    if DRY: print("DRY would wake Controller: %s" % text); return
    mark("coord-" + key, now())
    if not os.environ.get("LC_WAKE_NOINJECT"): subprocess.run([sys.executable, os.path.join(ROOT, "gen", "chat_sync.py"), "inject", "COORD", text], timeout=60)
    announce("Controller woken: %s" % text[:160])

def review_result(pr, sha):
    fs = sorted(glob.glob(os.path.join(V2, "runs", "review-%s-%s*.jsonl" % (pr, sha[:8]))), key=mtime)
    return fs[-1] if fs else ""

def step():
    pct = usage_pct(); E = epoch()
    for p in sorted(glob.glob(os.path.join(V2, "lanes", "*.json"))):
        L = os.path.basename(p)[:-5]; reg = js(p, {}) or {}
        st, pr = reg.get("state", ""), str(reg.get("pr") or "")
        if locked(L) or req_exists(L): continue
        last = max(mtime(reg.get("last_run") or ""), mtime(p) if st != "pr" else 0)
        # 1. review CHANGES newer than the lane's last run
        if st == "pr" and pr:
            ch = sorted(glob.glob(os.path.join(REV, "%s-*.changes" % pr)), key=mtime)
            ap = sorted(glob.glob(os.path.join(REV, "%s-*.approve" % pr)), key=mtime)
            if ch and mtime(ch[-1]) > E and mtime(ch[-1]) > mtime(reg.get("last_run") or "") and (not ap or mtime(ch[-1]) > mtime(ap[-1])):
                sha = os.path.basename(ch[-1])[len(pr) + 1:-8]
                key = "changes-%s-%s" % (pr, sha[:8])
                if marked(key): continue
                rounds = len(glob.glob(os.path.join(MK, "changes-%s-*" % pr)))
                if rounds >= 2:
                    wake_coord(key, "Watchdog: PR #%s (lane %s) got review CHANGES a 3rd time (head %s). Decide: split the task, "
                               "escalate the model once, or stop it. Review result: %s" % (pr, L, sha[:8], review_result(pr, sha)))
                    mark(key, "escalated"); continue
                if pct is not None and pct >= STOP_PCT:
                    if not marked("quota-" + key): announce("not requeuing %s for #%s CHANGES: usage %.0f%% >= %.0f%%" % (L, pr, pct, STOP_PCT)); mark("quota-" + key)
                    continue
                rr = review_result(pr, sha)
                brief = ("TASK %s, review round (automatic, watchdog). PR #%s, branch %s, head %s got review CHANGES.\n"
                         "Read the final \"result\" of %s (the findings) and fix every VERIFIED finding on this branch; fix SUSPECTED ones "
                         "or say in the PR why not. CLAUDE.md wins if a finding conflicts with the original brief.\n"
                         "Original brief (context only): %s\nRebase on origin/main, then resubmit tests.\n%s") % (
                    L, pr, reg.get("branch", ""), sha[:8], rr or "(review run file not found: read the PR's verdict comment)", reg.get("brief", ""), END)
                if write_req(L, reg, brief, "Review CHANGES on PR #%s head %s: fix the findings per the brief." % (pr, sha[:8]), "review CHANGES #%s %s, round %d" % (pr, sha[:8], rounds + 1)):
                    mark(key)
            continue
        # 2. a relaunch that died (no lock, nothing queued, state stuck for DEAD_MIN)
        job = reg.get("job") or ""
        res = os.path.join(TQ, "results", job + ".txt") if job else ""
        stuck = (st == "relaunching") or (st == "waiting" and res and os.path.exists(res))
        if stuck and mtime(p) > E and time.time() - max(mtime(p), mtime(res)) > DEAD_MIN * 60:
            key = "dead-%s-%s" % (L, job or st)
            n = len(glob.glob(os.path.join(MK, "dead-%s-*" % L)))
            if marked(key): continue
            if n >= 2:
                wake_coord(key, "Watchdog: lane %s keeps dying on relaunch (state %s, job %s). Check %s/v2/bg.log and the brief, then requeue or stop it." % (L, st, job, NOTES))
                mark(key, "escalated"); continue
            if pct is not None and pct >= STOP_PCT: continue
            ov = ""
            try: ov = [l for l in open(res).read().splitlines() if l.startswith("overall=")][0]
            except (OSError, IndexError): pass
            brief = ("TASK %s, continue (automatic, watchdog: the previous relaunch died without starting).\n"
                     "Original brief: %s (read it; if it is long, read only what you need).\nBranch %s, PR %s.\n"
                     "Test job %s finished: %s. Result file: %s (read it; never its .log files). Continue the task from the worktree state.\n%s") % (
                L, reg.get("brief", ""), reg.get("branch", ""), pr or "-", job or "-", ov or "?", res or "-", END)
            if write_req(L, reg, brief, "Test job %s finished: %s. Continue." % (job, ov or "?"), "dead relaunch, state %s" % st):
                mark(key)
    # 3. a lane that ended BLOCKED (not a push request, which the Controller brief handles too) wakes the Controller once
    for p in glob.glob(os.path.join(NOTES, "status", "*.txt")):
        L = os.path.basename(p)[:-4]
        try: ln = open(p).read().splitlines()
        except OSError: continue
        if ln and ln[0].split()[:1] == ["blocked"] and mtime(p) > E:
            wake_coord("blocked-%s-%d" % (L, int(mtime(p))), "Watchdog: lane %s ended BLOCKED: %s" % (L, (ln[1] if len(ln) > 1 else "")[:300]))

if __name__ == "__main__":
    os.makedirs(MK, exist_ok=True)
    try: step()
    except Exception as e: announce("error %r" % e)
