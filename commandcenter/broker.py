"""Starting agents, waking them, and knowing which are awake.

The broker owns every terminal, and that ownership is what makes liveness free. When a process
dies its terminal closes, the reading thread sees the end of the stream, and the record changes
in the same instant. Nothing polls, and the agent is never asked to report in.

An agent is a conversation, not a process. So starting one means deciding what the conversation
will be called and then giving it a process to live in, and waking one means giving the same
conversation a new process. Claude Code takes --session-id for the first and --resume for the
second, which is the whole reason this works.

One conversation, two possible engines, one of them running at a time:

    terminal      the interactive Claude Code in a pseudo-terminal, shown byte for byte
    chat engine   the same conversation headless, `claude -p` with typed events, for the chat tab

Which one is running is the agent's mode. Both engines run with the agent's own cwd and its own
session id, so both write the same session file, and that file is what keeps the terminal and the
chat showing one conversation. Switching engines kills one before starting the other, never
overlapping them: `--resume` on a session that is already open starts a copy of it, and two
copies write two files, which is the one way to break the thing that joins them.
"""

import json
import shutil
import sys
import threading
import time

from pathlib import Path

from . import codex, config, logs, mailbox, registry
from .chat import ChatEngine
from .terminal import Terminal

log = logs.get(__name__)

_lock = threading.RLock()
_terminals = {}          # agent id -> Terminal
_chats = {}              # agent id -> ChatEngine
_modes = {}              # agent id -> "chat" | "terminal"; missing means "off"
_listeners = []          # called with (event name, payload) when anything changes
_watchers = {}           # agent id -> [fn], called with each frame for that agent
_due = set()             # agents with a check for urgent held messages already booked

MODES = ("chat", "terminal", "off")

# How long to wait for an engine to die before starting the other one. Five seconds is long for a
# process that has been asked to stop, and short enough that a button does not feel stuck.
CLOSE_WAIT = 5.0

# What the chat tab is told when a typed message closes a live terminal and the chat engine takes
# the conversation over. The person needs to know why the screen they were watching went away and
# why the first reply is slow, and the honest answer is short: same conversation, new engine, and
# the seconds it takes to read the conversation back.
SWAP_NOTE = ("The terminal was running, so it has been closed and the chat is taking over. The "
             "conversation is the same, the first reply is slower while it loads.")

# What the chat tab is told when the live terminal is mid task, which is the one time the message
# does not close it. Held rather than sent, so the words say held rather than sent.
BUSY_NOTE = ("The terminal is busy with a task, so it was not closed. The message is held and "
             "goes to the agent when the task ends.")
# The same, for a message sent without wake. Only an urgent one goes in at the task's end.
BUSY_NOTE_QUIET = ("The terminal is busy with a task, so it was not closed. The message is held "
                   "and goes to the agent with its next message.")

# The permission modes an agent may be put in from the browser. "manual" and "auto" are left out
# because neither was tested against the engine here, and an option that might quietly do nothing
# is worse than an option that is not offered. The engine acknowledges "manual" as "default",
# which is already a sign that the two words are not the same word.
PERMISSION_MODES = ("acceptEdits", "plan", "bypassPermissions")

# What the tool result says when somebody presses no. Claude reads this and tells the person what
# it was going to do, which is more use than silence.
DENIED = "The owner said no."

# What the tool result says when somebody skips a question. A question is not a request to do
# something, so "said no" would read as a refusal of an option. This tells Claude nobody chose, and
# it decides whether to go on without the answer or to stop and ask in words.
SKIPPED = "The owner chose not to answer. Go on without the answer, or ask in words."

# Changing the permission mode while a terminal is running changes the record, not the running
# process. Shift+Tab is the terminal's own way, and saying so is better than a switch that looks
# like it worked and did not.
TERMINAL_PERM_NOTE = ("Takes effect the next time this agent starts. In the terminal, Shift+Tab "
                      "changes it now.")

# The engine did not answer in three seconds, which means it is not reading its pipe. The record
# is written either way, so the change is real at the next start and not before.
NO_ACK_NOTE = ("The chat engine did not answer, so this takes effect the next time the agent "
               "starts.")

# An ask dies with the process that was waiting on it. The card goes, and this says why, because
# a card that vanishes on its own looks like an answer that was sent.
LOST_ASK_NOTE = "The engine stopped while it was waiting for an answer, so the question is gone."

# How long a turn must stay ended before an urgent held message goes in. A turn that ends and
# starts again inside this is one piece of work, and a message sent into the gap would race it.
SETTLE = 1.0

# How often a terminal that is still mid task is looked at again while an urgent message waits.
# The console feed sees a spinner stop only while somebody watches, so this is the turn's end
# when nobody does.
RECHECK = 5.0


def on_change(fn):
    _listeners.append(fn)


def _emit(event, payload):
    for fn in list(_listeners):
        try:
            fn(event, payload)
        except Exception:
            pass


# -- frames, for the browsers watching one agent ---------------------------------
#
# Separate from on_change above, deliberately. on_change tells the console at large that something
# about the fleet changed; these are one agent's own live traffic, and they go only to the sockets
# attached to that agent.


def listen(agent_id, fn):
    with _lock:
        _watchers.setdefault(agent_id, []).append(fn)


def unlisten(agent_id, fn):
    with _lock:
        fns = _watchers.get(agent_id) or []
        if fn in fns:
            fns.remove(fn)
        if not fns:
            _watchers.pop(agent_id, None)


def _frame(agent_id, obj):
    with _lock:
        fns = list(_watchers.get(agent_id) or [])
    for fn in fns:
        try:
            fn(obj)
        except Exception:
            # A browser that went away is not worth a stack trace. The socket drops itself and
            # unlistens on its way out.
            pass


def _chat_frame(agent_id, obj):
    """A chat engine's frame to the watchers, and an ask to the inbox as well.

    A new ask is a "now" item for the front desk, and an answered or lost one closes it. Called with no
    lock held, so the inbox can ask this module what is pending without a deadlock.
    """
    _frame(agent_id, obj)
    kind = obj.get("t") if isinstance(obj, dict) else None
    if kind == "state" and obj.get("state") == "idle":
        # A turn ended, so the agent is free. A task card waiting on that moment tells the owner.
        try:
            from . import tasks
            tasks.free(agent_id, sure=True)
            # A turn that answered a prompted offer: Jev reads it and moves the card.
            tasks.prompt_settled(agent_id)
        except Exception:
            log.exception("the task reminder for %s failed", agent_id)
        try:
            # Jev reads the reply for an ask of the owner that is on no list (askcheck.py).
            from . import askcheck, transcript
            rec = registry.get(agent_id)
            if rec and askcheck.enabled():
                askcheck.turn_done(agent_id, str(transcript.last_message(rec) or ""), "chat")
        except Exception:
            log.exception("the ask check for %s failed", agent_id)
        turn_ended(agent_id)
        return
    if kind not in ("ask", "ask_done"):
        return
    try:
        from . import inbox
        if kind == "ask":
            inbox.ask_opened(agent_id, obj)
        else:
            inbox.ask_closed(agent_id, obj.get("id"))
    except Exception:
        log.exception("the inbox did not take %s's ask", agent_id)


def get(agent_id):
    with _lock:
        return _terminals.get(agent_id)


def chat(agent_id):
    """The agent's chat engine, if one is running. None means chat mode with nobody typing yet."""
    with _lock:
        return _chats.get(agent_id)


def mode(agent_id):
    """Which engine is running: chat, terminal, or off.

    Off is the normal resting state, not a fault, and it is what every agent is after a daemon
    restart. The mode is a thing somebody chose in a browser this afternoon, so it is held in
    memory and never written to disk.
    """
    with _lock:
        return _modes.get(agent_id, "off")


def _set_mode(agent_id, want):
    """Record the mode and tell every attached browser, but only when it really changed."""
    with _lock:
        if _modes.get(agent_id, "off") == want:
            return False
        if want == "off":
            _modes.pop(agent_id, None)
        else:
            _modes[agent_id] = want
    _frame(agent_id, {"t": "mode", "mode": want})
    return True


def awake(agent_id):
    with _lock:
        engines = (_terminals.get(agent_id), _chats.get(agent_id))
    # Either engine counts. An agent answering in the chat tab holds its context in memory and
    # costs the same memory as one sitting at a terminal, so calling it grey would be wrong about
    # the only thing green means.
    return any(e and e.alive() for e in engines)


def state(agent_id):
    """green when the terminal is open, grey when it is closed. Nothing in between.

    Being online but idle is not a third state, deliberately. An agent sitting at a prompt is
    still holding its context in memory and can answer at once, which is what green means.
    """
    return "green" if awake(agent_id) else "grey"


# The tools an agent is given, written per agent because each one is told its own name and whether it
# is the master. Not `--strict-mcp-config`: that would ignore every other MCP server the operator has
# configured, and taking away their own tools to add ours is not a trade anybody asked for.
TOOLS = Path(__file__).resolve().parent / "tools.py"


def write_tools_config(rec, run=None):
    """Write the MCP configuration for one agent and return its path.

    The agent learns its own id from here rather than being told at every call, because a tool that
    has to be passed "who am I" can be passed somebody else. A job's fresh run gets a file of its
    own, `run` = {"job", "transcript"}, so what the run posts says which job and which transcript
    it came from.
    """
    folder = config.VAR / "mcp"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (f"{rec['id']}-run-{Path(run['transcript']).stem}.json" if run else f"{rec['id']}.json")
    env = {
        "PLATFORM_AGENT": rec["id"],
        "PLATFORM_ROLE": rec.get("role") or "agent",
        # Always loopback, never config.BIND: BIND is what the daemon listens on, which is every
        # interface, and an agent has no business dialling out to reach a process beside it.
        "PLATFORM_API": f"http://127.0.0.1:{config.PORT}",
        "PLATFORM_OWNER": config.OWNER,
        # A delegate's parent. Its tools leave out everything that reaches the owner.
        "PLATFORM_PARENT": rec.get("parent") or "",
    }
    if run:
        env["PLATFORM_JOB"] = run["job"]
        env["PLATFORM_TRANSCRIPT"] = run["transcript"]
    path.write_text(json.dumps({"mcpServers": {"platform": {
        "command": sys.executable or "python3",
        "args": [str(TOOLS)],
        "env": env,
    }}}, indent=2))
    return path


def argv_for(rec, resume, tools_config=None, engine="terminal"):
    """The command line for one agent, for whichever engine is about to run it.

    --session-id names a new conversation; --resume rejoins an existing one. They cannot both
    appear, and which one is correct is the only difference between starting and waking. It is
    the same decision for both engines: `claude -p --session-id <id>` starts a session under that
    id and `claude -p --resume <id>` keeps the same id, so the file on disk is the same file
    either way.

    Everything else is the same for both engines too, on purpose. An agent's model, its
    permission mode and its tools are what the agent is, not what the operator is looking at.
    """
    exe = shutil.which(config.CLAUDE_BIN) or config.CLAUDE_BIN
    argv = [exe]
    if engine == "chat":
        # stream-json both ways is the whole point: one process for the conversation, reading a
        # message at a time off stdin and answering in typed events instead of a drawn screen.
        # --verbose is what stream-json output needs, and --include-partial-messages is what
        # makes the deltas arrive while the reply is being written rather than at the end.
        argv += ["-p",
                 "--input-format", "stream-json",
                 "--output-format", "stream-json",
                 "--verbose", "--include-partial-messages",
                 # An ask travels on the same pipe as everything else, as a control_request,
                 # and is answered with a card in the browser. --permission-prompt-tool stdio is
                 # the flag that makes it happen and it is not optional: a whole afternoon went
                 # on watching a run with "host" alone, where the ask never came, the turn ended
                 # in seven seconds and the tool was simply refused.
                 #
                 # --allow-dangerously-skip-permissions does not skip anything by itself. It only
                 # lets the mode be set to bypassPermissions later; without it the engine answers
                 # "the session was not launched with --dangerously-skip-permissions" and the
                 # switch in the browser has one option it can never choose. The engine still
                 # starts in the record's own permission mode.
                 "--permission-prompts", "host",
                 "--permission-prompt-tool", "stdio",
                 "--allow-dangerously-skip-permissions"]
    if tools_config:
        argv += ["--mcp-config", str(tools_config)]
        # The platform's own tools do not stop to ask. Everything else still does, which is the
        # point: `acceptEdits` lets an agent write a file and asks before it runs a command, and
        # that stays.
        #
        # This was found by watching a real agent: it called list_agents and Claude Code drew a
        # permission dialog, so the master driving the fleet would need a keypress per call and a
        # 3am job using a tool would sit at a dialog until morning. Pre-approving is also honest
        # about what is already true, since an agent can reach the same HTTP interface with curl.
        argv += ["--allowedTools", "mcp__platform"]
    if resume:
        argv += ["--resume", rec["session_id"]]
    else:
        argv += ["--session-id", rec["session_id"]]
    if rec.get("model"):
        argv += ["--model", rec["model"]]
    # autocompact false on the record keeps Claude Code's own limit, near the full window. The owner
    # turned it off for the assistant and Juno on 15 October, until recall() can bring back what a
    # compaction drops.
    if config.AUTOCOMPACT and rec.get("autocompact") is not False:
        argv += ["--autocompact", config.AUTOCOMPACT]
    if rec.get("permission_mode"):
        argv += ["--permission-mode", rec["permission_mode"]]
    # A display name makes the agent identifiable in Claude Code's own session list, so the
    # two views of the same fleet agree.
    argv += ["--name", rec["id"]]
    if config.REMOTE_CONTROL and engine != "chat":
        # Named, so the phone lists agents rather than a row of hostnames. Interactive only:
        # Remote Control runs inside a session somebody can take over, and a headless process has
        # no screen for anybody to take.
        argv += ["--remote-control", rec["id"]]
    return argv


def trust_folder(cwd, state=None):
    """Mark an agent's folder as trusted in Claude Code's own state file, ~/.claude.json.

    Claude Code asks "Do you trust this folder?" the first time it opens a folder, and the answer
    it starts on is "No, exit". The broker types the first message into the terminal, so on a new
    folder that keypress chose "No" and the agent died at once with exit status 1. Creating an
    agent in a folder is already the owner saying they trust it, so the question is answered here.

    Only the one flag is set, and only when it is missing. The file is replaced in one rename, so
    Claude Code never reads half of it. Any failure is logged and the terminal starts anyway: the
    worst case is the old question on the screen.
    """
    path = Path(state) if state else config.CLAUDE_STATE
    folder = str(Path(cwd).expanduser().resolve())
    try:
        held = json.loads(path.read_text()) if path.exists() else {}
        project = held.setdefault("projects", {}).setdefault(folder, {})
        if project.get("hasTrustDialogAccepted"):
            return False
        project["hasTrustDialogAccepted"] = True
        tmp = path.with_name(path.name + ".ergo-tmp")
        tmp.write_text(json.dumps(held, indent=2))
        tmp.replace(path)
        return True
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("could not mark %s as trusted in %s: %s", folder, path, exc)
        return False


def start(agent_id):
    """Open a terminal for an agent. Resumes its conversation if it has had one before.

    Whether this is a first start or a wake is worked out from the conversation on disk rather
    than from a flag, because a flag can disagree with reality after a reboot and the file
    cannot.
    """
    rec = registry.get(agent_id)
    if not rec:
        raise KeyError(agent_id)

    if rec.get("unix_user"):
        # The quarantined agent holds credentials and runs as its own Unix user. Starting it
        # from here would need a password the console does not have, and passwordless sudo
        # would make the quarantine decorative. So it starts by hand in its own session and
        # registers itself.
        raise PermissionError(
            f"{agent_id} runs as {rec['unix_user']} and must be started in that user's own "
            "session, then registered with POST /api/agents/register")
    with _lock:
        existing = _terminals.get(agent_id)
        if existing and existing.alive():
            return existing
        # One engine at a time, whichever path asked for this one. A wake from a card while the
        # chat engine is answering would otherwise put two claude processes on one session id,
        # and the second one starts a copy of the conversation rather than joining it. The wait
        # happens with the lock held, which is right here: a second terminal must not be spawned
        # while the first engine is still dying.
        leaving = _chats.pop(agent_id, None)
        if leaving and leaving.alive():
            _wait_gone(leaving)

        from . import opencode, transcript
        if opencode.is_opencode(rec):
            # OpenCode keeps its own session id. Until the first turn has made one there is
            # nothing to resume, and the discovery thread below fills it in.
            resume = bool(opencode.session_id(rec))
            env, _server = opencode.env_for(rec)
            term = Terminal(agent_id, opencode.argv_for(rec, resume, engine="terminal"),
                            rec["cwd"], env=env, on_exit=_died)
        else:
            resume = transcript.session_file(rec) is not None
            # Rewritten on every wake, so an agent promoted to master gets the fleet tools next
            # time it wakes rather than at some point nobody can name.
            if codex.is_codex(rec):
                # A Codex agent's terminal is the Codex interface, on its own conversation.
                argv = codex.terminal_argv(rec)
            else:
                trust_folder(rec["cwd"])
                argv = argv_for(rec, resume, write_tools_config(rec))
            term = Terminal(agent_id, argv, rec["cwd"], on_exit=_died)
        _terminals[agent_id] = term

    # A terminal is the mode, not merely a process in it. Waking an agent from a card, from cron
    # or from a cord therefore puts it in terminal mode, and every browser watching is told.
    _set_mode(agent_id, "terminal")
    registry.touch(agent_id)
    if opencode.is_opencode(rec) and not opencode.session_id(rec):
        # The terminal has no event stream to carry the new session id back, so it is read from
        # OpenCode's own database once the first turn has made one. Without it a later chat would
        # start a second conversation beside the terminal's.
        threading.Thread(target=_discover_opencode, args=(agent_id, term),
                         name=f"oc-discover-{agent_id}", daemon=True).start()
    log.info("%s awake, pid %s, %s", agent_id, term.pid,
             "resumed its conversation" if resume else "new conversation")
    _emit("agent.awake", {"agent": agent_id, "resumed": resume, "pid": term.pid})

    # Anything left while it was asleep is delivered now, on its own thread, because waiting for a
    # session to load its conversation takes seconds and nothing should block on that.
    if mailbox.waiting(agent_id, everything=True):
        threading.Thread(target=_deliver_held, args=(agent_id, term),
                         name=f"mail-{agent_id}", daemon=True).start()
    return term


def _deliver_held(agent_id, term):
    if not _wait_for_prompt(term, cap=45.0):
        return
    msgs = mailbox.take(agent_id)
    prompt = mailbox.merged(msgs)
    if not prompt:
        return
    if term.submit(prompt):
        registry.touch(agent_id)
        _emit("agent.message", {"agent": agent_id, "delivered": len(msgs)})
    else:
        # Put them back rather than lose them. A failed delivery is a delay; a dropped message is
        # The owner wondering why the agent ignored them.
        _put_back(agent_id, msgs)


def _discover_opencode(agent_id, term):
    """Keep the session id an interactive OpenCode terminal made.

    Polled rather than parsed. The terminal draws a screen, not an event stream, and the id is
    already in OpenCode's database; reading it there is the same source the transcript uses.
    """
    from . import opencode
    sid = opencode.discover_session(term.cwd, term.started, cap=900.0,
                                    alive=lambda: term.alive())
    if not sid:
        return
    rec = registry.get(agent_id)
    if rec and not rec.get("opencode_session"):
        registry.update(agent_id, opencode_session=sid)
        log.info("%s OpenCode session %s kept", agent_id, sid)


def _keep_session(agent_id, sid):
    """Write down the session id OpenCode made, once.

    Every event carries it, and the first turn alone emits dozens, so the record is read before it
    is written. Only a change is worth a write and a log line.
    """
    if not sid:
        return
    rec = registry.get(agent_id)
    if rec and rec.get("opencode_session") != sid:
        registry.update(agent_id, opencode_session=sid)
        log.info("%s OpenCode session %s kept", agent_id, sid)


def _died(term):
    log.info("%s terminal closed, exit status %s", term.agent_id, term.exit_status)
    # The terminal is the mode, so losing it is leaving the mode. Switching to chat kills the
    # terminal on purpose and sets the mode itself, and this is a no-op in that case, because a
    # mode that is already chat does not change.
    with _lock:
        ours = _terminals.get(term.agent_id) is term
        if ours:
            # Dropped here, not only when something replaces it. A dead terminal left in the
            # dictionary is a record that disagrees with the world, and since a switch to chat no
            # longer clears it, that record could sit in front of a live chat engine and be handed
            # to a cord waiting for the reply. The broker owns the terminal, so the record goes
            # when the process does.
            _terminals.pop(term.agent_id, None)
    if ours and mode(term.agent_id) == "terminal":
        _set_mode(term.agent_id, "off")
    rec = registry.get(term.agent_id)
    if codex.is_codex(rec) and not rec.get("codex_thread"):
        # The Codex interface named a new conversation and did not tell us. Find it and keep it,
        # so the chat and the next terminal go on with it.
        try:
            codex.adopt(rec, since=term.started)
        except Exception:
            log.exception("%s: could not find the Codex conversation", term.agent_id)
    _emit("agent.asleep", {"agent": term.agent_id, "exit_status": term.exit_status})


# -- the chat engine -------------------------------------------------------------


def start_chat(agent_id):
    """Open a chat engine for an agent, unless one is already running.

    Lazily called, from send(), and from nowhere else. An agent in chat mode with nobody typing
    runs no process at all, which is the same bargain the terminal makes: grey costs nothing.
    """
    rec = registry.get(agent_id)
    if not rec:
        raise KeyError(agent_id)
    if rec.get("unix_user"):
        # The same quarantine as start(). A headless process is still a process owned by another
        # Unix user, and the console still has no password for it.
        raise PermissionError(
            f"{agent_id} runs as {rec['unix_user']} and must be started in that user's own "
            "session, then registered with POST /api/agents/register")

    with _lock:
        existing = _chats.get(agent_id)
        if existing and existing.alive():
            return existing

        if codex.is_codex(rec):
            # One codex exec per turn, resumed on the thread the record holds. See codex.py.
            engine = codex.CodexEngine(
                agent_id, on_event=lambda frame, aid=agent_id: _chat_frame(aid, frame),
                on_exit=_chat_died)
            _chats[agent_id] = engine
            registry.touch(agent_id)
            log.info("%s Codex chat open, thread %s", agent_id, engine.thread or "new")
            _emit("agent.awake", {"agent": agent_id, "resumed": bool(engine.thread),
                                  "pid": None})
            return engine

        from . import opencode, transcript
        if opencode.is_opencode(rec):
            # OpenCode's id is the one it made, not one this daemon chose. The first turn's events
            # carry it; _keep_session writes it down so the next process continues the same one.
            resume = bool(opencode.session_id(rec))
            env, _server = opencode.env_for(rec)
            engine = opencode.OpenCodeEngine(
                agent_id, opencode.argv_for(rec, resume, engine="chat"),
                # The agent's own folder, exactly as the terminal gets it.
                rec["cwd"],
                on_event=lambda frame, aid=agent_id: _chat_frame(aid, frame),
                on_exit=_chat_died,
                permission_mode=rec.get("permission_mode"),
                on_session=lambda sid, aid=agent_id: _keep_session(aid, sid),
                env=env)
        else:
            # The same decision the terminal makes, from the same evidence: the file on disk. It
            # has to be the file rather than a flag, because resuming an id that was never written
            # out makes claude exit with "No conversation found", and the chat stops answering.
            resume = transcript.session_file(rec) is not None
            tools_config = write_tools_config(rec)
            engine = ChatEngine(agent_id, argv_for(rec, resume, tools_config, engine="chat"),
                                # The agent's own folder, exactly as the terminal gets it. This
                                # is the line that keeps both engines writing one session file.
                                rec["cwd"],
                                on_event=lambda frame, aid=agent_id: _chat_frame(aid, frame),
                                on_exit=_chat_died,
                                permission_mode=rec.get("permission_mode"))
        _chats[agent_id] = engine

    registry.touch(agent_id)
    log.info("%s chat engine up, pid %s, %s", agent_id, engine.pid,
             "resumed its conversation" if resume else "new conversation")
    _emit("agent.awake", {"agent": agent_id, "resumed": resume, "pid": engine.pid})
    return engine


def _chat_died(engine):
    agent_id = engine.agent_id
    with _lock:
        ours = _chats.get(agent_id) is engine
        if ours:
            _chats.pop(agent_id, None)
    if not ours:
        return
    log.info("%s chat engine closed, exit status %s", agent_id, engine.exit_status)

    tail = engine.stderr_tail()
    if "No conversation found" in tail:
        # This should be impossible: resume is decided by the file existing, not by a flag. If it
        # ever happens the chat looks like an agent that went quiet, so it is said loudly here and
        # said again in the browser.
        log.error("%s could not resume its conversation: %s", agent_id, tail)
        note = ("The chat engine could not find this conversation, so it stopped. "
                "Its answer was: " + tail)
    elif engine.exit_status:
        note = f"The chat engine exited ({engine.exit_status}). " + tail
    else:
        note = "The chat engine exited. The next message starts it again."

    # A turn that was in flight ends here, whatever the reason. Leaving the live message open
    # would leave half a sentence on the screen for ever.
    engine.stream.set_state("idle")
    engine.stream.end_live()
    # And so does anything Claude was waiting on. The card has a yes and a no and neither of them
    # can reach a process that has gone, so it is taken off the screen and the loss is said once.
    if engine.stream.drop_asks():
        _frame(agent_id, {"t": "note", "text": LOST_ASK_NOTE})
    _frame(agent_id, {"t": "note", "text": note.strip()})
    _emit("agent.asleep", {"agent": agent_id, "exit_status": engine.exit_status})


def _wait_gone(engine, cap=CLOSE_WAIT):
    """End an engine and wait for the process to really be gone. Returns whether it is.

    The wait is the point. `--resume` on a session that is still open starts a copy of it and
    says so, and a copy writes its own file, which is exactly the thing that would stop the
    terminal and the chat from showing one conversation. So the old engine must be dead before
    the new one is spawned, not merely asked to stop.
    """
    engine.terminate()
    deadline = time.time() + cap
    while time.time() < deadline:
        if not engine.alive():
            return True
        time.sleep(0.05)
    engine.terminate(force=True)
    return not engine.alive()


def switch(agent_id, want):
    """Put an agent in chat mode or terminal mode. Returns the mode it is in now.

    A tab switch must never start or stop an agent. Looking is not deciding. Waking costs memory
    and tokens, so a switch does not wake a sleeping agent, and a running agent is somebody's work
    in progress, so a switch does not end one either. A message is a decision. A tab is not.

    The second half of that rule was bought with a live agent. The console posts the mode whenever
    a tab opens, so opening the Chat tab on an awake agent killed its terminal and started nothing
    in its place, because the chat engine is lazy and waits for a message. Going back to the
    Terminal tab then found nothing alive and, by the first half of the rule, refused to wake it.
    So merely looking at the Chat tab destroyed a running terminal, and the Terminal tab went grey
    and offered to wake the agent, which is the console telling somebody their session is gone and
    asking if they would like to start again.

    So chat now closes nothing at all. A live terminal keeps running behind the chat tab until
    somebody types, and send() is what closes it. Terminal is the one direction a switch swaps: a
    live chat engine is ended and a pty takes its place, because the Terminal tab is blank without
    one and the person asked to see it. When nothing is running, both directions start nothing,
    and the agent stays grey.
    """
    if want not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    rec = registry.get(agent_id)
    if not rec:
        raise KeyError(agent_id)

    # Recorded first, so every browser watching hears the mode at once instead of after a process
    # has been spawned and waited for.
    _set_mode(agent_id, want)

    if want == "terminal":
        with _lock:
            engine = _chats.get(agent_id)
            term = _terminals.get(agent_id)
        # The only swap a switch is allowed to make, and start() is what makes it: it takes the
        # chat engine out of the dictionary and waits for the process to really be gone before it
        # spawns the pty. That wait is not optional. Two claude processes on one session id write
        # two files, and one session file is the only thing that keeps the terminal and the chat
        # showing one conversation.
        #
        # Nothing alive means nothing starts, which is the sleeping agent staying asleep. A
        # terminal that is already running is already what was asked for.
        if engine and engine.alive() and not (term and term.alive()):
            start(agent_id)
    return mode(agent_id)


def stop(agent_id):
    """Halt the current task without shutting the agent down.

    Escape is the terminal's own interrupt. The chat engine has its own too, on the same pipe as
    everything else, and it halts the turn while the process keeps running: the answer comes back
    as a result row and the engine takes the next message normally. Ending the process is the
    fallback and only that, because a restart costs the ten seconds of loading the conversation
    again. The mode is left alone in both cases, because halting a task is not leaving the tab it
    was typed in.
    """
    term = get(agent_id)
    if term and term.alive():
        return bool(term.interrupt())
    engine = chat(agent_id)
    if not engine:
        return False
    reply = engine.request("interrupt")
    if reply and reply.get("subtype") == "success":
        return True
    # No acknowledgement means the process is not listening, so it goes, which is what slice one
    # did every time.
    return bool(_wait_gone(engine))


# -- answering what Claude asks ---------------------------------------------------
#
# Claude stops and asks before it runs something its permission mode does not cover, and it waits
# for the answer without a time limit. That wait is deliberate: the operator's own words were to
# let it stay until I say yes. A phone that is in a pocket for an hour finds the card still there.


def answer(agent_id, request_id, allow=None, answers=None):
    """Answer one ask. Returns whether it was answered.

    False means the id is unknown, which is a card the browser is still drawing for an ask that
    was already answered by somebody else, or lost with the engine.
    """
    engine = chat(agent_id)
    if not (engine and engine.alive()):
        return False
    held = engine.stream.ask(request_id)
    if held is None:
        return False
    if answers is not None:
        # A question ask. updatedInput is the whole input as it arrived plus the answers, keyed by
        # the exact question text. Sending the answers alone is not enough: the questions have to
        # come back with them.
        said = dict(held["input"])
        said["answers"] = dict(answers)
        response = {"behavior": "allow", "updatedInput": said}
    elif allow:
        # The input goes back unchanged. Claude accepts an allow with no updatedInput as well, and
        # returning what it sent is the honest form of "yes, exactly that".
        response = {"behavior": "allow", "updatedInput": held["input"]}
    else:
        # No on a question is a skip. It is still a deny of the tool use, the same mechanism the
        # terminal uses when you press Escape on a question, only with words that fit.
        said = SKIPPED if held["kind"] == "question" else DENIED
        response = {"behavior": "deny", "message": said}
    return engine.answer(request_id, response)


def pending_asks(agent_id):
    """Every ask still waiting for this agent, as the frames that were already sent.

    This is what a page that reconnects gets, so a phone that locked mid question finds the card
    where it left it.
    """
    engine = chat(agent_id)
    return engine.pending_asks() if engine and engine.alive() else []


def permission_mode(agent_id):
    """What Claude may do without asking. The live engine's answer, or the record's.

    They differ for as long as somebody has changed the mode of a running engine, because the
    change reaches the process at once and the record is what the next start will use.
    """
    engine = chat(agent_id)
    if engine and engine.alive() and engine.permission_mode:
        return engine.permission_mode
    rec = registry.get(agent_id) or {}
    return rec.get("permission_mode")


def set_permission_mode(agent_id, mode):
    """Change what Claude may do without asking. Returns the mode in force now.

    The record is always written, so the next start is what was asked for whatever else happens.
    A live chat engine takes the change without restarting, which is the whole point: an agent
    stopped at a question can be given a freer hand and carry on, instead of losing the turn.
    """
    if mode not in PERMISSION_MODES:
        raise ValueError(f"permission mode must be one of {', '.join(PERMISSION_MODES)}")
    if not registry.get(agent_id):
        raise KeyError(agent_id)
    registry.update(agent_id, permission_mode=mode)

    engine = chat(agent_id)
    if engine and engine.alive():
        reply = engine.request("set_permission_mode", mode=mode)
        if reply and reply.get("subtype") == "success":
            engine.permission_mode = mode
            _frame(agent_id, {"t": "perm_mode", "mode": mode})
        else:
            # The record has it and the running process does not, so the browser is told the old
            # mode rather than a new one that is not true yet.
            _frame(agent_id, {"t": "note", "text": NO_ACK_NOTE})
    else:
        term = get(agent_id)
        if term and term.alive():
            # A pseudo-terminal has no pipe to ask on. Claude Code's own keystroke does, and the
            # person is sitting in front of it.
            _frame(agent_id, {"t": "note", "text": TERMINAL_PERM_NOTE})
    return permission_mode(agent_id)


def sleep(agent_id, force=False):
    """Close whichever engine is running. The conversation stays on disk and can be resumed.

    This frees the memory an idle agent was holding, which is the point: a fleet of green
    agents on one laptop is the thing that runs it out of memory.
    """
    with _lock:
        term = _terminals.get(agent_id)
        engine = _chats.get(agent_id)
    if not (term or engine):
        return False
    if term:
        term.terminate(force=force)
    if engine:
        engine.terminate(force=force)
    _set_mode(agent_id, "off")
    return True


def send(agent_id, text, wake=False):
    """Give an agent a message. Returns "sent", "held", or False.

    An asleep agent is not woken by default, and that default is the useful one. Waking costs
    memory and ten seconds of loading, and grey exists precisely so an agent can sit there costing
    nothing. So the message waits in the mailbox and arrives when the agent next wakes, which is
    what makes a quick note from the dashboard cheap.

    Pass wake=True when the message is the reason to wake it, as a cord does: a cord needs an
    answer now, and holding one would leave the cord waiting for ever.
    """
    term = get(agent_id)
    live = bool(term and term.alive())

    # Chat mode always uses the chat engine. Chat goes to `claude -p`, terminal is the terminal,
    # and that boundary holds even when the other engine is the one that happens to be running.
    #
    # It used to route by what was alive instead, and that was wrong in a way the chat tab could
    # not show. The owner typed a question into the Chat tab of an agent that was awake in a
    # terminal. The message went down the pty, Claude drew the question on a screen nobody was
    # looking at, and the chat showed a note and then silence. Cards are the chat engine's own
    # pipe, so the permission card and the question card never appeared at all. The daemon
    # restores agents awake when it starts, so most agents have a terminal most of the time, and
    # the effect was a chat box that had quietly become a remote control for a screen.
    #
    # So a typed message now ends the terminal and the chat engine takes the conversation over.
    # Nothing said is lost: both engines write one session file, and the chat reads its history
    # from that file. What goes is the pty, and the person is told so.
    #
    # This belongs here and nowhere else. Typing is a person deciding. Opening a tab is not, which
    # is why switch() still starts nothing and stops nothing.
    # An OpenCode agent always talks through the chat path. Its terminal is OpenCode's screen
    # app, and text typed into that screen never reached the agent's session (Aadarsh's Loki,
    # 26 Oct: every message sat at "sending", then "not in the record yet"). One `opencode run`
    # per message lands in the session every time.
    from . import opencode
    if mode(agent_id) == "chat" or opencode.is_opencode(registry.get(agent_id)):
        if live and term.working():
            # The one case that does not swap. A terminal running a command has work in it, and
            # killing it throws that work away, which is a different thing from closing an idle
            # one. An agent stopped at a question in the interface reads as not working, because
            # silence is the readiness signal, so that one is swapped and the unanswered question
            # goes with it. That is meant: the question was never answered, so nothing is lost but
            # the asking.
            mailbox.hold(agent_id, text, urgent=wake)
            registry.touch(agent_id)
            _frame(agent_id, {"t": "note", "text": BUSY_NOTE if wake else BUSY_NOTE_QUIET})
            _emit("agent.held", {"agent": agent_id, "waiting": mailbox.waiting(agent_id)})
            # Booked now as well as at the turn's end. Busy is read from output in the last
            # second, so a redraw can look like work, and then no turn ends to send it.
            turn_ended(agent_id)
            return "held"
        if live:
            # Said before the wait, so the person reads why while it happens rather than after.
            _frame(agent_id, {"t": "note", "text": SWAP_NOTE})
            # Dropped from the record first, the same as start() drops the chat engine, so nothing
            # asking which engine an agent has is handed a terminal that is on its way out.
            with _lock:
                _terminals.pop(agent_id, None)
            # The wait is not optional. `--resume` on a session another process is still holding
            # starts a copy of the conversation, and two copies write two files, which is the one
            # way to break the thing that keeps the terminal and the chat showing one thing.
            _wait_gone(term)
        # The common case is still the plain one: agents are grey most of the time, and the
        # message is what spawns the chat engine.
        return _send_chat(agent_id, text)

    # The terminal path. A wake here opens a terminal rather than a chat engine, and it still
    # does. The chat engine can now ask, so a Bash call is no longer quietly refused, but an ask
    # needs somebody looking at a browser to answer it. A 3am job in chat mode would stop at the
    # first ask and wait until morning, and a terminal in acceptEdits does not. Flipping the
    # default is a decision about unattended work, not about permissions any more.
    if not live:
        if not wake:
            mailbox.hold(agent_id, text)
            registry.touch(agent_id)
            _emit("agent.held", {"agent": agent_id, "waiting": mailbox.waiting(agent_id)})
            return "held"
        term = start(agent_id)
    # Always, not only after waking. A resumed session takes seconds to load its conversation and
    # draw a prompt, and typing into it before then loses the message. But so does typing into a
    # terminal that is redrawing for any other reason, and this was found the hard way: a transfer
    # cord to an agent woken half a minute earlier reported "sent" and the text never appeared on
    # its screen. The same text sent later arrived.
    #
    # It costs almost nothing when there is nothing to wait for. A terminal that has been quiet
    # returns at once, because silence is the readiness signal.
    _wait_for_prompt(term)
    full, held = _with_held(agent_id, text)
    ok = term.submit(full)
    if ok:
        registry.touch(agent_id)
        _emit("agent.message", {"agent": agent_id})
    else:
        _put_back(agent_id, held)
    return "sent" if ok else False


def leave(agent_id, text):
    """Leave a message that waits for the agent's next turn, awake or asleep. Never wakes it.

    For news that needs no answer now, like a fresh job's result. Typing it into an idle terminal
    or a chat engine would start a turn, and a turn on a cold cache rereads the whole conversation:
    the cost the fresh run was there to avoid. So it waits in the mailbox, and goes in with the
    next message the agent gets, or when it next wakes.
    """
    mailbox.hold(agent_id, text, quiet=True)
    _emit("agent.held", {"agent": agent_id, "waiting": mailbox.waiting(agent_id)})


def _with_held(agent_id, text):
    """The message with anything waiting in the mailbox put in front of it, oldest first."""
    held = mailbox.take(agent_id)
    if not held:
        return text, []
    if len(held) == 1:
        before = "[A message that arrived before this one]\n\n" + held[0]["text"]
    else:
        before = mailbox.merged(held)
    return f"{before}\n\n[Now]\n\n{text}", held


def _put_back(agent_id, held):
    # A failed delivery is a delay; a dropped message is the agent never hearing it.
    for m in held:
        mailbox.hold(agent_id, m["text"], quiet=m.get("quiet", False), ts=m.get("ts"),
                     urgent=m.get("urgent", False))


def turn_ended(agent_id):
    """An agent's turn ended. If an urgent message waits, send it as the next turn.

    The owner, 19 October 2026: "Some things are time sensitive and should not wait for my prompt."
    A message sent with wake to an agent mid task is held, and before this it went in only with
    his next message, hours later for a task report or an alarm check. Quiet ones still wait.

    The check runs SETTLE seconds later, once per agent however often this is called, so a turn
    that ends and starts again at once is not raced.
    """
    _book(agent_id, SETTLE)


def _book(agent_id, delay):
    if not agent_id or not mailbox.urgent(agent_id):
        return
    with _lock:
        if agent_id in _due:
            return
        _due.add(agent_id)
    timer = threading.Timer(delay, _send_urgent, args=(agent_id,))
    timer.daemon = True
    timer.start()


def _send_urgent(agent_id):
    with _lock:
        _due.discard(agent_id)
        engines = (_terminals.get(agent_id), _chats.get(agent_id))
    # An agent that fell asleep gets everything when it next wakes, as before.
    if not awake(agent_id):
        return
    # A new turn began, so this waits for its end. A chat engine always says when a turn ends.
    # A terminal says so only through the console feed, so it is looked at again as well.
    if any(e and e.working() for e in engines):
        _book(agent_id, RECHECK)
        return
    if not mailbox.urgent(agent_id):
        return
    # Taken in one step, so the owner's own message at the same instant either takes them first and
    # this finds none, or finds none itself and goes alone. Either way each goes in once.
    msgs = mailbox.take(agent_id)
    prompt = mailbox.merged(msgs)
    if not prompt:
        return
    outcome = send(agent_id, prompt, wake=True)
    if not outcome:
        _put_back(agent_id, msgs)
    # "held" means a turn started in between, and send held the prompt again as urgent.


def _send_chat(agent_id, text):
    """The chat path. Spawns the engine if the first message of the day is this one.

    Nothing is held here and nothing waits for a prompt. Both of those exist because a terminal
    is a screen being drawn: a message typed into a redrawing interface is lost, and a message
    typed at a sleeping agent has nowhere to land. A chat engine has neither problem. It reads a
    line off stdin whenever it arrives, and starting it is what the message does.
    """
    engine = start_chat(agent_id)
    # The line fed on stdin is never echoed back on stdout, so nothing in the stream says what
    # the operator typed. The transcript has it a moment later; until then this frame is what the
    # browser draws, and a message that appears the instant it is sent is the whole feel of a chat.
    _frame(agent_id, {"t": "echo", "text": text})
    full, held = _with_held(agent_id, text)
    if not engine.say(full):
        _put_back(agent_id, held)
        return False
    registry.touch(agent_id)
    _emit("agent.message", {"agent": agent_id})
    return "sent"


def _wait_for_prompt(term, cap=None, quiet=None):
    """Wait for the interface to draw itself and go quiet, then give up and send anyway.

    Silence is the readiness signal, because the interface animates while it is busy. The cap
    matters as much as the signal: a startup that keeps painting, or a question waiting for a
    keypress, would otherwise hold the message forever. Sending into a not-quite-ready prompt
    is recoverable and visible in the terminal; waiting for ever is neither.
    """
    cap = config.PROMPT_WAIT if cap is None else cap
    quiet = config.PROMPT_QUIET if quiet is None else quiet
    deadline = time.time() + cap
    while time.time() < deadline:
        if not term.alive():
            return False
        # No output at all means the process has not drawn its interface yet, which is exactly when
        # typing would be lost. So that waits, and the cap is what stops it waiting for ever.
        if term.last_output and (time.time() - term.last_output) > quiet:
            return True
        time.sleep(0.1)
    return term.alive()


def snapshot():
    """One row per running engine. Only one engine runs per agent, so one row per agent."""
    with _lock:
        rows = {aid: t.snapshot() for aid, t in _terminals.items() if t.alive()}
        for aid, engine in _chats.items():
            if engine.alive() and aid not in rows:
                rows[aid] = engine.snapshot()
        return rows


def restore(limit=None):
    """Wake the agents that were awake when the daemon last stopped. Answer 19.

    Waking is not free: each agent costs 400 to 700 MB, so no more than `limit` are woken at once,
    the same number the scheduler uses. They are woken in a thread each because a resumed session
    takes seconds to read its own conversation back, and four of those in series is a minute before
    the console looks right.
    """
    from . import status

    # Not OpenCode agents: their chat engine starts on the first message, and a terminal woken
    # here would only be closed again by it.
    from . import opencode
    want = [a for a in status.were_awake() if registry.get(a) and not awake(a)
            and not opencode.is_opencode(registry.get(a))]
    if not want:
        return []
    limit = int(limit or config.WAKE_AT_ONCE)
    gate = threading.Semaphore(limit)
    log.info("restoring %d agent(s) that were awake: %s", len(want), ", ".join(want))

    def one(agent_id):
        with gate:
            try:
                start(agent_id)
            except Exception:
                # A folder that has been deleted, or an agent that now runs as another user. It
                # stays grey and the console says so, which is better than refusing to start.
                log.exception("could not restore %s", agent_id)

    for agent_id in want:
        threading.Thread(target=one, args=(agent_id,), name=f"restore-{agent_id}",
                         daemon=True).start()
    return want


def shutdown_all(force=False):
    with _lock:
        ids = set(_terminals) | set(_chats)
    for aid in ids:
        sleep(aid, force=force)


def reset():
    """Drop every engine, mode and listener without stopping anything. For tests only.

    Deliberately does not terminate: a test never had a real terminal, and a test that could reach a
    real one should not be able to end it by tidying up.
    """
    with _lock:
        _terminals.clear()
        _chats.clear()
        _modes.clear()
        _watchers.clear()
        _due.clear()
    _listeners.clear()
