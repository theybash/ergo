"""Cords: how one agent's work reaches another.

Two kinds, drawn by hand between two cards.

    transfer   carries the sending agent's last message plus an instruction typed onto the
               cord. One use, then the cord is gone. The receiver cannot refuse.
    call       a bounded conversation. The caller hangs up when it has what it wanted, or the
               budget runs out.

The direction is drawn, so who starts is never ambiguous. An earlier design had push and pull
as separate things; a drawn direction makes them the same thing and three verbs became two.

Nothing here is clever. Asking an agent something means typing into its terminal and waiting
for the session file to gain a reply, which is the same pair of channels the console uses for
everything else.
"""

import json
import re
import threading
import time
import uuid

from . import broker, config, logs, registry, transcript, untrusted

log = logs.get(__name__)

_lock = threading.RLock()
_cords = {}
_listeners = []

# The caller ends a call by writing this at the start of a line, followed by what it learned.
# A tool would be better and needs an MCP server; this is the same contract in plain text and
# can be replaced without changing anything else here.
HANGUP = re.compile(r"^\s*HANGUP\s*:\s*(.*)$", re.MULTILINE | re.IGNORECASE)


def on_change(fn):
    _listeners.append(fn)


def _emit(event, payload):
    for fn in list(_listeners):
        try:
            fn(event, payload)
        except Exception:
            pass


def _log(entry):
    """Every transfer is appended to a file. The console shows the last few; this keeps all."""
    config.ensure_dirs()
    with open(config.TRANSFER_LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def all():
    with _lock:
        return [dict(c) for c in _cords.values()]


def get(cord_id):
    with _lock:
        c = _cords.get(cord_id)
        return dict(c) if c else None


# -- the one primitive -----------------------------------------------------------

def ask(agent_id, prompt, timeout=300.0, working_limit=None, whole=False):
    """Give an agent a message and wait for its reply. Returns the text, or nothing.

    The reply is read from the session file rather than the terminal, because the terminal has
    it wrapped inside a redrawn box and the file has it as text. Waiting is done by watching the
    turn count rise, which is unambiguous: a new turn means the agent finished speaking.
    """
    rec = registry.get(agent_id)
    if not rec:
        return None
    before = transcript.summary(rec)["turns"]

    # wake=True, and checked against "sent" rather than truthiness. A held message is not a
    # delivered one, and a cord waiting for an answer that is sitting in a mailbox would wait for
    # ever.
    if broker.send(agent_id, prompt, wake=True) != "sent":
        return None

    # With a working limit, the wait ends at the timeout only for an agent that is not working.
    # An agent that is still working on the answer gets up to the working limit. An agent can
    # answer a call after ten minutes of real work, and a five minute timeout would fail it.
    # Without one, the timeout is hard, which a voice call and a job's own timeout need.
    working_limit = max(timeout, working_limit or timeout)
    started = time.time()
    while True:
        time.sleep(1.0)
        rec = registry.get(agent_id)
        if not rec:
            return None
        now = transcript.summary(rec)
        if now["turns"] > before:
            # Whichever engine the agent is running. A reply can arrive in pieces: the agent
            # speaks, uses a tool, speaks again. Wait for it to fall quiet so the last message is
            # the finished thought. A terminal is quiet when it stops drawing, and a chat engine
            # is quiet when its turn has ended.
            # On a call, the whole reply, not its last message. A caller wrote HANGUP, then used a
            # tool and wrote one more line, and the call went on without it. A voice call, a job
            # and Telegram keep the last message: it is the answer, and the rest is narration.
            engine = broker.get(agent_id) or broker.chat(agent_id)
            if engine is None or not engine.working():
                if whole:
                    return transcript.said_since(rec, before) or now["last_message"]
                return now["last_message"]
        if not broker.awake(agent_id):
            return None
        waited = time.time() - started
        if waited >= working_limit:
            return None
        if waited >= timeout:
            engine = broker.get(agent_id) or broker.chat(agent_id)
            if engine is None or not engine.working():
                return None


# -- transfer --------------------------------------------------------------------

def transfer(from_id, to_id, instruction):
    """Hand one agent's last message to another, with an instruction on top.

    Both agents must be awake, which is checked rather than assumed: waking the receiver here
    would hide a decision the operator should make, because waking costs memory.
    """
    a, b = registry.get(from_id), registry.get(to_id)
    if not a or not b:
        raise KeyError("no such agent")
    if from_id == to_id:
        raise ValueError("a cord needs two different agents")
    for rec in (a, b):
        if not broker.awake(rec["id"]):
            raise RuntimeError(f"{rec['id']} is asleep; wake it first")

    payload = transcript.last_message(a)
    if not payload:
        raise RuntimeError(f"{from_id} has not said anything to carry")

    cord = {
        "id": uuid.uuid4().hex[:12],
        "kind": "transfer",
        "from": from_id, "to": to_id,
        "instruction": instruction,
        "state": "running",
        "created": time.time(),
        "messages": [],
    }
    with _lock:
        _cords[cord["id"]] = cord
    _emit("cord.started", cord)

    prompt = (
        f"[from {a['name']} over a transfer cord]\n\n"
        f"{instruction.strip()}\n\n"
        + untrusted.wrap(f"{a['name']}'s last message", payload)
    )
    ok = broker.send(to_id, prompt, wake=False) == "sent"

    cord["state"] = "completed" if ok else "failed"
    cord["finished"] = time.time()
    cord["messages"] = [{"role": "payload", "text": payload}]
    _log({"ts": cord["created"], "kind": "transfer", "from": from_id, "to": to_id,
          "instruction": instruction, "chars": len(payload), "ok": ok})
    _emit("cord.finished", cord)

    # One use, then the cord disappears. It stays in the log, not on the screen.
    _forget_soon(cord["id"])
    return dict(cord)


# -- call ------------------------------------------------------------------------

# How long one message on a call may take while the agent is still working on it.
CALL_WAIT = 1800.0

CALLEE_BRIEF = (
    "[you are on a call from {caller}, over a cord]\n\n"
    "Answer directly and completely in one message. Do not ask clarifying questions and do "
    "not offer options: there is a shared budget of {budget} messages and questions spend it. "
    "If you cannot answer, say what you would need and stop.\n\n"
)

CALLER_BRIEF = (
    "[you are calling {callee}, over a cord]\n\n"
    "{instruction}\n\n"
    "Your reply to this message is delivered to {callee}, not to the owner. So write it to "
    "them: ask the question. Do not answer it yourself, even if you could.\n\n"
    "You have a budget of {budget} messages in total for both of you. Once {callee} has answered "
    "and you have what you need, end a message with a line that starts HANGUP: followed by what "
    "you learned. That summary is what you keep; the rest of this call is not worth carrying.\n\n"
)


def call(from_id, to_id, instruction, budget=None):
    """A bounded conversation between two agents, started at once and run in the background.

    Three rules make it safe, and each one earns its place. Only the caller can hang up, so the
    call is asymmetric and the callee cannot end it. Hanging up requires a summary, so what
    enters the caller's context is a conclusion rather than a transcript. The callee is told it
    is on a call, because otherwise it behaves as though speaking to a person and spends the
    budget being polite.
    """
    a, b = registry.get(from_id), registry.get(to_id)
    if not a or not b:
        raise KeyError("no such agent")
    if from_id == to_id:
        raise ValueError("a cord needs two different agents")
    for rec in (a, b):
        if not broker.awake(rec["id"]):
            raise RuntimeError(f"{rec['id']} is asleep; wake it first")

    # The shortest call that can end well is three messages: the question, the answer, and the
    # caller's HANGUP. A smaller budget ran out on the answer, so the caller never read it and the
    # call could only time out. Seventeen calls did, mostly the nightly scan note at budget 2.
    budget = max(3, int(budget or config.CALL_BUDGET))
    cord = {
        "id": uuid.uuid4().hex[:12],
        "kind": "call",
        "from": from_id, "to": to_id,
        "instruction": instruction,
        "state": "running",
        "budget": budget,
        "used": 0,
        "summary": None,
        "created": time.time(),
        "messages": [],
    }
    with _lock:
        _cords[cord["id"]] = cord
    _emit("cord.started", cord)
    threading.Thread(target=_run_call, args=(cord, a, b), daemon=True).start()
    return dict(cord)


def _spend(cord, role, text):
    cord["used"] += 1
    cord["messages"].append({"role": role, "text": text, "ts": time.time()})
    _emit("cord.message", {"id": cord["id"], "used": cord["used"], "role": role})
    return cord["used"] >= cord["budget"]


def _forget_soon(cord_id, after=20.0):
    """Take a finished cord off the screen. It stays in the log; only the drawing goes.

    Transfers have always done this and calls never did, so a finished call sat on the console for
    ever. The operator found one still drawn twenty minutes after it had failed.
    """
    def drop():
        time.sleep(after)
        with _lock:
            _cords.pop(cord_id, None)
        _emit("cord.gone", {"id": cord_id})
    threading.Thread(target=drop, name=f"cord-drop-{cord_id}", daemon=True).start()


def _finish(cord, state):
    log.info("cord %s %s -> %s finished as %s after %d messages",
             cord["id"], cord["from"], cord["to"], state, cord.get("used", 0))
    cord["state"] = state
    cord["finished"] = time.time()
    _log({"ts": cord["created"], "kind": "call", "from": cord["from"], "to": cord["to"],
          "instruction": cord["instruction"], "state": state, "used": cord["used"],
          "summary": cord["summary"]})
    _emit("cord.finished", dict(cord))
    _forget_soon(cord["id"])


def _run_call(cord, a, b):
    caller, callee = cord["from"], cord["to"]
    try:
        opening = CALLER_BRIEF.format(callee=b["name"], instruction=cord["instruction"].strip(),
                                      budget=cord["budget"])
        turn = ask(caller, opening, working_limit=CALL_WAIT, whole=True)
        if turn is None:
            return _finish(cord, "failed")
        out_of_budget = _spend(cord, caller, turn)

        heard_back = False
        while True:
            # The HANGUP is read before the budget. A caller that hangs up on the last message it
            # is allowed has ended the call well, and it used to be logged as timed out.
            hung = HANGUP.search(turn)
            if hung and heard_back:
                cord["summary"] = hung.group(1).strip() or turn.strip()
                return _finish(cord, "completed")
            if out_of_budget:
                return _finish(cord, "timed_out")
            if hung and not heard_back:
                # A call where the caller hangs up before the callee has said anything is not a
                # call. This happened on the first real test: given a question it could answer
                # itself, the caller answered it and hung up, and the other agent was never asked.
                # So the marker is ignored until there has been an exchange, and the question is
                # passed on with the marker line removed.
                turn = HANGUP.sub("", turn).strip()
                cord["messages"].append({"role": "note", "ts": time.time(),
                                         "text": "hangup ignored: nobody had answered yet"})
                if not turn:
                    turn = cord["instruction"]

            reply = ask(callee, CALLEE_BRIEF.format(caller=a["name"], budget=cord["budget"])
                        + turn, working_limit=CALL_WAIT, whole=True)
            if reply is None:
                return _finish(cord, "failed")
            heard_back = True
            if _spend(cord, callee, reply):
                # Reaching the budget is not success. A call that runs out was a badly framed
                # question, and it should look different on the card from one that ended.
                return _finish(cord, "timed_out")

            turn = ask(caller, f"[{b['name']} answered]\n\n"
                               + untrusted.wrap(f"{b['name']}, the agent you called", reply),
                       working_limit=CALL_WAIT, whole=True)
            if turn is None:
                return _finish(cord, "failed")
            out_of_budget = _spend(cord, caller, turn)
    except Exception as exc:
        cord["error"] = str(exc)
        _finish(cord, "failed")


def hangup(cord_id, summary):
    """End a call from outside it. The console's stop button for a call."""
    with _lock:
        cord = _cords.get(cord_id)
    if not cord or cord["state"] != "running":
        return False
    cord["summary"] = summary
    _finish(cord, "completed")
    return True


def reset():
    """Drop every cord and every listener. For tests."""
    with _lock:
        _cords.clear()
    _listeners.clear()
