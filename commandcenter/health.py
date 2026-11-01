"""Machine health, taken from the collector that already existed.

healthd.py was written first, on purpose, as the simplest thing that exercised every layer of
the platform end to end. It works, so it is imported rather than rewritten.

One thing is added: a single shared snapshot on a timer. Processor percentage is measured as a
change since the last reading, so two watchers each asking for their own snapshot would each get
a shorter interval and a wrong number. One reading, handed to everybody, is both correct and
cheaper.
"""

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import healthd  # noqa: E402

_lock = threading.Lock()
_cached = None
_cached_at = 0.0
MIN_AGE = 1.0


def snapshot(max_age=MIN_AGE):
    global _cached, _cached_at
    with _lock:
        if _cached is None or (time.time() - _cached_at) > max_age:
            _cached = healthd.snapshot()
            _cached_at = time.time()
        return _cached


# Where a number turns from fine to a problem. Named in one place rather than spread through
# the rendering, so the console and any agent view agree on what trouble means.
LIMITS = {"mem_avail_gb": 1.5, "battery_pct": 20, "disk_free_pct": 10, "pressure": 1.0}
