#!/usr/bin/env python3
"""Tests for the Control Room chat's acting runs (gen/chat_sync.py store / act-run, mac/chat_act.sh, the page chip).
A fake lane.sh stands in for the LC repo's scripts/lane.sh; no model is called. Run: python3 gen/test_chat_act.py"""
import json, os, re, stat, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []
def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok: FAILS.append(name)

with tempfile.TemporaryDirectory() as tc:
    home = os.path.join(tc, "home"); os.makedirs(home); lc = os.path.join(tc, "lc"); os.makedirs(os.path.join(lc, "scripts"))
    fake = os.path.join(tc, "lane.sh"); calls = os.path.join(tc, "lane_calls.log")
    open(fake, "w").write('''#!/bin/bash
echo "$@" >> "%s"; cp "${@: -1}" "%s/brief_$1.md"
mkdir -p "$LC_NOTES/status" "$LC_NOTES/v2/lanes"
echo '{"branch":"v2/'$1'-x","pr":"512","job":"j-77"}' > "$LC_NOTES/v2/lanes/$1.json"
printf '%%s\\n%%s\\n' "${FAKE_STATE:-done}" "${FAKE_SIG:-DONE started W1-13 job j-77}" > "$LC_NOTES/status/$1.txt"
exit ${FAKE_RC:-0}
''' % (calls, tc)); os.chmod(fake, 0o755)
    env = dict(os.environ, HOME=home)
    common = ["--notes", tc, "--no-spawn", "--lane-sh", fake, "--lc-repo", lc]
    def cs(*a, **e): return subprocess.run([sys.executable, os.path.join(HERE, "chat_sync.py"), *a, *common], capture_output=True, text=True, env=dict(env, **e), timeout=60)
    def thread(t="V3"):
        try: return json.load(open(os.path.join(tc, "w1", "chat", t + ".json")))["msgs"]
        except OSError: return []
    def put(msgs, t="V3"):
        os.makedirs(os.path.join(tc, "w1", "chat"), exist_ok=True); json.dump({"target": t, "msgs": msgs}, open(os.path.join(tc, "w1", "chat", t + ".json"), "w"))
    def jb(cid, text): return {"id": cid, "from": "JB", "at": "2026-10-27T04:21:00", "text": text, "pending": True}
    def answer(cid, result, t="V3"):
        rd = os.path.join(tc, "w1", "chat", "run", cid); os.makedirs(rd, exist_ok=True)
        json.dump({"gen": "2026-10-27T04:20", "tasks": [{"id": "W1-13", "title": "Queue the L2 rides", "status": "todo", "rag": "amber", "why": "waits on the phone"},
                                                          {"id": "W1-99", "title": "unrelated", "status": "todo"}]}, open(os.path.join(rd, "context.json"), "w"))
        out = os.path.join(rd, "out.json")
        open(out, "w").write(json.dumps({"type": "result", "is_error": False, "result": result, "modelUsage": {"claude-sonnet-5-5": {}}}) + "\n")
        r = cs("store", t, cid, out); assert r.returncode == 0, r.stderr
        return rd
    nlanes = lambda: len(open(calls).read().splitlines()) if os.path.exists(calls) else 0

    # 1. answer-only: no act, no action run, no chip
    put([jb("ans00001", "Where is W1-13?")])
    rd = answer("ans00001", "W1-13 is waiting on the phone.\nINTENT: answer")
    m = thread(); ans = [x for x in m if x.get("re") == "ans00001"]
    check("answer-only: INTENT line is stripped, no act state, no act.txt", ans and "INTENT" not in ans[0]["text"] and not m[0].get("act") and not os.path.exists(os.path.join(rd, "act.txt")), m)
    check("answer-only: no action run is started", nlanes() == 0 and not os.path.exists(os.path.join(tc, "w1", "ACTIONS-from-JB.md")))
    # a message with no INTENT line at all is an answer
    put([jb("ans00002", "hmm")]); rd = answer("ans00002", "Not sure what you mean.")
    check("no INTENT line counts as answer", not thread()[0].get("act") and not os.path.exists(os.path.join(rd, "act.txt")))

    # 2. act: exactly one run, brief carries the request, ACTIONS line says started
    REQ = "EXECUTE NOW: start W1-13"
    put([jb("act00001", REQ)])
    rd = answer("act00001", "I will start W1-13 now.\n```record\n{\"type\": \"note\", \"text\": \"start W1-13\"}\n```\nINTENT: act start W1-13")
    m = thread(); me = m[0]
    check("act: chip state acting, lane id C+short cid, act.txt written", me["act"]["state"] == "acting" and me["act"]["lane"] == "Cct00001" and open(os.path.join(rd, "act.txt")).read().strip() == "Cct00001", me.get("act"))
    brief = open(me["act"]["brief"]).read()
    check("brief holds the request verbatim, target, time, W1-13 row, rules, last-line contract, <= 8000 bytes",
          REQ in brief and "Target: V3" in brief and "Time now:" in brief and "Queue the L2 rides" in brief and "unrelated" not in brief and "rule 0" in brief and "the train" in brief
          and "phone-free" in brief and "act_on_decided.sh need" in brief and "DONE <" in brief and "BLOCKED <" in brief and len(brief.encode()) <= 8000, brief)
    acts = open(os.path.join(tc, "w1", "ACTIONS-from-JB.md")).read()
    check("ACTIONS-from-JB.md line says 'started action run C<cid>'", "started action run Cct00001" in acts, acts)
    check("answer text says it is acting", "Acting: started action run Cct00001" in [x for x in m if x.get("re") == "act00001"][0]["text"])
    check("store itself does not run lane.sh (chat_reply.sh starts chat_act.sh)", nlanes() == 0)

    # 3. a second act while one runs: queued, not started
    put(thread() + [jb("act00002", "EXECUTE NOW: run the L2 rides")])
    rd2 = answer("act00002", "Queued.\nINTENT: act run the L2 rides")
    m = thread(); me2 = next(x for x in m if x["id"] == "act00002")
    q = [x for x in m if x.get("id") == "act00002q"]
    check("second act is queued behind the first, no act.txt, a note in the thread", me2["act"]["state"] == "queued" and me2["act"]["behind"] == "Cct00001" and not os.path.exists(os.path.join(rd2, "act.txt"))
          and q and "Queued behind Cct00001" in q[0]["text"], (me2.get("act"), q))
    check("queued request is logged as queued in ACTIONS-from-JB.md", "queued behind Cct00001" in open(os.path.join(tc, "w1", "ACTIONS-from-JB.md")).read())
    # another target is independent
    put([jb("lane0001", "EXECUTE NOW: restart it")], "L-F1")
    rd3 = answer("lane0001", "ok\nINTENT: act restart", "L-F1")
    check("one action per target: another target starts its own", os.path.exists(os.path.join(rd3, "act.txt")))

    # 4. act-run: lane.sh args, follow-up with DONE + evidence, chip done, queued one is promoted
    r = cs("act-run", "V3", "act00001", FAKE_STATE="done", FAKE_SIG="DONE started W1-13 as job j-77")
    check("act-run exits 0", r.returncode == 0, r.stdout + r.stderr)
    line = open(calls).read().splitlines()
    check("lane.sh gets lane, chat-<cid>, --role coord --model sonnet --effort medium --brief <file>; no dangerous flag",
          len(line) == 1 and line[0].startswith("Cct00001 chat-act00001 --role coord --model sonnet --effort medium --brief ") and line[0].endswith("brief.md") and "dangerously" not in line[0], line)
    m = thread(); fu = next(x for x in m if x.get("id") == "act00001a")
    check("follow-up from 'Claude (acting)' carries the DONE line and evidence (<= 5)", fu["from"] == "Claude (acting)" and fu["text"].startswith("DONE started W1-13 as job j-77") and "PR #512" in fu["text"] and "job j-77" in fu["text"]
          and len(fu["text"].splitlines()) <= 6 and not fu.get("error"), fu["text"])
    check("chip moves to done", m[0]["act"]["state"] == "done")
    me2 = next(x for x in m if x["id"] == "act00002")
    check("the queued request is promoted to acting when the first ends", me2["act"]["state"] == "acting" and me2["act"]["lane"] == "Cct00002", me2["act"])

    # 5. BLOCKED, parked and missing status
    r = cs("act-run", "V3", "act00002", FAKE_STATE="blocked", FAKE_SIG="BLOCKED the phone is locked; JB must unlock the iPhone")
    fu = next(x for x in thread() if x.get("id") == "act00002a"); me2 = next(x for x in thread() if x["id"] == "act00002")
    check("BLOCKED follow-up is flagged and the chip says blocked", fu["text"].startswith("BLOCKED the phone is locked") and fu["error"] and me2["act"]["state"] == "blocked", (fu, me2["act"]))
    put([jb("park0001", "EXECUTE NOW: x")]); answer("park0001", "go\nINTENT: act x")
    os.remove(fake); open(fake, "w").write("#!/bin/bash\nexit 75\n"); os.chmod(fake, 0o755)
    cs("act-run", "V3", "park0001"); fu = next(x for x in thread() if x.get("id") == "park0001a")
    check("a parked lane (exit 75, no status file) reports BLOCKED, not silence", fu["text"].startswith("BLOCKED") and "parked" in fu["text"], fu["text"])

    # 6. secrets never reach the thread
    put([jb("sec00001", "do x")]); answer("sec00001", "go\nINTENT: act x")
    open(fake, "w").write('#!/bin/bash\nmkdir -p "$LC_NOTES/status"; printf "done\\nDONE ok token=abc123secret\\n" > "$LC_NOTES/status/$1.txt"\n')
    cs("act-run", "V3", "sec00001"); check("secrets in the signal are redacted", "abc123secret" not in next(x for x in thread() if x["id"] == "sec00001a")["text"])

    # 7. stale acting is closed by sync's sweep
    import importlib.util
    put([dict(jb("old00001", "x"), pending=False, act={"state": "acting", "lane": "Cold0000", "at": "2026-10-26T01:00:00"}, at="2026-10-26T01:00:00")])
    open(os.path.join(tc, "issues.json"), "w").write("[]"); bd = os.path.join(tc, "bin"); os.makedirs(bd)
    open(os.path.join(bd, "gh"), "w").write("#!/bin/sh\ncat '%s'\n" % os.path.join(tc, "issues.json")); os.chmod(os.path.join(bd, "gh"), 0o755)
    cs("--key", os.path.join(tc, "nokey"), PATH=bd + os.pathsep + os.environ["PATH"])
    check("an action that never reported is closed as blocked", thread()[0]["act"]["state"] == "blocked" and any(x.get("id") == "old00001a" for x in thread()))

    # 8. scripts
    sh = open(os.path.join(HERE, "..", "mac", "chat_act.sh")).read(); rp = open(os.path.join(HERE, "..", "mac", "chat_reply.sh")).read()
    code = lambda t: "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    check("scripts: no --dangerously-skip-permissions in code; chat_reply starts chat_act only when act.txt exists", "dangerously" not in code(sh) + code(rp) + code(open(os.path.join(HERE, "chat_sync.py")).read())
          and "-s \"$W/act.txt\"" in rp and "chat_act.sh" in rp)
    check("chat_act.sh is valid bash", subprocess.run(["bash", "-n", os.path.join(HERE, "..", "mac", "chat_act.sh")]).returncode == 0)

print("ALL PASS" if not FAILS else "%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
