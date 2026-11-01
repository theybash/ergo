"""The daily quota alert. It replaces the $5 cap per run.

The weekly plan resets on Monday at 04:30. The day's line is 14% for each day since then. When
the weekly usage passes the line, Ergo shows a full-screen notice on his phone, at most once a
day. It stops nothing: he decides.

Every `claude -p` run, a chat turn or a scheduled job, streams `rate_limit_event` rows with the
weekly figure. Each one comes here through seen(). The last figure is kept in var/quota.json.
"""

import datetime
import json
import logging
import threading

from . import clock, config

log = logging.getLogger("cc.quota")

PER_DAY = 14.0
WEEK = 7 * 86400

_lock = threading.Lock()


def _file():
    config.VAR.mkdir(parents=True, exist_ok=True)
    return config.VAR / "quota.json"


def _load():
    try:
        got = json.loads(_file().read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(row):
    tmp = _file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row))
    tmp.replace(_file())


def _last_reset(now):
    """The last Monday 04:30, local time, at or before now."""
    at = datetime.datetime.fromtimestamp(now)
    monday = (at - datetime.timedelta(days=at.weekday())).replace(hour=4, minute=30, second=0,
                                                                  microsecond=0)
    if monday > at:
        monday -= datetime.timedelta(days=7)
    return monday.timestamp()


def line(now=None, resets_at=None):
    """The day's line in percent: 14 for each day since the reset, 98 at most."""
    now = clock.now() if now is None else now
    start = resets_at - WEEK if resets_at and resets_at - WEEK <= now < resets_at else _last_reset(now)
    return round(min(98.0, PER_DAY * (now - start) / 86400), 1)


def seen(event):
    """One event from a `claude -p` stream. Only rate_limit_event rows count. Never raises: a
    chat turn or a job must not fail because of this."""
    try:
        return _seen(event)
    except Exception:
        log.exception("quota: a rate limit event could not be read")
        return None


def _seen(event):
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return None
    week = (((event.get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("seven_day")
            or {})
    used = week.get("utilization")
    if not isinstance(used, (int, float)):
        return None
    # A fraction: 0.58 is 58%. Seen in real events; it can pass 1.0 past the limit.
    return note(used * 100, week.get("resetsAt"))


def note(used, resets_at=None):
    """The weekly figure in percent. Past the line, and not yet today: the notice goes."""
    now = clock.now()
    today = clock.local().strftime("%Y-%m-%d")
    with _lock:
        row = _load()
        row.update({"used": round(float(used), 1), "at": now})
        if isinstance(resets_at, (int, float)):
            row["resets_at"] = resets_at
        mark = line(now, row.get("resets_at"))
        over = row["used"] > mark and row.get("told") != today
        if over:
            row["told"] = today
        _save(row)
    if not over:
        return None
    from . import calm, phone
    if calm.on():
        log.info("quota: %.0f%% past the line of %.0f%%, held in calm mode", used, mark)
        return None
    log.info("quota: %.0f%% is past the day's line of %.0f%%, the phone is told", used, mark)
    return phone.notice("Usage is past today's line",
                        f"The week's usage is {used:.0f}%. Today's line is {mark:.0f}%. "
                        "Nothing is stopped. You decide.")


def state():
    row = _load()
    return {"used": row.get("used"), "at": row.get("at"),
            "line": line(clock.now(), row.get("resets_at")), "told": row.get("told")}


def reset():
    """For tests: nothing is held in memory."""
