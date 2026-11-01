"""The chat engine: one headless `claude -p` per agent, for the chat tab.

An agent has two possible engines and only ever one of them running. The terminal is the
interactive Claude Code in a pseudo-terminal, which the console shows byte for byte. The chat
engine is the same conversation driven headless, with typed events on stdout instead of a drawn
screen.

The chat engine exists because the chat tab used to read the message being written right now by
scraping the pseudo-terminal. That meant parsing a redrawn box with escape sequences in it, and
it broke every time the interface changed its layout. `claude -p --output-format stream-json`
hands over the same text as events, so the live message is read rather than guessed.

Both engines write to the same session file on disk, because both run with the agent's own cwd
and the same session id. That file is what keeps the two in step: a turn typed in the terminal is
in the chat's history, and a turn typed in the chat is in the terminal's conversation. Nothing
else joins them, so nothing here may change the working directory or the session id.

The parser is a separate class from the process on purpose. The parser is the part that gets a
rule wrong, and keeping it apart means a test can feed it a captured turn line by line with no
`claude` anywhere near the machine.
"""

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque

from . import logs, transcript
from .terminal import clean_env

log = logs.get(__name__)

# Enough of the error to say what went wrong, not the whole log. A crash that prints a stack
# trace of two hundred lines is not more informative than its last twenty.
STDERR_LINES = 20

# How long to wait for the engine to acknowledge one of our control requests. Setting the
# permission mode and interrupting a turn are both answered in milliseconds when they are answered
# at all, so a wait this long is already generous, and the caller is a browser waiting on a click.
CONTROL_WAIT = 3.0


class Stream:
    """One turn of `claude -p`, turned into the frames a browser draws.

    Every frame has a `t` field and goes out on the agent's existing WebSocket as text, beside
    the binary frames that carry terminal bytes. The frames are:

        mode      which engine runs now. The broker sends this one, not the parser.
        state     idle or thinking, for the whole turn.
        live      the message being written this instant: start, delta, end.
        tool      one line naming a tool the agent called.
        settled   the turn is in the session file now, so the browser refetches history.
        ask       Claude is waiting on us: a permission ask or a question ask.
        ask_done  that ask is answered or lost, so the browser removes the card.

    A live delta carries the whole reply so far rather than the new piece. That costs a few
    hundred bytes a frame and buys the thing that matters on a phone: a dropped frame repairs
    itself on the next one instead of leaving a hole in the middle of a sentence.
    """

    def __init__(self, on_event):
        self.on_event = on_event
        self.state = "idle"
        self.live_text = ""
        self.live = False
        self.session_id = None
        # Tool calls whose input is still arriving, by content block index. See _tool_frame.
        self._tools = {}
        # Asks Claude is waiting on, by request id, oldest first. Kept in memory only: a daemon
        # restart ends the engine, and the ask dies with the process that was waiting on it.
        self.asks = {}
        # Control requests we sent that are waiting for their acknowledgement, by request id.
        self.acks = {}
        # The reading thread fills both dictionaries and the browser's thread empties them.
        self._lock = threading.Lock()

    # -- outgoing ---------------------------------------------------------------

    def emit(self, frame):
        try:
            self.on_event(frame)
        except Exception:
            # A browser that went away must not stop the parser. The listener is what knows
            # about dead sockets; this only knows about events.
            log.exception("a chat frame listener raised")

    def set_state(self, state):
        """Say idle or thinking, and only when it changed.

        Repeating a state the browser already has is a frame per event on a long turn, and the
        browser draws exactly the same thing.
        """
        if state != self.state:
            self.state = state
            self.emit({"t": "state", "state": state})

    def end_live(self):
        if self.live:
            self.live = False
            self.live_text = ""
            self.emit({"t": "live", "phase": "end"})

    # -- incoming ---------------------------------------------------------------

    def feed(self, line):
        """One line of stdout. Anything that is not a whole JSON object is dropped."""
        line = (line or "").strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        self.handle(event)

    def handle(self, event):
        """One parsed event.

        The whole line is parsed before the type is read, deliberately. The `result` row does not
        have `type` as its first key, it sits deep in the object, so anything that peeks at the
        start of the line to decide what a row is will miss the end of every turn and leave the
        chat thinking for ever.
        """
        kind = event.get("type")
        if kind == "rate_limit_event":
            from . import quota
            quota.seen(event)
        elif kind == "system":
            if event.get("subtype") == "init":
                self.session_id = event.get("session_id") or self.session_id
            # The other subtype seen is "status", which reports the request lifecycle. The
            # browser already has `state` for that, so it is dropped.
        elif kind == "stream_event":
            self._stream(event.get("event") or {})
        elif kind == "assistant":
            # A cumulative snapshot of the whole message so far, sent again after every block.
            # Adding it to the live text would print the reply twice, then three times. The
            # deltas already carry every character, and the authoritative copy is the session
            # file, which the browser refetches on `settled`. So this row is read for nothing.
            pass
        elif kind == "result":
            self.set_state("idle")
            self.end_live()
            # Claude only waits on an ask inside a turn, so a turn that ended with one still
            # pending ended without it. An interrupt does exactly that. The card has to go, or it
            # sits there offering a yes that reaches nobody.
            self.drop_asks()
            # The turn is in the session file by now, so the browser stops drawing the live
            # message and refetches the settled history in its place.
            self.emit({"t": "settled"})
        elif kind == "control_request":
            self._ask(event)
        elif kind == "control_response":
            self._ack(event)

    def _stream(self, ev):
        kind = ev.get("type")
        if kind == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                # The text before a tool call is a finished message: Claude Code writes it to the
                # session file, and the browser draws it from the history. Keeping it in the live
                # reply as well drew it twice for as long as the tool ran. So the live reply
                # empties here, and the next text starts a fresh one.
                if self.live_text:
                    self.live_text = ""
                    self.emit({"t": "live", "phase": "delta", "text": ""})
                self._tool_start(ev.get("index"), block)
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                self._text(delta.get("text") or "")
            elif dtype == "input_json_delta":
                pending = self._tools.get(ev.get("index"))
                if pending is not None:
                    pending["json"] += delta.get("partial_json") or ""
            # thinking_delta and signature_delta are the model's private reasoning. The terminal
            # shows it and the chat does not, which is the operator's own choice: a chat is what
            # was said, not what was considered.
        elif kind == "content_block_stop":
            self._tool_frame(ev.get("index"))
        # message_start, message_delta and message_stop say nothing the browser draws. The turn
        # begins when the message is written to stdin and ends on `result`, both of which are
        # more honest than a boundary inside the stream.

    def _text(self, piece):
        if not self.live:
            self.live = True
            self.live_text = ""
            self.emit({"t": "live", "phase": "start"})
        self.live_text += piece
        self.emit({"t": "live", "phase": "delta", "text": self.live_text})

    def _tool_start(self, index, block):
        """A tool call has begun. Its input may be here, or may still be arriving.

        Both shapes are real. A block that already carries its input is named at once. A block
        whose input streams in as `input_json_delta` pieces is held until the block closes,
        because naming it at the start would say "Bash" with an empty command, which is the one
        part anybody reads.
        """
        tool = {"name": block.get("name") or "tool", "input": block.get("input") or {}}
        if tool["input"]:
            self._say_tool(tool["name"], tool["input"])
            return
        tool["json"] = ""
        self._tools[index] = tool

    def _tool_frame(self, index):
        tool = self._tools.pop(index, None)
        if tool is None:
            return
        got = tool["input"]
        if not got and tool["json"]:
            try:
                got = json.loads(tool["json"])
            except json.JSONDecodeError:
                got = tool["json"]
        self._say_tool(tool["name"], got)

    def _say_tool(self, name, got):
        # Trimmed with the same helper the settled history uses, so a tool call reads the same
        # way whether it is live or read back from the file. A file write can be a megabyte and
        # the line on the screen holds about one sentence of it.
        self.emit({"t": "tool", "name": name, "summary": transcript._short(got)})

    # -- asks and acks ----------------------------------------------------------
    #
    # Two kinds of control line share this stream and they travel in opposite directions. A
    # control_request coming in is Claude asking us something and waiting, indefinitely, for the
    # answer. A control_response coming in is the engine acknowledging something we asked it,
    # such as a change of permission mode.

    def _ask(self, event):
        """Claude wants an answer. Record it and put a card on the screen.

        Only can_use_tool is handled. Claude Code sends other control requests and will send more,
        and a card for a request nobody can answer is worse than no card: it sits there for ever
        because there is no button that ends it.
        """
        rid = event.get("request_id")
        req = event.get("request") or {}
        if not rid or req.get("subtype") != "can_use_tool":
            return
        tool = req.get("tool_name") or "tool"
        got = req.get("input") or {}
        if tool == "AskUserQuestion":
            # The question tool arrives through the same permission ask, so the tool name is the
            # only thing that separates "may I run this" from "which one do you want". The request
            # also carries requires_user_interaction, but the name is simpler and says the same.
            frame = {"t": "ask", "id": rid, "kind": "question",
                     "questions": got.get("questions") or []}
        else:
            # The raw input travels beside the summary because the summary is one line and a
            # command worth approving is often longer than one line. The card shows the summary
            # and the whole command on a tap.
            frame = {"t": "ask", "id": rid, "kind": "perm", "tool": tool,
                     "summary": transcript._short(got), "input": got}
        with self._lock:
            self.asks[rid] = {"kind": frame["kind"], "tool": tool, "input": got, "frame": frame}
        self.emit(frame)

    def _ack(self, event):
        """An answer to something we asked. Wake whoever is waiting on it."""
        reply = event.get("response") or {}
        rid = reply.get("request_id") or event.get("request_id")
        with self._lock:
            box = self.acks.get(rid)
        if box is None:
            return
        box["reply"] = reply
        box["event"].set()

    def ask(self, request_id):
        with self._lock:
            return self.asks.get(request_id)

    def take_ask(self, request_id):
        with self._lock:
            return self.asks.pop(request_id, None)

    def pending(self):
        """The ask frames still waiting, oldest first, exactly as they were sent."""
        with self._lock:
            return [held["frame"] for held in self.asks.values()]

    def drop_asks(self):
        """Forget every ask and say so, because the process that was waiting has gone."""
        with self._lock:
            gone = list(self.asks)
            self.asks.clear()
        for rid in gone:
            self.emit({"t": "ask_done", "id": rid})
        return gone


class ChatEngine:
    """One `claude -p` process, its stdout parsed and its stdin fed one message at a time.

    One process serves the whole conversation. It stays alive between turns reading stream-json
    off stdin, so the wait before an answer is the model rather than a start-up. It is spawned
    lazily on the first message, exactly like the terminal is spawned on the first wake: an agent
    sitting in chat mode with nobody typing costs nothing.
    """

    def __init__(self, agent_id, argv, cwd, on_event, on_exit=None, permission_mode=None):
        self.agent_id = agent_id
        self.argv = list(argv)
        self.cwd = str(cwd)
        self.on_exit = on_exit
        # What Claude may do without asking. It starts as the record says and can be changed
        # while the process runs, so the engine holds the live value and the record holds the one
        # the next start will use.
        self.permission_mode = permission_mode
        self.started = time.time()
        self.exited_at = None
        self.exit_status = None
        self.last_input = 0.0
        self.stderr = deque(maxlen=STDERR_LINES)
        self.stream = Stream(on_event)
        # One writer at a time on stdin. A message typed in the browser, an answer to an ask and
        # an interrupt all arrive on different threads, and two half lines interleaved on stdin
        # are two lines of json that neither side can parse.
        self._writing = threading.Lock()

        env = clean_env()
        # Lets an agent find the console without being told, the same as in a terminal.
        env["PLATFORM_AGENT_ID"] = agent_id
        # start_new_session puts the process in a group of its own. Without it the sweep in
        # terminate() would signal the daemon's own group, which ends the console.
        self.proc = subprocess.Popen(
            self.argv, cwd=self.cwd, env=env, bufsize=0, start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        threading.Thread(target=self._read_stdout, name=f"chat-{agent_id}",
                         daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"chat-err-{agent_id}",
                         daemon=True).start()

    # -- what the broker and the console ask ------------------------------------

    @property
    def pid(self):
        return self.proc.pid

    @property
    def state(self):
        return self.stream.state

    @property
    def live_text(self):
        return self.stream.live_text

    def alive(self):
        return self.exited_at is None and self.proc.poll() is None

    def working(self):
        return self.alive() and self.stream.state == "thinking"

    def stderr_tail(self):
        return "\n".join(self.stderr).strip()

    def snapshot(self):
        return {
            "engine": "chat",
            "pid": self.pid,
            "alive": self.alive(),
            "working": self.working(),
            "state": self.stream.state,
            "started": self.started,
            "exit_status": self.exit_status,
        }

    # -- input ------------------------------------------------------------------

    def _write(self, obj):
        """One JSON line on stdin, flushed. Returns whether it went."""
        if not self.alive():
            return False
        line = json.dumps(obj)
        with self._writing:
            try:
                self.proc.stdin.write((line + "\n").encode())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                return False
        return True

    def say(self, text):
        """Give the agent one message. One JSON line on stdin, flushed.

        The line is not echoed back on stdout, so nothing in the stream ever says what the
        operator typed. The transcript has it a moment later, and until then the browser is
        shown its own echo frame, which the broker sends before this is called.
        """
        if not self._write({"type": "user", "message": {
                "role": "user", "content": [{"type": "text", "text": text}]}}):
            return False
        self.last_input = time.time()
        # Thinking starts the moment the message is written, not at the first event. The model
        # takes seconds to say anything at all, and a chat that looks idle for those seconds
        # reads as a message that was dropped.
        self.stream.set_state("thinking")
        return True

    def answer(self, request_id, response):
        """Answer one ask. Returns whether the line went and the card was cleared.

        The shape is exact and it was learned the hard way. A control_response without the inner
        `response` wrapper is read, accepted and ignored: no error, no tool result, and the turn
        sits blocked for ever. So the wrapper is built here rather than by the caller.
        """
        held = self.stream.take_ask(request_id)
        if held is None:
            return False
        ok = self._write({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": response}})
        if not ok:
            return False
        # Answered, so every browser watching drops the card, including the one that did not
        # answer it. Two phones on one agent is the normal case here.
        self.stream.emit({"t": "ask_done", "id": request_id})
        return True

    def request(self, subtype, timeout=CONTROL_WAIT, **fields):
        """Ask the engine to do something and wait for its acknowledgement. None means no answer.

        The wait is bounded and the caller is told nothing came back, because the alternative is a
        browser click that never returns. Everything this sends is acknowledged in milliseconds
        when the process is healthy, so a timeout means the process is not.
        """
        rid = str(uuid.uuid4())
        box = {"event": threading.Event(), "reply": None}
        with self.stream._lock:
            self.stream.acks[rid] = box
        try:
            if not self._write({"type": "control_request", "request_id": rid,
                                "request": dict(fields, subtype=subtype)}):
                return None
            box["event"].wait(timeout)
            return box["reply"]
        finally:
            with self.stream._lock:
                self.stream.acks.pop(rid, None)

    def pending_asks(self):
        return self.stream.pending()

    def terminate(self, force=False):
        """End the process and sweep its group. Returns whether it is gone.

        Closing stdin first is the polite ending: `claude -p` stops when its input ends, and a
        process that stops by itself writes out whatever it was holding. The signals are the
        fallback, and they go to the group rather than the process because Claude Code starts
        helpers that outlive a hangup and would otherwise pile up on the laptop.
        """
        if self.proc.poll() is not None:
            return True
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        pid = self.proc.pid
        steps = ((signal.SIGKILL, 0.5),) if force else ((signal.SIGTERM, 2.0),
                                                        (signal.SIGKILL, 0.5))
        for sig, wait in steps:
            if self.proc.poll() is not None:
                break
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            deadline = time.time() + wait
            while time.time() < deadline and self.proc.poll() is None:
                time.sleep(0.05)
        return self.proc.poll() is not None

    # -- reading ----------------------------------------------------------------

    def _read_stdout(self):
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                self.stream.feed(raw.decode("utf-8", "replace"))
            except Exception:
                log.exception("%s: a chat event could not be read", self.agent_id)
        self._reap()

    def _read_stderr(self):
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr.append(line)

    def _reap(self):
        """The stream ended, so the process has. Called once, from the reading thread."""
        self.exited_at = time.time()
        try:
            self.exit_status = self.proc.wait(timeout=5)
        except Exception:
            pass
        # Three pipes per engine. A daemon that switches modes all day would run out of file
        # handles long before it ran out of memory.
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass
        if self.on_exit:
            try:
                self.on_exit(self)
            except Exception:
                log.exception("%s: the chat engine exit handler raised", self.agent_id)
