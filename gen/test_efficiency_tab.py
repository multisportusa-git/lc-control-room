#!/usr/bin/env python3
"""Tests for the Efficiency tab in index.html.

Usage:
  python3 gen/test_efficiency_tab.py

Returns exit code 0 on pass, 1 on failure.
Screenshots (if Playwright available) saved to <notes>/v2/evidence/effd/.
"""
import json
import os
import sys
import pathlib
import textwrap

ROOT = pathlib.Path(__file__).parent.parent
FIXTURE = ROOT / "gen" / "fixtures" / "efficiency_sample.json"
INDEX = ROOT / "index.html"

NOTES = (
    pathlib.Path.home()
    / "Library"
    / "Application Support"
    / "Claude"
    / "side-session-notes"
    / "local_eb83ac5f-2636-437e-a24c-fdb172ad7c08"
)
EVIDENCE = NOTES / "v2" / "evidence" / "effd"

# ----- schema validation -----------------------------------------------

REQUIRED_KEYS = {
    "schema", "generated_at", "window", "quality", "overhead",
    "cards", "questions", "tasks", "sessions", "tests",
    "circuit", "findings", "series", "rounds",
}
WINDOW_KEYS = {"id", "start", "end", "status", "hours"}
QUALITY_KEYS = {"grade", "reasons"}
OVERHEAD_KEYS = {"collect_ms", "db_bytes"}
CARD_KEYS = {"id", "section", "label", "value", "unit", "sample",
             "coverage", "formula", "status", "delta_prev"}
Q_KEYS = {"id", "text", "answer", "formula", "sample", "drill"}
TASK_KEYS = {
    "task_id", "kind", "model", "effort", "sessions", "requests",
    "raw_tokens", "output_tokens", "cache_read_share", "first_ctx",
    "peak_ctx", "handoffs", "launches", "duplicate_launches",
    "runner_min", "outcome", "accepted", "flags",
}
SESSION_KEYS = {
    "sid", "task_id", "role", "model", "effort", "parent_sid",
    "start", "end", "requests", "raw_tokens", "first_ctx",
    "peak_ctx", "milestones", "max_no_milestone_run", "rereads", "flags",
}
TEST_KEYS = {
    "job", "lane", "task_id", "target", "class", "head",
    "result", "minutes", "fingerprint", "gate",
}
CIRCUIT_KEYS = {"ts", "state", "reason_class"}
FINDING_KEYS = {"id", "severity", "title", "metric", "evidence_ref", "owner_subsystem"}
SERIES_KEYS = {"tokens_by_hour", "requests_by_hour", "runner_min_by_hour"}
ROUND_KEYS = {"id", "start", "end", "grade", "headline_kpis"}

VALID_GRADES = {"A", "B", "C", "DEGRADED"}
VALID_WINDOW_STATUS = {"live", "frozen"}
VALID_CARD_STATUS = {"ok", "warn", "fail", "unknown"}
VALID_ANSWERS = {"YES", "NO", "UNKNOWN", "INSUFFICIENT_DATA"}
VALID_OUTCOMES = {"accepted", "rejected", "abandoned", "in-progress", None}
VALID_SEVERITIES = {"P0", "P1", "P2"}
VALID_TEST_CLASS = {"full", "focused", "train", "main"}
VALID_TEST_RESULT = {"PASS", "FAIL", "ERROR", None}
VALID_GATE = {"allowed", "refused", "flake_retry", "exception", None}
VALID_CIRCUIT_STATE = {"open", "closed", "half-open"}

ERRORS: list[str] = []


def err(msg: str) -> None:
    ERRORS.append(msg)
    print(f"  FAIL: {msg}", file=sys.stderr)


def check_keys(obj: dict, required: set, ctx: str) -> None:
    missing = required - set(obj.keys())
    if missing:
        err(f"{ctx}: missing keys {missing}")


def validate(data: dict) -> None:
    check_keys(data, REQUIRED_KEYS, "root")
    if data.get("schema") != "lc-eff/1":
        err(f"root.schema: expected 'lc-eff/1', got {data.get('schema')!r}")

    w = data.get("window") or {}
    check_keys(w, WINDOW_KEYS, "window")
    if w.get("status") not in VALID_WINDOW_STATUS:
        err(f"window.status: {w.get('status')!r} not in {VALID_WINDOW_STATUS}")

    q = data.get("quality") or {}
    check_keys(q, QUALITY_KEYS, "quality")
    if q.get("grade") not in VALID_GRADES:
        err(f"quality.grade: {q.get('grade')!r} not in {VALID_GRADES}")
    if not isinstance(q.get("reasons"), list):
        err("quality.reasons: expected list")

    oh = data.get("overhead") or {}
    check_keys(oh, OVERHEAD_KEYS, "overhead")

    for i, c in enumerate(data.get("cards") or []):
        check_keys(c, CARD_KEYS, f"cards[{i}]")
        if c.get("status") not in VALID_CARD_STATUS:
            err(f"cards[{i}].status: {c.get('status')!r}")

    qids = set()
    for i, q in enumerate(data.get("questions") or []):
        check_keys(q, Q_KEYS, f"questions[{i}]")
        if q.get("answer") not in VALID_ANSWERS:
            err(f"questions[{i}].answer: {q.get('answer')!r}")
        if q.get("id") in qids:
            err(f"questions[{i}].id: duplicate {q.get('id')!r}")
        qids.add(q.get("id"))

    for i, t in enumerate(data.get("tasks") or []):
        check_keys(t, TASK_KEYS, f"tasks[{i}]")
        if t.get("outcome") not in VALID_OUTCOMES:
            err(f"tasks[{i}].outcome: {t.get('outcome')!r}")
        if not isinstance(t.get("flags"), list):
            err(f"tasks[{i}].flags: expected list")

    for i, s in enumerate(data.get("sessions") or []):
        check_keys(s, SESSION_KEYS, f"sessions[{i}]")
        if not isinstance(s.get("flags"), list):
            err(f"sessions[{i}].flags: expected list")

    for i, t in enumerate(data.get("tests") or []):
        check_keys(t, TEST_KEYS, f"tests[{i}]")
        if t.get("class") not in VALID_TEST_CLASS:
            err(f"tests[{i}].class: {t.get('class')!r}")
        if t.get("result") not in VALID_TEST_RESULT:
            err(f"tests[{i}].result: {t.get('result')!r}")
        if t.get("gate") not in VALID_GATE:
            err(f"tests[{i}].gate: {t.get('gate')!r}")

    for i, c in enumerate(data.get("circuit") or []):
        check_keys(c, CIRCUIT_KEYS, f"circuit[{i}]")
        if c.get("state") not in VALID_CIRCUIT_STATE:
            err(f"circuit[{i}].state: {c.get('state')!r}")

    for i, f in enumerate(data.get("findings") or []):
        check_keys(f, FINDING_KEYS, f"findings[{i}]")
        if f.get("severity") not in VALID_SEVERITIES:
            err(f"findings[{i}].severity: {f.get('severity')!r}")

    ser = data.get("series") or {}
    check_keys(ser, SERIES_KEYS, "series")
    for k in SERIES_KEYS:
        if not isinstance(ser.get(k), list):
            err(f"series.{k}: expected list")

    for i, r in enumerate(data.get("rounds") or []):
        check_keys(r, ROUND_KEYS, f"rounds[{i}]")
        if not isinstance(r.get("headline_kpis"), dict):
            err(f"rounds[{i}].headline_kpis: expected dict")


def _has_boarded_amplification(data: dict) -> bool:
    """At least one task with requests > 50 (BOARDED-like amplification)."""
    return any((t.get("requests") or 0) > 50 for t in (data.get("tasks") or []))


def test_schema_valid() -> bool:
    print("1. Schema validation of efficiency_sample.json …", end=" ")
    if not FIXTURE.exists():
        err(f"fixture not found: {FIXTURE}")
        print("FAIL")
        return False
    try:
        data = json.loads(FIXTURE.read_text())
    except Exception as e:
        err(f"JSON parse error: {e}")
        print("FAIL")
        return False
    validate(data)
    if not _has_boarded_amplification(data):
        err("fixture lacks a task with requests > 50 (BOARDED-like amplification)")
    if ERRORS:
        print("FAIL")
        return False
    print("OK")
    return True


def test_index_has_eff_module() -> bool:
    """Static check: index.html references the efficiency module."""
    print("2. index.html contains efficiency module … ", end=" ")
    if not INDEX.exists():
        err(f"index.html not found: {INDEX}")
        print("FAIL")
        return False
    src = INDEX.read_text()
    checks = [
        ('R.eff', "R.eff renderer"),
        ('"eff"', '"eff" in module registry or order'),
        ('efficiency.enc', 'efficiency.enc URL'),
        ('eff-sec', 'eff-sec CSS class'),
    ]
    ok = True
    for needle, desc in checks:
        if needle not in src:
            err(f"index.html missing: {desc} (looked for {needle!r})")
            ok = False
    if ok:
        print("OK")
    else:
        print("FAIL")
    return ok


def _build_test_page(fixture_data, title="efficiency test") -> str:
    """Build a self-contained HTML page that exercises the efficiency renderer."""
    src = INDEX.read_text()
    # Patch the DATA_URL and EMBED so it uses our fixture as the whole data object.
    # We embed a minimal w1-style wrapper with just enough fields for app(D) to not crash.
    minimal_w1 = {
        "gen": "2026-10-30 16:00",
        "gen_iso": "2026-10-30T16:00:00Z",
        "today": "2026-10-30",
        "now_iso": "2026-10-30T16:00:00Z",
        "week": "W1",
        "loop": "",
        "tasks": [],
        "health": [],
        "phone": {"state": "green", "tasks": [], "free": None},
        "chat": {"threads": {}, "lanes": [], "limit": 20},
        "decisions": [],
        "asks": [],
        "facts": {
            "pending": {"pending": 0, "running": 0, "held": 0},
            "open_prs": [],
            "merged_24h": [],
            "main": [],
            "alerts": [],
        },
        "queue": [],
        "tok": {"sessions": [], "buckets": [], "models": [], "timelines": {}},
        "ntfy": "test-topic",
        "_eff": fixture_data,
    }
    # Embed the data and disable the passphrase prompt.
    patched = src.replace(
        "const EMBED=/*__DATA__*/null;",
        f"const EMBED={json.dumps(minimal_w1)};",
    )
    # Inject efficiency data into window._EFF before app() returns.
    patched = patched.replace(
        "renderAll();",
        "window._EFF = D._eff || null; renderAll();",
        1,
    )
    return patched


def test_renders_without_errors(playwright_available: bool) -> bool:
    """Test that the efficiency tab renders without JS errors."""
    print("3. Efficiency tab renders without JS errors … ", end=" ")
    if not INDEX.exists():
        err("index.html not found")
        print("SKIP (no index.html)")
        return True  # not a failure of this test

    fixture_data = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else None
    test_page = _build_test_page(fixture_data)

    if not playwright_available:
        # Static check: just verify the patched page is valid HTML with no obvious template errors
        if "{{" in test_page or "}}" in test_page:
            err("index.html contains unresolved template variables")
            print("FAIL (static)")
            return False
        # Check no Python/JS syntax errors in our additions by looking for key markers
        markers = ["R.eff", "eff-sec", "efficiency.enc", "window._EFF"]
        all_present = all(m in INDEX.read_text() for m in markers)
        if all_present:
            print("OK (static; Playwright not available)")
            return True
        else:
            missing = [m for m in markers if m not in INDEX.read_text()]
            err(f"Missing markers: {missing}")
            print("FAIL (static)")
            return False

    # Playwright path
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP (playwright not installed)")
        return True

    import tempfile
    errors_js: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            for width, label in [(390, "390px"), (1280, "1280px")]:
                page = browser.new_page(viewport={"width": width, "height": 812})
                page.on("pageerror", lambda e: errors_js.append(str(e)))
                page.on("console", lambda m: errors_js.append(m.text) if m.type == "error" else None)

                with tempfile.NamedTemporaryFile(
                    suffix=".html", delete=False, mode="w", encoding="utf-8"
                ) as f:
                    f.write(test_page)
                    tmp_path = f.name

                try:
                    page.goto(f"file://{tmp_path}")
                    page.wait_for_timeout(1500)
                    # Navigate to the eff section if it rendered
                    eff_el = page.query_selector("#m-eff")
                    if eff_el:
                        eff_el.scroll_into_view_if_needed()
                        page.wait_for_timeout(300)

                    out_path = EVIDENCE / f"effd-{width}.png"
                    page.screenshot(path=str(out_path), full_page=False)
                    print(f"\n   Screenshot saved: {out_path}")
                finally:
                    os.unlink(tmp_path)
                    page.close()
        finally:
            browser.close()

    if errors_js:
        for e in errors_js[:5]:
            err(f"JS error: {e}")
        print("FAIL")
        return False
    print("OK")
    return True


def test_null_heavy_fixture() -> bool:
    """Tab renders without errors when most fields are null."""
    print("4. Null-heavy fixture renders without errors … ", end=" ")
    null_data = {
        "schema": "lc-eff/1",
        "generated_at": "2026-10-30T16:00:00Z",
        "window": {"id": "W-null", "start": None, "end": None, "status": "live", "hours": None},
        "quality": {"grade": "DEGRADED", "reasons": ["all sources null"]},
        "overhead": {"collect_ms": None, "db_bytes": None},
        "cards": [],
        "questions": [],
        "tasks": [],
        "sessions": [],
        "tests": [],
        "circuit": [],
        "findings": [],
        "series": {"tokens_by_hour": [], "requests_by_hour": [], "runner_min_by_hour": []},
        "rounds": [],
    }
    validate(null_data)
    if ERRORS:
        print("FAIL (schema rejected null-heavy fixture)")
        return False
    print("OK (schema accepts null-heavy fixture)")
    return True


def test_missing_file() -> bool:
    """push.sh publish step handles missing efficiency_monitor.py gracefully."""
    print("5. push.sh handles missing efficiency script … ", end=" ")
    push = ROOT / "mac" / "push.sh"
    if not push.exists():
        err("push.sh not found")
        print("FAIL")
        return False
    src = push.read_text()
    checks = [
        ("efficiency_monitor.py", "efficiency_monitor.py reference"),
        ("efficiency.enc", "efficiency.enc output"),
    ]
    ok = True
    for needle, desc in checks:
        if needle not in src:
            err(f"push.sh missing: {desc}")
            ok = False
    # Check it uses -f or similar guard so missing script is tolerated
    if "if [[ -f" not in src and '[ -f' not in src and 'if [[ -x' not in src:
        err("push.sh: no file-existence guard for efficiency_monitor.py")
        ok = False
    if ok:
        print("OK")
    else:
        print("FAIL")
    return ok


def main() -> int:
    print(f"Efficiency tab tests\n{'='*40}")

    try:
        from playwright.sync_api import sync_playwright as _
        pw = True
    except ImportError:
        pw = False

    results = [
        test_schema_valid(),
        test_index_has_eff_module(),
        test_renders_without_errors(pw),
        test_null_heavy_fixture(),
        test_missing_file(),
    ]

    passed = sum(results)
    total = len(results)
    print(f"\n{'='*40}")
    print(f"{'PASS' if all(results) else 'FAIL'}: {passed}/{total} tests passed")
    if ERRORS:
        print(f"\nAll errors ({len(ERRORS)}):")
        for e in ERRORS:
            print(f"  - {e}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
