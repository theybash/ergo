"""The OpenCode engine: one `opencode run` per turn, on the agent's own conversation.

OpenCode and Claude Code are asked for the same two things here, by different means.

    terminal      the interactive interface in a pseudo-terminal, shown byte for byte
    chat engine   `opencode run --format json`, one process per message

The important difference from Claude Code is the shape of the conversation. Claude Code keeps
one long-lived `claude -p` process reading messages off stdin, and writes a jsonl file that the
console reads. OpenCode runs one short process per turn, keeps the conversation in its own
SQLite database, and prints typed events on stdout. So this module owns three things:

    the argv        where the session id, the model and the permission rules go
    the turn        a process, its events turned into the same frames the chat tab already draws
    the ledger      the session read back from OpenCode's database as the rows transcript.py
                    already returns for Claude

The session id is discovered, not chosen. `opencode run` creates a session, every event carries
its id, and the first turn's id is written to the agent record. A generated id is refused by the
free tier (it treats the id as part of the credential); the one OpenCode made is the one to keep.

Nothing here touches a Claude or a Codex agent. `is_opencode` is the one question every caller
asks before coming in.
"""

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, logs, registry, transcript
from .terminal import clean_env

log = logs.get(__name__)

TOOLS = Path(__file__).resolve().parent / "tools.py"

# Where the binary lives when it was installed as the desktop app rather than on PATH. The app's
# own CLI is the same binary and the only one on this Mac at the time of writing.
APP_CLI = "/Applications/OpenCode.app/Contents/Resources/opencode-cli"

STDERR_LINES = 20

# The event types this version of OpenCode prints. They were read from the binary's output, not
# from a document. A new type is ignored rather than guessed at.
EVENT_TEXT = "text"
EVENT_TOOL = "tool_use"
EVENT_STEP_START = "step_start"
EVENT_STEP_FINISH = "step_finish"
EVENT_ERROR = "error"


def binary():
    exe = shutil.which(config.OPENCODE_BIN) or config.OPENCODE_BIN
    if os.path.sep not in str(exe) and not shutil.which(str(exe)) and os.path.exists(APP_CLI):
        return APP_CLI
    return str(exe)


def is_opencode(rec):
    return bool(rec) and (rec.get("engine") or "claude") == "opencode"


def session_id(rec):
    return (rec or {}).get("opencode_session") or None


# -- configuration ---------------------------------------------------------------

def home_for(rec):
    """The private HOME this agent's OpenCode runs under.

    V2 reads configuration from the project and from `$HOME/.config/opencode`; it ignores the
    v1 `OPENCODE_CONFIG` variable, and its background service caches the config of whichever
    HOME started it. So each agent gets its own HOME and runs with `--standalone`, which starts
    a private server instead of joining that shared one. `.local` and `.cache` are symlinked to
    the real ones, because the session database and the provider sign-in live there: every agent
    reads one ledger, and none of them inherits another's PLATFORM_AGENT.

    A real `.local` directory already in a home made by an older build is left alone rather than
    replaced. The agent still runs; only its ledger would be its own.
    """
    home = config.VAR / "opencode" / rec["id"] / "home"
    (home / ".config" / "opencode").mkdir(parents=True, exist_ok=True)
    for name in (".local", ".cache"):
        link = home / name
        real = Path.home() / name
        if link.is_symlink() or link.exists():
            continue
        if not real.exists():
            log.warning("%s: %s does not exist, so %s stays local", rec["id"], real, name)
            continue
        try:
            os.symlink(str(real), str(link), target_is_directory=True)
        except OSError:
            log.exception("%s: could not link %s to %s", rec["id"], link, real)
    return home


def config_path(rec):
    return home_for(rec) / ".config" / "opencode" / "opencode.json"


def _rule(action, resource="*", effect="allow"):
    return {"action": action, "resource": resource, "effect": effect}


def _permissions(rec):
    """The agent's permission mode, in OpenCode v2's ordered rules.

    V2 took the old `permission` object away: the field is `permissions`, each rule is an action,
    a resource and an effect, and the last matching rule wins. OpenCode's run mode cannot stop
    mid-turn to ask, so "ask" is never written here: a question nobody can answer is a turn that
    hangs. What Claude would ask about, OpenCode refuses, and the agent is told so in the result.

        plan                read only: no edit, no shell, no external directory
        acceptEdits         edits under the agent's own folder, nothing outside it, no shell
        bypassPermissions   everything
    """
    mode = rec.get("permission_mode") or config.DEFAULT_PERMISSION_MODE
    if mode == "bypassPermissions":
        return [_rule("*")]
    if mode != "plan":
        # acceptEdits. Everything the agent needs, and nothing that reaches outside its folder:
        # no shell, because run mode cannot stop to ask and an ask nobody answers is a hung turn.
        # An edit outside the folder is stopped by external_directory before its own rule runs.
        return [
            _rule("*"),
            _rule("read", "*.env", "deny"),
            _rule("read", "*.env.*", "deny"),
            _rule("read", "*.env.example", "allow"),
            _rule("external_directory", "*", "deny"),
            _rule("shell", "*", "deny"),
        ]
    return [
        _rule("*", "*", "deny"),
        _rule("read"),
        _rule("glob"),
        _rule("grep"),
        _rule("webfetch"),
        _rule("websearch"),
        _rule("question"),
        # Code Mode is how V2 exposes MCP tools; denying execute would hide the read-only house
        # tools too. The tools reached through it still check their own rules below.
        _rule("execute"),
        _rule("opencode_list_mcp_resources"),
        _rule("opencode_read_mcp_resource"),
        *[_rule(name) for name in (
            "platform_list_agents", "platform_list_jobs", "platform_task_list",
            "platform_read_agent", "platform_agents_health",
            "platform_browser_snapshot", "platform_browser_screenshot",
            "platform_browser_logins")],
        _rule("read", "*.env", "deny"),
        _rule("read", "*.env.*", "deny"),
        _rule("read", "*.env.example", "allow"),
        _rule("external_directory", "*", "deny"),
        _rule("shell", "*", "deny"),
    ]


ERGO_NOTE = """# Ergo

You are {name}, an agent in {owner}'s Ergo. Ergo is the console at http://127.0.0.1:{port}.
Use only that one; any other copy on this machine is old.

Your Ergo tools are in Code Mode under `tools.platform`, {count} of them. The catalog shows only
some. Before you say you cannot do something, look inside `execute`:
`search({{namespace: "platform", query: "alarm"}})`.

The ones people forget are there:

- `set_alarm` rings {owner}'s phone at a time; `cancel_alarm` takes one back.
- `call_owner` rings {owner}'s phone with you on the line.
- `add_job` runs work later or on a schedule (`in_minutes`, `at`, `once`). Use it, never `sleep`
  or crontab.
- `owner_task_add` puts a task on {owner}'s list; `feed_post` posts to his feed.
"""


def instructions(rec):
    """The files OpenCode reads as standing instructions for this agent.

    OpenCode reads AGENTS.md and not CLAUDE.md, so an agent's CLAUDE.md is named here when its
    folder has no AGENTS.md. First a short note of our own: what Ergo is, and that its tools are
    there even when the catalog does not show them (an agent once said "I don't have any
    tool that reaches your phone", with set_alarm one search away).
    """
    note = home_for(rec) / ".config" / "opencode" / "ergo.md"
    note.write_text(ERGO_NOTE.format(name=rec.get("name") or rec["id"], owner=config.OWNER,
                                      port=config.PORT, count="forty-odd"))
    files = [str(note)]
    cwd = Path(rec.get("cwd") or "")
    if (cwd / "CLAUDE.md").is_file() and not (cwd / "AGENTS.md").is_file():
        files.append(str(cwd / "CLAUDE.md"))
    return files


def write_config(rec):
    """Write this agent's own OpenCode configuration and return its path.

    The file lives in the agent's private HOME, not in the agent's folder: the folder belongs to
    the agent and may already have an `opencode.json`, and two agents sharing a folder must not
    share an identity. PLATFORM_AGENT is the point of the whole file.
    """
    path = config_path(rec)
    servers = {"platform": {
        "type": "local",
        "command": [sys.executable or "python3", str(TOOLS)],
        "environment": {
            "PLATFORM_AGENT": rec["id"],
            "PLATFORM_ROLE": rec.get("role") or "agent",
            # Loopback on purpose, the same as the Claude MCP config. An agent has no business
            # dialling the daemon on an interface other machines can reach.
            "PLATFORM_API": f"http://127.0.0.1:{config.PORT}",
            "PLATFORM_OWNER": config.OWNER,
            "PLATFORM_PARENT": rec.get("parent") or "",
        },
    }}
    body = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"servers": servers},
        "permissions": _permissions(rec),
    }
    body["instructions"] = instructions(rec)
    model = (rec.get("model") or "").strip()
    if "/" in model:
        # OpenCode wants provider/model. A Claude model name has no slash and is not guessed at.
        body["model"] = model
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2))
    tmp.replace(path)
    return path


def env_for(rec):
    write_config(rec)
    server = (os.environ.get("PLATFORM_OPENCODE_SERVER") or "").strip()
    return {"HOME": str(home_for(rec))}, server


def argv_for(rec, resume, engine="chat"):
    """The command line for one opencode process.

    The message is not on it. It goes on stdin, because this version wraps a single argument that
    contains a space in literal quotes, and the operator's words would arrive at the model with
    quote marks around them.
    """
    argv = [binary()]
    server = (os.environ.get("PLATFORM_OPENCODE_SERVER") or "").strip()
    # A private server, not the background one. The background service cached the config from
    # the HOME that started it, and every agent has a HOME of its own now. With a server given
    # explicitly there is nothing to start. `run` only accepts the flag after the subcommand;
    # before it the CLI exits 1 with "Unrecognized flag", which is why the order matters.
    if engine == "chat":
        argv.append("run")
        if not server:
            argv.append("--standalone")
        argv += ["--format", "json"]
    elif not server:
        argv.append("--standalone")
    if resume and session_id(rec):
        argv += ["--session", session_id(rec)]
    model = (rec.get("model") or "").strip()
    # Only run takes --model. The terminal (TUI) has no such flag and exits 1 on it; it reads
    # the model from the agent's own config, which write_config fills in.
    if "/" in model and engine == "chat":
        argv += ["--model", model]
    if (rec.get("permission_mode") or "") == "bypassPermissions":
        # The config already says allow. --auto is what stops the run mode from waiting on a
        # permission the config did not name.
        argv += ["--auto"]
    if server and engine == "chat":
        argv += ["--server", server]
    return argv


# -- the model list --------------------------------------------------------------
#
# OpenCode's own list, with its prices, so the picker can mark the models that cost nothing.
# Free is read from the prices, not from the name: Big Pickle is free and does not say so.

MODELS_TTL = 3600.0
_models = {"at": 0.0, "list": None}


def _free(cost):
    tiers = cost if isinstance(cost, list) else [cost] if isinstance(cost, dict) else []
    tiers = [t for t in tiers if isinstance(t, dict)]
    return bool(tiers) and all(not t.get("input") and not t.get("output") for t in tiers)


def models():
    """Every model OpenCode offers, as {id, name, free}, sorted by id. Asked again after an hour.

    A failed ask keeps the last answer, or gives an empty list, and the picker falls back to its
    own short list.
    """
    now = time.time()
    if _models["list"] is not None and now - _models["at"] < MODELS_TTL:
        return _models["list"]
    try:
        out = subprocess.run([binary(), "api", "GET", "/api/model"], env=clean_env(),
                             capture_output=True, timeout=30).stdout
        data = json.loads(out)["data"]
    except Exception as exc:
        log.warning("could not read OpenCode's model list: %s", exc)
        return _models["list"] or []
    got = [{"id": f"{m.get('providerID')}/{m.get('id')}",
            "name": m.get("name") or m.get("id"),
            "free": _free(m.get("cost"))}
           for m in data if isinstance(m, dict) and m.get("enabled") is not False]
    got.sort(key=lambda r: r["id"])
    _models.update(at=now, list=got)
    return got


# -- the chat engine -------------------------------------------------------------

def _iso(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat() \
            .replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


class OpenCodeEngine:
    """One conversation, one process per message.

    Its `alive` is not the process's. Between turns there is no process and the conversation is
    still the agent, exactly as a terminal at a prompt is still the agent. Only `terminate` ends
    it. That is what lets the broker treat this like any other chat engine: the chat tab stays
    green while the agent waits for the next message, and sleep is one explicit call.

    The argv is built again for every turn, never kept. The first turn has no session id to
    resume, and OpenCode makes one as it answers; a stored argv from that turn would start a
    second conversation on the next message. The same is true of the permission mode, which is
    read from the record and the config file each time.
    """

    def __init__(self, agent_id, argv, cwd, on_event, on_exit=None, permission_mode=None,
                 on_session=None, env=None):
        rec = registry.get(agent_id) or {}
        self.agent_id = agent_id
        # Only a fallback for an agent that is not in the registry (a test, or one being made).
        self.argv = list(argv)
        self.cwd = str(cwd)
        self.on_exit = on_exit
        self.on_session = on_session
        self.permission_mode = permission_mode or rec.get("permission_mode")
        self.started = time.time()
        self.exited_at = None
        self.exit_status = None
        self.last_input = 0.0
        self.session_id = session_id(rec)
        self.stderr = []
        self._env = env or {}
        self._lock = threading.RLock()
        self._proc = None
        self._active = False
        self._queue = []
        self._text_part = None
        # Reuse the Claude parser's frame vocabulary. Its `feed` expects Claude's envelope, so
        # only the frame methods are used here; the event mapping is below.
        from .chat import Stream
        self.stream = Stream(on_event)

    # -- what the broker and the console ask ------------------------------------

    @property
    def pid(self):
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    @property
    def state(self):
        return self.stream.state

    @property
    def live_text(self):
        return self.stream.live_text

    def alive(self):
        return self.exited_at is None

    def running(self):
        return self._active or (self._proc is not None and self._proc.poll() is None)

    def working(self):
        return self.alive() and (self.running() or bool(self._queue))

    def stderr_tail(self):
        return "\n".join(self.stderr).strip()

    def snapshot(self):
        return {
            "engine": "opencode",
            "pid": self.pid,
            "alive": self.alive(),
            "working": self.working(),
            "state": self.stream.state,
            "started": self.started,
            "exit_status": self.exit_status,
        }

    # -- input ------------------------------------------------------------------

    def say(self, text):
        """Give the agent one message, or queue it behind the turn already running."""
        if not self.alive():
            return False
        with self._lock:
            if self.running() or self._queue:
                self._queue.append(text)
                self.last_input = time.time()
                return True
            self.stream.set_state("thinking")
            ok = self._run(text)
            if not ok:
                self.stream.set_state("idle")
        if ok:
            self.last_input = time.time()
        return ok

    def _rec(self):
        rec = registry.get(self.agent_id)
        if not rec:
            return None
        return dict(rec, permission_mode=self.permission_mode or rec.get("permission_mode"))

    def _run(self, text):
        rec = self._rec()
        home = None
        if rec:
            rec = dict(rec, opencode_session=self.session_id or rec.get("opencode_session"))
            try:
                # The config is per agent and lives in its private HOME. Rewriting it each turn
                # is cheap and means a mode changed while the chat was open is already on disk.
                write_config(rec)
                home = home_for(rec)
            except Exception:
                log.exception("%s: could not write the OpenCode config", self.agent_id)
            argv = argv_for(rec, resume=bool(self.session_id), engine="chat")
            cwd = rec.get("cwd") or self.cwd
        else:
            argv, cwd = self.argv, self.cwd
        env = clean_env()
        env["PLATFORM_AGENT_ID"] = self.agent_id
        env.update(self._env)
        if home is not None:
            # After the broker's env, so a stale HOME cannot point this turn at another agent's
            # config. The MCP server reads PLATFORM_AGENT from the config file, not this.
            env["HOME"] = str(home)
            env["PLATFORM_AGENT"] = self.agent_id
            env["PLATFORM_ROLE"] = rec.get("role") or "agent"
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, bufsize=0, start_new_session=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as exc:
            log.exception("%s: opencode would not start", self.agent_id)
            self.stream.emit({"t": "note", "text": f"OpenCode would not start: {exc}"})
            return False
        with self._lock:
            self._proc = proc
            self._active = True
        self.stderr = []
        self._text_part = None
        self.stream.set_state("thinking")
        self.last_input = time.time()
        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        threading.Thread(target=self._read_stdout, args=(proc,), name=f"oc-{self.agent_id}",
                         daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), name=f"oc-err-{self.agent_id}",
                         daemon=True).start()
        return True

    def answer(self, request_id, response):
        # No asks exist in run mode. Kept so the broker's interface has one shape.
        return False

    def request(self, subtype, timeout=0, **fields):
        """Interrupt the turn, or take a new permission mode for the next one."""
        if subtype == "interrupt":
            self._stop_turn()
            return {"subtype": "success"}
        if subtype == "set_permission_mode":
            # Read when the next argv is built and written into the config file now, so a
            # running conversation changes without being restarted.
            self.permission_mode = fields.get("mode")
            rec = self._rec()
            if rec:
                write_config(rec)
            return {"subtype": "success"}
        return None

    def pending_asks(self):
        return []

    def terminate(self, force=False):
        """Close the chat. A turn in flight is stopped. Returns whether it is gone."""
        if self.exited_at is not None:
            return True
        with self._lock:
            self._queue = []
        self.exited_at = time.time()
        gone = self._stop_turn(force=force)
        self.exit_status = self._proc.returncode if self._proc else None
        self.stream.set_state("idle")
        self.stream.end_live()
        if self.on_exit:
            try:
                self.on_exit(self)
            except Exception:
                log.exception("%s: the OpenCode engine exit handler raised", self.agent_id)
        return gone

    def _stop_turn(self, force=False):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return True
        steps = ((signal.SIGKILL, 0.5),) if force else (
            (signal.SIGTERM, 2.0), (signal.SIGKILL, 0.5))
        for sig, wait in steps:
            if proc.poll() is not None:
                break
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            deadline = time.time() + wait
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
        return proc.poll() is not None

    # -- reading ----------------------------------------------------------------

    def _read_stdout(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            try:
                self.feed(raw.decode("utf-8", "replace"))
            except Exception:
                log.exception("%s: an opencode event could not be read", self.agent_id)
        self._reap(proc)

    def _read_stderr(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr.append(line)
                if len(self.stderr) > STDERR_LINES:
                    self.stderr.pop(0)
        try:
            proc.stderr.close()
        except Exception:
            pass

    def feed(self, line):
        line = (line or "").strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        self.handle(event)

    def handle(self, event):
        sid = event.get("sessionID") or (event.get("part") or {}).get("sessionID")
        if sid and sid != self.session_id:
            self.session_id = sid
            self.stream.session_id = sid
            if self.on_session:
                try:
                    self.on_session(sid)
                except Exception:
                    log.exception("%s: could not keep the opencode session id", self.agent_id)
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == EVENT_STEP_START:
            self.stream.set_state("thinking")
        elif kind == EVENT_TEXT:
            text = part.get("text") or ""
            if text:
                # One event carries the whole finished block, not deltas. A new block starts a
                # new live message, because two blocks are two messages in the transcript.
                pid = part.get("id")
                if self._text_part and pid and pid != self._text_part:
                    self.stream.end_live()
                self._text_part = pid or self._text_part
                self.stream._text(text)
        elif kind == EVENT_TOOL:
            if self.stream.live_text:
                self.stream.end_live()
            name = part.get("tool") or "tool"
            state = part.get("state") or {}
            got = state.get("input") or {}
            self.stream.emit({"t": "tool", "name": name,
                              "summary": transcript._short(got)})
        elif kind == EVENT_ERROR:
            err = event.get("error") or {}
            text = err.get("message") or err.get("type") or "OpenCode reported an error"
            self.stream.emit({"t": "note", "text": str(text)})

    def _reap(self, proc):
        if self._proc is not proc:
            return
        try:
            status = proc.wait(timeout=10)
        except Exception:
            status = None
        try:
            proc.stdout.close()
        except Exception:
            pass
        with self._lock:
            if self._proc is proc:
                self._proc = None
            self._active = False
            stuck = None
            if self.exited_at is None and self.alive():
                nxt = "\n\n".join(self._queue)
                self._queue = []
                stuck = nxt or None
        if self.exited_at is not None:
            return
        self.exit_status = status
        # The turn is over. The session file has it by now, so the browser refetches history in
        # place of the live message, exactly as it does when Claude's turn ends.
        self.stream.set_state("idle")
        self.stream.end_live()
        if status and not self.session_id:
            self.stream.emit({"t": "note", "text": (
                f"OpenCode stopped ({status}). " + self.stderr_tail()[-600:]).strip()})
        self.stream.emit({"t": "settled"})
        if status and self.session_id and self.stderr_tail():
            self.stream.emit({"t": "note", "text": self.stderr_tail()[-600:]})
        if stuck:
            # A message typed during the turn goes in now, as the next turn, in order.
            self.stream.set_state("thinking")
            if not self._run(stuck):
                self.stream.set_state("idle")


# -- the ledger ------------------------------------------------------------------
#
# OpenCode keeps one SQLite database, with a row per session and a row per message. It is read
# read-only and never written: the engine owns it. If the schema moves, the queries below fail
# and the console shows an agent with no history, which is the same thing it shows for an agent
# that has never spoken. Nothing else in the daemon depends on it.

_cache = {}
_cache_lock = threading.Lock()


def _db():
    return Path(config.OPENCODE_DB)


def _connect():
    path = _db()
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def _session_row(conn, sid):
    try:
        row = conn.execute(
            "select id, directory, title, cost, tokens_input, tokens_output, tokens_reasoning, "
            "tokens_cache_read, tokens_cache_write, time_created, time_updated "
            "from session_v2 where id = ?", (sid,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    keys = ("id", "directory", "title", "cost", "tokens_input", "tokens_output",
            "tokens_reasoning", "tokens_cache_read", "tokens_cache_write",
            "time_created", "time_updated")
    return dict(zip(keys, row))


def _messages(conn, sid):
    try:
        rows = conn.execute(
            "select type, seq, time_created, data from session_message "
            "where session_id = ? order by seq", (sid,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for kind, seq, created, data in rows:
        try:
            body = json.loads(data or "{}")
        except json.JSONDecodeError:
            continue
        out.append({"type": kind, "seq": seq, "created": created, "data": body})
    return out


# -- the recruit listing ---------------------------------------------------------
#
# The same question recruits.py asks of Claude Code's files, asked of OpenCode's database:
# which conversations exist, what was said first, and which folder they ran in. The screen is
# opened rarely, so this reads the newest few hundred sessions and no more. A schema this
# version does not know is an empty list, not a crash.

MAX_SESSIONS = 200
MAX_TURNS = 500
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _scratch(directory):
    """A folder a command made for a moment, not a place the owner worked."""
    try:
        real = os.path.realpath(str(directory or ""))
        tmp = os.path.realpath(tempfile.gettempdir())
    except OSError:
        return False
    return (real == tmp or real.startswith(tmp + os.sep)
            or real == "/tmp" or real.startswith("/tmp/")
            or real.startswith("/private/tmp/"))


def _first_text(conn, sid):
    """What the person typed first, or nothing if the session only holds machinery."""
    try:
        row = conn.execute(
            "select data from session_message where session_id = ? and type = 'user' "
            "order by seq limit 1", (sid,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        body = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return None
    text = body.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return _text_of(body.get("content")) or None


def _turns(conn, sid):
    """How many assistant messages it has. A rough size signal, like the Claude file count."""
    try:
        row = conn.execute(
            "select count(*) from session_message where session_id = ? and type = 'assistant'",
            (sid,)).fetchone()
    except sqlite3.Error:
        return 0
    return min(int(row[0] or 0) if row else 0, MAX_TURNS)


def _offer(conn, row):
    """One session in the shape the Recruit screen already draws for a Claude file."""
    sid, directory, title, created, updated = row
    return {
        "session_id": sid,
        "cwd": directory,
        "slug": None,
        "name": (title or "").strip() or None,
        "first": _first_text(conn, sid),
        "started": _iso(created),
        "last": _iso(updated),
        # OpenCode keeps no per-session file, so there is no byte size to show. The card can
        # draw the turn count and leave the weight off; a made-up number would be worse.
        "size": None,
        "turns": _turns(conn, sid),
        "engine": "opencode",
    }


def sessions(limit=MAX_SESSIONS):
    """Every OpenCode conversation worth offering, newest first.

    Child sessions are subagent work, and scratch directories are tests; neither is a
    conversation the owner can pick up. A session with no messages yet is not offered either.
    """
    conn = _connect()
    if conn is None:
        return []
    try:
        try:
            rows = conn.execute(
                "select id, directory, title, time_created, time_updated "
                "from session_v2 "
                "where (parent_id is null or parent_id = '') "
                "and (time_archived is null or time_archived = 0) "
                "order by time_updated desc limit ?", (int(limit),)).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            if _scratch(row[1]):
                continue
            got = _offer(conn, row)
            if got["turns"] or got["first"]:
                out.append(got)
        return out
    finally:
        conn.close()


def find(session_id):
    """One OpenCode session by id, even one past the listing's cut, or None."""
    if not SESSION_ID.match(session_id or ""):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        row = _session_row(conn, session_id)
        if not row:
            return None
        return _offer(conn, (row["id"], row["directory"], row["title"],
                             row["time_created"], row["time_updated"]))
    finally:
        conn.close()


def _cached(rec):
    """A session read, kept until OpenCode says the session changed.

    The console asks for the summary on every state request and on every health tick, and a chat
    asks for its rows on every open. Reading two hundred messages each time is wasted work when
    the only thing that can change them is the engine finishing a turn.
    """
    sid = session_id(rec)
    if not sid:
        return None, []
    conn = _connect()
    if conn is None:
        return None, []
    try:
        session = _session_row(conn, sid)
        if session is None:
            return None, []
        key = (sid, session.get("time_updated"))
        with _cache_lock:
            held = _cache.get(rec["id"])
        if held and held[0] == key:
            return session, held[1]
        messages = _messages(conn, sid)
        with _cache_lock:
            _cache[rec["id"]] = (key, messages)
        return session, messages
    finally:
        conn.close()


def forget(agent_id):
    with _cache_lock:
        _cache.pop(agent_id, None)


def _blocks(content):
    return [b for b in (content or []) if isinstance(b, dict)]


def _text_of(content):
    parts = [b.get("text") or "" for b in _blocks(content) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def summary(rec):
    """The same facts transcript.summary gives for a Claude session."""
    session, messages = _cached(rec)
    if session is None:
        return {
            "exists": False, "turns": 0, "last_message": "", "last_message_at": None,
            "last_user_at": None, "tools_used": 0,
            "tokens": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
            "context_tokens": 0, "by_model": {}, "cost_usd": None,
        }

    turns = 0
    tools = 0
    last_message = ""
    last_at = None
    last_user = None
    by_model = {}
    for row in messages:
        body = row.get("data") or {}
        if row["type"] == "user":
            last_user = _iso(body.get("time", {}).get("created") or row.get("created"))
            continue
        if row["type"] != "assistant":
            continue
        content = body.get("content") or []
        text = _text_of(content)
        for block in _blocks(content):
            if block.get("type") == "tool":
                tools += 1
        if text:
            turns += 1
            last_message = text
            last_at = _iso(body.get("time", {}).get("completed")
                           or body.get("time", {}).get("created") or row.get("created"))
        model = body.get("model") or {}
        name = "/".join(x for x in (model.get("providerID"), model.get("id")) if x) or "unknown"
        tokens = body.get("tokens") or {}
        cache = tokens.get("cache") or {}
        got = by_model.setdefault(name, {"input": 0, "output": 0,
                                         "cache_write": 0, "cache_read": 0})
        got["input"] += int(tokens.get("input") or 0)
        got["output"] += int(tokens.get("output") or 0)
        got["cache_write"] += int(cache.get("write") or 0)
        got["cache_read"] += int(cache.get("read") or 0)

    tokens = {
        "input": int(session.get("tokens_input") or 0),
        "output": int(session.get("tokens_output") or 0),
        "cache_write": int(session.get("tokens_cache_write") or 0),
        "cache_read": int(session.get("tokens_cache_read") or 0),
    }
    cost = session.get("cost")
    return {
        "exists": True,
        "turns": turns,
        "last_message": last_message,
        "last_message_at": last_at,
        "last_user_at": last_user,
        "tools_used": tools,
        "tokens": tokens,
        "context_tokens": tokens["input"] + tokens["cache_write"] + tokens["cache_read"],
        "by_model": by_model,
        "cost_usd": round(float(cost), 4) if cost is not None else None,
    }


def recent(rec, count=20):
    """The last few things said, in the same rows transcript.recent returns for Claude."""
    _, messages = _cached(rec)
    out = []
    for row in messages:
        body = row.get("data") or {}
        at = _iso(body.get("time", {}).get("created") or row.get("created"))
        if row["type"] == "user":
            text = (body.get("text") or "").strip()
            if text:
                out.append({"kind": "text", "who": "operator", "at": at, "text": text})
            continue
        if row["type"] != "assistant":
            continue
        for block in _blocks(body.get("content")):
            btype = block.get("type")
            if btype == "text" and (block.get("text") or "").strip():
                out.append({"kind": "text", "who": "agent",
                            "at": _iso((block.get("time") or {}).get("completed")
                                       or body.get("time", {}).get("completed") or row.get("created")),
                            "text": block["text"].strip()})
            elif btype == "tool":
                state = block.get("state") or {}
                out.append({"kind": "tool", "who": "agent", "at": at,
                            "tool": block.get("name"), "id": block.get("id"),
                            "input": transcript._short(state.get("input"))})
                output = state.get("output")
                if output is None:
                    content = state.get("content") or []
                    output = "\n".join(b.get("text") or "" for b in _blocks(content)
                                       if b.get("type") == "text")
                error = state.get("status") == "error" or bool(
                    (state.get("metadata") or {}).get("error"))
                out.append({"kind": "result", "who": "agent", "at": at,
                            "id": block.get("id"), "error": error,
                            "text": transcript._short(output, limit=1200)})
    return out[-count:]


def tool_call(rec, tool_id):
    """The call with this id and its result, in the shape transcript.tool_call returns."""
    if not tool_id:
        return None
    _, messages = _cached(rec)
    for row in messages:
        body = row.get("data") or {}
        if row["type"] != "assistant":
            continue
        for block in _blocks(body.get("content")):
            if block.get("type") != "tool" or block.get("id") != tool_id:
                continue
            state = block.get("state") or {}
            inp = state.get("input")
            if isinstance(inp, dict) and isinstance(inp.get("command"), str):
                shown = inp["command"]
            else:
                shown = json.dumps(inp, indent=2, ensure_ascii=False)
            output = state.get("output")
            if output is None:
                output = "\n".join(b.get("text") or "" for b in _blocks(state.get("content"))
                                   if b.get("type") == "text")
            output = output or ""
            return {"tool": block.get("name"), "input": str(shown)[:transcript.TOOL_MAX],
                    "result": str(output)[:transcript.TOOL_MAX],
                    "error": state.get("status") == "error" or bool(
                        (state.get("metadata") or {}).get("error")),
                    "cut": len(str(output)) > transcript.TOOL_MAX,
                    "at": _iso(body.get("time", {}).get("created"))}
    return None


def recap(rec):
    """OpenCode writes no away-summary row. An agent with none is the normal case."""
    return None


def discover_session(cwd, since, cap=600.0, alive=None):
    """The newest session OpenCode made in this folder after `since`, or None.

    Needed for the terminal, which (unlike run) has no event stream to carry the new session id
    back. Polled from a thread while the terminal is open, and written to the record once.
    """
    deadline = time.time() + cap
    while time.time() < deadline:
        if alive is not None and not alive():
            return None
        conn = _connect()
        if conn is not None:
            try:
                row = conn.execute(
                    "select id from session_v2 where directory = ? and time_created >= ? "
                    "order by time_created desc limit 1",
                    (str(cwd), int(since * 1000))).fetchone()
                if row:
                    return row[0]
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        time.sleep(2.0)
    return None
