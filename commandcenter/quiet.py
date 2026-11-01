"""Quiet time: nothing rings for a while, for a nap or a meeting. Alarms wait until it ends.

Any agent sets it with the quiet_for tool, for a number of minutes. Until it ends:
    - no call rings. An agent's call_owner is refused with the time quiet ends, and an urgent
      item from anywhere else waits; it rings when quiet ends, if it is still waiting.
    - nags wait. Each one due in the window moves to the end.
    - an alarm set now for a time in the window is set for the end instead.
    - an alarm already on the phone waits too: the phone gets the end time, and Ergo moves every
      alarm in the window to the end, one minute apart, so they do not all ring at once.
It ends by itself. Zero minutes ends it at once.

It is not calm mode. Calm is for anxious days and changes how agents write to him; quiet only
holds the rings, and only for a while.

    GET  /api/quiet                       -> {on, until, by}
    POST /api/quiet  {minutes, by?}        -> {on, until, by, said}
"""

import json
import logging
import threading

from . import clock, config

log = logging.getLogger("cc.quiet")

MOST = 12 * 60

_lock = threading.Lock()
_timer = None
_listeners = []


def _file():
    config.VAR.mkdir(parents=True, exist_ok=True)
    return config.VAR / "quiet.json"


def _read():
    try:
        got = json.loads(_file().read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def state():
    got = _read()
    until = got.get("until")
    on_ = isinstance(until, (int, float)) and until > clock.now()
    return {"on": on_, "until": until if on_ else None, "by": got.get("by") if on_ else None}


def until():
    """The time quiet ends, or None when it is not on."""
    return state()["until"]


def on():
    return state()["on"]


def hhmm(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def set_for(minutes, by=None):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ValueError("minutes is a whole number, 0 to end quiet time")
    if not 0 <= minutes <= MOST:
        raise ValueError(f"minutes is from 0 to {MOST}")
    by = " ".join(str(by or "").split())[:64] or None
    now = clock.now()
    with _lock:
        row = {"until": now + minutes * 60, "by": by, "since": now} if minutes else {}
        tmp = _file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(row))
        tmp.replace(_file())
    log.info("quiet: %s by %s", f"until {hhmm(row['until'])}" if minutes else "ended", by)
    arm()
    _changed()
    got = state()
    if got["on"]:
        said = (f"Quiet until {hhmm(got['until'])}. No call rings, nags and alarms in the window "
                f"wait until then, and then ring one minute apart.")
    else:
        said = "Quiet time is over. Anything held rings now."
    return dict(got, said=said)


def for_agent():
    got = state()
    if got["on"]:
        return dict(got, rules=(f"The owner asked for quiet until {hhmm(got['until'])}. Do not call "
                                "him before then; call_owner is refused. If it can wait, use "
                                "add_job to do it after that time."))
    return dict(got, rules="Quiet time is off.")


def on_change(fn):
    """Called with no arguments when quiet starts or ends."""
    _listeners.append(fn)


def _changed():
    # The phone moves its alarms, nags move or run, and a held call rings when it ends.
    from . import inbox, nags, phone
    for fn in [phone.quiet, nags.check, inbox.ring_check] + list(_listeners):
        try:
            fn()
        except Exception:
            log.exception("quiet: a listener failed")


def arm():
    """A timer for the end, so what was held rings then without anybody asking."""
    global _timer
    with _lock:
        if _timer:
            _timer.cancel()
            _timer = None
        end = state()["until"]
        if not end:
            return None
        wait = max(1.0, end - clock.now() + 1.0)
        _timer = threading.Timer(wait, _ended)
        _timer.daemon = True
        _timer.start()
        return wait


def _ended():
    global _timer
    with _lock:
        _timer = None
    if on():
        arm()
        return
    log.info("quiet: ended")
    _changed()


def reset():
    """For tests: no timer left running."""
    global _timer
    with _lock:
        if _timer:
            _timer.cancel()
        _timer = None
