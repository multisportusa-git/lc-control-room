"""Fixture test for gen/wake.py (no network, no model, temp notes dir). Run: python3 gen/test_wake.py"""
import json, os, subprocess, sys, tempfile, time, glob
H = os.path.dirname(os.path.abspath(__file__)); W = os.path.join(H, "wake.py")
t = tempfile.mkdtemp(); N = t
for d in ("v2/lanes", "v2/queue", "v2/locks", "v2/runs", "v2/sup/wake", "reviews", "testq/results", "status", "w1/evidence"): os.makedirs(os.path.join(N, d))
open(os.path.join(N, "w1/evidence/usage_pct.csv"), "w").write("2026-10-28T10:10,76,50,1,49,x\n")
def run(): return subprocess.run([sys.executable, W], env=dict(os.environ, LC_WAKE_NOTES=N, LC_WAKE_NOINJECT="1"), capture_output=True, text=True).stdout
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
os.rmdir(os.path.join(N, "v2/locks/lane-B")); open(os.path.join(N, "w1/evidence/usage_pct.csv"), "a").write("2026-10-28T11:00,86,55,1,50,x\n")
run(); assert not glob.glob(os.path.join(N, "v2/queue/B-u.req")), "queued at >= 85%"; print("PASS nothing queued at >= 85% usage")
# third CHANGES escalates instead of requeueing
open(os.path.join(N, "w1/evidence/usage_pct.csv"), "a").write("2026-10-28T11:05,76,55,1,50,x\n"); os.remove(p)
for s in ("bbbbbbbb1", "cccccccc1"): open(os.path.join(N, "v2/sup/wake/changes-9-%s" % s[:8]), "w").write("x")
open(os.path.join(N, "reviews/9-dddddddd99.changes"), "w").write("x")
out = run(); assert not glob.glob(os.path.join(N, "v2/queue/A-t.req")) and "Controller woken" in out, "3rd CHANGES did not escalate: " + out
print("PASS 3rd CHANGES wakes the Controller instead of a 3rd round"); print("ALL PASS")
