"""Whether an agent is actually working, asked of Claude Code rather than guessed.

The platform's own answer was "did anything appear on its terminal in the last second", which is a
guess and wrong in both directions. An agent thinking quietly for thirty seconds reads as idle. An
agent whose interface is animating a spinner reads as busy when it is waiting for you.

`claude agents --json` reports every live session with its own `status`, which is the authoritative
answer because it comes from the process doing the work. Sessions are matched by session id, which is
what an agent *is* here, so the two views of the same fleet cannot disagree about who is who.

It costs about a quarter of a second, so it is taken once on a timer and handed to everybody, the
same arrangement health uses. Asking per request would mean a tenth of a second of subprocess for
every card on every refresh.

When the CLI cannot be reached, or an agent is not in its list, the old guess is used and says so.
Falling back is right rather than lazy: a console that says nothing about what an agent is doing is
worse than one that says something imperfect.
"""

import json
import subprocess
import threading
import time

from . import config, logs
from .terminal import clean_env

log = logs.get(__name__)

PROBE = [config.CLAUDE_BIN, "agents", "--json"]
TIMEOUT = 4.0
MIN_AGE = 1.5

_lock = threading.Lock()
_cached = None
_cached_at = 0.0
_warned = False


def _probe():
    """Ask the CLI. Replaced in tests, which is why it is its own function."""
    out = subprocess.run(PROBE, capture_output=True, text=True, timeout=TIMEOUT,
                         env=clean_env())
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "").strip()[:200] or f"exit {out.returncode}")
    return json.loads(out.stdout or "[]")


def sessions(max_age=MIN_AGE):
    """Every live session Claude Code knows about, by session id. Empty if it cannot be asked."""
    global _cached, _cached_at, _warned
    with _lock:
        if _cached is not None and (time.time() - _cached_at) < max_age:
            return _cached
        try:
            rows = _probe()
            _cached = {row.get("sessionId"): row for row in rows if row.get("sessionId")}
            _warned = False
        except Exception as exc:
            # Said once, not on every refresh. A missing CLI is a permanent condition and would
            # otherwise fill the log at one line per two seconds.
            if not _warned:
                _warned = True
                log.warning("cannot read live status from `%s`: %s; falling back to the terminal",
                            " ".join(PROBE), exc)
            _cached = {}
        _cached_at = time.time()
        return _cached


def status_of(rec):
    """What Claude Code says about this agent, or None if it does not know it."""
    row = sessions().get(rec.get("session_id"))
    return row.get("status") if row else None


def working(rec, term):
    """True when the agent is doing something now.

    The terminal is still the fallback, and it is the answer for an agent this platform started but
    the CLI has not listed yet, which happens for the first second or two of a wake.
    """
    said = status_of(rec)
    if said is not None:
        return said == "busy"
    return bool(term and term.working())


def reset():
    """Forget the cached answer. For tests."""
    global _cached, _cached_at, _warned
    with _lock:
        _cached, _cached_at, _warned = None, 0.0, False
