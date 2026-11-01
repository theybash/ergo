"""Night shift requests: an agent asks the night shift for a change to the platform.

Each request is one block appended to the shift's requests.md, in the format that file gives.
The shift reads it first. The owner's own requests come before every other, whatever urgency an
agent gives, so a request the owner asked for says so: `from: owner, claimed by <agent>`. The agent's word is all
there is, so the shift checks a claimed request before it puts it first.

    POST /api/shift/request  {agent, title, what, why, urgency, requested_by_owner}  -> {ok}
"""

import datetime
import threading

from . import clock, config

_lock = threading.Lock()

TITLE_CHARS = 80
TEXT_CHARS = 1200


def _clean(value, count):
    return " ".join(str(value or "").split())[:count]


def request(agent, title, what, why, urgency="normal", requested_by_owner=False):
    from . import registry
    agent = _clean(agent, 64)
    if not registry.get(agent):
        raise KeyError(f"there is no agent called {agent}")
    title, what, why = _clean(title, TITLE_CHARS), _clean(what, TEXT_CHARS), _clean(why, TEXT_CHARS)
    if not (title and what and why):
        raise ValueError("a request needs a title, what to change, and why")
    urgency = urgency if urgency in ("urgent", "normal") else "normal"
    who = f"owner, claimed by {agent}" if requested_by_owner is True else agent
    asked = datetime.datetime.fromtimestamp(clock.now()).strftime("%Y-%m-%d %H:%M")
    block = (f"\n## {title}\n- from: {who}\n- asked: {asked}\n- urgency: {urgency}\n"
             f"- what: {what}\n- why: {why}\n- status: open\n")
    path = config.SHIFT_REQUESTS
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(block)
    return {"ok": True, "from": who}


def reset():
    """For tests: nothing is held in memory."""
