#!/usr/bin/env python3
"""Control Room chat, Mac side. 0 model tokens here; each message gets ONE fresh, short, read-only headless Claude run.

The page posts an issue titled "CHAT <target>" (target: V3, V2 or L-<lane>) whose body is encrypted in the browser with
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
CDIR = os.path.join(NOTES, "w1", "chat")
LIMIT = int(arg("--limit", "20"))          # messages per rolling hour, all targets together
STALE_MIN = 20                             # a pending message with no answer after this gets a "send again" note
LANE_SH = arg("--lane-sh", os.path.join(LC, "scripts", "lane.sh"))
ACT_STALE_MIN = 100                        # an action run that never reported is closed as blocked after this
ACT_TIMEOUT = int(arg("--act-timeout", str(90 * 60)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GH = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
OPENSSL = "/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else (shutil.which("openssl") or "openssl")
TITLE = re.compile(r"^\s*CHAT\s+(COORD|V3|V2|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})\s*$")
TARGET = re.compile(r"^(COORD|V3|V2|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})$")
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
        text = str(msg["m"]).strip()[:4000]
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
COORD_LOG = os.path.join(NOTES, "v2", "sup", "COORD_LOG.md")
DIGEST = os.path.join(NOTES, "v2", "sup", "digest.py")
ROLE = {"COORD": "the London Commuter Controller: the coordinator that owns the lanes, the queue, reviews, the merge train and the plan (JB's one point of contact)",
        "V3": "the V3 coordinator (London Commuter v3 rebuild, Week 1 plan)",
        "V2": "the V2 coordinator (the current London Commuter app's Roadmap to Beta)"}
def sources(target):
    w1 = os.path.join(NOTES, "w1")
    ctx = "- %s (compact snapshot of the Control Room data: tasks, status, PRs, decisions, fuel; start here)\n" % "CONTEXT_FILE"
    if target == "COORD":
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

def plan(target, cid, rd):
    if not TARGET.match(target) or not CID.match(cid): raise SystemExit("bad target or id")
    os.makedirs(os.path.join(rd, "cwd"), exist_ok=True)  # empty working dir: no CLAUDE.md, no project settings
    with Thread(target) as d: msgs = list(d["msgs"])
    me = next((x for x in msgs if x.get("id") == cid and x.get("from") in ("JB", "Watchdog")), None)
    if not me: raise SystemExit("message %s not in thread %s" % (cid, target))
    if target == "COORD":
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
    if target == "COORD":
        prompt = prompt.replace("JB's new message:", "JB's new message (from \"Watchdog\" = an automatic judgement event, not JB):" if me.get("from") == "Watchdog" else "JB's new message:")
        prompt += ("- You are the Controller. Things must keep moving without JB having to ask twice: if the message asks for anything to happen, "
                   "or the digest shows a FLAG that needs acting on, choose INTENT: act. Answer with facts from the digest (lanes, PRs, usage), not from memory.\n")
    model = route(me.get("depth"), me["text"])
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
    if target == "COORD": f = os.path.join(NOTES, "v2", "sup", "JB-to-controller.md")
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
def act_running(d, me_id):
    """The other action in this thread that is still running (acting, not stale), else None."""
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
    if target == "COORD":
        head = head.replace("CHAT ACTION RUN.", "CONTROLLER RUN (you are the London Commuter Controller/coordinator).", 1)
        head += ("You are the Controller. First read %s (the coordinator brief) and %s/digest.txt (the digest, generated minutes ago). Then do what JB asked "
                 "AND act on every FLAG in the digest, as the brief says. Launch or requeue lanes ONLY by writing %s/v2/queue/<L>-<task>.req (7 lines) with the "
                 "Write tool; lc_watch starts it within a minute. NEVER run scripts/lane.sh yourself (a lane started from your shell dies with you). Max %s code "
                 "lanes; no new lane at >= 85%% weekly usage. Things you cannot do with your tools (git push, gh pr create, review.sh, test-submit) go in your last line "
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
    text = SECRET.sub("[redacted]", text)[:6000]
    if err: text = "Sorry, the answer run failed. Please send it again (or choose Deep)."
    started, behind = "", None
    with Thread(target) as d:
        me = next((x for x in d["msgs"] if x.get("id") == cid and x.get("from") in ("JB", "Watchdog")), None)
        if intent == "act" and me is not None:
            req = SECRET.sub("[redacted]", req or me.get("text", ""))[:400]
            me["act"] = {"state": "answered", "req": req, "at": now(), "lane": lane_id(cid), "rundir": rd}
            behind = act_running(d, cid)
            if behind: me["act"].update(state="queued", behind=behind["act"].get("lane") or behind["id"])
            else: begin_act(d, me, target, rd); started = me["act"]["lane"]
            if not isinstance(rec, dict) or rec.get("type") != "decision": rec = {"type": "note", "text": (rec or {}).get("text") or req}
            rec = dict(rec); 
            if rec.get("type") == "note": rec["text"] = "%s [%s]" % (str(rec["text"]).strip(), ("started action run " + started) if started else "queued behind " + str(me["act"]["behind"]))
        if rec is not None:
            try: note = record(target, cid, rec)
            except Exception as e: note = "(Could not record the action: %s.)" % e
        for x in d["msgs"]:
            if x.get("id") == cid: x["pending"] = False
        d["msgs"] = [x for x in d["msgs"] if x.get("re") != cid]
        if started: note = (note + "\n" if note else "") + "Acting: started action run %s; I will post what it did here." % started
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
    blocked = state_word in ("blocked", "needs-coordinator", "no-signal", "parked", "failed") or sig.upper().startswith("BLOCKED")
    if blocked:
        line = sig if sig.upper().startswith("BLOCKED") else "BLOCKED %s" % (sig or {"needs-coordinator": "the run needs a coordinator (hand-off or escalation)", "parked": "the lane is parked (health gate or 3 lanes running); lc_watch.sh retries it", "no-signal": "the run ended without a result line"}.get(state_word, "the run did not finish: " + state_word))
    else:
        line = sig if sig.upper().startswith("DONE") else "DONE %s" % (sig or "(no detail)")
    ev = evidence(lane, state_word, sig)
    with Thread(target) as d:
        me = next((x for x in d["msgs"] if x.get("id") == cid), None)
        if me is not None and me.get("act"): me["act"]["state"] = "blocked" if blocked else "done"
        d["msgs"] = [x for x in d["msgs"] if x.get("id") != cid + "a"]
        d["msgs"].append({"id": cid + "a", "fu": cid, "from": "Claude (acting)", "at": now(), "error": blocked,
                          "text": line + "".join("\n- " + e for e in ev)})
    log("ACT %s %s %s %s" % ("BLOCKED" if blocked else "DONE", target, cid, line[:80]))

def act_next(target):
    """Start the oldest queued action of this thread if none is running."""
    nxt = None
    with Thread(target) as d:
        if act_running(d, ""): return
        q = next((x for x in d["msgs"] if (x.get("act") or {}).get("state") == "queued"), None)
        if q:
            rd = q["act"].get("rundir") or os.path.join(CDIR, "run", q["id"]); os.makedirs(rd, exist_ok=True)
            begin_act(d, q, target, rd); nxt = q["id"]
            d["msgs"].append({"id": nxt + "s", "fu": nxt, "from": "Claude (acting)", "at": now(), "text": "Starting action run %s now." % q["act"]["lane"]})
    if nxt: spawn_act(target, nxt)

def act_run(target, cid):
    with Thread(target) as d:
        me = next((x for x in d["msgs"] if x.get("id") == cid), None)
        a = (me or {}).get("act") or {}
    if not a.get("brief") or a.get("state") != "acting": raise SystemExit("no acting request %s in %s" % (cid, target))
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
    pos = [a for i, a in enumerate(A) if not a.startswith("--") and (i == 0 or not A[i - 1] in ("--notes", "--repo", "--key", "--lc-repo", "--limit", "--lane-sh", "--act-timeout"))]
    if pos[:1] == ["plan"]: plan(pos[1], pos[2], pos[3])
    elif pos[:1] == ["store"]: store(pos[1], pos[2], pos[3])
    elif pos[:1] == ["act-run"]: act_run(pos[1], pos[2])
    elif pos[:1] == ["inject"]: inject(pos[1], " ".join(pos[2:]))
    else: sync()
