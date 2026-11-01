#!/usr/bin/env python3
"""Claude Code status line. Prints one short line and records the plan's usage for the console.

Claude Code hands the status line a JSON object on stdin. For a Pro or Max plan it carries
rate_limits.five_hour and rate_limits.seven_day, each with used_percentage and resets_at, after the
first reply of a session. This is the only local place those numbers exist, so the newest ones are
written to var/plan-usage.json, where the command center reads them. Any failure prints nothing
worse than an empty line: a status line must never break the terminal it sits in.
"""
import json, os, sys, time

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var", "plan-usage.json")

try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)

limits = data.get("rate_limits") or {}
five = limits.get("five_hour") or {}
week = limits.get("seven_day") or {}
if five or week:
    rec = {"at": time.time(),
           "five_hour": {"used_percentage": five.get("used_percentage"), "resets_at": five.get("resets_at")},
           "seven_day": {"used_percentage": week.get("used_percentage"), "resets_at": week.get("resets_at")}}
    try:
        tmp = OUT + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, OUT)
    except Exception:
        pass

# Nothing is shown in the terminal. The owner wants usage in the console's Config page only.
print("")
