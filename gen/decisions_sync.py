#!/usr/bin/env python3
"""Decisions: approvals from the hosted page, recorded on JB's Mac. 0 model tokens. Run by mac/push.sh before each build.

A decision is a JSON file in <notes>/w1/decisions/D-<n>.json, written by any session that needs JB:
  {"id": "D-3", "created": "2026-10-26T12:40", "title": "...", "situation": "...", "why_now": "...",
   "options": [{"key": "A", "label": "...", "detail": "...", "pros": ["..."], "cons": ["..."], "cost": "...", "risk": "low"}],
   "recommend": "A", "because": "...", "tasks": ["W1-44"], "deadline": "2026-10-26T18:00", "status": "open"}
The page's Approve button opens a GitHub issue titled "DECISION D-3: A" in the public control-room repo. This script
  1. reads open issues whose title starts with DECISION, accepts them only from the repo owner, writes the choice into
     the decision file (status "decided", choice, at, via, note), comments and closes the issue;
  2. notifies once for each new open decision: a push via ntfy.sh to JB's private topic (no details, just the number
     and a link that opens the page); no macOS banner (it could not open the page);
  3. appends every change to <notes>/w1/decisions/decisions.log for the coordinator, which acts at its next step.
Usage: decisions_sync.py [--notes DIR] [--repo owner/name] [--dry-run]"""
import glob, json, os, re, subprocess, sys, time, datetime as dt

A = sys.argv[1:]
def arg(k, d): return A[A.index(k) + 1] if k in A else d
HOME = os.path.expanduser("~")
NOTES = arg("--notes", os.path.join(HOME, "Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"))
REPO = arg("--repo", "multisportusa-git/lc-control-room")
OWNER = REPO.split("/")[0]
DRY = "--dry-run" in A
DDIR = os.path.join(NOTES, "w1", "decisions")
import shutil
GH = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
TOPIC_FILE = os.path.join(HOME, ".config", "lc-control-room", "ntfy_topic")
PAGE = "https://%s.github.io/%s/" % (OWNER, REPO.split("/")[1])
TITLE = re.compile(r"^\s*DECISION\s+(D-\d+)\s*:\s*(OTHER|[A-Z])\b", re.I)
PHONE = re.compile(r"^\s*PHONE\s+FREE\b", re.I)
ACTION = re.compile(r"^\s*ACTION\s+(W1-\d{2}[A-Z]{0,2}|V2)\b\s*:?\s*(.*)$", re.I)
LC = arg("--lc-repo", os.path.join(HOME, "Claude_London Commuter"))
EV = os.path.join(NOTES, "w1", "evidence")
PY = sys.executable

def now(): return dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
def log(msg):
    os.makedirs(DDIR, exist_ok=True)
    with open(os.path.join(DDIR, "decisions.log"), "a") as f: f.write("%s %s\n" % (now(), msg))
    print(msg)
def gh(*args, t=30):
    if DRY: print("DRY gh", *args); return ""
    r = subprocess.run([GH, *args], capture_output=True, text=True, timeout=t)
    return r.stdout if r.returncode == 0 else ""
def load():
    out = {}
    for p in glob.glob(os.path.join(DDIR, "D-*.json")):
        try: d = json.load(open(p)); out[d["id"]] = (p, d)
        except (OSError, ValueError, KeyError): log("unreadable decision file %s" % os.path.basename(p))
    return out
def save(p, d):
    tmp = p + ".tmp"; json.dump(d, open(tmp, "w"), indent=1); os.replace(tmp, p)

def sync_issues(decs):
    raw = gh("issue", "list", "-R", REPO, "--state", "open", "--limit", "50", "--json", "number,title,body,author")
    try: issues = json.loads(raw) if raw else []
    except ValueError: issues = []
    for it in issues:
        if PHONE.match(it.get("title", "")):
            phone_free(it); continue
        if ACTION.match(it.get("title", "")):
            action(it); continue
        m = TITLE.match(it.get("title", ""))
        if not m: continue
        n, did, choice = it["number"], m.group(1).upper(), m.group(2).upper()
        who = (it.get("author") or {}).get("login", "")
        if who.lower() != OWNER.lower():
            log("ignored issue #%d from %s (only %s can decide)" % (n, who, OWNER))
            gh("issue", "close", str(n), "-R", REPO, "-c", "Only the repo owner can record decisions."); continue
        if did not in decs:
            gh("issue", "close", str(n), "-R", REPO, "-c", "Unknown decision %s; nothing recorded." % did); log("issue #%d: unknown %s" % (n, did)); continue
        p, d = decs[did]
        if choice == "OTHER":
            other(it, p, d); continue
        keys = [o.get("key", "").upper() for o in d.get("options", [])]
        if choice not in keys:
            gh("issue", "close", str(n), "-R", REPO, "-c", "%s has no option %s; nothing recorded." % (did, choice)); log("issue #%d: %s has no option %s" % (n, did, choice)); continue
        if d.get("status") == "decided":
            gh("issue", "close", str(n), "-R", REPO, "-c", "%s was already decided (%s)." % (did, d.get("decided", {}).get("choice"))); continue
        note = (it.get("body") or "").strip()
        note = re.sub(r"(?s)<!--.*?-->", "", note).strip()[:2000]
        d["status"] = "decided"; d["decided"] = {"choice": choice, "by": who, "at": now(), "via": "issue #%d" % n, "note": note}
        if not DRY: save(p, d)
        gh("issue", "close", str(n), "-R", REPO, "-c", "Recorded: %s = %s. The coordinator acts on it at its next step." % (did, choice))
        log("DECIDED %s = %s by %s via issue #%d%s" % (did, choice, who, n, (" note: " + note[:200].replace("\n", " ")) if note else ""))
        run_action(p, d, choice)

def task_defs():
    out = subprocess.run(["git", "-C", LC, "show", "origin/main:docs/handover/v3/W1_TASKS.json"], capture_output=True, text=True).stdout
    try: return {t["id"]: t for t in json.loads(out)["tasks"]}
    except (ValueError, KeyError): return {}

def phone_free(it):
    """PHONE FREE issue from the owner: JB says the iPhone is free and connected. Writes <notes>/w1/phone_free.json
    {"at", "by": "JB", "until": at + 2 h} (the page shows 'iPhone free (since HH:MM)' until then or until a phone job runs)
    and appends a PHONE FREE line to <notes>/w1/ACTIONS-from-JB.md for the coordinator."""
    n = it["number"]; who = (it.get("author") or {}).get("login", "")
    if who.lower() != OWNER.lower():
        log("ignored phone-free #%d from %s" % (n, who)); gh("issue", "close", str(n), "-R", REPO, "-c", "Only the repo owner can say the phone is free."); return
    t0 = dt.datetime.now().replace(second=0, microsecond=0); until = t0 + dt.timedelta(hours=2)
    if not DRY:
        W = os.path.join(NOTES, "w1"); os.makedirs(W, exist_ok=True)
        f = os.path.join(W, "phone_free.json"); tmp = f + ".tmp"
        json.dump({"at": t0.strftime("%Y-%m-%dT%H:%M"), "by": "JB", "until": until.strftime("%Y-%m-%dT%H:%M")}, open(tmp, "w")); os.replace(tmp, f)
        with open(os.path.join(W, "ACTIONS-from-JB.md"), "a") as fh:
            fh.write("%s JB (issue #%d): PHONE FREE - iPhone is free and connected until %s; phone jobs may run now.\n" % (now(), n, until.strftime("%H:%M")))
    gh("issue", "close", str(n), "-R", REPO, "-c", "Recorded: iPhone free until %s. The coordinator can run phone jobs." % until.strftime("%H:%M"))
    log("PHONE FREE via issue #%d until %s" % (n, until.strftime("%H:%M")))

def action(it):
    """ACTION W1-nn issues from the owner: write JB's report into the task's evidence file, then close the task with
    lc_state.py done --note (its done check still runs). W1-05 takes three numbers: week %, Claude Code %, Cowork %."""
    n = it["number"]; m = ACTION.match(it["title"]); tid, rest = m.group(1).upper(), m.group(2).strip()
    who = (it.get("author") or {}).get("login", "")
    if who.lower() != OWNER.lower():
        log("ignored action #%d from %s" % (n, who)); gh("issue", "close", str(n), "-R", REPO, "-c", "Only the repo owner can report tasks."); return
    if tid == "V2":
        # a reply to the V2 coordinator: appended where it reads JB's answers
        text = (rest + "\n" + re.sub(r"(?s)<!--.*?-->", "", it.get("body") or "").strip()).strip()
        f = os.path.join(NOTES, "status", "JB-to-V2-coordinator.md")
        if not DRY:
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "a") as fh: fh.write("%s JB (issue #%d): %s\n" % (now(), n, text))
        gh("issue", "close", str(n), "-R", REPO, "-c", "Passed to the V2 coordinator.")
        log("V2 REPLY via issue #%d: %s" % (n, text[:200].replace("\n", " "))); return
    T = task_defs(); t = T.get(tid)
    if not t or t.get("owner") != "JB":
        gh("issue", "close", str(n), "-R", REPO, "-c", "%s is not one of JB's tasks; nothing recorded." % tid); log("action #%d: %s not a JB task" % (n, tid)); return
    body = re.sub(r"(?s)<!--.*?-->", "", it.get("body") or "").strip()[:4000]
    ev = os.path.join(EV, t.get("evidence") or (tid + ".md"))
    if tid == "W1-05":
        nums = re.findall(r"\d+(?:\.\d+)?", rest or body)
        if not nums:
            gh("issue", "close", str(n), "-R", REPO, "-c", "No numbers found; nothing recorded."); return
        w, c, k = (nums + ["", ""])[:3]
        line = "%s,%s,%s,%s" % (dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ"), w, c, k)
        if not DRY:
            with open(ev, "a") as f: f.write(line + "\n")
        note = "JB via issue #%d: %s%% (Claude Code %s, Cowork %s)" % (n, w, c or "-", k or "-")
    else:
        text = (rest + "\n" + body).strip() or "done"
        if not DRY:
            with open(ev, "a") as f: f.write("%s\n(recorded from issue #%d by %s at %s)\n" % (text, n, who, now()))
        note = "JB via issue #%d: %s" % (n, text[:300].replace("\n", " "))
    r = subprocess.run([PY, os.path.join(LC, "scripts", "lc_state.py"), "done", tid, "--note", note], cwd=LC, capture_output=True, text=True, timeout=120) if not DRY else None
    ok = r is not None and r.returncode == 0
    msg = ("Recorded in %s and %s closed." % (os.path.basename(ev), tid)) if ok else ("Recorded in %s; %s not closed yet: %s" % (os.path.basename(ev), tid, ((r.stdout + r.stderr).strip()[-200:] if r else "dry run")))
    gh("issue", "close", str(n), "-R", REPO, "-c", msg)
    log("ACTION %s via issue #%d: %s" % (tid, n, msg))

CLAUDE = next((x for x in (os.path.join(HOME, ".local/bin/claude"), "/opt/homebrew/bin/claude") if os.path.exists(x)), "claude")
LANE_SETTINGS = os.path.join(NOTES, "w1", "evidence", "lane-settings-v2.json")

def other(it, p, d):
    """DECISION D-n: OTHER — JB asks a question or proposes something else. The text joins the decision's thread and a
    background Claude session (Opus high: decision briefs are judgement work) answers on the card, revising the options
    or recommendation if he asked for that. The decision stays open for his final answer."""
    n = it["number"]; who = (it.get("author") or {}).get("login", "")
    text = re.sub(r"(?s)<!--.*?-->", "", it.get("body") or "").strip()[:3000]
    if not text:
        gh("issue", "close", str(n), "-R", REPO, "-c", "Empty question; nothing recorded."); return
    d.setdefault("thread", []).append({"from": "JB", "at": now(), "text": text, "via": "issue #%d" % n})
    d["question_pending"] = True; d["status"] = "open"
    if not DRY: save(p, d)
    gh("issue", "close", str(n), "-R", REPO, "-c", "Question recorded on %s; the answer appears on the card." % d["id"])
    log("QUESTION %s via issue #%d: %s" % (d["id"], n, text[:200].replace("\n", " ")))
    if DRY or not os.path.exists(CLAUDE): return
    prompt = ("You answer JB's question on decision %(id)s of the London Commuter project, in the background (JB is not in this chat; zero chat). "
              "Read CLAUDE.md at the repo root and the decision file %(path)s (JSON). JB's newest message is the last item of \"thread\". "
              "Check any fact against the repo, the notes folder (%(notes)s) or the task state before stating it. Then edit ONLY that file: "
              "append {\"from\": \"Claude\", \"at\": \"<YYYY-MM-DDTHH:MM local>\", \"text\": \"<answer, plain English, at most 150 words>\"} to \"thread\"; "
              "if JB asked for a change (different recommendation, a new or reworded option, other facts), make it in \"options\", \"recommend\" and \"because\" "
              "and say what changed in your answer; set \"question_pending\" to false; keep \"status\" \"open\"; keep every other field; write valid JSON. "
              "Change nothing else and run nothing that changes anything else.") % {"id": d["id"], "path": p, "notes": NOTES}
    out = os.path.join(DDIR, "%s.answer.json" % d["id"])
    cmd = [CLAUDE, "-p", prompt, "--model", "opus", "--effort", "high", "--permission-mode", "auto", "-n", "Q %s" % d["id"], "--output-format", "json"]
    if os.path.exists(LANE_SETTINGS): cmd[1:1] = ["--settings", LANE_SETTINGS]
    with open(out, "w") as f:
        subprocess.Popen(cmd, cwd=LC if os.path.isdir(LC) else HOME, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    log("ANSWERING %s in the background (opus high)" % d["id"])

def run_action(p, d, choice):
    """A decision file may name what to do for each answer: "run": {"A": "<shell command>"}. Only the local file (written
    by a session on this Mac) names commands; nothing from the issue is ever executed. Runs in the background from the
    London Commuter checkout; output goes to <decisions>/<id>.run.log."""
    cmd = (d.get("run") or {}).get(choice)
    if not cmd or DRY: return
    logf = os.path.join(DDIR, "%s.run.log" % d["id"])
    f = open(logf, "a"); f.write("%s start %s: %s\n" % (now(), choice, cmd)); f.flush()
    subprocess.Popen(["/bin/zsh", "-c", cmd], cwd=LC if os.path.isdir(LC) else HOME, stdout=f, stderr=f, start_new_session=True)
    d["acted"] = "%s started the action for %s" % (now(), choice)
    save(p, d); log("ACTED %s: started the action for %s" % (d["id"], choice))

def topic():
    if os.path.exists(TOPIC_FILE): return open(TOPIC_FILE).read().strip()
    return ""
def notify(decs):
    t = topic()
    for did, (p, d) in sorted(decs.items()):
        if d.get("status", "open") != "open" or (d.get("notified") and d.get("notified_phone", True)): continue
        msg = "Decision needed: %s. Open the control room to review and approve." % did
        sent = []
        if t and not DRY:
            # system curl uses the macOS keychain certificates (python.org builds may have none)
            r = subprocess.run(["curl", "-fsS", "-m", "15", "-H", "Title: London Commuter: decision needed", "-H", "Click: " + PAGE + "#go=dec",
                                "-H", "Tags: railway_car", "-H", "Priority: high", "-d", msg, "https://ntfy.sh/" + t], capture_output=True, text=True)
            if r.returncode == 0: sent.append("phone")
            else: log("ntfy failed for %s: %s" % (did, (r.stderr or r.stdout)[:120]))
        # No macOS banner: an osascript notification can't carry a link, so clicking it opened
        # Script Editor's Applications folder (JB, A101). The ntfy push above opens the page.
        d["notified"] = now(); d["notified_phone"] = "phone" in sent or not t
        if not DRY: save(p, d)
        log("notified %s (%s)" % (did, ", ".join(sent) or ("dry run" if DRY else "no channel reachable")))

if __name__ == "__main__":
    os.makedirs(DDIR, exist_ok=True)
    decs = load()
    sync_issues(decs)
    notify(load())
