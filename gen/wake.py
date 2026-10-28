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
4. slot refill    : JB 28 Oct 16:03 "there should be no available slots". While code slots are free (<notes>/v2/max_lanes minus
                     lane locks minus pending .req files), weekly usage < 85% and memory free >= 20%, queue the first
                     v2/sup/next.txt line "NEXT <L> | <task> | <brief path> [| model | effort]" whose lane is idle (no
                     lock, no queued/held .req, not queued from next.txt before). The Controller keeps next.txt in P0 order.
5. staged tasks   : JB 28 Oct 16:08. A W1 plan task still "todo" (STAGED) whose dependencies are all done and whose day has
                     come: done check passes -> `lc_state.py done`; owner SCRIPT/QUEUE -> `lc_state.py run` (detached, once a
                     day); else one batched Controller wake (once per task a day). Each task is tried at most every 15 min.
6. timeline truth : JB 28 Oct 16:18 "the V3 timeline does not reflect what you are telling me". Every minute, compare the
                     published page data (w1/control-room/w1.json) with the machine: each lane lock (v2/locks/lane-<L>) must be
                     a live lane row holding a slot, no row may hold a slot without a lock, each lane's ETC must equal
                     v2/sup/lane_eta.json, no planned lane task may be drawn across "now" unless it is running, and the data
                     must be < 5 min old. A disagreement that lasts 3+ min is red: v2/sup/wake/timeline-mismatch (the digest
                     prints it under FLAGS), one ALERTS.txt line and one Controller wake per distinct mismatch.
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

AUTH = os.path.join(NOTES, "w1", "evidence", "usage_authorized")   # first line starts YYYY-MM-DD: JB authorised usage that day
def usage_pct():
    try:
        if open(AUTH).read()[:10] == dt.date.today().isoformat(): return None
    except OSError: pass
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
    if marked("coord-" + key): return False
    pct = usage_pct()
    if pct is not None and pct >= STOP_PCT:
        if not marked("coord-quota-" + key): announce("Controller wake skipped (usage %.0f%% >= %.0f%%): %s" % (pct, STOP_PCT, text[:120])); mark("coord-quota-" + key)
        return False
    if wakes_today() >= MAX_DAY:
        if not marked("coord-cap-" + key): announce("Controller wake skipped (daily cap %d): %s" % (MAX_DAY, text[:120])); mark("coord-cap-" + key)
        return False
    if DRY: print("DRY would wake Controller: %s" % text); return False
    mark("coord-" + key, now())
    if not os.environ.get("LC_WAKE_NOINJECT"): subprocess.run([sys.executable, os.path.join(ROOT, "gen", "chat_sync.py"), "inject", "COORD", text], timeout=60)
    announce("Controller woken: %s" % text[:160])
    return True

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

ASKS = os.path.join(NOTES, "w1", "asks.json")
ASK_EVERY, ASK_REWAKE = 5 * 60, 15 * 60
def asks_check():
    """4. JB's asks (JB 28 Oct 14:47: 'the coordinator should have been alerted multiple times'). Every 5 min: an open ask past
    its ETA, BLOCKED, or with no ETA is late. Late asks wake the Controller at once when a new id turns late, and again every
    15 min while any stays late. Not counted against MAX_DAY (JB's asks are P0); the usage stop still holds, then a P0 line
    goes to ALERTS.txt instead. The page turns overdue asks red itself (index.html, client-side)."""
    st = os.path.join(MK, "asks-check")
    if time.time() - mtime(st) < ASK_EVERY: return
    if not DRY: open(st, "w").write(now())
    t = dt.datetime.now(); late = []
    for a in (js(ASKS, {}) or {}).get("asks", []):
        if a.get("state") == "DONE": continue
        why = "BLOCKED" if (a.get("blocked") or a.get("state") == "BLOCKED") else ""
        if not why and not a.get("eta"): why = "no ETA"
        if not why:
            try:
                if dt.datetime.fromisoformat(a["eta"]) < t: why = "overdue since " + a["eta"][11:16]
            except ValueError: why = "bad ETA"
        if why: late.append((a["id"], why, a.get("title", "")[:60]))
    for p in glob.glob(os.path.join(NOTES, "status", "*.txt")):  # a BLOCKED flag left 60+ min (blocker may have merged) is late too
        try: first = open(p).read().split(None, 1)[:1]
        except OSError: continue
        if first == ["blocked"] and time.time() - mtime(p) > 3600:
            late.append(("lane-" + os.path.basename(p)[:-4], "BLOCKED %d min" % ((time.time() - mtime(p)) // 60), "clear it if its blocker is gone, else act"))
    lf = os.path.join(MK, "asks-late")
    prev = (open(lf).read().split() if os.path.exists(lf) else [])
    ids = [i for i, _, _ in late]
    if not late:
        if prev and not DRY: os.remove(lf)
        return
    new = [i for i in ids if i not in prev[1:]]
    if not new and time.time() - mtime(lf) < ASK_REWAKE: return
    text = ("Watchdog: %d of JB's asks are late (P0): %s. For each: act now (fix lane, train, page), then "
            "`ctl.sh asks set <id> --state ... --eta HH:MM --note ...` with a real ETA; DONE only when verified.") % (
        len(late), "; ".join("%s %s (%s)" % x for x in late[:15]))
    if not DRY: open(lf, "w").write(now()[:16].replace(" ", "T") + " " + " ".join(ids))
    pct = usage_pct()
    if pct is not None and pct >= STOP_PCT:
        if not DRY: open(os.path.join(NOTES, "ALERTS.txt"), "a").write("%s [P0] ASKS-LATE (no wake, usage %.0f%%): %s\n" % (now(), pct, text))
        announce("asks late, Controller wake skipped (usage %.0f%%): %s" % (pct, " ".join(ids)))
        return
    if DRY: print("DRY would wake Controller: %s" % text); return
    if not os.environ.get("LC_WAKE_NOINJECT"): subprocess.run([sys.executable, os.path.join(ROOT, "gen", "chat_sync.py"), "inject", "COORD", text], timeout=60)
    open(os.path.join(NOTES, "ALERTS.txt"), "a").write("%s [P0] ASKS-LATE: %s\n" % (now(), text))
    announce("Controller woken (asks late): %s" % " ".join(ids))

NEXT_F = os.path.join(SUP, "next.txt")
def max_lanes():
    try: return max(1, min(3, int(open(os.path.join(V2, "max_lanes")).read().strip())))   # CLAUDE.md rule 3: max 3
    except (OSError, ValueError): return 2
def mem_free_pct():
    if os.environ.get("LC_WAKE_MEMFREE"): return float(os.environ["LC_WAKE_MEMFREE"])
    try:
        o = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"free percentage:\s*(\d+)", o); return float(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError): return None
def next_items():
    out = []
    try: lines = open(NEXT_F).read().splitlines()
    except OSError: return out
    for l in lines:
        if not l.startswith("NEXT "): continue
        f = [x.strip() for x in l[5:].split("|")]
        if len(f) < 3 or not re.match(r"^[A-Z][A-Za-z0-9]{0,7}$", f[0]) or not re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", f[1]): continue
        out.append((f[0], f[1], f[2], f[3] if len(f) > 3 and f[3] in ("haiku", "sonnet", "opus") else "sonnet", f[4] if len(f) > 4 and f[4] else "high"))
    return out
def refill():
    """Fill free code slots from next.txt, in order, one .req per free slot."""
    pct = usage_pct()
    if pct is not None and pct >= STOP_PCT: return
    used = len(glob.glob(os.path.join(V2, "locks", "lane-*"))) + len(glob.glob(os.path.join(V2, "queue", "*.req")))
    free = max_lanes() - used
    if free <= 0: return
    mf = mem_free_pct()
    if mf is not None and mf < 20:
        if not marked("refill-mem-" + now()[:13]): announce("slot refill skipped: memory free %.0f%% < 20%%" % mf); mark("refill-mem-" + now()[:13])
        return
    for L, T, B, M, EF in next_items():
        if free <= 0: break
        if marked("next-%s-%s" % (L, T)) or locked(L) or req_exists(L) or glob.glob(os.path.join(V2, "queue", "held", L + "-*.req")): continue
        if not os.path.isfile(B) or os.path.getsize(B) > 8000:
            if not marked("next-bad-%s-%s" % (L, T)): announce("slot refill: next.txt %s/%s brief missing or over 8 KB: %s" % (L, T, B)); mark("next-bad-%s-%s" % (L, T))
            continue
        if DRY: print("DRY would refill %s/%s" % (L, T)); free -= 1; continue
        q = os.path.join(V2, "queue", "%s-%s.req" % (L, T)); tmp = q + ".tmp"
        open(tmp, "w").write("\n".join([L, T, B, M, EF, "lane", ""]) + "\n"); os.replace(tmp, q)
        mark("next-%s-%s" % (L, T), now()); free -= 1
        announce("slot refill: queued %s/%s (%s/%s) from next.txt, %d slot(s) still free" % (L, T, M, EF, free))

REPO = os.environ.get("LC_WAKE_REPO") or os.path.join(HOME, "Claude_London Commuter")
W1D = os.environ.get("LC_W1_DIR") or os.path.join(NOTES, "w1")
STAGED_EVERY = 15 * 60
def lc_state(*a):
    e = dict(os.environ, LC_W1_DIR=W1D)
    return subprocess.run([sys.executable, os.path.join(REPO, "scripts", "lc_state.py")] + list(a), cwd=REPO, env=e,
                          capture_output=True, text=True, timeout=120)
def staged():
    """W1 plan tasks still 'todo' (STAGED, red on the page) whose dependencies are all done, planned day reached:
    the done check passes -> close it; owner SCRIPT -> start it; otherwise one batched Controller wake (once per task a day)."""
    F = os.environ.get("LC_W1_TASKS") or os.path.join(REPO, "docs", "handover", "v3", "W1_TASKS.json")
    T = (js(F, {}) or {}).get("tasks") or []; S = js(os.path.join(W1D, "state.json"), {}) or {}
    st = lambda i: (S.get(i) or {}).get("status", "todo")
    today = dt.date.today().isoformat(); tell = []
    for t in T:
        i = t["id"]
        if st(i) != "todo" or t.get("day", "") > today or any(st(d) != "done" for d in t.get("depends", [])): continue
        k = "staged-try-" + i
        if time.time() - mtime(os.path.join(MK, k)) < STAGED_EVERY: continue
        if not DRY: mark(k, now())
        chk = t.get("done_check") or "false"
        ok = False
        if not chk.startswith("MANUAL"):
            env = dict(os.environ, EV=os.path.join(W1D, "evidence"), REPO=REPO, MAIN=REPO, LC_W1_DIR=W1D)
            try: ok = subprocess.run(["bash", "-o", "pipefail", "-c", chk], cwd=REPO, env=env, capture_output=True, timeout=120).returncode == 0
            except (OSError, subprocess.SubprocessError): ok = False
        if ok:
            if DRY: print("DRY would close %s" % i); continue
            r = lc_state("done", i, "--note", "closed by watchdog: dependencies done and the done check passes")
            if r.returncode == 0: announce("staged %s closed: dependencies done, done check passes" % i); continue
            tell.append("%s (done check passes but lc_state refused: %s)" % (i, (r.stdout + r.stderr).strip().splitlines()[-1][:140] if (r.stdout + r.stderr).strip() else "rc %d" % r.returncode))
        elif t.get("owner") in ("SCRIPT", "QUEUE"):
            if DRY: print("DRY would run %s" % i); continue
            if marked("staged-run-%s-%s" % (i, today)): continue
            mark("staged-run-%s-%s" % (i, today), now())
            lg = open(os.path.join(MK, "staged-run-%s.log" % i), "a")
            subprocess.Popen([sys.executable, os.path.join(REPO, "scripts", "lc_state.py"), "run", i], cwd=REPO, env=dict(os.environ, LC_W1_DIR=W1D),
                             stdout=lg, stderr=lg, start_new_session=True)
            announce("staged %s started (owner %s, dependencies done)" % (i, t.get("owner")))
        elif not marked("staged-told-%s-%s" % (i, today)):
            tell.append("%s (%s, %s/%s): %s" % (i, t.get("owner"), t.get("model"), t.get("effort"), str(t.get("title", ""))[:90]))
    tell = [x for x in tell if not marked("staged-told-%s-%s" % (x.split(" ")[0], today))]
    if not tell: return
    woke = wake_coord("staged-%s-%s" % (today, "+".join(x.split(" ")[0] for x in tell)),
               "Watchdog: STAGED plan tasks with every dependency closed (red on the page). Start each (lane, coordinator step, "
               "Cowork prompt or JB card) or close it with evidence (`lc_state.py done <id>`): " + "; ".join(tell))
    if woke:   # a capped or quota-skipped wake is retried on the next 15-min try
        for x in tell: mark("staged-told-%s-%s" % (x.split(" ")[0], today), now())

W1J = os.path.join(NOTES, "w1", "control-room", "w1.json")
TL_GRACE = 180
def timeline_problems(d, locks, leta, now_=None):
    """Pure check (tested in test_wake.py): w1.json dict, set of locked lanes, lane_eta.json dict -> list of problems."""
    now_ = now_ or dt.datetime.now(); out = []
    try: gen = dt.datetime.fromisoformat(d.get("gen_iso", ""))
    except ValueError: gen = None
    if not gen or (now_ - gen).total_seconds() > 300: out.append("page data stale (generated %s)" % (d.get("gen_iso", "?")[11:16] or "?"))
    lv = {x.get("lane"): x for x in d.get("live_lanes") or []}
    if "live_lanes" not in d: out.append("page data has no live_lanes (generator without the live-lane rows)")
    for L in sorted(locks):
        if not (lv.get(L) or {}).get("holds"): out.append("lane %s is running but has no live row" % L)
    for L, x in sorted(lv.items()):
        if x.get("holds") and L not in locks: out.append("row %s holds a slot but lane has no lock" % L)
        want = ((leta.get(L) or {}).get("eta") or "")[11:16]
        if want and x.get("etc") != want: out.append("lane %s ETC on page %s, set %s" % (L, x.get("etc") or "none", want))
    for t in d.get("tasks") or []:
        sl = t.get("slot") or ""
        if not sl.startswith("LANE ") or t.get("status") in ("done", "doing"): continue
        try: b, f = dt.datetime.fromisoformat(t["beg_iso"]), dt.datetime.fromisoformat(t["fin_iso"])
        except (KeyError, ValueError): continue
        if gen and b <= gen - dt.timedelta(minutes=2) and f > gen: out.append("%s drawn as running on %s but not started" % (t["id"], sl))
    return out

def timeline_check():
    """6. timeline truth (see the module doc)."""
    locks = {os.path.basename(p)[5:] for p in glob.glob(os.path.join(V2, "locks", "lane-*"))}
    probs = timeline_problems(js(W1J, {}) or {}, locks, js(os.path.join(SUP, "lane_eta.json"), {}) or {})
    mf = os.path.join(MK, "timeline-mismatch"); first = os.path.join(MK, "timeline-first")
    if not probs:
        for f in (mf, first):
            if os.path.exists(f) and not DRY: os.remove(f)
        return
    if not os.path.exists(first):
        if not DRY: open(first, "w").write(now())
        return
    if time.time() - mtime(first) < TL_GRACE: return
    text = "; ".join(probs[:8])
    if DRY: print("DRY timeline mismatch: %s" % text); return
    open(mf, "w").write("%s %s\n" % (now()[:16], text))
    key = "timeline-" + re.sub(r"[^A-Za-z0-9]+", "-", text)[:80]
    if not marked(key):
        mark(key, now())
        open(os.path.join(NOTES, "ALERTS.txt"), "a").write("%s [P1] TIMELINE-MISMATCH: %s\n" % (now(), text))
        announce("TIMELINE-MISMATCH (red): %s" % text[:200])
        wake_coord(key, "Watchdog: the V3 timeline disagrees with the live lanes (red): %s. Fix the cause (ETC via `ctl.sh eta`, "
                   "stale page = push.sh/generator, plan task state via lc_state.py), never the drawing." % text)

if __name__ == "__main__":
    os.makedirs(MK, exist_ok=True)
    try: step()
    except Exception as e: announce("error %r" % e)
    try: asks_check()
    except Exception as e: announce("asks check error %r" % e)
    try: refill()
    except Exception as e: announce("refill error %r" % e)
    try: staged()
    except Exception as e: announce("staged error %r" % e)
    try: timeline_check()
    except Exception as e: announce("timeline check error %r" % e)
