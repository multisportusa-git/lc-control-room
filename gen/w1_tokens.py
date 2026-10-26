#!/usr/bin/env python3
"""Token ledger for the W1 dashboard. 0 model tokens. Python 3.9 stdlib (sqlite3).
Indexes every Claude Code transcript (~/.claude/projects/**.jsonl, incl. subagents) and local Cowork audit logs
incrementally (byte offsets) into <w1>/tokens.db, then returns the aggregates the dashboard embeds.
Per request: fresh input, cache write, cache read, output. Session total = sum of its requests.
Start cost = the context of the session's first main-thread request (fresh + cache write + cache read).
Work cost = total - start cost. A message id counts once (one reply is split over several transcript lines)."""
import glob, json, os, re, sqlite3, time, datetime as dt

HOME = os.path.expanduser("~")
APP = os.path.join(HOME, "Library/Application Support/Claude")
SRC = [(os.path.join(HOME, ".claude/projects"), "**/*.jsonl", "code"),
       (os.path.join(APP, "local-agent-mode-sessions"), "**/audit.jsonl", "cowork")]
TASK = re.compile(r"\bW1-\d{2}[A-Z]{0,2}\b")
COORD = re.compile(r"(?:✱\s*)?\b(K\d{1,3})\b[^\n]*coordinator", re.I)

def db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    create table if not exists files(path text primary key, off integer, size integer);
    create table if not exists msgs(id text primary key, session text, ts integer, model text,
        inp integer, cw integer, cr integer, out integer, side integer);
    create index if not exists msgs_ts on msgs(ts);
    create index if not exists msgs_s on msgs(session, ts);
    create table if not exists meta(session text primary key, title text, src text, cwd text, model text, effort text);
    """)
    return c

def epoch(s):
    try: return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception: return None

def index(c, budget_s=90):
    """Read new bytes of each transcript. Stops after budget_s (the next run continues), so a first full pass of a
    large history spreads over a few runs instead of blocking the page."""
    t0 = time.time(); known = dict((p, (o, s)) for p, o, s in c.execute("select path, off, size from files"))
    files = []
    for root, pat, src in SRC:
        for p in glob.glob(os.path.join(root, pat), recursive=True):
            try: files.append((os.path.getmtime(p), p, src, os.path.getsize(p)))
            except OSError: pass
    files.sort(reverse=True)  # newest first, so today's numbers are right even on the first, partial pass
    done = left = 0
    for _, p, src, size in files:
        off, osz = known.get(p, (0, 0))
        if size < off: off = 0  # file was rewritten
        if size == off: continue
        if time.time() - t0 > budget_s: left += 1; continue
        rows, metas = [], {}
        with open(p, "rb") as f:
            f.seek(off); buf = f.read()
        end = buf.rfind(b"\n") + 1
        for raw in buf[:end].splitlines():
            if b'"usage"' not in raw and b'"cwd"' not in raw: continue
            try: j = json.loads(raw)
            except ValueError: continue
            sid = j.get("sessionId") or j.get("session_id")
            if j.get("cwd") and sid and sid not in metas: metas[sid] = (j.get("cwd"), src)
            m = j.get("message") if j.get("type") == "assistant" else None
            u = (m or {}).get("usage")
            if not u or not sid: continue
            mid = m.get("id") or j.get("requestId") or j.get("uuid")
            rows.append((mid, sid, epoch(j.get("timestamp") or "") or int(os.path.getmtime(p)), m.get("model", ""),
                         u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
                         u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0,
                         1 if (j.get("isSidechain") or "/subagents/" in p) else 0))
        # the last line of a reply carries the final output count: keep the largest output per message id
        c.executemany("""insert into msgs values(?,?,?,?,?,?,?,?,?) on conflict(id) do update set
                         out=max(out, excluded.out), cw=max(cw, excluded.cw), cr=max(cr, excluded.cr)""", rows)
        for sid, (cwd, s) in metas.items():
            c.execute("insert into meta(session, src, cwd) values(?,?,?) on conflict(session) do nothing", (sid, s, cwd))
        c.execute("insert or replace into files values(?,?,?)", (p, off + end, size)); done += 1
    c.commit()
    return done, left

def titles(c, lane_sessions):
    """Session titles from the desktop app's session files; lanes from lane-W1-*.json session ids."""
    for pat in ("claude-code-sessions/*/*/local_*.json", "local-agent-mode-sessions/*/*/local_*.json"):
        for p in glob.glob(os.path.join(APP, pat)):
            try: d = json.load(open(p))
            except (OSError, ValueError): continue
            sid = d.get("cliSessionId")
            if not sid: continue
            t = d.get("title") or ""
            prev = " | ".join(d.get("previousTitles") or [])
            c.execute("""insert into meta(session, title, src, model, effort) values(?,?,?,?,?) on conflict(session) do update set
                         title=excluded.title, model=excluded.model, effort=excluded.effort""",
                      (sid, (t + (" | " + prev if prev else ""))[:300], "cowork" if "agent-mode" in p else "code", d.get("model"), d.get("effort") or d.get("effortOverride")))
    for sid, (task, model) in lane_sessions.items():
        c.execute("""insert into meta(session, title, src, model) values(?,?,?,?) on conflict(session) do update set title=excluded.title""",
                  (sid, "lane %s (headless)" % task, "lane", model))
    c.commit()

def classify(title, cwd, src, side_only):
    t = title or ""
    m = TASK.search(t)
    task = m.group(0) if m else None
    k = COORD.search(t)
    if src == "lane" or t.startswith("lane "): kind = "lane"
    elif k: kind = "coordinator"
    elif src == "cowork": kind = "cowork"
    elif side_only: kind = "subagent"
    elif re.match(r"^\s*SPARE", t): kind = "spare"
    elif re.match(r"^\s*[A-Z]{1,3}\d*\s*[:(]", t): kind = "lane"
    else: kind = "other"
    project = "London Commuter" if ("London Commuter" in (cwd or "") or "London Commuter" in t or task or k or re.match(r"^\s*[A-Z]{1,3}\d*\s*[:(]", t)) else "other"
    return task, kind, project

def aggregate(c, now_ts, lane_sessions):
    """Hourly buckets per session for the last 90 days + per-session summary + request timelines for the last 7 days."""
    since = now_ts - 90 * 86400
    meta = {r[0]: r[1:] for r in c.execute("select session, title, src, cwd, model, effort from meta")}
    sess = {}
    for sid, first_ts, last_ts, n, inp, cw, cr, out, mainreq, models in c.execute(
            """select session, min(ts), max(ts), count(*), sum(inp), sum(cw), sum(cr), sum(out), sum(1-side), group_concat(distinct model)
               from msgs where ts >= ? group by session""", (since,)):
        title, src, cwd, model, effort = meta.get(sid, ("", "", "", "", ""))
        task, kind, project = classify(title, cwd, src, mainreq == 0)
        if sid in lane_sessions: task = task or lane_sessions[sid][0]; kind = "lane"
        st = c.execute("select inp+cw+cr from msgs where session=? and side=0 order by ts limit 1", (sid,)).fetchone()
        start = st[0] if st else 0
        peak = c.execute("select max(inp+cw+cr) from msgs where session=? and side=0", (sid,)).fetchone()[0] or 0
        lc = c.execute("select inp+cw+cr from msgs where session=? and side=0 order by ts desc limit 1", (sid,)).fetchone()
        total = inp + cw + cr + out
        sess[sid] = {"id": sid[:8], "sid": sid, "title": (title or "(untitled) " + os.path.basename(cwd or ""))[:140], "kind": kind, "task": task,
                     "project": project, "models": sorted(set((models or "").split(","))), "effort": effort or "",
                     "first": first_ts, "last": last_ts, "req": n, "inp": inp, "cw": cw, "cr": cr, "out": out,
                     "total": total, "start": start, "last_ctx": lc[0] if lc else 0, "work": max(total - start, 0), "peak": peak}
    keys = list(sess); idx = {k: i for i, k in enumerate(keys)}
    models = sorted({m for s in sess.values() for m in s["models"] if m})
    midx = {m: i for i, m in enumerate(models)}
    buckets = [[idx[sid], h, midx.get(model, -1), inp, cw, cr, out, n]
               for sid, h, model, inp, cw, cr, out, n in c.execute(
                   """select session, (ts/3600)*3600 h, model, sum(inp), sum(cw), sum(cr), sum(out), count(*) from msgs
                      where ts >= ? group by session, h, model""", (since,)) if sid in idx]
    tl = {}
    for sid in keys:
        if sess[sid]["last"] >= now_ts - 7 * 86400:
            pts = c.execute("select ts, inp+cw+cr, out, side from msgs where session=? order by ts", (sid,)).fetchall()
            step = max(1, len(pts) // 240)
            tl[idx[sid]] = [[p[0], p[1], p[2], p[3]] for p in pts[::step]]
    return {"sessions": [sess[k] for k in keys], "models": models, "buckets": buckets, "timelines": tl}

def run(w1_dir, lane_sessions, budget_s=90):
    c = db(os.path.join(w1_dir, "tokens.db"))
    done, left = index(c, budget_s)
    titles(c, lane_sessions)
    out = aggregate(c, int(time.time()), lane_sessions)
    out["index"] = {"files_read": done, "files_left": left, "msgs": c.execute("select count(*) from msgs").fetchone()[0]}
    c.close()
    return out

if __name__ == "__main__":
    import sys
    w1 = sys.argv[1] if len(sys.argv) > 1 else "."
    r = run(w1, {}, float(sys.argv[2]) if len(sys.argv) > 2 else 90)
    print(r["index"], len(r["sessions"]), "sessions", len(r["buckets"]), "buckets")
