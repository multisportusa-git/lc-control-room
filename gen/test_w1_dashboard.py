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

def build(tmp, tasks, state, files=None, now="2026-10-26T11:45", mtimes=None):
    w1 = os.path.join(tmp, "w1"); os.makedirs(os.path.join(w1, "evidence"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "testq", "results"), exist_ok=True)
    json.dump({"plan": "test", "week": "test week", "tasks": tasks}, open(os.path.join(w1, "W1_TASKS.json"), "w"))
    json.dump(state, open(os.path.join(w1, "state.json"), "w"))
    for rel, body in (files or {}).items():
        p = os.path.join(tmp, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(body if isinstance(body, str) else json.dumps(body))
    for rel, at_ in (mtimes or {}).items():
        e_ = datetime.fromisoformat(at_).timestamp(); os.utime(os.path.join(tmp, rel), (e_, e_))
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
    "w1/jb_briefs.json": {"briefs": {"W1-05": {"title": "Read the usage meter", "why": "w", "steps": ["a"], "time": "2 min", "answer": "numbers"}}},
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
    check("the page fetches only its data (and the QR library on demand)", {u for u in re.findall(r'https?://[^"\'`\s)]+', html) if not u.startswith(("https://ntfy.sh/", "https://github.com/settings/personal-access-tokens/new"))} <= {"https://raw.githubusercontent.com/multisportusa-git/lc-control-room/data/w1.enc", "https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js", "https://github.com/multisportusa-git/lc-control-room/issues/new", "https://api.github.com/repos/multisportusa-git/lc-control-room/issues", "https://api.github.com/repos/multisportusa-git/lc-control-room/contents/w1.enc?ref=data"}, set(re.findall(r'https?://[^"\'`\s)]+', html)))
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

# ---------- chat: page crypto <-> openssl, owner-only, rate limit, thread, answer store, escaping ----------
PAGE = open(os.path.join(HERE, "..", "index.html")).read()
def js_fn(name):
    i = PAGE.index("async function %s(" % name); j = PAGE.index("\nasync function", i + 10) if "\nasync function" in PAGE[i + 10:] else len(PAGE)
    k = PAGE.find("\nfunction ", i + 10); j = min(j, k) if k > 0 else j
    return PAGE[i:j]
NODE = __import__("shutil").which("node")
def node(script, *args):
    r = subprocess.run([NODE, "-e", "const ITER=200000;" + js_fn("encryptTxt") + "\n" + js_fn("decryptRaw") + "\n" + script, *args], capture_output=True, text=True, timeout=60)
    if r.returncode: print(r.stderr)
    return r.stdout.strip()
def ossl(args, data, pw):
    return subprocess.run(["openssl", "enc", *args, "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-base64", "-A", "-pass", "file:" + pw],
                          input=data.encode(), capture_output=True, timeout=30)
CHAT_PW = "test passphrase only 7f3a"
MSG = 'Héllo "coordinator" </script><b>x</b> ✓'
with tempfile.TemporaryDirectory() as tc:
    pw = os.path.join(tc, "pass"); open(pw, "w").write(CHAT_PW + "\n")
    if not NODE: check("node is available for the page crypto test", False, "install node")
    else:
        enc = node("encryptTxt(process.argv[1],process.argv[2]).then(x=>console.log(x))", MSG, CHAT_PW)
        r = ossl(["-d"], enc, pw)
        check("page-encrypted text decrypts with openssl -pass file:", r.returncode == 0 and r.stdout.decode() == MSG, r.stderr)
        check("page ciphertext is openssl format (Salted__ + salt)", base64.b64decode(enc)[:8] == b"Salted__")
        enc2 = ossl(["-salt"], MSG, pw).stdout.decode()
        check("openssl-encrypted text decrypts with the page's code", node("decryptRaw(process.argv[1],process.argv[2]).then(x=>console.log(x))", enc2, CHAT_PW) == MSG)
    def page_enc(obj): return node("encryptTxt(process.argv[1],process.argv[2]).then(x=>console.log(x))", json.dumps(obj), CHAT_PW) if NODE else ossl(["-salt"], json.dumps(obj), pw).stdout.decode()
    bindir = os.path.join(tc, "bin"); os.makedirs(bindir)
    open(os.path.join(bindir, "gh"), "w").write("#!/bin/sh\nif [ \"$1 $2\" = \"issue list\" ]; then cat '%s'; else echo \"$@\" >> '%s'; fi\n" % (os.path.join(tc, "issues.json"), os.path.join(tc, "gh.log")))
    os.chmod(os.path.join(bindir, "gh"), 0o755)
    OWN = {"login": "multisportusa-git"}
    def cs(issues, *extra):
        json.dump(issues, open(os.path.join(tc, "issues.json"), "w"))
        return subprocess.run([sys.executable, os.path.join(HERE, "chat_sync.py"), "--notes", tc, "--key", pw, "--no-spawn", "--limit", "3", *extra], capture_output=True, text=True,
                              env=dict(os.environ, HOME=os.path.join(tc, "home"), PATH=bindir + os.pathsep + os.environ["PATH"]), timeout=60)
    def thread(t):
        try: return json.load(open(os.path.join(tc, "w1", "chat", t + ".json")))["msgs"]
        except OSError: return []
    r = cs([{"number": 21, "title": "CHAT V3", "body": page_enc({"t": "V3", "m": "Where is W1-07?", "cid": "abc12345", "d": "auto"}), "author": OWN},
            {"number": 22, "title": "CHAT V2", "body": page_enc({"t": "V2", "m": "from a stranger", "cid": "zzz12345"}), "author": {"login": "someone-else"}},
            {"number": 23, "title": "CHAT V2", "body": "not encrypted at all", "author": OWN},
            {"number": 24, "title": "CHAT V3", "body": page_enc({"t": "V2", "m": "mismatch", "cid": "mmm12345"}), "author": OWN},
            {"number": 25, "title": "DECISION D-1: A", "body": "", "author": OWN}])
    v3 = thread("V3"); ghl = open(os.path.join(tc, "gh.log")).read()
    check("owner's CHAT is decrypted and appended to its thread, pending", len(v3) == 1 and v3[0]["text"] == "Where is W1-07?" and v3[0]["pending"] is True and v3[0]["id"] == "abc12345", r.stdout + r.stderr + str(v3))
    check("CHAT from anyone else records nothing", thread("V2") == [] and "close 22" in ghl and "Only the repo owner" in ghl)
    check("unreadable or mismatched CHAT is closed, nothing recorded", "close 23" in ghl and "close 24" in ghl and len(v3) == 1)
    check("CHAT issues are answered 'received (encrypted reply on the page)' and closed", "close 21 -R multisportusa-git/lc-control-room -c received (encrypted reply on the page)" in ghl, ghl)
    check("other issues are left alone", "25" not in ghl)
    open(os.path.join(tc, "gh.log"), "w").close()
    cs([{"number": 21, "title": "CHAT V3", "body": page_enc({"t": "V3", "m": "Where is W1-07?", "cid": "abc12345"}), "author": OWN}])
    check("a re-read issue is not appended twice", len(thread("V3")) == 1)
    cs([{"number": 30 + i, "title": "CHAT V2", "body": page_enc({"t": "V2", "m": "q%d" % i, "cid": "rate%04d" % i}), "author": OWN} for i in range(4)])
    v2t = thread("V2")
    check("rate limit: at most N accepted per hour, the rest get a 'send again' reply", sum(1 for m in v2t if m["from"] == "JB" and m.get("pending")) == 2 and sum(1 for m in v2t if "at most 3 messages an hour" in m.get("text", "")) == 2, v2t)
    # answer: stream-json output with the first request's usage, an action record and a secret to redact
    rd_ = os.path.join(tc, "run"); os.makedirs(rd_)
    out = os.path.join(rd_, "out.json")
    open(out, "w").write("\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_creation_input_tokens": 18000, "cache_read_input_tokens": 3000}}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 9, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 21000}}},
        {"type": "result", "is_error": False, "result": "PR #510 waits on review. token=abcdef123\n```record\n{\"type\": \"note\", \"text\": \"Run the L2 rides next\"}\n```", "modelUsage": {"claude-sonnet-5-5": {}}, "total_cost_usd": 0.02}]))
    cid0 = next(m["id"] for m in v2t if m.get("pending"))
    subprocess.run([sys.executable, os.path.join(HERE, "chat_sync.py"), "store", "V2", cid0, out, "--notes", tc], check=True, env=dict(os.environ, HOME=os.path.join(tc, "home")), timeout=30)
    v2t = thread("V2"); ans = [m for m in v2t if m.get("re") == cid0]
    check("the answer lands in the thread and clears pending", len(ans) == 1 and not next(m for m in v2t if m["id"] == cid0)["pending"] and "PR #510" in ans[0]["text"])
    check("first-request start tokens are measured from stream-json", ans and ans[0].get("start_tokens") == 21005, ans and ans[0].get("start_tokens"))
    check("secrets in an answer are redacted", ans and "abcdef123" not in ans[0]["text"])
    note = open(os.path.join(tc, "status", "JB-to-V2-coordinator.md")).read()
    check("a V2 action is recorded for the V2 coordinator and the answer says so", "Run the L2 rides next" in note and "Recorded in JB-to-V2-coordinator.md" in ans[0]["text"] and "```record" not in ans[0]["text"])
    pl = os.path.join(tc, "plan"); subprocess.run([sys.executable, os.path.join(HERE, "chat_sync.py"), "plan", "V3", "abc12345", pl, "--notes", tc], check=True, env=dict(os.environ, HOME=os.path.join(tc, "home")), timeout=30)
    st = json.load(open(os.path.join(pl, "settings.json"))); prm = open(os.path.join(pl, "prompt.txt")).read()
    check("answer runs are read-only, deny secrets and have no MCP servers", "Write" in st["permissions"]["deny"] and any("lc-control-room" in d for d in st["permissions"]["deny"]) and any("BrokerKey" in d for d in st["permissions"]["deny"])
          and json.load(open(os.path.join(pl, "mcp.json"))) == {"mcpServers": {}} and os.listdir(os.path.join(pl, "cwd")) == [] and "Where is W1-07?" in prm)
    check("Auto uses Haiku for a short status question", open(os.path.join(pl, "model.txt")).read() == "claude-haiku-5-5")

# ---------- fuel block, V2 mirror KPIs and chat escaping in the generated page ----------
def at(s_): return int(datetime.fromisoformat(s_).timestamp())
road = ('<p id="owner">✱ K88 coordinator</p><p id="sitrep">All fine. Current step: <b>C</b> (L2 run)</p><span id="asof">11:30</span>'
        '<tr><td>A</td><td>L0 phase 1 build</td><td id="what-A"><span class="done"><b>A1</b> built</span></td><td>Sun</td><td>done Sun 13:08</td></tr>'
        '<tr><td>B</td><td>L1 inventory</td><td id="what-B"><span class="done"><b>B1</b> done</span></td><td>Mon</td><td>done Mon 10:00</td></tr>'
        '<tr><td>C</td><td>L2 recorded-data run</td><td id="what-C">rides <span class="now">ride 3</span></td><td>Mon</td><td>~1 h<time data-eta="2026-10-26T10:00" hidden></time></td></tr>'
        '<tr><td>D</td><td>L2 build to JB</td><td id="what-D">later</td><td>Tue</td><td>ongoing</td></tr>'
        '<p>NEED: <span id="need"><b>Phone: unlock the iPhone; decide the glyph colours</b></span></p>'
        '<li data-ts="2026-10-26T09:10"><b>09:10</b> merged #510</li><li data-ts="2026-10-25T20:00"><b>20:00</b> old</li>')
fuelf = {"w1/control-room/fuel/state/aaaa1111-0000.json": {"role": "lean", "ctx": 190000, "at": at("2026-10-26T11:40"), "model": "claude-sonnet-5-5", "cwd": "/x/London Commuter"},
         "w1/control-room/fuel/state/bbbb2222-0000.json": {"role": "haiku", "ctx": 96000, "at": at("2026-10-26T11:44"), "model": "claude-haiku-5-5", "cwd": "/x"},
         "w1/control-room/fuel/state/cccc3333-0000.json": {"role": "coordinator", "ctx": 100000, "at": at("2026-10-26T11:30"), "model": "claude-opus-5-5", "cwd": "/x"},
         "w1/control-room/fuel/state/dddd4444-0000.json": {"role": "desktop", "ctx": 260000, "at": at("2026-10-26T10:00"), "model": "claude-opus-5-5", "cwd": "/x"},
         "w1/control-room/fuel/events.jsonl": json.dumps({"sid": "aaaa1111-0000", "ctx": 176000, "band": 1, "at": at("2026-10-26T11:20")}) + "\n",
         "ROADMAP_TO_BETA.html": road,
         "w1/chat/V3.json": {"msgs": [{"id": "abc12345", "from": "JB", "at": "2026-10-26T11:40:00", "text": "</script><script>alert(1)</script>", "pending": True},
                                       {"id": "abc12345r", "re": "abc12345", "from": "Claude", "at": "2026-10-26T11:41:00", "text": "<img src=x onerror=alert(2)>", "start_tokens": 21005}]},
         "w1/chat/../evil.json": {"msgs": []}, "K12-status.md": "x", "status/L2-F1-fix.md": "x"}
with tempfile.TemporaryDirectory() as tf:
    os.makedirs(os.path.join(tf, "status"), exist_ok=True); open(os.path.join(tf, "status", "V7-task.md"), "w").write("lane V7 status")
    htmlf, DF, jsf = build(tf, tasks, state, fuelf)
    if os.environ.get("KEEP_JSON"): open(os.environ["KEEP_JSON"], "w").write(jsf)  # for screenshots
    F = {r_["id"]: r_ for r_ in DF["fuel"]["sessions"]}
    check("fuel: lean Sonnet lane at 190k is 'start hand-over' on 175/200/225k", F.get("aaaa1111", {}).get("words") == "start hand-over" and F["aaaa1111"]["th"] == [175000, 200000, 225000], F.get("aaaa1111"))
    check("fuel: Haiku lane at 96k is STOP on 80/90/95k", F.get("bbbb2222", {}).get("words") == "STOP" and F["bbbb2222"]["th"] == [80000, 90000, 95000])
    check("fuel: coordinator at 100k is fine on 250/275/300k", F.get("cccc3333", {}).get("words") == "fine" and F["cccc3333"]["th"] == [250000, 275000, 300000] and F["cccc3333"]["role_txt"] == "coordinator")
    check("fuel: sessions idle over 30 min are left out", "dddd4444" not in F)
    check("fuel: worst band first, events carried", DF["fuel"]["sessions"][0]["id"] == "bbbb2222" and DF["fuel"]["events"] and DF["fuel"]["events"][0]["band"] == 1)
    K = DF["v2k"]
    check("V2 KPIs: plan steps done/total, no invented end", K["plan"]["done"] == 2 and K["plan"]["total"] == 4 and K["plan"]["eta"] == "" and K["plan"]["why"].startswith("no estimate") and "D" in K["plan"]["why"], K["plan"])
    check("V2 KPIs: gates from the roadmap step titles", (K["gates"]["L0"]["done"], K["gates"]["L0"]["total"], K["gates"]["L0"]["eta"]) == (1, 1, "done") and (K["gates"]["L2"]["done"], K["gates"]["L2"]["total"]) == (0, 2), K["gates"])
    check("V2 KPIs: a step past its planned finish is red with the reason", K["gates"]["L2"]["rag"] == "red" and next(x for x in DF["v2"]["steps"] if x["id"] == "C")["rag"] == "red" and "past its planned finish" in next(x for x in DF["v2"]["steps"] if x["id"] == "C")["why"])
    check("V2 KPIs: today's items = today's live updates + open items", K["today"]["done"] == 1 and K["today"]["total"] == 2 and K["today"]["eta"], K["today"])
    check("V2 KPIs: NEED count and items", K["need"]["n"] == 2 and K["need"]["rag"] == "amber", K["need"])
    check("chat: threads reach the page with start tokens; bad file names ignored", DF["chat"]["threads"]["V3"][1]["start"] == 21005 and set(DF["chat"]["threads"]) == {"V3"})
    check("chat: lanes come from status files, coordinators' K files excluded", {"L2-F1", "V7"} <= {l["id"] for l in DF["chat"]["lanes"]} and "K12" not in {l["id"] for l in DF["chat"]["lanes"]}, DF["chat"]["lanes"])
    check("chat text cannot close the script tag", htmlf.count("</script>") == 1 and "<img src=x" not in htmlf.replace(json.dumps("<img src=x onerror=alert(2)>")[1:-1], ""))
    check("page escapes chat text before showing it", "${esc(m.text)}" in PAGE and "${m.text}" not in PAGE)


# ---------- impact: critical path, slack, latest finish, holding up now; critical never amber ----------
def imp(tasks_, state_, files_=None, mtimes=None):
    with tempfile.TemporaryDirectory() as ti:
        _, Di, _ = build(ti, tasks_, state_, files_, mtimes=mtimes)
        return {t["id"]: t for t in Di["tasks"]}, Di
BR = lambda *ids: {"w1/jb_briefs.json": {"briefs": {i: {"title": "Do " + i, "why": "w", "steps": ["s"], "time": "5 min"} for i in ids}}}
# A: X1 (running since 10:00, 90 min estimate) -> X2 -> X3 all today = the week's chain; S is a 45 min side task
TA = [task("W1-71", "2026-10-26"), task("W1-72", "2026-10-26", depends=["W1-71"]), task("W1-73", "2026-10-26", depends=["W1-72"]),
      task("W1-79", "2026-10-26", model="haiku")]
IA, DA = imp(TA, {"W1-71": {"status": "doing", "opened": [{"at": "2026-10-26T10:00", "as": "sonnet/medium"}]}})
check("impact: the week's chain is critical with 0 slack", all(IA[i]["impact"]["critical"] and IA[i]["impact"]["slack_min"] == 0 for i in ("W1-71", "W1-72", "W1-73")), {i: IA[i]["impact"] for i in ("W1-72", "W1-73")})
s79 = IA["W1-79"]["impact"]
check("impact: a side task has positive slack and latest finish = week finish", (not s79["critical"]) and s79["slack_min"] == 145 and s79["latest_iso"] == "2026-10-26T14:55:00" and IA["W1-79"]["fin_iso"] == "2026-10-26T12:30:00", s79)
check("impact: unblocks counts all descendants and lists the nearest", IA["W1-71"]["impact"]["unblocks"] == 2 and [x["id"] for x in IA["W1-71"]["impact"]["next"]] == ["W1-72"] and IA["W1-71"]["impact"]["next"][0]["beg_iso"])
check("critical + past its estimate = red 'slack used up' (never amber)", IA["W1-71"]["rag"] == "red" and IA["W1-71"]["why"].startswith("Critical path: slack used up at Mon 11:30"), IA["W1-71"]["why"])
# B: JB task J1 ready, J2 (today) waits only on it -> holding now; on the GO-build chain -> red
TB = [task("W1-81", "2026-10-26", owner="JB", model="none", effort="none"), task("W1-82", "2026-10-26", depends=["W1-81"]),
      task("W1-83", "2026-10-26", depends=["W1-82"], gates=["GO-build"]),
      task("W1-85", "2026-10-26", owner="JB", model="none", effort="none"), task("W1-86", "2026-10-27", depends=["W1-85"])]
IB, DB = imp(TB, {}, BR("W1-81", "W1-85"))
check("impact: holding_now when a dependent is otherwise ready", IB["W1-81"]["impact"]["holding_now"] and IB["W1-81"]["impact"]["holding"] == ["W1-82"], IB["W1-81"]["impact"])
check("critical + holding up now = red naming the task", IB["W1-81"]["impact"]["critical"] and IB["W1-81"]["rag"] == "red" and "holding up W1-82 now" in IB["W1-81"]["why"], (IB["W1-81"]["rag"], IB["W1-81"]["why"]))
k = IB["W1-85"]
check("critical with slack left stays amber and carries when it turns red", k["impact"]["critical"] and not k["impact"]["holding_now"] and k["rag"] == "amber" and k["impact"]["red_iso"] == "2026-10-27T07:45:00" and k["impact"]["slack_min"] > 0, (k["rag"], k["why"], k["impact"]))
check("holding_now is false when the dependent is planned for a later day", not IB["W1-85"]["impact"]["holding_now"])
# C: a JB task with no brief is amber 'Brief missing'; a lane whose run ended long ago with no live process is red, owner Claude
TC = [task("W1-91", "2026-10-26", owner="JB", model="none", effort="none"), task("W1-92", "2026-10-26"), task("W1-93", "2026-10-28", depends=["W1-92"]), task("W1-94", "2026-10-28", depends=["W1-93"])]
IC_, _ = imp(TC, {"W1-92": {"status": "doing", "opened": [{"at": "2026-10-26T11:00", "as": "sonnet/medium"}]}},
             {"w1/evidence/lane-W1-92.json": {"session_id": "s92", "result": "ok", "num_turns": 3, "total_cost_usd": 0.1}}, {"w1/evidence/lane-W1-92.json": "2026-10-26T11:06"})
check("JB task without a brief is amber 'Brief missing'", IC_["W1-91"]["rag"] == "amber" and IC_["W1-91"]["why"] == "Brief missing" and IC_["W1-91"]["brief_missing"])
check("stale lane run is red 'Last run ended … not restarted', owner Claude", IC_["W1-92"]["rag"] == "red" and "Last run ended 11:06; Claude has not restarted it" in IC_["W1-92"]["why"] and IC_["W1-92"]["act"]["kind"] == "Claude", IC_["W1-92"]["why"])
check("owner line: Claude tasks never read as an ask to JB", all(t["act"]["kind"] == "Claude" and not t["act"]["doing"].lower().startswith("coordinator") for t in IC_.values() if t["owner"] != "JB") and IC_["W1-91"]["act"]["kind"] == "JB")

# ---------- iPhone alert: green / amber / red, the free state, the Mac recording the PHONE FREE issue ----------
ST_A = {"W1-71": {"status": "doing", "opened": [{"at": "2026-10-26T10:00", "as": "sonnet/medium"}]}}
def phn(tasks_, files_=None, state_=None):
    with tempfile.TemporaryDirectory() as tp:
        _, Dp, _ = build(tp, tasks_, state_ if state_ is not None else ST_A, files_); return Dp["phone"]
P0 = phn(TA)
check("phone: green and no tasks when nothing needs the phone", P0["state"] == "green" and P0["tasks"] == [] and P0["free"] is None, P0)
TP1 = [dict(t, where="iPhone via Mirroring") if t["id"] == "W1-79" else t for t in TA]
P1 = phn(TP1)
check("phone: amber when a non-critical task needs the phone (id, title, why, minutes listed)", P1["state"] == "amber" and [x["id"] for x in P1["tasks"]] == ["W1-79"] and not P1["tasks"][0]["critical"] and P1["tasks"][0]["min"] == 45 and P1["tasks"][0]["why"], P1)
TP2 = [dict(t, title="Install the build on the phone") if t["id"] == "W1-73" else t for t in TA]
P2 = phn(TP2)
check("phone: red when a critical phone task is due within 24 h (title says install)", P2["state"] == "red" and P2["tasks"][0]["id"] == "W1-73" and P2["tasks"][0]["critical"] and P2["tasks"][0]["due24"], P2)
TP3 = [dict(t, owner="QUEUE", model="none", effort="none", steps=["run the queue with targets install"]) if t["id"] == "W1-79" else t for t in TA]
check("phone: a QUEUE task with install targets needs the phone; a done one does not", phn(TP3)["state"] == "amber" and phn(TP1, None, dict(ST_A, **{"W1-79": {"status": "done", "updated": "2026-10-26T09:00"}}))["state"] == "green")
P4 = phn(TA, {"v2/owed_phone_checks.txt": "# owed\nL2 ride check on the 08:14 | the tunnel case was never seen on a real phone | 20\nW1-79\n"})
check("phone: V2 owed checks count as phone tasks (programme V2); a task id line marks that task", P4["state"] == "amber" and {x["id"] for x in P4["tasks"]} == {"V2-P1", "W1-79"} and next(x for x in P4["tasks"] if x["id"] == "V2-P1")["programme"] == "V2" and next(x for x in P4["tasks"] if x["id"] == "V2-P1")["min"] == 20, P4)
P5 = phn(TP1, {"w1/phone_free.json": {"at": "2026-10-26T11:30", "by": "JB", "until": "2026-10-26T13:30"}})
check("phone: free after phone_free.json (active, since 11:30)", P5["free"] and P5["free"]["active"] and P5["free"]["since"] == "11:30", P5)
P6 = phn(TP1, {"w1/phone_free.json": {"at": "2026-10-26T08:30", "by": "JB", "until": "2026-10-26T10:30"}})
check("phone: free note past its until is not active", P6["free"] and not P6["free"]["active"], P6)
P7 = phn(TP1, {"w1/phone_free.json": {"at": "2026-10-26T11:00", "by": "JB", "until": "2026-10-26T13:00"}, "testq/results/j1.txt": "job=j1 targets=install overall=pass\n"})
check("phone: free note ends when a phone job ran after it", P7["free"] and not P7["free"]["active"], P7)
check("page: chip is a button when amber/red, panel lists tasks with one 'iPhone is free and connected' action posting PHONE FREE",
      all(x in PAGE for x in ("iPhone: not needed", "iPhone needed", "iPhone is free and connected", 'title="PHONE FREE"', "data-phone", "iPhone free (since")))

with tempfile.TemporaryDirectory() as tm:
    hm_ = os.path.join(tm, "home"); os.makedirs(hm_); bind = os.path.join(tm, "bin"); os.makedirs(bind)
    gl = os.path.join(tm, "gh.log")
    issues = [{"number": 31, "title": "PHONE FREE", "body": "x", "author": {"login": "multisportusa-git"}},
              {"number": 32, "title": "PHONE FREE", "body": "x", "author": {"login": "mallory"}}]
    open(os.path.join(bind, "gh"), "w").write("#!/bin/bash\necho \"$@\" >> '%s'\nif [ \"$1 $2\" = 'issue list' ]; then cat '%s/issues.json'; fi\n" % (gl, tm))
    os.chmod(os.path.join(bind, "gh"), 0o755); json.dump(issues, open(os.path.join(tm, "issues.json"), "w"))
    r_ = subprocess.run([sys.executable, os.path.join(HERE, "decisions_sync.py"), "--notes", tm, "--repo", "multisportusa-git/lc-control-room"], capture_output=True, text=True,
                        env=dict(os.environ, HOME=hm_, PATH=bind + os.pathsep + os.environ["PATH"]), timeout=60)
    pf = os.path.join(tm, "w1", "phone_free.json"); pj = json.load(open(pf)) if os.path.exists(pf) else {}
    a_ = datetime.fromisoformat(pj["at"]) if pj else None; u_ = datetime.fromisoformat(pj["until"]) if pj else None
    check("sync: owner's PHONE FREE issue writes phone_free.json {at, by JB, until = at + 2 h}", r_.returncode == 0 and pj.get("by") == "JB" and u_ and (u_ - a_).total_seconds() == 7200, (r_.stdout, r_.stderr, pj))
    acts_ = open(os.path.join(tm, "w1", "ACTIONS-from-JB.md")).read() if os.path.exists(os.path.join(tm, "w1", "ACTIONS-from-JB.md")) else ""
    check("sync: appends one 'PHONE FREE' line to ACTIONS-from-JB.md", acts_.count("PHONE FREE") == 1 and "issue #31" in acts_ and "#32" not in acts_, acts_)
    glog = open(gl).read()
    check("sync: owner's issue is closed with a comment; another account's is closed and ignored", "issue close 31" in glog and "issue close 32" in glog and "Only the repo owner" in glog)

# ---------- chat acting runs: chip states reach the page; the acting-run tests ----------
with tempfile.TemporaryDirectory() as ta:
    _, DA_, _ = build(ta, [task("W1-1", "2026-10-26")], {}, {"w1/chat/V3.json": {"msgs": [
        {"id": "m1", "from": "JB", "at": "2026-10-27T04:21:00", "text": "go", "act": {"state": "acting", "lane": "Cct00001", "behind": ""}},
        {"id": "m1a", "fu": "m1", "from": "Claude (acting)", "at": "2026-10-27T04:30:00", "text": "DONE x\n- PR #1"}]}})
    th_ = DA_["chat"]["threads"]["V3"]
    check("chat: act state and the 'Claude (acting)' follow-up reach the page data", th_[0]["act"]["state"] == "acting" and th_[1]["acting"] is True and th_[1]["fu"] == "m1" and th_[1]["from"] == "Claude", th_)
check("page: chip moves Answered -> Acting… -> Done / Blocked, queued shows behind; open acts keep polling",
      all(x in PAGE for x in ('answered:"Answered"', 'acting:"Acting…"', 'done:"Done"', 'blocked:"Blocked"', 'queued:"Queued"')) and "actChip(m.act)" in PAGE and "actOpen(m)" in PAGE and ".achip.done" in PAGE and ".achip.blocked" in PAGE)
check("page: follow-up text is escaped and labelled", 'class="bwho">Claude (acting)' in PAGE and "${esc(m.text)}" in PAGE)
r_ = subprocess.run([sys.executable, os.path.join(HERE, "test_chat_act.py")], capture_output=True, text=True, timeout=300)
print("\n".join(l for l in r_.stdout.splitlines() if l.startswith("FAIL")))
check("chat acting-run tests (gen/test_chat_act.py) all pass", r_.returncode == 0, r_.stdout[-600:] + r_.stderr[-300:])

# ---------- Your-asks filters survive the background reload (JB 28 Oct 16:23, A93) ----------
def js_line(prefix):
    i = PAGE.index("\n" + prefix) + 1; return PAGE[i:PAGE.index("\n", i)]
if NODE:
    harness = ("let store={},hash='';const window={};const LS={get(k,d){return k in store?JSON.parse(store[k]):d},set(k,v){store[k]=JSON.stringify(v)}};"
               "let H={};function setH(k,v){if(v==null)delete H[k];else H[k]=v;hash=new URLSearchParams(H).toString()}"
               + js_line("function askF(){") + "\n" + js_line("function askFSave(F){") + "\n"
               "const reload=(keepHash)=>{delete window._askF;H=keepHash?Object.fromEntries(new URLSearchParams(hash)):{};};const out=[];"
               "let F=askF();F.cat='Your decisions';F.rags=['red'];F.sts=['WORKING','BLOCKED'];askFSave(F);"
               "reload(true);out.push(JSON.stringify(askF()));"
               "reload(false);out.push(JSON.stringify(askF()));"
               "F=askF();F.cat='';F.rags=[];F.sts=[];askFSave(F);reload(true);out.push(JSON.stringify(askF())+'|'+hash);"
               "store['w1dash.askf']='{\"cat\":5,\"rags\":\"x\",\"sts\":[1,\"DONE\"]}';H={askf:'{bad'};delete window._askF;out.push(JSON.stringify(askF()));"
               "console.log(out.join('\\n'))")
    r_ = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    o_ = r_.stdout.strip().split("\n")
    want = '{"cat":"Your decisions","rags":["red"],"sts":["WORKING","BLOCKED"]}'
    check("asks filters: restored from the URL after a background reload", len(o_) == 4 and o_[0] == want, (o_, r_.stderr))
    check("asks filters: restored from localStorage when the URL has none", len(o_) == 4 and o_[1] == want, o_)
    check("asks filters: clearing them clears both stores", len(o_) == 4 and o_[2] == '{"cat":"","rags":[],"sts":[]}|', o_)
    check("asks filters: bad saved values fall back safely", len(o_) == 4 and o_[3] == '{"cat":"","rags":[],"sts":["DONE"]}', o_)
check("page: asks draw and click handler read askF(), the click saves with askFSave", PAGE.count("const F=askF();") == 2 and "askFSave(F);rerender(\"asks\")" in PAGE and "window._askF||(window._askF=" not in PAGE)

# A97 (JB 28 Oct 17:16): a task a live lane holds is running, not "not started"; running test jobs are live runner rows
with tempfile.TemporaryDirectory() as tmpR:
    os.makedirs(os.path.join(tmpR, "v2", "locks", "lane-W197"))
    _, DR, _ = build(tmpR, [task("W1-97", "2026-10-26"), task("W1-98", "2026-10-26", owner="QUEUE")], {}, {
        "status/W197.txt": "running w1-97\n", "v2/sup/lane_eta.json": {"W197": {"eta": "2026-10-26T12:30"}},
        "testq/runners/r1.status": "r1 2026-10-26 11:30:05 5-20261026-112900-W197-abcdef12-1 running unit\n", "testq/runners/r1.lock": "",
        "testq/runners/r2.status": "r2 2026-10-26 11:00:00 old-job running unit\n", "testq/runners/r2.lock": "",
        "testq/running/5-20261026-112900-W197-abcdef12-1.job": "lane=W197\nsha=abcdef1234\ntargets=unit snapshot\n"}, mtimes={"status/W197.txt": "2026-10-26T11:20"})
    TR = {t["id"]: t for t in DR["tasks"]}
    check("live lane: its plan task reads 'running' with the lane's ETC, never 'not started'", "not started" not in TR["W1-97"]["why"] and "running" in TR["W1-97"]["why"] and "12:30" in TR["W1-97"]["why"] and TR["W1-97"]["lane_running"], TR["W1-97"]["why"])
    check("live lane: its plan task's status is 'doing' (board Now, timeline active), never 'todo' (A98)", TR["W1-97"]["status"] == "doing", TR["W1-97"]["status"])
    LRr = DR.get("live_runners") or []
    check("live runners: only a runner whose job is in testq/running is live", [x["runner"] for x in LRr] == ["r1"] and LRr[0]["lane"] == "W197" and LRr[0]["est"] == "12:20", LRr)
    check("live runners: planned queue work starts after the live job frees its runner or on the idle one", TR["W1-98"]["beg_iso"][11:16] in ("11:45", "12:20"), TR["W1-98"]["beg_iso"])
check("page: timeline draws live runner rows and labels plan rows 'Planned runner'", 'owner:"RUNNER"' in PAGE and '"Planned runner "+n' in PAGE)

print("ALL PASS" if not FAILS else "%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
