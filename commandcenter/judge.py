"""The referee. Jev reads what the owner and the voice said, so the console need not trust the voice.

Two questions, one route (POST /api/voice/judge):

    reply   A card is waiting. The owner said a line. Is it a yes to the card exactly as read, a yes
            with a change, a no, or none of those? One Choice.
    claim   The voice said a line. Does it say an action was carried out? One Noul.

The voice once said "Sent" after his "yeah" and never called confirm. The daemon log proved it:
a propose, no confirm, and then "I did, Acme has it". The voice can say anything; the card is
what sends. So the browser asks here after each finished line, and acts on the answer through the
same confirm and cancel the buttons use.

This module decides nothing and runs nothing. It answers a question. Every failure answers the
safe way: "unrelated" for a reply and "not claimed" for a claim, so nothing ever acts on a fault.

The transport is memory.post_jev: the same key, the same scrub, the same timeouts. It is looked
up on the module each call, so a test that replaces it replaces it here too.
"""

import time

from . import config, logs, memory, voice

log = logs.get(__name__)

# The four answers to a card. The browser acts on the first three and ignores the fourth.
DECISIONS = ("confirmed", "modified", "cancelled", "unrelated")

# A judgement that arrives after this is no use: he has moved on. Shorter than the recall
# timeout, because recall is a question the voice waits for and this is not.
TIMEOUT = 6.0

# How much of his line is kept in the state and in the log. The state needs a sentence; the log
# needs a hint and no more, because the daemon log is not a copy of his words.
LINE_CHARS = 400
LOG_CHARS = 40

REPLY_QUESTION = {
    "type": "choice",
    "instructions": {
        "situation": "The owner is on a voice call with the front desk, his voice assistant. The front desk "
                     "proposed an action and read `proposal.readback` aloud. A card on screen "
                     "waits for the owner's answer, and nothing is sent until he gives it. "
                     "`owner_said` is the next thing the owner said, as speech recognition heard it.",
        "question": "What is `owner_said` as an answer to the proposal?",
    },
    "criteria": {
        "confirmed": {
            "meaning": "A clear yes to the proposal exactly as it was read. Nothing about the "
                       "agent, the recipient, the task, the time or the scope changes.",
            "examples": ["Yeah", "Yes, send", "yeah sure", "send it", "Yes please",
                         "Go ahead", "Do it"],
        },
        "modified": {
            "meaning": "An answer that changes the proposal: a different or an extra agent or "
                       "recipient, a different task, time or scope. A yes followed by a change "
                       "is this, not a yes.",
            "examples": ["yes but add initech", "change it to tomorrow",
                         "yeah but send it to job search instead", "yes and also cc Acme"],
        },
        "cancelled": {
            "meaning": "A no, a stop, or dropping the proposal.",
            "examples": ["no wait", "no", "don't send that", "cancel it", "forget it"],
        },
        "unrelated": {
            "meaning": "Anything that is not an answer to the proposal: filler, a sound, thinking "
                       "aloud, a question about something else, or a line too unclear to act on.",
            "examples": ["mm", "hmm", "uh", "what time is it"],
        },
    },
}

CLAIM_QUESTION = {
    "type": "noul",
    "instructions": {
        "situation": "The front desk is a voice assistant. He may only carry out an action after the "
                     "console confirms it, so saying an action is done does not make it done. "
                     "`desk_said` is one line he just said aloud.",
        "question": "Does `desk_said` claim that an action has already been carried out by "
                    "The front desk, such as a message sent, a task delegated, a proposal confirmed or "
                    "something done?",
    },
    "criteria": {
        "true": {
            "meaning": "The line says or implies that the front desk's action is complete.",
            "examples": ["Sent.", "I did, Acme has it.", "Done, I passed that to job search.",
                         "Confirmed, it has gone out."],
        },
        "false": {
            "meaning": "The line proposes, asks, describes a plan, or reports news or what an "
                       "agent said, without saying the front desk has carried out an action.",
            "examples": ["I will ask job search to draft it. Shall I?", "Want me to send it?",
                         "Job search has finished its turn.", "Here is what I found."],
        },
    },
}


def _line(text):
    return " ".join(str(text or "").split())[:LINE_CHARS]


def reply_payload(held, line):
    """One Choice. The proposal and his line are the state; the question holds only the judgment."""
    proposal = {"readback": held.get("readback") or "", "action": held.get("kind") or "send",
                "agent": held.get("agent") or held.get("name") or "", "task": held.get("task") or ""}
    return {"state": {"proposal": proposal, "owner_said": _line(line)},
            "model": memory.JEV_MODEL, "questions": {"reply": REPLY_QUESTION}}


def claim_payload(line):
    """One Noul. His line is not in it; only hers."""
    return {"state": {"desk_said": _line(line)},
            "model": memory.JEV_MODEL, "questions": {"claim": CLAIM_QUESTION}}


def _hint(line):
    text = _line(line)
    return text if len(text) <= LOG_CHARS else text[:LOG_CHARS - 3] + "..."


def _ask(payload):
    """(answer dict or None, why it failed or ''). Never raises."""
    if not config.TYPESAFE_KEY:
        return None, "no key"
    try:
        got = memory.post_jev(payload, config.TYPESAFE_KEY,
                              timeout=min(TIMEOUT, config.TYPESAFE_TIMEOUT))
    except Exception as exc:
        # The reason up to its colon. What follows can be the service repeating the request,
        # and the request holds his words.
        return None, memory._safe(str(exc)).split(":")[0][:80]
    answers = got.get("answers") if isinstance(got, dict) else None
    return (answers if isinstance(answers, dict) else None), ""


def reply(proposal_id, line):
    """{decision, confidence}. "unrelated" at 0 whenever there is nothing safe to say."""
    started = time.monotonic()
    held = voice.pending_proposal(proposal_id)
    decision, confidence, why = "unrelated", 0.0, ""
    if not held:
        why = "no such proposal"
    elif not _line(line):
        why = "empty line"
    else:
        answers, why = _ask(reply_payload(held, line))
        got = (answers or {}).get("reply")
        if isinstance(got, dict) and got.get("choice") in DECISIONS:
            decision = got["choice"]
            try:
                confidence = max(0.0, min(1.0, float(got.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
        elif not why:
            why = "no answer"
    ms = round((time.monotonic() - started) * 1000)
    log.info("voice judge reply %s: %s %.2f, %d ms%s, line %r", str(proposal_id or "")[:16],
             decision, confidence, ms, f", {why}" if why else "", _hint(line))
    return {"decision": decision, "confidence": round(confidence, 3)}


def claim(line):
    """{claimed, p}. Not claimed at 0 whenever there is nothing safe to say."""
    started = time.monotonic()
    p, why = 0.0, ""
    if not _line(line):
        why = "empty line"
    else:
        answers, why = _ask(claim_payload(line))
        got = (answers or {}).get("claim")
        value = got.get("noul") if isinstance(got, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            p = max(0.0, min(1.0, float(value)))
        elif not why:
            why = "no answer"
    ms = round((time.monotonic() - started) * 1000)
    log.info("voice judge claim: %s %.2f, %d ms%s, line %r", "claimed" if p > 0.5 else "not",
             p, ms, f", {why}" if why else "", _hint(line))
    return {"claimed": p > 0.5, "p": round(p, 3)}


def judge(body):
    """The route's one entry. A kind it does not know is a ValueError, which the route says as 400."""
    body = body if isinstance(body, dict) else {}
    kind = str(body.get("kind") or "").strip()
    if kind == "reply":
        return reply(body.get("proposal_id"), body.get("line"))
    if kind == "claim":
        return claim(body.get("line"))
    raise ValueError("kind must be reply or claim")
