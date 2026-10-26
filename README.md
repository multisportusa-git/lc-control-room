# London Commuter · Control Room

Live view of the v3 Week 1 plan: what is done, running and next, an ETA and green/amber/red status (with the reason
and next action) for every task, the critical paths, token use per session and task, and the quota forecast.

**Page:** https://multisportusa-git.github.io/lc-control-room/ (the data is encrypted; the page asks for the
passphrase once per browser, or takes it from a `#k=` link and removes it from the address bar).

## How it works (no model tokens to view or refresh)
- `mac/push.sh` runs on JB's Mac every 10 minutes (launchd, `mac/install.sh`). It pulls this repo, runs
  `gen/w1_dashboard.py` over local files (W1 task definitions from the London Commuter repo's origin/main,
  `w1/state.json`, the evidence folder, the test queue, `gh pr list`, and the Claude Code transcripts via
  `gen/w1_tokens.py`), gzips and encrypts the data (openssl aes-256-cbc, PBKDF2-SHA256, 200,000 iterations), and
  force-pushes it as a single commit to the `data` branch.
- `index.html` (GitHub Pages, from `main`) fetches `data/w1.enc` from raw.githubusercontent.com and decrypts it in the
  browser (WebCrypto). Nothing readable leaves the Mac; the passphrase lives in `~/.config/lc-control-room/pass`.
- If the Mac is asleep or offline the page says so (Data health turns red after 25 minutes without a push).
- The same generator also writes a local copy with the data embedded: `<notes>/w1/W1_DASHBOARD.html`.

## Change it
Edit `index.html` (page) or `gen/` (data), run `python3 gen/test_w1_dashboard.py`, push to `main`. The Mac picks the
change up on its next run. The coordinator can override a task's note, plan, colour or estimate in
`<notes>/w1/dashboard_notes.json`: `{"W1-07": {"note": "...", "plan": "...", "rag": "amber", "est_min": 60}}`.

## Remove it
`zsh mac/install.sh --uninstall` on the Mac stops the job; delete the repo to take the page down.
