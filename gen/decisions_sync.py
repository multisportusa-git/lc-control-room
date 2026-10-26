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
     and a link) and a macOS notification;
  3. appends every change to <notes>/w1/decisions/decisions.log for the coordinator, which acts at its next step.
Usage: decisions_sync.py [--notes DIR] [--repo owner/name] [--dry-run]"""
import glob, json, os, re, subprocess, sys, time, urllib.request, datetime as dt

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
TITLE = re.compile(r"^\s*DECISION\s+(D-\d+)\s*:\s*([A-Z])\b", re.I)
ACTION = re.compile(r"^\s*ACTION\s+(W1-\d{2}[A-Z]{0,2})\b\s*:?\s*(.*)$", re.I)
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

def task_defs():
    out = subprocess.run(["git", "-C", LC, "show", "origin/main:docs/handover/v3/W1_TASKS.json"], capture_output=True, text=True).stdout
    try: return {t["id"]: t for t in json.loads(out)["tasks"]}
    except (ValueError, KeyError): return {}

def action(it):
    """ACTION W1-nn issues from the owner: write JB's report into the task's evidence file, then close the task with
    lc_state.py done --note (its done check still runs). W1-05 takes three numbers: week %, Claude Code %, Cowork %."""
    n = it["number"]; m = ACTION.match(it["title"]); tid, rest = m.group(1).upper(), m.group(2).strip()
    who = (it.get("author") or {}).get("login", "")
    if who.lower() != OWNER.lower():
        log("ignored action #%d from %s" % (n, who)); gh("issue", "close", str(n), "-R", REPO, "-c", "Only the repo owner can report tasks."); return
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

def topic():
    if os.path.exists(TOPIC_FILE): return open(TOPIC_FILE).read().strip()
    return ""
def notify(decs):
    t = topic()
    for did, (p, d) in sorted(decs.items()):
        if d.get("status", "open") != "open" or d.get("notified"): continue
        msg = "Decision needed: %s. Open the control room to review and approve." % did
        sent = []
        if t and not DRY:
            try:
                req = urllib.request.Request("https://ntfy.sh/" + t, data=msg.encode(), method="POST",
                                             headers={"Title": "London Commuter: decision needed", "Click": PAGE + "#go=dec", "Tags": "railway_car", "Priority": "high"})
                urllib.request.urlopen(req, timeout=15).read(); sent.append("phone")
            except Exception as e:
                log("ntfy failed for %s: %s" % (did, str(e)[:120]))
        if not DRY:
            try:
                subprocess.run(["osascript", "-e", 'display notification "%s" with title "London Commuter" sound name "Glass"' % msg.replace('"', "'")], capture_output=True, timeout=10)
                sent.append("mac")
            except (OSError, subprocess.SubprocessError):
                pass
        d["notified"] = now()
        if not DRY: save(p, d)
        log("notified %s (%s)" % (did, ", ".join(sent) or ("dry run" if DRY else "no channel reachable")))

if __name__ == "__main__":
    os.makedirs(DDIR, exist_ok=True)
    decs = load()
    sync_issues(decs)
    notify(load())
