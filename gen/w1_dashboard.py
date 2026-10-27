#!/usr/bin/env python3
"""London Commuter v3 Week 1 control room: one self-contained HTML page built from files on disk. 0 model tokens.
Reads: W1_TASKS.json (origin/main, else the kit worktree), w1/state.json, w1/evidence/*, testq/results/*, ALERTS.txt,
COORDINATOR.json, git origin/main, and a 10-minute gh cache. Optional coordinator overrides: w1/dashboard_notes.json
  {"W1-07": {"note": "...", "plan": "...", "rag": "amber", "why": "...", "est_min": 60}}
Writes: w1/W1_DASHBOARD.html (atomic). Python 3.9 stdlib. Run by launchd every 2 minutes.
Usage: w1_dashboard.py [--notes DIR] [--repo DIR] [--out FILE] [--json FILE] [--now YYYY-MM-DDTHH:MM]
  --out writes the self-contained page (data embedded) for local viewing; --json writes the data alone, which push.sh
  encrypts and publishes for the hosted page (index.html at the repo root)."""
import glob, json, os, re, subprocess, sys, time, datetime as dt

A = sys.argv[1:]
def arg(k, d):
    return A[A.index(k) + 1] if k in A else d
HOME = os.path.expanduser("~")
NOTES = arg("--notes", os.path.join(HOME, "Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"))
REPO = arg("--repo", os.path.join(HOME, "Claude_London Commuter"))
W1 = os.path.join(NOTES, "w1"); EV = os.path.join(W1, "evidence")
OUT = arg("--out", os.path.join(W1, "W1_DASHBOARD.html"))
GH = next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
NOW = dt.datetime.strptime(arg("--now", ""), "%Y-%m-%dT%H:%M") if "--now" in A else dt.datetime.now().replace(second=0, microsecond=0)
TODAY = NOW.date().isoformat()

def rd(p, n=None):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read() if n is None else f.read(n)
    except OSError:
        return ""
def js(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d
def sh(cmd, cwd=None, t=20):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=t).stdout
    except Exception:
        return ""
def ts(s):
    if not s: return None
    s = s.replace("Z", "").replace(" ", "T")[:16]
    try: return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except ValueError: return None
def mtime(p):
    try: return dt.datetime.fromtimestamp(os.path.getmtime(p)).replace(microsecond=0)
    except OSError: return None
def hm(d): return d.strftime("%H:%M") if d else ""
SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*\S+")
def clean(s, n=600):
    return SECRET.sub(r"\1=[redacted]", (s or "").strip())[:n]

# ---------- definitions and state ----------
defs = None
if os.path.isdir(REPO):
    raw = sh(["git", "show", "origin/main:docs/handover/v3/W1_TASKS.json"], cwd=REPO)
    try: defs = json.loads(raw)
    except ValueError: defs = None
if not defs:
    for p in (os.path.join(REPO, ".worktrees/v3-w1-kit/docs/handover/v3/W1_TASKS.json"), os.path.join(W1, "W1_TASKS.json")):
        defs = js(p)
        if defs: break
defs = defs or {"tasks": [], "plan": "W1_TASKS.json not found"}
T = defs["tasks"]; BY = {t["id"]: t for t in T}
state = js(os.path.join(W1, "state.json"), {}) or {}
notes = js(os.path.join(W1, "dashboard_notes.json"), {}) or {}

# ---------- GitHub (cached 10 min; never blocks the page) ----------
ghc = os.path.join(W1, "gh_cache.json")
if "--now" not in A and os.path.isdir(REPO) and (not os.path.exists(ghc) or time.time() - os.path.getmtime(ghc) > 600):
    out = sh([GH, "pr", "list", "--state", "all", "--limit", "40", "--json", "number,title,state,mergedAt,headRefName,url,createdAt"], cwd=REPO, t=25)
    try:
        json.loads(out); tmp = ghc + ".tmp"; open(tmp, "w").write(out); os.replace(tmp, ghc)
    except ValueError:
        pass
prs = js(ghc, []) or []
PR = {p["number"]: p for p in prs}

# ---------- evidence ----------
def pr_for(tid):
    t = BY.get(tid, {}); ev = t.get("evidence") or ""
    cands = [ev] if ev else []
    cands.append("w1%s_pr.txt" % tid[3:].lower())
    for c in cands:
        m = re.search(r"^pr=(\d+)", rd(os.path.join(EV, c)), re.M)
        if m: return int(m.group(1))
    ro = t.get("review_of") or (tid[:-1] if tid.endswith("R") and tid[:-1] in BY else None)
    return pr_for(ro) if ro and ro != tid else None
lanes = {}
for p in glob.glob(os.path.join(EV, "lane-W1-*.json")):
    tid = os.path.basename(p)[5:-5]; d = js(p, {}) or {}; u = d.get("usage") or {}
    den = d.get("permission_denials") or []
    lanes[tid] = {"task": tid, "at": hm(mtime(p)), "mt": mtime(p), "cost": round(d.get("total_cost_usd") or 0, 2),
                  "turns": d.get("num_turns"), "error": bool(d.get("is_error")),
                  "start_ctx": (u.get("cache_creation_input_tokens") or 0),
                  "denied": [clean((x.get("tool_input") or {}).get("command") or x.get("tool_name", ""), 140) for x in den],
                  "result": clean(d.get("result"), 900)}
running_lanes = set()
for line in sh(["ps", "-axo", "command"]).splitlines():
    m = re.search(r"-n\s+\S+:\s*(W1-[0-9A-Z]+)", line)
    if m and "claude" in line: running_lanes.add(m.group(1))

def evidence_excerpt(tid):
    out = []
    for f in sorted(set(glob.glob(os.path.join(EV, tid + "-*.md")) + glob.glob(os.path.join(EV, "*" + tid + "*.md")) +
                        ([os.path.join(EV, BY[tid]["evidence"])] if BY.get(tid, {}).get("evidence") else []))):
        if os.path.isfile(f) and "J_answers" not in f:
            out.append({"file": os.path.basename(f), "at": hm(mtime(f)), "text": clean(rd(f, 1500), 700)})
    return out[:4]

# ---------- estimates (minutes) ----------
def est(t):
    if t["id"] in notes and notes[t["id"]].get("est_min"): return int(notes[t["id"]]["est_min"])
    o, m, e = t["owner"], t["model"], t["effort"]
    if o == "SCRIPT": return 5
    if o == "QUEUE": return 45
    if o == "JB": return 15
    if o == "COORD": return 20
    if o == "COWORK": return 90 if m == "opus" else 60
    if t["id"].endswith("R") or t.get("review_of"): return 45 if m != "opus" else 60
    return {"haiku": 45, "sonnet": 150 if e == "high" else 90, "opus": 180}.get(m, 60)

def started(tid):
    op = (state.get(tid) or {}).get("opened") or []
    return ts(op[-1]["at"]) if op else None
def status(tid): return (state.get(tid) or {}).get("status", "todo")

# ---------- projection: capacity-aware list scheduling ----------
# Pools: code lanes from <notes>/v2/max_lanes (default 3, CLAUDE.md max; 4 in the JB 27 Oct trial), 2 test runners, 1 coordinator, 1 Cowork, JB; scripts run at once.
# People (JB, Cowork) work 08:00-22:00. Nothing starts before its planned day at 08:00.
def _max_lanes():
    try: return max(1, min(6, int(open(os.path.join(NOTES, "v2", "max_lanes")).read().strip())))
    except Exception: return 3
POOLS = {"LANE": _max_lanes(), "QUEUE": 2, "COORD": 1, "COWORK": 1, "JB": 1, "SCRIPT": 99}   # LANE from <notes>/v2/max_lanes (JB 27 Oct: 4-lane 12 h trial)
kids = {}
for t in T:
    for d in t.get("depends", []): kids.setdefault(d, []).append(t["id"])
def desc(i, seen=None):
    seen = set() if seen is None else seen
    for k in kids.get(i, []):
        if k not in seen: seen.add(k); desc(k, seen)
    return seen
NDESC = {t["id"]: len(desc(t["id"])) for t in T}
def hours_ok(t, b):
    if t["owner"] in ("JB", "COWORK") and not 8 <= b.hour < 22:
        b = (b if b.hour < 8 else b + dt.timedelta(days=1)).replace(hour=8, minute=0)
    return b
fin, beg, slot = {}, {}, {}
free = {p: [NOW] * n for p, n in POOLS.items()}
for t in T:  # finished and running work first
    i = t["id"]; st = status(i)
    if st == "done":
        fin[i] = ts((state.get(i) or {}).get("updated")) or NOW; beg[i] = started(i) or fin[i] - dt.timedelta(minutes=5)
    elif st in ("doing", "blocked") and started(i):
        beg[i] = started(i); fin[i] = max(beg[i] + dt.timedelta(minutes=est(t)), NOW + dt.timedelta(minutes=10))
        pool = free.get(t["owner"], free["SCRIPT"]); k = pool.index(min(pool)); pool[k] = max(pool[k], fin[i]); slot[i] = "%s %d" % (t["owner"], k + 1)
left = [t for t in T if t["id"] not in fin]
while left:
    ready = [t for t in left if all(d in fin or d not in BY for d in t.get("depends", []))]
    if not ready:  # dependency cycle: place the rest at the end, flagged
        for t in left: beg[t["id"]] = fin[t["id"]] = max(fin.values(), default=NOW); slot[t["id"]] = "cycle"
        break
    def rt(t): return max([fin[d] for d in t.get("depends", []) if d in fin] + [NOW, dt.datetime.strptime(t["day"], "%Y-%m-%d").replace(hour=8)])
    t = min(ready, key=lambda t: (rt(t), t["day"], -NDESC[t["id"]], t["id"]))
    pool = free.get(t["owner"], free["SCRIPT"]); k = pool.index(min(pool))
    b = hours_ok(t, max(rt(t), pool[k])); f = b + dt.timedelta(minutes=est(t))
    beg[t["id"]], fin[t["id"]] = b, f; pool[k] = f; slot[t["id"]] = "%s %d" % (t["owner"], k + 1)
    left.remove(t)

# critical path: walk back from the latest projected finish through the latest-finishing open dependency
def chain(end):
    c = [end]
    while True:
        ds = [d for d in BY[c[-1]].get("depends", []) if d in BY and status(d) != "done"]
        if not ds: break
        c.append(max(ds, key=lambda d: fin[d]))
    return list(reversed(c))
open_ids = [t["id"] for t in T if status(t["id"]) != "done"]
crit = chain(max(open_ids, key=lambda i: fin[i])) if open_ids else []
gates = {}
for g in ("GO-build", "G1"):
    g_t = [t["id"] for t in T if g in (t.get("gates") or [])]
    gates[g] = {"done": sum(status(i) == "done" for i in g_t), "total": len(g_t),
                "eta": max((fin[i] for i in g_t), default=NOW).strftime("%a %H:%M") if g_t else "",
                "path": chain(max(g_t, key=lambda i: fin[i])) if [i for i in g_t if status(i) != "done"] else []}


# ---------- impact of each open task: what it unblocks, whether it is critical, how far it can slip ----------
jb = js(os.path.join(W1, "jb_briefs.json"), {}) or {}  # plain-English briefs for JB's asks (written by sessions)
BRIEFS = jb.get("briefs") if isinstance(jb.get("briefs"), dict) else {}
crit_set = set(crit) | set(gates["GO-build"]["path"])
def open_kids(i): return [k for k in kids.get(i, []) if k in BY and status(k) != "done"]
WEEK_FIN = max(fin.values(), default=NOW)
GATE_ETA = {g: max((fin[i] for i in BY if g in (BY[i].get("gates") or [])), default=WEEK_FIN) for g in ("GO-build", "G1")}
# Latest-finish rule (a backward pass over the projected schedule; durations are est()):
#   Target = what the critical path is measured against: the projected week finish (the latest projected finish,
#   where chain() starts), lowered to the projected gate finish for a task tagged with that gate (GO-build, G1).
#   LF(i) = min(target(i), min over open dependents k of LS(k)),  LS(k) = beg[k] + slack(k)
#           i.e. i may finish as late as each dependent can start late without moving the week finish or the gate.
#           Waits for a free lane or for 08:00 already in the projection count as slack.
#   slack_min  = LF(i) - fin[i] in minutes (0 on the critical chain and the GO-build chain by construction).
#   latest_iso = LF(i): "becomes critical at"; past that, each minute late moves the week finish / gate.
#   delay_min  = minutes i can slip before its nearest open dependent's projected start moves (free float).
LFm = {}
def latest(i, stack=()):
    if i in LFm: return LFm[i]
    if i in stack: return fin[i]  # dependency cycle guard
    cap = WEEK_FIN
    for g in (BY[i].get("gates") or []):
        if g in GATE_ETA: cap = min(cap, GATE_ETA[g])
    for k in open_kids(i):
        cap = min(cap, beg[k] + max(dt.timedelta(0), latest(k, stack + (i,)) - fin[k]))
    LFm[i] = max(cap, fin[i])
    return LFm[i]
# holding_now: this task is the only thing an otherwise-ready dependent (other deps done, planned day reached) waits for,
# and nobody is moving it on schedule: it is blocked, or a ready person/coordinator task (JB, Cowork, coordinator) not
# started, or ready since an earlier day and not started, or running past 1.5x its estimate.
def stuck(i):
    st = status(i); t = BY[i]
    if st == "blocked": return True
    if st == "todo" and all(status(d) == "done" for d in t.get("depends", []) if d in BY):
        return t["owner"] in ("JB", "COWORK", "COORD") or t["day"] < TODAY
    if st == "doing" and started(i): return (NOW - started(i)).total_seconds() / 60 > 1.5 * est(t)
    return False
def impact(i):
    if status(i) == "done": return None
    lf = latest(i); sl = max(0, int((lf - fin[i]).total_seconds() // 60))
    ks = open_kids(i)
    nk = min(ks, key=lambda k: beg[k]) if ks else None
    hold = [k for k in ks if status(k) == "todo" and BY[k]["day"] <= TODAY and all(status(d) == "done" for d in BY[k].get("depends", []) if d != i and d in BY)] if stuck(i) else []
    return {"unblocks": NDESC.get(i, 0),
            "next": [{"id": k, "title": clean(BY[k]["title"], 120), "beg_iso": beg[k].isoformat(), "fin_iso": fin[k].isoformat()} for k in sorted(ks, key=lambda k: beg[k])][:6],
            "critical": i in crit_set, "slack_min": sl, "latest_iso": lf.isoformat(), "holding_now": bool(hold), "holding": hold,
            "delay_id": nk, "delay_min": max(0, int((beg[nk] - fin[i]).total_seconds() // 60)) if nk else None,
            "red_iso": red_at(i, lf).isoformat()}
# red_at: when a critical task stops being "attention" and becomes a red alert (JB 26 Oct 15:06).
#   running: the earlier of its latest finish and the end of its estimate (on the chain, every minute past it moves the finish);
#   not started: its latest start (latest finish - estimate), but never sooner than 30 min after it became ready, so the
#   coordinator has a moment to start a lane that has just been freed.
def red_at(i, lf):
    t = BY[i]; e = dt.timedelta(minutes=est(t))
    if status(i) == "doing" and started(i): return min(lf, started(i) + e)
    ls = lf - e
    if all(status(d) == "done" for d in t.get("depends", []) if d in BY):
        rdy = max([fin[d] for d in t.get("depends", []) if d in fin] + [dt.datetime.strptime(t["day"], "%Y-%m-%d").replace(hour=8)])
        ls = max(ls, rdy + dt.timedelta(minutes=30))
    return ls
IMPACT = {t["id"]: impact(t["id"]) for t in T}

# ---------- RAG ----------
STALE_MIN = 20  # a lane whose last headless run ended this long ago, with no live process, is stopped, not running
def rag(t):
    tid = t["id"]; st = status(tid); s = state.get(tid) or {}; n = notes.get(tid) or {}
    deps_open = [d for d in t.get("depends", []) if status(d) != "done"]
    ln = lanes.get(tid); pr = pr_for(tid); prs_ = PR.get(pr) if pr else None
    blockfile = os.path.join(EV, "lane-%s-blocked.md" % tid)
    if st == "done":
        r = ("green", "Done %s" % hm(ts(s.get("updated"))), "")
        if "forced" in (s.get("note") or ""): r = ("green", "Done, closed with --force (model rail skipped)", "Coordinator: record the reason in the CHANGELOG")
    elif st == "blocked" or os.path.exists(blockfile):
        r = ("red", "Blocked: " + clean(s.get("note") or rd(blockfile, 300), 200), "Coordinator: clear the cause, then re-run or re-open the lane")
    elif ln and ln["denied"] and st == "doing" and (not started(tid) or ln["mt"] >= started(tid)) and tid not in running_lanes:
        r = ("red", "Lane stopped: the sandbox refused %d command(s): %s" % (len(ln["denied"]), "; ".join(ln["denied"])[:160]),
             "Coordinator: allow that command in the lane settings (or run the step itself), then resume the lane session")
    elif st == "doing" and prs_ and prs_.get("state") == "MERGED":
        r = ("amber", "PR #%d merged %s; task not closed" % (pr, hm(ts(prs_.get("mergedAt")) and ts(prs_["mergedAt"]) + dt.timedelta(hours=0))),
             "Coordinator: python3 scripts/lc_state.py done %s" % tid)
    elif st == "doing" and ln and tid not in running_lanes and started(tid) and ln["mt"] >= started(tid) and NOW - ln["mt"] > dt.timedelta(minutes=STALE_MIN):
        r = ("red", "Last run ended %s; Claude has not restarted it" % ln["mt"].strftime("%H:%M" if ln["mt"].date() == NOW.date() else "%a %H:%M"),
             "Coordinator: read the lane result, then resume the lane or close the task")
    elif st == "doing":
        b = started(tid); el = (NOW - b).total_seconds() / 60 if b else 0; e = est(t)
        if el > 2 * e: r = ("red", "Running %d min, twice the %d min estimate" % (el, e), "Coordinator: read the lane log and status file; escalate or re-brief per the stall triggers")
        elif el > 1.5 * e: r = ("amber", "Running %d min, past 1.5x the %d min estimate" % (el, e), "Coordinator: check the lane at its next message")
        else: r = ("green", "On time: %d of %d min" % (el, e), "")
    elif deps_open:
        late = t["day"] < TODAY
        r = ("amber" if late else "grey", "Waiting on %s" % ", ".join(deps_open[:4]) + (" (planned %s)" % t["day"][5:] if late else ""),
             "Follows its dependencies" if not late else "Behind plan: unblock " + deps_open[0])
    else:
        if t["owner"] == "JB": r = ("amber", "Ready: needs JB", "JB: see the steps")
        elif t["day"] < TODAY: r = ("red", "Ready since %s, not started" % t["day"][5:], "Coordinator: start it now (lane.sh / autorun)")
        elif t["day"] == TODAY: r = ("blue", "Ready to start", "Coordinator starts it (lc_state.py next --today)")
        else: r = ("grey", "Ready (planned %s)" % t["day"][5:], "")
    if st != "done" and r[0] in ("grey", "blue") and fin.get(tid) and fin[tid].date().isoformat() > t["day"]:
        r = ("amber", r[1] + "; projected to finish %s, planned %s" % (fin[tid].strftime("%a %H:%M"), t["day"][5:]), r[2] or "Coordinator: pull the chain forward or re-plan")
    if t["owner"] == "JB" and r[1] == "Ready: needs JB" and not BRIEFS.get(tid):
        r = ("amber", "Brief missing", "Coordinator: write the plain-English ask for JB in jb_briefs.json")
    if n.get("rag"): r = (n["rag"], r[1], r[2])
    if n.get("why"): r = (r[0], n["why"], r[2])  # coordinator override when the computed reason is out of date
    im = IMPACT.get(tid)
    # JB 26 Oct 15:06: a task that reaches a critical-path point is never amber; it is red.
    if st != "done" and im and im["critical"]:
        if im["holding_now"]:
            r = ("red", "Critical path: holding up %s now" % ", ".join(im["holding"][:3]) + ("; " + r[1] if r[1] not in ("Ready: needs JB",) else ""), r[2])
        elif NOW >= dt.datetime.fromisoformat(im["red_iso"]):
            r = ("red", "Critical path: slack used up at %s; %s" % (dt.datetime.fromisoformat(im["red_iso"]).strftime("%a %H:%M"), r[1]), r[2] or "Coordinator: pull the chain forward or re-plan")
    return {"rag": r[0], "why": r[1], "plan": n.get("plan") or r[2], "note": n.get("note", "")}

# who acts next, in plain words: JB, or Claude (coordinator / a lane / a script / the test queue) and when it is next looked at
WHO = {"COORD": "coordinator", "LANE": "a lane", "SCRIPT": "a script", "QUEUE": "the test queue", "COWORK": "Cowork"}
def who_line(t, R):
    tid = t["id"]; st = status(tid)
    own = (notes.get(tid) or {}).get("owner_now") or t["owner"]  # coordinator override: Claude has taken a JB task over
    if own == "JB": return {"kind": "JB"}
    who = "coordinator" if own == "Claude" else WHO.get(own, own)
    if t["owner"] == "LANE" and slot.get(tid, "").startswith("LANE "): who = "lane " + slot[tid].split(" ")[1]
    doing = R["plan"] or (state.get(tid) or {}).get("note") or R["note"]
    doing = re.sub(r"^(Coordinator|Claude)\s*:\s*", "", clean(doing, 300))
    if re.match(r"(?i)^coordinator\b", R["plan"] or ""): who = "coordinator"
    owes = False
    if not doing:
        if R["rag"] in ("green", "grey", "blue"): doing = R["why"]  # plain state: on time / waiting on / ready
        else: owes = True
    if R["rag"] in ("red", "amber"): nxt = NOW + dt.timedelta(minutes=10)  # an alert is re-checked at the next publish
    elif st == "doing" and started(tid) and started(tid) + dt.timedelta(minutes=1.5 * est(t)) > NOW: nxt = started(tid) + dt.timedelta(minutes=1.5 * est(t))
    elif st == "todo" and beg.get(tid) and beg[tid] > NOW: nxt = beg[tid]
    else: nxt = NOW + dt.timedelta(minutes=10)  # the control room re-checks it at the next publish
    return {"kind": "Claude", "who": who, "doing": doing, "owes": owes, "next_check_iso": nxt.isoformat()}

tasks = []
for t in T:
    tid = t["id"]; s = state.get(tid) or {}; R = rag(t); pr = pr_for(tid)
    tasks.append({**{k: t.get(k) for k in ("id", "day", "title", "owner", "model", "effort", "depends", "steps", "done_check", "evidence", "where", "gates", "why_opus", "review_of", "manual_start")},
                  "status": status(tid), "updated": s.get("updated", ""), "state_note": clean(s.get("note"), 300),
                  "opened": s.get("opened", []), "escalations": s.get("escalations", []),
                  "est": est(t), "start": hm(beg.get(tid)), "start_day": beg[tid].date().isoformat() if beg.get(tid) else "",
                  "finish": hm(fin.get(tid)), "finish_day": fin[tid].date().isoformat() if fin.get(tid) else "",
                  "pr": pr, "pr_state": (PR.get(pr) or {}).get("state") if pr else None, "lane": {k: v for k, v in (lanes.get(tid) or {}).items() if k != "mt"} or None,
                  "decision": t.get("decision"), "slot": slot.get(tid, ""), "beg_iso": beg[tid].isoformat() if beg.get(tid) else "", "fin_iso": fin[tid].isoformat() if fin.get(tid) else "",
                  "ndesc": NDESC.get(tid, 0), "slip_days": max(0, (fin[tid].date() - dt.date.fromisoformat(t["day"])).days) if fin.get(tid) and status(tid) != "done" else 0,
                  "lane_running": tid in running_lanes, "impact": IMPACT.get(tid), "act": who_line(t, R), "brief_missing": t["owner"] == "JB" and not (notes.get(tid) or {}).get("owner_now") and status(tid) != "done" and not BRIEFS.get(tid), "ev": evidence_excerpt(tid) if status(tid) != "todo" or tid in lanes else [], **R})

for x in tasks:  # coordinator took a JB task over (notes owner_now): the page shows Claude as the owner, never "You"
    if (notes.get(x["id"]) or {}).get("owner_now") and x["owner"] == "JB": x["plan_owner"] = "JB"; x["owner"] = "COORD"

# ---------- queue, usage, facts ----------
queue = []
for p in sorted(glob.glob(os.path.join(NOTES, "testq/results/*.txt")), key=os.path.getmtime, reverse=True)[:10]:
    kv = dict(re.findall(r"(\w+)=(\S+)", rd(p, 2000)))
    queue.append({"job": kv.get("job", os.path.basename(p))[-22:], "lane": kv.get("lane", ""), "branch": kv.get("branch", ""),
                  "sha": kv.get("sha", "")[:8], "targets": kv.get("targets", ""), "overall": kv.get("overall", "?"),
                  "dur": kv.get("duration_s", ""), "at": hm(mtime(p))})
pending = {k: len(glob.glob(os.path.join(NOTES, "testq", k, "*"))) for k in ("pending", "running", "held")}
usage = []
for line in rd(os.path.join(EV, "usage_pct.csv")).splitlines():
    f = line.split(",")
    if len(f) >= 2 and re.match(r"\d{4}-", f[0]):
        try: usage.append({"at": f[0][:16], "week": float(f[1]), "code": float(f[2]) if len(f) > 2 else None, "cowork": float(f[3]) if len(f) > 3 else None})
        except ValueError: pass
main = sh(["git", "log", "origin/main", "-1", "--format=%h|%cd|%s", "--date=format:%a %H:%M"], cwd=REPO).strip().split("|", 2) if os.path.isdir(REPO) else []
coord = js(os.path.join(NOTES, "COORDINATOR.json"), {}) or {}
alerts = [clean(l, 260) for l in rd(os.path.join(NOTES, "ALERTS.txt")).splitlines()[-3:] if l.strip()]
lane_cost = round(sum(l["cost"] for l in lanes.values()), 2)
starts = [l["start_ctx"] for l in lanes.values() if l["start_ctx"]]
facts = {"main": main, "coordinator": coord.get("name", ""), "coord_set": (coord.get("set") or "")[:16].replace("T", " "),
         "open_prs": [{"n": p["number"], "t": clean(p["title"], 90), "b": p.get("headRefName", ""), "url": p.get("url", "")} for p in prs if p.get("state") == "OPEN"],
         "merged_24h": [{"n": p["number"], "t": clean(p["title"], 90), "at": hm(ts(p.get("mergedAt")))} for p in prs if p.get("state") == "MERGED" and ts(p.get("mergedAt")) and NOW - ts(p["mergedAt"]) < dt.timedelta(hours=24)],
         "alerts": alerts, "pending": pending, "lane_cost": lane_cost, "lane_start_ctx": min(starts) if starts else None,
         "gh_age_min": int((time.time() - os.path.getmtime(ghc)) / 60) if os.path.exists(ghc) else None}

# ---------- tokens (incremental index of transcripts; see w1_tokens.py) ----------
lane_sessions = {}
for p in glob.glob(os.path.join(EV, "lane-W1-*.json")):
    d = js(p, {}) or {}
    if d.get("session_id"):
        lane_sessions[d["session_id"]] = (os.path.basename(p)[5:-5], ",".join((d.get("modelUsage") or {}).keys()))
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import w1_tokens
    tok = w1_tokens.run(W1, lane_sessions, float(arg("--budget", "60")))
except Exception as e:  # the page must still build
    tok = {"sessions": [], "buckets": [], "models": [], "timelines": {}, "error": str(e)[:200]}

# ---------- decisions waiting on JB (gen/decisions_sync.py records the answers) ----------
decisions = []
for p in glob.glob(os.path.join(W1, "decisions", "D-*.json")):
    d = js(p)
    if not isinstance(d, dict) or not d.get("id"): continue
    has_run = bool(d.get("run"))
    d = {k: d.get(k) for k in ("id", "created", "title", "situation", "why_now", "options", "recommend", "because", "tasks", "deadline", "status", "decided", "notified", "links", "acted", "thread", "question_pending", "programme")}
    d["has_run"] = has_run  # the commands themselves never leave the Mac
    for k in ("title", "situation", "why_now", "because"): d[k] = clean(d.get(k) or "", 4000)
    d["thread"] = [{"from": m.get("from", ""), "at": m.get("at", ""), "text": clean(m.get("text", ""), 3000)} for m in (d.get("thread") or []) if isinstance(m, dict)]
    d["status"] = d.get("status") or "open"
    decisions.append(d)
decisions.sort(key=lambda d: (d["status"] != "open", d.get("deadline") or "9", d["id"]))
ntfy = rd(os.path.join(HOME, ".config", "lc-control-room", "ntfy_topic")).strip()

# ---------- V2: the Roadmap to Beta (ROADMAP_TO_BETA.html, edited by the V2 coordinator with roadmap.py) ----------
import html as _html
def _txt(x): return _html.unescape(re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", x or ""))).strip()
def parse_roadmap(path):
    h = rd(path)
    if not h: return None
    out = {"age": age_min(path) if "age_min" in globals() else None}
    m = re.search(r'<p id="owner">(.*?)</p>', h, re.S); out["owner"] = clean(_txt(m.group(1)) if m else "", 300)
    m = re.search(r'<p id="sitrep">(.*?)</p>', h, re.S); out["sitrep"] = clean(_txt(m.group(1)) if m else "", 1500)
    m = re.search(r'<span id="asof">(.*?)</span>', h, re.S); out["asof"] = _txt(m.group(1)) if m else ""
    m = re.search(r'<!--STALE--><div class="stale">(.*?)</div>', h, re.S); out["stale"] = clean(_txt(m.group(1)) if m else "", 400)
    m = re.search(r'<span id="need">(.*?)</span></p>', h, re.S); out["need"] = clean(_txt(m.group(1)) if m else "", 1200)
    steps = []
    for row in re.findall(r'<tr><td>([A-Z])</td><td>(.*?)</td><td id="what-[A-Z]">(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>', h, re.S):
        sid, title, what, start, dur = row
        items = [{"state": "done" if cls == "done" else "now", "text": clean(_txt(t), 400)} for cls, t in re.findall(r'<span class="(done|now)">(.*?)</span>', what, re.S)]
        has_now = 'class="now"' in what; all_done = items and all(i["state"] == "done" for i in items) and not _txt(re.sub(r'(?s)<span class="done">.*?</span>', "", what))
        st = "done" if all_done else "now" if has_now else "todo"
        m_e = re.search(r'data-eta="(\d{4}-\d\d-\d\dT\d\d:\d\d)', dur)
        steps.append({"id": sid, "title": clean(_txt(title), 120), "state": st, "what": clean(_txt(what), 900), "items": items[-6:], "eta": m_e.group(1) if m_e else "",
                      "n_now": len([1 for i in items if i["state"] == "now"]) + (1 if st == "now" and not items else 0),
                      "start": clean(_txt(start), 60), "dur": clean(_txt(re.sub(r"(?s)<time.*?</time>", "", dur)), 120)})
    # the step the SITREP names as current wins
    m = re.search(r"Current step:\s*<b>([A-Z])</b>", h)
    out["current"] = m.group(1) if m else next((x["id"] for x in steps if x["state"] == "now"), "")
    for x in steps:
        if x["id"] == out["current"] and x["state"] == "todo": x["state"] = "now"
    out["steps"] = steps
    out["live"] = [{"at": a_, "ts": t0, "text": clean(_txt(t), 300)} for t0, a_, t in re.findall(r'<li data-ts="([^"]*)"><b>(.*?)</b>(.*?)</li>', h, re.S)][:40]
    return out
v2 = parse_roadmap(os.path.join(NOTES, "ROADMAP_TO_BETA.html"))

# ---------- programme of each item: V3 (the W1 plan), V2 (Roadmap to Beta), Both (main) ----------
v3_branches, v3_prs = set(), set()
for line in sh(["git", "worktree", "list", "--porcelain"], cwd=REPO).split("\n\n") if os.path.isdir(REPO) else []:
    if "/.worktrees/w1-" in line or "/.worktrees/v3-" in line:
        m = re.search(r"^branch refs/heads/(.+)$", line, re.M)
        if m: v3_branches.add(m.group(1))
for f in glob.glob(os.path.join(EV, "*_pr.txt")):
    t_ = rd(f)
    m = re.search(r"^pr=(\d+)", t_, re.M)
    if m: v3_prs.add(int(m.group(1)))
    m = re.search(r"^branch=(\S+)", t_, re.M)
    if m: v3_branches.add(m.group(1))
V3_WORDS = re.compile(r"\b(v3|V3|W1-\d+|W1 )", re.I)
def prog_branch(b):
    if not b: return "V2"
    if b in ("main",) or b.startswith("train/"): return "Both"
    return "V3" if (b in v3_branches or b.startswith(("v3", "docs/w1", "w1-"))) else "V2"
for j in queue: j["prog"] = prog_branch(j.get("branch", ""))
for p_ in prs: p_["prog"] = "V3" if (p_.get("number") in v3_prs or V3_WORDS.search(p_.get("title", "")) or p_.get("headRefName", "") in v3_branches) else "V2"
facts["open_prs"] = [{"n": p["number"], "t": clean(p["title"], 90), "b": p.get("headRefName", ""), "url": p.get("url", ""), "prog": p["prog"]} for p in prs if p.get("state") == "OPEN"]
facts["merged_24h"] = [{"n": p["number"], "t": clean(p["title"], 90), "at": hm(ts(p.get("mergedAt"))), "prog": p["prog"]} for p in prs if p.get("state") == "MERGED" and ts(p.get("mergedAt")) and NOW - ts(p["mergedAt"]) < dt.timedelta(hours=24)]
K_COORD = re.compile(r"\bK\d{1,3}\b")
for s_ in tok.get("sessions", []):
    t_ = s_.get("title", "")
    if s_.get("task") or V3_WORDS.search(t_) or t_.startswith(("lane W1", "Q D-")): s_["prog"] = "V3"
    elif s_.get("project") == "London Commuter": s_["prog"] = "V2"
    else: s_["prog"] = "Other"
m_ = re.search(r"✱\s*(K\d{1,3})", (v2 or {}).get("owner", ""))
if m_: facts["coordinator"] = m_.group(1)  # the roadmap's Owner line is newer than COORDINATOR.json
if v2: v2["sitrep"] = re.sub(r"\s*Current step:\s*[A-Z]\s*\([^)]*\)\.?", "", v2["sitrep"]).strip()

# ---------- session fuel: context per active session vs the rule-2 thresholds (mac/fuel_guard.py state, copied by push.sh) ----------
FUEL_IN = arg("--fuel", os.path.join(W1, "control-room", "fuel"))
NOW_TS = NOW.timestamp() if "--now" in A else time.time()
# same table as mac/fuel_guard.py: lean Sonnet lanes, Haiku lanes, desktop/non-lean sessions, the coordinator
FUEL_TH = {"coordinator": (250000, 275000, 300000), "lean": (175000, 200000, 225000), "haiku": (80000, 90000, 95000), "desktop": (225000, 250000, 275000)}
FROLE = {"coordinator": "coordinator", "lean": "lean lane", "haiku": "Haiku lane", "desktop": "desktop session"}
FWORDS = ["fine", "start hand-over", "hand-over due", "STOP"]
cps = js(os.path.join(FUEL_IN, "compact_points.json"), {}) or {}
def fuel_role(role, model):
    if role in FUEL_TH: return role
    return "haiku" if "haiku" in (model or "") else "desktop"   # older state files said "lane"
def fuel_th(role, model):
    base = FUEL_TH[fuel_role(role, model)]
    low = cps.get(model) if isinstance(cps, dict) else None
    if isinstance(low, (int, float)) and low > 0: base = tuple(min(b, int(low * k)) for b, k in zip(base, (0.80, 0.88, 0.94)))
    return list(base)
TKS = {s_.get("sid"): s_ for s_ in tok.get("sessions", [])}
fevents = []
for ln in rd(os.path.join(FUEL_IN, "events.jsonl")).splitlines()[-200:]:
    try: e_ = json.loads(ln)
    except ValueError: continue
    if isinstance(e_, dict) and e_.get("sid"): fevents.append(e_)
def fuel_row(sid, ctx, role, model, at, cwd, src):
    t_ = TKS.get(sid) or {}
    role = fuel_role(role, model)
    th = fuel_th(role, model)
    band = 3 if ctx >= th[2] else 2 if ctx >= th[1] else 1 if ctx >= th[0] else 0
    title = (t_.get("title") or "").split(" | ")[0] or os.path.basename(cwd or "") or sid[:8]
    prog = t_.get("prog") or ("V3" if V3_WORDS.search(title) else "V2" if "London Commuter" in (cwd or "") or "side-session-notes" in (cwd or "") else "Other")
    comp = [{"at": e_.get("at"), "ctx": e_.get("ctx"), "model": e_.get("model", "")} for e_ in fevents if e_.get("sid") == sid and (e_.get("band") == -1 or e_.get("note") == "compacted")][-3:]
    return {"sid": sid, "id": sid[:8], "title": clean(title, 120), "role": role, "role_txt": FROLE[role], "prog": prog, "model": model or "", "ctx": int(ctx or 0), "th": th,
            "band": band, "words": FWORDS[band], "at": at, "age_min": max(0, int((NOW_TS - (at or 0)) / 60)), "src": src, "compactions": comp}
fuel_rows = {}
for p_ in glob.glob(os.path.join(FUEL_IN, "state", "*.json")):
    st_ = js(p_, {}) or {}
    sid = os.path.basename(p_)[:-5]
    if not isinstance(st_, dict) or not isinstance(st_.get("at"), (int, float)) or NOW_TS - st_["at"] > 1800: continue
    role = "coordinator" if (st_.get("role") == "coordinator" or sid == coord.get("cli_uuid")) else str(st_.get("role") or "lane")
    fuel_rows[sid] = fuel_row(sid, st_.get("ctx") or 0, role, st_.get("model") or "", int(st_["at"]), st_.get("cwd") or "", "hook")
for s_ in tok.get("sessions", []):
    sid = s_.get("sid") or ""
    if not sid or sid in fuel_rows or not isinstance(s_.get("last"), (int, float)) or NOW_TS - s_["last"] > 1800: continue
    role = "coordinator" if (sid == coord.get("cli_uuid") or s_.get("kind") == "coordinator") else "desktop"
    fuel_rows[sid] = fuel_row(sid, s_.get("last_ctx") or 0, role, (s_.get("models") or [""])[-1], int(s_["last"]), "", "index")
fuel = {"sessions": sorted(fuel_rows.values(), key=lambda r: (-r["band"], r["role"] != "coordinator", -r["ctx"])),
        "events": [{"at": e_.get("at"), "id": str(e_.get("sid", ""))[:8], "title": clean(((TKS.get(e_.get("sid")) or {}).get("title") or e_.get("cwd") or "").split(" | ")[0], 80),
                    "ctx": e_.get("ctx"), "band": e_.get("band"), "note": clean(str(e_.get("note", "")), 40), "model": e_.get("model", "")} for e_ in fevents[-12:]][::-1],
        "compact_points": cps if isinstance(cps, dict) else {}}

# ---------- chat threads (gen/chat_sync.py writes them; replies come from short read-only headless runs) ----------
CHAT_DIR = os.path.join(W1, "chat")
threads = {}
for p_ in glob.glob(os.path.join(CHAT_DIR, "*.json")):
    t_ = os.path.basename(p_)[:-5]
    if not re.match(r"^(V3|V2|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})$", t_): continue
    d_ = js(p_, {}) or {}
    threads[t_] = [{"id": str(m.get("id", ""))[:40], "re": str(m.get("re", ""))[:40], "fu": str(m.get("fu", ""))[:40], "from": "JB" if m.get("from") == "JB" else "Claude",
                    "acting": str(m.get("from", "")) == "Claude (acting)",
                    "act": ({"state": str(m["act"].get("state", ""))[:12], "lane": str(m["act"].get("lane", ""))[:12], "behind": str(m["act"].get("behind", ""))[:12]} if isinstance(m.get("act"), dict) else None), "at": str(m.get("at", ""))[:19],
                    "text": clean(str(m.get("text", "")), 6000), "depth": m.get("depth", ""), "model": str(m.get("model", ""))[:60], "pending": bool(m.get("pending")),
                    "error": bool(m.get("error")), "start": m.get("start_tokens") if isinstance(m.get("start_tokens"), int) else None} for m in (d_.get("msgs") or [])[-40:] if isinstance(m, dict)]
lanes_ = {}
for p_ in glob.glob(os.path.join(NOTES, "*-status.md")) + glob.glob(os.path.join(NOTES, "status", "*.md")):
    m = re.match(r"^([A-Z][A-Za-z0-9]{0,5}(?:-[A-Z][A-Za-z0-9]{0,4})?)-", os.path.basename(p_))
    if not m or m.group(1).startswith("JB") or re.match(r"^K\d+$", m.group(1)): continue
    a_ = os.path.getmtime(p_)
    if NOW_TS - a_ < 12 * 3600: lanes_[m.group(1)] = max(lanes_.get(m.group(1), 0), a_)
chat = {"threads": threads, "lanes": [{"id": k, "age_min": max(0, int((NOW_TS - v) / 60))} for k, v in sorted(lanes_.items(), key=lambda kv: -kv[1])][:16], "limit": 20}

# ---------- V2 mirror KPIs: the same tiles as V3, read from ROADMAP_TO_BETA.html, V2 status files, PRs, the queue, tokens ----------
def _eta(x):
    try: return dt.datetime.strptime(x, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError): return None
def _when(d): return d.strftime("%a %H:%M") if d else ""
def _proj(d): return ("overdue (planned %s)" % _when(d)) if d and d < NOW else _when(d)   # never show a past time as a projection
def v2_kpis(V):
    if not V: return None
    S = V.get("steps") or []
    for x in S:
        if x["state"] == "now" and not x.get("n_now"): x["n_now"] = 1
        e = _eta(x.get("eta"))
        if x["state"] == "done": x["rag"], x["why"] = "green", "Done."
        elif x["state"] == "now" and not e: x["rag"], x["why"] = "amber", "In progress, but the roadmap gives no finish time for it."
        elif x["state"] == "now" and e < NOW: x["rag"], x["why"] = "red", "Running past its planned finish (%s) by %s." % (_when(e), _dur(NOW - e))
        elif x["state"] == "now": x["rag"], x["why"] = "green", "In progress; planned to finish %s." % _when(e)
        else: x["rag"], x["why"] = "grey", "Not started; follows the current step."
        x["eta_txt"] = _when(e) if e else ""
    open_ = [x for x in S if x["state"] != "done"]
    def gate(prefix):
        G = [x for x in S if x["title"].startswith(prefix)]
        if not G: return {"done": 0, "total": 0, "eta": "", "why": "no estimate: no %s steps in the roadmap table" % prefix, "rag": "grey", "steps": []}
        op = [x for x in G if x["state"] != "done"]
        es = [_eta(x["eta"]) for x in op]
        if not op: eta, why, rag = "done", "All %d %s steps are done." % (len(G), prefix), "green"
        elif all(es): eta, why = _proj(max(es)), "Latest planned finish of the open %s steps (%s)." % (prefix, ", ".join(x["id"] for x in op))
        else: eta, why = "", "no estimate: step %s has no finish time in the roadmap" % ", ".join(x["id"] for x, e in zip(op, es) if not e)
        if op: rag = "red" if any(x.get("rag") == "red" for x in op) else "amber" if not all(es) else "green"
        return {"done": len(G) - len(op), "total": len(G), "eta": eta, "why": why, "rag": rag, "steps": [x["id"] for x in G]}
    gates_ = {g: gate(g) for g in ("L0", "L1", "L2")}
    es = [_eta(x["eta"]) for x in open_]
    if not open_: end, end_why = "done", "Every step is done."
    elif all(es): end, end_why = _proj(max(es)), "Latest planned finish among the open steps (%s)." % ", ".join(x["id"] for x in open_)
    else: end, end_why = "", "no estimate: step %s has no finish time in the roadmap" % ", ".join(x["id"] for x, e in zip(open_, es) if not e)
    plan_rag = "red" if any(x["rag"] == "red" for x in open_) else "amber" if not end else "green"
    live_today = [l for l in V.get("live") or [] if str(l.get("ts", "")).startswith(TODAY)]
    n_open = sum(x.get("n_now", 0) for x in open_ if x["state"] == "now")
    cur = next((x for x in S if x["id"] == V.get("current")), None)
    ce = _eta(cur["eta"]) if cur else None
    today = {"done": len(live_today), "total": len(live_today) + n_open, "eta": _proj(ce) if ce else "",
             "why": ("Done = the coordinator's live updates dated today; open = items still marked 'now' in the current step(s). Finish = the current step's planned finish."
                     if ce else "no estimate: the current step %s has no finish time in the roadmap" % (V.get("current") or "?")),
             "rag": cur["rag"] if cur else "grey"}
    sess = [s_ for s_ in tok.get("sessions", []) if s_.get("prog") == "V2"]
    idx = {i for i, s_ in enumerate(tok.get("sessions", [])) if s_.get("prog") == "V2"}
    d0 = dt.datetime.combine(NOW.date(), dt.time()).timestamp()
    tt = sum(b[3] + b[4] + b[5] + b[6] for b in tok.get("buckets", []) if b[0] in idx and b[1] >= d0)
    live_n = sum(1 for s_ in sess if isinstance(s_.get("last"), (int, float)) and NOW_TS - s_["last"] < 900)
    prs_ = [p for p in facts.get("open_prs", []) if p.get("prog") == "V2"]
    trains = [j for j in queue if str(j.get("branch", "")).startswith("train/")]
    tr = trains[0] if trains else None
    train = {"text": ("%s %s at %s" % (tr["branch"], tr["overall"], tr["at"])) if tr else "no train result in the last ten queue results",
             "rag": ("red" if tr and tr["overall"] == "FAIL" else "green") if tr else "grey"}
    need_txt = V.get("need") or ""
    need_items = [x for x in re.split(r";\s+|\.\s+(?=[A-Z])", need_txt) if x.strip() and not re.match(r"(?i)^(none|nothing)\b", x.strip())]
    stale = V.get("age") is not None and V["age"] > 180
    return {"plan": {"done": len(S) - len(open_), "total": len(S), "eta": end, "why": end_why, "rag": plan_rag},
            "today": today, "gates": gates_,
            "tokens": {"today": tt, "live": live_n, "sessions": len(sess), "rag": "amber" if live_n > 3 else "green",
                       "why": "Tokens today across sessions classed V2 (London Commuter project, not V3 work). Amber when more than 3 V2 sessions are live."},
            "prs": {"open": len(prs_), "list": prs_[:8], "train": train, "rag": "red" if train["rag"] == "red" else "amber" if len(prs_) > 6 else "green",
                    "why": "Open V2 pull requests and the newest merge-train result in the test queue. Red when the newest train failed; amber with more than 6 open PRs."},
            "need": {"n": len(need_items), "items": [clean(x, 300) for x in need_items][:8], "rag": "amber" if need_items else "green",
                     "why": "Items in the roadmap's NEED line (what only JB can do). Amber while any are open."},
            "stale": stale, "stale_why": "The roadmap file is %s min old: the V2 coordinator has not updated it for over 3 hours." % V.get("age") if stale else "",
            "live_today": live_today[:20]}
def _dur(d):
    m = int(d.total_seconds() // 60)
    return "%d min" % m if m < 90 else "%d h" % round(m / 60) if m < 2880 else "%d days" % round(m / 1440)
v2k = v2_kpis(v2)

# ---------- data health: every source, its age and whether to trust it ----------
def age_min(p):
    try: return int((time.time() - os.path.getmtime(p)) / 60)
    except OSError: return None
def newest(pattern):
    fs = glob.glob(pattern)
    return max(fs, key=os.path.getmtime) if fs else None
qnew = newest(os.path.join(NOTES, "testq/results/*.txt")); evnew = newest(os.path.join(EV, "*"))
src_sha = sh(["git", "rev-parse", "--short", "origin/main"], cwd=REPO).strip() if os.path.isdir(REPO) else ""
last_usage_h = None
if usage:
    lu = ts(usage[-1]["at"]); last_usage_h = round((NOW - lu).total_seconds() / 3600, 1) if lu else None
fetch_age = age_min(os.path.join(REPO, ".git/FETCH_HEAD")) if os.path.isdir(REPO) else None
health = [
    {"name": "Task definitions", "detail": "%d tasks from %s" % (len(T), "origin/main " + src_sha if src_sha else "a local copy"), "age": fetch_age,
     "ok": bool(T) and (fetch_age is None or fetch_age < 180), "why": "git fetch is %s min old" % fetch_age if fetch_age is not None else ""},
    {"name": "Task state (state.json)", "detail": "%d tasks recorded by lc_state.py" % len(state), "age": age_min(os.path.join(W1, "state.json")), "ok": bool(state), "why": ""},
    {"name": "Evidence folder", "detail": os.path.basename(evnew) if evnew else "empty", "age": age_min(evnew) if evnew else None, "ok": bool(evnew), "why": ""},
    {"name": "Test queue results", "detail": "latest %s" % (os.path.basename(qnew)[:28] if qnew else "none"), "age": age_min(qnew) if qnew else None, "ok": bool(qnew), "why": ""},
    {"name": "GitHub pull requests", "detail": "%d PRs cached" % len(prs), "age": age_min(ghc), "ok": age_min(ghc) is not None and age_min(ghc) < 30, "why": "gh not reachable" if age_min(ghc) is None else ""},
    {"name": "Token ledger (transcripts)", "detail": "%s requests indexed%s" % ((tok.get("index") or {}).get("msgs", "?"), ", %s files still to read" % tok["index"]["files_left"] if (tok.get("index") or {}).get("files_left") else ""),
     "age": 0 if not tok.get("error") else None, "ok": not tok.get("error"), "why": tok.get("error", "")},
    {"name": "V2 Roadmap to Beta", "detail": ("step %s · owner %s" % (v2["current"], (v2["owner"] or "")[:60])) if v2 else "ROADMAP_TO_BETA.html not found",
     "age": age_min(os.path.join(NOTES, "ROADMAP_TO_BETA.html")), "ok": bool(v2) and not (v2 or {}).get("stale"), "why": (v2 or {}).get("stale", "")[:200]},
    {"name": "Weekly quota reading (W1-05)", "detail": ("%s%% at %s" % (usage[-1]["week"], usage[-1]["at"].replace("T", " "))) if usage else "no reading", "age": int(last_usage_h * 60) if last_usage_h is not None else None,
     "ok": last_usage_h is not None and last_usage_h < 14, "why": "older than 14 h: W1-05 is morning and evening" if last_usage_h is not None and last_usage_h >= 14 else ""},
]

# ---------- SITREP: the few sentences that answer "where are we" ----------
def short(x, n):
    x = x.strip()
    if len(x) <= n: return x
    cut = x[:n].rsplit(" ", 1)[0].rstrip(",;:(-")
    return cut + "…"
def tl(i): return "%s (%s)" % (i, short(BY[i]["title"], 70))
sit = []
reds_ = [t for t in tasks if t["rag"] == "red"]
for t in reds_:
    on = " It is on the GO-build path, so the install and the v2 tag wait for it." if t["id"] in gates["GO-build"]["path"] else (" It is on the critical path." if t["id"] in crit_set else "")
    sit.append({"k": "red", "ids": [t["id"]], "text": "%s: %s.%s Next: %s" % (tl(t["id"]), t["why"].rstrip("."), on, t["plan"] or "see the task")})
closable = [t["id"] for t in tasks if t["status"] == "doing" and "merged" in t["why"] and "not closed" in t["why"]]
if closable: sit.append({"k": "next", "ids": closable, "text": "Ready to close (PR merged, task still open): %s. Coordinator: lc_state.py done for each; this frees %s." % (", ".join(closable), "a lane" if len(closable) == 1 else "%d lanes" % len(closable))})
doing = [t["id"] for t in tasks if t["status"] in ("doing",) and t["rag"] != "red" and t["id"] not in closable]
if doing: sit.append({"k": "now", "ids": doing, "text": "In progress: " + "; ".join("%s %s" % (i, short(BY[i]["title"], 56)) for i in doing)})
nxt = sorted([t for t in tasks if t["status"] == "todo" and not t["why"].startswith("Waiting") and t["owner"] != "JB"], key=lambda t: (t["beg_iso"], -t["ndesc"]))[:4]
if nxt: sit.append({"k": "next", "ids": [t["id"] for t in nxt], "text": "Next to start: " + "; ".join("%s at %s" % (t["id"], t["start"]) for t in nxt)})
opend = [d for d in decisions if d["status"] == "open"]
if opend: sit.insert(0, {"k": "you", "ids": [], "text": "Decision%s waiting for you: %s. Approve them in Your decisions at the top of the page." % ("s" if len(opend) > 1 else "", "; ".join("%s %s" % (d["id"], short(d.get("title") or "", 60)) for d in opend))})
mine = [t for t in tasks if t["status"] != "done" and t["owner"] == "JB" and not t["why"].startswith("Waiting")]
if mine: sit.append({"k": "you", "ids": [t["id"] for t in mine], "text": "Waiting on you (%d): " % len(mine) + "; ".join("%s %s" % (t["id"], short(BY[t["id"]]["title"], 56)) for t in mine)})
slips = [t for t in tasks if t["slip_days"] > 0]
sit.append({"k": "plan", "ids": [], "text": "Today projected to finish %s. GO-build gate %s, G1 %s. %s" % (
    max((fin[i] for i in [x["id"] for x in tasks if x["day"] <= TODAY and x["status"] != "done"]), default=NOW).strftime("%a %H:%M"),
    gates["GO-build"]["eta"], gates["G1"]["eta"], ("%d tasks are projected to finish after their planned day." % len(slips)) if slips else "Every task is projected within its planned day.")})

for l in sit: l.setdefault("prog", "V3")
if v2:
    cur = next((x for x in v2["steps"] if x["id"] == v2["current"]), None)
    sit.append({"k": "now", "prog": "V2", "ids": [], "text": "Roadmap to Beta: step %s%s. %s" % (v2["current"], (" (" + cur["title"] + ")") if cur else "", short(v2["sitrep"], 420))})
    if v2.get("need"): sit.append({"k": "you", "prog": "V2", "ids": [], "text": "V2 needs you: " + short(v2["need"], 400)})
    if v2.get("stale"): sit.append({"k": "red", "prog": "V2", "ids": [], "text": "The V2 roadmap is out of date: " + short(v2["stale"], 240)})

# ---------- burn-up: planned vs done vs projected, per day ----------
days_all = sorted({t["day"] for t in T})
burn = [{"day": d, "planned": sum(t["day"] <= d for t in T),
         "done": sum(1 for t in tasks if t["status"] == "done" and (t["updated"] or "9")[:10] <= d) if d <= TODAY else None,
         "proj": sum(1 for t in T if fin.get(t["id"]) and fin[t["id"]].date().isoformat() <= d)} for d in days_all]

today_ids = [t["id"] for t in tasks if t["day"] == TODAY or (t["day"] < TODAY and t["status"] != "done") or (t["status"] == "done" and (t["updated"] or "")[:10] == TODAY)]
reds = [t for t in tasks if t["rag"] == "red"]; ambers = [t for t in tasks if t["rag"] == "amber"]
overall = "red" if reds else "amber" if ambers else "green"
# ---------- iPhone alert (JB 27 Oct 04:42): which open tasks need the phone connected, and has JB said it is free ----------
#   A task needs the phone when its `where` mentions iPhone/phone/Mirroring, its gates or title mention install or "phone check",
#   it is a QUEUE task whose targets/where/title/steps say install, or it is named in <notes>/v2/owed_phone_checks.txt (first word = task id,
#   or any text = an owed V2 check; optional "title | why | minutes"). State: red = a phone task is critical and due within 24 h
#   (projected start or red_iso within 24 h, or holding_now); amber = some phone task open; green = none. free = phone_free.json while valid.
PHONE_RE = re.compile(r"(?i)iphone|\bphone\b|mirroring")
def needs_phone(t):
    if PHONE_RE.search(t.get("where") or ""): return True
    # "install" alone is not enough (W1-49 "gitleaks ... install" is a Mac task): it must be an install on the phone
    if re.search(r"(?i)phone check|install\b[^.;]{0,40}\b(i?phone|device)|\b(i?phone|device)\b[^.;]{0,40}install", " ".join(t.get("gates") or []) + " " + (t.get("title") or "")): return True
    return t.get("owner") == "QUEUE" and bool(re.search(r"(?i)install", " ".join([str(t.get("targets") or ""), t.get("where") or "", t.get("title") or ""] + [str(x) for x in (t.get("steps") or [])])))
owed_lines = [l.strip() for l in rd(os.path.join(NOTES, "v2", "owed_phone_checks.txt")).splitlines() if l.strip() and not l.strip().startswith("#")]
owed_ids = {l.split()[0] for l in owed_lines if l.split()[0] in BY}
H24 = NOW + dt.timedelta(hours=24)
def due24(x):
    im = x.get("impact") or {}
    if not im.get("critical"): return False
    if im.get("holding_now"): return True
    return any(ts(x.get(k)) and ts(x.get(k)) <= H24 for k in ("beg_iso",)) or bool(im.get("red_iso") and ts(im["red_iso"]) and ts(im["red_iso"]) <= H24)
ph_tasks = []
for x in tasks:
    if x["status"] == "done" or not (needs_phone(BY[x["id"]]) or x["id"] in owed_ids): continue
    ph_tasks.append({"id": x["id"], "title": x["title"], "why": x["why"], "min": x["est"], "programme": "V3", "critical": bool((x.get("impact") or {}).get("critical")), "due24": due24(x)})
for n, l in enumerate(owed_lines, 1):
    if l.split()[0] in BY: continue
    f = [c.strip() for c in l.split("|")]
    ph_tasks.append({"id": "V2-P%d" % n, "title": f[0][:160], "why": f[1][:200] if len(f) > 1 else "Owed V2 phone check", "min": int(f[2]) if len(f) > 2 and f[2].isdigit() else 10, "programme": "V2", "critical": False, "due24": False})
ph_state = "red" if any(x["due24"] for x in ph_tasks) else "amber" if ph_tasks else "green"
ph_free = js(os.path.join(W1, "phone_free.json"))
if isinstance(ph_free, dict) and ts(ph_free.get("at")) and ts(ph_free.get("until")):
    at_, un_ = ts(ph_free["at"]), ts(ph_free["until"])
    ran = False   # a phone job ran after JB said it is free: the phone is in use, the note is spent
    for q in glob.glob(os.path.join(NOTES, "testq/results/*.txt")):
        if mtime(q) and mtime(q) > at_ and re.search(r"(?i)install", rd(q, 2000)): ran = True
    ph_free = {"at": ph_free["at"], "by": ph_free.get("by", "JB"), "until": ph_free["until"], "since": hm(at_), "active": at_ <= NOW < un_ and not ran}
else:
    ph_free = None
phone = {"state": ph_state, "tasks": ph_tasks, "free": ph_free}

data = {"phone": phone, "gen": NOW.strftime("%a %d %b %H:%M"), "gen_iso": NOW.isoformat(), "today": TODAY, "plan": defs.get("plan", ""), "week": defs.get("week", ""),
        "decisions": decisions, "ntfy": ntfy, "fuel": fuel, "chat": chat, "v2": v2, "v2k": v2k, "jb": jb, "tok": tok, "health": health, "sit": sit, "burn": burn, "coord_sid": coord.get("cli_uuid", ""), "now_iso": NOW.isoformat(), "pools": POOLS,
        "loop": rd(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "LOOP_LOG.md"), 6000),
        "tasks": tasks, "today_ids": today_ids, "crit": crit, "gates": gates, "queue": queue, "usage": usage, "facts": facts, "overall": overall,
        "day_finish": max((fin[i] for i in today_ids if status(i) != "done"), default=NOW).strftime("%a %H:%M"),
        "week_finish": max(fin.values(), default=NOW).strftime("%a %d %b %H:%M")}

HERE = os.path.dirname(os.path.abspath(__file__))
payload = json.dumps(data, default=str, separators=(",", ":"))
def atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: f.write(text)
    os.replace(tmp, path)
if "--json" in A: atomic(arg("--json", ""), payload)
if "--out" in A or "--json" not in A:
    TEMPLATE = open(os.path.join(HERE, "..", "index.html"), encoding="utf-8").read()
    atomic(OUT, TEMPLATE.replace("/*__DATA__*/null", payload.replace("</", "<\\/"), 1))
print("wrote", OUT, len(tasks), "tasks", overall, "red", len(reds), "amber", len(ambers))
