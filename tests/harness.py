"""What every test needs: its own state, its own clock, and no real agents.

Three problems had to be solved before any of this could be tested, and they are worth naming
because they are the reason there were no tests for so long.

**State.** Every module keeps its records in a module-level dictionary, so a test cannot simply make
its own copy. Instead each one now has a `reset()`, and `config.use_var()` repoints every state file
at a temporary directory. That gives a clean world per test without turning nine modules into
classes, which would have been a large change to code that was already checked by hand and working.

**The clock.** The scheduler is the only part whose behaviour depends on the time, and waiting until
03:00 to find out what happens at 03:00 is not a test. `clock.freeze()` pins it and the test moves
it.

**Agents.** A real agent costs 400 to 700 MB and ten seconds. `FakeFleet` replaces the three calls
the scheduler makes into the rest of the platform, and records them, so a test can assert that an
agent was woken and then put back to sleep.
"""

import http.server
import json
import warnings
import logging
import shutil
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from commandcenter import (agentcall, broker, live_usage, browsers, phone, clock, config, cords, cron,  # noqa: E402
                           fresh, inbox, mailbox, memory, registry, servers, status, tasks,
                           telegram, usage, voice, watch)

STATEFUL = (registry, mailbox, status, cords, broker, cron, servers, telegram, voice,
            usage, memory, inbox, phone, agentcall, tasks, watch, live_usage)


def _no_speech(payload, key, timeout=None):
    """No test reaches OpenAI's speech endpoint. A test that wants audio replaces this."""
    raise RuntimeError("no speech in tests")

# A fixed moment, so a test that fails says the same thing tomorrow. Wednesday 19 August 2026, 09:00.
NINE_AM = 1787110200.0


class Case(unittest.TestCase):
    """A test with its own state directory, a frozen clock and nothing real behind it."""

    def setUp(self):
        self._real = {name: getattr(config, name) for name in
                      ("VAR", "VIEWS", "AGENTS_FILE", "CORDS_FILE", "TRANSFER_LOG",
                       "PRICES_FILE", "STATUS_FILE", "JOBS_FILE", "JOB_LOG", "SHOWN_DIR", "PORTS_FILE",
                       "TELEGRAM_FILE", "FETCH_TIMEOUT", "FETCH_MAX_BYTES", "WAKE_AT_ONCE",
                       "SERVER_RESTARTS", "TELEGRAM_TOKEN", "TELEGRAM_OPERATOR",
                       "PROMPT_WAIT", "PROMPT_QUIET", "REMOTE_CONTROL", "PROJECTS", "WEB",
                       "VOICE_DIR", "OPENAI_KEY", "VOICE_MODEL", "VOICE_BACKEND", "VOICE_NAME",
                       "VOICE_DAILY_MINUTES", "VOICE_SESSION_MINUTES", "VOICE_CONFIRM_SECONDS",
                       "VOICE_WAIT_PAUSE_SECONDS", "VOICE_TURN_GAP_MS", "VOICE_READ_ROOTS",
                       "VOICE_VISION_MODEL", "VOICE_VISION_TIMEOUT", "VOICE_TRANSCRIBE_MODEL",
                       "VOICE_TRANSCRIBE_TIMEOUT",
                       "OWNER", "HOME", "HOME_AGENT", "SHIFT_REQUESTS", "GEMINI_ENV",
                       "VOICE_MEMORY_TURNS", "TYPESAFE_KEY", "TYPESAFE_TIMEOUT",
                       "VOICE_RECALL_K", "VOICE_RECALL_SPAN", "VOICE_SILENCE_MINUTES",
                       "SMALLTALK", "CLAUDE_BIN", "WATCH_ROOTS", "CODEX_BIN", "CODEX_HOME", "CLAUDE_CREDS", "CLAUDE_STATE",
                       "OPENCODE_BIN", "OPENCODE_DB")}
        self.dir = Path(tempfile.mkdtemp(prefix="cc-test-"))
        config.use_var(self.dir / "var", views=self.dir / "views")
        config.SMALLTALK = False
        # A name set in the shell would change every greeting and tool description.
        config.OWNER = ""
        # The shell running the suite may set PLATFORM_WEB; every test starts on the default console.
        config.WEB = config.web_dir("")
        # No test reaches the owner's real Telegram. The daemon's environment carries the bot token and
        # his chat, and a test run inherited them: every cron test's "job succeeded" went to his
        # phone. A test that wants Telegram sets its own and replaces the transport.
        config.TELEGRAM_TOKEN = ""
        config.TELEGRAM_OPERATOR = ""
        # Nor does any test start a real claude, which would spend real money. A test that wants
        # one points this at its own fake, as the chat tests do.
        config.CLAUDE_BIN = str(self.dir / "no-claude-in-tests")
        # The same for Codex, and its conversations are the test's own too.
        config.CODEX_BIN = str(self.dir / "no-codex-in-tests")
        config.CODEX_HOME = self.dir / "codex"
        # No test asks Anthropic or ChatGPT for the plan limits with the real logins.
        config.CLAUDE_CREDS = self.dir / "no-claude-login"
        # Nor does a test mark folders as trusted in the real ~/.claude.json.
        config.CLAUDE_STATE = self.dir / "claude.json"
        # Session files are Claude Code's, not the platform's, so they live somewhere else and the
        # tests need their own copy of that somewhere.
        config.PROJECTS = self.dir / "projects"
        # OpenCode's ledger is the real one on this laptop. A test that reads the recruit list
        # must not see those conversations unless it builds a database of its own.
        config.OPENCODE_DB = self.dir / "opencode.db"
        # No test reaches TypeSafe. A test that wants the key sets one and replaces the transport.
        config.TYPESAFE_KEY = ""
        config.VOICE_MEMORY_TURNS = 30
        config.VOICE_RECALL_K = 30
        config.VOICE_RECALL_SPAN = 2
        config.VOICE_SILENCE_MINUTES = 8
        # The daemon's environment can carry the real OpenAI key, and a ring records an opening
        # line in the background. Nothing in a test may spend money on that.
        self._speech = agentcall.post_speech
        agentcall.post_speech = _no_speech
        for module in STATEFUL:
            module.reset()
        clock.freeze(NINE_AM)
        # Real Chrome tests check that every character is a key press, not how long a hand takes.
        browsers.TYPING_PAUSE = 0.0
        # One test deliberately orphans a process to check it is adopted after an unclean stop, and
        # Python complains when the Popen object for a live child is collected. The complaint is
        # correct and the test wants it to happen.
        warnings.filterwarnings("ignore", message="subprocess .* is still running")
        # Quiet. A passing test that prints forty log lines is a test nobody reads the output of.
        self._log = logging.getLogger("cc")
        self._handlers, self._level = list(self._log.handlers), self._log.level
        self._log.handlers = [logging.NullHandler()]
        self._log.setLevel(logging.CRITICAL)
        self.addCleanup(self._restore)

    def _restore(self):
        clock.unfreeze()
        browsers.TYPING_PAUSE = 1.0
        for module in STATEFUL:
            module.reset()
        agentcall.post_speech = self._speech
        for name, value in self._real.items():
            setattr(config, name, value)
        self._log.handlers, self._log.level = self._handlers, self._level
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------

    def agent(self, name="box", **kw):
        """A registered agent with a real folder, and nothing running."""
        cwd = self.dir / name
        cwd.mkdir(parents=True, exist_ok=True)
        return registry.create(name=name, cwd=str(cwd), **kw)

    def transcript_for(self, rec, entries):
        """Write a session file for an agent, the way Claude Code writes one."""
        folder = config.project_dir(rec["cwd"])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{rec['session_id']}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return path

    def said(self, text, who="assistant"):
        return {"type": who, "timestamp": "2026-08-20T10:00:00Z",
                "message": {"role": "assistant" if who == "assistant" else "user",
                            "content": [{"type": "text", "text": text}]}}

    def used_tool(self, name, **inputs):
        return {"type": "assistant", "timestamp": "2026-08-20T10:00:01Z",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": name, "input": inputs}]}}

    def tool_said(self, body, error=False):
        return {"type": "user", "timestamp": "2026-08-20T10:00:02Z",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": body,
                     "is_error": error}]}}

    def publish(self, rec, name, text):
        """Write a file an agent has published for its screens."""
        folder = Path(rec["cwd"]) / ".platform" / "data"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(text)
        return path

    def declare(self, rec, command, **extra):
        """Write the server declaration an agent puts in its own folder."""
        folder = Path(rec["cwd"]) / ".platform"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "server.json").write_text(json.dumps({"command": list(command), **extra}))
        return folder / "server.json"

    def catch_alerts(self):
        """Collect what would have gone to the phone, and return the list."""
        sent = []
        real = telegram.notify
        telegram.notify = lambda text: sent.append(text) or True
        self.addCleanup(lambda: setattr(telegram, "notify", real))
        return sent

    def fleet(self, reply="done\ntask succeeded", awake=False, delay=0.0):
        """Stand in for the rest of the platform: no processes, and a record of what was asked."""
        fake = FakeFleet(reply=reply, awake=awake, delay=delay)
        fake.install(self)
        return fake

    def http(self, routes):
        """A local HTTP server for the length of one test. `routes` maps a path to a handler."""
        return LocalServer(routes, self)


class FakeFleet:
    """The three calls the scheduler makes outward, replaced and recorded.

    `awake` is what the platform will say when asked whether an agent is up, and it changes when the
    fake is woken, because the interesting assertion is "woken, then put back", and an `awake` that
    never changed would let a broken implementation pass.
    """

    def __init__(self, reply="done\ntask succeeded", awake=False, delay=0.0):
        self.reply = reply
        self.awake_ids = set()
        self.start_awake = awake
        self.delay = delay
        self.asked = []
        self.woken = []
        self.slept = []
        self.fresh = []
        self.inside = 0
        self.peak = 0
        self._lock = threading.Lock()

    def install(self, case):
        real = (cords.ask, broker.awake, broker.sleep, broker.start, fresh.run)

        def ask(agent_id, prompt, timeout=300.0):
            with self._lock:
                self.inside += 1
                self.peak = max(self.peak, self.inside)
            self.asked.append({"agent": agent_id, "prompt": prompt, "timeout": timeout})
            self.awake_ids.add(agent_id)
            self.woken.append(agent_id)
            if self.delay:
                import time as _t
                _t.sleep(self.delay)
            with self._lock:
                self.inside -= 1
            return self.reply

        def awake(agent_id):
            return self.start_awake or agent_id in self.awake_ids

        def sleep(agent_id, force=False):
            self.slept.append(agent_id)
            self.awake_ids.discard(agent_id)
            return True

        def start(agent_id):
            self.awake_ids.add(agent_id)
            self.woken.append(agent_id)
            return None

        def fresh_run(job, prompt, timeout):
            # The same counting as ask(), and no wake: a fresh run never touches the agent.
            with self._lock:
                self.inside += 1
                self.peak = max(self.peak, self.inside)
            self.asked.append({"agent": job["agent"], "prompt": prompt, "timeout": timeout})
            self.fresh.append(job["agent"])
            if self.delay:
                import time as _t
                _t.sleep(self.delay)
            with self._lock:
                self.inside -= 1
            return {"reply": self.reply, "model": job.get("model"), "cost_usd": 0.01,
                    "tokens": {"in": 10, "cache_write": 0, "cache_read": 0, "out": 5},
                    "turns": 1, "file": None, "error": None}

        cords.ask, broker.awake, broker.sleep, broker.start, fresh.run = (
            ask, awake, sleep, start, fresh_run)

        def undo():
            cords.ask, broker.awake, broker.sleep, broker.start, fresh.run = real
        case.addCleanup(undo)
        return self


class LocalServer:
    """A throwaway HTTP server. Used for fetching, for an agent's own server, and for Telegram."""

    def __init__(self, routes, case):
        self.routes = routes
        self.hits = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _run(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                outer.hits.append({"path": self.path, "method": self.command,
                                   "headers": dict(self.headers), "body": body})
                handler = outer.routes.get(path) or outer.routes.get("*")
                if handler is None:
                    return self.send_error(404)
                got = handler(self, body)
                if got is None:
                    return
                code, ctype, payload = got
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode()
                elif isinstance(payload, str):
                    payload = payload.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _run

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        case.addCleanup(self.close)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def json_route(value, code=200):
    return lambda handler, body: (code, "application/json", value)


def text_route(value, code=200, ctype="text/plain"):
    return lambda handler, body: (code, ctype, value)
