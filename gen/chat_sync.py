#!/usr/bin/env python3
"""Control Room chat, Mac side. 0 model tokens here; each message gets ONE fresh, short, read-only headless Claude run.

The page posts an issue titled "CHAT <target>" (target: V3, V2 or L-<lane>) whose body is encrypted in the browser with
the page passphrase (openssl enc format: "Salted__" + salt, PBKDF2-SHA256 200000 iterations, AES-256-CBC, base64), so the
public issue holds nothing readable. Commands:
  chat_sync.py [--notes DIR] [--repo o/n]        sync: read open CHAT issues (owner only), decrypt, rate-limit, append to
                                                 <notes>/w1/chat/<target>.json, comment + close, start mac/chat_reply.sh
  chat_sync.py plan <target> <cid> <rundir>      write the prompt, model, settings and empty MCP config for one answer
  chat_sync.py store <target> <cid> <out.json>   put the answer (claude -p JSON) in the thread; record any action
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
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GH = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
OPENSSL = "/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else (shutil.which("openssl") or "openssl")
TITLE = re.compile(r"^\s*CHAT\s+(V3|V2|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})\s*$")
TARGET = re.compile(r"^(V3|V2|L-[A-Za-z0-9][A-Za-z0-9-]{0,11})$")
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
            for x in d["msgs"]:
                if x.get("pending") and x.get("at") and (dt.datetime.now() - dt.datetime.fromisoformat(x["at"])).total_seconds() > STALE_MIN * 60:
                    x["pending"] = False
                    d["msgs"].append({"id": x["id"] + "r", "re": x["id"], "from": "Claude", "at": now(), "model": "", "error": True,
                                      "text": "No answer arrived within %d minutes (the run may have failed). Please send it again." % STALE_MIN})

# ---------- one answer: prompt, profile, store ----------
ROLE = {"V3": "the V3 coordinator (London Commuter v3 rebuild, Week 1 plan)",
        "V2": "the V2 coordinator (the current London Commuter app's Roadmap to Beta)"}
def sources(target):
    w1 = os.path.join(NOTES, "w1")
    ctx = "- %s (compact snapshot of the Control Room data: tasks, status, PRs, decisions, fuel; start here)\n" % "CONTEXT_FILE"
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
    me = next((x for x in msgs if x.get("id") == cid and x.get("from") == "JB"), None)
    if not me: raise SystemExit("message %s not in thread %s" % (cid, target))
    hist = [x for x in msgs[: msgs.index(me)]][-6:]
    ctxf = os.path.join(rd, "context.json"); context_snapshot(ctxf)
    role = ROLE.get(target) or "the status voice of London Commuter lane %s (you are NOT that lane's running session; you read its status file)" % target[2:]
    rec = {"V2": "a note the V2 coordinator reads (type \"note\")",
           "V3": "a decision card for the coordinator (type \"decision\") or an action note (type \"note\")"}.get(target, "a note for that lane (type \"note\") or a decision card (type \"decision\")")
    prompt = ("You are answering JB in the London Commuter Control Room chat, speaking as %s. This is one short, fresh, read-only run.\n\n"
              "Sources (read only what you need; quote facts from them, do not guess):\n%s\n"
              "Conversation so far (oldest first):\n%s\n\nJB's new message:\n<<<\n%s\n>>>\n\n"
              "Rules:\n- Answer concisely in plain English (at most about 150 words). Say how fresh the facts are when it matters.\n"
              "- You cannot change code, git or any file; you only read. Never claim you did something.\n"
              "- If JB asks for an action, record it as %s by ending your answer with exactly one fenced block:\n"
              "```record\n{\"type\": \"note\", \"text\": \"<the instruction, one or two lines>\"}\n```\n"
              "  or for a decision card: ```record\n{\"type\": \"decision\", \"title\": \"...\", \"situation\": \"...\", \"why_now\": \"...\", "
              "\"options\": [{\"key\": \"A\", \"label\": \"...\", \"detail\": \"...\"}, {\"key\": \"B\", \"label\": \"...\", \"detail\": \"...\"}], \"recommend\": \"A\", \"because\": \"...\"}\n```\n"
              "  and say in the answer that you recorded it and who picks it up. Otherwise add no block.\n"
              "- Never reveal or read secrets: tokens, keys, passphrases, .env, BrokerKey.xcconfig, build logs, preference plists.\n"
              "- Do not use tools you were not given; if something needs a session, say so.\n") % (
        role, sources(target).replace("CONTEXT_FILE", ctxf),
        "\n".join("[%s %s] %s" % (x.get("at", "")[11:16], x.get("from"), (x.get("text") or "")[:1200]) for x in hist) or "(none)",
        me["text"], rec)
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
    if target == "V2": f = os.path.join(NOTES, "status", "JB-to-V2-coordinator.md")
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

def store(target, cid, outp):
    o, start = read_out(outp)
    try:
        text = str(o.get("result") or "").strip(); err = bool(o.get("is_error")) or not text
        model = ",".join((o.get("modelUsage") or {}).keys()); cost = o.get("total_cost_usd")
    except AttributeError:
        text, err, model, cost = "", True, "", None
    note = ""
    m = re.search(r"```record\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try: note = record(target, cid, json.loads(m.group(1)))
        except ValueError: note = "(Could not record the action: the record was not valid JSON.)"
        text = (text[: m.start()] + text[m.end():]).strip()
    text = SECRET.sub("[redacted]", text)[:6000]
    if err: text = "Sorry, the answer run failed. Please send it again (or choose Deep)."
    with Thread(target) as d:
        for x in d["msgs"]:
            if x.get("id") == cid: x["pending"] = False
        d["msgs"] = [x for x in d["msgs"] if x.get("re") != cid]
        d["msgs"].append({"id": cid + "r", "re": cid, "from": "Claude", "at": now(), "text": (text + ("\n\n" + note if note else "")).strip(),
                          "model": model, "error": err, "cost": cost, "start_tokens": start})
    log("ANSWERED %s %s (%s) start=%s%s" % (target, cid, model or "-", start, " ERROR" if err else ""))

if __name__ == "__main__":
    pos = [a for i, a in enumerate(A) if not a.startswith("--") and (i == 0 or not A[i - 1] in ("--notes", "--repo", "--key", "--lc-repo", "--limit"))]
    if pos[:1] == ["plan"]: plan(pos[1], pos[2], pos[3])
    elif pos[:1] == ["store"]: store(pos[1], pos[2], pos[3])
    else: sync()
