"""The time, asked for in one place so a test can lie about it.

The scheduler is the only part of this platform whose behaviour depends on the clock, and it is the
part that cannot be checked by hand: waiting until 03:00 to find out what happens at 03:00 is not a
test. So the scheduler asks here instead of asking the operating system, and a test moves the clock
by hand.

Nothing else uses this. A timestamp written into a log or a record is better as the real time, and
pretending otherwise would make a log read from a test confusing rather than useful.
"""

import datetime as _datetime
import threading
import time as _time

_lock = threading.RLock()
_fixed = None


def now():
    """Seconds since the epoch, or whatever a test has pinned it to."""
    with _lock:
        return _fixed if _fixed is not None else _time.time()


def local():
    """The same moment as a datetime, for anything reasoning about a time of day."""
    return _datetime.datetime.fromtimestamp(now())


def sleep(seconds):
    """Wait, unless the clock is frozen, in which case waiting would never end.

    A frozen clock plus a real sleep is the worst of both: the test would take the full wall clock
    time and still see no progress. So a frozen clock makes this a no-op and the test advances time
    itself.
    """
    with _lock:
        frozen = _fixed is not None
    if not frozen:
        _time.sleep(seconds)


# -- for tests only --------------------------------------------------------------

def freeze(at=None):
    """Pin the clock. Returns the moment it was pinned to."""
    global _fixed
    with _lock:
        _fixed = float(at if at is not None else _time.time())
        return _fixed


def advance(seconds):
    """Move a frozen clock forward. Raises if the clock is not frozen, because a test that thinks
    it moved time and did not is a test that passes for the wrong reason."""
    global _fixed
    with _lock:
        if _fixed is None:
            raise RuntimeError("the clock is not frozen; call freeze() first")
        _fixed += float(seconds)
        return _fixed


def unfreeze():
    global _fixed
    with _lock:
        _fixed = None
