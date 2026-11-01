"""Dictation: the chat's microphone writes what he says into the message box, live.

Three engines, chosen on the Config page:

    openai   OpenAI live transcription, on the work key. The default, by his choice. Words arrive
             one at a time, about a fifth of a second after they are said.
    gemini   Gemini's live transcription model, on his personal key. A running draft about twice a
             second, made final at each pause.
    record   Not live: the browser records, and the whole clip is written down when he stops,
             through the same route a voice note uses.

The browser never holds a real key. For a live engine the daemon mints a short lived key that can
open one transcription session and nothing else, and the browser streams the microphone straight to
the provider with it. The audio does not pass through this laptop, so a slow tailnet does not slow
the words down.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, logs
from .voicenote import ENGLISH

log = logs.get(__name__)

ENGINES = ("openai", "gemini", "record")
DEFAULT = "openai"

OPENAI_SECRET_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_STREAM_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
GEMINI_TOKEN_URL = "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"
GEMINI_STREAM_URL = ("wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage."
                     "v1alpha.GenerativeService.BidiGenerateContentConstrained")

# A dictation is a message, not a meeting. Five minutes, then it stops by itself.
MAX_SECONDS = 300
# How long a minted key may wait before the browser opens its session with it.
OPEN_WITHIN = 60

_lock = threading.Lock()


def _file():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "dictation.json"


def settings():
    """Which engine the chat's microphone uses, and the choices."""
    try:
        engine = json.loads(_file().read_text()).get("engine")
    except (OSError, ValueError):
        engine = None
    return {"engine": engine if engine in ENGINES else DEFAULT, "engines": list(ENGINES),
            "default": DEFAULT}


def save_settings(body):
    engine = (body or {}).get("engine")
    if engine not in ENGINES:
        raise ValueError("engine must be one of " + ", ".join(ENGINES))
    with _lock:
        path = _file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"engine": engine}))
        tmp.replace(path)
    log.info("dictation engine set to %s", engine)
    return settings()


def _gemini_key():
    """The Gemini key: from the environment, or from the file config.GEMINI_ENV names."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        for line in Path(config.GEMINI_ENV).read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _post(url, payload, headers):
    """One POST. Replaced in tests. Errors say what went wrong and never carry a key."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=config.VOICE_MINT_TIMEOUT) as answer:
            return json.loads(answer.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read().decode()).get("error", {}).get("message") or ""
        except Exception:
            said = ""
        raise RuntimeError(f"The transcription service refused ({exc.code}). {said}".strip()) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"The transcription service could not be reached: {exc.reason}") from None


def _openai():
    if not config.OPENAI_KEY:
        raise PermissionError("No OpenAI key is set, so live dictation on OpenAI is off.")
    session = {
        "type": "transcription",
        "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "transcription": {"model": config.DICTATE_OPENAI_MODEL, "prompt": ENGLISH,
                              "languages": ["en"], "delay": "low"},
            # No turns: one recording is one message, finished when he taps done.
            "turn_detection": None,
        }},
    }
    got = _post(OPENAI_SECRET_URL,
                {"expires_after": {"anchor": "created_at", "seconds": OPEN_WITHIN}, "session": session},
                {"Authorization": f"Bearer {config.OPENAI_KEY}"})
    token = got.get("value")
    if not token:
        raise RuntimeError("OpenAI gave no key for the session.")
    return {"engine": "openai", "url": OPENAI_STREAM_URL,
            "protocols": ["realtime", "openai-insecure-api-key." + token], "rate": 24000}


def _gemini():
    key = _gemini_key()
    if not key:
        raise PermissionError("No Gemini key is set, so live dictation on Gemini is off.")
    setup = {"model": "models/" + config.DICTATE_GEMINI_MODEL,
             "generationConfig": {"responseModalities": ["TEXT"]},
             "inputAudioTranscription": {"languageCodes": ["en-IN"]}}
    now = datetime.now(timezone.utc)
    got = _post(GEMINI_TOKEN_URL, {
        "uses": 1,
        "expireTime": (now + timedelta(seconds=MAX_SECONDS + 60)).isoformat(),
        "newSessionExpireTime": (now + timedelta(seconds=OPEN_WITHIN)).isoformat(),
        "bidiGenerateContentSetup": setup,
    }, {"x-goog-api-key": key})
    token = got.get("name")
    if not token:
        raise RuntimeError("Gemini gave no key for the session.")
    return {"engine": "gemini", "url": GEMINI_STREAM_URL + "?access_token=" + token,
            "setup": setup, "rate": 16000}


def session(body=None):
    """What the browser needs to start dictating with the chosen engine, or the one it names."""
    engine = (body or {}).get("engine") or settings()["engine"]
    if engine not in ENGINES:
        raise ValueError("engine must be one of " + ", ".join(ENGINES))
    if engine == "openai":
        out = _openai()
    elif engine == "gemini":
        out = _gemini()
    else:
        out = {"engine": "record"}
    out["max_seconds"] = MAX_SECONDS
    log.info("dictation: %s session opened", engine)
    return out
