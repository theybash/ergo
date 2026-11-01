"""The voice front desk.

The owner talks to a British voice that knows what their agents are doing. The speech model runs at
OpenAI and the audio goes browser to OpenAI directly, because audio through this daemon would add
a hop to every syllable. What stays here is everything that must not leave:

    the key            the daemon opens the call and passes back an address, never a credential
    the briefing       who the agents are and what they last said, assembled per call
    the tools          one route, so the browser forwards a tool call without knowing what it is
    the confirm        the daemon decides that a delegation was proposed before it can be run

That last one is the point of the module. The voice can say anything; it cannot make anything
happen. To give an agent work it calls `propose`, which writes a note in memory and hands back a
sentence to read aloud. Only `confirm`, with that note's id, still fresh, reaches the broker. A
`confirm` for an id that was never proposed does nothing at all, however it was arrived at.

Two prompts leave here, not one. GPT-Live is two models: the live one holds the conversation and
decides when to ask for help, and a backend Responses model does the thinking and calls the tools.
So `live_prompt` is the front of house and `briefing` is the whole file behind it.

Nothing here logs the key. There is no debug mode that does.
"""

import base64
import contextlib
import contextvars
import binascii
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from pathlib import Path

from . import (broker, clock, config, cron, data, inbox, live, logs, memory, network, registry,
               transcript)

log = logs.get(__name__)

_lock = threading.RLock()

# The standing instructions, in a file the owner edits. Read per call rather than cached, so a
# change to the persona takes effect on the next call and never needs a restart.
PERSONA = Path(__file__).resolve().parent / "voice_persona.md"

# Where a call is opened. GPT-Live has no ephemeral client secret of any kind, so the browser
# never holds credentials at all: it hands its connection offer to this daemon, the daemon opens
# the session with the project key, and the answer goes back. The key is one hop further from the
# browser than the Realtime design would have put it.
LIVE_URL = "https://api.openai.com/v1/live/sessions"

# A recap read aloud is two sentences, not a page.
RECAP_WORDS = 60

# How many words the briefing may spend on everything below the persona. The persona is the
# owner's own file and is never cut; the previous turns are, oldest first. The number is a token
# budget in disguise, and words are the unit this code can count. The base pays for the fleet and
# the recaps, and each turn of the memory window adds TURN_WORDS, which is what one turn costs at
# most once it is cut to thirty words. So 30 turns fit whole unless the fleet and the recaps
# overrun the base, and then it is the oldest turns that go, never the persona.
BRIEFING_WORDS = 300
TURN_WORDS = 32

# How many of this call's own turns a resumed session carries over. About a minute of talk, which
# is what "where were we" needs, and well inside the 128 messages the API takes.
RESUME_TURNS = 12

# Said to both models when a paused call opens its second session. The persona tells them to speak
# first and to open with "Welcome back". That is right once and wrong twice, so a resume has
# to take it back in words, because the persona file is the owner's and this code does not edit it.
NO_GREETING_BASE = (
    "## You are resuming, not starting\n\n"
    "This call was paused a moment ago and is now going again. It is one conversation and they "
    "know it. Do not greet them. Do not say welcome back, do not say hello, and do not introduce "
    "yourself. Carry on from the last thing that was said."
)

# The last sentence of an ordinary resume. Left out when an agent woke the call, because then
# the wait block tells them to speak first, and "wait in silence" beside "tell them now" is the
# contradiction that kept them quiet on 23 September.
WAIT_SILENT = "If they have said nothing yet, wait for them in silence."

NO_GREETING = f"{NO_GREETING_BASE} {WAIT_SILENT}"

# Said when a paused call is woken by the agent it was waiting for. The whole point of pausing
# rather than hanging up: they asked for something, the answer arrived, and they should be told
# without having to ask a second time.
WAIT_REPORT = "## While you were paused"

# The largest answer a tool may hand back. A page of data is a page; anything more is a file, and
# a file read aloud is nobody's idea of a conversation.
MAX_DATA = 2048


# -- small helpers ---------------------------------------------------------------

def _words(text, count):
    """The first `count` words, with a marker when there were more."""
    parts = (text or "").split()
    if len(parts) <= count:
        return " ".join(parts)
    return " ".join(parts[:count]) + " ..."


def _one_line(text, count=30):
    """One line of at most `count` words. Newlines become spaces, because this is read aloud."""
    return _words(" ".join((text or "").split()), count)


def _when(ts):
    """A timestamp as something sayable, or nothing."""
    if not ts:
        return ""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return str(ts)[:16]


def _speaker(who):
    """How one logged row is named when it is read back to the model.

    "You" is the front desk, because the model is reading its own words. A note is neither of
    them: it is the console saying a task went out, and a note read back as something the voice
    said is a voice that believes it sent something it did not.
    """
    if who == "you":
        return "The owner"
    return "Note" if who == "note" else "You"


# -- the view a call is in -----------------------------------------------------------
# The console's view (work, demo, personal) hides cards from someone looking over their shoulder.
# A call made in a view keeps to it: The front desk is built from the agents tagged for that view and no
# others, and every tool refuses the rest as if they did not exist. Telling them "this is a demo"
# out loud did not work, because the private facts were already in their prompt before they heard
# it. So they are left out instead. In the demo view past calls are left out too, since they
# mention everyone; every other view keeps its memory.
#
# The view rides on each request from the browser and is held for that request only, so two
# requests never see each other's view. No view, or "all", is the whole fleet, as before.
_view = contextvars.ContextVar("voice_view", default=None)


@contextlib.contextmanager
def scoped(view):
    """Run the body as a call made in `view`."""
    want = (view or "").strip().lower() if isinstance(view, str) else ""
    token = _view.set(want if want in config.VIEW_NAMES else None)
    try:
        yield
    finally:
        _view.reset(token)


# Past calls are free across views: a call in Work remembers what was said in Personal, and the
# other way round. The Demo view alone forgets them, because it is the one shown to strangers and a
# past call mentions everyone.
FORGETS_PAST = ("demo",)


def _forgets_past():
    return _view.get() in FORGETS_PAST


def current_view():
    """The view this request is in, or nothing for the whole fleet."""
    return _view.get()


def in_view(rec, view=None):
    """The console's own rule: an agent with no tags shows in every view."""
    view = view if view is not None else _view.get()
    if not view:
        return True
    if not rec:
        # Something from no known agent, such as a job whose agent was removed: left out, since
        # nobody tagged it for this view.
        return False
    tags = rec.get("views")
    return not tags or view in tags


def _fleet():
    """The agents this call may know about."""
    return [rec for rec in registry.all() if in_view(rec)]


def _brief():
    """The untold inbox items from agents this call may know about."""
    return [it for it in inbox.for_call() if in_view(registry.get(it.get("agent")))]


def _view_block():
    """What they are told when the call is in a view. Nothing for the whole fleet."""
    view = _view.get()
    if not view:
        return []
    return ["", "## This call is in the " + view + " view", "",
            "They may be showing this call to other people. The agents listed here are the only "
            "ones you know. Never guess at others, and never mention another agent, client, "
            "company or person unless they name them first."]


def resolve(name):
    """Find an agent from what was said. Returns the record, or nothing.

    A name arrives through speech recognition, so it arrives loosely: "job search" for job-search,
    "Personal Assistant" for personal-assistant, sometimes only the first word. The id is tried
    first, then the name, then both with every non letter dropped, then a prefix. Whoever calls
    this says out loud which agent it landed on, so a wrong guess is heard rather than acted on.
    """
    want = (name or "").strip().lower()
    if not want:
        return None
    agents = [rec for rec in registry.all(include_hidden=True) if in_view(rec)]

    def flat(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    for rec in agents:
        if rec["id"].lower() == want or (rec["name"] or "").lower() == want:
            return rec
    tight = flat(want)
    for rec in agents:
        if flat(rec["id"]) == tight or flat(rec["name"]) == tight:
            return rec
    if len(tight) >= 3:
        for rec in agents:
            if flat(rec["id"]).startswith(tight) or flat(rec["name"]).startswith(tight):
                return rec
    return None


# -- the call log ----------------------------------------------------------------
# Every finished turn of every call, one JSON object per line. The browser posts each turn as the
# transcript of it arrives, so the file is written while the conversation happens rather than at
# the end of it. A call that drops mid sentence still leaves everything said before it.

def _calls_file():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "calls.jsonl"


# A turn worth keeping has a letter or a digit somewhere in it. Speech recognition emits rows that
# are one full stop or one comma, because a fragment arrived with the punctuation of the sentence
# before it and nothing else. Those are not turns and the log is better without them.
HAS_WORD = re.compile(r"[^\W_]", re.UNICODE)


# The three kinds of row. "you" is the owner, "voice" is the front desk, and "note" is the console
# itself saying what it did: a task went out, or it did not. A note is neither of them speaking,
# and the next call's briefing has to read it as a fact rather than as something one of them said.
WHO = ("you", "voice", "note")


def say_down(who, text):
    """Write one turn down. `who` is "you", "voice", or "note" for what the console did.

    A turn of only punctuation or whitespace is dropped rather than written. The browser already
    refuses to post one; this refuses it again, because the log is read back into the next call's
    briefing and a briefing full of full stops is a briefing that says nothing.
    """
    text = (text or "").strip()
    if not text or not HAS_WORD.search(text):
        return None
    row = {"at": clock.now(), "who": who if who in WHO else "voice", "text": text}
    with _lock:
        with open(_calls_file(), "a") as fh:
            fh.write(json.dumps(row) + "\n")
    return row


def calls(count=12):
    """The last few turns of previous calls, oldest first."""
    path = _calls_file()
    if not path.exists():
        return []
    try:
        _, lines = transcript.tail_lines(path, max(1, int(count)))
    except OSError:
        return []
    out = []
    for raw in lines[-max(1, int(count)):]:
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


# -- the budget ------------------------------------------------------------------
# Speech is billed by the minute, so a tab left open overnight is a bill. Two ceilings: the daily
# one refuses a new call, and the session one ends the call in hand. Both are counted from what
# the browser reports, because the browser is the only thing that knows when the audio stopped.

def _usage_file():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "usage.json"


def _today():
    return clock.local().strftime("%Y-%m-%d")


def usage():
    """Seconds spent today. A new day starts at nought rather than carrying yesterday forward."""
    try:
        held = json.loads(_usage_file().read_text())
    except (OSError, ValueError):
        held = {}
    if held.get("day") != _today():
        return {"day": _today(), "seconds": 0.0, "calls": 0}
    return {"day": held["day"], "seconds": float(held.get("seconds") or 0.0),
            "calls": int(held.get("calls") or 0)}


def budget():
    """What is left today, in minutes, as the overlay's clock shows it."""
    spent = usage()["seconds"] / 60.0
    cap = config.VOICE_DAILY_MINUTES
    return {"used_minutes": round(spent, 2), "daily_minutes": cap,
            "left_minutes": round(max(0.0, cap - spent), 2),
            "session_minutes": config.VOICE_SESSION_MINUTES}


def spend(seconds, calls_delta=0):
    """Add to today's total. Returns the budget as it stands afterwards."""
    with _lock:
        held = usage()
        held["seconds"] = round(held["seconds"] + max(0.0, float(seconds or 0.0)), 2)
        held["calls"] = held["calls"] + int(calls_delta or 0)
        _usage_file().write_text(json.dumps(held))
    return budget()


# -- the briefing ----------------------------------------------------------------

def _fleet_lines():
    """One line per agent: who it is, whether it is up, and what it is for."""
    out = []
    for rec in _fleet():
        bits = [rec["id"], "awake" if broker.awake(rec["id"]) else "asleep"]
        if rec.get("role"):
            bits.append(rec["role"])
        if rec.get("note"):
            bits.append(_one_line(rec["note"], 12))
        out.append(", ".join(bits))
    return out


# The most a persona may be. The file is read into every call's two prompts, and a prompt that
# large pushes the conversation itself out of the window.
PERSONA_CHARS = 20000


def _previous_persona():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "persona.previous.md"


# The front desk's greeting as audio: their own voice saying "Welcome back", taken once from a real
# call by the browser and played the moment a call is tapped. At most this size.
GREETING_BYTES = 1024 * 1024


def _greeting_file():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "greeting.wav"


def greeting():
    """The recorded greeting as (bytes, mime), or nothing before the first one is taken."""
    try:
        data = _greeting_file().read_bytes()
    except OSError:
        return None
    return (data, "audio/wav") if data else None


def save_greeting(body):
    """POST /api/voice/greeting {audio: base64 WAV}. Kept once; a second one replaces it."""
    raw = (body or {}).get("audio") or ""
    if isinstance(raw, str) and raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise ValueError("The greeting did not arrive whole.")
    if not data.startswith(b"RIFF") or len(data) < 2000 or len(data) > GREETING_BYTES:
        raise ValueError("The greeting must be a short WAV recording.")
    tmp = _greeting_file().with_suffix(".wav.tmp")
    tmp.write_bytes(data)
    tmp.replace(_greeting_file())
    log.info("voice greeting recorded, %d bytes", len(data))
    return {"ok": True, "bytes": len(data)}


def reset_greeting():
    """Forget the greeting, so the next call records it again."""
    try:
        _greeting_file().unlink()
    except OSError:
        pass
    return {"ok": True}


def prompt_doc(path, previous, limit=PERSONA_CHARS):
    """A prompt file as it stands, and whether a previous version is kept. The front desk's persona and
    the agents' voice prompt are both edited on Config this way."""
    try:
        text = path.read_text()
    except OSError:
        text = ""
    return {"text": text, "previous": previous.exists(), "limit": limit}


def save_doc(path, previous, text, limit=PERSONA_CHARS):
    """Write a new prompt file. The one before it is kept, once.

    Written to a temporary file and renamed, so a call that opens in the same instant reads the
    old file or the new one, never half of each. The next call uses it; a call already open keeps
    what it started with, because a session's instructions are fixed when it opens.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("The prompt cannot be empty.")
    if len(text) > limit:
        raise ValueError(f"The prompt is longer than {limit} characters.")
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    try:
        old = path.read_text()
    except OSError:
        old = None
    if old == text:
        return prompt_doc(path, previous, limit)
    if old is not None:
        previous.write_text(old)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    log.info("%s saved, %d characters", path.name, len(text))
    return prompt_doc(path, previous, limit)


def restore_doc(path, previous, limit=PERSONA_CHARS):
    """Swap the saved file and the previous one."""
    if not previous.exists():
        raise ValueError("There is no previous version.")
    back = previous.read_text()
    try:
        now = path.read_text()
    except OSError:
        now = None
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(back)
    tmp.replace(path)
    if now is not None:
        previous.write_text(now)
    log.info("%s put back to the previous version", path.name)
    return prompt_doc(path, previous, limit)


def persona():
    """GET /api/voice/persona: The front desk's persona file."""
    return prompt_doc(PERSONA, _previous_persona())


def save_persona(text):
    """POST /api/voice/persona: write their new persona."""
    return save_doc(PERSONA, _previous_persona(), text)


def restore_persona():
    """POST /api/voice/persona/previous."""
    return restore_doc(PERSONA, _previous_persona())


def _persona():
    """The standing instructions, as the owner wrote them."""
    try:
        return PERSONA.read_text().strip()
    except OSError:
        # The file is the owner's to edit, so it can be moved or emptied. A voice with no persona
        # is still better than no voice, and the sentence says which file to put back.
        log.warning("%s is missing; using the fallback persona", PERSONA)
        return ("You are the front desk, the front desk of Ergo, the owner's house of AI agents. Be British, dry and brief. "
                "commandcenter/voice_persona.md is missing, so these are the fallback "
                "instructions.")


def _no_greeting(back, brief=None):
    """The resume section: the full one, or without the silence when there is news to tell."""
    return NO_GREETING_BASE if came_back(back) or brief else NO_GREETING


def _calm_block():
    """Calm mode: no reminders, no task list, no "overdue", and no questions about it."""
    from . import calm
    if not calm.on():
        return []
    return ["", "## Calm mode", "",
            "Calm mode is on. Do not raise their task list or remind them of anything. Never say "
            "overdue, late or forgot. Do not ask how they feel, and never mention calm mode. "
            "Answer what they ask, warmly and briefly."]


# Their task list stays out of the opening. It added up to 5 tasks, each with "done, move it or drop
# it", after every untold item, and the 18 October opening ran to 200 words. The front desk still owns the
# list (The owner, 18 October 2026: "owning my task lists is important"), and opens it with the
# his_tasks tool when they ask.
TASKS_LINE = ("Their task list is not in this prompt. Do not raise it. When they ask about their tasks, "
              "the backend has them.")


def _side():
    return "work" if current_view() == "work" else "personal"


def live_prompt(resuming=False, back=None, sent=None, brief=None):
    """What the live model itself is told. Short, because it is the front of house.

    `resuming` is a paused call opening its second session. The persona tells them to speak first
    and to open with "Welcome back", and that instruction is in this prompt rather than the
    briefing, because this is the model that talks. So this is where it has to be taken back, or
    they greet the owner again in the middle of a sentence.

    It holds the conversation and decides when to hand a question to the backend. It does not run
    the tools and has no need of the recaps or the history, so it gets the persona, the names of
    the agents and the time, and an instruction to ask the backend for everything else. The docs
    are explicit about this split, and it is also the cheap arrangement: this prompt is read on
    every turn of speech, and the briefing below is read once per delegated question.

    `brief` is the inbox items still untold, most urgent first. They go here, in the startup
    instructions, because on a long call a replacement engine keeps these and little else.
    """
    parts = [_persona(), "", "## The fleet right now", ""]
    parts.extend(_fleet_lines() or ["Nobody is registered."])
    parts += ["",
              "Anything about the agents, the schedule or the past goes to the backend. It holds "
              "the tools and the records. Say what you are looking up, then wait for it.",
              "",
              f"The time is {clock.local().strftime('%A %d %B, %H:%M')}."]
    parts += _view_block()
    parts += _calm_block()
    if resuming:
        parts += ["", _no_greeting(back, brief)]
        parts += _wait_block(back)
        parts += _sent_block(sent)
    parts += inbox.brief_block(brief, resuming=resuming)
    parts += ["", TASKS_LINE]
    return "\n".join(parts)


def briefing(skip=None, brief=None):
    """Everything the backend model is told: who you are, who they are, and where we left off.

    `skip` is lines the caller is about to say itself. A resume reads this call's own turns out
    under their own heading, and those turns are already in the log, so without this they would
    be read twice and the second copy would push an older one out.

    Persona first, because it is the part that does not change. Then the fleet, then a recap for
    each agent that is awake, then the last turns of previous calls, then the time. Kept short on
    purpose: every word of this is read on every delegated question, and a briefing longer than
    the conversation is a briefing that pushes the conversation out of the window.
    """
    parts = [_persona(), "", "## The fleet right now", ""]
    lines = _fleet_lines()
    parts.extend(lines or ["Nobody is registered."])

    parts += _view_block()

    recaps = []
    for rec in _fleet():
        if not broker.awake(rec["id"]):
            continue
        text = transcript.recap(rec)
        if text:
            recaps.append(f"{rec['id']}: {_words(text, RECAP_WORDS)}")
    if recaps:
        parts += ["", "## What the awake ones last wrote down", ""] + recaps

    held = set(skip or ())
    previous = []
    window = memory.memory_turns()
    for row in ([] if _forgets_past() else calls(window)):
        line = f"{_speaker(row.get('who'))}: {_one_line(row.get('text'), 30)}"
        if line not in held:
            previous.append(line)

    # The oldest turns go first when it is too long. A briefing that overruns its budget pushes
    # the conversation itself out of the window, which is a worse failure than a short memory.
    def spent():
        return sum(len(line.split()) for line in parts[3:] + recaps + previous)

    allowed = BRIEFING_WORDS + TURN_WORDS * window
    while previous and spent() > allowed:
        previous.pop(0)

    if previous:
        parts += ["", "## The last of your previous calls", ""] + previous

    parts += _calm_block()
    parts += inbox.brief_block(brief)
    parts += ["", f"The time is {clock.local().strftime('%A %d %B, %H:%M')}."]
    return "\n".join(parts)


# The most agents one report names. A call delegates to two or three; past this it is a list
# read aloud, and nobody wants that.
BACK_MAX = 6


def _one_back(back):
    """One sentence for one agent that came back, or nothing."""
    if not isinstance(back, dict):
        return ""
    agent = _one_line(back.get("agent"), 4)
    if not agent:
        return ""
    if (back.get("kind") or "") == "ask":
        asking = _one_line(back.get("asking"), 25)
        return (f"{agent} has stopped and is waiting on them: {asking}" if asking
                else f"{agent} has stopped and is waiting on an answer from them.")
    return f"{agent} has finished the turn you gave it."


def came_back(back):
    """What arrived while the call was paused, one short sentence per agent, or nothing.

    `back` is one fact or a list of them. A pause can come before they were told, so the browser
    keeps every fact not yet told and sends them all on the next resume. One agent named twice
    is said once, with the newest fact.

    The browser sends the agent and which of the two things happened, never the sentence. It is
    the same rule as the briefing: facts leave the browser, words are written here.
    """
    facts = back if isinstance(back, list) else [back]
    newest = {}
    for fact in facts:
        if _view.get() and isinstance(fact, dict) and not resolve(fact.get("agent")):
            continue
        said = _one_back(fact)
        if said:
            agent = _one_line(fact.get("agent"), 4)
            newest.pop(agent, None)
            newest[agent] = said
    return " ".join(list(newest.values())[-BACK_MAX:])


def _wait_block(back):
    """The section both models get when an agent is what woke the call."""
    said = came_back(back)
    if not said:
        return []
    return ["", WAIT_REPORT, "", said, "",
            "Tell them that now, in one sentence, without being asked. Then stop and wait for "
            "them. Do not greet them first."]


def _epoch(ts):
    """A transcript timestamp as seconds, or nothing. Claude Code writes ISO with a Z."""
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _sent_at(value):
    """When the browser says a task went out, in seconds. Milliseconds are taken too."""
    try:
        at = float(value)
    except (TypeError, ValueError):
        return None
    return at / 1000.0 if at > 1e11 else at


def delegated_lines(sent):
    """One line per agent given work on this call, with its state as the daemon sees it now.

    `sent` is what the browser holds: [{agent, at}], `at` being when the confirm went out. The
    state is read here, from the broker and the transcript, never from the browser, because the
    browser only knows what its socket happened to see. Three states: stopped on an ask, finished
    since the task went out, or still working.

    Finished means the agent's last message is newer than the task and newer than anything said
    to it since, and it is not busy right now. A message in the middle of a turn is not a finish.
    """
    out = []
    seen = set()
    for row in (sent if isinstance(sent, list) else [])[:BACK_MAX]:
        if not isinstance(row, dict):
            continue
        rec = resolve(row.get("agent"))
        if not rec or rec["id"] in seen:
            continue
        seen.add(rec["id"])
        at = _sent_at(row.get("at"))
        when = f" at {_when(at)}" if at else ""
        up = broker.awake(rec["id"])
        if up and broker.pending_asks(rec["id"]):
            out.append(f"{rec['id']}: stopped, waiting on an answer from them.")
            continue
        facts = transcript.summary(rec)
        said_at = _epoch(facts.get("last_message_at"))
        user_at = _epoch(facts.get("last_user_at"))
        busy = False
        if up:
            from . import live
            busy = live.working(rec, broker.get(rec["id"]) or broker.chat(rec["id"]))
        done = (said_at is not None and (at is None or said_at > at)
                and (user_at is None or said_at >= user_at) and not busy)
        if done:
            out.append(f"{rec['id']}: finished since the task went out{when}.")
        else:
            out.append(f"{rec['id']}: still working, no answer since the task went out{when}.")
    return out


def _sent_block(sent):
    """The section both models get on a resume: every agent delegated on this call, as it is."""
    lines = delegated_lines(sent)
    if not lines:
        return []
    return ["", "## Delegated on this call, as it stands now", ""] + lines + [
        "", "This is the state right now. Before you say an agent has not replied, check with a "
            "tool. Never answer that from memory."]


def _call_lines(turns):
    """This call's own turns, as the model reads them back. Newest last."""
    out = []
    for row in (turns or [])[-RESUME_TURNS:]:
        if not isinstance(row, dict):
            continue
        text = _one_line(row.get("text"), 30)
        if not text or not HAS_WORD.search(text):
            continue
        out.append(f"{_speaker(row.get('who'))}: {text}")
    return out


def resume_brief(turns, back=None, sent=None, brief=None):
    """The backend's briefing for a call that was paused and has opened a second session.

    The ordinary briefing plus this call's own turns and an instruction to carry on. It lives here
    rather than in the browser for the same reason the briefing does: the browser has never seen
    the persona, the fleet or the recaps, and a prompt assembled in two places is a prompt that
    drifts. The browser sends facts, which are the turns it holds, and the daemon writes the words.

    The turns go to the live model as well, through the session's own `input` field, which is the
    documented way to seed a conversation. This section is the backend's copy: the backend gets
    instructions and tool results and nothing else, so without it the thinking half of the pair
    would not know what the talking half had just said.
    """
    lines = _call_lines(turns)
    parts = [briefing(skip=lines)]
    if lines:
        parts += ["", "## This call so far", ""] + lines
    parts += ["", _no_greeting(back, brief)]
    parts += _wait_block(back)
    parts += _sent_block(sent)
    parts += inbox.brief_block(brief, resuming=True)
    return "\n".join(parts)


def resume_input(turns):
    """This call's turns as conversation items the new session starts holding.

    GPT-Live takes up to 128 of these on a session, one text part each, so a resumed call reads as
    one conversation rather than as a stranger who has read a summary of it.
    """
    out = []
    for row in (turns or [])[-RESUME_TURNS:]:
        if not isinstance(row, dict):
            continue
        text = (row.get("text") or "").strip()
        if not text or not HAS_WORD.search(text):
            continue
        if row.get("who") == "note":
            # The console's own line. It is in the briefing as a fact; putting it in the
            # conversation would make it something one of them said out loud.
            continue
        mine = row.get("who") == "you"
        out.append({"type": "message", "role": "user" if mine else "assistant",
                    "content": [{"type": "input_text" if mine else "output_text", "text": text}]})
    return out


# -- proposals -------------------------------------------------------------------
# The whole safety story in one dictionary. `propose` writes here and runs nothing; `confirm`
# reads here and is the only path to the broker. Held in memory rather than on disk on purpose: a
# spoken yes belongs to the conversation it was said in, and a daemon restart ends that.

_proposals = {}

# The most turns of the call a proposal may carry to an agent. A number larger than this is cut
# down to it rather than refused, because the voice picked it in the middle of a sentence and a
# refusal there costs a whole exchange to recover from.
MAX_TURNS = 100

# How many rows of the log are read to find those turns. A note sits between turns and is not one,
# so the scan has to be wider than the count it is looking for.
ATTACH_SCAN = 500


def _turns_wanted(value):
    """How many turns to attach: a whole number from nought to MAX_TURNS.

    The voice picks this, so it arrives as whatever the model wrote: a string of digits, a float,
    a number far too large, or nothing at all. Anything unreadable is nought, which attaches
    nothing, because a proposal that fails over a bad count is worse than one that carries no
    conversation.
    """
    try:
        want = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(want, MAX_TURNS))


def _attached_lines(count):
    """The last `count` turns of the call in hand, as the agent will read them. Newest last.

    Nothing writes down the moment a session opens, so the window is the only marker there is: a
    session runs for VOICE_SESSION_MINUTES at most, so a row older than that cannot belong to the
    call in hand. A paused call that went again is one call to the owner, and the window keeps the
    two halves together, which is what they mean by this call.

    A note is the console saying what it did, not a thing either of them said, so it is left out
    and does not count as a turn. The speaker names are for the agent reading this, which is why
    the voice is "The front desk" here and "You" in `_speaker`, where the voice reads its own words back.
    """
    count = _turns_wanted(count)
    if not count:
        return []
    cutoff = clock.now() - config.VOICE_SESSION_MINUTES * 60.0
    lines = []
    for row in calls(ATTACH_SCAN):
        if not isinstance(row, dict) or row.get("who") == "note":
            continue
        try:
            at = float(row.get("at") or 0.0)
        except (TypeError, ValueError):
            continue
        if at < cutoff:
            continue
        text = " ".join((row.get("text") or "").split())
        if not text:
            continue
        lines.append(f"{'The owner' if row.get('who') == 'you' else 'The front desk'}: {text}")
    return lines[-count:]


def _with_turns(task, lines):
    """The task the agent receives, with the call that led to it under a heading.

    The heading says how many turns are really there rather than how many were asked for. A call
    two turns old cannot hand over twenty, and an agent told it has twenty is an agent that looks
    for eighteen that do not exist.
    """
    if not lines:
        return task
    return (f"{task}\n\n"
            f"From the call with the owner, last {len(lines)} turns\n" + "\n".join(lines))


def _sweep():
    cutoff = clock.now() - config.VOICE_CONFIRM_SECONDS
    for key in [k for k, p in _proposals.items() if p["at"] < cutoff]:
        _proposals.pop(key, None)


def propose(agent, task=None, turns=0, name=None, cwd=None, action="task"):
    """Write down what the voice wants to do, and hand back the sentence to read aloud.

    Runs nothing. That is the entire contract, and it is why the voice may be wrong about what
    the owner asked for without anything happening.

    `turns` is how much of the call in hand goes with the task. A task alone loses the
    conversation that led to it, and an agent that reads "do the second one" has no second one.
    The voice picks the number; nought attaches nothing and MAX_TURNS is the ceiling. The turns
    themselves are read at the confirm rather than now, so a proposal carries a count and never a
    copy of the conversation.
    """
    # Stopping an agent's current turn is a proposal like any other, so Ask reads it back and
    # waits, and Auto sends it at once. Only the confirm differs: it interrupts instead of sends.
    if (action or "task").strip().lower() == "stop":
        rec = resolve(agent)
        if not rec:
            raise KeyError(f"no agent called {agent!r}")
        readback = f"I will stop what {rec['id']} is doing now. Shall I?"
        held = {"id": uuid.uuid4().hex[:8], "kind": "stop", "agent": rec["id"],
                "name": rec["name"], "cwd": None, "task": "", "turns": 0, "at": clock.now(),
                "readback": readback}
        with _lock:
            _sweep()
            _proposals[held["id"]] = held
        return {"id": held["id"], "readback": readback,
                "agent": rec["id"], "action": "stop", "task": "", "turns": 0,
                "expires_in": config.VOICE_CONFIRM_SECONDS}
    task = (task or "").strip()
    if not task:
        raise ValueError("a proposal needs a task")
    turns = _turns_wanted(turns)
    # Said aloud only when there is something to say. A proposal that attaches nothing reads the
    # way it always has, which is most of them.
    attached = f" with the last {turns} turns of this call attached" if turns else ""

    if (agent or "").strip().lower() == "new":
        wanted = (name or "").strip()
        if not wanted:
            raise ValueError("a new agent needs a name")
        where = config.resolve_dir(cwd or "~")
        held = {"id": uuid.uuid4().hex[:8], "kind": "new", "agent": None, "name": wanted,
                "cwd": where, "task": task, "turns": turns, "at": clock.now()}
        readback = (f"I will make a new agent called {wanted} in {where}, "
                    f"and ask it to {task}{attached}. Shall I?")
    else:
        rec = resolve(agent)
        if not rec:
            raise KeyError(f"no agent called {agent!r}")
        held = {"id": uuid.uuid4().hex[:8], "kind": "send", "agent": rec["id"], "name": rec["name"],
                "cwd": None, "task": task, "turns": turns, "at": clock.now()}
        readback = f"I will ask {rec['id']} to {task}{attached}. Shall I?"

    # Kept with the proposal, so the referee in judge.py reads the sentence they actually heard.
    held["readback"] = readback
    with _lock:
        _sweep()
        _proposals[held["id"]] = held
    return {"id": held["id"], "readback": readback, "agent": held["agent"] or held["name"],
            "task": task, "turns": turns, "expires_in": config.VOICE_CONFIRM_SECONDS}


def propose_tell(agent, text, call_id, now=False, busy=False):
    """An agent's call proposes to put what the owner said into its own chat.

    Held with the front desk's proposals, so the card, the referee and the confirm route are the same
    ones. Ask reads it back and waits; Auto confirms it at once. The agent is the call's own.

    Kept for the end of the call, unless they asked for it now. Now, into an agent that is in the
    middle of its work, is theirs to choose every time (The owner, 20 October 2026: "Do you still want
    to inject it into the context? That gives me more control"), so that one is never confirmed
    by Auto: `choose` says so.
    """
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("say what to pass on")
    choose = bool(now and busy)
    if choose:
        readback = (f"I'm in the middle of work. Put this into it now: {text}? Or keep it for "
                    "the end of the call?")
    elif now:
        readback = f"I'll put this in my chat now: {text}. OK?"
    else:
        readback = f"I'll keep this for the end of the call: {text}. OK?"
    held = {"id": uuid.uuid4().hex[:8], "kind": "tell", "agent": agent, "name": agent,
            "cwd": None, "task": text, "turns": 0, "at": clock.now(), "call": call_id,
            "readback": readback, "now": bool(now)}
    with _lock:
        _sweep()
        _proposals[held["id"]] = held
    return {"id": held["id"], "readback": readback, "task": text, "choose": choose,
            "expires_in": config.VOICE_CONFIRM_SECONDS}


def keep_for_later(proposal_id):
    """They chose the end of the call for an ask they first wanted now."""
    with _lock:
        held = _proposals.get((proposal_id or "").strip())
        if held:
            held["now"] = False


def pending_proposal(proposal_id):
    with _lock:
        _sweep()
        held = _proposals.get((proposal_id or "").strip())
        return dict(held) if held else None


def confirm(proposal_id):
    """Run what was proposed, once, if it was proposed and is still fresh.

    Taken out of the dictionary before anything happens, so a second yes on the same id cannot
    send the same task twice. Two phones, or a tap and a spoken yes arriving together, is the
    ordinary way that would otherwise happen.
    """
    with _lock:
        _sweep()
        held = _proposals.pop((proposal_id or "").strip(), None)
    if not held:
        # Not an exception. The voice reads this back, and "I cannot, that has expired" is a
        # better thing to hear than an error.
        log.info("voice confirm %s: nothing sent, no such proposal", (proposal_id or "")[:16])
        return {"ok": False, "said": "That proposal has expired or was never made. "
                                     "Say it again and I will put it to you afresh."}

    if held["kind"] == "tell":
        # An agent's call: what they said goes into that agent's own chat. No `agent` key in the
        # answer, because the call is that agent and must not start waiting on itself.
        from . import agentcall
        now = bool(held.get("now"))
        if now and not agentcall._busy(held["agent"]):
            log.info("voice confirm %s: told %s from its call, now, waiting", held["id"],
                     held["agent"])
            reply = agentcall.tell_and_hear(held["agent"], held["task"], held["call"])
            if reply:
                return {"ok": True, "reply": reply,
                        "said": "Put in the agent's chat, and reply is its answer. Read them the "
                                "answer in a few plain sentences, in your own voice, then go on."}
            return {"ok": True, "said": "Put in the agent's chat now. It has not answered in "
                                        "time; its answer will be in the chat. Tell them so in "
                                        "a few words and go on."}
        threading.Thread(target=agentcall.tell, args=(held["agent"], held["task"], held["call"]),
                         kwargs={"now": now}, name="call-tell", daemon=True).start()
        log.info("voice confirm %s: told %s from its call, %s", held["id"], held["agent"],
                 "now" if now else "at the end")
        if now:
            return {"ok": True, "said": "Put in the agent's chat now. Tell them in a few words, "
                                        "behind a small beat, and go on. Do not repeat what they "
                                        "said."}
        return {"ok": True, "said": "Kept for the agent; it gets it when the call ends. Tell "
                                    "them in a few words, behind a small beat, and go on with "
                                    "the call. Do not repeat what they said."}

    if held["kind"] == "stop":
        stopped = broker.stop(held["agent"])
        log.info("voice confirm %s: stop %s, %s", held["id"], held["agent"],
                 "stopped" if stopped else "nothing to stop")
        # No `agent` key: a stop is not a delegation, so the call must not start waiting on it.
        return {"ok": bool(stopped), "stopped": held["agent"],
                "said": (f"Stopped {held['agent']}." if stopped
                         else f"{held['agent']} had nothing running to stop.")}

    # The conversation is read here rather than at the proposal, so what goes out is the call as
    # it stood when they said yes. Only the count is logged. The turns are their own words and the
    # daemon log is not the place for them.
    lines = _attached_lines(held.get("turns"))
    message = _with_turns(held["task"], lines)
    carried = f" with {len(lines)} turns" if lines else ""

    if held["kind"] == "new":
        rec = registry.create(name=held["name"], cwd=held["cwd"])
        outcome = broker.send(rec["id"], message, wake=True)
        log.info("voice confirm %s: made %s and sent to it, %s, %d turns", held["id"], rec["id"],
                 outcome or "failed", len(lines))
        return {"ok": True, "agent": rec["id"], "created": True, "outcome": outcome or "failed",
                "said": f"Made {rec['id']} and gave it the task{carried}."}

    outcome = broker.send(held["agent"], message, wake=True)
    # The line that was missing. Twice in one night the voice said "sent" and nothing had been,
    # and the log could not settle it either way. Now a send and a failure both leave a line,
    # with the task cut short because the log is not a copy of their work.
    log.info("voice confirm %s: %s %s, task %r, %d turns", held["id"],
             "sent to" if outcome else "nothing sent to", held["agent"],
             str(held["task"])[:LOG_TASK], len(lines))
    if not outcome:
        return {"ok": False, "agent": held["agent"],
                "said": f"{held['agent']} did not take it. Nothing was sent."}
    said = (f"Sent to {held['agent']}{carried}." if outcome == "sent"
            else f"{held['agent']} is asleep, so it is waiting for it{carried}.")
    return {"ok": True, "agent": held["agent"], "outcome": outcome, "said": said}


def cancel(proposal_id):
    with _lock:
        gone = _proposals.pop((proposal_id or "").strip(), None)
    return {"ok": bool(gone), "said": "Dropped." if gone else "There was nothing to drop."}


# -- the tools -------------------------------------------------------------------
# One route carries all of them, so the browser forwards whatever the model asked for without
# knowing what any of it means. Every answer is compact text or a small object, because it is
# going back into a speech model's context and then out of a person's mouth.

def _fleet_status():
    rows = []
    for rec in _fleet():
        term = broker.get(rec["id"]) or broker.chat(rec["id"])
        up = broker.awake(rec["id"])
        bits = [rec["id"], "awake" if up else "asleep"]
        if up:
            from . import live
            bits.append("working" if live.working(rec, term) else "idle")
            waiting = len(broker.pending_asks(rec["id"]))
            if waiting:
                bits.append(f"{waiting} ask waiting" if waiting == 1 else f"{waiting} asks waiting")
        said = _one_line(transcript.last_message(rec), 30)
        if said:
            bits.append(f"last said: {said}")
        rows.append(", ".join(bits))
    return "\n".join(rows) or "No agents are registered."


# How much of one final answer the front desk is handed. Whole answers are the point, so the cap is a
# guard against a runaway reply, not a summary.
ANSWER_CHARS = 6000
# All the answers of one history call together, so ten long ones cannot flood the backend.
HISTORY_CHARS = 16000


def _is_prompt(entry):
    """A message the owner (or a cord, a job, the front desk) actually sent: a user row that is not a tool's
    result and not one of Claude Code's own notes."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        return "tool_result" not in kinds and any(
            b.get("type") == "text" and (b.get("text") or "").strip()
            for b in content if isinstance(b, dict))
    return False


def _prompt_text(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text")


def _turns_of(rec):
    """The conversation as turns: each is what was asked, the last text the agent wrote in reply,
    and the last tool it called. Read from the session file, oldest first."""
    path = transcript.session_file(rec)
    if path is None:
        return []
    turns = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if _is_prompt(entry):
                    turns.append({"asked": _prompt_text(entry), "at": entry.get("timestamp"),
                                  "answer": "", "tool": None, "tools": 0})
                    continue
                if entry.get("type") != "assistant" or entry.get("isSidechain") or not turns:
                    continue
                for block in (entry.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and (block.get("text") or "").strip():
                        # The newest text wins: the final answer is the last thing it wrote.
                        turns[-1]["answer"] = block["text"].strip()
                        turns[-1]["answered_at"] = entry.get("timestamp")
                    elif block.get("type") == "tool_use":
                        turns[-1]["tools"] += 1
                        turns[-1]["tool"] = {"name": block.get("name"),
                                             "input": _one_line(str(block.get("input") or ""), 25),
                                             "at": entry.get("timestamp")}
                        # Text before this tool call was mid-turn talk, not the answer.
                        turns[-1]["answer_before_tool"] = turns[-1]["answer"]
                        turns[-1]["answer"] = ""
    except OSError:
        return []
    return turns


def _agent_history(rec, count):
    """The agent's last `count` final answers, whole, and the turn it is on if it is running.

    A final answer is the last text the agent wrote in a turn, after its last tool. What it said
    on the way, and the tools, are left out: The front desk needs what it told the owner, not how it got
    there. The one exception is a turn still running, where the tool in hand is the news.
    """
    count = max(1, min(int(count or 3), 10))
    turns = _turns_of(rec)
    busy = live.working(rec, broker.get(rec["id"]) or broker.chat(rec["id"]))
    running = turns.pop() if (busy and turns) else None

    done = [t for t in turns if t["answer"]][-count:]
    answers, used = [], 0
    for t in reversed(done):
        text = t["answer"][:ANSWER_CHARS]
        if used + len(text) > HISTORY_CHARS and answers:
            break
        used += len(text)
        answers.append({"asked": _one_line(t["asked"], 30), "when": _when_iso(t.get("answered_at")),
                        "answer": text})
    answers.reverse()

    out = {"answers": answers or f"{rec['id']} has not answered anything yet."}
    if running:
        now = {"asked": _one_line(running["asked"], 30), "since": _when_iso(running.get("at")),
               "tools_run": running["tools"]}
        said = running["answer"] or running.get("answer_before_tool") or ""
        if said:
            now["said_so_far"] = _one_line(said, 80)
        if running["tool"]:
            now["running_tool"] = (f"{running['tool']['name']}: {running['tool']['input']}")
        out["still_running"] = now
    return out


def _when_iso(ts):
    """An ISO timestamp from the session file as local HH:MM, or nothing."""
    if not ts:
        return ""
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime(
            "%a %d %b %H:%M")
    except ValueError:
        return ""


def _pending_asks():
    out = []
    for rec in _fleet():
        if not broker.awake(rec["id"]):
            continue
        for frame in broker.pending_asks(rec["id"]):
            if frame.get("kind") == "question":
                titles = [q.get("question") or q.get("header") or "a question"
                          for q in (frame.get("questions") or [])]
                out.append({"agent": rec["id"], "ask_id": frame.get("id"), "kind": "question",
                            "asking": _one_line("; ".join(titles), 40)})
            else:
                out.append({"agent": rec["id"], "ask_id": frame.get("id"), "kind": "permission",
                            "asking": f"may it run {frame.get('tool')}: "
                                      f"{_one_line(frame.get('summary'), 25)}"})
    return out or "Nobody is waiting on you."


def _insights():
    """What the Insights screen shows, said rather than drawn."""
    agents = []
    for rec in _fleet():
        facts = transcript.summary(rec)
        agents.append({"agent": rec["id"], "turns": facts["turns"],
                       "tokens": facts["context_tokens"],
                       "last_seen": _when(rec.get("last_activity"))})
    jobs = []
    for job in cron.all():
        if not in_view(registry.get(job["agent"])):
            continue
        jobs.append({"job": job["id"], "agent": job["agent"], "task": _one_line(job["task"], 12),
                     "next": ("when files change" if job.get("watch") else _when(job.get("next")))
                             if job.get("enabled") else "off",
                     "last": job.get("last_outcome") or "never run"})
    out = {"agents": agents, "jobs": jobs}
    # The network reading, only if it was already taken. Measuring it costs half a second, and
    # half a second of silence in a conversation is a long time.
    snap = network.cached()
    out["network"] = (snap.get("verdict") or {}).get("say") if snap else "not measured recently"
    return out


def _page_data(rec, name):
    got = data.resolve(rec, name)
    text = got.get("text")
    if text is None and got.get("value") is not None:
        text = json.dumps(got["value"], separators=(",", ":"))
    text = text or ""
    cut = len(text) > MAX_DATA
    return {"agent": rec["id"], "name": name, "source": got.get("source"),
            "age_seconds": got.get("age"),
            "data": text[:MAX_DATA] + (" ... cut here" if cut else ""),
            "note": f"cut at {MAX_DATA} characters, {len(text)} in all" if cut else None}


# -- reading a file --------------------------------------------------------------
# The voice may read a file out. That is a door, so it has two locks and they are independent of
# each other: the path has to resolve inside a root the owner named, and no part of it may be one
# of the names below whatever root it sits under. A symlink is followed before either check, so a
# link out of a root is out of the root and a link called something harmless does not launder the
# name it points at.
#
# It is deliberately not a search. The voice is given one path at a time and reads a page of it,
# because a file read aloud is a page, and because a tool that can walk a tree is a tool that can
# walk into somewhere it should not.

# Refused wherever they appear, as a whole path component. These are the four places a credential
# lives on this machine, plus the one file that holds the OpenAI key itself.
DENY_NAMES = frozenset({"secrets.env", ".env", ".creds", ".ssh", ".gnupg"})

# Refused as two components in a row, because var/secrets is a folder rather than a name and the
# files inside it are called ordinary things.
DENY_PAIR = ("var", "secrets")

# One page. The voice reads the start of a file and offers the rest, the way a person would.
READ_LINES = 200

# The most text one answer may carry, whatever the line count says. A long line is still a long
# line, and this is going into a speech model's context.
READ_BYTES = 16384

# How much of the front of a file decides whether it is text. A NUL in here and it is not.
SNIFF_BYTES = 8192

NOT_ALLOWED = ("That path is not allowed. I can only read inside the folders you named, "
               "and never a credential.")


def _denied_name(parts):
    """True when any part of a path is a name that is refused wherever it appears."""
    lowered = [str(part).lower() for part in parts]
    if any(part in DENY_NAMES for part in lowered):
        return True
    return any(pair == DENY_PAIR for pair in zip(lowered, lowered[1:]))


def _under_root(resolved):
    """True when the resolved path sits inside one of the roots the owner named."""
    for root in config.VOICE_READ_ROOTS:
        try:
            base = Path(root).expanduser().resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            return True
    return False


def allowed_file(raw):
    """The resolved path, or nothing when it may not be read.

    The name check runs on what was asked for and on what it resolved to. A symlink called
    notes.txt pointing at .ssh/id_rsa fails the second; a real folder called .ssh fails the first
    even if nothing resolves at all.
    """
    given = (raw or "").strip()
    if not given:
        return None
    asked = Path(given).expanduser()
    if _denied_name(asked.parts):
        return None
    try:
        resolved = asked.resolve()
    except (OSError, RuntimeError):
        return None
    if _denied_name(resolved.parts):
        return None
    if not _under_root(resolved):
        return None
    if _view.get():
        for rec in registry.all(include_hidden=True):
            if in_view(rec) or not rec.get("cwd"):
                continue
            try:
                home = Path(rec["cwd"]).expanduser().resolve()
            except OSError:
                continue
            if resolved == home or home in resolved.parents:
                return None
    return resolved


def _count_lines(path):
    """How many lines the file has, read in chunks so a large one costs no memory."""
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            total += chunk.count(b"\n")
    return total


def read_file(path, start=1, lines=None):
    """A page of one text file: {path, start, end, total_lines, text}.

    Paged rather than whole. The voice is told to read the start and offer more, because a file
    read aloud is a page at a time and because a whole file in a speech model's context is a
    whole file being paid for on every turn afterwards.
    """
    resolved = allowed_file(path)
    if not resolved:
        return NOT_ALLOWED
    if not resolved.is_file():
        return f"There is no file at {path}."

    try:
        with open(resolved, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError as exc:
        return f"That file could not be read: {exc.strerror or exc}"
    if b"\x00" in head:
        return f"{resolved.name} is not a text file, so I cannot read it out."

    try:
        start = max(1, int(start or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        want = int(lines) if lines else READ_LINES
    except (TypeError, ValueError):
        want = READ_LINES
    want = max(1, min(want, READ_LINES))

    kept = []
    spent = 0
    last = start - 1
    cut = False
    try:
        total = _count_lines(resolved)
        with open(resolved, "r", errors="replace") as fh:
            for number, line in enumerate(fh, start=1):
                if number < start:
                    continue
                if len(kept) >= want:
                    break
                size = len(line.encode("utf-8", "replace"))
                if spent + size > READ_BYTES and kept:
                    cut = True
                    break
                kept.append(line)
                spent += size
                last = number
    except OSError as exc:
        return f"That file could not be read: {exc.strerror or exc}"

    text = "".join(kept)
    if len(text.encode("utf-8", "replace")) > READ_BYTES:
        text = text.encode("utf-8", "replace")[:READ_BYTES].decode("utf-8", "ignore")
        cut = True
    out = {"path": str(resolved), "start": start, "end": max(last, start - 1),
           "total_lines": total, "text": text}
    if cut:
        out["note"] = f"cut at {READ_BYTES} characters. Ask for the next page from line {last + 1}."
    elif last < total:
        out["note"] = f"there are {total - last} more lines. Ask for them from line {last + 1}."
    return out


def _task_answer(name, args):
    """task_tick and task_later: their spoken answer about one of their own tasks."""
    from . import phone, tasks
    if current_view() == "demo":
        return "Their tasks are not shown in the demo view."
    task = tasks.find_mine(args.get("task"), _side())
    if not task:
        return (f"There is no single open task of theirs that fits {args.get('task')!r}. Ask them "
                "which one.")
    if name == "task_tick":
        tasks.tick(task["id"], "desk")
        return {"ok": True, "said": f"Ticked: {task['title']}."}
    if name == "task_drop":
        tasks.dismiss(task["id"], "desk")
        return {"ok": True, "said": f"Dropped: {task['title']}."}
    if name == "task_keep":
        tasks.asked(task["id"])
        return {"ok": True, "said": f"Kept: {task['title']}. They will not be asked again."}
    if name == "task_move":
        import datetime as _dt
        raw = " ".join(str(args.get("due") or "").split())
        for shape in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                when = _dt.datetime.strptime(raw, shape)
                break
            except ValueError:
                when = None
        if not when:
            return "Give the new time as YYYY-MM-DD HH:MM, their local time."
        if len(raw) == 10:
            when = when.replace(hour=9)
        tasks.move(task["id"], when.timestamp(), "desk")
        return {"ok": True, "said": f"Moved {task['title']} to {when.strftime('%A %d %B, %H:%M')}."}
    words = " ".join(str(args.get("words") or "").split())
    if not words:
        return "What did they say? Pass their words."
    alarm = phone.alarm_for_task(task["id"])
    if not alarm or not alarm.get("by") or alarm["by"] == "desk":
        return {"ok": True, "task_id": task["id"],
                "said": "You set this alarm. Work out the time from their words, read it back, "
                        f"and call set_alarm with task_id {task['id']}."}
    to = phone._setter(alarm)
    broker.send(to, f"[the owner, on a call with the front desk, about the alarm for their task "
                    f"\"{task['title']}\", task {task['id']}. They stopped it and have not done it. "
                    f"Set the alarm again from their words, with this task_id] {words}", wake=True)
    return {"ok": True, "agent": to, "said": f"Passed to {to}, which sets it again."}


def _run(name, args=None):
    """One tool call in, one answer out. Anything unknown says so rather than failing.

    The browser forwards whatever the model asked for, so this is where a name is checked. An
    unknown name is a model mistake and not a fault of the console, and saying "no such tool"
    lets the voice recover in the same breath.
    """
    args = args if isinstance(args, dict) else {}
    name = (name or "").strip()

    if name == "fleet_status":
        return _fleet_status()

    if name == "end_call":
        return {"ok": True, "hangup": True,
                "said": "Say one short goodbye now and nothing else. The call ends after it."}

    if name == "pending_asks":
        return _pending_asks()

    if name == "insights":
        return _insights()

    if name in ("calls", "recall", "turns") and _forgets_past():
        return ("Past calls are off in the demo view, because they mention agents outside it. "
                "Only this call counts.")

    if name == "calls":
        return calls(args.get("n") or 12)

    if name == "read_file":
        return read_file(args.get("path"), args.get("start"), args.get("lines"))

    if name == "recall":
        return memory.recall(args.get("keywords"), args.get("about"))

    if name == "turns":
        return memory.turns(args.get("from"), args.get("to"))

    if name in ("agent_history", "agent_recap", "agent_pages", "page_data", "answer_ask"):
        rec = resolve(args.get("agent"))
        if not rec:
            return f"There is no agent called {args.get('agent')!r}."
        if name == "agent_history":
            return {"resolved_to": rec["id"], "history": _agent_history(rec, args.get("n") or 3)}
        if name == "agent_recap":
            text = transcript.recap(rec)
            return {"resolved_to": rec["id"],
                    "recap": _words(text, RECAP_WORDS) if text else
                             f"{rec['id']} has written no recap."}
        if name == "agent_pages":
            return {"resolved_to": rec["id"],
                    "pages": [p["title"] for p in config.pages(rec["id"])],
                    "data": [f["name"] for f in data.available(rec)["files"]]}
        if name == "page_data":
            try:
                return _page_data(rec, (args.get("name") or "").strip())
            except KeyError as exc:
                # A KeyError prints its own quotes, and a sentence read aloud with quotation
                # marks around it is a sentence the voice stumbles over.
                return str(exc.args[0]) if exc.args else "no such data"
            except ValueError as exc:
                return str(exc)
            except OSError as exc:
                return f"That data could not be read: {exc}"
        # answer_ask. Their own yes, spoken, so it needs no proposal of its own: the ask is already
        # a question they were asked and this is the answer to it.
        ask_id = (args.get("ask_id") or "").strip()
        allow = bool(args.get("allow"))
        if not ask_id:
            return "Which ask? Say the id."
        ok = broker.answer(rec["id"], ask_id, allow=allow)
        word = "allowed" if allow else "denied"
        return {"resolved_to": rec["id"], "ok": ok,
                "said": f"{rec['id']} {word}." if ok
                        else "That ask has gone, it was answered somewhere else."}

    if name == "set_alarm":
        from . import phone
        try:
            return phone.alarm(args.get("hour"), args.get("minute"), args.get("label"), "desk",
                               args.get("task_id"))
        except ValueError as exc:
            return str(exc)

    if name == "inbox":
        # Read only. The opening says at most three items; this is the rest, when they ask.
        items = _brief()
        return {"waiting": inbox.waiting_view(items),
                "note": ("Nothing is waiting." if not items else
                         "Say at most three in a turn, one sentence each. If an item's time "
                         "has passed, say it was missed.")}

    if name == "his_tasks":
        from . import tasks
        if current_view() == "demo":
            return "Their tasks are not shown in the demo view."
        return tasks.for_desk(_side())

    if name in ("task_tick", "task_later", "task_move", "task_drop", "task_keep"):
        return _task_answer(name, args)

    if name == "propose":
        try:
            return propose(args.get("agent"), args.get("task"), turns=args.get("turns"),
                           name=args.get("name"), cwd=args.get("cwd"),
                           action=args.get("action") or "task")
        except KeyError as exc:
            return str(exc.args[0]) if exc.args else "no such agent"
        except ValueError as exc:
            return str(exc)

    if name == "confirm":
        return confirm(args.get("id"))

    if name == "cancel":
        return cancel(args.get("id"))

    return f"no such tool: {name}"


# -- what the tools did ----------------------------------------------------------
# One line per tool call. Twice in one night the voice said "sent" without calling confirm, and
# the log could not say so either way, because nothing wrote a tool call down. Now it does.
#
# The name, the agent it landed on, and how it went. Never the answer: a tool answer is a page of
# the owner's own work and the log is not the place for it. The task is the one exception and it
# is cut to LOG_TASK characters, because a proposal with no hint of what it was is a line that
# proves a send happened and nothing else.

LOG_TASK = 80


def _tool_agent(args, result):
    """Which agent a tool call landed on, as the answer itself reports it."""
    if isinstance(result, dict):
        found = result.get("resolved_to") or result.get("agent")
        if found:
            return str(found)
    return str(args.get("agent") or "") or None


# How a refused answer opens. Every tool here answers a refusal in a sentence rather than by
# raising, because the sentence is read aloud, so this is the only way to tell one apart.
FAILED_STARTS = ("There is no", "That path is not allowed", "That file could not be read",
                 "That data could not be read", "no agent called", "a proposal needs",
                 "a new agent needs", "give a full path")


def _tool_outcome(result):
    """ok, failed, or no such tool. Read from the answer rather than from an exception."""
    if isinstance(result, str):
        if result.startswith("no such tool"):
            return "no such tool"
        if result.startswith(FAILED_STARTS) or "is not a text file" in result:
            return "failed"
        return "ok"
    if isinstance(result, dict) and "ok" in result:
        return "ok" if result.get("ok") else "failed"
    return "ok"


def call(name, args=None):
    """One tool call, run and written down.

    The work is in `_run`. This is here so that every tool call leaves one line in the daemon
    log whatever happened to it, including the ones that fail and the ones that do not exist.
    """
    args = args if isinstance(args, dict) else {}
    name = (name or "").strip()
    try:
        result = _run(name, args)
    except Exception as exc:
        log.info("voice tool %s: failed, %s", name or "(none)", type(exc).__name__)
        raise
    agent = _tool_agent(args, result)
    extra = ""
    if name == "propose" and args.get("task"):
        extra = f", task {str(args['task'])[:LOG_TASK]!r}"
        if isinstance(result, dict):
            # The count and never the turns. What was said on the call is theirs, and one line in
            # the daemon log saying how much of it went out is the whole of what is useful here.
            extra += f", {result.get('turns') or 0} turns"
    elif name == "recall" and isinstance(result, dict):
        # How many and never which. The memories are their own words.
        extra = f", {len(result.get('memories') or [])} memories"
    elif name == "turns" and isinstance(result, dict):
        extra = f", {len(result.get('turns') or [])} turns"
    elif name == "read_file" and isinstance(result, dict):
        extra = f", lines {result.get('start')} to {result.get('end')} of {result.get('total_lines')}"
    log.info("voice tool %s%s: %s%s", name or "(none)",
             f" -> {agent}" if agent else "", _tool_outcome(result), extra)
    return result


# The tools as the model is told about them. One sentence each, because the sentence is what the
# model chooses on, and a tool the model picks for the wrong reason is worse than one it never
# picks at all.
TOOLS = [
    {"name": "fleet_status",
     "description": "Every agent, whether it is awake, whether it is working, how many asks are "
                    "waiting on the owner, and the last thing it said.",
     "parameters": {"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False}},
    {"name": "agent_history",
     "description": "One agent's last final answers to the owner, whole, each with what was asked. "
                    "If the agent is running right now, also what it is on: the question, what "
                    "it has said so far and the tool it is running.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string", "description": "The agent id or name, however it was said."},
         "n": {"type": "integer", "description": "How many final answers, newest last. Three "
                                                  "by default, ten at most."}},
         "required": ["agent"], "additionalProperties": False}},
    {"name": "agent_recap",
     "description": "The newest recap one agent wrote about where it got to.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string"}}, "required": ["agent"], "additionalProperties": False}},
    {"name": "set_alarm",
     "description": "Set an alarm on the owner's phone, rung by Ergo itself, for the next time this "
                    "hour and minute comes round. Work out the time from what they said, read it "
                    "back in one line, then set it. It reaches the phone within half a minute.",
     "parameters": {"type": "object", "properties": {
         "hour": {"type": "integer", "description": "0 to 23, their local time."},
         "minute": {"type": "integer", "description": "0 to 59."},
         "label": {"type": "string", "description": "A few words for the alarm, or empty."},
         "task_id": {"type": "string", "description": "The task on their list this alarm is for, "
                                                      "when it is one they already have. Empty "
                                                      "makes a task with the label."}},
         "required": ["hour", "minute"], "additionalProperties": False}},
    {"name": "task_tick",
     "description": "Tick one of the owner's own tasks off their list, after they said out loud that it "
                    "is done. For example when you asked about an alarm they stopped.",
     "parameters": {"type": "object", "properties": {
         "task": {"type": "string", "description": "The task's id, or its title or a part of "
                                                   "it."}},
         "required": ["task"], "additionalProperties": False}},
    {"name": "inbox",
     "description": "What else is waiting for the owner: every untold item their agents left, most "
                    "urgent first, with when it was left and the time of its event. Read only. "
                    "Use it when they ask what else there is.",
     "parameters": {"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False}},
    {"name": "his_tasks",
     "description": "The owner's own open tasks: what is due today or overdue, the others, and "
                    "pairs that look like the same task. Each with its id. Only when they ask: "
                    "then take one task at a time.",
     "parameters": {"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False}},
    {"name": "task_move",
     "description": "Give one of their tasks a new time, after they said when out loud.",
     "parameters": {"type": "object", "properties": {
         "task": {"type": "string", "description": "The task's id, or its title or a part of "
                                                   "it."},
         "due": {"type": "string", "description": "YYYY-MM-DD HH:MM in their local time, or "
                                                  "YYYY-MM-DD for 09:00 that day."}},
         "required": ["task", "due"], "additionalProperties": False}},
    {"name": "task_drop",
     "description": "Take one of their tasks off their list, after they said to drop it, or that it "
                    "is a double of another.",
     "parameters": {"type": "object", "properties": {
         "task": {"type": "string", "description": "The task's id, or its title or a part of "
                                                   "it."}},
         "required": ["task"], "additionalProperties": False}},
    {"name": "task_keep",
     "description": "They want to keep a task you asked about as done or a double. They are not "
                    "asked about it again.",
     "parameters": {"type": "object", "properties": {
         "task": {"type": "string", "description": "The task's id, or its title or a part of "
                                                   "it."}},
         "required": ["task"], "additionalProperties": False}},
    {"name": "task_later",
     "description": "They have not done a task an alarm was for. Ask them when, then pass their exact "
                    "words here. They go to the agent that set the alarm, which sets it again. "
                    "If the answer says you set it, set the alarm again yourself with the same "
                    "task_id.",
     "parameters": {"type": "object", "properties": {
         "task": {"type": "string", "description": "The task's id, or its title or a part of "
                                                   "it."},
         "words": {"type": "string", "description": "What they said, word for word."}},
         "required": ["task", "words"], "additionalProperties": False}},
    {"name": "pending_asks",
     "description": "Every agent stopped and waiting on a yes or a no from the owner, with the id of "
                    "each ask.",
     "parameters": {"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False}},
    {"name": "answer_ask",
     "description": "Answer one waiting ask with the owner's own yes or no. They must have just said "
                    "it out loud.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string"},
         "ask_id": {"type": "string"},
         "allow": {"type": "boolean", "description": "True for yes, false for no."}},
         "required": ["agent", "ask_id", "allow"], "additionalProperties": False}},
    {"name": "insights",
     "description": "Turns, tokens and last seen per agent, the schedule with the next run and "
                    "the last outcome of each job, and the network verdict.",
     "parameters": {"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False}},
    {"name": "agent_pages",
     "description": "The pages one agent has built and the data names it publishes.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string"}}, "required": ["agent"], "additionalProperties": False}},
    {"name": "page_data",
     "description": "The data behind one of an agent's pages, by name.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string"}, "name": {"type": "string"}},
         "required": ["agent", "name"], "additionalProperties": False}},
    {"name": "calls",
     "description": "Turns from previous voice calls, oldest first.",
     "parameters": {"type": "object", "properties": {
         "n": {"type": "integer"}}, "required": [], "additionalProperties": False}},
    {"name": "recall",
     "description": "Search every earlier voice call for something the owner asks about that is "
                    "not in front of you. Returns the remembered turns that matter with the "
                    "turns around them, each with its number n, its date and who said it. The "
                    "matches themselves are marked hit.",
     "parameters": {"type": "object", "properties": {
         "keywords": {"type": "string",
                      "description": "Words that would appear in the turns you want: names, "
                                     "places, things, numbers. Several, space separated."},
         "about": {"type": "string",
                   "description": "One sentence of what you are trying to remember."}},
         "required": ["keywords", "about"], "additionalProperties": False}},
    {"name": "turns",
     "description": "Read turns of earlier voice calls by number, from n to n inclusive, sixty "
                    "at most. Use it after recall to read around where the conversation "
                    "clusters, by the turn numbers recall gave you. Notes are the console's "
                    "own lines and are marked note.",
     "parameters": {"type": "object", "properties": {
         "from": {"type": "integer", "description": "The first turn number."},
         "to": {"type": "integer", "description": "The last turn number."}},
         "required": ["from", "to"], "additionalProperties": False}},
    {"name": "propose",
     "description": "Write down a task for an agent, or a stop of what it is doing, and get back "
                    "the sentence to read aloud. This runs nothing. Use agent 'new' with a name "
                    "and a cwd to propose a new agent. Use action 'stop' to halt an agent's "
                    "current turn; it keeps the agent awake and needs no task.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string", "description": "An agent id or name, or 'new'."},
         "task": {"type": "string", "description": "What the agent should do, in full. "
                                                  "Leave empty for a stop."},
         "action": {"type": "string", "enum": ["task", "stop"],
                    "description": "task by default. stop halts the agent's current turn."},
         "turns": {"type": "integer",
                   "description": "How many recent turns of this call the agent should read to "
                                  "understand the ask. None by default, a hundred at most. "
                                  "Enough for the agent to understand the ask and no more."},
         "name": {"type": "string", "description": "For agent 'new', what to call it."},
         "cwd": {"type": "string", "description": "For agent 'new', the folder it works in."}},
         "required": ["agent"], "additionalProperties": False}},
    {"name": "confirm",
     "description": "Run a proposal, using the id propose gave you. Only after the owner has said "
                    "yes out loud.",
     "parameters": {"type": "object", "properties": {
         "id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}},
    {"name": "cancel",
     "description": "Drop a proposal the owner turned down.",
     "parameters": {"type": "object", "properties": {
         "id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}},
    {"name": "read_file",
     "description": "Read a page of one text file by its full path. Read the shape first: start "
                    "at line 1 with a small number of lines, say what the file looks like, and "
                    "ask before reading more. The answer says how many lines are left and which "
                    "line to ask for next. Only inside the folders the owner allows, never a "
                    "credential, and 200 lines at most in one call.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "The full path, starting with / or ~."},
         "start": {"type": "integer", "description": "The first line to read. One by default."},
         "lines": {"type": "integer",
                   "description": "How many lines. Two hundred by default and at most."}},
         "required": ["path"], "additionalProperties": False}},
    {"name": "end_call",
     "description": "Hang up the call. Use it when the owner says goodbye, asks you to hang up, or "
                    "plainly has nothing more, so they never have to reach for the phone. After it "
                    "returns, say one short goodbye; the call ends when you finish.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


# -- minting the client secret ---------------------------------------------------

def _safe(text, key):
    """Anything about to be shown to a person, with the key taken out of it.

    Belt and braces. Nothing here is meant to put the key in a sentence, and one thing did: a key
    with a trailing carriage return made urllib raise a ValueError whose text was the whole
    Authorization header, and that sentence went to the browser and onto the screen. Every string
    that leaves this module for a person now goes through here first.
    """
    said = str(text)
    if not key or len(key) < 9:
        return said
    # Three spellings, because the one that leaked was none of them plainly. urllib reported the
    # header as a bytes repr, so the carriage return had become a backslash and an r and a plain
    # substring test missed it.
    for shape in (key, key.strip(), repr(key)[1:-1], repr(key.encode())[2:-1]):
        if shape and shape in said:
            said = said.replace(shape, "the key")
    return said


def post_json(url, payload, key, timeout=None):
    """One POST to OpenAI. Replaced wholesale in tests, which is why it is a module function.

    The key goes in a header and nowhere else. Nothing in this function prints, logs or returns
    it, and every error that leaves it is scrubbed before it does.
    """
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.VOICE_MINT_TIMEOUT) as answer:
            return json.loads(answer.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read().decode()).get("error", {}).get("message") or ""
        except Exception:
            said = ""
        raise RuntimeError(
            _safe(f"OpenAI refused the session ({exc.code}): {said}".strip(), key)) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(_safe(f"OpenAI could not be reached: {exc.reason}", key)) from None
    except ValueError:
        # The one error whose text is the request rather than the answer. A key that is not a
        # usable header value makes http.client print the whole Authorization header, so this
        # says what is wrong in its own words and never repeats a character of it.
        raise RuntimeError("The OpenAI key is not a usable header value. "
                           "Check var/secrets.env for stray spaces or line endings.") from None
    except OSError as exc:
        raise RuntimeError(
            _safe(f"OpenAI answered with something unreadable: {exc}", key)) from None


def session_payload(sdp, resume=None, back=None, sent=None, brief=None):
    """The session as OpenAI is asked to make it, with the browser's connection offer in it.

    `resume` is the turns of a paused call, and it changes three things: the front of house prompt
    is told not to greet them twice, the backend briefing carries this call so far, and the session
    starts holding the turns themselves as conversation items. `back` is the agent that woke the
    call, when an agent is what woke it, and both prompts then say what arrived and to report it
    without being asked.

    Two prompts, not one, because GPT-Live is two models. The live one holds the conversation and
    decides when to ask for help, so it gets the short front of house prompt. The backend
    Responses model does the thinking and runs the tools, so it gets the full briefing. The docs
    are explicit about the split: conversation style in front, business rules and tool workflows
    behind.

    `store` is the Live session's own switch and it is set off. It is the only switch the API
    offers: it decides whether the recording is kept for forking, and off means OpenAI keeps no
    application state for this call. There is no `store` on `delegation.responses`, because Live
    supplies the context and runs the backend itself, and none on the transcript events, which
    travel to the browser rather than being kept.
    """
    carried = resume_input(resume) if resume else []
    # A resume with no turns yet is still a resume when it carries news or a delegation.
    resuming = bool(carried) or bool(came_back(back)) or bool(delegated_lines(sent))
    # What the inbox still has to say, read at this moment, so a resume reads only what is
    # still untold.
    if brief is None:
        brief = _brief()
    session = {
        "model": config.VOICE_MODEL,
        # Front of house. Immutable once the session starts, which is why it is assembled in
        # full before the call rather than added to during it.
        "instructions": live_prompt(resuming=resuming, back=back, sent=sent, brief=brief),
        # WebRTC settles the audio format in the SDP, so naming a format here is refused.
        # The voice is the one thing left to say, and it cannot be changed once she speaks.
        "audio": {"output": {"voice": memory.voice_for()}},
        "delegation": {
            "type": "responses",
            "responses": {
                "model": config.VOICE_BACKEND,
                "instructions": (resume_brief(resume, back, sent, brief) if resuming
                                 else briefing(brief=brief)),
                "tools": ([{"type": "web_search"}] if config.VOICE_WEB_SEARCH else [])
                         + [dict(tool, type="function") for tool in TOOLS],
                "tool_choice": "auto",
                # One tool at a time. Every one of these reads the fleet, and a proposal read
                # back beside three other answers is a proposal they will not hear.
                "parallel_tool_calls": False,
            },
        },
        # Nothing is kept at OpenAI. This conversation is about their fleet, their schedule and
        # their work, and the only copy that should exist is the one in var/voice.
        "store": False,
    }
    if carried:
        # The turns of the call they paused, so the new session opens holding the conversation
        # rather than a description of it. Left out entirely on a first call: an empty `input` is
        # not the same thing as no `input`.
        session["input"] = carried
    return {"session": session, "transport": {"type": "webrtc", "sdp": sdp}}


def start_session(caller=None, fresh=True):
    """Say whether a call may be made, and on what terms. Calls OpenAI not at all.

    `caller` names an agent's call, when the browser knows it is answering one. Without it, a
    fresh call made while an agent's ring is up is that agent's call. Either way the answer then
    carries `caller`: who it is, its opening line and the recording of it, so the browser plays
    the agent's line at once rather than the front desk's greeting. Nothing is answered here; the offer
    does that. A the front desk call's answer is exactly what it was.

    This is the door, and it is a separate step from opening the call on purpose: it answers
    before the browser asks for the microphone, so a console with no key says so rather than
    prompting for a microphone it is about to have no use for.

    Raises PermissionError with no key and RuntimeError over the daily budget. The caller turns
    those into status codes.
    """
    if not config.OPENAI_KEY:
        raise PermissionError(
            "There is no OpenAI key. Put OPENAI_API_KEY in var/secrets.env and restart the daemon.")

    left = budget()
    if left["left_minutes"] <= 0:
        raise RuntimeError(
            f"The voice has used its {left['daily_minutes']:g} minutes for today. "
            "It comes back at midnight.")

    from . import agentcall
    who = agentcall.peek(caller, fresh)
    out = {
        "ok": True,
        "model": config.VOICE_MODEL,
        "voice": who["voice"] if who else memory.voice_for(),
        "backend": config.VOICE_BACKEND,
        "budget": left,
        # The browser runs its own clock off this and warns two minutes before the end.
        "session_minutes": config.VOICE_SESSION_MINUTES,
        # Two numbers the browser counts with, because the browser is the only thing that hears
        # the silence. The first pauses a call that is waiting on an agent; the second decides
        # where the call log cuts one line and starts the next, and nothing else.
        "wait_pause_seconds": config.VOICE_WAIT_PAUSE_SECONDS,
        "turn_gap_ms": config.VOICE_TURN_GAP_MS,
        # Minutes with nobody speaking before the call hangs up. The browser counts it, for the
        # same reason as the two above.
        "silence_minutes": memory.silence_minutes(),
    }
    if who:
        out["caller"] = who
    return out


def open_call(sdp, resume=None, back=None, sent=None, timing=None, caller=None):
    """Swap the browser's connection offer for OpenAI's answer, and start the session doing it.

    `resume` is the turns of a call the owner paused. A paused call is closed at OpenAI, so going
    again is a new session, and the turns are what make the two read as one conversation. `back`
    says which agents came back and have not been told yet, one fact or a list. `sent` is every
    agent delegated on this call with when, so the prompts can say how each one stands now.

    `report` in the answer is the sentence for what came back, or nothing. The browser hands it
    to the live model as its first line, because a prompt alone did not make them speak first.
    The words are written here, so the browser never writes its own.

    The key is used here and nowhere else in the browser's path. What comes back is an address to
    connect to, not a credential: the answer is useless without the peer connection that made the
    offer, so it can travel to the browser the way any other reply does.
    """
    sdp = sdp or ""
    if not sdp.strip():
        raise ValueError("a call needs a connection offer")
    # The trailing line ending stays, and is put back if it is missing. An SDP whose last line has
    # no terminator is refused with "failed to unmarshal SDP: EOF", which reads like a truncated
    # body and is not: it is one missing newline. Stripping the offer cost half an hour to find.
    if not sdp.endswith("\n"):
        sdp += "\r\n"
    # The same two refusals as the door above, checked again. The browser asks twice and a budget
    # can run out between the two, and a check that only the front door does is not a check.
    start_session()
    # An agent's call, when the browser names one or an agent's ring is up. That call is the
    # agent's from here on, in agentcall.py; everything below is the front desk's and is unchanged.
    from . import agentcall
    theirs = agentcall.pick(caller, fresh=not resume)
    if theirs:
        return agentcall.open_call(sdp, theirs, resume, timing)
    # The small talk pack, fetched now in the background so it is fresh by the first wait.
    from . import smalltalk
    smalltalk.refresh_soon()

    brief = _brief()
    asked = time.monotonic()
    got = post_json(LIVE_URL, session_payload(sdp, resume, back, sent, brief), config.OPENAI_KEY)
    openai_ms = int((time.monotonic() - asked) * 1000)
    answer = (got.get("transport") or {}).get("sdp")
    if not answer:
        raise RuntimeError("OpenAI opened no call.")
    session_id = (got.get("session") or {}).get("id") or uuid.uuid4().hex[:12]
    # The items this session says first. Their first finished line marks them told, in the daemon.
    inbox.pushed([it["id"] for it in inbox.opening(brief)])
    # A resume is the same call going again, so it is not a second call in the count. The minutes
    # are still counted: they are charged per session as each one ends.
    spend(0.0, calls_delta=0 if resume else 1)
    # Said every call, because "nothing is kept at OpenAI" is a claim worth being able to check
    # afterwards. The key and the session config itself are never written anywhere.
    log.info("voice call %s opened on %s, backend %s, voice %s, store off%s",
             session_id, config.VOICE_MODEL, config.VOICE_BACKEND, memory.voice_for(),
             (", resumed" if resume else "") + (", with news to tell" if came_back(back) else "")
             + (f", {len(brief)} untold in the brief" if brief else "")
             + (f", {_view.get()} view" if _view.get() else ""))
    # Where the wait to connect goes: the phone's steps before the offer, then OpenAI's answer.
    # Numbers only, so the call can be made faster where it is slow.
    steps = {k: v for k, v in (timing or {}).items()
             if k in ("door_ms", "mic_ms", "gather_ms", "before_offer_ms")
             and isinstance(v, (int, float))}
    log.info("voice call %s timing: %s openai %d ms", session_id,
             " ".join(f"{k[:-3]} {int(v)} ms" for k, v in steps.items()) or "phone none,",
             openai_ms)
    return {"sdp": answer, "session_id": session_id, "model": config.VOICE_MODEL,
            "voice": memory.voice_for(), "budget": budget(),
            "session_minutes": config.VOICE_SESSION_MINUTES,
            "wait_pause_seconds": config.VOICE_WAIT_PAUSE_SECONDS,
            "turn_gap_ms": config.VOICE_TURN_GAP_MS,
            "silence_minutes": memory.silence_minutes(),
            "record": memory.record_calls(),
            "report": came_back(back),
            # The untold items this session was told to say, at most three, for the transcript
            # rows, and on a resume the line that makes them say them first. Both written here,
            # not in the browser.
            "brief": inbox.opening(brief),
            "brief_say": inbox.brief_say(brief) if resume else ""}


# -- an image they paste -----------------------------------------------------------
# The live voice cannot see. So a small vision model looks at the image, writes a few spoken
# sentences about it, and the browser hands the voice those words. The image goes to OpenAI and
# nowhere else: it is not written to disk, not logged, and not kept in the call log.

RESPONSES_URL = "https://api.openai.com/v1/responses"

# The largest image, decoded. A phone photo is well under this; anything larger is a scan or a
# mistake, and the sentence says so rather than paying to find out.
IMAGE_MAX_BYTES = 8 * 1024 * 1024

# The image kinds the vision model reads.
IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp")

DATA_URL = re.compile(r"^data:([a-z0-9.+/-]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)

# What the vision model is told, with their own words in front of it when they typed any.
LOOK = ("Describe what matters in this image for a spoken answer, in plain sentences, under 120 "
        "words. Read any text in it exactly. No lists, no markdown.")


def image_bytes(data_url):
    """The decoded size of one image data URL. Raises ValueError with a sentence to show.

    Checked here rather than at OpenAI, so a wrong file costs nothing and says what was wrong.
    """
    raw = (data_url or "").strip() if isinstance(data_url, str) else ""
    if not raw:
        raise ValueError("There is no image in that.")
    m = DATA_URL.match(raw)
    if not m:
        raise ValueError("That image did not arrive as a data URL.")
    kind = m.group(1).lower()
    if kind not in IMAGE_TYPES:
        raise ValueError("That is not an image I can read. Send a PNG, JPEG, GIF or WebP.")
    body = "".join(m.group(2).split())
    # The size from the length, before decoding, so a huge upload is refused without being
    # decoded at all.
    if len(body) * 3 // 4 > IMAGE_MAX_BYTES + 3:
        raise ValueError("That image is too large. The limit is 8 MB.")
    try:
        size = len(base64.b64decode(body, validate=True))
    except (binascii.Error, ValueError):
        raise ValueError("That image is damaged and could not be read.") from None
    if not size:
        raise ValueError("There is no image in that.")
    if size > IMAGE_MAX_BYTES:
        raise ValueError("That image is too large. The limit is 8 MB.")
    return size


def image_payload(data_url, text=None):
    """The Responses request for one image. One user message: their words and the image."""
    words = " ".join((text or "").split()) if isinstance(text, str) else ""
    prompt = (f"The owner sent this image with these words: {words}\n\n{LOOK}" if words
              else f"The owner sent this image.\n\n{LOOK}")
    return {
        "model": config.VOICE_VISION_MODEL,
        # Nothing is kept at OpenAI, the same rule as the call itself.
        "store": False,
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": data_url.strip()},
        ]}],
    }


def _answer_text(got):
    """The model's words out of a Responses answer. The raw API has no output_text field; that
    one is the SDK's, so both are read."""
    if isinstance(got, dict) and isinstance(got.get("output_text"), str):
        return got["output_text"].strip()
    parts = []
    for item in (got or {}).get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for piece in item.get("content") or []:
            if isinstance(piece, dict) and piece.get("type") == "output_text":
                parts.append(piece.get("text") or "")
    return " ".join(p.strip() for p in parts if p.strip())


def see_image(data_url, text=None):
    """What one image shows, as {"said": ...}. Raises ValueError, PermissionError or RuntimeError,
    each with a sentence the overlay shows.

    One log line per image: its size, the model, and whether it worked. Never the image and never
    the description, because both are theirs.
    """
    if not config.OPENAI_KEY:
        raise PermissionError(
            "There is no OpenAI key. Put OPENAI_API_KEY in var/secrets.env and restart the daemon.")
    size = image_bytes(data_url)
    kb = max(1, round(size / 1024))
    model = config.VOICE_VISION_MODEL
    try:
        got = post_json(RESPONSES_URL, image_payload(data_url, text), config.OPENAI_KEY,
                        timeout=config.VOICE_VISION_TIMEOUT)
        said = _answer_text(got)
        from . import openai_log
        tin, tout = openai_log.tokens_of(got)
        openai_log.record("vision", config.VOICE_VISION_MODEL, tokens_in=tin or 0, tokens_out=tout or 0)
        if not said:
            raise RuntimeError("The vision model looked and said nothing.")
    except Exception as exc:
        log.info("voice image %d kB, model %s, failed", kb, model)
        raise RuntimeError(_safe(str(exc).replace("refused the session", "refused the image"),
                                 config.OPENAI_KEY)) from None
    log.info("voice image %d kB, model %s, ok", kb, model)
    return {"said": said}


# What each session has already been charged. A pause ends one session and a resume opens another,
# so a call now ends more than once, and the browser also posts the seconds again when the overlay
# unmounts after a pause. Charging by session id rather than by post makes the repeat free.
_charged = {}


# How a call ended, as the browser says it. One short word, and only these, so a body nobody
# checked cannot write anything else into the log.
#
#   hangup    the Hang up button or Escape        pagehide  the tab or the browser closed
#   back      the phone's back button             hidden    the tab stayed hidden 20 seconds
#   cap       the session ceiling                 silence   nobody spoke for the setting
#   pause     the Pause button                    wait      the call paused itself on an agent
WHYS = ("hangup", "back", "pagehide", "hidden", "silence", "cap", "pause", "wait")


def end_session(session_id, seconds, why=None):
    """Charge a finished session to today's budget. Returns what is left.

    The same session ended twice adds nothing the second time and is logged as ignored. A Hang
    up and then the tab closing can both post, and the second must change nothing. Only the part
    above what that session has already been charged is added, so a later post with a larger
    number still lands and the same number twice does not.

    `why` is how it ended, one of WHYS. Anything else is left out of the log line.
    """
    try:
        seconds = max(0.0, float(seconds or 0.0))
    except (TypeError, ValueError):
        seconds = 0.0
    key = (session_id or "").strip() if isinstance(session_id, str) else ""
    how = why if isinstance(why, str) and why in WHYS else ""
    again = False
    with _lock:
        if key:
            again = key in _charged
            already = _charged.get(key, 0.0)
            owing = max(0.0, seconds - already)
            _charged[key] = max(already, seconds)
        else:
            # No id, so there is nothing to recognise it by. Charged as it comes.
            owing = seconds
    if again and not owing:
        log.info("voice session %s ended again%s, ignored", key, f" by {how}" if how else "")
        return budget()
    left = spend(owing)
    if owing:
        from . import openai_log
        openai_log.record("calls", config.VOICE_MODEL, seconds=owing)
    log.info("voice session %s ended%s after %.0f seconds, %.0f charged, %.1f minutes left today",
             key or "unknown", f" by {how}" if how else "", seconds, owing,
             left["left_minutes"])
    return left


def reset():
    """Forget the proposals and the charges held in memory. For tests, and for nothing else."""
    with _lock:
        _proposals.clear()
        _charged.clear()


# -- recordings ------------------------------------------------------------------------
# Android's screen recorder leaves call audio out, by design, so the app records each call itself:
# their microphone and the voice that answered, mixed, one file per session of the call. The audio
# never passes through this laptop during the call, so the app sends the file when the session
# closes. Kept under var/voice/recordings/<date>/, named by time and by who they talked to.

RECORDING_MAX = 200 * 1024 * 1024


def recordings_dir():
    return config.VOICE_DIR / "recordings"


# Pieces of a session's recording on their way in: session id -> [path, next piece expected].
# The app sends a piece every ten seconds during the call, so a call that drops keeps what it had
# (The owner, 21 October 2026: many calls had no recording, because it went up only on a clean close).
_pieces = {}


def save_recording(session_id, stream, length, part=None):
    """Write one session's recording, or with `part` one piece of it, added to the pieces before.
    Returns the path. Raises ValueError when it is off, empty or too big, so the route can say
    why."""
    if not memory.record_calls():
        raise ValueError("call recording is off on Config")
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", session_id or ""):
        raise ValueError("no such session")
    if not length or length <= 0:
        raise ValueError("an empty recording")
    if length > RECORDING_MAX:
        raise ValueError("a recording over 200 MB")
    who = "desk"
    try:
        from . import agentcall
        call = agentcall.session_call(session_id)
        if call and call.get("agent"):
            who = call["agent"]
    except Exception:
        log.exception("recording: could not tell whose call %s was", session_id)
    now = time.localtime()
    folder = recordings_dir() / time.strftime("%Y-%m-%d", now)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%H%M%S', now)}-{who}-{session_id[-8:]}.webm"
    if part is not None:
        return _save_piece(session_id, path, stream, length, part)
    tmp = path.with_suffix(".part")
    left = length
    with open(tmp, "wb") as fh:
        while left > 0:
            chunk = stream.read(min(1 << 16, left))
            if not chunk:
                break
            fh.write(chunk)
            left -= len(chunk)
    if left:
        tmp.unlink(missing_ok=True)
        raise ValueError("the recording arrived cut short")
    tmp.replace(path)
    log.info("recording: %s, %d bytes", path.name, length)
    return path


def _save_piece(session_id, fresh, stream, length, part):
    """One piece, appended in order. Piece 0 opens the file at `fresh`. A piece sent twice is kept
    once; a lost piece leaves a gap, not a refusal of every piece after it."""
    data = stream.read(length)
    if len(data) != length:
        raise ValueError("the recording arrived cut short")
    with _lock:
        held = _pieces.get(session_id)
        if held is None:
            if part != 0:
                raise ValueError("a piece of a recording that never started")
            held = _pieces[session_id] = [fresh, 0]
        path, expected = held
        if part < expected:
            return path
        if path.exists() and path.stat().st_size + length > RECORDING_MAX:
            raise ValueError("a recording over 200 MB")
        with open(path, "ab") as fh:
            fh.write(data)
        held[1] = part + 1
    if part == 0:
        log.info("recording: %s started", path.name)
    return path
