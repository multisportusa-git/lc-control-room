#!/usr/bin/env python3
"""London Commuter v3 Week 1 control room: one self-contained HTML page built from files on disk. 0 model tokens.
Reads: W1_TASKS.json (origin/main, else the kit worktree), w1/state.json, w1/evidence/*, testq/results/*, ALERTS.txt,
COORDINATOR.json, git origin/main, and a 10-minute gh cache. Optional coordinator overrides: w1/dashboard_notes.json
  {"W1-07": {"note": "...", "plan": "...", "rag": "amber", "est_min": 60}}
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
# Pools: 3 code lanes (CLAUDE.md max), 2 test runners, 1 coordinator, 1 Cowork, JB; scripts run at once.
# People (JB, Cowork) work 08:00-22:00. Nothing starts before its planned day at 08:00.
POOLS = {"LANE": 3, "QUEUE": 2, "COORD": 1, "COWORK": 1, "JB": 1, "SCRIPT": 99}
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

# ---------- RAG ----------
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
    if n.get("rag"): r = (n["rag"], r[1], r[2])
    return {"rag": r[0], "why": r[1], "plan": n.get("plan") or r[2], "note": n.get("note", "")}

tasks = []
for t in T:
    tid = t["id"]; s = state.get(tid) or {}; R = rag(t); pr = pr_for(tid)
    tasks.append({**{k: t.get(k) for k in ("id", "day", "title", "owner", "model", "effort", "depends", "steps", "done_check", "evidence", "where", "gates", "why_opus", "review_of", "manual_start")},
                  "status": status(tid), "updated": s.get("updated", ""), "state_note": clean(s.get("note"), 300),
                  "opened": s.get("opened", []), "escalations": s.get("escalations", []),
                  "est": est(t), "start": hm(beg.get(tid)), "start_day": beg[tid].date().isoformat() if beg.get(tid) else "",
                  "finish": hm(fin.get(tid)), "finish_day": fin[tid].date().isoformat() if fin.get(tid) else "",
                  "pr": pr, "pr_state": (PR.get(pr) or {}).get("state") if pr else None, "lane": {k: v for k, v in (lanes.get(tid) or {}).items() if k != "mt"} or None,
                  "slot": slot.get(tid, ""), "beg_iso": beg[tid].isoformat() if beg.get(tid) else "", "fin_iso": fin[tid].isoformat() if fin.get(tid) else "",
                  "ndesc": NDESC.get(tid, 0), "slip_days": max(0, (fin[tid].date() - dt.date.fromisoformat(t["day"])).days) if fin.get(tid) and status(tid) != "done" else 0,
                  "lane_running": tid in running_lanes, "ev": evidence_excerpt(tid) if status(tid) != "todo" or tid in lanes else [], **R})

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
    d = {k: d.get(k) for k in ("id", "created", "title", "situation", "why_now", "options", "recommend", "because", "tasks", "deadline", "status", "decided", "notified", "links", "acted", "thread", "question_pending")}
    d["has_run"] = has_run  # the commands themselves never leave the Mac
    for k in ("title", "situation", "why_now", "because"): d[k] = clean(d.get(k) or "", 4000)
    d["thread"] = [{"from": m.get("from", ""), "at": m.get("at", ""), "text": clean(m.get("text", ""), 3000)} for m in (d.get("thread") or []) if isinstance(m, dict)]
    d["status"] = d.get("status") or "open"
    decisions.append(d)
decisions.sort(key=lambda d: (d["status"] != "open", d.get("deadline") or "9", d["id"]))
ntfy = rd(os.path.join(HOME, ".config", "lc-control-room", "ntfy_topic")).strip()

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
    {"name": "Weekly quota reading (W1-05)", "detail": ("%s%% at %s" % (usage[-1]["week"], usage[-1]["at"].replace("T", " "))) if usage else "no reading", "age": int(last_usage_h * 60) if last_usage_h is not None else None,
     "ok": last_usage_h is not None and last_usage_h < 14, "why": "older than 14 h: W1-05 is morning and evening" if last_usage_h is not None and last_usage_h >= 14 else ""},
]

# ---------- SITREP: the few sentences that answer "where are we" ----------
crit_set = set(crit) | set(gates["GO-build"]["path"])
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

# ---------- burn-up: planned vs done vs projected, per day ----------
days_all = sorted({t["day"] for t in T})
burn = [{"day": d, "planned": sum(t["day"] <= d for t in T),
         "done": sum(1 for t in tasks if t["status"] == "done" and (t["updated"] or "9")[:10] <= d) if d <= TODAY else None,
         "proj": sum(1 for t in T if fin.get(t["id"]) and fin[t["id"]].date().isoformat() <= d)} for d in days_all]

today_ids = [t["id"] for t in tasks if t["day"] == TODAY or (t["day"] < TODAY and t["status"] != "done") or (t["status"] == "done" and (t["updated"] or "")[:10] == TODAY)]
reds = [t for t in tasks if t["rag"] == "red"]; ambers = [t for t in tasks if t["rag"] == "amber"]
overall = "red" if reds else "amber" if ambers else "green"
data = {"gen": NOW.strftime("%a %d %b %H:%M"), "gen_iso": NOW.isoformat(), "today": TODAY, "plan": defs.get("plan", ""), "week": defs.get("week", ""),
        "decisions": decisions, "ntfy": ntfy, "tok": tok, "health": health, "sit": sit, "burn": burn, "coord_sid": coord.get("cli_uuid", ""), "now_iso": NOW.isoformat(), "pools": POOLS,
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
