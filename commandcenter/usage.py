"""What the fleet has used: the plan, the voice minutes and each agent's spend.

One answer for the Usage section of the Config screen. Only the plan limits call out, at most once
a minute each; every other number is already on this laptop.

    the plan        asked live from Anthropic (live_usage.py); when that fails,
                    var/plan-usage.json, written by Claude Code's status line, so it can be old
    Codex           asked live from ChatGPT (live_usage.py)
    voice           today's minutes against the daily cap, from voice.usage()
    each agent      the last "cost-state" row in its session file, plus every reply after it

The dollar figure in a cost-state row is what the same tokens would cost on the API. On a
subscription it is not a bill, and the screen says so.

Claude Code writes a cost-state row only now and then, and an agent can work for hours past the
last one. So the replies written after it are added on top: each carries its own token counts,
priced here per model. Claude Code also makes small side calls that are not in the file, so the
figure runs a little under what Claude Code would say, never over.

A session file can be eighty megabytes. It is read from the end in blocks and stops at the first
cost-state row it meets, which is the last one written. The whole list is kept for a minute, and
a file is not read again while its size and time stay the same.
"""

import json
import threading

from . import clock, config, live_usage, openai_log, registry, transcript, voice

CACHE_SECONDS = 60.0
# The status line writes the plan file after every reply. Older than this, the numbers are shown
# with the word "stale" after them.
PLAN_STALE_SECONDS = 30 * 60
BLOCK = 256 * 1024

_lock = threading.Lock()
_list = {"at": None, "agents": None}
_files = {}
_tails = {}


def _parse(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def scan(path):
    """The last cost-state row and the model of the last assistant row, read from the end.

    Returns (cost_state, model). Either is None when the file has none. Only lines that hold the
    right words are parsed, so a long file costs a read and a few substring checks per block.
    """
    cost, model, _ = _scan(path)
    return cost, model


def _scan(path):
    """scan(), and the byte just past the cost-state row, where the replies it misses begin."""
    cost, model, after = None, None, 0
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        pos, tail = fh.tell(), b""
        while pos > 0 and (cost is None or model is None):
            step = min(BLOCK, pos)
            pos -= step
            fh.seek(pos)
            lines = (fh.read(step) + tail).split(b"\n")
            # The first piece is only a whole line once the read has reached the top.
            start = pos
            if pos > 0:
                tail = lines.pop(0)
                start += len(tail) + 1
            else:
                tail = b""
            # Where each whole line begins in the file, so the cost-state row can be found again.
            begins = []
            for line in lines:
                begins.append(start)
                start += len(line) + 1
            for line, begin in zip(reversed(lines), reversed(begins)):
                if cost is None and b"cost-state" in line:
                    row = _parse(line)
                    if isinstance(row, dict) and row.get("type") == "cost-state":
                        cost, after = row, begin + len(line) + 1
                if model is None and (b'"type":"assistant"' in line
                                      or b'"type": "assistant"' in line):
                    row = _parse(line)
                    if isinstance(row, dict) and row.get("type") == "assistant":
                        got = (row.get("message") or {}).get("model")
                        # Claude Code writes <synthetic> for a message it made up itself.
                        if got and not got.startswith("<"):
                            model = got
                if cost is not None and model is not None:
                    break
    return cost, model, after


# Dollars per million tokens: input, output, cache read. A cache write costs 1.25 times the input
# price for five minutes and twice it for an hour. From Anthropic's price list, 6 October 2026.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}


def price(model, used):
    """What one reply's tokens cost on the API, or None for a model not in PRICES."""
    rates = PRICES.get(model or "")
    if rates is None:
        # A dated name such as claude-haiku-4-5-20251001 is the same model.
        rates = next((v for k, v in PRICES.items() if (model or "").startswith(k + "-2")), None)
    if rates is None:
        return None
    given, made, read = rates
    n = lambda key, box=used: _number((box or {}).get(key)) or 0
    split = used.get("cache_creation")
    if isinstance(split, dict):
        short, long = n("ephemeral_5m_input_tokens", split), n("ephemeral_1h_input_tokens", split)
    else:
        short, long = n("cache_creation_input_tokens"), 0
    return (n("input_tokens") * given + short * given * 1.25 + long * given * 2
            + n("cache_read_input_tokens") * read + n("output_tokens") * made) / 1e6


def _since(path, after):
    """The replies written after the cost-state row: (dollars, output tokens, input tokens).

    Kept per file and read on from where the last read stopped, so a growing file costs only its
    new lines. A reply is written as several rows with one message id, so the id is the key."""
    held = _tails.get(str(path))
    if not held or held["after"] != after:
        held = {"after": after, "upto": after, "replies": {}}
        _tails[str(path)] = held
    with open(path, "rb") as fh:
        fh.seek(held["upto"])
        chunk = fh.read()
    # Only whole lines. A row still being written is read next time.
    end = chunk.rfind(b"\n") + 1
    for line in chunk[:end].split(b"\n"):
        if b'"assistant"' not in line or b'"usage"' not in line:
            continue
        row = _parse(line)
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        msg = row.get("message") or {}
        if msg.get("id") and isinstance(msg.get("usage"), dict):
            held["replies"][msg["id"]] = (msg.get("model"), msg["usage"])
    held["upto"] += end
    dollars, out, given = 0.0, 0, 0
    for model, used in held["replies"].values():
        dollars += price(model, used) or 0.0
        out += _number(used.get("output_tokens")) or 0
        given += sum(_number(used.get(k)) or 0 for k in
                     ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return dollars, out, given


def _scan_cached(path):
    """(cost_state, model, (dollars, output tokens) since it), not read again while the file keeps
    its size and time."""
    try:
        st = path.stat()
    except OSError:
        return None, None, (0.0, 0, 0)
    key = (st.st_size, st.st_mtime_ns)
    held = _files.get(str(path))
    if held and held[0] == key:
        return held[1]
    try:
        cost, model, after = _scan(path)
        extra = _since(path, after) if cost else (0.0, 0, 0)
    except OSError:
        return None, None, (0.0, 0, 0)
    got = (cost, model, extra)
    _files[str(path)] = (key, got)
    return got


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def agent_usage(rec):
    """One row of the agents table. Every number is None when the file does not say."""
    row = {"id": rec["id"], "name": rec["name"], "cost_usd": None, "tokens_out": None,
           "tokens_in": None,
           "model": None, "session_started": None}
    path = transcript.session_file(rec)
    if path is None:
        return row
    cost, model, extra = _scan_cached(path)
    row["model"] = model
    # The scan reads to the top when it meets no reply, so no model means the file holds no
    # reply at all. A cost-state row there is not this agent's spend: Claude Code carries it over
    # from a copied conversation when it rewrites the file's settings rows.
    if cost and model:
        total = _number(cost.get("totalCostUSD"))
        row["cost_usd"] = None if total is None else round(total + extra[0], 2)
        models = cost.get("modelUsage")
        if isinstance(models, dict):
            row["tokens_out"] = sum(_number((m or {}).get("outputTokens")) or 0
                                    for m in models.values() if isinstance(m, dict)) + extra[1]
            # Everything the model read: new input, cache writes and cache reads. The re-read
            # conversation is most of it, and it is what the API would charge for.
            row["tokens_in"] = sum(sum(_number((m or {}).get(k)) or 0 for k in
                                       ("inputTokens", "cacheReadInputTokens",
                                        "cacheCreationInputTokens"))
                                   for m in models.values() if isinstance(m, dict)) + extra[2]
        started = _number(cost.get("startTime"))
        # Claude Code writes milliseconds. Everything else here speaks seconds.
        row["session_started"] = started / 1000.0 if started else None
    return row


def agents():
    """Every agent the daemon lists, most spent first. Kept for a minute."""
    with _lock:
        now = clock.now()
        if _list["agents"] is not None and now - _list["at"] < CACHE_SECONDS:
            return _list["agents"]
        rows = [agent_usage(r) for r in registry.all()]
        # No record sorts after a record of nought.
        rows.sort(key=lambda r: (r["cost_usd"] is None, -(r["cost_usd"] or 0.0)))
        _list["at"], _list["agents"] = now, rows
        return rows


def _window(value):
    if not isinstance(value, dict):
        return None
    return {"used_percentage": _number(value.get("used_percentage")),
            "resets_at": _number(value.get("resets_at"))}


def plan():
    """The plan's own limits, live from Anthropic, else as the status line last saw them, else None."""
    live = live_usage.claude()
    if live:
        return live
    try:
        held = json.loads((config.VAR / "plan-usage.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(held, dict):
        return None
    at = _number(held.get("at"))
    return {"five_hour": _window(held.get("five_hour")),
            "seven_day": _window(held.get("seven_day")),
            "at": at,
            "stale": at is None or clock.now() - at > PLAN_STALE_SECONDS}


def snapshot():
    """The whole answer to GET /api/usage."""
    spent = voice.budget()
    return {
        "plan": plan(),
        "codex": live_usage.codex(),
        "voice": {"used_minutes": spent["used_minutes"], "cap_minutes": spent["daily_minutes"]},
        "openai": openai_log.summary(),
        "agents": agents(),
    }


def reset():
    """Forget everything held in memory. For tests, and for nothing else."""
    with _lock:
        _list["at"], _list["agents"] = None, None
        _files.clear()
        _tails.clear()
