"""Fixture test for gen/wake.py (no network, no model, temp notes dir). Run: python3 gen/test_wake.py"""
import json, os, subprocess, sys, tempfile, time, glob
H = os.path.dirname(os.path.abspath(__file__)); W = os.path.join(H, "wake.py")
t = tempfile.mkdtemp(); N = t
for d in ("v2/lanes", "v2/queue", "v2/locks", "v2/runs", "v2/sup/wake", "reviews", "testq/results", "status", "w1/evidence"): os.makedirs(os.path.join(N, d))
open(os.path.join(N, "w1/evidence/usage_pct.csv"), "w").write("2026-10-28T10:10,76,50,1,49,x\n")
RP = os.path.join(t, "repo"); os.makedirs(os.path.join(RP, "scripts")); TF = os.path.join(t, "w1tasks.json")
json.dump({"tasks": []}, open(TF, "w"))
open(os.path.join(RP, "scripts/lc_state.py"), "w").write("import sys,os\nopen(os.path.join(os.path.dirname(__file__),'calls.txt'),'a').write(' '.join(sys.argv[1:3])+'\\n')\nsys.exit(5 if sys.argv[2]=='X-REF' else 0)\n")
def run(): return subprocess.run([sys.executable, W], env=dict(os.environ, LC_WAKE_NOTES=N, LC_WAKE_NOINJECT="1", LC_WAKE_REPO=RP, LC_W1_TASKS=TF), capture_output=True, text=True).stdout
run()  # first run writes the epoch
old = time.time() - 3600
os.utime(os.path.join(N, "v2/sup/wake/epoch"), (old, old))
lr = os.path.join(N, "v2/runs/A-1.jsonl"); open(lr, "w").write("{}"); os.utime(lr, (old + 60, old + 60))
json.dump({"task": "t", "brief": "/b.md", "branch": "v2/A-t", "model": "sonnet", "effort": "high", "role": "lane", "last_run": lr, "pr": "9", "state": "pr"}, open(os.path.join(N, "v2/lanes/A.json"), "w"))
open(os.path.join(N, "reviews/9-abcdef1234.changes"), "w").write("x")
out = run(); q = glob.glob(os.path.join(N, "v2/queue/A-t.req"))
assert q, "1 review CHANGES did not queue: " + out
lines = open(q[0]).read().splitlines(); assert len(lines) == 7 and lines[0] == "A" and lines[3] == "sonnet", lines
assert os.path.getsize(lines[2]) < 8000; print("PASS review CHANGES -> .req (7 lines, brief < 8 KB)")
os.remove(q[0]); out = run(); assert not glob.glob(os.path.join(N, "v2/queue/*.req")), "requeued the same CHANGES twice"; print("PASS same CHANGES not requeued twice")
# dead relaunch: state relaunching, no lock, json older than 5 min but newer than the epoch
os.remove(os.path.join(N, "reviews/9-abcdef1234.changes"))
p = os.path.join(N, "v2/lanes/B.json"); json.dump({"task": "u", "brief": "/c.md", "model": "sonnet", "role": "lane", "state": "relaunching", "job": "j1"}, open(p, "w"))
open(os.path.join(N, "testq/results/j1.txt"), "w").write("overall=FAIL\n"); os.utime(p, (old + 120, old + 120)); os.utime(os.path.join(N, "testq/results/j1.txt"), (old + 120, old + 120))
run(); assert glob.glob(os.path.join(N, "v2/queue/B-u.req")), "2 dead relaunch not queued"; print("PASS dead relaunch -> .req")
# locked lane is left alone
os.remove(os.path.join(N, "v2/queue/B-u.req")); os.remove(glob.glob(os.path.join(N, "v2/sup/wake/dead-B-*"))[0]); os.makedirs(os.path.join(N, "v2/locks/lane-B"))
run(); assert not glob.glob(os.path.join(N, "v2/queue/B-u.req")), "queued a locked lane"; print("PASS running lane is left alone")
# quota stop
os.rmdir(os.path.join(N, "v2/locks/lane-B")); open(os.path.join(N, "w1/evidence/usage_pct.csv"), "a").write("2026-10-28T11:00,96,55,1,50,x\n")
run(); assert not glob.glob(os.path.join(N, "v2/queue/B-u.req")), "queued at >= 95%"; print("PASS nothing queued at >= 95% usage")
# third CHANGES escalates instead of requeueing
open(os.path.join(N, "w1/evidence/usage_pct.csv"), "a").write("2026-10-28T11:05,76,55,1,50,x\n"); os.remove(p)
for s in ("bbbbbbbb1", "cccccccc1"): open(os.path.join(N, "v2/sup/wake/changes-9-%s" % s[:8]), "w").write("x")
open(os.path.join(N, "reviews/9-dddddddd99.changes"), "w").write("x")
out = run(); assert not glob.glob(os.path.join(N, "v2/queue/A-t.req")) and "Controller woken" in out, "3rd CHANGES did not escalate: " + out
print("PASS 3rd CHANGES wakes the Controller instead of a 3rd round")
# 4 slot refill from next.txt (JB 28 Oct 16:03)
os.environ["LC_WAKE_MEMFREE"] = "50"
for f in glob.glob(os.path.join(N, "v2/queue/*.req")): os.remove(f)
os.makedirs(os.path.join(N, "v2/queue/held"), exist_ok=True)
bf = os.path.join(N, "brief x.md"); open(bf, "w").write("brief")
open(os.path.join(N, "v2/max_lanes"), "w").write("3\n")
os.makedirs(os.path.join(N, "v2/locks/lane-Z"))
open(os.path.join(N, "v2/queue/held/H-h.req"), "w").write("H")
open(os.path.join(N, "v2/sup/next.txt"), "w").write("# comment\nNEXT Z | z | %s\nNEXT H | h | %s\nNEXT P | p | %s | opus | high\nNEXT Q | q | /missing.md\nNEXT R | r | %s\nNEXT S | s | %s\n" % (bf, bf, bf, bf, bf))
out = run(); got = sorted(os.path.basename(f) for f in glob.glob(os.path.join(N, "v2/queue/*.req")))
assert got == ["P-p.req", "R-r.req"], "refill picked wrong lanes: %s %s" % (got, out)
l = open(os.path.join(N, "v2/queue/P-p.req")).read().splitlines(); assert l[:6] == ["P", "p", bf, "opus", "high", "lane"], l
print("PASS refill fills free slots in order, skips locked/held/missing-brief lanes, keeps path with spaces")
run(); assert len(glob.glob(os.path.join(N, "v2/queue/*.req"))) == 2, "over-queued when slots full"; print("PASS no refill while slots are full")
os.remove(os.path.join(N, "v2/queue/P-p.req")); run()
got = sorted(os.path.basename(f) for f in glob.glob(os.path.join(N, "v2/queue/*.req"))); assert got == ["R-r.req", "S-s.req"], got
print("PASS a consumed next.txt line is never re-queued; next one takes the slot")
for f in glob.glob(os.path.join(N, "v2/queue/*.req")): os.remove(f)
open(os.path.join(N, "v2/sup/next.txt"), "a").write("NEXT U | u | %s\n" % bf)
open(os.path.join(N, "w1/evidence/usage_pct.csv"), "a").write("2026-10-28T12:00,96,55,1,50,x\n")
run(); assert not glob.glob(os.path.join(N, "v2/queue/U-u.req")), "refilled at >= 95%"
open(os.path.join(N, "w1/evidence/usage_pct.csv"), "a").write("2026-10-28T12:05,70,55,1,50,x\n"); os.environ["LC_WAKE_MEMFREE"] = "10"
run(); assert not glob.glob(os.path.join(N, "v2/queue/U-u.req")), "refilled at low memory"
print("PASS no refill at >= 95% usage or < 20% memory free")
# 5 staged plan tasks whose dependencies are done (JB 28 Oct 16:08)
open(os.path.join(N, "w1/evidence/usage_pct.csv"), "a").write("2026-10-28T12:10,70,55,1,50,x\n"); os.environ["LC_WAKE_MEMFREE"] = "50"
for f in glob.glob(os.path.join(N, "v2/sup/wake/coord-*")): os.remove(f)
json.dump({"D": {"status": "done"}, "X-OPEN": {"status": "todo"}, "X-DONE": {"status": "done"}}, open(os.path.join(N, "w1/state.json"), "w"))
tk = lambda i, o, chk, dep=("D",), day="2026-01-01": {"id": i, "owner": o, "model": "sonnet", "effort": "low", "title": i, "day": day, "depends": list(dep), "done_check": chk}
json.dump({"tasks": [tk("D", "LANE", "true", ()), tk("X-PASS", "COORD", "true"), tk("X-REF", "LANE", "true"), tk("X-SCR", "SCRIPT", "false"),
                     tk("X-LANE", "LANE", "false"), tk("X-WAIT", "LANE", "true", ("X-OPEN",)), tk("X-LATER", "LANE", "true", day="2999-01-01"),
                     tk("X-DONE", "LANE", "true")]}, open(TF, "w"))
out = run(); import time as _t; _t.sleep(1); calls = open(os.path.join(RP, "scripts/calls.txt")).read().split("\n")
assert "done X-PASS" in calls and "run X-SCR" in calls and "done X-REF" in calls, calls
assert not [c for c in calls if any(x in c for x in ("X-WAIT", "X-LATER", "X-DONE", "X-LANE"))], calls
told = sorted(os.path.basename(f).split("-")[2] + "-" + os.path.basename(f).split("-")[3] for f in glob.glob(os.path.join(N, "v2/sup/wake/staged-told-*")))
assert "Controller woken" in out and told == ["X-LANE", "X-REF"], (told, out)
print("PASS staged: passing check closes, SCRIPT starts, LANE and refused close wake the Controller once; open deps / future day / done skipped")
os.remove(os.path.join(RP, "scripts/calls.txt")); out = run()
assert not os.path.exists(os.path.join(RP, "scripts/calls.txt")) and "Controller woken" not in out, out
print("PASS staged: nothing retried within 15 min, no second wake")
# 6 timeline truth (pure function)
import importlib.util, datetime as dt
os.environ["LC_WAKE_NOTES"] = N; sp = importlib.util.spec_from_file_location("wk", W); wk = importlib.util.module_from_spec(sp); sp.loader.exec_module(wk)
n0 = dt.datetime(2026, 10, 28, 16, 30); g = n0.isoformat()
ok = {"gen_iso": g, "live_lanes": [{"lane": "SCOPE", "holds": True, "etc": "17:45"}], "tasks": [{"id": "W1-19", "slot": "LANE 1", "status": "todo", "beg_iso": "2026-10-28T17:45", "fin_iso": "2026-10-28T19:15"}]}
assert wk.timeline_problems(ok, {"SCOPE"}, {"SCOPE": {"eta": "2026-10-28T17:45"}}, n0) == []
bad = dict(ok, tasks=[{"id": "W1-19", "slot": "LANE 1", "status": "todo", "beg_iso": "2026-10-28T16:20", "fin_iso": "2026-10-28T17:50"}])
pr = wk.timeline_problems(bad, {"SCOPE", "R402"}, {"SCOPE": {"eta": "2026-10-28T18:00"}}, n0)
assert any("R402 is running but has no live row" in x for x in pr) and any("ETC on page 17:45, set 18:00" in x for x in pr) and any("W1-19 drawn as running" in x for x in pr), pr
assert any("stale" in x for x in wk.timeline_problems(dict(ok, gen_iso="2026-10-28T16:00"), {"SCOPE"}, {}, n0))
assert any("no live_lanes" in x for x in wk.timeline_problems({"gen_iso": g, "tasks": []}, set(), {}, n0))
print("PASS timeline truth: missing live row, ETC drift, planned task drawn as running, stale data, old generator all caught; clean page passes")
print("ALL PASS")

# --- CH-020 dedupe + circuit tests (pytest-collectable) ---

def _wake_notes(usage_row="2026-10-28T10:00,70,50,1,49,x\n"):
    """Fresh temp notes dir for CH-020 tests."""
    d = tempfile.mkdtemp()
    for sub in ("v2/lanes", "v2/queue", "v2/locks", "v2/sup/wake",
                 "reviews", "testq/results", "status", "w1/evidence", "w1/decisions"):
        os.makedirs(os.path.join(d, sub))
    open(os.path.join(d, "w1/evidence/usage_pct.csv"), "w").write(usage_row)
    return d

def _wake_run(notes, extra=None):
    env = dict(os.environ, LC_WAKE_NOTES=notes, LC_WAKE_NOINJECT="1",
               LC_WAKE_REPO=RP, LC_W1_TASKS=TF)
    if extra:
        env.update(extra)
    return subprocess.run([sys.executable, W], env=env, capture_output=True, text=True).stdout

def _make_ask(notes, state="WORKING", blocked=False, eta=None):
    asks = {"asks": [{"id": "A-1", "state": state, "title": "Test ask",
                       "cat": "Question", "blocked": blocked}]}
    if eta:
        asks["asks"][0]["eta"] = eta
    open(os.path.join(notes, "w1/asks.json"), "w").write(json.dumps(asks))

def _force_recheck(notes):
    """Backdate throttle files so asks_check re-runs."""
    distant_past = time.time() - 7200
    for fname in ("asks-check", "asks-late"):
        p = os.path.join(notes, "v2/sup/wake", fname)
        if os.path.exists(p):
            os.utime(p, (distant_past, distant_past))

def _setup(n, usage_extra=None):
    """Write epoch with no asks (clean state), backdate it, optionally add usage row."""
    _wake_run(n)  # epoch run (no asks yet → no P0 pollution)
    old = time.time() - 7200
    os.utime(os.path.join(n, "v2/sup/wake/epoch"), (old, old))
    if usage_extra:
        open(os.path.join(n, "w1/evidence/usage_pct.csv"), "a").write(usage_extra)

def test_dedup_same_hash():
    """Same state hash within 60 min: only 1 wake; second run deduped."""
    n = _wake_notes()
    _setup(n)
    _make_ask(n)
    _force_recheck(n)

    out1 = _wake_run(n)  # run 1: no prior state → wake
    assert "Controller woken (asks late)" in out1, "run 1 should wake: " + out1

    _force_recheck(n)
    out2 = _wake_run(n)  # run 2: same hash, < 60 min → dedup
    assert "Controller woken (asks late)" not in out2, "same hash should dedup: " + out2
    assert "dedup" in out2, "expected dedup announce: " + out2

def test_changed_ask_wakes():
    """A changed ask state bypasses dedup and wakes immediately."""
    n = _wake_notes()
    _setup(n)
    _make_ask(n, state="WORKING")
    _force_recheck(n)

    out1 = _wake_run(n)  # run 1: wake
    assert "Controller woken (asks late)" in out1, "run 1: " + out1

    _force_recheck(n)  # same state → dedup
    out_dup = _wake_run(n)
    assert "Controller woken (asks late)" not in out_dup, "same state should dedup: " + out_dup

    _force_recheck(n)
    _make_ask(n, state="BLOCKED")  # state changed → different hash → bypasses dedup
    out2 = _wake_run(n)
    assert "Controller woken (asks late)" in out2, "changed ask should wake: " + out2

def test_circuit_open_no_wake():
    """provider_circuit.json state=open suppresses all Controller wakes."""
    n = _wake_notes()
    _setup(n)
    _make_ask(n)
    _force_recheck(n)
    open(os.path.join(n, "v2/provider_circuit.json"), "w").write(json.dumps({"state": "open"}))

    out = _wake_run(n)
    assert "Controller woken" not in out, "circuit open should suppress wake: " + out
    assert "circuit" in out.lower(), "expected circuit announce: " + out

def test_stale_p0_once_per_hash():
    """Repeated stale escalations write 1 ALERTS.txt [P0] ASKS-LATE line per state hash."""
    n = _wake_notes(usage_row="2026-10-28T10:00,96,50,1,49,x\n")
    _setup(n)
    _make_ask(n)
    _force_recheck(n)
    alerts_f = os.path.join(n, "ALERTS.txt")

    _wake_run(n)  # run 1: first hash → 1 P0 line
    _force_recheck(n)
    _wake_run(n)  # run 2: same hash → no new P0
    _force_recheck(n)
    _wake_run(n)  # run 3: same hash → still no new P0
    p0_lines = [l for l in (open(alerts_f).read().splitlines() if os.path.exists(alerts_f) else []) if "[P0] ASKS-LATE" in l]
    assert len(p0_lines) == 1, "expected 1 P0 line, got %d: %s" % (len(p0_lines), p0_lines)

    _force_recheck(n)
    _make_ask(n, state="BLOCKED")  # different hash
    open(os.path.join(n, "w1/evidence/usage_pct.csv"), "a").write("2026-10-28T11:00,96,50,1,49,x\n")
    _wake_run(n)  # run 4: new hash → second P0 line
    p0_lines = [l for l in open(alerts_f).read().splitlines() if "[P0] ASKS-LATE" in l]
    assert len(p0_lines) == 2, "expected 2 P0 lines after hash change, got %d: %s" % (len(p0_lines), p0_lines)
