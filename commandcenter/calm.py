"""Calm mode: one switch on the Config page for anxious days.

While it is on:
    - agents hold offers, nudges and briefs that are not urgent. The daemon refuses an offer and
      a today or fyi brief with a sentence that says why, so the agent holds it itself.
    - The front desk makes no reminder calls. The alarm's 30-minute check does not call.
    - The front desk does not raise his task list, and never says "overdue" or "you forgot".
    - the morning letter shows only what is due today. The agent that writes it reads this.
Nobody asks how he feels or why it is on. It stays on until he turns it off.

Every agent can read it: the calm_mode tool, and "calm" in /api/state.

    GET  /api/calm             -> {on, since}
    POST /api/calm   {on}      -> {on, since}     from the console only
"""

import json
import threading

from . import clock, config

_lock = threading.Lock()

RULES = ("Calm mode is on. Hold offers, nudges and briefs that are not urgent until he turns it "
         "off. No reminder calls. Do not raise his task list. In anything he reads, show only "
         "what is due today, and never write \"overdue\" or \"you forgot\". Do not ask how he "
         "feels or why it is on, and do not mention it.")


def _file():
    config.VAR.mkdir(parents=True, exist_ok=True)
    return config.VAR / "calm.json"


def state():
    try:
        got = json.loads(_file().read_text())
    except (OSError, ValueError):
        got = {}
    on_ = bool(got.get("on")) if isinstance(got, dict) else False
    return {"on": on_, "since": got.get("since") if on_ else None}


def on():
    return state()["on"]


def save(body):
    body = body if isinstance(body, dict) else {}
    if not isinstance(body.get("on"), bool):
        raise ValueError("on is true or false")
    with _lock:
        now = state()
        if body["on"] == now["on"]:
            return now
        row = {"on": body["on"], "since": clock.now() if body["on"] else None}
        tmp = _file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(row))
        tmp.replace(_file())
    _changed()
    return state()


def for_agent():
    """The calm_mode tool."""
    got = state()
    return dict(got, rules=RULES if got["on"] else "Calm mode is off. Work as usual.")


_listeners = []


def on_change(fn):
    _listeners.append(fn)


def _changed():
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass


def reset():
    """For tests: nothing is held in memory."""
