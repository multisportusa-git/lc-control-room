"""JB's asks tracker (JB 28 Oct 10:55): every question or ask JB sends gets an entry with a state and an ETC, shown at the top of
the Control Room. Store: <notes>/w1/asks.json (locked read-modify-write). 0 model tokens.

States (JB's words): STAGED, WORKING, PARTIAL (partially done), BLOCKED (dark red), DONE. Flags: needs_jb (waiting on JB), blocked.
ETC rule (JB 28 Oct 15:52): every state except STAGED carries an ETC. add/set refuse a WORKING/PARTIAL/BLOCKED ask
without one; DONE without one takes its finish time. `asks.py backfill` fills the gaps in old entries.
RAG (computed): red = overdue (ETC passed, not DONE) or blocked; amber = STAGED, PARTIAL or waiting on JB; green = WORKING on time or DONE.

  asks.py list [--open]
  asks.py add --title T --cat C [--eta HH:MM|ISO] [--state S] [--msg CID] [--note N] [--needs-jb]
  asks.py set <id> [--state S] [--eta HH:MM|ISO] [--note N] [--needs-jb yes|no] [--blocked yes|no]
Categories: Install & phone | Commute fixes | Merge train | Control Room | Ops & tooling | Question
"""
import datetime as dt, difflib, fcntl, json, os, re, sys, time

NOTES = os.environ.get("LC_ASKS_NOTES") or os.path.join(os.path.expanduser("~"), "Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08")
P = os.path.join(NOTES, "w1", "asks.json")
STATES = ("STAGED", "WORKING", "PARTIAL", "BLOCKED", "DONE")
CATS = ("Install & phone", "Commute fixes", "Merge train", "Control Room", "Ops & tooling", "Question")

def now(): return dt.datetime.now()
def iso(t): return t.strftime("%Y-%m-%dT%H:%M")
def norm_eta(s):
    if not s: return ""
    s = str(s).strip()
    m = re.match(r"^([0-2]?\d):([0-5]\d)$", s)
    if m:
        t = now().replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t < now() - dt.timedelta(hours=1): t += dt.timedelta(days=1)
        return iso(t)
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d)", s)
    return m.group(1) if m else ""
def norm_state(s):
    s = (s or "").strip().upper().replace(" ", "_")
    return {"PARTIALLY_DONE": "PARTIAL", "IN_PROGRESS": "WORKING", "QUEUED": "STAGED"}.get(s, s) if s else ""
def norm_cat(c):
    c = (c or "").strip().lower()
    for k in CATS:
        if c and (c == k.lower() or c.split()[0] in k.lower()): return k
    return "Question" if not c else "Ops & tooling"

def rag(a, t=None):
    t = t or now()
    if a.get("state") == "DONE": return "green"
    if a.get("blocked") or a.get("state") == "BLOCKED": return "red"
    try:
        if a.get("eta") and dt.datetime.fromisoformat(a["eta"]) < t: return "red"
    except ValueError: pass
    if a.get("state") in ("STAGED", "PARTIAL") or a.get("needs_jb"): return "amber"
    return "green"

class Store:
    def __enter__(self):
        os.makedirs(os.path.dirname(P), exist_ok=True)
        self.lk = open(P + ".lock", "w"); fcntl.flock(self.lk, fcntl.LOCK_EX)
        try: self.d = json.load(open(P))
        except (OSError, ValueError): self.d = {"asks": []}
        self.d.setdefault("asks", []); return self.d
    def __exit__(self, *e):
        if not e[0]:
            self.d["asks"] = self.d["asks"][-300:]
            self.d["verified_at"] = now().strftime("%Y-%m-%dT%H:%M:%S")  # only a Controller add/set/verify resets the page's AS OF
            tmp = P + ".tmp"; json.dump(self.d, open(tmp, "w"), indent=1); os.replace(tmp, P)
        fcntl.flock(self.lk, fcntl.LOCK_UN); self.lk.close()

LEVELS = ((15, "darkred"), (5, "red"), (2, "amber"), (0, "ok"))
def verified_at():
    try: return json.load(open(P)).get("verified_at", "")
    except (OSError, ValueError): return ""

def stale_check():
    """0-token watcher (launchd, every minute): alert the coordinator once per level; P0 at 15 min."""
    try: age = (now() - dt.datetime.fromisoformat(verified_at())).total_seconds() / 60
    except ValueError: age = 999
    lv = next(n for m, n in LEVELS if age >= m); sf = os.path.join(NOTES, "w1", "asks_stale.state")
    try: last = open(sf).read().strip()
    except OSError: last = "ok"
    try: repeat = lv == last == "darkred" and time.time() - os.path.getmtime(sf) >= 15 * 60  # P0 repeats every 15 min, never goes quiet
    except OSError: repeat = False
    if lv != last or repeat:
        open(sf, "w").write(lv)
        if lv != "ok":
            msg = {"amber": "UPDATE needed", "red": "LOOK INTO / ESCALATE / UPDATE", "darkred": "P0 ESCALATION: address and resolve now"}[lv]
            line = "%s %sASKS-STALE %s: your-asks 'as of' is %d min old (%s). Verify each ask, then `ctl.sh asks set <id> ...` or `ctl.sh asks verify`.\n" % (now().strftime("%Y-%m-%d %H:%M:%S"), "[P0] " if lv == "darkred" else "", msg, age, lv)
            for f in ("ALERTS.txt", "announce.log"):
                try: open(os.path.join(NOTES, f), "a").write(line)
                except OSError: pass
    print(lv, int(age))

def need_eta(state, eta):
    if norm_state(state) not in ("STAGED", "DONE", "") and not eta:
        raise SystemExit("ETC required: every state except STAGED needs --eta HH:MM (JB 28 Oct 15:52)")

def next_id(d):
    n = max([int(a["id"][1:]) for a in d["asks"] if re.match(r"^A\d+$", a.get("id", ""))] or [0])
    return "A%d" % (n + 1)

def add(title, cat="", eta="", state="STAGED", msg="", note="", needs_jb=False, by="Controller"):
    need_eta(state or "STAGED", norm_eta(eta))
    with Store() as d:
        if msg and any(a.get("msg") == msg and a.get("title") == title for a in d["asks"]): return None
        key = lambda s: " ".join(re.findall(r"[a-z0-9#]+", str(s).lower()))  # JB 28 Oct 14:47: re-adding an open ask made duplicates
        dup = next((a for a in d["asks"] if a.get("state") != "DONE" and difflib.SequenceMatcher(None, key(a.get("title")), key(title)).ratio() >= 0.8), None)
        if dup and "--allow-dup" not in sys.argv: return dict(dup, dup=True)
        a = {"id": next_id(d), "at": iso(now()), "title": str(title)[:200], "cat": norm_cat(cat), "state": norm_state(state) or "STAGED",
             "eta": norm_eta(eta), "msg": msg, "note": str(note)[:400], "needs_jb": bool(needs_jb), "blocked": False,
             "updated": iso(now()), "history": [{"at": iso(now()), "by": by, "state": norm_state(state) or "STAGED", "note": str(note)[:200]}]}
        if a["state"] == "DONE": a["done_at"] = iso(now()); a["eta"] = a["eta"] or a["done_at"]
        d["asks"].append(a); return a

def set_(aid, state="", eta="", note="", needs_jb=None, blocked=None, by="Controller"):
    with Store() as d:
        a = next((x for x in d["asks"] if x["id"] == aid), None)
        if not a: return None
        if state:
            s = norm_state(state)
            if s not in STATES: raise SystemExit("state must be one of %s" % ", ".join(STATES))
            a["state"] = s
            if s == "DONE": a["done_at"] = iso(now()); a["blocked"] = False
        if eta: a["eta"] = norm_eta(eta)
        if a["state"] == "DONE" and not a.get("eta"): a["eta"] = a.get("done_at") or iso(now())
        need_eta(a["state"], a.get("eta"))  # raising inside Store aborts the write
        if note: a["note"] = str(note)[:400]
        if needs_jb is not None: a["needs_jb"] = needs_jb
        if blocked is not None: a["blocked"] = blocked
        a["updated"] = iso(now())
        a.setdefault("history", []).append({"at": iso(now()), "by": by, "state": a["state"], "note": str(note)[:200]})
        a["history"] = a["history"][-12:]
        return a

def close_decision(did, state, note, by="Controller"):
    """D-<n> ids are decision cards (w1/decisions/D-<n>.json), not tracker asks: DONE closes the card."""
    f = os.path.join(NOTES, "w1", "decisions", did + ".json")
    if not os.path.exists(f): return None
    d = json.load(open(f))
    if norm_state(state or "") == "DONE":
        d["status"] = "withdrawn"
        d["acted"] = "%s %s" % (iso(now()), note or "closed done by " + by)
    else:
        d["acted_note"] = "%s %s" % (iso(now()), note or state)
    json.dump(d, open(f + ".tmp", "w"), indent=1); os.replace(f + ".tmp", f)
    return d

def load():
    try: d = json.load(open(P))
    except (OSError, ValueError): d = {"asks": []}
    for a in d.get("asks", []): a["rag"] = rag(a)
    return d

def opt(k, default=""):
    return sys.argv[sys.argv.index(k) + 1] if k in sys.argv and sys.argv.index(k) + 1 < len(sys.argv) else default

if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else "list"
    if c == "list":
        for a in load()["asks"]:
            if "--open" in sys.argv and a["state"] == "DONE": continue
            print("%-5s %-7s %-6s %-16s eta %-16s %s%s%s" % (a["id"], a["state"], a["rag"], a["cat"], a.get("eta") or "-", a["title"][:70],
                  " [needs JB]" if a.get("needs_jb") else "", " — " + a["note"][:80] if a.get("note") else ""))
    elif c == "add":
        a = add(opt("--title"), opt("--cat"), opt("--eta"), opt("--state", "STAGED"), opt("--msg"), opt("--note"), "--needs-jb" in sys.argv)
        print(("already open as %s: update it with `asks set %s` (or add --allow-dup)" % (a["id"], a["id"]) if a.get("dup") else "added %s" % a["id"]) if a else "already there")
    elif c == "stale": stale_check()
    elif c == "backfill":  # DONE -> finish time; any other non-STAGED gap is listed for the Controller to set
        with Store() as d:
            for a in d["asks"]:
                if a.get("state") == "DONE" and not a.get("eta"):
                    a["eta"] = a.get("done_at") or a.get("updated") or a.get("at"); print("%s DONE eta <- %s" % (a["id"], a["eta"]))
            miss = [a["id"] for a in d["asks"] if a.get("state") not in ("STAGED", "DONE") and not a.get("eta")]
        print("missing ETC (set them now): " + (" ".join(miss) if miss else "none"))
    elif c == "verify":
        with Store() as d: pass  # the Controller confirmed every open ask is accurate
        print("verified", verified_at())
    elif c == "set":
        yn = lambda k: None if k not in sys.argv else opt(k).lower() in ("yes", "y", "true", "1")
        if sys.argv[2].startswith("D-"):
            d = close_decision(sys.argv[2], opt("--state"), opt("--note"))
            print("decision %s: %s" % (sys.argv[2], d["status"]) if d else "no such decision"); sys.exit(0 if d else 1)
        a = set_(sys.argv[2], opt("--state"), opt("--eta"), opt("--note"), yn("--needs-jb"), yn("--blocked"))
        print("updated %s: %s eta %s" % (a["id"], a["state"], a.get("eta")) if a else "no such ask")
    else: print(__doc__); sys.exit(2)
