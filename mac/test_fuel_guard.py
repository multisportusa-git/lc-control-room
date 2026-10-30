"""Tests for fuel_guard.py efficiency tracking (CH-011/012). Run: python3 -m pytest mac/test_fuel_guard.py -q"""
import importlib.util, io, json, os, sys, tempfile, time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_fg(tmpdir):
    spec = importlib.util.spec_from_file_location("fg_%s" % id(tmpdir),
                                                   os.path.join(_HERE, "fuel_guard.py"))
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    fg.CFG = os.path.join(tmpdir, "cfg")
    fg.NOTES = os.path.join(tmpdir, "notes")
    os.makedirs(os.path.join(tmpdir, "cfg", "state"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "notes", "w1", "fuel"), exist_ok=True)
    return fg


def _call(fg, sid, tool_name, tool_input, tp=""):
    inp = {"session_id": sid, "transcript_path": tp, "cwd": "/test",
           "tool_name": tool_name, "tool_input": tool_input}
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(inp))
    buf = io.StringIO()
    sys.stdout = buf
    try:
        fg.main()
    finally:
        sys.stdin = old_in
        sys.stdout = old_out
    return buf.getvalue()


def _bash(fg, sid, cmd, tp=""):
    return _call(fg, sid, "Bash", {"command": cmd}, tp)


def _read(fg, sid, path, tp=""):
    return _call(fg, sid, "Read", {"file_path": path}, tp)


def _state(fg, sid):
    p = os.path.join(fg.CFG, "state", sid + ".json")
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return {}


def test_no_milestone_advisory():
    """20 no-milestone Bash calls produce exactly 1 advisory on the 20th call."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t)
    sid = "sid-nm-adv"
    advisories = []
    for _ in range(20):
        out = _bash(fg, sid, "ls -la")
        if "EFFICIENCY ADVISORY" in out and "milestone" in out:
            advisories.append(out)
    assert len(advisories) == 1, "expected 1 advisory in 20 calls, got %d" % len(advisories)
    st = _state(fg, sid)
    assert st.get("nm") == 20


def test_milestone_resets_count():
    """A git commit resets the no-milestone counter; advisory appears at 20 *after* commit."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t)
    sid = "sid-ms-reset"
    # 15 non-milestone calls
    for _ in range(15):
        _bash(fg, sid, "cat file.txt")
    assert _state(fg, sid).get("nm") == 15

    # milestone: git commit
    _bash(fg, sid, "git commit -m 'fix: something'")
    assert _state(fg, sid).get("nm") == 0, "commit should reset nm counter"

    # 20 more non-milestone calls – advisory appears on the 20th
    advisories = []
    for _ in range(20):
        out = _bash(fg, sid, "echo hello")
        if "EFFICIENCY ADVISORY" in out and "milestone" in out:
            advisories.append(out)
    assert len(advisories) == 1, "expected 1 advisory after reset, got %d" % len(advisories)


def test_reread_advisory():
    """Reading the same file path 3 times triggers exactly 1 reread advisory."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t)
    sid = "sid-rr"
    path = "/Users/test/SomeFile.swift"
    advisories = []
    for _ in range(3):
        out = _read(fg, sid, path)
        if "EFFICIENCY ADVISORY" in out and "read" in out.lower():
            advisories.append(out)
    assert len(advisories) == 1, "expected 1 reread advisory, got %d" % len(advisories)

    # 4th read: no new advisory (already sent)
    out = _read(fg, sid, path)
    assert "EFFICIENCY ADVISORY" not in out or "milestone" in out, "no 2nd reread advisory expected"


def test_no_command_text_in_state():
    """State file never stores command text or file paths."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t)
    sid = "sid-no-cmd"
    secret_cmd = "git push origin very-secret-branch-name-xyz"
    _bash(fg, sid, secret_cmd)
    _read(fg, sid, "/secret/path/to/file.py")

    st_raw = open(os.path.join(fg.CFG, "state", sid + ".json")).read()
    assert "very-secret-branch-name" not in st_raw, "command text leaked into state"
    assert "/secret/path" not in st_raw, "file path leaked into state"


def test_timing_budget():
    """Each call adds < 50 ms on average (measured over 200 calls)."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t)
    sid = "sid-timing"
    N = 200
    start = time.monotonic()
    for i in range(N):
        _bash(fg, sid, "ls" if i % 3 else "git commit -m x")
    elapsed = time.monotonic() - start
    avg_ms = elapsed / N * 1000
    assert avg_ms < 50, "avg %.1f ms per call exceeds 50 ms budget" % avg_ms
