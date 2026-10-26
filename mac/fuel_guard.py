#!/usr/bin/env python3
"""Fuel guard: no session ever reaches auto-compact. Claude Code PostToolUse hook, 0 model tokens.

Installed in ~/.claude/settings.json by mac/install_fuel_guard.py, so it runs in every Claude Code session on this Mac
(desktop-app sessions, terminal sessions, headless lanes). After each tool call it reads the session's own transcript tail,
takes the context size of the last main-thread request (input + cache read + cache write) and compares it with the
CLAUDE.md rule-2 thresholds:
  (JB 26 Oct 12:42/12:45 decision A; same for V2 and V3)
  lean Sonnet lanes (started with a lane-settings profile): 175k start hand-over, 200k complete, 225k stop
  Haiku lanes: 80k, 90k, 95k (never send a 100k prompt)
  desktop-app and other non-lean sessions: 225k, 250k, 275k
  the coordinator (COORDINATOR.json cli_uuid, or LC_ROLE=coordinator): 250k, 275k, 300k
If a model has auto-compacted earlier than that on this Mac (a compact_boundary seen in any transcript), the thresholds
drop to 80/88/94 % of the lowest compaction point seen for that model, so a smaller context window is covered too.
On entering a band the session is told (hook additionalContext), JB gets one phone alert (ntfy) and the event is
written to <notes>/w1/fuel/events.jsonl for the Control Room. Band 2 repeats every 5 tool calls, band 3 on every call.
Never blocks a tool call; any error exits 0 silently (errors go to ~/.config/lc-fuel/guard.log)."""
import json, os, sys, time, subprocess

HOME = os.path.expanduser("~")
CFG = os.path.join(HOME, ".config", "lc-fuel")
NOTES = os.path.join(HOME, "Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08")
TOPIC_FILE = os.path.join(HOME, ".config", "lc-control-room", "ntfy_topic")
DESKTOP = (225000, 250000, 275000)
LEAN = (175000, 200000, 225000)
HAIKU = (80000, 90000, 95000)
COORD = (250000, 275000, 300000)
TH = {"coordinator": COORD, "lean": LEAN, "haiku": HAIKU, "desktop": DESKTOP}


def parent_lean():
    """True when the Claude process that ran this hook was started with a lane-settings profile (lean lane)."""
    try:
        out = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(os.getppid())], capture_output=True, text=True, timeout=2).stdout
        if "claude" not in out:  # hook ran via a shell: look one level up
            pp = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(os.getppid())], capture_output=True, text=True, timeout=2).stdout.strip()
            out = subprocess.run(["/bin/ps", "-o", "command=", "-p", pp], capture_output=True, text=True, timeout=2).stdout
        return "lane-settings" in out
    except Exception:
        return False
TAIL = 400000


def last_usage(path):
    """(context tokens, model, compaction preTokens or None) from the transcript tail."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - TAIL))
        lines = f.read().decode("utf-8", "replace").splitlines()
    ctx, model, compact = 0, "", None
    for ln in reversed(lines):
        if '"usage"' not in ln and "compact_boundary" not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("subtype") == "compact_boundary" and compact is None:
            compact = (d.get("compactMetadata") or {}).get("preTokens")
            if ctx:
                break
            continue
        if ctx or d.get("isSidechain") or d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        u = m.get("usage") or {}
        ctx = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
        model = m.get("model", "")
        if ctx and compact is not None:
            break
    return ctx, model, compact


def jload(p, d):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def jsave(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def thresholds(role, model, compact_points):
    base = TH.get(role, DESKTOP)
    low = compact_points.get(model)
    if low:
        base = tuple(min(b, int(low * k)) for b, k in zip(base, (0.80, 0.88, 0.94)))
    return base


def message(band, ctx, th, role, sid, cwd):
    k = ctx // 1000
    brief = os.path.join(NOTES, "handoffs", "%s-%s.md" % (time.strftime("%Y%m%d"), sid[:8]))
    who = {"coordinator": "coordinator", "lean": "lean lane", "haiku": "Haiku lane"}.get(role, "session")
    if band == 1:
        act = ("Start hand-over now (CLAUDE.md rule 2): finish only the current step, reach a safe point (committed and pushed, "
               "no git op half done, no slot held, nothing in flight), write the hand-over brief (<= ~2k tokens: task, branch/worktree, "
               "state, exact next step) to %s, then send `HANDOFF-%dk` with the brief path. Start no new task." % (brief, th[0] // 1000))
    elif band == 2:
        act = ("Hand-over must be COMPLETE now (target %dk). If the brief is not written, write it to %s now; end the turn after the "
               "current step. A desktop-app chat with no lane protocol: tell JB in one line to open a fresh session with that brief." % (th[1] // 1000, brief))
    else:
        act = ("STOP (max %dk). Make safe, make sure the brief exists at %s, send `STOPPED-%dk` with its path, end the turn and take no "
               "more work. Auto-compact must never happen." % (th[2] // 1000, brief, th[2] // 1000))
    return "FUEL GUARD (%s, context %dk; thresholds %d/%d/%dk): %s" % (who, k, th[0] // 1000, th[1] // 1000, th[2] // 1000, act)


def alert(title, body):
    try:
        topic = open(TOPIC_FILE).read().strip()
    except OSError:
        return
    if not topic:
        return
    subprocess.Popen(["/usr/bin/curl", "-s", "-m", "10", "-H", "Title: " + title, "-H", "Tags: fuelpump",
                      "-d", body, "https://ntfy.sh/" + topic],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def main():
    inp = json.load(sys.stdin)
    sid = inp.get("session_id") or ""
    tp = inp.get("transcript_path") or ""
    cwd = inp.get("cwd") or ""
    if not sid or not tp or not os.path.exists(tp):
        return
    ctx, model, compact = last_usage(tp)
    cps = jload(os.path.join(CFG, "compact_points.json"), {})
    st_path = os.path.join(CFG, "state", sid + ".json")
    st = jload(st_path, {"band": 0, "calls": 0, "compacts": 0})
    if compact and model and compact != st.get("seen_compact"):
        st["seen_compact"] = compact
        if not cps.get(model) or compact < cps[model]:
            cps[model] = int(compact)
            jsave(os.path.join(CFG, "compact_points.json"), cps)
        alert("LC fuel: auto-compact happened", "%s compacted at %dk (%s). Thresholds for %s now adapt." % (os.path.basename(cwd), compact // 1000, model, model))
        ev(sid, cwd, compact, model, -1, "compacted")
    if not ctx:
        return
    coord = jload(os.path.join(NOTES, "COORDINATOR.json"), {})
    if os.environ.get("LC_ROLE") == "coordinator" or coord.get("cli_uuid") == sid:
        role = "coordinator"
    elif "haiku" in model:
        role = "haiku"
    else:
        if "lean" not in st:
            st["lean"] = parent_lean()
        role = "lean" if st["lean"] else "desktop"
    th = thresholds(role, model, cps)
    band = 3 if ctx >= th[2] else 2 if ctx >= th[1] else 1 if ctx >= th[0] else 0
    st["calls"] = st.get("calls", 0) + 1
    say = False
    if band > st.get("band", 0):
        say = True
        alert("LC fuel: %s %dk" % (os.path.basename(cwd) or "session", ctx // 1000),
              ["", "start hand-over", "hand-over must be complete", "STOP: make safe"][band] + " (%s, %s)" % (role, sid[:8]))
        ev(sid, cwd, ctx, model, band, role)
        st["calls"] = 0
    elif band > 0 and band != st.get("band", 0):
        say = True  # fell back into a lower band (e.g. after a compaction): still past a threshold
        st["calls"] = 0
    elif band == 3 or (band == 2 and st["calls"] % 5 == 0):
        say = True
    if band < st.get("band", 0):
        st["calls"] = 0
    st.update(band=band, ctx=ctx, role=role, at=int(time.time()), cwd=cwd, model=model)
    jsave(st_path, st)
    if say:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": message(band, ctx, th, role, sid, cwd)}}))


def ev(sid, cwd, ctx, model, band, note):
    p = os.path.join(NOTES, "w1", "fuel", "events.jsonl")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"at": int(time.time()), "sid": sid, "cwd": os.path.basename(cwd), "ctx": ctx,
                                "model": model, "band": band, "note": note}) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break a session
        try:
            os.makedirs(CFG, exist_ok=True)
            with open(os.path.join(CFG, "guard.log"), "a") as f:
                f.write("%s %r\n" % (time.strftime("%F %T"), e))
        except OSError:
            pass
    sys.exit(0)
