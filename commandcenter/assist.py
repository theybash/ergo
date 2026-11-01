"""Assistant mode: the bar Ergo opens when the owner holds the power key.

The owner types, dictates or holds the mic for a memo, over any app and on the lock screen. Jev
picks the agent, and the message lands in that agent's chat as the owner's own message. The phone
shows Jev's pick live, as the face on the Send button, and a long press on Send lets the owner pick.

    GET  /api/assist/agents   ->  {agents: [{id, name, face, awake, last}], newest first}
    POST /api/assist/route    {text, app?}                    ->  {agent, name, confidence, sure}
    POST /api/assist/send     {text | audio+mime, agent?, picked?, jev?, app?, url?,
                               screenshot?, screen_text?}     ->  {ok, agent, name, said}

A memo comes back at once: the laptop writes it down, routes it and delivers it afterwards, so the
owner never waits for the words. The screen goes only when the owner pressed the image button: the phone then
sends the screenshot and the text Android handed over, and both land in the agent's inbox as
files, named in the message, the way the chat composer attaches a picture.

Jev's routing is on trial. Every choice is a line in var/assist/routes.jsonl with its confidence,
and so is every send, with the agent Jev showed and the agent the message went to. A send where the
owner picked another agent is a labelled mistake.

What each agent does comes from var/assist/agents.json ({id: one sentence}) when it names the
agent, and otherwise from the first plain lines of the agent's CLAUDE.md.
"""

import base64
import binascii
import json
import re
import threading
import time
from datetime import datetime, timezone

from . import broker, config, files, logs, memory, registry, transcript, voicenote

log = logs.get(__name__)

# Who gets a message Jev is not sure about: the home agent.
FALLBACK = config.HOME_AGENT

# Below this Jev's pick is not trusted and the message goes to the fallback. Jev's confidence
# says how concentrated its answer is; a first guess, to move on the logged choices.
SURE = 0.45

TEXT_CHARS = 4000
SCREEN_CHARS = 20000
# How much of the screen's text Jev reads when he added the screen: the top of the page, enough to
# tell what it is about, small enough to keep the route quick.
JEV_SCREEN_CHARS = 1500
MAX_PICTURE = 12 * 1024 * 1024
DESCRIBE_CHARS = 300

# The route answers while he types, so it is short; the phone keeps the last face meanwhile.
ROUTE_TIMEOUT = 4.0

_lock = threading.Lock()
_cache = {}


def _dir():
    d = config.VAR / "assist"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(entry):
    """One line in the routing log. His words are in it: the log exists to check Jev's picks."""
    entry = {"at": _now(), **entry}
    with _lock:
        with open(_dir() / "routes.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _clean(text, limit=TEXT_CHARS):
    return " ".join(str(text or "").split())[:limit]


# -- who the agents are ---------------------------------------------------------------------

def _written():
    try:
        got = json.loads((_dir() / "agents.json").read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


# Lines about accounts and credentials are not a description of the agent, and are not sent out.
SKIP = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|credential|password|token|api key|github|account", re.I)


def _from_claude_md(rec):
    """The first plain lines of the agent's CLAUDE.md: no headings, no rules about accounts."""
    try:
        text = (config.Path(rec["cwd"]) / "CLAUDE.md").read_text(errors="replace")
    except (OSError, KeyError, TypeError):
        return ""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", "-", "```", ">")) or SKIP.search(line):
            continue
        lines.append(line)
        if sum(len(x) for x in lines) > DESCRIBE_CHARS:
            break
    return " ".join(lines)[:DESCRIBE_CHARS]


def agents():
    """The agents the owner can send to, the most recent first, for the long press list."""
    out = []
    for rec in registry.all():
        out.append({"id": rec["id"], "name": rec.get("name") or rec["id"],
                    "face": f"/file/{rec['id']}/avatar.png",
                    "awake": bool(broker.get(rec["id"]) and broker.get(rec["id"]).alive()),
                    "last": rec.get("last_activity") or 0})
    out.sort(key=lambda a: -a["last"])
    return out


def _roster():
    written = _written()
    roster = []
    for a in agents():
        rec = registry.get(a["id"]) or {}
        role = rec.get("role") if rec.get("role") not in (None, "master") else None
        about = _clean(written.get(a["id"]) or role, DESCRIBE_CHARS)
        roster.append({"id": a["id"], "name": a["name"],
                       "does": about or _from_claude_md(rec) or "No description yet."})
    return roster


# -- Jev ------------------------------------------------------------------------------------

def payload(text, roster, app=None, screen=None):
    """One Choice: which agent should read this message. The roster is the state; each agent is
    an answer, described by what it does. `screen` is {link, text} when he added his screen: a
    short ask like "Explain this" says nothing by itself, the page says what it is about."""
    criteria = {a["id"]: {"meaning": f"{a['name']}: {a['does']}"} for a in roster}
    state = {"message": text, "agents": [{"id": a["id"], "name": a["name"], "does": a["does"]}
                                         for a in roster]}
    if app:
        state["open_app"] = app
    if screen:
        state["screen"] = screen
    return {
        "state": state,
        "model": memory.JEV_MODEL,
        "questions": {"agent": {
            "type": "choice",
            "instructions": {
                "situation": "The owner has several AI agents. Each one in `agents` does one part of "
                             "his life. He just wrote `message` from his phone, without saying "
                             "who it is for." + (" `open_app` is the app on his screen when he "
                                                 "wrote it." if app else "") +
                             (" He sent his screen with it: `screen` holds its link and the start "
                              "of its text. A short message like \"Explain this\" or \"Make this "
                              "a task\" is about that screen, so judge by what the screen is "
                              "about as well as by the words." if screen else ""),
                "question": "Which agent should receive `message`? A message that names an agent "
                            "goes to that agent. A general errand, a reminder, an alarm, a task, "
                            "mail, calendar or anything that fits no one agent goes to the "
                            "personal assistant.",
            },
            "criteria": criteria,
        }},
    }


def _screen(screen_text=None, url=None):
    """What Jev reads of the screen he added, or None."""
    out = {}
    if url:
        out["link"] = _clean(url, 500)
    words = _clean(screen_text, JEV_SCREEN_CHARS)
    if words:
        out["text"] = words
    return out or None


# He talks to an agent the way he talks to a person: "The master agent, do this", "hey Scout", "ask Darwin
# why". A name used like that is his choice, not Jev's guess (The owner, 31 Oct 2026: "If I am even
# saying the agent's name like the master agent, do this. It sends to the assistant"). A name only mentioned, as in
# "five scout" or "Darwin's theory", is left to Jev.
_CALLED = r"(?:^\W*(?:(?:hey|hi|hello|ok|okay|yo|dear)\W+)?@?|@|\b(?:ask|tell|to|for|with)\s+)"


def _named(text, roster):
    """The agent he addressed by name in `text`, or None. The first one addressed wins."""
    best = None
    for a in roster:
        for word in {a["name"], a["id"]}:
            word = (word or "").strip()
            if len(word) < 3:
                continue
            m = re.search(_CALLED + re.escape(word) + r"(?![\w-])", text or "", re.IGNORECASE)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), a["id"])
    return best[1] if best else None


def route(text, app=None, record=True, screen=None):
    """Jev's pick for one message: {agent, name, confidence, sure}. Never raises; any fault, or a
    pick Jev is not sure of, answers the fallback. `screen` comes from _screen()."""
    text = _clean(text)
    app = _clean(app, 120) or None
    roster = _roster()
    ids = {a["id"]: a["name"] for a in roster}
    fallback = FALLBACK if FALLBACK in ids else (roster[0]["id"] if roster else FALLBACK)
    key = (text, app, json.dumps(screen, sort_keys=True), tuple((a["id"], a["does"]) for a in roster))
    with _lock:
        hit = _cache.get(key)
    if hit:
        return dict(hit)
    started = time.monotonic()
    pick, confidence, why = None, 0.0, ""
    ask = text or ("(no words, only the screen)" if screen else "")
    named = _named(text, roster)
    if named:
        pick, confidence, why = named, 1.0, "named"
    elif not ask:
        why = "empty"
    elif not config.TYPESAFE_KEY:
        why = "no key"
    elif len(roster) < 2:
        why = "one agent"
    else:
        try:
            got = memory.post_jev(payload(ask, roster, app, screen), config.TYPESAFE_KEY,
                                  timeout=min(ROUTE_TIMEOUT, config.TYPESAFE_TIMEOUT))
            answer = ((got or {}).get("answers") or {}).get("agent") or {}
            if answer.get("choice") in ids:
                pick = answer["choice"]
                confidence = max(0.0, min(1.0, float(answer.get("confidence") or 0.0)))
            else:
                why = "no answer"
        except Exception as exc:
            why = memory._safe(str(exc)).split(":")[0][:80]
    sure = bool(pick) and confidence >= SURE
    agent = pick if sure else fallback
    out = {"agent": agent, "name": ids.get(agent, agent), "confidence": round(confidence, 3),
           "sure": sure, "jev": pick}
    ms = round((time.monotonic() - started) * 1000)
    if ask:
        log.info("assist route: %s %.2f%s, %d ms%s", pick or "-", confidence,
                 "" if sure else f", to {agent}", ms, f", {why}" if why else "")
    if pick and why != "named":
        with _lock:
            if len(_cache) > 200:
                _cache.clear()
            _cache[key] = out
        if record:
            _record({"kind": "route", "text": text, "app": app, "screen": bool(screen), "jev": pick,
                     "confidence": out["confidence"], "agent": agent, "ms": ms})
    return dict(out)


# -- sending --------------------------------------------------------------------------------

def _bytes(raw):
    if isinstance(raw, str) and raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        return base64.b64decode(raw or "", validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise ValueError("The picture did not arrive whole.")


def _save(rec, name, data):
    import io
    path, _ = files.receive(rec, name, io.BytesIO(data), len(data))
    return str(path)


def _compose(rec, text, body, voice=False):
    """His message as the agent reads it: his words, then where he was, then the attachments."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    head = "[From the assistant bar" + (", a voice memo, transcribed" if voice else "") + "]"
    parts = [f"{head} {text}"]
    shot, screen = body.get("screenshot"), _clean(body.get("screen_text"), SCREEN_CHARS)
    if shot or screen or body.get("url"):
        where = [f"app: {_clean(body.get('app'), 120)}"] if body.get("app") else []
        if body.get("url"):
            where.append(f"link: {_clean(body.get('url'), 2000)}")
        lines = ["He added his screen" + (f" ({', '.join(where)})" if where else "") + "."]
        if shot:
            data = _bytes(shot)
            if len(data) > MAX_PICTURE:
                raise ValueError("That screenshot is too big.")
            lines.append(f"[Attached: {_save(rec, f'screen-{stamp}.jpg', data)}]")
        if screen:
            path = _save(rec, f"screen-{stamp}.txt", str(body.get("screen_text"))[:SCREEN_CHARS]
                         .encode())
            lines.append(f"[Attached: {path}] (the text on his screen)")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _deliver(agent_id, text, body, voice=False, jev=None, how="typed"):
    rec = registry.get(agent_id)
    if not rec:
        raise ValueError("There is no agent by that name.")
    message = _compose(rec, text, body, voice=voice)
    # His message, typed or said, the same as one typed in the chat: the chat engine takes it, as
    # it does when the chat screen is open, so the answer and any ask card are in the chat.
    broker.switch(agent_id, "chat")
    outcome = broker.send(agent_id, message, wake=True)
    _record({"kind": "send", "text": text, "app": _clean(body.get("app"), 120) or None,
             "jev": jev, "agent": agent_id, "how": how, "voice": voice,
             "screen": bool(body.get("screenshot") or body.get("screen_text")),
             "outcome": outcome or "failed"})
    if outcome not in ("sent", "held"):
        raise RuntimeError(f"{rec.get('name') or agent_id} could not be reached.")
    from .server import hub
    hub.publish("agents")
    return rec, outcome


def _target(body, text):
    """(agent, jev pick, how): his own pick wins; otherwise Jev's, made now on the words and the
    screen as sent. The face the phone showed came from the words while he typed, often before the
    last words or the screen were in, so it is not trusted (The owner, 31 Oct 2026: "it barely
    routes"). The same words and screen hit the route cache, so this costs nothing then."""
    jev = body.get("jev") or None
    agent = (body.get("agent") or "").strip()
    if agent and body.get("picked"):
        return agent, jev, "picked"
    got = route(text, body.get("app"), record=False,
                screen=_screen(body.get("screen_text"), body.get("url")))
    return got["agent"], got["jev"], "routed"


def send(body):
    """POST /api/assist/send. A typed message is routed and delivered before the answer; a memo is
    taken, answered at once, and written down and delivered on a thread."""
    body = body if isinstance(body, dict) else {}
    if body.get("audio"):
        data = _bytes(body["audio"])
        if len(data) < voicenote.MIN_BYTES:
            raise ValueError("That memo was too short to hear.")
        if len(data) > voicenote.MAX_BYTES:
            raise ValueError("That memo is too long.")
        if not config.OPENAI_KEY:
            raise PermissionError("No OpenAI key is set, so voice memos are off.")
        threading.Thread(target=_memo, args=(data, dict(body, audio=None)), name="assist-memo",
                         daemon=True).start()
        return {"ok": True, "said": "Sent", "agent": None}
    text = _clean(body.get("text"))
    if not text and not body.get("screenshot"):
        raise ValueError("There is nothing to send.")
    agent, jev, how = _target(body, text)
    before = _turns(agent)
    rec, outcome = _deliver(agent, text or "(no words, only the screen)", body, jev=jev, how=how)
    name = rec.get("name") or agent
    return {"ok": True, "agent": agent, "name": name, "turns": before,
            "said": f"Sent to {name}" if outcome == "sent" else f"Held for {name}"}


def _turns(agent_id):
    rec = registry.get(agent_id)
    try:
        return transcript.summary(rec)["turns"] if rec else 0
    except Exception:
        return 0


# How long the bar waits for the agent's answer. A quick answer shows in the bar; a slow one is
# in the chat as always, and the bar has closed by then.
REPLY_WAIT = 45


def reply(agent_id, turns, wait=REPLY_WAIT):
    """GET /api/assist/reply: the agent's answer to what he just sent, once it has finished
    speaking, or {text: None} when it takes longer than `wait`. The same watch a cord keeps."""
    try:
        turns = int(turns)
    except (TypeError, ValueError):
        raise ValueError("turns is a number")
    started = time.time()
    while time.time() - started < wait:
        rec = registry.get(agent_id)
        if not rec:
            raise ValueError("There is no agent by that name.")
        now = transcript.summary(rec)
        if now["turns"] > turns:
            engine = broker.get(agent_id) or broker.chat(agent_id)
            if engine is None or not engine.working():
                return {"text": transcript.said_since(rec, turns) or now["last_message"],
                        "done": True}
        time.sleep(1.0)
    return {"text": None, "done": False}


def _memo(data, body):
    """The memo's slow half: written down, routed, delivered. A failure is logged and kept as a
    line in the routing log, so a lost memo is visible."""
    mime = (body.get("mime") or "audio/mp4").split(";")[0].strip().lower()
    try:
        got = voicenote.post_audio(data, f"memo.{voicenote.EXTENSIONS.get(mime, 'm4a')}", mime,
                                   config.OPENAI_KEY)
        voicenote._logged(got)
        text = _clean((got or {}).get("text"))
        if not text or not voicenote.SPOKEN.search(text):
            _record({"kind": "memo", "outcome": "nothing heard", "bytes": len(data)})
            return
        agent, jev, how = _target(body, text)
        _deliver(agent, text, body, voice=True, jev=jev, how=how)
        log.info("assist memo: %d bytes, %d words, to %s", len(data), len(text.split()), agent)
    except Exception as exc:
        log.warning("assist memo failed: %s", type(exc).__name__)
        _record({"kind": "memo", "outcome": f"failed: {type(exc).__name__}", "bytes": len(data)})
