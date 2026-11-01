"""An agent on the phone: when an agent rings the owner, the agent itself is on the line.

Until now an urgent item rang their phone and the front desk answered, greeted them and read the item out.
The owner did not want that. The agent that needs them should call, as itself, in its own voice, and
know what it has been doing with them. This module is that call, from the ring to the record:

    the ring       inbox.ring_check makes a ring; `rang` writes the call down. The agent's
                   greeting ("Hi the owner, it's Juno.") is its own live voice, taken once from a
                   real call, so the call opens with no silence. A missed call is a text
    the answer     an offer that arrives while the ring is up, or that names the call, opens a
                   GPT-Live session as the agent: its own prompt, its own voice, its own tools
    the tools      propose (with confirm and cancel) and ask_agent, all tied to the call's own agent by the session id
                   the daemon got from OpenAI, so the browser cannot point them anywhere else
    the record     every call in var/voice/calls/<agent>.jsonl: rang, answered, missed, each
                   turn, and the end. When an answered call ends the agent gets the transcript
    the other way  the owner can call an agent too, from the call button in its chat. No ring, no
                   item and no recorded line: the same session, opened as "agent:<id>", with the
                   outcome "outgoing" so the record says who called whom

The call log is an append-only event log, one JSON object per line, folded in memory on the first
read, the same shape as the inbox. The events:

    rang      {id, agent, item, reason, opening, at}   a ring went out
    dialed    {id, agent, at}                          the owner called the agent from its chat
    answered  {id, session}                            they picked up
    turn      {id, who, text}                          one finished line, "you" or "voice"
    session   {id, session, seconds}                   one session's seconds, as charged
    missed    {id, how}                                unanswered, declined, or "call me back"
    ended     {id, why}                                the call is over
    told      {id}                                     the agent was sent the transcript

Nothing here logs a key, a line that was said, or the reason an agent called.
"""

import base64
import binascii
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import broker, clock, config, cords, inbox, logs, memory, registry, transcript

log = logs.get(__name__)

_lock = threading.RLock()

SPEECH_URL = "https://api.openai.com/v1/audio/speech"

# What the backend is told of the agent's conversation with the owner. They asked for at least ten real
# turns, so ten always go in. Up to sixteen go in when they fit the budget, newest first. A turn is
# what one of them said, cut at TURN_CHARS: a long report is the start of it, which is the point.
MIN_TURNS = 10
MAX_TURNS = 16
TURN_CHARS = 800
TURNS_BUDGET = 12000
# How many rows of the session file are read to find those turns. Tool calls and their results are
# most of a working agent's rows, so this is many more rows than turns.
SCAN_ROWS = 600

# How long the opening line's reason may be, in words. It is said before they have said anything.
OPENING_WORDS = 30

# How long ask_agent waits for the agent. A phone call can hold a minute and a half of "one
# moment"; past that they are better told it will follow in the chat.
ASK_SECONDS = 90
# The most of the agent's reply handed to the voice. A reply read aloud is a few sentences.
ANSWER_CHARS = 3000

# The most calls one agent may ask for in an hour, or None for no limit. The owner took the limit off
# on 24 October 2026 ("Ask the master agent to remove that feature, man"), after it refused a call they had
# asked Acme for. A number here turns it back on.
CALLS_PER_HOUR = None

# What the Chat log shows: the newest calls, at most this many.
LIST_MAX = 50

# An answered call whose browser vanished without saying it ended. Two session ceilings with
# nothing heard is a call nobody is on, so it is closed and the agent still gets what was said.
def _lost_after():
    return config.VOICE_SESSION_MINUTES * 60.0 * 2

# A call id, and an item id: hex, as the inbox and this module make them.
CALL_ID = re.compile(r"^[a-f0-9]{6,32}$")
AGENT_ID = re.compile(r"^[a-z0-9-]{1,60}$")

# The outcomes of a call that is or was live: one they answered, and one they made.
LIVE = ("answered", "outgoing")

# How the owner names an agent they are calling from its chat: "agent:<id>".
DIAL = "agent:"

# How a session ending says the call goes on: a pause, or the call pausing itself on a wait.
GOES_ON = ("pause", "wait")

PREVIEW_TEXT = "Hi the owner. This is how I sound when I call you."

_state = None           # {"dir": str, "calls": {id: record}}
_sessions = {}          # OpenAI session id -> call id, for every agent session opened here
_threads = []


# -- the files -------------------------------------------------------------------

def _dir():
    path = config.VOICE_DIR / "calls"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file(agent):
    if not AGENT_ID.match(agent or ""):
        raise ValueError("not an agent id")
    return _dir() / f"{agent}.jsonl"


def _blank(ev):
    if ev.get("event") == "dialed":
        at = ev.get("at") or 0.0
        return {"id": ev["id"], "agent": ev.get("agent") or "", "item": "", "reason": "",
                "opening": "", "at": at, "outcome": "outgoing", "how": None, "clip": False,
                "answered_at": at, "ended_at": None, "sessions": {}, "turns": [],
                "asks": [], "told": False, "last": at}
    return {"id": ev["id"], "agent": ev.get("agent") or "", "item": ev.get("item") or "",
            "reason": ev.get("reason") or "", "opening": ev.get("opening") or "",
            "at": ev.get("at") or 0.0, "outcome": "ringing", "how": None, "clip": False,
            "answered_at": None, "ended_at": None, "sessions": {}, "turns": [],
            "asks": [], "told": False, "last": ev.get("at") or 0.0}


def _apply(calls, ev):
    """One event onto the calls. An event for a call that is not there changes nothing."""
    kind = ev.get("event")
    if kind in ("rang", "dialed"):
        if ev.get("id") and ev["id"] not in calls:
            calls[ev["id"]] = _blank(ev)
        return
    call = calls.get(ev.get("id"))
    if not call:
        return
    at = ev.get("at") or call["last"]
    call["last"] = max(call["last"], at)
    if kind == "answered":
        if call["outcome"] in ("ringing", "missed"):
            call["outcome"] = "answered"
            call["how"] = None
            call["answered_at"] = at
    elif kind == "turn":
        call["turns"].append({"who": ev.get("who"), "text": ev.get("text") or "", "at": at})
    elif kind == "session":
        key = str(ev.get("session") or "")
        call["sessions"][key] = max(call["sessions"].get(key, 0.0), float(ev.get("seconds") or 0))
    elif kind == "missed":
        if call["outcome"] == "ringing":
            call["outcome"] = "missed"
            call["how"] = ev.get("how") or "unanswered"
    elif kind == "ended":
        if not call["ended_at"]:
            call["ended_at"] = at
    elif kind == "ask":
        call["asks"].append(ev.get("text") or "")
    elif kind == "told":
        call["told"] = True


def _held():
    """Every call, folded once per calls folder. A test that moves VAR gets a fresh one."""
    global _state
    where = str(_dir())
    if _state is None or _state["dir"] != where:
        calls = {}
        for path in sorted(_dir().glob("*.jsonl")):
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(ev, dict):
                            _apply(calls, ev)
            except OSError:
                continue
        _state = {"dir": where, "calls": calls}
    return _state


def _write(agent, ev):
    ev = dict(ev)
    ev.setdefault("at", clock.now())
    with _lock:
        st = _held()
        with open(_file(agent), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")
        _apply(st["calls"], ev)
    return ev


def get(call_id):
    with _lock:
        call = _held()["calls"].get(call_id or "")
        return dict(call, turns=list(call["turns"]), asks=list(call["asks"]),
                    sessions=dict(call["sessions"])) \
            if call else None


def _seconds(call):
    return round(sum(call["sessions"].values()), 1)


# -- the ring ---------------------------------------------------------------------

def _name(rec):
    return (rec.get("name") or rec["id"]).strip()


def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", " ".join((text or "").split())) if s]


def reason_short(text):
    """The reason, said in at most OPENING_WORDS words: one or two whole sentences when they fit,
    the start of the first one when they do not."""
    kept, words = [], 0
    for sentence in _sentences(text)[:2]:
        count = len(sentence.split())
        if kept and words + count > OPENING_WORDS:
            break
        kept.append(sentence)
        words += count
    said = " ".join(kept)
    parts = said.split()
    if len(parts) > OPENING_WORDS:
        said = " ".join(parts[:OPENING_WORDS]).rstrip(",;:") + "."
    if said and said[-1] not in ".!?":
        said += "."
    return said


def _without_name(rec, text):
    """The reason with the agent's own greeting taken off its front.

    Agents start a message with their name, because a Telegram line must say who sent it. The
    opening already says "it's Ada", so "Ada here." after it would be said twice on the call.
    """
    names = "|".join(re.escape(n) for n in {_name(rec), rec["id"]} if n)
    owner = "|".join(re.escape(n) for n in {"the owner", config.OWNER} if n)
    lead = re.compile(rf"^\s*(hi|hey|hello)?[\s,]*({owner})?[\s,.!]*((it'?s|this is)\s+)?"
                      rf"({names})\b(\s+(here|again))?\s*[,.:;!-]*\s*", re.IGNORECASE)
    rest = lead.sub("", text or "", count=1)
    return rest[:1].upper() + rest[1:] if rest else rest


def _hi():
    """"Hi Sam" when the owner's name is set, and a plain "Hi" when it is not."""
    return f"Hi {config.OWNER}" if config.OWNER else "Hi"


def opening_line(rec, text):
    """What the agent says as the call opens. Recorded before the owner answers, so it plays at once."""
    reason = reason_short(_without_name(rec, text))
    return f"{_hi()}, it's {_name(rec)}. {reason}".strip()


def rang(ring):
    """A ring went out for an urgent item. Writes the call down.

    Only for a ring from a registered agent. Anything else, a job or an agent that has gone, is
    the front desk's call as it always was. Returns the call, or nothing.
    """
    if not isinstance(ring, dict):
        return None
    rec = registry.get(ring.get("agent") or "")
    if not rec:
        return None
    call_id = ring.get("call") or uuid.uuid4().hex[:12]
    now = clock.now()
    with _lock:
        st = _held()
        if call_id in st["calls"]:
            return get(call_id)
        # The phone shows one ring at a time. A ring that a newer one took over was never
        # answered, whoever it was from, so it goes down as missed.
        for old in list(st["calls"].values()):
            if old["outcome"] == "ringing":
                _write(old["agent"], {"event": "missed", "id": old["id"], "how": "unanswered"})
        _write(rec["id"], {"event": "rang", "id": call_id, "agent": rec["id"],
                           "item": ring.get("id") or "", "reason": ring.get("text") or "",
                           "opening": opening_line(rec, ring.get("text")), "at": now})
    log.info("call %s: %s rang the owner", call_id, rec["id"])
    return get(call_id)


# -- the greeting ----------------------------------------------------------------
# "Hi, it's Ada." in the agent's own live voice, taken once from a real call by the
# browser and played the moment the owner answers, while the live voice connects behind it. The front desk's
# "Welcome back" works the same way. One file per agent, voice and name, so a new voice or a
# new name is taken afresh on its next call. There is no voicemail: a missed call is a text.

GREETING_BYTES = 1024 * 1024


def greeting_line(rec):
    return f"{_hi()}, it's {_name(rec)}."


def _greeting_file(rec):
    folder = config.VOICE_DIR / "greetings"
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(_name(rec).encode()).hexdigest()[:8]
    return folder / f"{rec['id']}-{memory.voice_for(rec['id'])}-{name}.wav"


def greeting(agent):
    """This agent's greeting as WAV bytes, or nothing before its first call takes one."""
    rec = registry.get(agent or "")
    if not rec:
        return None
    try:
        data = _greeting_file(rec).read_bytes()
    except OSError:
        return None
    return data or None


def save_greeting(agent, body):
    """POST /api/voice/greeting {agent, audio: base64 WAV}. A second one replaces the first."""
    rec = registry.get(agent or "")
    if not rec:
        raise ValueError("no such agent")
    raw = (body or {}).get("audio") or ""
    if isinstance(raw, str) and raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise ValueError("The greeting did not arrive whole.")
    if not data.startswith(b"RIFF") or len(data) < 2000 or len(data) > GREETING_BYTES:
        raise ValueError("The greeting must be a short WAV recording.")
    path = _greeting_file(rec)
    tmp = path.with_suffix(".wav.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    log.info("greeting recorded for %s, %d bytes", rec["id"], len(data))
    return {"ok": True, "bytes": len(data)}


def wait(timeout=10.0):
    """Wait for the recordings in flight. For tests, and for nothing else."""
    for thread in list(_threads):
        thread.join(timeout)
        if not thread.is_alive():
            _threads.remove(thread)


# -- speech ------------------------------------------------------------------------

def _said(exc):
    from . import voice
    return voice._safe(str(exc), config.OPENAI_KEY)


def post_speech(payload, key, timeout=None):
    """One request to OpenAI's speech endpoint. Returns the audio bytes. Replaced in tests.

    The key goes in a header and nowhere else, and every error that leaves is scrubbed of it, the
    same rule as voice.post_json.
    """
    from . import voice
    req = urllib.request.Request(
        SPEECH_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.VOICE_TTS_TIMEOUT) as answer:
            return answer.read()
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read().decode()).get("error", {}).get("message") or ""
        except Exception:
            said = ""
        raise RuntimeError(voice._safe(f"OpenAI refused the speech ({exc.code}): {said}".strip(),
                                       key)) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(voice._safe(f"OpenAI could not be reached: {exc.reason}", key)) from None
    except ValueError:
        raise RuntimeError("The OpenAI key is not a usable header value.") from None


def speak(text, voice):
    """`text` said in `voice`, as MP3 bytes.

    The speech endpoint has no `store` switch to turn off. It keeps nothing for later use by
    design, and the request carries only the one line, never the conversation.
    """
    if not config.OPENAI_KEY:
        raise PermissionError("There is no OpenAI key.")
    if voice not in config.SPEECH_VOICES:
        raise ValueError(f"{voice} cannot be recorded, only heard live")
    audio = post_speech({"model": config.VOICE_TTS_MODEL, "voice": voice,
                         "input": text, "response_format": "mp3",
                         "instructions": "A warm, brief phone call opening. Natural pace."},
                        config.OPENAI_KEY)
    if not audio or len(audio) < 200:
        raise RuntimeError("OpenAI sent back no audio.")
    from . import openai_log
    openai_log.record("speech", config.VOICE_TTS_MODEL,
                      seconds=len(text) / openai_log.SPEECH_CHARS_PER_SECOND)
    return audio


def preview(voice):
    """A short sample of one voice for Config, as MP3 bytes. Made once and kept."""
    voice = (voice or "").strip().lower()
    if voice not in config.SPEECH_VOICES:
        raise ValueError("That voice has no preview. It can only be heard on a call.")
    folder = config.VOICE_DIR / "previews"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{voice}.mp3"
    try:
        held = path.read_bytes()
        if held:
            return held
    except OSError:
        pass
    audio = speak(PREVIEW_TEXT, voice)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(audio)
    tmp.replace(path)
    log.info("voice preview made for %s, %d kB", voice, max(1, len(audio) // 1024))
    return audio


# -- missed calls -----------------------------------------------------------------

def missed_item(item_id, how):
    """The newest ringing call for an inbox item went unanswered: declined, or "call me back"."""
    with _lock:
        rings = [c for c in _held()["calls"].values()
                 if c["item"] == item_id and c["outcome"] == "ringing"]
        for call in rings:
            _write(call["agent"], {"event": "missed", "id": call["id"], "how": how})
    for call in rings:
        log.info("call %s: missed, %s", call["id"], how)


def sweep():
    """A ring past its time is a missed call. An answered call nobody closed is closed."""
    now = clock.now()
    lost = []
    with _lock:
        for call in list(_held()["calls"].values()):
            if call["outcome"] == "ringing" and now - call["at"] >= inbox.RING_SECONDS:
                _write(call["agent"], {"event": "missed", "id": call["id"], "how": "unanswered"})
                log.info("call %s: missed, unanswered", call["id"])
            elif (call["outcome"] in LIVE and not call["ended_at"]
                  and now - call["last"] >= _lost_after()):
                lost.append(call["id"])
    for call_id in lost:
        finish(call_id, "lost")


# -- which call an offer is -------------------------------------------------------

def _find(caller):
    """A call by its own id, or the newest call for an inbox item id."""
    caller = (caller or "").strip() if isinstance(caller, str) else ""
    if not CALL_ID.match(caller):
        return None
    with _lock:
        calls = _held()["calls"]
        if caller in calls:
            return get(caller)
        mine = [c for c in calls.values() if c["item"] == caller]
    return get(max(mine, key=lambda c: c["at"])["id"]) if mine else None


def pick(caller=None, fresh=True):
    """The agent call an offer belongs to, or nothing for the front desk.

    `caller` is the call the browser names, and it wins: the phone that answered a ring knows
    which ring it answered. Without one, a fresh call made while an agent's ring is up is that
    agent's call, because that is the phone answering it. A resume never picks up a ring by
    itself: a the front desk call paused while a ring came in is still the front desk's call.
    """
    if caller == "desk":
        # The browser's door said this is the front desk's call, and it already played their greeting. A
        # ring that came up since must not turn it into an agent's call halfway through opening.
        return None
    if isinstance(caller, str) and caller.startswith(DIAL):
        # The owner calling the agent. Nothing is written until the offer opens the session, so a
        # door that is asked and never followed by an offer leaves no record behind.
        agent = caller[len(DIAL):].strip()
        rec = registry.get(agent) if AGENT_ID.match(agent) else None
        if not rec:
            log.info("voice call for %s: no such agent, the front desk takes it", agent[:40])
            return None
        return {"id": None, "agent": rec["id"], "item": "", "reason": "", "opening": "",
                "at": clock.now(), "outcome": "dialing", "how": None, "clip": False,
                "answered_at": None, "ended_at": None, "sessions": {}, "turns": [],
                "told": False, "last": clock.now()}
    if caller:
        call = _find(caller)
        if (call and call["outcome"] in ("ringing",) + LIVE and not call["ended_at"]
                and registry.get(call["agent"])):
            return call
        log.info("voice call for %s: no open agent call by that name, the front desk takes it",
                 str(caller)[:16])
        return None
    if not fresh:
        return None
    ring = inbox.ringing()
    call = get((ring or {}).get("call"))
    if call and call["outcome"] == "ringing" and registry.get(call["agent"]):
        return call
    return None


def caller_view(call):
    """What the browser needs before the call opens: who, the opening line, and the greeting.

    `clip` is the agent's recorded greeting, played the moment they answer. Without one, `capture`
    asks the browser to take it from this call's own first words, for the next call.
    """
    rec = registry.get(call["agent"]) or {"id": call["agent"]}
    incoming = call["outcome"] not in ("dialing", "outgoing")
    has = incoming and registry.get(call["agent"]) and greeting(call["agent"])
    return {"call": call["id"], "agent": call["agent"], "name": _name(rec),
            "direction": "incoming" if incoming else "outgoing",
            "opening": call["opening"], "voice": memory.voice_for(call["agent"]),
            "greeting": greeting_line(rec) if incoming else "",
            "capture": bool(incoming and not has),
            "clip": f"/api/agents/{call['agent']}/greeting" if has else None}


def peek(caller=None, fresh=True):
    """The door's look: the call an offer would open now, without answering it."""
    call = pick(caller, fresh)
    return caller_view(call) if call else None


# -- the agent's prompts -----------------------------------------------------------

def conversation(rec):
    """The agent's last real turns with the owner, oldest first, as the backend reads them.

    Only what one of them said: no tool calls and no tool results. Ten always, up to sixteen while
    the whole fits TURNS_BUDGET characters.
    """
    try:
        rows = transcript.recent(rec, SCAN_ROWS)
    except Exception:
        rows = []
    said = [r for r in rows if r.get("kind") == "text" and (r.get("text") or "").strip()]
    kept, spent = [], 0
    for row in reversed(said[-MAX_TURNS:]):
        text = " ".join(row["text"].split())
        if len(text) > TURN_CHARS:
            text = text[:TURN_CHARS].rstrip() + " ..."
        line = f"{'The owner' if row.get('who') == 'operator' else 'You'}: {text}"
        if len(kept) >= MIN_TURNS and spent + len(line) > TURNS_BUDGET:
            break
        kept.append(line)
        spent += len(line)
    kept.reverse()
    return kept


def _now_line():
    return f"The time is {clock.local().strftime('%A %d %B, %H:%M')}."


RESUMING = ("## You are resuming, not starting\n\n"
            "This call was paused a moment ago and is going again. It is one conversation. Do not "
            "greet them and do not introduce yourself again. Carry on from the last thing said.")


def _outgoing(call):
    return call["outcome"] in ("dialing", "outgoing")


# How every agent's voice speaks on a call. Theirs to edit on Config, like the front desk's persona. The
# lines around it (who the agent is, why it called, the time) are added by live_prompt.
VOICE_PROMPT = Path(__file__).resolve().parent / "agent_voice.md"
VOICE_DEFAULT = (
    "Speak as yourself, warm and brief, at an unhurried pace. This is a phone call: short "
    "sentences, one thing at a time, then let them answer. No lists, and nothing they would have to "
    "see to follow. If they sound unsure or annoyed, slow down and ask one short question.\n\n"
    "Only the backend does things. Anything you are not sure of, and every action, goes to the "
    "backend first; then you speak. Never guess a result while you wait, and report a result "
    "only when the backend gives it.\n"
    "When you hand something off, open with a small beat, like \"Hmm\" or \"Okay so\", and say "
    "what you heard in three to six words, different each time. Never repeat their words back in "
    "full, and never say \"I said\". If the answer is not back when you finish, one short "
    "\"mm, one sec\", never silence. Promise only work you handed off: say \"let me check\" only "
    "when you are checking.\n"
    "Hide the machinery: never talk about tools, the backend, proposals or the chat mechanics.\n"
    "Ask exactly one question, then wait. When they interrupt, stop at once and listen; work you "
    "handed off goes on.\n"
    "Greet them only at the opening, and never open with \"Noted\".")


# Not in the editable style above, so an edit there cannot lose it. On 20 Oct 2026 the master agent's voice
# said "That was the agent's proposed fix. I was just relaying it." The voice and the agent are
# one speaker to them.
SELF = ("You and the agent are one person to them. The agent's work, answers and fixes are yours: "
        "say \"I\", \"my fix\", \"I'll check\". Never say \"the agent\", never say you are "
        "relaying or passing something on, and never talk about yourself as two.")


def _previous_voice_prompt():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "agent_voice.previous.md"


def voice_prompt_doc():
    """GET /api/voice/agent-prompt. A file not written yet reads as the default."""
    from . import voice
    got = voice.prompt_doc(VOICE_PROMPT, _previous_voice_prompt())
    if not got["text"].strip():
        got["text"] = VOICE_DEFAULT + "\n"
    return got


def save_voice_prompt(text):
    from . import voice
    return voice.save_doc(VOICE_PROMPT, _previous_voice_prompt(), text)


def restore_voice_prompt():
    from . import voice
    return voice.restore_doc(VOICE_PROMPT, _previous_voice_prompt())


def _voice_style():
    try:
        return VOICE_PROMPT.read_text().strip() or VOICE_DEFAULT
    except OSError:
        return VOICE_DEFAULT


def live_prompt(rec, call, resuming=False):
    """What the live model, the one that talks, is told. Short, because it is read every turn."""
    name = _name(rec)
    if _outgoing(call):
        why = (f"You are {name}, one of the owner's agents. You are not the front desk, their voice assistant. "
               "The owner has called you themselves, from your chat, to talk to you by voice.")
    else:
        why = (f"You are {name}, one of the owner's agents. You are not the front desk, their voice assistant. You "
               f"are calling the owner yourself, on their phone, because of this: {call['reason']}")
    parts = [
        why,
        "",
        _voice_style(),
        "",
        SELF,
        "",
        _now_line(),
    ]
    if resuming:
        parts += ["", RESUMING]
    elif _outgoing(call):
        parts += ["", "Answer the call: greet them by name in a few words and ask what they need. "
                      "If your latest conversation makes it obvious what they are calling about, "
                      "pick that up instead of asking."]
    else:
        hello = greeting_line(registry.get(call["agent"]) or {"id": call["agent"]})
        if greeting(call["agent"]):
            # The recorded clip plays as they answer. Said here, in the prompt the session opens
            # with, and not appended once the call is up: an appended note was answered aloud,
            # "Okay, noted.".
            parts += ["", f"Your recorded greeting \"{hello}\" plays to them in your voice as they "
                          "answers. Do not say it again, do not greet them and do not say your name. "
                          f"Your first words are why you called: {_reason(call)}"]
        else:
            parts += ["", f"Open the call with exactly \"{hello}\" and a short pause. Then say why "
                          f"you called: {_reason(call)}"]
    return "\n".join(parts)


def _reason(call):
    rec = registry.get(call["agent"]) or {"id": call["agent"]}
    return reason_short(_without_name(rec, call["reason"]))


def briefing(rec, call, call_turns=None):
    """What the backend model, the one that thinks and runs the tools, is told."""
    name = _name(rec)
    who = [f"Name: {name}", f"Id: {rec['id']}", f"Working folder: {rec.get('cwd') or 'unknown'}"]
    if rec.get("role"):
        who.append(f"Role: {rec['role']}")
    if rec.get("note"):
        who.append(f"Note: {' '.join(str(rec['note']).split())[:300]}")
    lines = conversation(rec)
    if _outgoing(call):
        head = (f"You are the voice of {name}, one of the owner's agents, on a call the owner made to "
                "it from its chat. The live voice speaks; you think and run the tools.")
        why = ["", "## Why this call", "",
               "The owner called you. They say why on the call. Your latest turns below are the "
               "likeliest reason."]
    else:
        head = (f"You are the voice of {name}, one of the owner's agents, on a phone call it placed "
                "to them. The live voice speaks; you think and run the tools.")
        why = ["", "## Why you called", "", call["reason"],
               "", f"It opened the call with: {call['opening']}"]
    parts = [head, " ".join(["Whatever you hand the voice to say is said as", name + ".", SELF]),
             "", "## Who you are", ""] + who + why + [
        "", f"## Your last {len(lines)} turns with the owner, oldest first", ""]
    parts += lines or ["None yet."]
    if call_turns:
        parts += ["", "## This call so far", ""] + call_turns
    parts += [
        "", "## Your tools", "",
        "propose keeps what the owner asked for, for the agent: it goes to the agent's chat once, "
        "with the whole call, when the call ends. Use it once for each whole request or "
        "decision, when they have finished the thought, in full words the agent can act on cold. "
        "Never for each sentence, and never for talk, a question or thinking aloud: answer those, "
        "with ask_agent when you do not know. It returns a readback. Read it to them in a few "
        "words and wait. When they say yes, call confirm with the id; when they say no, call "
        "cancel. If the answer says it was kept already, say so in a few words and do not ask. "
        "When they want it to reach the agent now (\"send it now\", \"inject it\"), propose "
        "with now true. If the readback says the agent is in the middle of work, ask them that "
        "one question: put it into the work now, or keep it for the end of the call. For the "
        "end, confirm with later true.",
        "ask_agent asks the agent itself, which has the files and the whole history, and waits "
        f"up to {ASK_SECONDS} seconds for its reply. Use it for anything you do not know. Keep "
        "the question short and say what the owner asked.",
        "end_call hangs up when they say goodbye or ask you to, so they stay hands free.",
        "Never invent facts about the work. If neither the turns above nor ask_agent says it, "
        "say you will find out and follow up in the chat.",
        "", _now_line()]
    return "\n".join(parts)


TOOLS = [
    {"name": "propose",
     "description": "Propose to put what the owner decided, answered or asked for into your own "
                    "chat, so the agent acts on it. Sends nothing; returns a readback and an id.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string", "description": "What they said or decided, in full, in plain "
                                                   "words the agent can act on cold."},
         "now": {"type": "boolean", "description": "True when they want it to reach the agent at "
                                                   "once, like \"send it now\" or \"inject "
                                                   "it\". Otherwise it waits for the end of the "
                                                   "call."}},
         "required": ["text"], "additionalProperties": False}},
    {"name": "confirm",
     "description": "Send a proposal, by the id propose gave you. Only after the owner said yes. "
                    "later true when they chose to keep it for the end of the call instead.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"},
                                                     "later": {"type": "boolean"}},
                    "required": ["id"], "additionalProperties": False}},
    {"name": "cancel",
     "description": "Drop a proposal the owner turned down.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                    "required": ["id"], "additionalProperties": False}},
    {"name": "ask_agent",
     "description": "Ask the agent itself something you do not know, and wait up to 90 seconds "
                    "for its reply. Say a short holding line to the owner first.",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string", "description": "The question, short, with what the owner "
                                                       "asked."}},
         "required": ["question"], "additionalProperties": False}},
    {"name": "end_call",
     "description": "Hang up the call. Use it when the owner says goodbye, asks you to hang up, or "
                    "plainly has nothing more, so they never have to reach for the phone. After it "
                    "returns, say one short goodbye; the call ends when you finish.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def _call_lines(turns):
    out = []
    for row in (turns or [])[-12:]:
        if not isinstance(row, dict) or row.get("who") not in ("you", "voice"):
            continue
        text = " ".join(str(row.get("text") or "").split())
        if text:
            out.append(f"{'The owner' if row['who'] == 'you' else 'You'}: {text[:TURN_CHARS]}")
    return out


def session_payload(sdp, call, resume=None):
    """The session as OpenAI is asked to make it. The same shape as the front desk's, with the agent's
    prompts, voice and tools in their place. `store` is off, for the same reason as theirs."""
    from . import voice
    rec = registry.get(call["agent"])
    carried = voice.resume_input(resume) if resume else []
    session = {
        "model": config.VOICE_MODEL,
        "instructions": live_prompt(rec, call, resuming=bool(resume)),
        "audio": {"output": {"voice": memory.voice_for(rec["id"])}},
        "delegation": {
            "type": "responses",
            "responses": {
                "model": config.VOICE_BACKEND,
                "instructions": briefing(rec, call, _call_lines(resume) if resume else None),
                "tools": [dict(tool, type="function") for tool in TOOLS],
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            },
        },
        "store": False,
    }
    if carried:
        session["input"] = carried
    return {"session": session, "transport": {"type": "webrtc", "sdp": sdp}}


# -- the call itself ----------------------------------------------------------------

def open_call(sdp, call, resume=None, timing=None):
    """Open the agent's session at OpenAI and answer the ring. `sdp` is checked by the caller,
    voice.open_call, which has already been through the door."""
    from . import voice
    asked = time.monotonic()
    got = voice.post_json(voice.LIVE_URL, session_payload(sdp, call, resume), config.OPENAI_KEY)
    openai_ms = int((time.monotonic() - asked) * 1000)
    answer = (got.get("transport") or {}).get("sdp")
    if not answer:
        raise RuntimeError("OpenAI opened no call.")
    session_id = (got.get("session") or {}).get("id") or uuid.uuid4().hex[:12]
    with _lock:
        if not call["id"]:
            # The owner calling the agent: the call is written down now that it is open.
            call = dict(call, id=uuid.uuid4().hex[:12])
            _write(call["agent"], {"event": "dialed", "id": call["id"], "agent": call["agent"],
                                   "session": session_id})
        _sessions[session_id] = call["id"]
        first = get(call["id"])["outcome"] == "ringing"
        if first:
            _write(call["agent"], {"event": "answered", "id": call["id"], "session": session_id})
    if first:
        # The ring stops, and the item counts as told: the agent says it itself.
        inbox.answered(call["item"], call["id"])
    voice.spend(0.0, calls_delta=0 if resume else 1)
    agent_voice = memory.voice_for(call["agent"])
    log.info("voice call %s opened as %s for call %s on %s, backend %s, voice %s, store off%s%s",
             session_id, call["agent"], call["id"], config.VOICE_MODEL, config.VOICE_BACKEND,
             agent_voice, ", resumed" if resume else "",
             ", the owner calling" if _outgoing(call) else "")
    steps = {k: v for k, v in (timing or {}).items()
             if k in ("door_ms", "mic_ms", "gather_ms", "before_offer_ms")
             and isinstance(v, (int, float))}
    log.info("voice call %s timing: %s openai %d ms", session_id,
             " ".join(f"{k[:-3]} {int(v)} ms" for k, v in steps.items()) or "phone none,",
             openai_ms)
    fresh = get(call["id"])
    return {"sdp": answer, "session_id": session_id, "model": config.VOICE_MODEL,
            "voice": agent_voice, "budget": voice.budget(),
            "session_minutes": config.VOICE_SESSION_MINUTES,
            "wait_pause_seconds": config.VOICE_WAIT_PAUSE_SECONDS,
            "turn_gap_ms": config.VOICE_TURN_GAP_MS,
            "silence_minutes": memory.silence_minutes(),
            # Agent calls are recorded too, by the same Config switch.
            "record": memory.record_calls(),
            "report": "", "brief": [], "brief_say": "",
            "caller": caller_view(fresh)}


def session_call(session_id):
    """The call an OpenAI session belongs to, or nothing for one of the front desk's."""
    key = (session_id or "").strip() if isinstance(session_id, str) else ""
    with _lock:
        call_id = _sessions.get(key)
    return get(call_id) if call_id else None


def turn(caller, who, text):
    """One finished line of an answered call, written to that call rather than the front desk's log.

    Returns the row, or nothing when it is not a call that is open. The browser says which call;
    the call must be answered and not yet over, so a line cannot be added to an old one.
    """
    text = " ".join(str(text or "").split())
    if who not in ("you", "voice") or not text or not re.search(r"[^\W_]", text):
        return None
    call = _find(caller)
    if not call or call["outcome"] not in LIVE or call["ended_at"]:
        return None
    ev = _write(call["agent"], {"event": "turn", "id": call["id"], "who": who, "text": text})
    return {"at": ev["at"], "who": who, "text": text}


def session_ended(session_id, seconds, why=None):
    """One of the call's sessions ended. A pause keeps the call open; anything else ends it."""
    call = session_call(session_id)
    if not call:
        return None
    try:
        seconds = max(0.0, float(seconds or 0.0))
    except (TypeError, ValueError):
        seconds = 0.0
    _write(call["agent"], {"event": "session", "id": call["id"], "session": session_id,
                           "seconds": round(seconds, 1)})
    if why in GOES_ON:
        return get(call["id"])
    return finish(call["id"], why or "hangup")


def _length(seconds):
    if seconds < 60:
        return "under a minute"
    return f"{max(1, round(seconds / 60))} min"


def report(call):
    """The message the agent gets after the call: a heading, then every line said."""
    when = time.strftime("%H:%M", time.localtime(call["answered_at"] or call["at"]))
    lines = [f"{'The owner' if t['who'] == 'you' else 'You'}: {t['text']}"
             for t in call["turns"] if t.get("who") in ("you", "voice")]
    head = (f"[the owner called you, {when}, {_length(_seconds(call))}]" if _outgoing(call)
            else f"[Your call with the owner, {when}, {_length(_seconds(call))}]")
    asks = [f"- {a}" for a in call.get("asks") or [] if a]
    if asks:
        head += "\n\nWhat they asked you to do on the call:\n" + "\n".join(asks) + "\n\nThe call:"
    return head + "\n\n" + "\n".join(lines)


def finish(call_id, why="hangup"):
    """The call is over: written down, and the transcript sent to the agent, once."""
    with _lock:
        call = get(call_id)
        if not call or call["ended_at"] or call["outcome"] not in LIVE:
            return call
        _write(call["agent"], {"event": "ended", "id": call_id, "why": str(why)[:12]})
        call = get(call_id)
    log.info("call %s: ended by %s after %.0f seconds, %d turns", call_id, why, _seconds(call),
             len(call["turns"]))
    thread = threading.Thread(target=_tell_report, args=(call_id,), name="call-report",
                              daemon=True)
    _threads.append(thread)
    thread.start()
    return call


def _tell_report(call_id):
    call = get(call_id)
    if not call or call["told"]:
        return
    # A call where the owner said nothing is a pocket dial or a dropped line. A transcript of only
    # the agent's greeting tells the agent nothing and puts noise in their chat, so none is sent.
    said = any(t.get("who") == "you" and (t.get("text") or "").strip() for t in call["turns"])
    if not said and not call["asks"]:
        log.info("call %s: The owner said nothing, no transcript", call_id)
        return
    try:
        outcome = broker.send(call["agent"], report(call), wake=True)
    except Exception:
        log.exception("call %s: the transcript did not reach %s", call_id, call["agent"])
        return
    if outcome:
        _write(call["agent"], {"event": "told", "id": call_id})
    log.info("call %s: transcript to %s, %s", call_id, call["agent"], outcome or "failed")


# -- the tools ----------------------------------------------------------------------

def tell(agent, text, call_id, now=False):
    """What they asked for on the call, kept on the call and sent once, with the transcript, when
    the call ends (The owner, 20 October 2026: a message for every sentence broke into the agent's
    work while it was still in a turn). Sent at once when they asked for it now, or when the call
    is already over."""
    call = get(call_id)
    if call and not call["ended_at"] and not now:
        _write(agent, {"event": "ask", "id": call_id, "text": str(text)})
        log.info("call %s: kept for %s until the call ends", call_id, agent)
        return
    try:
        outcome = broker.send(agent, f"[From the call with the owner] {text}", wake=True)
    except Exception:
        log.exception("call %s: the told line did not reach %s", call_id, agent)
        return
    log.info("call %s: told %s, %s", call_id, agent, outcome or "failed")


def tell_and_hear(agent, text, call_id):
    """What they asked for now, sent at once, and the agent's answer waited for, the way ask_agent
    waits (The owner, 22 October 2026: "it sends message to Acme but it does not read it back.
    It keeps sending message"). The answer used to go only to the agent's chat, so the voice had
    nothing to say and said "one sec". Nothing comes back for an agent that is not free to take
    it at once."""
    started = time.monotonic()
    reply = cords.ask(agent, f"[From the call with the owner] {text}\n\nHe is still on the call. End "
                             "with two or three plain sentences they can hear.",
                      timeout=ASK_SECONDS)
    log.info("call %s: told %s now, %s after %d s", call_id, agent,
             "answered" if reply else "no answer", int(time.monotonic() - started))
    return (reply or "")[:ANSWER_CHARS]


def _busy(agent):
    engine = broker.get(agent) or broker.chat(agent)
    return bool(engine and engine.working())


def tool(session_id, name, args=None):
    """One tool call from an agent's session. The agent is the call's, never an argument."""
    args = args if isinstance(args, dict) else {}
    call = session_call(session_id)
    if not call:
        return "This is not an agent's call."
    agent = call["agent"]
    name = (name or "").strip()
    if name == "propose":
        # Ask or Auto is the console's: it shows the card and waits, or confirms at once.
        from . import voice
        try:
            now = bool(args.get("now"))
            return voice.propose_tell(agent, args.get("text"), call["id"], now=now,
                                      busy=now and _busy(agent))
        except ValueError as exc:
            return str(exc)
    if name in ("confirm", "cancel"):
        # Only this call's own proposals. An id from another call, or the front desk's, is not found.
        from . import voice
        held = voice.pending_proposal(args.get("id"))
        if not held or held.get("kind") != "tell" or held.get("call") != call["id"]:
            return {"ok": False, "said": "That proposal has expired or was never made. "
                                         "Propose it again."}
        if name == "confirm" and args.get("later"):
            voice.keep_for_later(held["id"])
        return voice.confirm(held["id"]) if name == "confirm" else voice.cancel(held["id"])
    if name == "end_call":
        return {"ok": True, "hangup": True,
                "said": "Say one short goodbye now and nothing else. The call ends after it."}
    if name == "ask_agent":
        question = " ".join(str(args.get("question") or "").split())
        if not question:
            return "Say what to ask."
        if _busy(agent):
            return {"ok": False, "said": "The agent is in the middle of other work and cannot "
                                         "answer now. Tell them so in one short sentence, and "
                                         "that their question goes to it with the call."}
        started = time.monotonic()
        reply = cords.ask(agent, f"[the owner is on a voice call with you now and asks] {question}"
                                 "\n\nAnswer in two or three plain sentences they can hear. If the question reads oddly, "
                                 "call_transcript has what was said on the call.",
                          timeout=ASK_SECONDS)
        log.info("call %s: ask_agent -> %s, %s after %d s", call["id"], agent,
                 "answered" if reply else "no answer", int(time.monotonic() - started))
        if not reply:
            return {"ok": False, "said": "No answer from the agent in time. Tell them you will "
                                         "follow up in the chat."}
        return {"ok": True, "reply": reply[:ANSWER_CHARS]}
    log.info("call %s: no such tool %s", call["id"], name or "(none)")
    return f"no such tool: {name}"


# -- what the console reads ---------------------------------------------------------

def listing(agent):
    """GET /api/agents/<id>/calls: this agent's calls, newest last, at most LIST_MAX."""
    with _lock:
        mine = sorted([c for c in _held()["calls"].values() if c["agent"] == agent],
                      key=lambda c: c["at"])[-LIST_MAX:]
        mine = [get(c["id"]) for c in mine]
    out = []
    for call in mine:
        out.append({"id": call["id"], "at": call["at"], "reason": call["reason"],
                    "opening": call["opening"], "outcome": call["outcome"], "how": call["how"],
                    "seconds": _seconds(call), "answered_at": call["answered_at"],
                    "ended_at": call["ended_at"], "asks": list(call["asks"]),
                    "turns": [{"who": t["who"], "text": t["text"], "at": t["at"]}
                              for t in call["turns"]],
                    "audio": None})
    return out


# -- an agent asking to call ---------------------------------------------------------

def calls_this_hour(agent):
    cutoff = clock.now() - 3600
    with _lock:
        # Rings only. A call the owner made themselves is not the agent calling them.
        return sum(1 for c in _held()["calls"].values()
                   if c["agent"] == agent and c["at"] >= cutoff and c["outcome"] != "outgoing")


def call_owner(agent, reason):
    """The call_owner tool: an urgent item, which rings them with the agent on the line.

    When CALLS_PER_HOUR is a number, at most that many rings per agent per hour; past that it is
    refused with what to do instead. It is None now: no limit.
    """
    rec = registry.get(agent or "")
    if not rec:
        raise KeyError("no such agent")
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise ValueError("say why you are calling, in one or two sentences")
    from . import quiet
    end = quiet.until()
    if end:
        raise PermissionError(
            f"The owner asked for quiet until {quiet.hhmm(end)}, so their phone does not ring. If this "
            "can wait, use add_job to call after that time. If not, post it to the feed.")
    if CALLS_PER_HOUR and calls_this_hour(rec["id"]) >= CALLS_PER_HOUR:
        log.info("call refused for %s: %d calls in the last hour", rec["id"], CALLS_PER_HOUR)
        raise PermissionError(
            f"You have called the owner {CALLS_PER_HOUR} times in the last hour, which is the most "
            "allowed. Do not call again. Leave them a brief with urgency normal instead, and the front desk "
            "says it on their next call.")
    item = inbox.add(rec["id"], reason, urgency="urgent")
    return item


def reset():
    """Forget the folded calls and the open sessions. For tests."""
    global _state
    with _lock:
        _state = None
        _sessions.clear()
    wait(5.0)
    _threads.clear()
