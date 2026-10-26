#!/usr/bin/env python3
"""Tests for the control-room generator. Builds a small notes folder, runs w1_dashboard.py on it with a fixed clock and
an empty HOME (no transcripts), and checks the status colours, the schedule limits, the page safety rules, the JSON
output and the encryption format the page decrypts (openssl enc aes-256-cbc, pbkdf2 sha256, 200000 iterations, gzip).
Run: python3 gen/test_w1_dashboard.py   (stdlib + openssl; exit 0 = all pass)"""
import base64, gzip, hashlib, json, os, re, subprocess, sys, tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []
def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok: FAILS.append(name)

def task(i, day, owner="LANE", model="sonnet", effort="medium", depends=(), **kw):
    t = dict(id=i, day=day, title="task " + i, owner=owner, model=model, effort=effort, depends=list(depends),
             steps=["do it"], done_check="true", evidence="", where="")
    t.update(kw); return t

def build(tmp, tasks, state, files=None, now="2026-10-26T11:45"):
    w1 = os.path.join(tmp, "w1"); os.makedirs(os.path.join(w1, "evidence"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "testq", "results"), exist_ok=True)
    json.dump({"plan": "test", "week": "test week", "tasks": tasks}, open(os.path.join(w1, "W1_TASKS.json"), "w"))
    json.dump(state, open(os.path.join(w1, "state.json"), "w"))
    for rel, body in (files or {}).items():
        p = os.path.join(tmp, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(body if isinstance(body, str) else json.dumps(body))
    home = os.path.join(tmp, "home"); os.makedirs(home, exist_ok=True)
    out, js = os.path.join(w1, "page.html"), os.path.join(w1, "w1.json")
    r = subprocess.run([sys.executable, os.path.join(HERE, "w1_dashboard.py"), "--notes", tmp, "--repo", os.path.join(tmp, "norepo"),
                        "--now", now, "--out", out, "--json", js], capture_output=True, text=True, env=dict(os.environ, HOME=home), timeout=60)
    if r.returncode != 0:
        print(r.stdout, r.stderr); raise SystemExit("generator failed")
    html = open(out).read()
    m = re.search(r"const EMBED=(\{.*?\});\n", html, re.S)
    return html, json.loads(m.group(1)), open(js).read()

tasks = [
    task("W1-01", "2026-10-25", owner="JB", model="none", effort="none"),
    task("W1-02", "2026-10-25", depends=["W1-01"]),                      # done
    task("W1-03", "2026-10-25", depends=["W1-02"]),                      # doing; lane refused a command -> red
    task("W1-03R", "2026-10-25", depends=["W1-03"], review_of="W1-03"),  # waits on red, planned yesterday -> amber
    task("W1-04", "2026-10-25", depends=["W1-02"], model="haiku"),       # ready since yesterday, not started -> red
    task("W1-05", "2026-10-26", owner="JB", model="none", effort="none"),# needs JB -> amber
    task("W1-06", "2026-10-26", depends=["W1-02"]),                      # doing; PR merged, not closed -> amber
    task("W1-07", "2026-10-26", depends=["W1-02"], model="haiku"),       # doing far past estimate -> red
    task("W1-08", "2026-10-26", depends=["W1-02"]),
    task("W1-09", "2026-10-26", owner="SCRIPT", model="none", effort="none", depends=["W1-02"]),
] + [task("W1-%d" % n, "2026-10-27", depends=["W1-02"], gates=["G1"]) for n in range(20, 28)]
state = {
    "W1-01": {"status": "done", "updated": "2026-10-25T20:00"},
    "W1-02": {"status": "done", "updated": "2026-10-25T21:00"},
    "W1-03": {"status": "doing", "opened": [{"at": "2026-10-26T10:00", "as": "sonnet/medium"}]},
    "W1-06": {"status": "doing", "opened": [{"at": "2026-10-26T11:00", "as": "sonnet/medium"}]},
    "W1-07": {"status": "doing", "opened": [{"at": "2026-10-26T08:00", "as": "haiku/medium"}]},
}
files = {
    "w1/evidence/lane-W1-03.json": {"session_id": "s3", "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "scripts/test-submit.sh --lane L1"}}],
                                    "result": "blocked; api_key=SHOULD_NOT_APPEAR </script><script>alert(1)</script>", "num_turns": 5, "total_cost_usd": 0.1},
    "w1/evidence/w106_pr.txt": "pr=900\n",
    "w1/gh_cache.json": [{"number": 900, "state": "MERGED", "mergedAt": "2026-10-26T10:00:00Z", "title": "x", "headRefName": "b", "url": "u"}],
    "w1/evidence/usage_pct.csv": "2026-10-25T22:57Z,40,12.8,26.4\n",
}
with tempfile.TemporaryDirectory() as tmp:
    html, D, js = build(tmp, tasks, state, files)
    T = {t["id"]: t for t in D["tasks"]}
    check("done task is green", T["W1-02"]["rag"] == "green")
    check("lane with a refused command is red and names it", T["W1-03"]["rag"] == "red" and "test-submit" in T["W1-03"]["why"], T["W1-03"]["why"])
    check("red lane carries a next action", "lane settings" in T["W1-03"]["plan"])
    check("waiting on a red task planned yesterday is amber", T["W1-03R"]["rag"] == "amber", T["W1-03R"]["rag"])
    check("ready since yesterday and not started is red", T["W1-04"]["rag"] == "red", T["W1-04"]["why"])
    check("JB task with nothing blocking is amber 'needs JB'", T["W1-05"]["rag"] == "amber" and "JB" in T["W1-05"]["why"])
    check("merged PR on an open task is amber with the done command", T["W1-06"]["rag"] == "amber" and "lc_state.py done W1-06" in T["W1-06"]["plan"], T["W1-06"]["why"])
    check("running past twice its estimate is red", T["W1-07"]["rag"] == "red" and "twice" in T["W1-07"]["why"], T["W1-07"]["why"])
    check("overall status is red when any task is red", D["overall"] == "red")
    iv = [(datetime.fromisoformat(t["beg_iso"]), datetime.fromisoformat(t["fin_iso"])) for t in D["tasks"] if t["owner"] == "LANE" and t["status"] != "done"]
    peak = max(sum(1 for b, f in iv if b <= x < f) for x, _ in iv) if iv else 0
    check("schedule never runs more than 3 lanes at once", peak <= 3, "peak %d" % peak)
    check("nothing starts before its planned day 08:00", all(datetime.fromisoformat(t["beg_iso"]) >= datetime.fromisoformat(t["day"] + "T08:00") for t in D["tasks"] if t["status"] == "todo"))
    check("every task gets a projected finish", all(t["fin_iso"] for t in D["tasks"]))
    check("G1 gate counts its tagged tasks", D["gates"]["G1"]["total"] == 8)
    check("SITREP leads with the red items", D["sit"] and D["sit"][0]["k"] == "red")
    check("data health lists the sources", len(D["health"]) >= 6)
    check("secrets are redacted", "SHOULD_NOT_APPEAR" not in html and "SHOULD_NOT_APPEAR" not in js)
    check("evidence text cannot close the script tag", html.count("</script>") == 1, html.count("</script>"))
    check("the page fetches only its data (and the QR library on demand)", {u for u in re.findall(r'https?://[^"\'`\s)]+', html) if not u.startswith(("https://ntfy.sh/", "https://github.com/settings/personal-access-tokens/new"))} <= {"https://raw.githubusercontent.com/multisportusa-git/lc-control-room/data/w1.enc", "https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js", "https://github.com/multisportusa-git/lc-control-room/issues/new", "https://api.github.com/repos/multisportusa-git/lc-control-room/issues"}, set(re.findall(r'https?://[^"\'`\s)]+', html)))
    check("JSON output equals the embedded data", json.loads(js) == D)

    # encryption round trip in the exact format the page decrypts
    pw = os.path.join(tmp, "pass"); open(pw, "w").write("correct horse battery staple")
    enc = subprocess.run("gzip -9nc '%s' | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass file:'%s' -base64 -A" % (os.path.join(tmp, "w1", "w1.json"), pw),
                         shell=True, capture_output=True, text=True).stdout
    raw = base64.b64decode(enc)
    check("ciphertext uses the openssl Salted__ header", raw[:8] == b"Salted__")
    kiv = hashlib.pbkdf2_hmac("sha256", b"correct horse battery staple", raw[8:16], 200000, 48)
    dec = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-K", kiv[:32].hex(), "-iv", kiv[32:].hex(), "-nosalt"], input=raw[16:], capture_output=True)
    check("key and IV derive as the page derives them (PBKDF2-SHA256, 48 bytes)", dec.returncode == 0 and json.loads(gzip.decompress(dec.stdout)) == D)
    check("ciphertext holds no readable task text", b"task W1" not in raw and "task W1" not in enc)

with tempfile.TemporaryDirectory() as tmp2:
    html2, D2, _ = build(tmp2, tasks, {}, {"w1/gh_cache.json": "not json", "w1/evidence/lane-W1-03.json": "{bad"})
    check("page still builds from empty state and bad files", len(D2["tasks"]) == len(tasks))

# decisions: shown on the page, and recorded only from the owner's issues
with tempfile.TemporaryDirectory() as tmp3:
    dec = {"id": "D-7", "title": "Pick a thing", "situation": "S", "options": [{"key": "A", "label": "a"}, {"key": "B", "label": "b"}], "recommend": "A", "status": "open"}
    html3, D3, _ = build(tmp3, tasks, state, {"w1/decisions/D-7.json": dec, "w1/decisions/D-8.json": dict(dec, id="D-8")})
    check("open decisions reach the page data", [d["id"] for d in D3["decisions"]] == ["D-7", "D-8"])
    check("SITREP leads with waiting decisions", D3["sit"][0]["k"] == "you" and "D-7" in D3["sit"][0]["text"])
    bindir = os.path.join(tmp3, "bin"); os.makedirs(bindir)
    issues = [{"number": 11, "title": "DECISION D-7: B", "body": "go with B", "author": {"login": "multisportusa-git"}},
              {"number": 12, "title": "DECISION D-8: A", "body": "", "author": {"login": "someone-else"}},
              {"number": 13, "title": "DECISION D-8: Z", "body": "", "author": {"login": "multisportusa-git"}},
              {"number": 14, "title": "DECISION D-8: OTHER", "body": "Why not B? <!-- x -->", "author": {"login": "multisportusa-git"}}]
    open(os.path.join(bindir, "gh"), "w").write("#!/bin/sh\nif [ \"$1 $2\" = \"issue list\" ]; then cat '%s'; else echo \"$@\" >> '%s'; fi\n" % (os.path.join(tmp3, "issues.json"), os.path.join(tmp3, "gh.log")))
    os.chmod(os.path.join(bindir, "gh"), 0o755); json.dump(issues, open(os.path.join(tmp3, "issues.json"), "w"))
    r = subprocess.run([sys.executable, os.path.join(HERE, "decisions_sync.py"), "--notes", tmp3], capture_output=True, text=True,
                       env=dict(os.environ, HOME=os.path.join(tmp3, "home"), PATH=bindir + os.pathsep + os.environ["PATH"]), timeout=60)
    d7 = json.load(open(os.path.join(tmp3, "w1/decisions/D-7.json"))); d8 = json.load(open(os.path.join(tmp3, "w1/decisions/D-8.json")))
    check("owner's issue records the choice and its note", d7["status"] == "decided" and d7["decided"]["choice"] == "B" and d7["decided"]["note"] == "go with B", r.stdout + r.stderr)
    check("someone else's issue records nothing", d8["status"] == "open", d8.get("decided"))
    check("an option that does not exist records nothing", d8["status"] == "open")
    d8b = json.load(open(os.path.join(tmp3, "w1/decisions/D-8.json")))
    check("Other adds JB's text to the thread and keeps the decision open", d8b["status"] == "open" and d8b["thread"][-1]["text"] == "Why not B?" and d8b.get("question_pending") is True, d8b.get("thread"))
    ghlog = open(os.path.join(tmp3, "gh.log")).read()
    check("every DECISION issue is answered and closed", all(("close %d" % n) in ghlog for n in (11, 12, 13, 14)), ghlog)
    check("each new open decision is notified once", "notified D-8" in open(os.path.join(tmp3, "w1/decisions/decisions.log")).read() and json.load(open(os.path.join(tmp3, "w1/decisions/D-8.json"))).get("notified"))

# JB task reports: evidence written, task closed through lc_state.py with his words as the note
with tempfile.TemporaryDirectory() as tmp4:
    lc = os.path.join(tmp4, "lc"); os.makedirs(os.path.join(lc, "docs/handover/v3")); os.makedirs(os.path.join(lc, "scripts"))
    json.dump({"tasks": [task("W1-05", "2026-10-26", owner="JB", model="none", effort="none", evidence="usage_pct.csv"),
                         task("W1-55", "2026-10-26", owner="JB", model="none", effort="none", evidence="dh0.md"),
                         task("W1-12", "2026-10-26")]}, open(os.path.join(lc, "docs/handover/v3/W1_TASKS.json"), "w"))
    open(os.path.join(lc, "scripts/lc_state.py"), "w").write("import sys\nopen(%r,'a').write(' '.join(sys.argv[1:])+'\\n')\n" % os.path.join(tmp4, "lc.log"))
    subprocess.run("git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm x && git update-ref refs/remotes/origin/main HEAD", shell=True, cwd=lc, check=True)
    os.makedirs(os.path.join(tmp4, "w1/evidence")); bindir = os.path.join(tmp4, "bin"); os.makedirs(bindir)
    issues = [{"number": 21, "title": "ACTION W1-05: 46 15 30", "body": "", "author": {"login": "multisportusa-git"}},
              {"number": 22, "title": "ACTION W1-55: works, iOS 26 boots", "body": "", "author": {"login": "multisportusa-git"}},
              {"number": 23, "title": "ACTION W1-12: done", "body": "", "author": {"login": "multisportusa-git"}},
              {"number": 24, "title": "ACTION W1-55: fake", "body": "", "author": {"login": "intruder"}}]
    json.dump(issues, open(os.path.join(tmp4, "issues.json"), "w"))
    open(os.path.join(bindir, "gh"), "w").write("#!/bin/sh\nif [ \"$1 $2\" = \"issue list\" ]; then cat '%s'; else echo \"$@\" >> '%s'; fi\n" % (os.path.join(tmp4, "issues.json"), os.path.join(tmp4, "gh.log")))
    os.chmod(os.path.join(bindir, "gh"), 0o755)
    r = subprocess.run([sys.executable, os.path.join(HERE, "decisions_sync.py"), "--notes", tmp4, "--lc-repo", lc], capture_output=True, text=True,
                       env=dict(os.environ, HOME=os.path.join(tmp4, "home"), PATH=bindir + os.pathsep + os.environ["PATH"]), timeout=60)
    usage = open(os.path.join(tmp4, "w1/evidence/usage_pct.csv")).read().strip().split(",")
    check("W1-05 report appends a usage line", usage[1:] == ["46", "15", "30"], usage)
    dh0 = open(os.path.join(tmp4, "w1/evidence/dh0.md")).read()
    check("a JB report becomes the task's evidence", "works, iOS 26 boots" in dh0 and "fake" not in dh0, dh0)
    lcl = open(os.path.join(tmp4, "lc.log")).read()
    check("the task is closed through lc_state.py with JB's words", "done W1-05 --note JB via issue #21" in lcl and "done W1-55 --note JB via issue #22" in lcl, lcl)
    check("only JB's own tasks can be reported", "W1-12" not in lcl, lcl)
    check("reports from anyone else are ignored", "#24" not in lcl and "close 24" in open(os.path.join(tmp4, "gh.log")).read())

print("ALL PASS" if not FAILS else "%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
