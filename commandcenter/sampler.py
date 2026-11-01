"""What the daemon is busy with, on request.

`kill -USR2 <daemon pid>` samples every thread's stack a hundred times a second for fifteen
seconds and writes var/logs/profile.txt: for each thread, how often it was seen running Python
(rather than waiting), and its most common stacks. Nothing runs until the signal comes.

It exists because the daemon was found at a full CPU core with nothing visible to say why, and
Linux does not let another process read its stacks without root."""

import collections
import signal
import sys
import threading
import time
import traceback

from . import config

SECONDS = 15
EVERY = 0.01

# A thread parked in one of these is waiting, not working.
IDLE = ("wait", "select", "poll", "accept", "recv", "recv_into", "read", "readline", "sleep",
        "_recv_exactly", "acquire", "get", "readinto", "serve_forever", "join")


def _sample():
    names = {t.ident: t.name for t in threading.enumerate()}
    me = threading.get_ident()
    busy = collections.Counter()
    seen = collections.Counter()
    stacks = collections.defaultdict(collections.Counter)
    rounds = 0
    end = time.monotonic() + SECONDS
    while time.monotonic() < end:
        rounds += 1
        for tid, frame in sys._current_frames().items():
            if tid == me:
                continue
            seen[tid] += 1
            if frame.f_code.co_name in IDLE:
                continue
            busy[tid] += 1
            st = traceback.extract_stack(frame)[-8:]
            stacks[tid]["\n".join(f"    {f.filename.rsplit('/', 2)[-1]}:{f.lineno} {f.name}" for f in st)] += 1
        time.sleep(EVERY)
    lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S')}: {rounds} samples over {SECONDS}s, threads alive: {len(seen)}", ""]
    for tid, n in busy.most_common(12):
        lines.append(f"thread {names.get(tid, '?')} ({tid}): running in {n} of {seen[tid]} samples")
        for stack, k in stacks[tid].most_common(3):
            lines.append(f"  {k}x")
            lines.append(stack)
        lines.append("")
    path = config.VAR / "logs" / "profile.txt"
    path.write_text("\n".join(lines))


def install():
    def on_signal(*_):
        threading.Thread(target=_sample, name="sampler", daemon=True).start()
    signal.signal(signal.SIGUSR2, on_signal)
