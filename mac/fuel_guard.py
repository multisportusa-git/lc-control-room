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

Circuit breaker (JB 2026-10-31): when requests since last milestone >= threshold (default 20) OR context growth
since last milestone >= threshold (default 50000 tokens), PostToolUse switches from advisory to ENFORCED CHECKPOINT:
it returns decision=block for non-milestone tool calls, allowing only git, scripts/test-submit.sh, Write/Edit of a
brief or status file, and reading at most 1 file. Thresholds: ~/.config/lc-fuel/breaker.json (keys "requests",
"ctx_growth"). Kill switch: ~/.config/lc-fuel/breaker.off. Coordinator sessions exempt.

Repeated reads: the 3rd read of the same unchanged file (same path and mtime/size) in a session is refused
(decision=block) with the line range already read.

Any error exits 0 silently (errors go to ~/.config/lc-fuel/guard.log)."""
import json, os, re, sys, time, subprocess

HOME = os.path.expanduser("~")
CFG = os.environ.get("LC_FUEL_CFG") or os.path.join(HOME, ".config", "lc-fuel")
NOTES = os.environ.get("LC_FUEL_NOTES") or os.path.join(HOME, "Library/Application Support/Claude/side-session-notes/local_eb83ac5f-2636-437e-a24c-fdb172ad7c08")
BREAKER_CFG = os.environ.get("LC_BREAKER_CFG") or os.path.join(HOME, ".config", "lc-fuel", "breaker.json")
BREAKER_OFF = os.environ.get("LC_BREAKER_OFF") or os.path.join(HOME, ".config", "lc-fuel", "breaker.off")

# CH-011/012: efficiency thresholds (config at top of file)
NM_ADVISORY = 20     # no-milestone requests before advisory
NM_WARN = 50         # no-milestone requests before warning
CTX_NM_WARN = 125000 # context size (tokens) without milestone before warning
REREAD_ADVISORY = 2  # reads of the same file before advisory (triggers on the 3rd read)
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


_MILESTONE_RE = re.compile(r'git\s+commit|git\s+push|scripts/test-submit\.sh|gh\s+pr\s+(?:create|comment|ready)|train\.sh')


def _is_milestone(tool_name, cmd):
    return tool_name == "Bash" and bool(_MILESTONE_RE.search(cmd))


def _path_hash(path):
    import hashlib
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def _ev_eff(sid, cwd, event_type, count):
    p = os.path.join(NOTES, "w1", "fuel", "events.jsonl")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"at": int(time.time()), "type": event_type,
                                 "sid": sid, "count": count, "cwd": os.path.basename(cwd)}) + "\n")
    except OSError:
        pass


def _eff_track(st, tool_name, tool_input, sid, cwd, msgs):
    """Update efficiency counters in st (mutates); append advisory/warning strings to msgs.
    Returns True if a milestone was hit (nm reset). Never stores command text or file paths — only counts."""
    cmd = (tool_input.get("command") or "") if tool_name == "Bash" else ""
    if _is_milestone(tool_name, cmd):
        st["nm"] = 0
        for k in ("nm_adv", "nm_warn", "ctx_warn"):
            st.pop(k, None)
        return True
    nm = st.get("nm", 0) + 1
    st["nm"] = nm
    if nm == NM_ADVISORY and not st.get("nm_adv"):
        st["nm_adv"] = True
        msgs.append("EFFICIENCY ADVISORY (%s): %d requests without a milestone "
                    "(git commit/push, test-submit, gh pr, train.sh). "
                    "Consider committing progress." % (sid[:8], nm))
        _ev_eff(sid, cwd, "nm_advisory", nm)
    if nm == NM_WARN and not st.get("nm_warn"):
        st["nm_warn"] = True
        msgs.append("EFFICIENCY WARNING (%s): %d requests without a milestone. "
                    "Act now: commit, push, or submit tests." % (sid[:8], nm))
        _ev_eff(sid, cwd, "nm_warn", nm)
    if tool_name == "Read":
        path = str(tool_input.get("file_path") or "")
        if path:
            ph = _path_hash(path)
            rc = st.get("rc", {})
            rc[ph] = rc.get(ph, 0) + 1
            st["rc"] = rc
            if rc[ph] == REREAD_ADVISORY + 1 and not st.get("rr_" + ph):
                st["rr_" + ph] = True
                msgs.append("EFFICIENCY ADVISORY (%s): file hash %s read %d times. "
                             "Consider caching the result." % (sid[:8], ph, rc[ph]))
                _ev_eff(sid, cwd, "reread", rc[ph])
    return False


# ── Circuit breaker ────────────────────────────────────────────────────────────

def _load_breaker_cfg():
    """Returns (req_thresh, ctx_growth_thresh) from config file (defaults 20 / 50000)."""
    d = jload(BREAKER_CFG, {})
    return int(d.get("requests", 20)), int(d.get("ctx_growth", 50000))


def _circuit_open(role, st, nm, ctx_growth, req_thresh, ctx_thresh):
    """True when enforced-checkpoint mode is active."""
    if role == "coordinator":
        return False
    if os.path.exists(BREAKER_OFF):
        return False
    return nm >= req_thresh or ctx_growth >= ctx_thresh


_ENFORCED_BASH_RE = re.compile(r'\bgit\b|scripts/test-submit\.sh')


def _is_allowed_in_enforced(tool_name, tool_input, st):
    """True when the tool is permitted while the circuit is open."""
    if tool_name == "Bash":
        cmd = tool_input.get("command") or ""
        return bool(_ENFORCED_BASH_RE.search(cmd))
    if tool_name in ("Write", "Edit"):
        return True  # brief / status file; path validation is Claude's job
    if tool_name == "Read":
        return st.get("enforced_reads", 0) < 1
    return False


def _enforced_block_msg(sid, nm, ctx_growth, req_thresh, ctx_thresh):
    reasons = []
    if nm >= req_thresh:
        reasons.append("%d requests without a milestone (threshold %d)" % (nm, req_thresh))
    if ctx_growth >= ctx_thresh:
        reasons.append("context grew %dk tokens without a milestone (threshold %dk)" % (ctx_growth // 1000, ctx_thresh // 1000))
    return ("CHECKPOINT REQUIRED (%s): %s. "
            "Commit/push your work or write a hand-over brief and end with HANDOFF/WAITING. "
            "Allowed: git, scripts/test-submit.sh, Write/Edit (brief or status file), "
            "Read (once more this period)." % (sid[:8], "; ".join(reasons)))


# ── Repeated-read refusal ──────────────────────────────────────────────────────

def _file_stat(path):
    """Returns (mtime_ns, size) tuple or None."""
    try:
        s = os.stat(path)
        return (s.st_mtime_ns, s.st_size)
    except OSError:
        return None


def _reread_check(st, tool_name, tool_input):
    """Return a block-reason string when the same unchanged file is read a 3rd time; else None."""
    if tool_name != "Read":
        return None
    path = str(tool_input.get("file_path") or "")
    if not path:
        return None
    stat = _file_stat(path)
    if stat is None:
        return None
    ph = _path_hash(path)
    rr2 = st.get("rr2", {})
    entry = rr2.get(ph)
    if entry is None:
        rr2[ph] = {"stat": list(stat), "count": 1,
                   "range": [tool_input.get("offset"), tool_input.get("limit")]}
        st["rr2"] = rr2
        return None
    prev_stat = tuple(entry.get("stat", [None, None]))
    if stat != prev_stat:
        rr2[ph] = {"stat": list(stat), "count": 1,
                   "range": [tool_input.get("offset"), tool_input.get("limit")]}
        st["rr2"] = rr2
        return None
    count = entry.get("count", 0) + 1
    entry["count"] = count
    st["rr2"] = rr2
    if count >= 3:
        rng = entry.get("range", [None, None])
        offset, limit = rng[0], rng[1]
        if offset is not None or limit is not None:
            rng_str = "lines %d--%d" % (offset or 0, (offset or 0) + (limit or 9999))
        else:
            rng_str = "full file"
        return ("REPEATED READ REFUSED (%s): already read %d times unchanged; "
                "use the previously read content (%s)." % (path, count, rng_str))
    return None


def main():
    inp = json.load(sys.stdin)
    sid = inp.get("session_id") or ""
    tp = inp.get("transcript_path") or ""
    cwd = inp.get("cwd") or ""
    tool_name = inp.get("tool_name") or ""
    tool_input = inp.get("tool_input") or {}
    if not sid:
        return
    cps = jload(os.path.join(CFG, "compact_points.json"), {})
    st_path = os.path.join(CFG, "state", sid + ".json")
    st = jload(st_path, {"band": 0, "calls": 0, "compacts": 0})

    # Repeated-read refusal (always active, independent of ctx)
    reread_block = _reread_check(st, tool_name, tool_input)
    if reread_block:
        jsave(st_path, st)
        print(json.dumps({"decision": "block", "reason": reread_block}))
        return

    eff_msgs = []
    hit_milestone = _eff_track(st, tool_name, tool_input, sid, cwd, eff_msgs)

    ctx, model, compact = 0, "", None
    if tp and os.path.exists(tp):
        ctx, model, compact = last_usage(tp)
        if compact and model and compact != st.get("seen_compact"):
            st["seen_compact"] = compact
            if not cps.get(model) or compact < cps[model]:
                cps[model] = int(compact)
                jsave(os.path.join(CFG, "compact_points.json"), cps)
            alert("LC fuel: auto-compact happened", "%s compacted at %dk (%s). Thresholds for %s now adapt." % (os.path.basename(cwd), compact // 1000, model, model))
            ev(sid, cwd, compact, model, -1, "compacted")

    # Determine role early (needed for both band tracking and circuit breaker)
    coord = jload(os.path.join(NOTES, "COORDINATOR.json"), {})
    if os.environ.get("LC_ROLE") == "coordinator" or coord.get("cli_uuid") == sid:
        role = "coordinator"
    elif model and "haiku" in model:
        role = "haiku"
    else:
        if "lean" not in st:
            st["lean"] = parent_lean()
        role = "lean" if st["lean"] else "desktop"

    # Circuit-breaker milestone tracking (update ctx baseline on milestone)
    if ctx and "ctx_at_milestone" not in st:
        st["ctx_at_milestone"] = ctx  # first ctx reading becomes the baseline
    if hit_milestone:
        if ctx:
            st["ctx_at_milestone"] = ctx
        st.pop("enforced_reads", None)
        st.pop("enforced_ever", None)

    band_msg = None
    if ctx:
        th = thresholds(role, model, cps)
        band = 3 if ctx >= th[2] else 2 if ctx >= th[1] else 1 if ctx >= th[0] else 0
        # CH-011: 125K context without milestone warning
        if ctx >= CTX_NM_WARN and st.get("nm", 0) >= 1 and not st.get("ctx_warn"):
            st["ctx_warn"] = True
            eff_msgs.append("EFFICIENCY WARNING (%s): context at %dk with %d requests since last milestone."
                            % (sid[:8], ctx // 1000, st["nm"]))
            _ev_eff(sid, cwd, "ctx_nm_warn", ctx)
        st["calls"] = st.get("calls", 0) + 1
        say = False
        if band > st.get("band", 0):
            say = True
            alert("LC fuel: %s %dk" % (os.path.basename(cwd) or "session", ctx // 1000),
                  ["", "start hand-over", "hand-over must be complete", "STOP: make safe"][band] + " (%s, %s)" % (role, sid[:8]))
            ev(sid, cwd, ctx, model, band, role)
            st["calls"] = 0
        elif band > 0 and band != st.get("band", 0):
            say = True
            st["calls"] = 0
        elif band == 3 or (band == 2 and st["calls"] % 5 == 0):
            say = True
        if band < st.get("band", 0):
            st["calls"] = 0
        st.update(band=band, ctx=ctx, role=role, at=int(time.time()), cwd=cwd, model=model)
        if say:
            band_msg = message(band, ctx, th, role, sid, cwd)

    # Circuit breaker check (runs even without ctx — nm alone can trip it)
    req_thresh, ctx_thresh = _load_breaker_cfg()
    nm = st.get("nm", 0)
    ctx_growth = max(0, ctx - st.get("ctx_at_milestone", ctx)) if ctx else 0
    if _circuit_open(role, st, nm, ctx_growth, req_thresh, ctx_thresh):
        if not _is_allowed_in_enforced(tool_name, tool_input, st):
            block_reason = _enforced_block_msg(sid, nm, ctx_growth, req_thresh, ctx_thresh)
            if not st.get("enforced_ever"):
                st["enforced_ever"] = True
                alert("LC fuel: CHECKPOINT %s" % os.path.basename(cwd),
                      "circuit open: %d nm, %dk ctx growth (%s)" % (nm, ctx_growth // 1000, sid[:8]))
                ev(sid, cwd, ctx, model, -2, "enforced_block")
            jsave(st_path, st)
            print(json.dumps({"decision": "block", "reason": block_reason}))
            return
        # Allowed tool in enforced mode: track Read count
        if tool_name == "Read":
            st["enforced_reads"] = st.get("enforced_reads", 0) + 1

    jsave(st_path, st)
    all_msgs = ([band_msg] if band_msg else []) + eff_msgs
    if all_msgs:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": "\n\n".join(all_msgs)}}))


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
