#!/usr/bin/env python3
"""Controller chat simulations (JB 28 Oct 11:12: "use the same communication to create 5 simulation conversations to ensure the
coordinator responds accordingly, in a timely manner, and does not give 'I am not sure, I've read it'. I expect action not shoulder shrugs").

Each conversation gets a fresh temp chat dir and asks dir, so the real threads (<notes>/w1/chat) and the real asks tracker
(<notes>/w1/asks.json) are never touched. Every message goes through the real path:
  chat_sync.py plan COORD <cid> <rundir> --chat-dir X   ->  claude exactly as mac/chat_reply.sh runs it  ->  chat_sync.py store ... --chat-dir X
No action runs, except conversation 5: ONE real action run for the W1-40 message (chat_sync.py act-run, LC_CTL_SIM=1: ctl.sh refuses
anything that changes lanes, jobs or the phone, and the run gets no Write/Edit), to prove the action path and its toolkit end to end.

  sim_controller.py [--convs 1,2,3,4,5] [--par 2] [--report PATH]
Results accumulate in <notes>/v2/sup/sim/results.json (re-running a conversation replaces its rows); the table is written to the report.
"""
import concurrent.futures as cf, datetime as dt, glob, importlib.util, json, os, random, re, shutil, string, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
HOME = os.path.expanduser("~")
NOTES = os.path.join(HOME, "Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08")
CS = os.path.join(HERE, "chat_sync.py")
PY = sys.executable
A = sys.argv[1:]
def arg(k, d): return A[A.index(k) + 1] if k in A else d
REPORT = arg("--report", os.path.join(NOTES, "v2", "sup", "SIM_CONTROLLER_2026-10-28.md"))
RES = os.path.join(NOTES, "v2", "sup", "sim", "results.json")

MSGS = {
    1: "CONFIRMING THAT THE WATCHDOG TERMINAL SCRIPT HAS RAN AND WATCHDOG IS ON.  PLEASE PROVIDE CURRENT STATUS OF ALL FOUR LANES AFTER YOU CONFIRM AND HAVE ENSURED THAT ALL LANES ARE ACTIVELY WORKING.  PROVIDE ME A BREAKOUT OF THE CHANGES FROM THE COMMUTE FEEDBACK PROVIDED YESTERDAY.  I WANT THIS DONE ASAP AND A NEW INSTALL ON MY IPHONE ASAP.",
    2: "IPHONE IS CONNECTED.  RUN APPIUM AND CONFIRM CONNECTION.  RUN MERGE AND BUILD.  CREATE A NEW SECTION IN THE LC CONTROL ROOM, PLACE AT TOP (ABOVE YOUR DECISIONS)...ANY QUESTION/ASK GETS ADDED WITH A CURRENT STATE (STAGED, WORKING, PARTIALLY DONE, DONE) WITH ESTIMATED TIME TO COMPLETION. PROVIDE INFOGRAPHIC TO COMMUNICATE STATUS OF EACH CATEGORY, PROVIDE INFOGRAPHIC OF OVERALL LC CONTROL ROOM WORK GREEN, AMBER, RED WITH DRILL DOWN AND IN BOLD HEADER AS OF TIME.  CHANGE: WHEN I AM TYPING HERE IN THE SESSION BLOCK AND THE LC CONTROL ROOM ROUTINE SCRIPT RUNS, THE WHOLE SCREEN BLINKS AND MY CURSOR GOES TO THE VERY BEGINNING OF THE CHAT SESSION. THIS ABSOLUTELY NEEDS TO BE FIXED IMMEDIATELY.",
    3: "IF THERE IS A LANE AVAILABLE; TAKE IT AND ESCALATE MY PREVIOUS TEXTS AS P0; PAUSE NON P0 WORK AND MOVE MINE AHEAD.  STANDING RULE.",
    4: "WHAT'S THE ISSUE WITH W1-40?  COORDINATOR ACTION, GET RESOLUTION AND GET IT DONE (ESCALATE MODEL LEVEL IF NECESSARY)",
}
CONVS = {1: [1, 2, 3, 4], 2: [1, 2, 3, 4], 3: [1, 2, 3, 4], 4: [1, 2, 3, 4], 5: [1, 2, 4]}
MIN_ASKS = {1: 3, 2: 3, 3: 1, 4: 1}
LAT_MAX, ACT_MAX = 90, 25 * 60

spec = importlib.util.spec_from_file_location("chat_sync_mod", CS); _argv = sys.argv; sys.argv = [CS]
CSM = importlib.util.module_from_spec(spec); spec.loader.exec_module(CSM); sys.argv = _argv
EXTRA = re.compile(r"(?i)i'?ve\s+read|\bread\s+it\b|\bnot\s+sure\b")
FACT = re.compile(r"#\d{2,}|\b\d+(?:\.\d+)?\s?%")
CL = (sorted(glob.glob(os.path.join(HOME, ".local/share/claude/versions/*")), key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", os.path.basename(p))]) or [os.path.join(HOME, ".local/bin/claude")])[-1]

def now(): return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
def cid_for(c, i): return "s%dm%d%s" % (c, i, "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8)))
def shrugs(t): return len(CSM.SHRUG.findall(t or "")) + len(EXTRA.findall(t or ""))
def queue_snapshot():
    q = os.path.join(NOTES, "v2", "queue")
    return sorted(os.path.relpath(p, q) for p in glob.glob(os.path.join(q, "**", "*"), recursive=True))

def add_msg(cd, cid, text):
    p = os.path.join(cd, "COORD.json")
    try: d = json.load(open(p))
    except (OSError, ValueError): d = {"target": "COORD", "msgs": []}
    d["msgs"].append({"id": cid, "from": "JB", "at": now(), "text": text, "depth": "auto", "issue": 0, "pending": True})
    json.dump(d, open(p, "w"), indent=1)

def finish_prior_acts(cd, note):
    """conversation 5: earlier messages' actions are not run in a simulation; mark them done so the W1-40 action takes a slot."""
    p = os.path.join(cd, "COORD.json"); d = json.load(open(p))
    for x in d["msgs"]:
        if (x.get("act") or {}).get("state") in ("acting", "queued", "answered"): x["act"]["state"] = "done"; x["act"]["sim"] = note
    json.dump(d, open(p, "w"), indent=1)

def env_for(an): return dict(os.environ, LC_ASKS_NOTES=an)

def run_answer(conv, mi, cd, an, clog):
    cid = cid_for(conv, mi); add_msg(cd, cid, MSGS[mi]); rd = os.path.join(cd, "run", cid)
    common = ["--chat-dir", cd, "--asks-notes", an, "--coord-log", clog, "--no-spawn"]
    r = subprocess.run([PY, CS, "plan", "COORD", cid, rd, *common], env=env_for(an), capture_output=True, text=True, timeout=200)
    if r.returncode != 0: return {"conv": conv, "msg": mi, "cid": cid, "error": "plan failed: " + (r.stderr or r.stdout)[-300:]}
    rf = lambda f: open(os.path.join(rd, f)).read()
    cmd = ["/usr/bin/perl", "-e", "alarm 300; exec @ARGV", CL, "-p", rf("prompt.txt"), "--model", rf("model.txt").strip(),
           "--setting-sources", "project,local", "--settings", os.path.join(rd, "settings.json"), "--strict-mcp-config", "--mcp-config", os.path.join(rd, "mcp.json"),
           "--tools", "Read,Grep,Glob,Bash", "--disable-slash-commands", "--allowedTools", rf("tools.txt"), "--permission-mode", "default",
           "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
    t0 = time.time()
    with open(os.path.join(rd, "out.json"), "w") as fo, open(os.path.join(rd, "err.txt"), "w") as fe:
        subprocess.run(cmd, cwd=os.path.join(rd, "cwd"), stdout=fo, stderr=fe)
    lat = round(time.time() - t0, 1)
    o, start = CSM.read_out(os.path.join(rd, "out.json"))
    raw = str(o.get("result") or "")
    if conv == 5 and mi == 4: finish_prior_acts(cd, "not run in the simulation")
    r = subprocess.run([PY, CS, "store", "COORD", cid, os.path.join(rd, "out.json"), *common], env=env_for(an), capture_output=True, text=True, timeout=120)
    th = json.load(open(os.path.join(cd, "COORD.json")))["msgs"]
    rep = next((x for x in th if x.get("re") == cid), {}); me = next((x for x in th if x.get("id") == cid), {})
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    m2 = re.search(r"```asks\s*(\[.*?\])\s*```", raw, re.S)
    try: na = len([x for x in json.loads(m2.group(1)) if isinstance(x, dict) and x.get("title")]) if m2 else 0
    except ValueError: na = -1
    body = re.sub(r"```asks.*?```", "", raw, flags=re.S)
    res = {"conv": conv, "msg": mi, "cid": cid, "rundir": rd, "latency": lat, "model": rf("model.txt").strip(), "cost": o.get("total_cost_usd"),
           "error": bool(o.get("is_error")) or not raw, "shrug_raw": shrugs(body), "shrug_stored": shrugs(rep.get("text", "")),
           "doing_now": "doing now" in raw.lower(), "eta": bool(re.search(r"ETA\W{0,4}\d{1,2}:\d\d", raw)),
           "intent": (lines[-1] if lines else "")[:60], "intent_act": bool(lines) and lines[-1].upper().startswith("INTENT: ACT"),
           "asks": na, "facts": len(FACT.findall(body)), "act_state": (me.get("act") or {}).get("state", ""), "store_rc": r.returncode,
           "reply": (rep.get("text") or "")[:1500]}
    res["pass"] = (not res["error"] and res["latency"] <= LAT_MAX and res["shrug_raw"] == 0 and res["shrug_stored"] == 0 and res["doing_now"] and res["eta"]
                   and res["intent_act"] and res["asks"] >= MIN_ASKS[mi] and res["facts"] >= 1 and r.returncode == 0)
    return res

def run_action(conv, cid, cd, an, clog):
    before = queue_snapshot(); t0 = time.time()
    e = dict(env_for(an), LC_CTL_SIM="1")
    r = subprocess.run([PY, CS, "act-run", "COORD", cid, "--chat-dir", cd, "--asks-notes", an, "--coord-log", clog, "--no-spawn"], env=e,
                       capture_output=True, text=True, timeout=ACT_MAX + 120)
    dur = round(time.time() - t0, 1)
    th = json.load(open(os.path.join(cd, "COORD.json")))["msgs"]
    fu = next((x for x in th if x.get("id") == cid + "a"), {})
    rd = os.path.join(cd, "run", cid); jl = os.path.join(rd, "act.jsonl")
    o, _ = CSM.read_out(jl)
    ctl_calls, denied = [], 0
    try:
        for ln in open(jl):
            try: ev = json.loads(ln)
            except ValueError: continue
            msg = ev.get("message") if isinstance(ev, dict) else None
            for c in (msg.get("content") or []) if isinstance(msg, dict) and isinstance(msg.get("content"), list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash" and "ctl.sh" in str((c.get("input") or {}).get("command", "")):
                    ctl_calls.append(str(c["input"]["command"]).split("ctl.sh", 1)[1].strip()[:60])
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error") and re.search(r"(?i)permission|approval|blocked", str(c.get("content", ""))): denied += 1
    except OSError: pass
    line = (fu.get("text") or "").splitlines()[0] if fu.get("text") else ""
    res = {"conv": conv, "msg": "4-act", "cid": cid, "duration": dur, "line": line[:400], "ctl_calls": ctl_calls, "denied": denied,
           "cost": o.get("total_cost_usd"), "summary": str(o.get("result") or "")[-700:], "rc": r.returncode, "stderr": (r.stderr or "")[-300:], "queue_unchanged": queue_snapshot() == before}
    res["pass"] = (bool(re.match(r"^(DONE|PARTIAL|NEEDS JB)\b", line)) and bool(ctl_calls) and dur <= ACT_MAX and res["queue_unchanged"] and r.returncode == 0)
    return res

def run_conv(conv):
    # under <notes>/v2/sup/sim/ (not /tmp): the action run's allowed directories include <notes>, so it can read its own sim log.
    # Still separate from the real threads (<notes>/w1/chat) and the real tracker (<notes>/w1/asks.json).
    sd = os.path.join(NOTES, "v2", "sup", "sim"); os.makedirs(sd, exist_ok=True)
    base = tempfile.mkdtemp(prefix="conv%d_" % conv, dir=sd); cd = os.path.join(base, "chat"); an = os.path.join(base, "asks")
    os.makedirs(cd); os.makedirs(os.path.join(an, "w1"))
    clog = os.path.join(base, "COORD_LOG.md")
    try: open(clog, "w").writelines(open(os.path.join(NOTES, "v2", "sup", "COORD_LOG.md")).readlines()[-40:])
    except OSError: open(clog, "w").close()
    out = []
    for mi in CONVS[conv]:
        r = run_answer(conv, mi, cd, an, clog); out.append(r)
        print("conv %d msg %d: %s lat %s s, shrug %s/%s, intent %s, asks %s, facts %s" % (conv, mi, "PASS" if r.get("pass") else "FAIL", r.get("latency"),
              r.get("shrug_raw"), r.get("shrug_stored"), r.get("intent_act"), r.get("asks"), r.get("facts")), flush=True)
        if conv == 5 and mi == 4 and r.get("act_state") == "acting":
            try: a = run_action(conv, r["cid"], cd, an, clog)
            except Exception as e: a = {"conv": 5, "msg": "4-act", "pass": False, "line": "scoring error: %r" % e, "ctl_calls": [], "duration": None}
            out.append(a)
            print("conv 5 action: %s %s s, line %s, ctl %s" % ("PASS" if a["pass"] else "FAIL", a["duration"], a["line"][:120], a["ctl_calls"]), flush=True)
        elif conv == 5 and mi == 4:
            out.append({"conv": 5, "msg": "4-act", "pass": False, "line": "action not started (act state %s)" % r.get("act_state")})
    for r in out: r["base"] = base; r["at"] = now()
    return out

def table(rows):
    f = lambda v: "-" if v is None else v
    L = ["# Controller chat simulations — %s" % now().replace("T", " "), "",
         "JB 28 Oct 11:12: action, not shoulder shrugs. 5 conversations through the real chat path (chat_sync.py plan -> claude as chat_reply.sh -> store), "
         "each in its own temp chat dir + asks dir (real threads and asks untouched). Criteria per answer: latency <= %d s; 0 shrug phrases in the raw output "
         "and in the stored reply; 'Doing now' + an ETA HH:MM; last line INTENT: act; asks block with >= 1 ask (>= 3 for messages 1 and 2); >= 1 digest fact "
         "(PR # or %%). Conversation 5 also runs ONE real action run for the W1-40 message (LC_CTL_SIM: read-only toolkit, no Write/Edit)." % LAT_MAX, "",
         "| Conv | Msg | Latency s | Shrugs raw/stored | Doing now | ETA | Intent act | Asks | Facts | Model | Cost $ | Result |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tot = 0.0
    for r in rows:
        if r.get("msg") == "4-act":
            L.append("| %s | 4 action run | %s | - | - | - | - | - | - | sonnet | %s | %s |" % (r["conv"], f(r.get("duration")), f(r.get("cost")), "PASS" if r.get("pass") else "FAIL"))
        else:
            L.append("| %s | %s | %s | %s/%s | %s | %s | %s | %s | %s | %s | %s | %s |" % (r["conv"], r["msg"], f(r.get("latency")), f(r.get("shrug_raw")), f(r.get("shrug_stored")),
                     "yes" if r.get("doing_now") else "NO", "yes" if r.get("eta") else "NO", "yes" if r.get("intent_act") else "NO", f(r.get("asks")), f(r.get("facts")),
                     str(r.get("model", "")).replace("claude-", ""), "%.3f" % r["cost"] if isinstance(r.get("cost"), (int, float)) else "-", "PASS" if r.get("pass") else "FAIL"))
        tot += r["cost"] if isinstance(r.get("cost"), (int, float)) else 0
    ans = [r for r in rows if r.get("msg") != "4-act"]; lat = [r["latency"] for r in ans if isinstance(r.get("latency"), (int, float))]
    convs = sorted({r["conv"] for r in rows}); cp = {c: all(r.get("pass") for r in rows if r["conv"] == c) for c in convs}
    L += ["", "**Totals:** %d answers, %d pass; latency median %s s, max %s s; shrug phrases raw %d, stored %d; cost $%.2f." % (
              len(ans), sum(1 for r in ans if r.get("pass")), sorted(lat)[len(lat) // 2] if lat else "-", max(lat) if lat else "-",
              sum(r.get("shrug_raw") or 0 for r in ans), sum(r.get("shrug_stored") or 0 for r in ans), tot),
          "**Conversations:** " + ", ".join("%d %s" % (c, "PASS" if cp[c] else "FAIL") for c in convs) + (" — ALL PASS" if convs and all(cp.values()) else "")]
    for r in rows:
        if r.get("msg") == "4-act":
            L += ["", "**Conversation 5 action run (W1-40):** %s in %s s. Last line: `%s`. ctl.sh calls: %s. Permission denials: %s. Real queue unchanged: %s." % (
                "PASS" if r.get("pass") else "FAIL", r.get("duration"), r.get("line", ""), ", ".join(r.get("ctl_calls") or []) or "none", r.get("denied"), r.get("queue_unchanged")),
                  "", "Action run's closing text:", "", "> " + (r.get("summary") or "").replace("\n", "\n> ")]
    L += ["", "## Sample replies (stored, as JB sees them)"]
    for r in ans:
        if r["conv"] == min(convs): L += ["", "**Conv %d, message %d** (%s s):" % (r["conv"], r["msg"], r.get("latency")), "", "> " + (r.get("reply") or "").replace("\n", "\n> ")]
    return "\n".join(L) + "\n"

if __name__ == "__main__":
    want = [int(x) for x in arg("--convs", "1,2,3,4,5").split(",")]
    par = int(arg("--par", "2"))
    with cf.ThreadPoolExecutor(par) as ex: got = [x for rs in ex.map(run_conv, want) for x in rs]
    os.makedirs(os.path.dirname(RES), exist_ok=True)
    try: prev = json.load(open(RES))
    except (OSError, ValueError): prev = []
    rows = [r for r in prev if r.get("conv") not in want] + got
    rows.sort(key=lambda r: (r["conv"], str(r["msg"])))
    json.dump(rows, open(RES, "w"), indent=1)
    t = table(rows); open(REPORT, "w").write(t); print(t)
