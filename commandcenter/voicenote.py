"""A voice note: the phone records, OpenAI writes it down, an agent reads it.

The wheel's Voice note choice records while he holds it and sends the file here on release. The
daemon, which holds the key, sends the audio to OpenAI's transcription endpoint and hands the
text to the agent as a message, the same way a typed chat message would arrive.

The key is the daemon's OpenAI key (OPENAI_API_KEY). OpenAI keeps nothing for the audio endpoint
beyond its own retention; the file itself is never written to disk here.

    POST /api/voice/note       {agent, mime, audio: base64, send?}  ->  {ok, text, said?}
    POST /api/voice/note/send  {agent, text}                        ->  {ok, said}

`send: false` writes the note down and sends nothing, so the phone can show the words with Send,
Edit and Cancel. The words he approves, edited or not, then go through /note/send. An older app
that sends no `send` field still gets the one step it was built for.
"""

import base64
import binascii
import json
import re
import logging
import urllib.error
import urllib.request
import uuid

from . import broker, config, registry

log = logging.getLogger("cc.voicenote")

TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
# The most audio one note may carry, decoded. Two minutes of m4a is well under this; the cap is
# OpenAI's own file limit, so a larger file would fail there anyway.
MAX_BYTES = 25 * 1024 * 1024
MIN_BYTES = 1000

HINT = "The owner speaking English or Hindi, often mixed in one sentence."
# Dictation in the chat is English only, his choice: the mixed hint put Hindi words into English.
ENGLISH = "The owner speaking English with an Indian accent. Write it in English only."

# Letters of the two languages he speaks. A transcript with none of them is the model inventing
# words from silence, not a note.
SPOKEN = re.compile(r"[A-Za-z\u0900-\u097F]")

EXTENSIONS = {"audio/mp4": "m4a", "audio/m4a": "m4a", "audio/aac": "m4a", "audio/webm": "webm",
              "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
              "audio/3gpp": "3gp"}


def _logged(got):
    """One line in the OpenAI usage log: the seconds the answer reports, or its tokens."""
    from . import openai_log
    seconds = openai_log.seconds_of(got)
    tin, tout = openai_log.tokens_of(got)
    openai_log.record("voice note", config.VOICE_TRANSCRIBE_MODEL, seconds=seconds,
                      tokens_in=None if seconds is not None else tin, tokens_out=tout)


def post_audio(data, filename, mime, key, english=False):
    """One multipart request to the transcription endpoint. Its own function so a test can stand
    in for OpenAI."""
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{value}\r\n".encode())

    field("model", config.VOICE_TRANSCRIBE_MODEL)
    field("response_format", "json")
    # A hint, not a lock: he speaks English, Hindi, or the two mixed. Without it, silence came back
    # as invented Chinese and Bengali words.
    field("prompt", ENGLISH if english else HINT)
    if english:
        field("language", "en")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode())
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(TRANSCRIBE_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(req, timeout=config.VOICE_TRANSCRIBE_TIMEOUT) as resp:
        return json.loads(resp.read())


def transcribe(body):
    """The words in one recording, sent nowhere. For what he says while a call is still
    connecting, so it reaches the front desk once the call is up. Returns {"text"}; empty when nothing
    was heard, which is not an error here: most of the time he says nothing in that gap."""
    body = body if isinstance(body, dict) else {}
    if not config.OPENAI_KEY:
        raise PermissionError("No OpenAI key is set, so this is off.")
    raw = body.get("audio") or ""
    if isinstance(raw, str) and raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise ValueError("The recording did not arrive whole.")
    if len(data) < MIN_BYTES or len(data) > MAX_BYTES:
        return {"text": ""}
    mime = (body.get("mime") or "audio/webm").split(";")[0].strip().lower()
    try:
        got = post_audio(data, f"heard.{EXTENSIONS.get(mime, 'webm')}", mime, config.OPENAI_KEY,
                         english=bool(body.get("english")))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("voice gap: transcription failed, %s", type(exc).__name__)
        raise RuntimeError("OpenAI could not write it down.")
    _logged(got)
    text = " ".join(str((got or {}).get("text") or "").split())
    if not SPOKEN.search(text):
        text = ""
    log.info("voice gap: %d bytes, %d words heard while connecting", len(data), len(text.split()))
    return {"text": text}


def note(body):
    """Transcribe one note and send it. Raises ValueError for his mistakes, PermissionError for a
    missing key, and RuntimeError when OpenAI fails."""
    body = body if isinstance(body, dict) else {}
    if not config.OPENAI_KEY:
        raise PermissionError("No OpenAI key is set, so voice notes are off.")
    rec = registry.get((body.get("agent") or config.HOME_AGENT).strip())
    if not rec:
        raise ValueError("There is no agent by that name.")
    raw = body.get("audio") or ""
    if isinstance(raw, str) and raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise ValueError("The recording did not arrive whole.")
    if len(data) < MIN_BYTES:
        raise ValueError("That note was too short to hear.")
    if len(data) > MAX_BYTES:
        raise ValueError("That note is too long. Keep it under two minutes.")
    mime = (body.get("mime") or "audio/mp4").split(";")[0].strip().lower()
    ext = EXTENSIONS.get(mime, "m4a")

    try:
        got = post_audio(data, f"note.{ext}", mime, config.OPENAI_KEY)
    except urllib.error.HTTPError as exc:
        log.warning("voice note: transcription refused, %s", exc.code)
        raise RuntimeError(f"OpenAI refused the note ({exc.code}).")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("voice note: transcription failed, %s", type(exc).__name__)
        raise RuntimeError("OpenAI could not be reached. Try again.")
    _logged(got)
    text = " ".join(str((got or {}).get("text") or "").split())
    if not text or not SPOKEN.search(text):
        log.info("voice note: %d bytes, nothing heard", len(data))
        raise ValueError("Nothing heard in that note.")

    if body.get("send") is False:
        log.info("voice note: %d bytes, %d words, written down for %s to check", len(data),
                 len(text.split()), rec["id"])
        return {"ok": True, "text": text, "agent": rec["id"]}
    got = _deliver(rec, text)
    log.info("voice note: %d bytes, %d words, to %s, %s", len(data), len(text.split()),
             rec["id"], got["outcome"])
    return {"ok": True, "text": text, "said": got["said"], "agent": rec["id"]}


# The most text one approved note may carry. A two minute note is a few hundred words.
MAX_TEXT = 4000


def _deliver(rec, text):
    outcome = broker.send(rec["id"], f"[Voice note from the owner, transcribed] {text}", wake=True)
    if outcome not in ("sent", "held"):
        raise RuntimeError(f"{rec['id']} could not be reached.")
    said = f"Sent to {rec['id']}." if outcome == "sent" else f"Held for {rec['id']}."
    return {"outcome": outcome, "said": said}


def send_text(body):
    """POST /api/voice/note/send: the words he checked, edited or not, sent to the agent."""
    body = body if isinstance(body, dict) else {}
    rec = registry.get((body.get("agent") or config.HOME_AGENT).strip())
    if not rec:
        raise ValueError("There is no agent by that name.")
    text = " ".join(str(body.get("text") or "").split())
    if not text:
        raise ValueError("There is nothing to send.")
    if len(text) > MAX_TEXT:
        raise ValueError("That note is too long to send.")
    got = _deliver(rec, text)
    # The length only: the words are his and the log is not the place for them.
    log.info("voice note: %d words checked and sent to %s, %s", len(text.split()), rec["id"],
             got["outcome"])
    return {"ok": True, "said": got["said"], "agent": rec["id"]}
