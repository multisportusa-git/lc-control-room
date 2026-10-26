#!/usr/bin/env python3
"""Adds the fuel-guard PostToolUse hook to ~/.claude/settings.json (JB approved 2026-10-26 13:5x). Idempotent.
Backs the file up first (settings.json.bak-fuel-<time>), keeps every other key, and copies the guard to
~/.config/lc-fuel/fuel_guard.py so the hook never depends on the repo checkout. --uninstall removes the hook."""
import json, os, shutil, sys, time

HOME = os.path.expanduser("~")
S = os.path.join(HOME, ".claude", "settings.json")
DST = os.path.join(HOME, ".config", "lc-fuel", "fuel_guard.py")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fuel_guard.py")
CMD = "/usr/bin/python3 " + DST.replace(" ", "\\ ")


def main():
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    shutil.copy2(SRC, DST)
    d = json.load(open(S)) if os.path.exists(S) else {}
    hooks = d.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    mine = [g for g in post if any("fuel_guard.py" in h.get("command", "") for h in g.get("hooks", []))]
    if "--uninstall" in sys.argv:
        if not mine:
            print("fuel guard: not installed"); return
        hooks["PostToolUse"] = [g for g in post if g not in mine]
        if not hooks["PostToolUse"]: del hooks["PostToolUse"]
        if not hooks: del d["hooks"]
    else:
        if mine:
            print("fuel guard: already installed (guard file refreshed)"); return
        post.append({"matcher": "*", "hooks": [{"type": "command", "command": CMD, "timeout": 5}]})
    if os.path.exists(S):
        shutil.copy2(S, S + ".bak-fuel-" + time.strftime("%Y%m%d-%H%M%S"))
    tmp = S + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    json.load(open(tmp))
    os.replace(tmp, S)
    print("fuel guard: %s" % ("removed" if "--uninstall" in sys.argv else "installed"))


if __name__ == "__main__":
    main()
