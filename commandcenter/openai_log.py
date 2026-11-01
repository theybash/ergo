"""The OpenAI usage log: one line per OpenAI use, kept for every day.

Each line is one use: what it was for, the model, the minutes or tokens, and the dollar cost at
the published price. var/openai-usage.jsonl, appended, never rewritten.

    calls       a voice call, the front desk's or an agent's       GPT-Live-1, by the minute
    dictation   the chat's microphone, OpenAI engine       gpt-live-transcribe, by the minute
    voice note  a note, a recorded dictation, a voice gap  gpt-transcribe, by the minute
    speech      a spoken line, a voice preview             gpt-4o-mini-tts, by the minute
    vision      a picture read on a call                   GPT-6 Luna, by the token
    memory      call memory embeddings                     text-embedding-3-small, by the token

Not seen here: the voice's backend model on a call. OpenAI bills it inside the session, and the
browser holds that session, so the daemon never learns its tokens.

Prices are dollars, looked up on 18 October 2026. A price in var/openai-prices.json wins, so a
change needs no code.

    GET /api/openai/usage            -> {today, week, by_use, rows}
    POST /api/openai/used  {use, seconds}   the browser's dictation minutes
"""

import datetime
import json
import logging
import threading

from . import clock, config

log = logging.getLogger("cc.openai_log")

# Per minute, or per million tokens as {"in": .., "out": ..}.
PRICES = {
    "gpt-live-1": {"minute": 0.05},
    "gpt-live-transcribe": {"minute": 0.017},
    "gpt-transcribe": {"minute": 0.0045},
    "gpt-4o-mini-tts": {"minute": 0.015},
    "gpt-6-luna": {"in": 0.10, "out": 0.50},
    "text-embedding-3-small": {"in": 0.02, "out": 0.0},
}
USES = ("calls", "dictation", "voice note", "speech", "vision", "memory")
# Speech is billed by the minute of audio. Its length is not in the answer, so it is counted from
# the text: about fifteen characters a second of speech.
SPEECH_CHARS_PER_SECOND = 15.0

_lock = threading.Lock()


def _file():
    config.VAR.mkdir(parents=True, exist_ok=True)
    return config.VAR / "openai-usage.jsonl"


def prices():
    out = {k: dict(v) for k, v in PRICES.items()}
    try:
        extra = json.loads((config.VAR / "openai-prices.json").read_text())
        if isinstance(extra, dict):
            for k, v in extra.items():
                if isinstance(v, dict):
                    out[k] = v
    except (OSError, ValueError):
        pass
    return out


def cost(model, seconds=None, tokens_in=None, tokens_out=None):
    price = prices().get(model) or {}
    if seconds is not None and "minute" in price:
        return round(price["minute"] * seconds / 60.0, 6)
    if tokens_in is not None and "in" in price:
        return round((price["in"] * tokens_in + price.get("out", 0) * (tokens_out or 0)) / 1e6, 6)
    return None


def record(use, model, seconds=None, tokens_in=None, tokens_out=None):
    """One use. Never raises: a call or a note must not fail because of the log."""
    try:
        row = {"at": round(clock.now(), 1), "day": clock.local().strftime("%Y-%m-%d"),
               "use": use, "model": model}
        if seconds is not None:
            row["seconds"] = round(max(0.0, float(seconds)), 1)
        if tokens_in is not None:
            row["tokens_in"] = int(tokens_in)
            row["tokens_out"] = int(tokens_out or 0)
        row["usd"] = cost(model, row.get("seconds"), row.get("tokens_in"), row.get("tokens_out"))
        with _lock, _file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return row
    except Exception:
        log.exception("openai log: a line was not written")
        return None


def tokens_of(got):
    """(in, out) from an OpenAI answer's usage, or (None, None)."""
    usage = (got or {}).get("usage") if isinstance(got, dict) else None
    if not isinstance(usage, dict):
        return None, None
    tin = usage.get("input_tokens", usage.get("prompt_tokens"))
    tout = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return (tin, tout) if isinstance(tin, int) else (None, None)


def seconds_of(got):
    """The audio length an OpenAI transcription reports, or None."""
    usage = (got or {}).get("usage") if isinstance(got, dict) else None
    if isinstance(usage, dict) and usage.get("type") == "duration":
        return usage.get("seconds")
    if isinstance(got, dict) and isinstance(got.get("duration"), (int, float)):
        return got["duration"]
    return None


def summary(days=7):
    """Today, the last seven days, and today and the week by use."""
    today = clock.local().strftime("%Y-%m-%d")
    first = (clock.local() - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = []
    try:
        with _file().open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("day", "") >= first:
                    rows.append(row)
    except OSError:
        pass

    def total(sel):
        return round(sum(r.get("usd") or 0 for r in sel), 4)
    by_use = {}
    for r in rows:
        u = by_use.setdefault(r.get("use") or "?", {"today": 0.0, "week": 0.0, "count": 0,
                                                    "minutes": 0.0})
        u["week"] += r.get("usd") or 0
        u["count"] += 1
        u["minutes"] += (r.get("seconds") or 0) / 60.0
        if r.get("day") == today:
            u["today"] += r.get("usd") or 0
    for u in by_use.values():
        u["today"], u["week"], u["minutes"] = round(u["today"], 4), round(u["week"], 4), round(u["minutes"], 1)
    return {"today": total([r for r in rows if r.get("day") == today]), "week": total(rows),
            "by_use": by_use, "unpriced": sum(1 for r in rows if r.get("usd") is None)}


def reset():
    """For tests: nothing is held in memory."""
