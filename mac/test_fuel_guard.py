"""Tests for fuel_guard.py efficiency tracking + circuit breaker + repeated-read refusal.
Run: python3 -m pytest mac/test_fuel_guard.py -q"""
import importlib.util, io, json, os, sys, tempfile, time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_fg(tmpdir, breaker_cfg=None, breaker_off=False):
    spec = importlib.util.spec_from_file_location("fg_%s" % id(tmpdir),
                                                   os.path.join(_HERE, "fuel_guard.py"))
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    fg.CFG = os.path.join(tmpdir, "cfg")
    fg.NOTES = os.path.join(tmpdir, "notes")
    fg.BREAKER_CFG = os.path.join(tmpdir, "breaker.json")
    fg.BREAKER_OFF = os.path.join(tmpdir, "breaker.off")
    os.makedirs(os.path.join(tmpdir, "cfg", "state"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "notes", "w1", "fuel"), exist_ok=True)
    if breaker_cfg is not None:
        with open(fg.BREAKER_CFG, "w") as f:
            json.dump(breaker_cfg, f)
    if breaker_off:
        open(fg.BREAKER_OFF, "w").close()
    return fg


def _call(fg, sid, tool_name, tool_input, tp="", env_role=""):
    inp = {"session_id": sid, "transcript_path": tp, "cwd": "/test",
           "tool_name": tool_name, "tool_input": tool_input}
    old_in, old_out = sys.stdin, sys.stdout
    old_env = os.environ.get("LC_ROLE")
    if env_role:
        os.environ["LC_ROLE"] = env_role
    elif "LC_ROLE" in os.environ:
        del os.environ["LC_ROLE"]
    sys.stdin = io.StringIO(json.dumps(inp))
    buf = io.StringIO()
    sys.stdout = buf
    try:
        fg.main()
    finally:
        sys.stdin = old_in
        sys.stdout = old_out
        if old_env is not None:
            os.environ["LC_ROLE"] = old_env
        elif "LC_ROLE" in os.environ:
            del os.environ["LC_ROLE"]
    return buf.getvalue()


def _bash(fg, sid, cmd, tp="", env_role=""):
    return _call(fg, sid, "Bash", {"command": cmd}, tp, env_role)


def _read(fg, sid, path, tp="", offset=None, limit=None):
    ti = {"file_path": path}
    if offset is not None:
        ti["offset"] = offset
    if limit is not None:
        ti["limit"] = limit
    return _call(fg, sid, "Read", ti, tp)


def _write(fg, sid, path, tp=""):
    return _call(fg, sid, "Write", {"file_path": path, "content": "x"}, tp)


def _edit(fg, sid, path, tp=""):
    return _call(fg, sid, "Edit", {"file_path": path, "old_string": "a", "new_string": "b"}, tp)


def _glob(fg, sid, tp=""):
    return _call(fg, sid, "Glob", {"pattern": "*.swift"}, tp)


def _state(fg, sid):
    p = os.path.join(fg.CFG, "state", sid + ".json")
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return {}


def _is_blocked(out):
    try:
        d = json.loads(out)
        return d.get("decision") == "block"
    except (ValueError, AttributeError):
        return False


# ── Existing efficiency tests (unchanged behaviour) ───────────────────────────

def test_no_milestone_advisory():
    """20 no-milestone Bash calls produce exactly 1 advisory on the 20th call."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})  # disable circuit
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
    """A git commit resets the no-milestone counter."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})
    sid = "sid-ms-reset"
    for _ in range(15):
        _bash(fg, sid, "cat file.txt")
    assert _state(fg, sid).get("nm") == 15

    _bash(fg, sid, "git commit -m 'fix: something'")
    assert _state(fg, sid).get("nm") == 0, "commit should reset nm counter"

    advisories = []
    for _ in range(20):
        out = _bash(fg, sid, "echo hello")
        if "EFFICIENCY ADVISORY" in out and "milestone" in out:
            advisories.append(out)
    assert len(advisories) == 1, "expected 1 advisory after reset, got %d" % len(advisories)


def test_reread_advisory():
    """Reading the same file 3 times triggers the advisory (legacy rr counter)."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})
    sid = "sid-rr"
    path = "/Users/test/SomeFile.swift"
    advisories = []
    for _ in range(3):
        out = _read(fg, sid, path)
        if "EFFICIENCY ADVISORY" in out and "read" in out.lower():
            advisories.append(out)
    assert len(advisories) == 1, "expected 1 reread advisory, got %d" % len(advisories)


def test_no_command_text_in_state():
    """State file never stores command text or file paths."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})
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
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})
    sid = "sid-timing"
    N = 200
    start = time.monotonic()
    for i in range(N):
        _bash(fg, sid, "ls" if i % 3 else "git commit -m x")
    elapsed = time.monotonic() - start
    avg_ms = elapsed / N * 1000
    assert avg_ms < 50, "avg %.1f ms per call exceeds 50 ms budget" % avg_ms


# ── Circuit breaker tests ─────────────────────────────────────────────────────

def test_circuit_blocks_at_nm_threshold():
    """Non-milestone Bash call is blocked after nm reaches the threshold (default 20)."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 5, "ctx_growth": 999999})
    sid = "sid-cb-nm"
    for _ in range(5):
        _bash(fg, sid, "ls -la")
    out = _bash(fg, sid, "ls -la")
    assert _is_blocked(out), "expected block after 5 non-milestone calls; got: %r" % out


def test_circuit_allows_git_in_enforced():
    """git commit is allowed (not blocked) when circuit is open."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 3, "ctx_growth": 999999})
    sid = "sid-cb-git"
    for _ in range(3):
        _bash(fg, sid, "ls")
    out = _bash(fg, sid, "git commit -m 'fix'")
    assert not _is_blocked(out), "git commit should not be blocked; got: %r" % out


def test_circuit_allows_test_submit_in_enforced():
    """scripts/test-submit.sh is allowed when circuit is open."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 3, "ctx_growth": 999999})
    sid = "sid-cb-ts"
    for _ in range(3):
        _bash(fg, sid, "ls")
    out = _bash(fg, sid, "scripts/test-submit.sh --lane X --targets unit")
    assert not _is_blocked(out), "test-submit should not be blocked; got: %r" % out


def test_circuit_allows_write_in_enforced():
    """Write is allowed in enforced mode."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 3, "ctx_growth": 999999})
    sid = "sid-cb-write"
    for _ in range(3):
        _bash(fg, sid, "ls")
    out = _write(fg, sid, "/tmp/mybrief.md")
    assert not _is_blocked(out), "Write should not be blocked; got: %r" % out


def test_circuit_allows_edit_in_enforced():
    """Edit is allowed in enforced mode."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 3, "ctx_growth": 999999})
    sid = "sid-cb-edit"
    for _ in range(3):
        _bash(fg, sid, "ls")
    out = _edit(fg, sid, "/tmp/status.txt")
    assert not _is_blocked(out), "Edit should not be blocked; got: %r" % out


def test_circuit_allows_one_read_in_enforced():
    """First Read is allowed in enforced mode; second Read is blocked."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 3, "ctx_growth": 999999})
    sid = "sid-cb-read"
    for _ in range(3):
        _bash(fg, sid, "ls")
    out1 = _read(fg, sid, "/tmp/mybrief.md")
    assert not _is_blocked(out1), "first Read should not be blocked; got: %r" % out1
    out2 = _read(fg, sid, "/tmp/another.md")
    assert _is_blocked(out2), "second Read should be blocked; got: %r" % out2


def test_circuit_blocks_glob_in_enforced():
    """Glob is blocked when circuit is open."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 3, "ctx_growth": 999999})
    sid = "sid-cb-glob"
    for _ in range(3):
        _bash(fg, sid, "ls")
    out = _glob(fg, sid)
    assert _is_blocked(out), "Glob should be blocked in enforced mode; got: %r" % out


def test_circuit_milestone_resets_enforcement():
    """After a milestone, circuit closes and non-milestone calls are allowed again."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 3, "ctx_growth": 999999})
    sid = "sid-cb-reset"
    for _ in range(3):
        _bash(fg, sid, "ls")
    # Confirm circuit is open
    out_blocked = _glob(fg, sid)
    assert _is_blocked(out_blocked), "expected block before milestone"
    # Milestone closes the circuit
    _bash(fg, sid, "git commit -m 'chore: commit'")
    # Now non-milestone should be allowed again
    out_after = _glob(fg, sid)
    assert not _is_blocked(out_after), "Glob should be unblocked after milestone; got: %r" % out_after


def test_circuit_coordinator_exempt():
    """Coordinator role is exempt from circuit enforcement."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 1, "ctx_growth": 999999})
    sid = "sid-cb-coord"
    _bash(fg, sid, "ls", env_role="coordinator")
    out = _glob(fg, sid, env_role="coordinator")
    assert not _is_blocked(out), "coordinator should be exempt from circuit; got: %r" % out


def test_circuit_kill_switch():
    """breaker.off disables enforcement even when thresholds are exceeded."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 1, "ctx_growth": 999999}, breaker_off=True)
    sid = "sid-cb-off"
    _bash(fg, sid, "ls")
    out = _glob(fg, sid)
    assert not _is_blocked(out), "kill switch should disable circuit; got: %r" % out


def test_circuit_block_reason_contains_message():
    """Block reason contains the required CHECKPOINT REQUIRED message."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 2, "ctx_growth": 999999})
    sid = "sid-cb-msg"
    for _ in range(2):
        _bash(fg, sid, "ls")
    out = _glob(fg, sid)
    assert _is_blocked(out), "should be blocked"
    d = json.loads(out)
    assert "CHECKPOINT REQUIRED" in d.get("reason", ""), "reason should contain CHECKPOINT REQUIRED"
    assert "commit" in d.get("reason", "").lower() or "HANDOFF" in d.get("reason", ""), \
        "reason should mention commit/push or HANDOFF"


def test_circuit_default_thresholds_from_config():
    """Default thresholds (requests=20) are loaded from config file."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t)  # no breaker_cfg → defaults
    sid = "sid-cb-defaults"
    # 19 calls: no block
    for _ in range(19):
        _bash(fg, sid, "ls")
    out19 = _glob(fg, sid)
    assert not _is_blocked(out19), "should not block at 19 requests (default threshold 20)"
    # 20th non-milestone call: circuit opens, 21st glob is blocked
    _bash(fg, sid, "ls")
    out21 = _glob(fg, sid)
    assert _is_blocked(out21), "should block when nm >= 20 (default threshold)"


# ── Repeated-read refusal tests ───────────────────────────────────────────────

def test_reread_refusal_3rd_unchanged(tmp_path):
    """3rd read of the same unchanged file returns decision=block."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})
    sid = "sid-rr2-block"
    # Create a real file so stat() works
    f = tmp_path / "target.swift"
    f.write_text("hello")
    path = str(f)
    out1 = _read(fg, sid, path)
    assert not _is_blocked(out1), "1st read should not block"
    out2 = _read(fg, sid, path)
    assert not _is_blocked(out2), "2nd read should not block"
    out3 = _read(fg, sid, path)
    assert _is_blocked(out3), "3rd read of unchanged file should block"
    d = json.loads(out3)
    assert "already read" in d.get("reason", "").lower(), "reason should say already read"


def test_reread_no_refusal_if_file_changed(tmp_path):
    """If the file changes between reads, refusal counter resets."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})
    sid = "sid-rr2-changed"
    f = tmp_path / "target.swift"
    f.write_text("v1")
    path = str(f)
    _read(fg, sid, path)
    _read(fg, sid, path)
    # Mutate file (forces new mtime/size)
    time.sleep(0.01)
    f.write_text("v2 changed content")
    out3 = _read(fg, sid, path)
    assert not _is_blocked(out3), "read after file change should not block"


def test_reread_refusal_includes_range(tmp_path):
    """Block reason includes the line range from the first read."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})
    sid = "sid-rr2-range"
    f = tmp_path / "bigfile.swift"
    f.write_text("x" * 100)
    path = str(f)
    _read(fg, sid, path, offset=50, limit=20)
    _read(fg, sid, path)
    out3 = _read(fg, sid, path)
    assert _is_blocked(out3)
    reason = json.loads(out3).get("reason", "")
    assert "50" in reason, "range offset 50 should appear in reason: %r" % reason


def test_reread_nonexistent_file_no_block():
    """Reading a nonexistent file path is not refused (stat fails gracefully)."""
    t = tempfile.mkdtemp()
    fg = _load_fg(t, breaker_cfg={"requests": 999, "ctx_growth": 999999})
    sid = "sid-rr2-missing"
    path = "/nonexistent/path/that/doesnt/exist.swift"
    for _ in range(3):
        out = _read(fg, sid, path)
        assert not _is_blocked(out), "nonexistent path should not trigger reread block"
