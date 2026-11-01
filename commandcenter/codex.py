"""The Codex engine: an agent that runs on the Codex CLI instead of Claude Code.

An agent's record says which engine it runs on: "claude", the default, or "codex". A Codex agent
looks the same in the console. Its chat streams the same frames, its history comes back in the
same rows, and its terminal is the Codex interface in a pseudo-terminal.

The chat engine for Claude Code is one long `claude -p` process that reads a message at a time.
Codex has no such mode. `codex exec --json` runs one turn and exits, and `codex exec resume <id>`
runs the next turn in the same conversation. So here one turn is one process, and the engine
object is what lives between turns. It holds no memory while it waits, which is cheaper than
Claude's engine, and the cost is a start-up of a second or two on every turn.

Codex names its own conversation. It does not take an id from us the way Claude Code takes
--session-id. So the first turn runs without resume, Codex says the id in its first line
(thread.started), and the record keeps it as codex_thread. Every later turn resumes it.

Codex cannot ask mid-turn in exec mode. Its approval policy there is "never": a command the
sandbox does not allow fails, and the model sees the failure. So a Codex chat shows no ask cards,
and the permission mode is turned into a sandbox instead:

    plan, or no mode        read-only: it reads, it changes nothing
    acceptEdits             workspace-write: it writes inside the agent's folder, no network
    bypassPermissions       danger-full-access: it may do anything, as the mode says

workspace-write runs commands without asking, which acceptEdits does not. The sandbox is what
keeps that inside the folder, so nothing outside the folder changes without the mode allowing it.
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import config, logs, registry
from .chat import STDERR_LINES, Stream
from .terminal import clean_env

log = logs.get(__name__)

ENGINES = ("claude", "codex")

TOOLS = Path(__file__).resolve().parent / "tools.py"

# The permission mode, as a Codex sandbox. See the module docstring for why each one.
SANDBOX = {
    "plan": "read-only",
    "acceptEdits": "workspace-write",
    "bypassPermissions": "danger-full-access",
}

# Claude model names on a Codex agent would make codex exit at once with an unknown model, so they
# are left out and Codex uses its own default.
CLAUDE_MODELS = ("opus", "sonnet", "haiku", "fable", "claude")


def is_codex(rec):
    return bool(rec) and rec.get("engine") == "codex"


def binary():
    found = shutil.which(config.CODEX_BIN)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "codex"
    return str(local) if local.exists() else config.CODEX_BIN


# The daemon's own keys. OPENAI_API_KEY is for the voice features, and Codex would take a key it
# finds in its environment over its own sign-in. So a Codex run
# never sees either name.
HIDDEN_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY")


def run_env(agent_id):
    env = {k: v for k, v in clean_env().items() if k not in HIDDEN_ENV}
    env["PLATFORM_AGENT_ID"] = agent_id
    return env


def sandbox_for(mode):
    return SANDBOX.get(mode or "", "read-only")


def _toml(value):
    # A JSON string is a valid TOML basic string, which is what codex -c parses.
    return json.dumps(value)


def common_flags(rec, approval):
    """The -c flags every Codex run of this agent gets: sandbox, approvals, model, tools."""
    flags = ["-c", f"sandbox_mode={_toml(sandbox_for(rec.get('permission_mode')))}",
             "-c", f"approval_policy={_toml(approval)}"]
    model = (rec.get("model") or "").strip()
    if model and not model.lower().startswith(CLAUDE_MODELS):
        flags += ["-m", model]
    # The platform's own tools, the same server and the same environment a Claude agent gets.
    # They are approved ahead, as --allowedTools mcp__platform does for Claude: without that, exec
    # refuses every call with "requires approval, but approval policy is never".
    env = {"PLATFORM_AGENT": rec["id"], "PLATFORM_ROLE": rec.get("role") or "agent",
           "PLATFORM_API": f"http://127.0.0.1:{config.PORT}",
           "PLATFORM_PARENT": rec.get("parent") or ""}
    table = "{" + ",".join(f"{k}={_toml(v)}" for k, v in env.items()) + "}"
    flags += ["-c", f"mcp_servers.platform.command={_toml(sys.executable or 'python3')}",
              "-c", f"mcp_servers.platform.args=[{_toml(str(TOOLS))}]",
              "-c", f"mcp_servers.platform.env={table}",
              "-c", 'mcp_servers.platform.default_tools_approval_mode="approve"']
    # The instructions. Codex reads AGENTS.md, and the houses keep CLAUDE.md, so without these a
    # Codex agent ran with none of its own or its house's rules. CLAUDE.md is read where AGENTS.md
    # is missing. Codex reads every one from the project root down to the agent's folder, and the
    # root is the folder with the marker in it: the home folder (see ROOT_MARK), so each house's
    # CLAUDE.md counts, and a git repo inside an agent's folder does not cut the chain. The global
    # file, ~/.claude/CLAUDE.md, is ~/.codex/AGENTS.md (see link_global).
    link_global()
    flags += ["-c", 'project_doc_fallback_filenames=["CLAUDE.md"]',
              "-c", f"project_root_markers=[{_toml(ROOT_MARK)}]",
              "-c", f"project_doc_max_bytes={DOC_BYTES}"]
    return flags


# The marker for Codex's project root, kept in the home folder, and how much of the CLAUDE.md
# chain Codex may read. A house's file alone is about 20 KB, and Codex stops at 32 KB by default.
ROOT_MARK = ".ergo-root"
DOC_BYTES = 262144


def link_global():
    """Make the home folder Codex's project root, and the global CLAUDE.md its global AGENTS.md.

    Run each time a Codex run is built, so it holds on a new laptop too. An AGENTS.md of its own that
    Codex already has is left as it is."""
    home = Path.home()
    try:
        (home / ROOT_MARK).touch(exist_ok=True)
        mine, theirs = home / ".claude" / "CLAUDE.md", home / ".codex" / "AGENTS.md"
        if mine.exists() and not theirs.exists() and not theirs.is_symlink():
            theirs.parent.mkdir(parents=True, exist_ok=True)
            theirs.symlink_to(mine)
    except OSError as exc:
        log.warning("codex: could not link the instructions: %s", exc)


# -- models and the sign-in ---------------------------------------------------------------------

def models():
    """The models the signed-in account may pick, from the list Codex keeps on disk. Codex fetches
    it itself, for the account it is signed in with, so a new model shows up here with no change."""
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
    try:
        got = json.loads(path.read_text()).get("models") or []
    except (OSError, ValueError):
        return []
    out = [{"id": m.get("slug"), "name": m.get("display_name") or m.get("slug"),
            "about": m.get("description") or ""}
           for m in sorted(got, key=lambda m: m.get("priority") or 0)
           if m.get("slug") and m.get("visibility") == "list"]
    return out


_login = {"proc": None, "url": None, "code": None, "error": None}
_login_lock = threading.Lock()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def login_status():
    """Whether Codex is signed in, and how. Never which account: that stays on the laptop."""
    try:
        out = subprocess.run([binary(), "login", "status"], capture_output=True, text=True,
                             timeout=20, env=run_env("codex-login"))
        said = ANSI.sub("", (out.stdout or "") + (out.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        said, out = str(exc), None
    signed = bool(out) and out.returncode == 0 and "logged in" in said.lower()
    how = "ChatGPT" if "chatgpt" in said.lower() else ("an API key" if "api key" in said.lower() else None)
    with _login_lock:
        pending = _login["proc"] is not None and _login["proc"].poll() is None
        return {"signed_in": signed, "how": how if signed else None,
                "pending": {"url": _login["url"], "code": _login["code"]} if pending else None,
                "error": _login["error"]}


def login_start(on_done=None):
    """Start a sign-in with a one-time code, for a phone as well as this laptop: Codex prints a
    page and a code, he opens the page anywhere and types the code, and Codex finishes here.
    `on_done` hears the new status when it ends, so the screen needs no polling."""
    with _login_lock:
        if _login["proc"] is not None and _login["proc"].poll() is None:
            return login_status()
        _login.update(url=None, code=None, error=None)
        proc = subprocess.Popen([binary(), "login", "--device-auth"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env={**run_env("codex-login"), "BROWSER": "true", "NO_COLOR": "1"})
        _login["proc"] = proc
    seen, deadline = [], time.monotonic() + 20
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = ANSI.sub("", line).strip()
        seen.append(line)
        url = re.search(r"https://\S+", line)
        if url and not _login["url"]:
            _login["url"] = url.group(0)
        code = re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4,6}", line)
        if code:
            _login["code"] = code.group(0)
            break
    if not _login["code"]:
        proc.kill()
        _login["error"] = "Codex did not give a code: " + " ".join(seen[-3:])[:300]
        return login_status()

    def wait():
        for _ in proc.stdout:
            pass
        proc.wait()
        if proc.returncode != 0:
            _login["error"] = "The sign-in did not finish. The code may have run out: start again."
        if on_done:
            on_done(login_status())
    threading.Thread(target=wait, name="codex-login", daemon=True).start()
    return login_status()


def exec_argv(rec, thread=None):
    """One chat turn. The prompt goes on stdin, so the argv ends with "-"."""
    argv = [binary(), "exec"]
    if thread:
        argv += ["resume"]
    argv += ["--json", "--skip-git-repo-check"] + common_flags(rec, "never")
    if thread:
        argv += [thread]
    return argv + ["-"]


def terminal_argv(rec):
    """The Codex interface in a terminal. Here a person is watching, so Codex may ask."""
    approval = "never" if rec.get("permission_mode") == "bypassPermissions" else "on-request"
    # The terminal builds its own environment, so env takes the keys out here.
    argv = ["env"] + [f"--unset={k}" for k in HIDDEN_ENV] + [binary()]
    if rec.get("codex_thread"):
        argv += ["resume", rec["codex_thread"]]
    return argv + common_flags(rec, approval)


# -- the conversation on disk -------------------------------------------------------

_paths = {}


def session_file(rec):
    """The rollout file holding this agent's conversation, or nothing before its first turn."""
    thread = rec.get("codex_thread")
    if not thread:
        return None
    held = _paths.get(thread)
    if held and held.exists():
        return held
    root = config.CODEX_HOME / "sessions"
    try:
        for found in root.glob(f"*/*/*/rollout-*-{thread}.jsonl"):
            _paths[thread] = found
            return found
    except OSError:
        pass
    return None


def adopt(rec, since):
    """Find the conversation a terminal started for an agent with no thread yet, and keep it.

    The Codex interface started without resume names a new conversation and does not tell us.
    The newest rollout file in the agent's folder, started after the terminal, is that one.
    """
    if rec.get("codex_thread"):
        return rec["codex_thread"]
    best = None
    # A thread another agent holds is that agent's conversation, never this one's.
    owned = {a.get("codex_thread") for a in registry.all(include_hidden=True) if a.get("codex_thread")}
    root = config.CODEX_HOME / "sessions"
    try:
        files = sorted(root.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        return None
    for path in files[:20]:
        try:
            if path.stat().st_mtime < since:
                break
            with open(path, encoding="utf-8") as fh:
                meta = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        payload = meta.get("payload") or {}
        if (meta.get("type") == "session_meta" and payload.get("cwd") == rec["cwd"]
                and payload.get("id") not in owned):
            best = payload.get("id")
            break
    if best:
        registry.update(rec["id"], codex_thread=best)
    return best


def _texts(content, kinds=("text", "Text")):
    return "\n".join(b.get("text") or "" for b in content or []
                     if isinstance(b, dict) and b.get("type") in kinds).strip()


def _command(item):
    """The command a person would have typed, without the shell wrapper Codex adds."""
    cmd = item.get("command")
    if isinstance(cmd, list):
        parts = cmd
    else:
        try:
            parts = shlex.split(cmd or "")
        except ValueError:
            return cmd or ""
    if len(parts) >= 3 and parts[1] in ("-lc", "-c"):
        return parts[2]
    return " ".join(parts) if isinstance(cmd, list) else (cmd or "")


def _tool_name(kind, item):
    kind = kind.lower().replace("_", "")
    if kind == "commandexecution":
        return "Bash"
    if kind == "mcptoolcall":
        return f"mcp__{item.get('server') or 'mcp'}__{item.get('tool') or 'tool'}"
    if kind == "filechange":
        return "Edit"
    if kind == "websearch":
        return "WebSearch"
    if kind == "todolist":
        return "TodoWrite"
    return item.get("type") or "tool"


def _tool_input(kind, item):
    from .transcript import _short
    kind = kind.lower().replace("_", "")
    if kind == "commandexecution":
        return _short(_command(item))
    if kind == "mcptoolcall":
        return _short(item.get("arguments"))
    if kind == "filechange":
        return _short("\n".join(f"{c.get('kind', '')} {c.get('path', '')}".strip()
                                for c in item.get("changes") or [] if isinstance(c, dict)))
    if kind == "websearch":
        return _short(item.get("query"))
    skip = {"id", "type", "status", "duration"}
    return _short({k: v for k, v in item.items() if k not in skip})


def _tool_result(kind, item):
    """(text, error) for a finished tool item, or None when it carries no result."""
    kind = kind.lower().replace("_", "")
    if kind == "commandexecution":
        code = item.get("exit_code")
        return item.get("aggregated_output") or "", bool(code) or item.get("status") == "failed"
    if kind == "mcptoolcall":
        if item.get("error"):
            return (item["error"] or {}).get("message") or "failed", True
        res = item.get("result") or {}
        return _texts(res.get("content")), bool(res.get("isError"))
    return None


MESSAGES = ("UserMessage", "AgentMessage", "Reasoning")


def _rows(entry):
    """The chat rows one line of a rollout file holds, in the shapes transcript.recent returns."""
    from .transcript import _short
    if entry.get("type") != "event_msg":
        return []
    payload = entry.get("payload") or {}
    if payload.get("type") != "item_completed":
        return []
    item = payload.get("item") or {}
    kind = item.get("type") or ""
    at = entry.get("timestamp")
    if kind == "UserMessage":
        text = _texts(item.get("content"))
        return [{"kind": "text", "who": "operator", "at": at, "text": text}] if text else []
    if kind == "AgentMessage":
        text = _texts(item.get("content"))
        return [{"kind": "text", "who": "agent", "at": at, "text": text}] if text else []
    if kind in MESSAGES:
        return []
    rows = [{"kind": "tool", "who": "agent", "at": at, "tool": _tool_name(kind, item),
             "id": item.get("id"), "input": _tool_input(kind, item)}]
    got = _tool_result(kind, item)
    if got is not None:
        rows.append({"kind": "result", "who": "agent", "at": at, "id": item.get("id"),
                     "error": got[1], "text": _short(got[0], limit=1200)})
    return rows


def recent(path, count):
    from .transcript import tail_lines
    # A rollout file writes each item twice (event_msg and response_item) plus usage rows, so
    # many more raw lines than rows are read.
    _, lines = tail_lines(path, max(count * 10, 80),
                          max_bytes=(8 << 20) if count <= 200 else (64 << 20))
    out = []
    for raw in lines:
        try:
            out.extend(_rows(json.loads(raw)))
        except Exception:
            continue
    return out[-count:]


def tool_call(path, tool_id):
    from .transcript import TOOL_MAX, _lines_with
    for raw in _lines_with(path, tool_id.encode(), want=4):
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        payload = entry.get("payload") or {}
        item = payload.get("item") or {}
        if payload.get("type") != "item_completed" or item.get("id") != tool_id:
            continue
        kind = item.get("type") or ""
        if kind.lower().replace("_", "") == "commandexecution":
            shown = _command(item)
        else:
            shown = json.dumps({k: v for k, v in item.items() if k not in ("id", "type")},
                               indent=2, ensure_ascii=False)
        found = {"tool": _tool_name(kind, item), "input": shown[:TOOL_MAX],
                 "at": entry.get("timestamp")}
        got = _tool_result(kind, item)
        if got is not None:
            found.update(result=got[0][:TOOL_MAX], error=got[1], cut=len(got[0]) > TOOL_MAX)
        return found
    return None


def read_entry(reader, entry):
    """One rollout line into transcript.Reader's counters: turns, last message, tokens."""
    kind = entry.get("type")
    payload = entry.get("payload") or {}
    ts = entry.get("timestamp")
    if kind == "turn_context":
        reader.codex_model = payload.get("model") or getattr(reader, "codex_model", None)
    if kind != "event_msg":
        return
    sub = payload.get("type")
    if sub == "item_completed":
        item = payload.get("item") or {}
        if item.get("type") == "AgentMessage":
            text = _texts(item.get("content"))
            if text:
                reader.turns += 1
                # A call reads the whole reply from here, as it does for a Claude agent.
                reader.said.append((reader.turns, text))
                reader.last_message = text
                reader.last_message_at = ts
        elif item.get("type") == "UserMessage":
            reader.last_user_at = ts
        elif item.get("type") not in MESSAGES:
            reader.tools_used += 1
    elif sub == "token_count":
        # Codex writes the running total for the conversation, so it replaces rather than adds.
        total = ((payload.get("info") or {}).get("total_token_usage")) or {}
        if total:
            cached = total.get("cached_input_tokens", 0) or 0
            model = getattr(reader, "codex_model", None) or "codex"
            reader.tokens = {model: {"input": (total.get("input_tokens", 0) or 0) - cached,
                                     "output": total.get("output_tokens", 0) or 0,
                                     "cache_write": total.get("cache_write_input_tokens", 0) or 0,
                                     "cache_read": cached}}


# -- the live stream ------------------------------------------------------------------


class CodexStream(Stream):
    """`codex exec --json` events, turned into the frames the console already reads.

    Codex sends each message whole, when it is finished, not word by word. So a message is one
    live start and one delta with all of its text. A tool is named when it starts, because the
    command is already there, and its end says nothing more the chat draws.
    """

    def __init__(self, on_event, on_thread=None):
        super().__init__(on_event)
        self.on_thread = on_thread
        self._named = set()
        self.turn_open = False
        # Whether this process said turn.started at all. A bad flag or a lost thread makes Codex
        # exit before that, with the reason on stderr only.
        self.turn_seen = False

    def handle(self, event):
        kind = event.get("type")
        if kind == "thread.started":
            tid = event.get("thread_id")
            if tid and tid != self.session_id:
                self.session_id = tid
                if self.on_thread:
                    self.on_thread(tid)
        elif kind == "turn.started":
            self.turn_open = True
            self.turn_seen = True
            self.set_state("thinking")
        elif kind in ("item.started", "item.updated"):
            self._item(event.get("item") or {}, done=False)
        elif kind == "item.completed":
            self._item(event.get("item") or {}, done=True)
        elif kind == "turn.completed":
            self.finish()
        elif kind == "turn.failed":
            said = ((event.get("error") or {}).get("message") or "").strip()
            self.emit({"t": "note", "text": "The Codex turn failed. " + said})
            self.finish()
        elif kind == "error":
            said = (event.get("message") or "").strip()
            if said:
                self.emit({"t": "note", "text": "Codex said: " + said})

    def _item(self, item, done):
        kind = item.get("type") or ""
        if kind == "agent_message":
            if done and (item.get("text") or "").strip():
                self.live = True
                self.live_text = item["text"]
                self.emit({"t": "live", "phase": "start"})
                self.emit({"t": "live", "phase": "delta", "text": self.live_text})
            return
        if kind in ("reasoning", "user_message"):
            # Reasoning is the model's private thinking. The Claude chat leaves it out as well.
            return
        if kind == "error":
            if done and item.get("message"):
                self.emit({"t": "note", "text": "Codex said: " + item["message"]})
            return
        rid = item.get("id")
        if rid in self._named:
            return
        self._named.add(rid)
        self.emit({"t": "tool", "name": _tool_name(kind, item),
                   "summary": _tool_input(kind, item)})

    def finish(self):
        """The turn is over and in the rollout file, however it ended. Said once."""
        self.set_state("idle")
        self.end_live()
        if self.turn_open:
            self.turn_open = False
            self._named.clear()
            self.emit({"t": "settled"})


class CodexEngine:
    """One Codex conversation for the chat tab, with a `codex exec` process per turn.

    It answers the same calls the broker makes on chat.ChatEngine, so the broker treats both the
    same. alive() means the chat is open, not that a process runs: between turns nothing does.
    """

    def __init__(self, agent_id, on_event, on_exit=None):
        rec = registry.get(agent_id) or {}
        self.agent_id = agent_id
        self.on_exit = on_exit
        self.permission_mode = rec.get("permission_mode")
        self.thread = rec.get("codex_thread")
        self.started = time.time()
        self.exited_at = None
        self.exit_status = None
        self.last_input = 0.0
        self.stderr = deque(maxlen=STDERR_LINES)
        self.stream = CodexStream(on_event, on_thread=self._thread)
        self.proc = None
        # True from the start of a turn until its reader has said the turn is over. The process
        # can be gone a moment before that, and a turn started in that moment would be settled
        # by the old reader.
        self._active = False
        self._queue = []
        self._lock = threading.RLock()

    def _thread(self, tid):
        self.thread = tid
        try:
            registry.update(self.agent_id, codex_thread=tid)
        except Exception:
            log.exception("%s: could not keep the Codex thread id", self.agent_id)

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    @property
    def state(self):
        return self.stream.state

    @property
    def live_text(self):
        return self.stream.live_text

    def alive(self):
        return self.exited_at is None

    def running(self):
        return self._active or (self.proc is not None and self.proc.poll() is None)

    def working(self):
        return self.alive() and (self.running() or bool(self._queue))

    def stderr_tail(self):
        return "\n".join(self.stderr).strip()

    def snapshot(self):
        return {"engine": "codex", "pid": self.pid, "alive": self.alive(),
                "working": self.working(), "state": self.stream.state,
                "started": self.started, "exit_status": self.exit_status}

    # -- turns ----------------------------------------------------------------

    def say(self, text):
        """Start a turn, or queue the message when a turn is already running.

        A Codex turn cannot take a second message while it runs, so the next one waits and goes
        in as the next turn, the same order Claude would read them in.
        """
        if not self.alive():
            return False
        with self._lock:
            if self.running() or self._queue:
                self._queue.append(text)
                self.last_input = time.time()
                return True
            # Thinking from the moment the message goes, as the Claude chat does. The model takes
            # seconds to say anything, and an idle chat for those seconds reads as a lost message.
            self.stream.set_state("thinking")
            ok = self._run(text)
            if not ok:
                self.stream.set_state("idle")
        if ok:
            self.last_input = time.time()
        return ok

    def _run(self, text):
        rec = registry.get(self.agent_id)
        if not rec:
            return False
        rec = dict(rec, permission_mode=self.permission_mode or rec.get("permission_mode"))
        argv = exec_argv(rec, self.thread)
        env = run_env(self.agent_id)
        try:
            proc = subprocess.Popen(argv, cwd=rec["cwd"], env=env, bufsize=0,
                                    start_new_session=True, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            self.stream.emit({"t": "note", "text": f"Codex did not start: {exc}"})
            return False
        try:
            proc.stdin.write(text.encode())
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        self.proc = proc
        self._active = True
        self.stream.turn_seen = False
        threading.Thread(target=self._read, args=(proc,), name=f"codex-{self.agent_id}",
                         daemon=True).start()
        threading.Thread(target=self._read_err, args=(proc,),
                         name=f"codex-err-{self.agent_id}", daemon=True).start()
        return True

    def _read(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            try:
                self.stream.feed(raw.decode("utf-8", "replace"))
            except Exception:
                log.exception("%s: a Codex event could not be read", self.agent_id)
        try:
            status = proc.wait(timeout=10)
        except Exception:
            status = None
        try:
            proc.stdout.close()
        except Exception:
            pass
        with self._lock:
            failed_early = bool(status) and not self.stream.turn_seen
            if (self.stream.turn_open or failed_early) and self.alive():
                # The process ended without turn.completed: a crash, a bad flag or an interrupt.
                self.stream.emit({"t": "note", "text": (f"Codex stopped ({status}). "
                                                        + self.stderr_tail()[-600:]).strip()})
            self.stream.finish()
            self._active = False
            # A message that arrived during the turn goes in now, as the next turn.
            nxt = "\n\n".join(self._queue) if self._queue and self.alive() else None
            self._queue = []
            if nxt:
                self.stream.set_state("thinking")
                if not self._run(nxt):
                    self.stream.set_state("idle")

    def _read_err(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            # "Reading additional input from stdin..." is Codex being polite, not a fault.
            if line and not line.startswith("Reading additional input"):
                self.stderr.append(line)
        # Closed here, by the thread that reads it, so no other thread closes it mid-read.
        try:
            proc.stderr.close()
        except Exception:
            pass

    # -- what the broker asks -------------------------------------------------

    def answer(self, request_id, response):
        # Codex in exec mode never asks, so there is nothing to answer.
        return False

    def pending_asks(self):
        return []

    def request(self, subtype, timeout=None, **fields):
        """The two control requests the broker sends, done the Codex way."""
        if subtype == "interrupt":
            with self._lock:
                self._queue = []
            self._kill()
            return {"subtype": "success"}
        if subtype == "set_permission_mode":
            # Read at the start of each turn, so the next turn runs in the new sandbox.
            self.permission_mode = fields.get("mode")
            return {"subtype": "success"}
        return None

    def _kill(self, force=False):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return True
        steps = ((signal.SIGKILL, 0.5),) if force else ((signal.SIGINT, 2.0),
                                                        (signal.SIGTERM, 2.0),
                                                        (signal.SIGKILL, 0.5))
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

    def terminate(self, force=False):
        """Close the chat. A turn in flight is stopped. Returns whether it is gone."""
        if self.exited_at is not None:
            return True
        with self._lock:
            self._queue = []
        gone = self._kill(force=force)
        self.exited_at = time.time()
        self.exit_status = self.proc.returncode if self.proc else None
        if self.on_exit:
            try:
                self.on_exit(self)
            except Exception:
                log.exception("%s: the Codex engine exit handler raised", self.agent_id)
        return gone
