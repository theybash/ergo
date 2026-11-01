"""The engines' plan limits, asked live from their own servers, the way claude.ai and Codex show
live usage.

claude.ai and Claude Code's /usage ask Anthropic, and Codex's /status asks ChatGPT, each with its
own login. This does the same when the Usage screen opens, so the rows are never older than a
minute. Nothing runs on a timer.

    Claude   GET api.anthropic.com/api/oauth/usage, with the login in ~/.claude/.credentials.json
    Codex    GET chatgpt.com/backend-api/wham/usage, with the login in $CODEX_HOME/auth.json

Neither address is a published API, so either can change. The logins are only read: Claude Code
and Codex renew them themselves. An expired login or a failed ask keeps the last good answer, which
is marked stale once it is old.
"""

import json
import threading
import urllib.request
from datetime import datetime

from . import clock, config

CACHE_SECONDS = 60.0
STALE_SECONDS = 30 * 60
TIMEOUT = 5.0

_lock = threading.Lock()
_held = {}          # name -> {"asked": when, "good": answer or None, "good_at": when}


def _get(url, headers):
    """One GET, its JSON. Tests replace this."""
    req = urllib.request.Request(url, headers={"User-Agent": "ergo", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _iso(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _window(pct, resets_at):
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    return {"used_percentage": float(pct), "resets_at": resets_at}


def _ask_claude():
    creds = json.loads(config.CLAUDE_CREDS.read_text())["claudeAiOauth"]
    if (creds.get("expiresAt") or 0) / 1000 < clock.now():
        return None
    d = _get("https://api.anthropic.com/api/oauth/usage",
             {"Authorization": "Bearer " + creds["accessToken"], "anthropic-beta": "oauth-2025-04-20"})
    five, week = d.get("five_hour") or {}, d.get("seven_day") or {}
    return {"five_hour": _window(five.get("utilization"), _iso(five.get("resets_at"))),
            "seven_day": _window(week.get("utilization"), _iso(week.get("resets_at")))}


def _ask_codex():
    tokens = json.loads((config.CODEX_HOME / "auth.json").read_text())["tokens"]
    d = _get("https://chatgpt.com/backend-api/wham/usage",
             {"Authorization": "Bearer " + tokens["access_token"],
              "ChatGPT-Account-Id": tokens.get("account_id") or ""})
    out = {"five_hour": None, "seven_day": None}
    # The plan sets the windows: a business plan has only the week. Each goes where its length says.
    for w in ((d.get("rate_limit") or {}).get("primary_window"),
              (d.get("rate_limit") or {}).get("secondary_window")):
        if not isinstance(w, dict):
            continue
        key = "seven_day" if (w.get("limit_window_seconds") or 0) > 86400 else "five_hour"
        out[key] = _window(w.get("used_percent"), w.get("reset_at"))
    return out


def _read(name, ask):
    now = clock.now()
    with _lock:
        held = _held.setdefault(name, {"asked": None, "good": None, "good_at": None})
        fresh = held["asked"] is not None and now - held["asked"] < CACHE_SECONDS
    if not fresh:
        try:
            got = ask()
        except Exception:  # noqa: BLE001 - any failure keeps the last good answer
            got = None
        with _lock:
            held["asked"] = now
            if got:
                held["good"], held["good_at"] = got, now
    with _lock:
        if not held["good"]:
            return None
        return {**held["good"], "at": held["good_at"],
                "stale": now - held["good_at"] > STALE_SECONDS}


def claude():
    """Claude's 5 hour and week windows, or None when it has never answered."""
    return _read("claude", _ask_claude)


def codex():
    """Codex's windows, the same shape, or None."""
    return _read("codex", _ask_codex)


def reset():
    """For tests."""
    with _lock:
        _held.clear()
