"""When the owner last read each agent's chat, so the console can mark what he has not seen.

One small file, var/reads.json, {agent id: seconds}. Kept by the daemon rather than the browser,
so reading a chat on the phone clears its mark on the laptop too. An agent with no entry counts
as read: the first day of this does not light every card at once.
"""

import json
import threading

from . import clock, config

_lock = threading.Lock()


def _file():
    return config.VAR / "reads.json"


def _load():
    try:
        got = json.loads(_file().read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def read_at(agent_id):
    """When he last read this agent's chat, in seconds, or None."""
    with _lock:
        v = _load().get(agent_id)
    return float(v) if isinstance(v, (int, float)) else None


def mark(agent_id):
    """He has read this agent's chat up to now."""
    with _lock:
        rows = _load()
        rows[agent_id] = clock.now()
        _file().parent.mkdir(parents=True, exist_ok=True)
        tmp = _file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(_file())
    return {"ok": True, "read_at": rows[agent_id]}


def reset():
    """For tests: nothing kept in memory, so nothing to clear."""
