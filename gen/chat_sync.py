#!/usr/bin/env python3
"""Control Room chat, Mac side. 0 model tokens here; each message gets ONE fresh, short, read-only headless Claude run.

The page posts an issue titled "CHAT <target>" (target: COORD, A<n> (one ask's breakout thread), V3, V2 or L-<lane>) whose body is encrypted in the browser with
the page passphrase (openssl enc format: "Salted__" + salt, PBKDF2-SHA256 200000 iterations, AES-256-CBC, base64), so the
public issue holds nothing readable. Commands:
  chat_sync.py [--notes DIR] [--repo o/n]        sync: read open CHAT issues (owner only), decrypt, rate-limit, append to
                                                 <notes>/w1/chat/<target>.json, comment + close, start mac/chat_reply.sh
  chat_sync.py plan <target> <cid> <rundir>      write the prompt, model, settings and empty MCP config for one answer
  chat_sync.py store <target> <cid> <out.json>   put the answer (claude -p JSON) in the thread; record any action; when the
                                                 answer ends "INTENT: act <request>" start or queue ONE action run (act.txt)
  chat_sync.py act-run <target> <cid>            run the action through the LC repo's scripts/lane.sh (coord role), wait, then
                                                 post the DONE/BLOCKED follow-up from "Claude (acting)" and start a queued one
The passphrase is only ever passed to openssl as -pass file:<path>; this script never reads it."""
import fcntl, glob, json, os, re, shutil, subprocess, sys, time, datetime as dt

A = sys.argv[1:]
def arg(k, d): return A[A.index(k) + 1] if k in A else d
HOME = os.path.expanduser("~")
NOTES = arg("--notes", os.path.join(HOME, "Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"))
REPO = arg("--repo", "multisportusa-git/lc-control-room")
OWNER = REPO.split("/")[0]
KEY = arg("--key", os.path.join(HOME, ".config", "lc-control-room", "pass"))
LC = arg("--lc-repo", os.path.join(HOME, "Claude_London Commuter"))
CDIR = arg("--chat-dir", os.path.join(NOTES, "w1", "chat"))   # --chat-dir: isolated threads/run dirs/chat.log (gen/sim_controller.py)
if "--asks-notes" in A: os.environ["LC_ASKS_NOTES"] = arg("--asks-notes", "")   # the asks tracker (gen/asks.py) reads LC_ASKS_NOTES
LIMIT = int(arg("--limit", "20"))          # messages per rolling hour, all targets together
STALE_MIN = 20                             # a pending message with no answer after this gets a "send again" note
LANE_SH = arg("--lane-sh", os.path.join(LC, "scripts", "lane.sh"))
ACT_STALE_MIN = 100                        # an action run that never reported is closed as blocked after this
ACT_TIMEOUT = int(arg("--act-timeout", str(90 * 60)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GH = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
OPENSSL = "/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else (shutil.which("openssl") or "openssl")
TITLE = re.compile(r"^\s*CHAT\s+(COORD|V3|V2|A\d{1,4}|D-\d{1,4}|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})\s*$")
TARGET = re.compile(r"^(COORD|V3|V2|A\d{1,4}|D-\d{1,4}|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})$")
ASK_T = re.compile(r"^(A\d{1,4}|D-\d{1,4})$")   # per-ask breakout thread (JB 28 Oct 11:33): the Controller, scoped to one ask
def is_coord(t): return t == "COORD" or bool(ASK_T.match(t or ""))
CID = re.compile(r"^[A-Za-z0-9]{6,32}$")
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}

def now(): return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
def log(msg):
    os.makedirs(CDIR, exist_ok=True)
    with open(os.path.join(CDIR, "chat.log"), "a") as f: f.write("%s %s\n" % (now(), msg))
def gh(*a, t=30):
    r = subprocess.run([GH, *a], capture_output=True, text=True, timeout=t)
    return r.stdout if r.returncode == 0 else ""

# ---------- crypto: openssl only, the passphrase stays in its file ----------
def decrypt(b64, key=KEY):
    # the page sends only the ciphertext; take the openssl block ("Salted__" = U2FsdGVkX1 in base64) and ignore anything around it
    m = re.search(r"U2FsdGVkX1[A-Za-z0-9+/=\s]+", b64 or "")
    b64 = re.sub(r"\s", "", m.group(0)) if m else ""
    if not b64: return None
    r = subprocess.run([OPENSSL, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-base64", "-A",
                        "-pass", "file:" + key], input=b64.encode(), capture_output=True, timeout=30)
    if r.returncode != 0: return None
    try: return r.stdout.decode("utf-8")
    except UnicodeDecodeError: return None
def encrypt(text, key=KEY):
    r = subprocess.run([OPENSSL, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-salt", "-base64", "-A",
                        "-pass", "file:" + key], input=text.encode(), capture_output=True, timeout=30, check=True)
    return r.stdout.decode().strip()

# ---------- threads: <notes>/w1/chat/<target>.json, locked read-modify-write ----------
def tpath(target): return os.path.join(CDIR, target + ".json")
class Thread:
    def __init__(self, target): self.target = target
    def __enter__(self):
        os.makedirs(CDIR, exist_ok=True)
        self.lk = open(os.path.join(CDIR, ".lock"), "w"); fcntl.flock(self.lk, fcntl.LOCK_EX)
        try: self.d = json.load(open(tpath(self.target)))
        except (OSError, ValueError): self.d = {}
        self.d.setdefault("target", self.target); self.d.setdefault("msgs", [])
        self.orig = json.dumps(self.d, sort_keys=True)
        return self.d
    def __exit__(self, *e):
        if not e[0] and json.dumps(self.d, sort_keys=True) != self.orig:
            self.d["msgs"] = self.d["msgs"][-200:]
            tmp = tpath(self.target) + ".tmp"; json.dump(self.d, open(tmp, "w"), indent=1); os.replace(tmp, tpath(self.target))
        fcntl.flock(self.lk, fcntl.LOCK_UN); self.lk.close()

def rate_ok(limit=LIMIT):
    """True and records the message if fewer than `limit` messages were accepted in the last hour."""
    p = os.path.join(CDIR, "rate.json")
    try: ts = [t for t in json.load(open(p)) if t > time.time() - 3600]
    except (OSError, ValueError): ts = []
    ok = len(ts) < limit
    if ok: ts.append(time.time())
    tmp = p + ".tmp"; json.dump(ts, open(tmp, "w")); os.replace(tmp, p)
    return ok

def spawn(target, cid):
    sh = os.path.join(ROOT, "mac", "chat_reply.sh")
    if "--no-spawn" in A or not os.path.exists(sh): return
    rd = os.path.join(CDIR, "run", cid); os.makedirs(rd, exist_ok=True)
    with open(os.path.join(rd, "reply.log"), "w") as f:
        subprocess.Popen(["/bin/zsh", sh, target, cid], stdout=f, stderr=subprocess.STDOUT, start_new_session=True, cwd=HOME)

def sync():
    raw = gh("issue", "list", "-R", REPO, "--state", "open", "--limit", "50", "--json", "number,title,body,author")
    try: issues = json.loads(raw) if raw else []
    except ValueError: issues = []
    for it in issues:
        m = TITLE.match(it.get("title", ""))
        if not m: continue
        n, target = it["number"], m.group(1)
        who = (it.get("author") or {}).get("login", "")
        if who.lower() != OWNER.lower():
            gh("issue", "close", str(n), "-R", REPO, "-c", "Only the repo owner can use the chat."); log("ignored #%d from %s" % (n, who)); continue
        txt = decrypt(it.get("body") or "")
        try: msg = json.loads(txt) if txt else None
        except ValueError: msg = None
        if not isinstance(msg, dict) or not str(msg.get("m", "")).strip():
            gh("issue", "close", str(n), "-R", REPO, "-c", "Could not read this message (it must come from the Control Room page)."); log("unreadable #%d" % n); continue
        if msg.get("t") != target:
            gh("issue", "close", str(n), "-R", REPO, "-c", "Message target does not match the title; nothing recorded."); log("target mismatch #%d" % n); continue
        cid = str(msg.get("cid") or "") if CID.match(str(msg.get("cid") or "")) else "i%d" % n
        depth = "deep" if msg.get("d") == "deep" else "auto"
        text = str(msg["m"]).strip()[:12000]   # JB 28 Oct: a 4,000 cap cut a test prompt in half
        with Thread(target) as d:
            if any(x.get("id") == cid for x in d["msgs"]):
                dup = True
            else:
                dup = False; ok = rate_ok()
                d["msgs"].append({"id": cid, "from": "JB", "at": now(), "text": text, "depth": depth, "issue": n, "pending": ok})
                if not ok:
                    d["msgs"].append({"id": cid + "r", "re": cid, "from": "Claude", "at": now(), "model": "",
                                      "text": "I have not answered this: the chat takes at most %d messages an hour, so a runaway loop cannot spend tokens. Please send it again in a few minutes, or open a session if it is urgent." % LIMIT})
        gh("issue", "close", str(n), "-R", REPO, "-c", "received (encrypted reply on the page)")
        if dup: continue
        log("CHAT %s #%d %s%s" % (target, n, cid, "" if ok else " RATE-LIMITED"))
        if ok: spawn(target, cid)
    # a run that died leaves its message pending: say so instead of spinning forever
    for p in glob.glob(os.path.join(CDIR, "*.json")):
        t = os.path.basename(p)[:-5]
        if not TARGET.match(t): continue
        with Thread(t) as d:
            sweep_acts(t, d)
            for x in d["msgs"]:
                if x.get("pending") and x.get("at") and (dt.datetime.now() - dt.datetime.fromisoformat(x["at"])).total_seconds() > STALE_MIN * 60:
                    x["pending"] = False
                    d["msgs"].append({"id": x["id"] + "r", "re": x["id"], "from": "Claude", "at": now(), "model": "", "error": True,
                                      "text": "No answer arrived within %d minutes (the run may have failed). Please send it again." % STALE_MIN})

# ---------- one answer: prompt, profile, store ----------
COORD_BRIEF = os.path.join(NOTES, "cutover", "COORD_BRIEF.md")
COORD_LOG = arg("--coord-log", os.path.join(NOTES, "v2", "sup", "COORD_LOG.md"))   # simulations log to their own copy
DIGEST = os.path.join(NOTES, "v2", "sup", "digest.py")
ROLE = {"COORD": "the London Commuter Controller: the coordinator that owns the lanes, the queue, reviews, the merge train and the plan (JB's one point of contact)",
        "V3": "the V3 coordinator (London Commuter v3 rebuild, Week 1 plan)",
        "V2": "the V2 coordinator (the current London Commuter app's Roadmap to Beta)"}
def sources(target):
    w1 = os.path.join(NOTES, "w1")
    ctx = "- %s (compact snapshot of the Control Room data: tasks, status, PRs, decisions, fuel; start here)\n" % "CONTEXT_FILE"
    if is_coord(target):
        return ctx + ("- DIGEST_FILE (the digest, generated for this message: lanes, slots, PRs, queue, FLAGS; READ this file, do not try to run digest.py; start here)\n- %s (the coordinator brief: rules, budget, what to do per FLAG)\n"
                      "- %s (what the Controller did before; newest last)\n- %s/v2/sup/next.txt (plan and queue)\n- %s/announce.log (event log; read only its tail)\n"
                      "- %s/v2/kpi4/issues.md (known problems)\n- %s/w1/evidence/usage_pct.csv (JB's usage %%: ts,total,code,chats,cowork)\n"
                      "- %s/w1/evidence/jb_journey_20261027/DEFECTS.md (JB's commute defects J1-J14)\n") % (COORD_BRIEF, COORD_LOG, NOTES, NOTES, NOTES, NOTES, NOTES)
    if target == "V3":
        return ctx + "- %s/state.json (task state written by lc_state.py)\n- %s/evidence/ (evidence files)\n- %s/decisions/ (decision cards)\n- %s/docs/handover/v3/ (plan docs, read-only)\n" % (w1, w1, w1, LC)
    if target == "V2":
        return ctx + "- %s/ROADMAP_TO_BETA.html (the V2 roadmap and SITREP)\n- %s/docs/STATUS.md (live state; read-only)\n- %s/status/*.md (lane and coordinator status files)\n- %s/status/JB-to-V2-coordinator.md (JB's earlier notes)\n" % (NOTES, LC, NOTES, NOTES)
    x = target[2:]
    fs = sorted(set(glob.glob(os.path.join(NOTES, x + "-status.md")) + glob.glob(os.path.join(NOTES, "status", x + "-*.md")) + glob.glob(os.path.join(NOTES, "status", x + ".md"))))
    return ctx + "".join("- %s\n" % f for f in fs) or ctx + "- (no status file found for lane %s)\n" % x

def route(depth, text):
    if depth == "deep": return "opus"
    t = text.strip().lower()
    if len(t) < 160 and re.match(r"^(what|where|how far|how many|is|are|any|status|eta|when|which|has|have|did)\b", t) \
            and not re.search(r"\b(why|should|plan|explain|change|fix|start|stop|merge|approve|tell|ask|run|make|add|remove|please|decide|propose|compare)\b", t):
        return "haiku"
    return "sonnet"

DENY = ["Read(//**/.env)", "Read(//**/.env.*)", "Read(**/.env)", "Read(//**/BrokerKey.xcconfig)", "Read(**/BrokerKey.xcconfig)",
        "Read(//**/*.xcactivitylog)", "Read(//**/testq/results/*.log)", "Read(~/.config/lc-control-room/**)", "Read(~/.config/gh/**)",
        "Read(~/.ssh/**)", "Read(~/.claude/.credentials*)", "Read(~/Library/Preferences/**)", "Read(~/Library/Keychains/**)",
        "Read(~/Library/Containers/**)", "Read(//**/*.plist)", "Read(//**/*.p12)", "Read(//**/*.pem)",
        "Bash(git -C * push:*)", "Bash(git -C * reset:*)", "Bash(git -C * checkout:*)", "Bash(git -C * commit:*)", "Bash(git -C * stash:*)",
        "Bash(git -C * show:*)", "Bash(git -C * config:*)", "Bash(git -C * cat-file:*)", "Bash(gh workflow:*)", "Bash(gh api:*)", "Bash(gh pr merge:*)",
        "Bash(gcloud:*)", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task"]
ALLOW = ["Read", "Grep", "Glob", "Bash(gh pr view:*)", "Bash(gh pr list:*)", "Bash(git log:*)", "Bash(git -C * log:*)",
         "Bash(git -C * status:*)", "Bash(git -C * branch:*)", "Bash(git -C * rev-parse:*)"]

def context_snapshot(path):
    w = os.path.join(NOTES, "w1", "control-room", "w1.json")
    try: D = json.load(open(w))
    except (OSError, ValueError): D = {}
    keep = {"gen": D.get("gen"), "overall": D.get("overall"), "sit": D.get("sit"), "decisions": D.get("decisions"), "v2": D.get("v2"),
            "fuel": D.get("fuel"), "facts": {k: (D.get("facts") or {}).get(k) for k in ("open_prs", "merged_24h", "coordinator", "pending", "main")},
            "queue": (D.get("queue") or [])[:10], "gates": D.get("gates"), "day_finish": D.get("day_finish"), "week_finish": D.get("week_finish"),
            "tasks": [{k: t.get(k) for k in ("id", "title", "status", "rag", "why", "plan", "owner", "model", "day", "finish", "finish_day")} for t in D.get("tasks") or []]}
    json.dump(keep, open(path, "w"), indent=0, default=str)

CTL = os.path.join(ROOT, "mac", "ctl.sh")
SHRUG = re.compile(r"(?i)\b(i\s+can(?:no|')t|i\s+cannot|i'?m\s+not\s+sure|not\s+sure|i\s+haven'?t|i\s+have\s+not|i'?ve\s+recorded|i\s+recorded|recorded\s+(?:it|this|as)|read-only|the\s+acting\s+run\s+will|an?\s+acting\s+run|i\s+don'?t\s+know|unable\s+to|no\s+way\s+to|can'?t\s+see|cannot\s+see|doesn'?t\s+show|does\s+not\s+show)\b")
CAN = ("queue, hold or unhold lanes (JB's asks first), write lane briefs, start reviews, read test results and failing lines, submit the FULL job "
       "for an install, run the install gate, start Appium and check the phone connection, record JB's phone-free answer, update the asks tracker, "
       "create decision cards, edit the Control Room page and scripts")
def ask_get(aid):
    """One ask from the tracker (gen/asks.py), or None."""
    if (aid or "").startswith("D-"):   # a decision card: synthesise from its file (JB 28 Oct: every card has the box)
        try:
            d = json.load(open(os.path.join(NOTES, "w1", "decisions", aid + ".json")))
            return {"id": aid, "title": d.get("title", ""), "cat": "Your decisions", "state": "STAGED", "rag": "amber",
                    "note": ("decision %s (status %s). Kinds JB may prefix: [DIRECT] [UPDATE] [ESCALATE] [PRIORITISE] [COMMENT] [CLOSE]. To track work, `ctl.sh asks add`, "
                             "not `asks set %s`. " % (aid, d.get("status"), aid)) + (d.get("situation") or "")[:300], "history": []}
        except Exception: return None
    try:
        sys.path.insert(0, os.path.join(ROOT, "gen")); import asks as AK
        return next((a for a in AK.load()["asks"] if a.get("id") == aid), None)
    except Exception: return None

def ask_block(aid):
    a = ask_get(aid)
    if not a: return "THIS IS THE BREAKOUT THREAD FOR ASK %s (not found in the tracker; treat JB's message as the ask).\n\n" % aid
    hist = "\n".join("  %s %s%s" % ((h.get("at") or "")[11:16], h.get("state", ""), (": " + h["note"]) if h.get("note") else "") for h in (a.get("history") or [])[-8:])
    return ("THIS IS THE BREAKOUT THREAD FOR ASK %s. Everything JB writes here is about this one ask; answer only about it.\n"
            "Ask %s: %s\nCategory: %s · State: %s · RAG: %s · ETA: %s%s\nNote: %s\nHistory (oldest first):\n%s\n\n") % (
        aid, aid, a.get("title", ""), a.get("cat", ""), a.get("state", ""), a.get("rag", ""), (a.get("eta") or "-").replace("T", " "),
        " · needs JB" if a.get("needs_jb") else "", a.get("note") or "-", hist or "  (none)")

def coord_prompt(me, hist, ctxf, rd, target="COORD"):
    """The Controller's answer (JB 28 Oct 11:12: action, not shoulder shrugs). The action run starts right after this answer.
    target A<n>: the per-ask breakout thread (JB 28 Oct 11:33): same Controller, scoped to that ask and that thread's history."""
    who = "an automatic judgement event from the watchdog" if me.get("from") == "Watchdog" else "JB"
    asks = ""
    try:
        sys.path.insert(0, os.path.join(ROOT, "gen")); import asks as A
        op = [a for a in A.load()["asks"] if a["state"] != "DONE"][-12:]
        asks = "\n".join("%s %s %s eta %s: %s" % (a["id"], a["state"], a["rag"], (a.get("eta") or "-")[11:16], a["title"][:90]) for a in op)
    except Exception: pass
    brk = ask_block(target) if ASK_T.match(target or "") else ""
    if brk: asks = "(this thread is about %s only; see above)" % target
    return (brk + "You are the London Commuter CONTROLLER: the coordinator who owns the lanes, the queue, reviews, the merge train, installs and the plan. "
            "You speak to JB in the Control Room chat. The moment you answer, YOUR action run starts and does the work, "
            "so you answer as the one doing it: what you are doing now and when each thing will be done.\n\n"
            "Facts (read the digest first; it was generated for this message):\n- digest: %s\n- Control Room snapshot: %s\n- coordinator rules: %s\n- your log: %s\n"
            "- JB's commute defects: %s/w1/evidence/jb_journey_20261027/DEFECTS.md\nOpen asks already tracked:\n%s\n\n"
            "Your action run can: %s. It does not merge by hand (the merge train merges on PASS), deploy the broker (JB runs gcloud), change phone "
            "settings or touch secrets. Physical steps only JB can do: unlock or plug in the phone, approve a prompt on it, run gcloud.\n\n"
            "Conversation so far (oldest first):\n%s\n\nNew message from %s:\n<<<\n%s\n>>>\n\n"
            "Write your answer in this exact shape (plain English, at most ~170 words):\n"
            "**Doing now** (as of HH:MM): one bullet per ask: the concrete action and **ETA HH:MM** (when JB will see it done).\n"
            "**Facts**: 2-4 bullets with numbers from the digest (lanes, PRs, jobs, usage), each true now.\n"
            "**Needs you**: only if a physical step by JB is required; otherwise leave this heading out.\n"
            "Rules: never write 'I can't', 'I cannot', 'not sure', 'I haven't', 'I've recorded', 'read-only', 'the acting run will', 'unable to', "
            "'doesn't show' or 'I don't know'. If a fact is missing, say what you are doing to get it and when. If something is blocked, name the blocker, "
            "your next move and its time. A standing rule from JB (e.g. 'P0 first') is applied now, not just noted. Never reveal secrets.\n"
            "Then one fenced block listing every ask in the message (split compound messages into separate asks):\n"
            "```asks\n[{\"title\": \"<short imperative>\", \"cat\": \"Install & phone|Commute fixes|Merge train|Control Room|Ops & tooling|Question\", \"eta\": \"HH:MM\", \"needs_jb\": false}]\n```\n"
            "Last line exactly: INTENT: act <one-line summary of everything to do>  (INTENT: answer only for a pure question that needs no action).\n") % (
        os.path.join(rd, "digest.txt"), ctxf, COORD_BRIEF, COORD_LOG, NOTES, asks or "(none)", CAN,
        "\n".join("[%s %s] %s" % (x.get("at", "")[11:16], x.get("from"), (x.get("text") or "")[:700]) for x in hist) or "(none)", who, me["text"])

def plan(target, cid, rd):
    if not TARGET.match(target) or not CID.match(cid): raise SystemExit("bad target or id")
    os.makedirs(os.path.join(rd, "cwd"), exist_ok=True)  # empty working dir: no CLAUDE.md, no project settings
    with Thread(target) as d: msgs = list(d["msgs"])
    me = next((x for x in msgs if x.get("id") == cid and x.get("from") in ("JB", "Watchdog")), None)
    if not me: raise SystemExit("message %s not in thread %s" % (cid, target))
    if is_coord(target):
        try: dg = subprocess.run([sys.executable, DIGEST], capture_output=True, text=True, timeout=90).stdout
        except Exception as e: dg = "digest failed: %r" % e
        open(os.path.join(rd, "digest.txt"), "w").write("\n".join(l for l in dg.splitlines() if "install gate" not in l)[-12000:])
    hist = [x for x in msgs[: msgs.index(me)]][-6:]
    ctxf = os.path.join(rd, "context.json"); context_snapshot(ctxf)
    role = ROLE.get(target) or "the status voice of London Commuter lane %s (you are NOT that lane's running session; you read its status file)" % target[2:]
    rec = {"COORD": "a note in the Controller's inbox (type \"note\") or a decision card (type \"decision\")",
           "V2": "a note the V2 coordinator reads (type \"note\")",
           "V3": "a decision card for the coordinator (type \"decision\") or an action note (type \"note\")"}.get(target, "a note for that lane (type \"note\") or a decision card (type \"decision\")")
    prompt = ("You are answering JB in the London Commuter Control Room chat, speaking as %s. This is one short, fresh, read-only run.\n\n"
              "Sources (read only what you need; quote facts from them, do not guess):\n%s\n"
              "Conversation so far (oldest first):\n%s\n\nJB's new message:\n<<<\n%s\n>>>\n\n"
              "Rules:\n- Answer concisely in plain English (at most about 150 words). Say how fresh the facts are when it matters.\n"
              "- You cannot change code, git or any file in this run; you only read. Never claim you did something yourself.\n"
              "- If JB asks for an action, record it as %s by ending your answer with exactly one fenced block:\n"
              "```record\n{\"type\": \"note\", \"text\": \"<the instruction, one or two lines>\"}\n```\n"
              "  or for a decision card: ```record\n{\"type\": \"decision\", \"title\": \"...\", \"situation\": \"...\", \"why_now\": \"...\", "
              "\"options\": [{\"key\": \"A\", \"label\": \"...\", \"detail\": \"...\"}, {\"key\": \"B\", \"label\": \"...\", \"detail\": \"...\"}], \"recommend\": \"A\", \"because\": \"...\"}\n```\n"
              "  and say in the answer that you recorded it and who picks it up. Otherwise add no block.\n"
              "- Never reveal or read secrets: tokens, keys, passphrases, .env, BrokerKey.xcconfig, build logs, preference plists.\n"
              "- Do not use tools you were not given; if something needs a session, say so.\n"
              "- Your LAST line must be exactly one of:\n  INTENT: answer\n  INTENT: act <one-line imperative request>\n"
              "  Use act ONLY when JB asks for something to be done (start a task, run, fix, merge-train, check and act). Anything vague, a question or an opinion is answer. "
              "When you choose act, a separate acting run starts after your answer and reports back in this thread: say in one line what it will do, still add the record block above, and do not say you only read.\n") % (
        role, sources(target).replace("CONTEXT_FILE", ctxf).replace("DIGEST_FILE", os.path.join(rd, "digest.txt")),
        "\n".join("[%s %s] %s" % (x.get("at", "")[11:16], x.get("from"), (x.get("text") or "")[:1200]) for x in hist) or "(none)",
        me["text"], rec)
    if is_coord(target):
        prompt = coord_prompt(me, hist, ctxf, rd, target)
    model = route(me.get("depth"), me["text"])
    if is_coord(target) and model == "haiku": model = "sonnet"   # JB 28 Oct: the Controller never answers with the cheapest model
    open(os.path.join(rd, "prompt.txt"), "w").write(prompt)
    open(os.path.join(rd, "model.txt"), "w").write(MODELS[model])
    # the SP1 lean lane start (LC docs/handover/v3/sp1/lane-settings.json) plus the read-only rails
    json.dump({"enableArtifact": False, "disableWorkflows": True, "skillListingMaxDescChars": 150, "disableAllHooks": True,
               "permissions": {"allow": ALLOW, "deny": DENY + ["SuggestPluginInstall", "SearchPlugins", "SuggestSkills", "mcp__Claude_Browser__*", "mcp__visualize__*"],
                               "defaultMode": "default"}},
              open(os.path.join(rd, "settings.json"), "w"), indent=1)
    json.dump({"mcpServers": {}}, open(os.path.join(rd, "mcp.json"), "w"))
    open(os.path.join(rd, "tools.txt"), "w").write(",".join(ALLOW))
    return prompt, MODELS[model]

SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|passphrase|bearer)\s*[:=]\s*\S+|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_-]{10,}")
def record(target, cid, rec):
    """Write what JB asked for where its owner reads it; return the confirmation sentence."""
    if not isinstance(rec, dict): return ""
    if rec.get("type") == "decision" and target != "V2" and rec.get("title") and isinstance(rec.get("options"), list) and len(rec["options"]) >= 2:
        dd = os.path.join(NOTES, "w1", "decisions"); os.makedirs(dd, exist_ok=True)
        nums = [int(m.group(1)) for f in os.listdir(dd) for m in [re.match(r"D-(\d+)\.json$", f)] if m]
        did = "D-%d" % (max(nums, default=0) + 1)
        opts = [{k: str(o.get(k, ""))[:600] for k in ("key", "label", "detail")} for o in rec["options"][:6] if isinstance(o, dict) and o.get("key")]
        card = {"id": did, "created": now()[:16], "title": str(rec["title"])[:200], "situation": str(rec.get("situation", ""))[:2000],
                "why_now": str(rec.get("why_now", ""))[:600], "options": opts, "recommend": str(rec.get("recommend", ""))[:2],
                "because": str(rec.get("because", ""))[:800], "tasks": [], "status": "open", "programme": "V3",
                "raised_by": "chat %s %s" % (target, cid)}
        p = os.path.join(dd, did + ".json"); tmp = p + ".tmp"; json.dump(card, open(tmp, "w"), indent=1); os.replace(tmp, p)
        log("RECORDED %s decision %s" % (target, did)); return "Recorded as decision %s (Your decisions)." % did
    text = SECRET.sub("[redacted]", str(rec.get("text", "")).strip())[:1500]
    if not text: return ""
    if is_coord(target): f = os.path.join(NOTES, "v2", "sup", "JB-to-controller.md")
    elif target == "V2": f = os.path.join(NOTES, "status", "JB-to-V2-coordinator.md")
    elif target == "V3": f = os.path.join(NOTES, "w1", "ACTIONS-from-JB.md")
    else: f = os.path.join(NOTES, "status", "JB-to-%s.md" % target[2:])
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "a") as fh: fh.write("%s ACTION JB (chat %s): %s\n" % (now()[:16], cid, text.replace("\n", " ")))
    log("RECORDED %s note in %s" % (target, os.path.basename(f))); return "Recorded in %s." % os.path.basename(f)

def read_out(outp):
    """(final result object, first-request context tokens) from `claude -p --output-format stream-json --verbose` output
    (one JSON object per line) or a single --output-format json object."""
    res, start = {}, None
    try: lines = open(outp).read().splitlines()
    except OSError: return {}, None
    for ln in lines:
        try: e = json.loads(ln)
        except ValueError: continue
        if not isinstance(e, dict): continue
        if e.get("type") == "assistant" and start is None:
            u = (e.get("message") or {}).get("usage") or {}
            start = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")) or None
        if e.get("type") == "result" or ("result" in e and "is_error" in e): res = e
    return res, start

INTENT = re.compile(r"^[ \t]*INTENT:[ \t]*(answer|act)\b[ \t:-]*(.*?)[ \t]*$", re.I | re.M)
def lane_id(cid): return "C" + re.sub(r"[^A-Za-z0-9]", "", cid)[-7:]   # lane ids are letter + <= 7; the tail of a cid varies most
COORD_PAR = 2
def act_running(d, me_id):
    """The other action in this thread that is still running (acting, not stale), else None. The Controller (COORD) runs up to
    COORD_PAR actions at once (JB 28 Oct: nothing waits behind a long run), so it reports busy only when all are taken."""
    if is_coord(d.get("target")):
        # COORD and every A<n> breakout thread share the Controller's COORD_PAR action slots. Caller holds the CDIR lock,
        # so the other threads' files can be read directly.
        msgs = list(d["msgs"])
        for p in glob.glob(os.path.join(CDIR, "*.json")):
            t = os.path.basename(p)[:-5]
            if t == d.get("target") or not is_coord(t): continue
            try: msgs += json.load(open(p)).get("msgs") or []
            except (OSError, ValueError, AttributeError): pass
        run = []
        for x in msgs:
            a = x.get("act") or {}
            if x.get("id") == me_id or a.get("state") != "acting": continue
            try: age = (dt.datetime.now() - dt.datetime.fromisoformat(a.get("at") or x["at"])).total_seconds()
            except (ValueError, KeyError): age = 0
            if age < ACT_STALE_MIN * 60: run.append(x)
        return run[0] if len(run) >= COORD_PAR else None
    for x in d["msgs"]:
        a = x.get("act") or {}
        if x.get("id") != me_id and a.get("state") == "acting":
            try: age = (dt.datetime.now() - dt.datetime.fromisoformat(a.get("at") or x["at"])).total_seconds()
            except (ValueError, KeyError): age = 0
            if age < ACT_STALE_MIN * 60: return x
    return None

def make_brief(target, cid, req, rd, jb_text):
    """The action run's brief (<= 8000 bytes): JB's request verbatim, target, time, the read run's facts, the rules, the last-line contract."""
    try: C = json.load(open(os.path.join(rd, "context.json")))
    except (OSError, ValueError, TypeError): C = {}
    ids = set(re.findall(r"W\d+-\d+", req + " " + jb_text)); tasks = C.get("tasks") or []
    rows = [t for t in tasks if t.get("id") in ids]
    facts = ["Snapshot of the Control Room data from the read run (generated %s; may be minutes old, re-check with scripts/lc_state.py before acting):" % (C.get("gen") or "unknown")]
    if C.get("overall"): facts.append("overall: " + json.dumps(C["overall"], default=str)[:400])
    for t in rows[:8]: facts.append("task " + json.dumps({k: v for k, v in t.items() if v not in (None, "")}, default=str)[:420])
    if not rows: facts.append("(no task id named in the request)")
    for k in ("queue", "decisions", "gates"):
        if C.get(k): facts.append("%s: %s" % (k, json.dumps(C[k], default=str)[:500]))
    head = ("CHAT ACTION RUN. JB asked in the Control Room chat (target %s, message %s) for something to be DONE. Do it, see it through, and report.\n"
            "Time now: %s\nTarget: %s\n\nJB's request, verbatim:\n<<<\n%s\n>>>\nThe read-only reply run summarised it as: %s\n\n") % (
        target, cid, now(), target, jb_text[:2500], req[:400])
    rules = ("\nRules that apply:\n- CLAUDE.md rule 0 applies in full (read CLAUDE.md first).\n- Merges happen only by the train; never merge a PR yourself.\n"
             "- Phone actions need a phone-free answer: if it needs JB's iPhone, do everything else and say exactly what JB must do on it.\n"
             "- A decision only JB can make becomes a decision card (scripts/act_on_decided.sh need ...), then end BLOCKED naming the card.\n"
             "- Use only the tools you were given; never read secrets; do not widen permissions or work around a denied tool.\n"
             "- Nobody reads chat: your last line is posted back to JB in the Control Room.\n\n"
             "Your LAST line must be exactly one of:\nDONE <what was done, with ids: task, job, lane, PR numbers, files>\nBLOCKED <exact reason and what JB must do>\n"
             "DONE only if EVERY part of the request was done where JB asked (right file, right place). If any part was refused,\n"
             "blocked by the sandbox or done somewhere else instead (e.g. /tmp), the line is BLOCKED and says which part.\n")
    if is_coord(target):
        head = head.replace("CHAT ACTION RUN.", "CONTROLLER RUN (you are the London Commuter Controller/coordinator).", 1)
        head += ("You are the Controller. First read %s (the coordinator brief) and %s/digest.txt (the digest, generated minutes ago). Then do what JB asked "
                 "AND act on every FLAG in the digest, as the brief says. Launch or requeue lanes ONLY by writing %s/v2/queue/<L>-<task>.req (7 lines) with the "
                 "Write tool; lc_watch starts it within a minute. NEVER run scripts/lane.sh yourself (a lane started from your shell dies with you). Max %s code "
                 "lanes; no new lane at >= 95%% weekly usage. Things you cannot do with your tools (git push, gh pr create, review.sh, test-submit) go in your last line "
                 "as BLOCKED with the exact one-line command for JB or a session. Append one line per action to %s.\n") % (
            COORD_BRIEF, rd, NOTES, os.environ.get("LC_MAX_LANES", "2"), COORD_LOG)
    room = 7800 - len((head + rules).encode())
    body = "\n".join(facts)
    while len(body.encode()) > max(room, 0): body = body[: int(len(body) * 0.9)]
    return head + body + "\n" + rules

def begin_act(d, me, target, rd):
    """Mark me.act as acting and write its brief. Caller holds the thread lock."""
    a = me["act"]; lane = lane_id(me["id"]); bf = os.path.join(rd, "brief.md")
    open(bf, "w").write(make_brief(target, me["id"], a.get("req", ""), rd, me.get("text", "")))
    a.update(state="acting", lane=lane, brief=bf, at=now(), rundir=rd)
    a.pop("behind", None)

def store(target, cid, outp):
    o, start = read_out(outp)
    rd = os.path.dirname(os.path.abspath(outp))
    try:
        text = str(o.get("result") or "").strip(); err = bool(o.get("is_error")) or not text
        model = ",".join((o.get("modelUsage") or {}).keys()); cost = o.get("total_cost_usd")
    except AttributeError:
        text, err, model, cost = "", True, "", None
    intent, req = "answer", ""
    its = INTENT.findall(text)
    if its: intent, req = its[-1][0].lower(), its[-1][1].strip()
    text = INTENT.sub("", text).strip()
    if err: intent = "answer"
    note = ""
    m = re.search(r"```record\s*(\{.*?\})\s*```", text, re.S)
    rec = None
    if m:
        try: rec = json.loads(m.group(1))
        except ValueError: note = "(Could not record the action: the record was not valid JSON.)"
        text = (text[: m.start()] + text[m.end():]).strip()
    asks_in = []
    m2 = re.search(r"```asks\s*(\[.*?\])\s*```", text, re.S)
    if m2:
        try: asks_in = [x for x in json.loads(m2.group(1)) if isinstance(x, dict) and x.get("title")][:8]
        except ValueError: asks_in = []
        text = (text[: m2.start()] + text[m2.end():]).strip()
    if is_coord(target):
        n_shrug = len(SHRUG.findall(text))
        if n_shrug:
            log("SHRUG %s %s: %d phrase(s) removed" % (target, cid, n_shrug))
            text = "\n".join(l for l in text.splitlines() if not SHRUG.search(l))
        if not err and intent == "answer" and asks_in and any(not str(a.get("cat", "")).lower().startswith("question") for a in asks_in):
            intent, req = "act", "; ".join(str(a["title"]) for a in asks_in)[:400]
    text = SECRET.sub("[redacted]", text)[:6000]
    if err: text = "Sorry, the answer run failed. Please send it again (or choose Deep)."
    ask_ids = []
    if ASK_T.match(target) and not err:
        # breakout thread: never new asks; this ask gets the update (state WORKING when there is work, one-line note, history)
        try:
            sys.path.insert(0, os.path.join(ROOT, "gen")); import asks as A
            summ = re.sub(r"\s+", " ", (req or next((l.strip("*-# ") for l in text.splitlines() if l.strip("*-# ")), "") or "answered in the breakout thread")).strip()[:200]
            eta = next((str(x.get("eta")) for x in asks_in if x.get("eta")), "")
            if A.set_(target, "WORKING" if intent == "act" else "", eta if intent == "act" else "", "chat: " + summ, by="Controller (breakout)"): ask_ids = [target]
        except Exception as e: log("ASKS error %r" % e)
    elif target == "COORD" and not err:
        try:
            sys.path.insert(0, os.path.join(ROOT, "gen")); import asks as A
            if not asks_in: asks_in = [{"title": (req or "")[:120] or "Answer: " + text[:80], "cat": "Question"}]
            for x in asks_in:
                q = str(x.get("cat", "")).lower().startswith("question") and intent == "answer"
                a = A.add(str(x["title"])[:200], x.get("cat", ""), x.get("eta", ""), "DONE" if q else "WORKING", cid, "", bool(x.get("needs_jb")))
                if a: ask_ids.append(a["id"])
        except Exception as e: log("ASKS error %r" % e)
    started, behind = "", None
    with Thread(target) as d:
        me = next((x for x in d["msgs"] if x.get("id") == cid and x.get("from") in ("JB", "Watchdog")), None)
        if intent == "act" and me is not None:
            req = SECRET.sub("[redacted]", req or me.get("text", ""))[:400]
            me["act"] = {"state": "answered", "req": req, "at": now(), "lane": lane_id(cid), "rundir": rd, "asks": ask_ids}
            behind = act_running(d, cid)
            if behind: me["act"].update(state="queued", behind=behind["act"].get("lane") or behind["id"])
            else: begin_act(d, me, target, rd); started = me["act"]["lane"]
            if not isinstance(rec, dict) or rec.get("type") != "decision": rec = {"type": "note", "text": (rec or {}).get("text") or req}
            rec = dict(rec); 
            if rec.get("type") == "note": rec["text"] = "%s [%s]" % (str(rec["text"]).strip(), ("started action run " + started) if started else "queued behind " + str(me["act"]["behind"]))
        if rec is not None and is_coord(target) and rec.get("type") == "note": rec = None   # the asks tracker is the record
        if rec is not None:
            try: note = record(target, cid, rec)
            except Exception as e: note = "(Could not record the action: %s.)" % e
        for x in d["msgs"]:
            if x.get("id") == cid: x["pending"] = False
        d["msgs"] = [x for x in d["msgs"] if x.get("re") != cid]
        if started: note = (note + "\n" if note else "") + ("Working on it now (run %s%s); updates post here." % (started, ", asks " + ", ".join(ask_ids) if ask_ids else "") if is_coord(target) else "Acting: started action run %s; I will post what it did here." % started)
        d["msgs"].append({"id": cid + "r", "re": cid, "from": "Claude", "at": now(), "text": (text + ("\n\n" + note if note else "")).strip(),
                          "model": model, "error": err, "cost": cost, "start_tokens": start})
        if behind:
            d["msgs"].append({"id": cid + "q", "fu": cid, "from": "Claude (acting)", "at": now(),
                              "text": "Queued behind %s: one action at a time per target. It starts when that one ends." % me["act"]["behind"]})
    try: os.remove(os.path.join(rd, "act.txt"))
    except OSError: pass
    if started: open(os.path.join(rd, "act.txt"), "w").write(started + "\n")
    log("ANSWERED %s %s (%s) start=%s intent=%s%s%s" % (target, cid, model or "-", start, intent, " ERROR" if err else "", " QUEUED" if behind else ""))

def spawn_act(target, cid):
    sh = os.path.join(ROOT, "mac", "chat_act.sh")
    if "--no-spawn" in A or not os.path.exists(sh): return
    rd = os.path.join(CDIR, "run", cid); os.makedirs(rd, exist_ok=True)
    with open(os.path.join(rd, "act.log"), "w") as f:
        subprocess.Popen(["/bin/bash", sh, target, cid], stdout=f, stderr=subprocess.STDOUT, start_new_session=True, cwd=HOME)

def evidence(lane, state, sig):
    """Up to 5 short evidence lines: lane registry (branch, PR, job) and the last lines the run wrote before its signal."""
    ev = []
    try: reg = json.load(open(os.path.join(NOTES, "v2", "lanes", lane + ".json")))
    except (OSError, ValueError): reg = {}
    ev.append("lane %s, state %s" % (lane, state))
    if reg.get("branch"): ev.append("branch %s" % reg["branch"])
    if reg.get("pr"): ev.append("PR #%s" % reg["pr"])
    if reg.get("job"): ev.append("job %s" % reg["job"])
    lr = reg.get("last_run")
    if lr and os.path.exists(lr):
        res, _ = read_out(lr)
        ls = [l.strip() for l in str(res.get("result") or "").splitlines() if l.strip() and l.strip() != sig]
        for l in ls[-(5 - len(ev)):] if len(ev) < 5 else []: ev.append(l[:160])
    return [SECRET.sub("[redacted]", e) for e in ev[:5]]

def finish_act(target, cid, state_word, sig, lane):
    """Post the follow-up into the thread and set the chip state."""
    sig = SECRET.sub("[redacted]", (sig or "").strip())[:500]
    if is_coord(target):
        blocked = sig.upper().startswith(("BLOCKED", "NEEDS JB"))
        line = sig if re.match(r"^(DONE|PARTIAL|NEEDS JB|BLOCKED)\b", sig) else ("DONE " if state_word == "done" else "PARTIAL ") + (sig or "(no detail)")
    else:
        blocked = state_word in ("blocked", "needs-coordinator", "no-signal", "parked", "failed") or sig.upper().startswith("BLOCKED")
    if is_coord(target): pass
    elif blocked:
        line = sig if sig.upper().startswith("BLOCKED") else "BLOCKED %s" % (sig or {"needs-coordinator": "the run needs a coordinator (hand-off or escalation)", "parked": "the lane is parked (health gate or 3 lanes running); lc_watch.sh retries it", "no-signal": "the run ended without a result line"}.get(state_word, "the run did not finish: " + state_word))
    else:
        line = sig if sig.upper().startswith("DONE") else "DONE %s" % (sig or "(no detail)")
    ev = evidence(lane, state_word, sig)
    full = ""
    if is_coord(target):
        try: full = open(os.path.join(CDIR, "run", cid, "act_full.md")).read().strip()
        except OSError: full = ""
    with Thread(target) as d:
        me = next((x for x in d["msgs"] if x.get("id") == cid), None)
        if me is not None and me.get("act"): me["act"]["state"] = "blocked" if blocked else ("partial" if line.startswith("PARTIAL") else "done")
        d["msgs"] = [x for x in d["msgs"] if x.get("id") != cid + "a"]
        d["msgs"].append({"id": cid + "a", "fu": cid, "from": "Claude (acting)", "at": now(), "error": blocked,
                          "text": line + (("\n\n" + full) if full else "".join("\n- " + e for e in ev))})
    log("ACT %s %s %s %s" % ("BLOCKED" if blocked else "DONE", target, cid, line[:80]))

def act_next(target):
    """Start the oldest queued action of this thread if none is running. The Controller's threads (COORD and A<n>) share slots,
    so a finished run there also starts a queued one in any of them (this thread first)."""
    ts = [target]
    if is_coord(target):
        ts += sorted(t for t in (os.path.basename(p)[:-5] for p in glob.glob(os.path.join(CDIR, "*.json"))) if t != target and is_coord(t))
    for t in ts:
        nxt = None
        with Thread(t) as d:
            if act_running(d, ""): return
            q = next((x for x in d["msgs"] if (x.get("act") or {}).get("state") == "queued"), None)
            if q:
                rd = q["act"].get("rundir") or os.path.join(CDIR, "run", q["id"]); os.makedirs(rd, exist_ok=True)
                begin_act(d, q, t, rd); nxt = q["id"]
                d["msgs"].append({"id": nxt + "s", "fu": nxt, "from": "Claude (acting)", "at": now(), "text": "Starting action run %s now." % q["act"]["lane"]})
        if nxt: spawn_act(t, nxt); return

CL_BIN = (sorted(glob.glob(os.path.join(HOME, ".local/share/claude/versions/*")), key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", os.path.basename(p))]) or [os.path.join(HOME, ".local/bin/claude")])[-1]
# The Controller's permissions: the fixed-command toolkit (mac/ctl.sh) plus read-only git/gh. No push, no PR creation, no free shell,
# no edits in the app repo (lanes do code), no secrets.
# JB 28 Oct 12:01: "I want the controller's permissions to match yours, I authorize it." Full shell like the coordinator
# session; the standing project rules stay as denies (no gcloud, secrets, hand merges, force pushes, branch switching in the
# main checkout, workflow runs, app-repo edits outside lanes, phone settings).
CTL_ALLOW = ["Read", "Grep", "Glob", "Write", "Edit", "Bash"]
CTL_DENY = [x for x in DENY if x not in ("Write", "Edit") and not x.startswith("Bash(git -C * ")] + [
    "Bash(gcloud:*)", "Bash(gh pr merge:*)", "Bash(gh workflow:*)", "Bash(gh api -X DELETE:*)", "Bash(git push --force:*)", "Bash(git push -f:*)",
    "Bash(git push origin main:*)", "Bash(git push origin HEAD:main:*)", "Bash(git reset --hard:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
    "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)", "Bash(sudo:*)", "Bash(defaults write:*)", "Bash(security:*)",
    "Bash(git -C * checkout:*)", "Bash(git -C * switch:*)", "Bash(git -C * reset --hard:*)", "Bash(git -C * push --force:*)", "Bash(git -C * push -f:*)",
    "Edit(//Users/multisportusa/Claude_London Commuter/**)", "Write(//Users/multisportusa/Claude_London Commuter/**)",
    "Edit(//**/.github/workflows/**)", "Write(//**/.github/workflows/**)", "Edit(//**/*.xcconfig)", "Write(//**/*.xcconfig)"]
CTL_TIMEOUT = 25 * 60

def coord_brief(cid, a, me, target="COORD"):
    asks = ", ".join(a.get("asks") or []) or "(none)"
    pre = ""
    if ASK_T.match(target or ""):
        k = ask_get(target) or {}
        pre = ("BREAKOUT RUN FOR ASK %s ONLY. JB wrote in this ask's own thread. You are working on this single ask: %s (category %s, state %s, "
               "ETA %s, note: %s). Do the work for this ask only; update ONLY %s in the tracker (`ctl.sh asks set %s ...`); do not add new asks.\n\n") % (
            target, k.get("title", "(not in tracker)"), k.get("cat", "-"), k.get("state", "-"), (k.get("eta") or "-").replace("T", " "),
            (k.get("note") or "-")[:200], target, target)
    if os.environ.get("LC_CTL_SIM"):
        pre += ("SIMULATION (gen/sim_controller.py): this run proves the action path. Verify and report only: ctl.sh refuses queue/hold/unhold/full/"
                "install-tick/phone-free/appium/review here. Do NOT use Write or Edit at all (no queue files, no briefs, no page edits). "
                "Read, Grep, Glob, ctl.sh digest/train/job/fails/asks/log and read-only git/gh are fine. End with the normal last line.\n\n")
    return pre + ("You are the London Commuter CONTROLLER (coordinator). JB asked in the Control Room chat; you already told him what you are doing "
            "and by when. Now DO it, fully, right now. Time: %s.\n\nJB's message:\n<<<\n%s\n>>>\nYour summary of the work: %s\n"
            "Asks to update as you go: %s\n\n"
            "How:\n- You have a full shell (JB authorized 28 Oct 12:01) within the standing rules: no gcloud, secrets, hand merges (the train merges), "
            "force pushes, branch switching in the main checkout, workflow runs, phone settings; app code changes go through lanes.\n"
            "- Your toolkit first: %s <command> (no args lists them): digest, queue, hold, unhold, train, job, fails, full, install-tick, phone, "
            "phone-free, appium-start, appium-check, review, asks, log. Start with `%s digest`.\n"
            "- Rules: %s (read the sections that matter; CLAUDE.md wins). JB's standing rule: his asks are P0; when a slot is free take it for his "
            "work and hold non-P0 queued lanes (`ctl.sh hold <L>`). Max 2 code lanes; no new lane at >= 95%% weekly usage.\n"
            "- Lanes do code: write a brief (< 8 KB, ending with the lane end-line contract from the coordinator brief) under %s/v2/briefs/ and run "
            "`ctl.sh queue <L> <task> <brief> sonnet high`. Never run scripts/lane.sh. Never edit the app repo.\n"
            "- Control Room page or scripts: edit files under ~/.lc-control-room; say in your last line that they need publishing.\n"
            "- Keep JB's asks current at every step: `ctl.sh asks set <id> --state WORKING|PARTIAL|DONE --eta HH:MM --note \"...\"` (every state except STAGED needs an ETC; lanes too: `ctl.sh eta <L> HH:MM`); split a big ask with "
            "`ctl.sh asks add --title ... --cat ... --eta HH:MM --msg %s`. DONE only when verifiably done (job PASS, lane started, page edited, ...).\n"
            "- Log each action: `ctl.sh log \"...\"`. Be economical: never read large files whole; use the toolkit.\n"
            "- Other Controller runs may be working in parallel: FIRST read the last 20 lines of %s. Never reverse a hold/unhold/queue made there in the "
            "last 60 minutes unless the situation changed, and say why. One lane per PR: before queueing a fix, check the digest for a lane already on "
            "that PR or branch (it owns the fix). Never remove lock directories.\n\n"
            "Your LAST line must be exactly one of:\nDONE <what was done, with ids>\nPARTIAL <what was done> | NEXT <what continues and when>\n"
            "NEEDS JB <the one physical step only JB can do, e.g. unlock the phone>\n") % (
        now(), (me or {}).get("text", "")[:12000], a.get("req", "")[:400], asks, CTL, CTL, COORD_BRIEF, NOTES, cid, COORD_LOG)

def run_usage(p):
    """Tokens a headless run actually processed, from its stream-json log: per request the whole context is re-read."""
    T = [0, 0, 0, 0]; pk = 0; n = 0; res = {}
    try: lines = open(p).read().splitlines()
    except OSError: return None
    for l in lines:
        try: e = json.loads(l)
        except ValueError: continue
        if not isinstance(e, dict): continue
        if e.get("type") == "assistant":
            u = (e.get("message") or {}).get("usage") or {}
            v = [int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")]
            if any(v): T = [x + y for x, y in zip(T, v)]; pk = max(pk, sum(v[:3])); n += 1
        if e.get("type") == "result": res = e
    return {"req": n, "in": T[0], "cw": T[1], "cr": T[2], "out": T[3], "total": sum(T), "peak": pk,
            "cost": res.get("total_cost_usd"), "secs": round((res.get("duration_ms") or 0) / 1000, 1),
            "model": ",".join((res.get("modelUsage") or {}).keys())}

def usage_line(rd):
    """One measured line for JB: answer run + action run, from their logs (JB 28 Oct 12:28)."""
    a, b = run_usage(os.path.join(rd, "out.json")), run_usage(os.path.join(rd, "act.jsonl"))
    parts, tot, cost = [], 0, 0.0
    for name, u in (("answer", a), ("action", b)):
        if not u: continue
        tot += u["total"]; cost += u["cost"] or 0
        parts.append("%s %s: %d requests, %s tokens processed (%s cache reads, %s output), peak context %s, $%.3f, %ss" % (
            name, u["model"] or "?", u["req"], format(u["total"], ","), format(u["cr"], ","), format(u["out"], ","), format(u["peak"], ","), u["cost"] or 0, u["secs"]))
    return ("Usage (measured from the run logs): " + "; ".join(parts) + ". Total %s tokens, $%.3f." % (format(tot, ","), cost)) if parts else ""

def coord_act(target, cid, a, me):
    """The Controller's action run: one headless Claude run with the toolkit (not lane.sh: no worktree, no code-lane slot)."""
    rd = a.get("rundir") or os.path.join(CDIR, "run", cid); os.makedirs(rd, exist_ok=True)
    brief = coord_brief(cid, a, me, target); open(os.path.join(rd, "act_brief.md"), "w").write(brief)
    sp = os.path.join(rd, "act_settings.json")
    allow, deny = CTL_ALLOW, CTL_DENY
    if os.environ.get("LC_CTL_SIM"):   # simulations only ever narrow the Controller: no Write/Edit at all
        allow = ["Read", "Grep", "Glob", "Bash(%s:*)" % CTL, "Bash(git log:*)", "Bash(git status:*)", "Bash(gh pr view:*)", "Bash(gh pr list:*)"]
        deny = CTL_DENY + ["Write", "Edit"]
    json.dump({"enableArtifact": False, "disableWorkflows": True, "permissions": {"allow": allow, "deny": deny, "defaultMode": "default"}}, open(sp, "w"), indent=1)
    mp = os.path.join(rd, "mcp.json")
    if not os.path.exists(mp): json.dump({"mcpServers": {}}, open(mp, "w"))
    model = "opus" if (me or {}).get("depth") == "deep" else "sonnet"
    out = os.path.join(rd, "act.jsonl")
    cmd = [CL_BIN, "-p", brief, "--model", MODELS[model], "--settings", sp, "--setting-sources", "project,local",
           "--strict-mcp-config", "--mcp-config", mp, "--add-dir", NOTES, "--add-dir", ROOT,
           "--allowedTools", ",".join(allow), "--permission-mode", "default", "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
    if "--dry-act" in A: open(os.path.join(rd, "act_cmd.json"), "w").write(json.dumps(cmd)); return "DONE dry run", "done"
    try:
        with open(out, "w") as fo:
            subprocess.run(cmd, cwd=LC if os.path.isdir(LC) else HOME, env=dict(os.environ, LC_NOTES=NOTES, LC_COORD_LOG=COORD_LOG), stdout=fo, stderr=subprocess.STDOUT, timeout=CTL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "PARTIAL the run hit its %d-minute limit | NEXT the watchdog re-wakes the Controller" % (CTL_TIMEOUT // 60), "partial"
    except OSError as e:
        return "PARTIAL could not start the run: %s" % e, "partial"
    res, _ = read_out(out)
    lines = [l.strip() for l in str(res.get("result") or "").splitlines() if l.strip()]
    last = next((l for l in reversed(lines) if re.match(r"^(DONE|PARTIAL|NEEDS JB|BLOCKED)\b", l)), lines[-1] if lines else "")
    st = "done" if last.startswith("DONE") else "blocked" if last.startswith(("NEEDS JB", "BLOCKED")) else "partial"
    with open(COORD_LOG, "a") as f: f.write("%s coord(%s): %s\n" % (now()[:16].replace("T", " "), lane_id(cid), last[:300]))
    # JB 28 Oct 12:28: post the whole final message (a report must reach JB intact), then the measured usage
    body = "\n".join(l for l in str(res.get("result") or "").strip().splitlines() if l.strip() != last).strip()
    try: open(os.path.join(rd, "act_full.md"), "w").write(SECRET.sub("[redacted]", body)[:6000] + "\n\n" + usage_line(rd))
    except OSError: pass
    return last or "PARTIAL the run ended without a result line", st

def act_run(target, cid):
    with Thread(target) as d:
        me = next((x for x in d["msgs"] if x.get("id") == cid), None)
        a = (me or {}).get("act") or {}
    if not a.get("brief") or a.get("state") != "acting": raise SystemExit("no acting request %s in %s" % (cid, target))
    if is_coord(target):
        sig, st = coord_act(target, cid, a, me)
        finish_act(target, cid, st, sig, a["lane"]); act_next(target); return
    lane = a["lane"]; task = "chat-" + re.sub(r"[^a-z0-9-]", "", cid.lower())[:30]
    sf = os.path.join(NOTES, "status", lane + ".txt")
    try: os.remove(sf)
    except OSError: pass
    env = dict(os.environ, LC_NOTES=NOTES)
    cmd = [LANE_SH, lane, task, "--role", "coord", "--model", "sonnet", "--effort", "medium", "--brief", a["brief"]]
    if target == "COORD":
        # the Controller never waits behind code lanes: it may take the slot above LC_MAX_LANES (lanes only probe up to LC_MAX_LANES)
        env["LC_MAX_LANES"] = str(int(os.environ.get("LC_MAX_LANES") or 2) + 1)
        deep = (me or {}).get("depth") == "deep"
        cmd = [LANE_SH, lane, task, "--role", "coord", "--model", "opus" if deep else "sonnet", "--effort", "high", "--brief", a["brief"]]
    state, sig = "failed", ""
    try:
        r = subprocess.run(cmd, cwd=LC if os.path.isdir(LC) else HOME, env=env, capture_output=True, text=True, timeout=ACT_TIMEOUT)
        rc, err = r.returncode, (r.stderr or r.stdout or "").strip().splitlines()[-1:] 
    except subprocess.TimeoutExpired:
        rc, err = -1, ["timed out after %d minutes" % (ACT_TIMEOUT // 60)]
    except OSError as e:
        rc, err = -2, [str(e)]
    try:
        ln = open(sf).read().splitlines()
        state = (ln[0].split() or ["failed"])[0]; sig = ln[1] if len(ln) > 1 else ""
    except OSError:
        if rc == 75: state = "parked"
        else: sig = "BLOCKED lane.sh ended (exit %s) without a status file: %s. JB: check %s/announce.log" % (rc, (err or ["no output"])[0][:200], NOTES)
    if state == "running": state, sig = "failed", sig or "BLOCKED the run was still marked running when it stopped"
    finish_act(target, cid, state, sig, lane)
    act_next(target)

def inject(target, text):
    """wake.py: a judgement event becomes one message from "Watchdog" in the target's thread and one answer run (then act)."""
    if not TARGET.match(target): raise SystemExit("bad target")
    cid = "w" + dt.datetime.now().strftime("%m%d%H%M%S")
    with Thread(target) as d:
        d["msgs"].append({"id": cid, "from": "Watchdog", "at": now(), "text": SECRET.sub("[redacted]", text)[:2000], "depth": "auto", "pending": True})
    log("INJECT %s %s" % (target, cid)); spawn(target, cid)

def sweep_acts(target, d):
    """sync(): an action that never reported (Mac restart) is closed as blocked. Caller holds the thread lock."""
    for x in list(d["msgs"]):
        a = x.get("act") or {}
        if a.get("state") == "acting":
            try: age = (dt.datetime.now() - dt.datetime.fromisoformat(a.get("at") or x["at"])).total_seconds()
            except (ValueError, KeyError): continue
            if age > ACT_STALE_MIN * 60:
                a["state"] = "blocked"
                d["msgs"].append({"id": x["id"] + "a", "fu": x["id"], "from": "Claude (acting)", "at": now(), "error": True,
                                  "text": "BLOCKED the action run %s never reported (the Mac may have restarted). JB: ask again or open a session." % a.get("lane", "")})

if __name__ == "__main__":
    pos = [a for i, a in enumerate(A) if not a.startswith("--") and (i == 0 or not A[i - 1] in ("--notes", "--repo", "--key", "--lc-repo", "--limit", "--lane-sh", "--act-timeout", "--chat-dir", "--asks-notes", "--coord-log"))]
    if pos[:1] == ["plan"]: plan(pos[1], pos[2], pos[3])
    elif pos[:1] == ["store"]: store(pos[1], pos[2], pos[3])
    elif pos[:1] == ["act-run"]: act_run(pos[1], pos[2])
    elif pos[:1] == ["inject"]: inject(pos[1], " ".join(pos[2:]))
    else: sync()
