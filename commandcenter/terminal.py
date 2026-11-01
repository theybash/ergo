"""One pseudo-terminal per awake agent.

This is the thing that makes the console show what the terminal shows, byte for byte. Claude
Code is a full-screen text interface; it draws itself with escape sequences and repaints as
tokens arrive. So the honest way to show it is to keep every byte and let the browser render
them with a terminal emulator. Nothing is interpreted here, and nothing is cleaned up.

Token streaming needs no special work as a result. The interface already prints each token as
it arrives, so forwarding its output is forwarding tokens.

Reading the pseudo-terminal is not how cost or the last message is measured. That comes from
the session file on disk, which is structured. Terminal for the eyes, session file for the
ledger.
"""

import errno
import os
import signal
import threading
import time
from collections import deque

from ptyprocess import PtyProcess

# Enough scrollback that a browser attaching mid-task sees the current screen and some of what
# led to it. Claude Code repaints its whole frame often, so the last few kilobytes usually
# contain a complete picture; the rest is history worth having.
RING_BYTES = 256 * 1024

# A browser that stops reading must not be able to hold memory forever. Past this many pending
# chunks the connection is considered broken and dropped.
QUEUE_LIMIT = 2048

DEFAULT_SIZE = (32, 120)   # rows, cols


def clean_env():
    """The environment an agent starts with: this process's, minus every Claude Code marker.

    This is not tidiness. If the console is started from inside a Claude Code session, the agent
    inherits that session's variables, and two of them do real damage. One marks the process as a
    child session, which switches its transcript off: no session file means no cost, no last
    message, and nothing to resume, so waking the agent would silently start a blank conversation
    instead of continuing its own. Another two are a socket and a token for the parent session's
    message bus, which the agent has no business holding.

    Anything beginning with CLAUDE_ goes, so a marker added in a future version is dropped
    without this code being touched. Authentication lives in ANTHROPIC_* and in the keychain, and
    is kept.
    """
    return {k: v for k, v in os.environ.items()
            if not k.startswith("CLAUDE_") and k != "CLAUDECODE"}


class Subscriber:
    """One attached browser. Owns a queue and the window size it can display."""

    def __init__(self, rows, cols):
        self.chunks = deque()
        self.event = threading.Event()
        self.rows, self.cols = rows, cols
        self.dead = False

    def push(self, data):
        if len(self.chunks) >= QUEUE_LIMIT:
            self.dead = True
            self.event.set()
            return
        self.chunks.append(data)
        self.event.set()

    def drain(self, timeout=None):
        """Wait for output, then return everything pending joined into one piece.

        Joining matters. A repaint arrives as dozens of small writes, and sending each as its
        own frame makes the browser do dozens of renders for one visual change.
        """
        if not self.chunks:
            self.event.wait(timeout)
        self.event.clear()
        out = b"".join(self.chunks)
        self.chunks.clear()
        return out


class Terminal:
    """A running process and everyone watching it."""

    def __init__(self, agent_id, argv, cwd, env=None, on_exit=None):
        self.agent_id = agent_id
        self.argv = list(argv)
        self.cwd = str(cwd)
        self.on_exit = on_exit
        self.started = time.time()
        self.exited_at = None
        self.exit_status = None

        self.ring = deque()
        self.ring_bytes = 0
        self.subs = []
        self.lock = threading.RLock()

        self.last_output = 0.0
        self.last_input = 0.0
        self.bytes_out = 0

        environ = clean_env()
        # A terminal interface needs to know it is talking to a terminal that understands
        # colour, otherwise Claude Code falls back to a plain renderer and the console shows
        # something the operator would never see in their own shell.
        environ.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "FORCE_COLOR": "1",
            # Lets an agent find the console without being told, so it can register itself.
            "PLATFORM_AGENT_ID": agent_id,
        })
        if env:
            environ.update(env)

        self.proc = PtyProcess.spawn(self.argv, cwd=self.cwd, env=environ,
                                     dimensions=DEFAULT_SIZE)
        self.rows, self.cols = DEFAULT_SIZE

        self.reader = threading.Thread(target=self._read_loop, name=f"pty-{agent_id}",
                                       daemon=True)
        self.reader.start()

    # -- process ----------------------------------------------------------------

    @property
    def pid(self):
        return self.proc.pid

    def alive(self):
        return self.exited_at is None and self.proc.isalive()

    def working(self):
        """True when the agent is doing something rather than waiting for input.

        This reads output activity rather than parsing the screen, and it works because the
        interface animates while it thinks: a spinner repaints several times a second, so a
        busy agent is never silent and an idle one produces nothing at all. Parsing the screen
        for a phrase would break the first time the wording changed.
        """
        return self.alive() and (time.time() - self.last_output) < 1.0

    def _read_loop(self):
        while True:
            try:
                data = self.proc.read(65536)
            except EOFError:
                break
            except OSError as exc:
                # The pseudo-terminal closing shows up as EIO on Linux, which is normal exit
                # rather than a fault.
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not data:
                break
            self._fanout(data)

        self.exited_at = time.time()
        try:
            self.proc.wait()
            self.exit_status = self.proc.exitstatus
        except Exception:
            pass
        with self.lock:
            for sub in self.subs:
                sub.dead = True
                sub.event.set()
        # The broker learns the agent has gone the moment the terminal closes. Nothing polls
        # and nothing is asked of the agent, which is why a crashed agent goes grey at once.
        if self.on_exit:
            try:
                self.on_exit(self)
            except Exception:
                pass

    def _fanout(self, data):
        now = time.time()
        with self.lock:
            self.last_output = now
            self.bytes_out += len(data)
            self.ring.append(data)
            self.ring_bytes += len(data)
            while self.ring_bytes > RING_BYTES and len(self.ring) > 1:
                self.ring_bytes -= len(self.ring.popleft())
            for sub in list(self.subs):
                if sub.dead:
                    self.subs.remove(sub)
                else:
                    sub.push(data)

    # -- input ------------------------------------------------------------------

    def write(self, data):
        """Send raw bytes. This is what a keypress in the browser becomes."""
        if not self.alive():
            return False
        if isinstance(data, str):
            data = data.encode()
        try:
            self.proc.write(data)
            self.last_input = time.time()
            return True
        except (OSError, EOFError):
            return False

    def submit(self, text):
        """Type a message and send it.

        The wrapping is load bearing. Those two escape sequences are bracketed paste, and they
        tell the interface that everything between them is pasted text rather than typing. A
        message containing a newline would otherwise be submitted at the first line and the
        rest would land in an empty prompt.
        """
        if not self.alive():
            return False
        body = text.replace("\r\n", "\n")
        self.write(b"\x1b[200~" + body.encode() + b"\x1b[201~")
        # A short pause lets the interface finish redrawing the pasted block before the return
        # key arrives. Without it a long message is occasionally submitted half-read.
        time.sleep(0.14)
        return self.write(b"\r")

    def interrupt(self):
        """Halt the current task and leave the agent alive, ready for the next message.

        Escape is the interface's own interrupt. A signal would kill the process, which is a
        different action with a different button.
        """
        return self.write(b"\x1b")

    def terminate(self, force=False):
        """Close the terminal and sweep the process group.

        Killing the process alone is not enough. Claude Code starts helpers of its own, and it
        can outlive a hangup signal, so a plain terminate leaves those helpers running and the
        laptop slowly fills with them. The negative pid is the process group.
        """
        pid = self.proc.pid
        try:
            self.proc.terminate(force=force)
        except Exception:
            pass
        for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 0.5)):
            if not self.proc.isalive():
                break
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            deadline = time.time() + wait
            while time.time() < deadline and self.proc.isalive():
                time.sleep(0.05)
        return not self.proc.isalive()

    # -- watchers ---------------------------------------------------------------

    def attach(self, rows=None, cols=None):
        sub = Subscriber(rows or self.rows, cols or self.cols)
        with self.lock:
            self.subs.append(sub)
            history = b"".join(self.ring)
        self._resize_to_smallest()
        return sub, history

    def detach(self, sub):
        with self.lock:
            if sub in self.subs:
                self.subs.remove(sub)
        self._resize_to_smallest()

    def set_size(self, sub, rows, cols):
        sub.rows, sub.cols = rows, cols
        self._resize_to_smallest()

    def _resize_to_smallest(self):
        """Fit the terminal to the smallest attached screen.

        Several people watch one terminal: a phone, a laptop, a large display. A terminal has
        one size, so somebody's view must be wrong. Choosing the smallest means the narrow
        screen shows everything and the wide screen has empty space at the edge. The other way
        round, the phone gets text wrapped into nonsense, which is unreadable rather than
        merely untidy.
        """
        with self.lock:
            live = [s for s in self.subs if not s.dead]
            if not live:
                return
            rows = max(10, min(s.rows for s in live))
            cols = max(40, min(s.cols for s in live))
            if (rows, cols) == (self.rows, self.cols):
                return
            self.rows, self.cols = rows, cols
        try:
            self.proc.setwinsize(rows, cols)
        except Exception:
            pass

    def snapshot(self):
        return {
            "pid": self.pid,
            "alive": self.alive(),
            "working": self.working(),
            "watchers": len([s for s in self.subs if not s.dead]),
            "rows": self.rows,
            "cols": self.cols,
            "started": self.started,
            "bytes_out": self.bytes_out,
            "exit_status": self.exit_status,
        }
