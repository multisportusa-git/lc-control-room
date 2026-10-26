# Decisions for JB: how sessions raise them, how JB answers, how the answer comes back

Raise (any session; zero chat): write `<notes>/w1/decisions/D-<n>.json` (next free number). Within 10 minutes the page
shows it at the top under **Your decisions**, and JB gets one alert (ntfy app on his phone, macOS notification).

```json
{"id": "D-3", "created": "2026-10-26T12:40", "title": "One line: what is being decided",
 "situation": "What is happening and what it affects (W1-nn ids become links)",
 "why_now": "What waiting costs",
 "options": [{"key": "A", "label": "Short name", "detail": "What happens", "pros": ["..."], "cons": ["..."], "cost": "time/tokens/$", "risk": "low|med|high"},
             {"key": "B", "label": "...", "pros": [], "cons": []}],
 "recommend": "A", "because": "Why A", "tasks": ["W1-07"], "deadline": "2026-10-26T14:00", "status": "open"}
```

JB tasks: the Report done box files `ACTION W1-nn: <text>` (W1-05: `ACTION W1-05: week code cowork`); the Mac appends it to the task's evidence file and runs `lc_state.py done W1-nn --note "JB via issue #n: ..."`.

Answer (JB, on the page): **Approve X** opens a pre-filled GitHub issue `DECISION D-3: X`; he presses Submit (an optional
note goes in the issue body, which is public). `gen/decisions_sync.py` on the Mac (every 10 minutes) accepts it only from
the repo owner, writes `status: decided` and `decided: {choice, by, at, via, note}` into the file, comments, closes the
issue, and appends a line to `<notes>/w1/decisions/decisions.log`.

Act (coordinator): read `decisions.log` (or the files with `status: decided` and no `acted`) at each step, carry the
choice out, then add `"acted": "<time> <what was done>"` to the file. Withdraw a decision with `"status": "withdrawn"`.
Never put secrets in a decision: the page data is encrypted, but notes JB adds are public issues.
