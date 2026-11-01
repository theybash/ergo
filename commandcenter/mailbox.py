"""Messages waiting for an agent that is asleep.

A quick note typed on the dashboard should not wake an agent. Waking costs memory and takes ten
seconds, and the whole point of grey is that an agent can sit there costing nothing. So a message
to a sleeping agent is held here, and delivered when it next wakes.

They arrive merged into one prompt with the time each was sent, rather than one at a time. Three
separate prompts would make the agent answer the first, then be interrupted by the second, and the
timestamps matter because a note left at midnight and a note left at eight in the morning are
different instructions even when the words are identical.

Held messages survive a restart of the console, because losing one silently is worse than any
delay in delivering it.

A quiet message is one he never sees: a fresh job's result, left for the
agent's next turn. It travels the same way, but it is not counted on the card, not listed, and
"delete them all" leaves it. "It feels cluttery": a result that waits on purpose is not undelivered.

An urgent message is one sent with wake that found the agent mid task.
"Some things are time sensitive and should not wait for my prompt." The broker sends it the moment
the task ends, instead of waiting for his next message.
"""

import json
import threading
import time
import uuid

from . import config

_lock = threading.RLock()
_held = None


def _load():
    global _held
    if _held is not None:
        return _held
    config.ensure_dirs()
    try:
        _held = json.loads((config.VAR / "mailbox.json").read_text())
        if not isinstance(_held, dict):
            _held = {}
    except Exception:
        _held = {}

    # Older files were written before messages had ids, and one is needed to delete a single
    # message rather than the lot. Position would work until a delivery shifted everything by one
    # and the wrong note vanished, so anything without an id gets one here, once.
    patched = False
    for msgs in _held.values():
        for m in msgs:
            if isinstance(m, dict) and not m.get("id"):
                m["id"] = uuid.uuid4().hex[:10]
                patched = True
            # Job results left before quiet existed, so they stop showing on the card. Once.
            if (isinstance(m, dict) and "quiet" not in m
                    and str(m.get("text", "")).startswith("[Your scheduled job ")):
                m["quiet"] = True
                patched = True
    if patched:
        _save()
    return _held


def _save():
    tmp = config.VAR / "mailbox.json.tmp"
    tmp.write_text(json.dumps(_held, indent=2))
    tmp.replace(config.VAR / "mailbox.json")


def hold(agent_id, text, quiet=False, ts=None, urgent=False):
    """Keep a message until the agent wakes. Returns how many are now waiting."""
    with _lock:
        held = _load()
        m = {"id": uuid.uuid4().hex[:10], "ts": ts or time.time(), "text": text}
        if quiet:
            m["quiet"] = True
        if urgent:
            m["urgent"] = True
        held.setdefault(agent_id, []).append(m)
        _save()
        return len(held[agent_id])


def _shown(msgs):
    return [m for m in msgs if not m.get("quiet")]


def waiting(agent_id, everything=False):
    """How many the owner sees waiting. With everything, the quiet ones count too."""
    with _lock:
        msgs = _load().get(agent_id, [])
        return len(msgs if everything else _shown(msgs))


def urgent(agent_id):
    """Whether a message waits that must go in as soon as the agent's turn ends."""
    with _lock:
        return any(m.get("urgent") and not m.get("quiet") for m in _load().get(agent_id, []))


def counts():
    with _lock:
        return {aid: len(_shown(msgs)) for aid, msgs in _load().items() if _shown(msgs)}


def take(agent_id):
    """Remove and return everything waiting for this agent."""
    with _lock:
        held = _load()
        msgs = held.pop(agent_id, [])
        if msgs:
            _save()
        return msgs


def merged(msgs, now=None):
    """One prompt from several held messages.

    A single message is delivered as itself, with no framing at all. Wrapping one note in an
    explanation of the delivery mechanism tells the agent about the platform instead of telling it
    what was asked.
    """
    if not msgs:
        return None
    if len(msgs) == 1:
        return msgs[0]["text"]

    now = now or time.time()
    lines = [f"[{len(msgs)} messages arrived for you, oldest first]", ""]
    for m in msgs:
        gap = max(0, now - m["ts"])
        if gap < 3600:
            when = f"{round(gap / 60)} minutes ago"
        elif gap < 86400:
            when = f"{round(gap / 3600)} hours ago"
        else:
            when = f"{round(gap / 86400)} days ago"
        lines.append(f"--- {when} ---")
        lines.append(m["text"])
        lines.append("")
    return "\n".join(lines).strip()


def waiting_list(agent_id):
    """The held messages themselves, oldest first, for showing before deleting one.

    Kept out of the console's main state payload on purpose. The count belongs on a card; the full
    text of every undelivered note for every agent does not, and would travel on every refresh.
    """
    with _lock:
        return [dict(m) for m in _shown(_load().get(agent_id, []))]


def drop_one(agent_id, message_id):
    """Delete a single held message. Returns whether it was there."""
    with _lock:
        held = _load()
        msgs = held.get(agent_id) or []
        keep = [m for m in msgs if m.get("id") != message_id]
        if len(keep) == len(msgs):
            return False
        if keep:
            held[agent_id] = keep
        else:
            held.pop(agent_id, None)
        _save()
        return True


def take_one(agent_id, message_id):
    """Take a single held message out, to send it now. Returns it, or None if it was not there."""
    with _lock:
        held = _load()
        msgs = held.get(agent_id) or []
        got = next((m for m in msgs if m.get("id") == message_id), None)
        if got is None:
            return None
        keep = [m for m in msgs if m is not got]
        if keep:
            held[agent_id] = keep
        else:
            held.pop(agent_id, None)
        _save()
        return got


def drop(agent_id, keep_quiet=False):
    """Delete everything held for an agent. Returns how many went.

    keep_quiet is his "delete them all": it deletes what he can see, never the job results he
    cannot, or the agent would never learn how its jobs went.
    """
    with _lock:
        held = _load()
        msgs = held.get(agent_id)
        if not msgs:
            return 0
        keep = [m for m in msgs if m.get("quiet")] if keep_quiet else []
        if keep:
            held[agent_id] = keep
        else:
            held.pop(agent_id, None)
        _save()
        return len(msgs) - len(keep)


def reset():
    """Forget everything held in memory. For tests."""
    global _held
    with _lock:
        _held = None
