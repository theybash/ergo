#!/usr/bin/env python3
"""A stand-in for `claude`, so the broker can be tested without the real model.

Two shapes, chosen by whether -p is in the arguments, because that is exactly what tells the real
CLI which one it is:

    chat engine   read stream-json user messages on stdin and answer each one with a run of
                  events: init, a few text deltas, then result. It also speaks the control
                  channel: it asks before a tool when the message says to, and it answers the
                  requests the broker sends it.
    terminal      behave like a small terminal program, so the pseudo-terminal path has something
                  to draw: print a line and echo what is typed.

The control lines are copied from a real run against claude 2.1.270, wrapper for wrapper. That
matters more than it looks: a control_response without its inner `response` object is accepted and
ignored by the real CLI, with no error and no tool result, and the turn sits blocked for ever.
Only a fake that is exact catches that.

It never talks to Anthropic and spends nothing. It exists only for the tests.
"""

import json
import queue
import sys
import threading
import time
import uuid

SESSION = "fake-session"

# One writer at a time, because the turn runs on the main thread and the acknowledgements are
# written by the thread reading stdin.
PEN = threading.Lock()
# Messages typed at the agent, in the order they arrived. None means stdin ended.
SAID = queue.Queue()
# Answers to the asks we sent, by request id, with the event that says one has landed.
ANSWERS = {}
WAITING = {}
# The permission mode the last set_permission_mode asked for.
MODE = ["acceptEdits"]
# Set by an interrupt, cleared at the start of each turn.
ABORT = threading.Event()

QUESTION = {"questions": [{
    "question": "Which colour do you prefer?",
    "header": "Colour",
    "options": [{"label": "red", "description": "warm"},
                {"label": "blue", "description": "cool"}],
    "multiSelect": False}]}


def emit(obj):
    with PEN:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


# -- the control channel ----------------------------------------------------------


def control(got):
    """Something the broker asked us. Acknowledged in the real shapes, wrapper for wrapper."""
    rid = got.get("request_id")
    req = got.get("request") or {}
    subtype = req.get("subtype")
    if subtype == "set_permission_mode":
        MODE[0] = req.get("mode")
        reply = {"mode": MODE[0]}
    elif subtype == "interrupt":
        ABORT.set()
        reply = {"still_queued": []}
    else:
        emit({"type": "control_response", "response": {
            "subtype": "error", "request_id": rid, "error": f"unknown subtype {subtype}"}})
        return
    emit({"type": "control_response", "response": {
        "subtype": "success", "request_id": rid, "response": reply}})


def read_stdin():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            got = json.loads(raw)
        except ValueError:
            continue
        kind = got.get("type")
        if kind == "control_response":
            wrapper = got.get("response") or {}
            rid = wrapper.get("request_id")
            # The inner object is the answer. A line without it is ignored here exactly as the
            # real CLI ignores it, so the turn stays blocked and a test can see the block.
            if "response" not in wrapper:
                continue
            ANSWERS[rid] = wrapper.get("response") or {}
            event = WAITING.get(rid)
            if event:
                event.set()
        elif kind == "control_request":
            control(got)
        else:
            SAID.put(got)
    SAID.put(None)


def ask(tool_name, got):
    """Stop and ask, then wait. None means the turn was interrupted while waiting."""
    rid = str(uuid.uuid4())
    event = threading.Event()
    WAITING[rid] = event
    emit({"type": "control_request", "request_id": rid, "request": {
        "subtype": "can_use_tool", "tool_name": tool_name, "display_name": tool_name,
        "input": got, "description": "a fake tool call",
        "decision_reason": "This command requires approval",
        "tool_use_id": "toolu_" + rid[:12]}})
    # No time limit, exactly as the real one waits. The interrupt is the way out.
    while not event.wait(0.05):
        if ABORT.is_set():
            WAITING.pop(rid, None)
            return None
    WAITING.pop(rid, None)
    return ANSWERS.pop(rid, None)


# -- turns ------------------------------------------------------------------------


def stream(reply):
    emit({"type": "stream_event", "event": {"type": "message_start", "message": {}}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}}})
    for piece in (reply[:4], reply[4:]):
        emit({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": piece}}})
    emit({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    emit({"type": "stream_event", "event": {"type": "message_stop"}})
    # The real result row does not carry "type" as its first key, so neither does this one.
    emit({"duration_ms": 1, "session_id": SESSION, "type": "result",
          "subtype": "success", "result": reply})


def slow():
    emit({"type": "stream_event", "event": {"type": "message_start", "message": {}}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}}})
    for n in range(100):
        if ABORT.is_set():
            return aborted()
        emit({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": f"word{n} "}}})
        time.sleep(0.3)
    emit({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    emit({"type": "stream_event", "event": {"type": "message_stop"}})
    emit({"duration_ms": 1, "session_id": SESSION, "type": "result",
          "subtype": "success", "result": "slow"})


def aborted():
    """How an interrupted turn ends, named the way the real one names it."""
    emit({"duration_ms": 1, "session_id": SESSION, "type": "result",
          "subtype": "error_during_execution", "terminal_reason": "aborted_streaming",
          "is_error": True, "result": "[Request interrupted by user]"})


def turn(said):
    ABORT.clear()
    if "ASK-PERM" in said:
        got = ask("Bash", {"command": "curl -s https://example.com"})
        if got is None:
            return aborted()
        reply = "allowed" if got.get("behavior") == "allow" else "denied"
    elif "ASK-QUESTION" in said:
        got = ask("AskUserQuestion", QUESTION)
        if got is None:
            return aborted()
        if got.get("behavior") != "allow":
            # The deny message is what the model reads, so the reply echoes it for a test to see.
            reply = "denied: " + (got.get("message") or "")
        else:
            answers = (got.get("updatedInput") or {}).get("answers") or {}
            reply = answers.get(QUESTION["questions"][0]["question"]) or "no answer"
    elif "SLOW" in said:
        # A long reply, a piece every 0.3 s for about 30 s, so the browser has a working turn to
        # look at and a stop has something to halt.
        return slow()
    else:
        reply = f"you said {said}"
    stream(reply)


def chat_engine():
    emit({"type": "system", "subtype": "init", "session_id": SESSION, "tools": []})
    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        got = SAID.get()
        if got is None:
            return
        try:
            said = got["message"]["content"][0]["text"]
        except Exception:
            said = json.dumps(got)
        turn(said)


def terminal():
    sys.stdout.write("fake claude ready\n")
    sys.stdout.flush()
    for line in sys.stdin:
        sys.stdout.write("> " + line)
        sys.stdout.flush()
    # A terminal that has read everything waits rather than exiting, the way an interface does.
    time.sleep(30)


if __name__ == "__main__":
    if "-p" in sys.argv[1:]:
        chat_engine()
    else:
        terminal()
