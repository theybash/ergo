"""The session file on disk, read as a ledger.

Claude Code writes every conversation to a JSON-lines file. It is not how the console shows the
terminal, because that file is flushed at turn boundaries and would never stream. It is how the
console knows things the terminal cannot tell it reliably:

    what the agent last said        the terminal has it, wrapped and repainted; the file has
                                    the text
    what the agent has cost         token counts per turn, with the model that was charged
    whether the conversation exists at all, which is how waking decides between starting fresh
                                    and resuming

Scraping the terminal for any of that would mean reading a redrawn box with escape sequences in
it. The file is the same information already structured, so it is used for facts and the
terminal is used for the picture.

One session file on this laptop is already twenty-seven megabytes. So it is read from the end on
first sight and by byte offset after that, never from the top.
"""

import collections
import json
import re
import threading
import time
from pathlib import Path

from . import config

_lock = threading.RLock()
_readers = {}

# Tool calls and thinking are shown in the terminal. Here they only need to be recognised, so
# the last message is the agent's words rather than the tail of a file listing.
TEXT_BLOCKS = ("text",)

# Tools whose whole input the chat needs, because it draws them as cards rather than as a call:
# the agent's question, a card from the show tool, and a feed post, drawn as the feed's card.
DRAWN_TOOLS = ("AskUserQuestion", "mcp__platform__show", "mcp__platform__feed_post")

# His tap on a choice card comes back to the agent as one machine line, with the id of the call
# that drew the card. The chat folds it into that card instead of drawing it as his message.
CHOICE = re.compile(r"^\[Choice ([A-Za-z0-9_-]+)\] .*? picked: (.+)$", re.S)


def session_file(rec):
    """The file holding this agent's conversation, or nothing if it has never spoken.

    The obvious path is checked first, then every project directory, because an agent whose
    working directory moved still owns its conversation and losing it would silently start a
    blank one on the next wake.
    """
    if rec.get("engine") == "codex":
        # A Codex agent keeps its conversation in Codex's own folder, under the id Codex gave it.
        from . import codex
        return codex.session_file(rec)
    name = f"{rec['session_id']}.jsonl"
    direct = config.project_dir(rec["cwd"]) / name
    if direct.exists():
        return direct
    try:
        for found in config.PROJECTS.glob(f"*/{name}"):
            return found
    except OSError:
        pass
    return None


def tail_lines(path, count, max_bytes=8 << 20):
    """The last `count` complete lines, without reading the file from the top."""
    size, _, lines = tail_lines_at(path, count, max_bytes)
    return size, [ln for _, ln in lines]


def tail_lines_at(path, count, max_bytes=8 << 20):
    """The last `count` complete lines, each with the byte offset where it starts.

    Seeks backwards in blocks until enough newlines have been passed. The byte ceiling is there
    so a file with one enormous line cannot pull the whole thing into memory. Also says whether
    the lines reach back to the first line of the file. A session file only grows at the end, so
    a line's offset never changes, and the chat keys its rows on it.
    """
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        block, data, pos = 65536, b"", size
        while pos > 0 and data.count(b"\n") <= count and (size - pos) < max_bytes:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
        lines, at = [], pos
        for ln in data.split(b"\n"):
            lines.append((at, ln))
            at += len(ln) + 1
        # The first piece is only a whole line if the read reached the start of the file.
        if pos > 0:
            lines = lines[1:]
        kept = lines[-count - 1:]
        start = pos == 0 and len(kept) == len(lines)
        return size, start, [(off, ln) for off, ln in kept if ln.strip()]


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _text_of(content):
    parts = []
    for b in _blocks(content):
        if b.get("type") in TEXT_BLOCKS and b.get("text"):
            parts.append(b["text"])
    return "\n".join(parts).strip()


class Reader:
    """Accumulated facts about one conversation, kept up to date by byte offset."""

    def __init__(self, rec):
        self.agent_id = rec["id"]
        self.path = None
        self.offset = 0
        self.turns = 0
        self.last_message = ""
        self.last_message_at = None
        # The newest few messages with their turn numbers. A cord reads a whole reply from here,
        # because one reply can be several messages with tools between them.
        self.said = collections.deque(maxlen=40)
        self.last_user_at = None
        self.tokens = {}          # model -> counters
        self.tools_used = 0
        self.checked = 0.0

    # -- ingesting ---------------------------------------------------------------

    def _entry(self, entry):
        if "payload" in entry:
            # A Codex rollout line. Every Claude Code line has "message" and none has "payload".
            from . import codex
            codex.read_entry(self, entry)
            return
        # A sidechain is a subagent's private conversation. Its tokens are real and are
        # counted; its words are not what the agent said to the operator.
        side = bool(entry.get("isSidechain"))
        kind = entry.get("type")
        msg = entry.get("message")
        if not isinstance(msg, dict):
            return
        ts = entry.get("timestamp")

        if kind == "assistant":
            usage = msg.get("usage") or {}
            model = msg.get("model") or "unknown"
            row = self.tokens.setdefault(model, {"input": 0, "output": 0,
                                                 "cache_write": 0, "cache_read": 0})
            row["input"] += usage.get("input_tokens", 0) or 0
            row["output"] += usage.get("output_tokens", 0) or 0
            row["cache_write"] += usage.get("cache_creation_input_tokens", 0) or 0
            row["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0

            for b in _blocks(msg.get("content")):
                if b.get("type") == "tool_use":
                    self.tools_used += 1
            if not side:
                text = _text_of(msg.get("content"))
                if text:
                    self.turns += 1
                    self.said.append((self.turns, text))
                    self.last_message = text
                    self.last_message_at = ts

        elif kind == "user" and not side:
            self.last_user_at = ts

    def refresh(self, rec, first_load=config.CHAT_PAGE):
        """Read whatever is new. Cheap enough to call on every poll."""
        path = session_file(rec)
        if path is None:
            return self
        if path != self.path:
            self.path, self.offset = path, 0

        try:
            size = path.stat().st_size
        except OSError:
            return self

        if size < self.offset:
            # The file shrank, which means it was replaced. Start again rather than read from
            # an offset that now points into the middle of a line.
            self.offset = 0

        if self.offset == 0 and size > 0:
            self.offset, lines = tail_lines(path, first_load * 6)
        elif size > self.offset:
            with open(path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(size - self.offset)
            # Stop at the last newline. A partial line means the write is still in progress,
            # and parsing half a record would drop the whole turn.
            cut = chunk.rfind(b"\n")
            if cut == -1:
                return self
            self.offset += cut + 1
            lines = chunk[:cut].split(b"\n")
        else:
            self.checked = time.time()
            return self

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                self._entry(json.loads(line))
            except Exception:
                continue
        self.checked = time.time()
        return self

    # -- reporting ---------------------------------------------------------------

    def cost(self):
        """Dollars, when a price list exists. Nothing when it does not.

        Prices change and are not knowable from here, so an absent price list means the console
        reports tokens. A token count is a fact; a guessed cost is a number that looks like one.
        """
        table = config.prices()
        if not table:
            return None
        total = 0.0
        for model, row in self.tokens.items():
            p = table.get(model) or table.get(model.split("-2")[0]) or table.get("default")
            if not p:
                continue
            total += (row["input"] / 1e6) * p.get("input", 0)
            total += (row["output"] / 1e6) * p.get("output", 0)
            total += (row["cache_write"] / 1e6) * p.get("cache_write", p.get("input", 0))
            total += (row["cache_read"] / 1e6) * p.get("cache_read", 0)
        return round(total, 4)

    def totals(self):
        out = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        for row in self.tokens.values():
            for k in out:
                out[k] += row[k]
        return out

    def summary(self):
        t = self.totals()
        return {
            "exists": self.path is not None,
            "turns": self.turns,
            "last_message": self.last_message,
            "last_message_at": self.last_message_at,
            "last_user_at": self.last_user_at,
            "tools_used": self.tools_used,
            "tokens": t,
            # Everything the agent has read across every turn, added up. This is what a bill is
            # made of, and it is not the size of the current context window: cached tokens are
            # counted again each time they are read.
            "context_tokens": t["input"] + t["cache_write"] + t["cache_read"],
            "by_model": self.tokens,
            "cost_usd": self.cost(),
        }


def reader(rec):
    with _lock:
        r = _readers.get(rec["id"])
        if r is None:
            r = _readers[rec["id"]] = Reader(rec)
    return r


def summary(rec):
    from . import opencode
    if opencode.is_opencode(rec):
        return opencode.summary(rec)
    return reader(rec).refresh(rec).summary()


def last_message(rec):
    """What the agent said last. This is the payload a transfer cord carries."""
    return reader(rec).refresh(rec).last_message


def said_since(rec, turns):
    """Every message after turn number `turns`, joined. What an agent said in one reply."""
    r = reader(rec).refresh(rec)
    return "\n\n".join(text for n, text in r.said if n > turns)


def recent(rec, count=20):
    """The rows of `page`, without the flag."""
    return page(rec, count)["messages"]


def page(rec, count=20):
    """The last few things said in a conversation, as plain text with who said them.

    The `Reader` above keeps only the newest message, because that is all a card shows and all a
    transfer cord carries. Reading a conversation is a different question, asked by the master when
    it wants to know where another agent got to, so it is answered from the file directly.

    Read from the end of the file, never the top. A session file grows without limit and the
    interesting part is always the last page of it.

    Each row carries `pos`, the byte offset of its line and its place in that line, which stays
    the same however much of the file is read.

    `more` is true unless the read reached the first row of the file. A count of rows that came
    back short says nothing, because a raw line is not always a row.
    """
    from . import opencode
    if opencode.is_opencode(rec):
        rows = opencode.recent(rec, count + 1)
        return {"messages": rows[-count:], "more": len(rows) > count}
    path = session_file(rec)
    if path is None:
        return {"messages": [], "more": False}
    if rec.get("engine") == "codex":
        from . import codex
        rows = codex.recent(path, count + 1)
        return {"messages": rows[-count:], "more": len(rows) > count}
    # Three lines per turn is a guess on the generous side: tool calls and their results each take a
    # line, so asking for more raw lines than messages is what makes the count come out right.
    # A long history reaches further back than the usual eight megabytes, so the ceiling grows with
    # what was asked for.
    _, start, lines = tail_lines_at(path, max(count * 6, 40),
                                    max_bytes=(8 << 20) if count <= 200 else (64 << 20))
    out, done = [], 0

    def mark(off):
        # The rows one line gave, numbered in the order they came in it.
        nonlocal done
        for i, row in enumerate(out[done:]):
            row["pos"] = f"{off}.{i}"
        done = len(out)

    off = 0
    for off_next, raw in lines:
        mark(off)
        off = off_next
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if entry.get("isSidechain"):
            continue
        kind = entry.get("type")
        # A message he sends while the agent is mid turn is not a user row: Claude Code saves it as
        # a queued_command attachment and hands it to the model inside the running turn. Drawn
        # from user rows only, those messages were missing from the chat. Background task notices
        # travel the same way and are not his words, so only the "prompt" kind counts.
        if kind == "attachment":
            att = entry.get("attachment") or {}
            if att.get("type") == "queued_command" and att.get("commandMode", "prompt") == "prompt":
                text = _text_of(att.get("prompt"))
                if text:
                    out.append({"kind": "text", "who": "operator", "at": entry.get("timestamp"), "text": text})
            continue
        if kind not in ("assistant", "user"):
            continue
        # Claude Code writes its own notes into the session as user rows marked isMeta: the size an
        # image was scaled to after a Read, a skill being loaded, a command's caveat. Nobody typed
        # them, and drawn as the owner's messages they read like a message he never sent.
        if kind == "user" and entry.get("isMeta"):
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        at = entry.get("timestamp")
        who = "agent" if kind == "assistant" else "operator"
        # The summary Claude Code writes when it compacts the conversation. He did not write it, and
        # it is long: the chat folds it under "Compacted".
        if kind == "user" and entry.get("isCompactSummary"):
            text = _text_of(msg.get("content"))
            if text:
                out.append({"kind": "text", "who": "operator", "at": at, "text": text.strip(),
                            "compacted": True})
            continue

        # One entry can hold several blocks: the agent says something, then calls a tool. They are
        # separate rows in a chat, so they are separate items here. The order inside the entry is
        # the order they happened.
        for block in _blocks(msg.get("content")):
            btype = block.get("type")
            if btype in TEXT_BLOCKS and (block.get("text") or "").strip():
                out.append({"kind": "text", "who": who, "at": at,
                            "text": block["text"].strip()})
            elif btype == "tool_use":
                row = {"kind": "tool", "who": "agent", "at": at,
                       "tool": block.get("name"), "id": block.get("id"),
                       # The whole input, trimmed. A chat shows the command it ran or the file it
                       # wrote, which is the only part anyone reads.
                       "input": _short(block.get("input"))}
                if block.get("name") in DRAWN_TOOLS:
                    # A question with options, or a card, and the console draws it as that rather
                    # than as a tool call, so it needs the structure rather than a summary.
                    row["inputRaw"] = json.dumps(block.get("input"))
                out.append(row)
            elif btype == "tool_result":
                body = block.get("content")
                out.append({"kind": "result", "who": "agent", "at": at,
                            "id": block.get("tool_use_id"),
                            "error": bool(block.get("is_error")),
                            "text": _short(body, limit=1200)})
    mark(off)
    out = _fold_choices(out)
    return {"messages": out[-count:], "more": not start or len(out) > count}


def _fold_choices(rows):
    """His pick on a choice card, written onto the card as its answer, and his line taken out.
    A pick whose card is too far back to be here stays a line, and the chat draws it quietly.
    A card the tool refused is marked, so the chat does not draw data the tool said was wrong."""
    cards = {r["id"]: r for r in rows if r["kind"] == "tool" and r.get("tool") == DRAWN_TOOLS[1]}
    out = []
    for r in rows:
        if r["kind"] == "result" and r.get("error") and r.get("id") in cards:
            cards[r["id"]]["refused"] = True
        m = CHOICE.match(r.get("text") or "") if r["kind"] == "text" and r["who"] == "operator" else None
        if m and m.group(1) in cards:
            cards[m.group(1)]["answer"] = m.group(2).strip()
            continue
        out.append(r)
    return out


# The whole of one tool call, for the panel a tap on a command or a result opens. The chat's rows
# carry the first few hundred characters; this reads the call and its answer whole, up to a
# ceiling so a giant output cannot stall the phone.
TOOL_MAX = 400_000


def _lines_with(path, needle, want, chunk=1 << 20, limit=256 << 20):
    """The whole lines that hold `needle`, searched from the end of the file backwards in chunks,
    stopping once `want` are found. A call and its result sit next to each other, almost always
    near the end, so this reads a megabyte or two of a file that can be hundreds."""
    found = []
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        pos, carry, read = end, b"", 0
        while pos > 0 and read < limit and len(found) < want:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step) + carry
            read += step
            parts = block.split(b"\n")
            # The first part may be the tail of a line that starts in the next chunk back.
            carry = parts[0] if pos > 0 else b""
            for line in reversed(parts[1:] if pos > 0 else parts):
                if needle in line:
                    found.append(line)
        return found


def tool_call(rec, tool_id):
    """The call with this id and its result: {tool, input, result, error}, or None if not found."""
    from . import opencode
    if opencode.is_opencode(rec):
        return opencode.tool_call(rec, tool_id)
    path = session_file(rec)
    if path is None or not tool_id:
        return None
    if rec.get("engine") == "codex":
        from . import codex
        return codex.tool_call(path, tool_id)
    found = {}
    for raw in _lines_with(path, tool_id.encode(), want=2):
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        for block in _blocks((entry.get("message") or {}).get("content")):
            if block.get("type") == "tool_use" and block.get("id") == tool_id:
                inp = block.get("input")
                if isinstance(inp, dict) and set(inp) <= {"command", "description", "timeout", "run_in_background"} \
                        and isinstance(inp.get("command"), str):
                    shown = inp["command"]
                else:
                    shown = json.dumps(inp, indent=2, ensure_ascii=False)
                found.update(tool=block.get("name"), input=shown[:TOOL_MAX], at=entry.get("timestamp"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") == tool_id:
                body = block.get("content")
                text = body if isinstance(body, str) else "\n".join(
                    b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')}]" for b in _blocks(body))
                found.update(result=text[:TOOL_MAX], error=bool(block.get("is_error")),
                             cut=len(text) > TOOL_MAX)
    return found or None


# The newest recap per agent, keyed by the size the session file had when it was found. A recap
# is near the end of a big file, so finding one costs a read of the last few megabytes; a file
# that has not grown cannot have a newer recap, so that read happens once per new turn at worst.
_recaps = {}

# How much of the tail to search. A recap is written at the end of a stretch of work, so it is
# within the last page of the conversation; four megabytes is several hundred turns.
RECAP_LINES = 4000
RECAP_BYTES = 4 << 20


def recap(rec):
    """What the agent last wrote down about where it got to, or nothing.

    OpenCode writes no away-summary row, so an OpenCode agent answers none. That is the same
    answer every caller already handles for an agent that has never spoken.

    Claude Code writes a `system` row with `subtype: away_summary` when a stretch of work ends.
    It is the one line worth reading aloud, because it is the agent's own account of what it did
    rather than the last sentence it happened to say.

    Nothing writes these rows on this machine yet. That is fine: this returns None, and every
    caller already has to handle an agent that has never spoken.
    """
    from . import opencode
    if opencode.is_opencode(rec):
        return opencode.recap(rec)
    path = session_file(rec)
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None

    with _lock:
        held = _recaps.get(rec["id"])
    if held and held[0] == path and held[1] == size:
        return held[2]

    found = None
    try:
        _, lines = tail_lines(path, RECAP_LINES, max_bytes=RECAP_BYTES)
    except OSError:
        return None
    # Backwards, so the first hit is the newest one and the rest of the tail is never parsed.
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw or b"away_summary" not in raw:
            continue
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        if entry.get("type") != "system" or entry.get("subtype") != "away_summary":
            continue
        text = entry.get("content")
        if isinstance(text, list):
            text = _text_of(text)
        if isinstance(text, str) and text.strip():
            found = text.strip()
            break

    with _lock:
        _recaps[rec["id"]] = (path, size, found)
    return found


def _short(value, limit=600):
    """One line-ish summary of a tool input or result. Never the whole thing.

    A tool result can be a megabyte of file content, and 72% of a session's characters are exactly
    that. Sending it all to a browser to be truncated by CSS wastes the transfer and the memory.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for b in value:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or "")
            else:
                parts.append(str(b))
        text = "\n".join(str(p) for p in parts)
    elif isinstance(value, dict):
        # Tool inputs are small and named, so the useful form is key: value, not raw json.
        text = "\n".join(f"{k}: {v}" for k, v in value.items())
    else:
        text = str(value)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"\n… {len(text) - limit} more characters"


def forget(agent_id):
    with _lock:
        _readers.pop(agent_id, None)
        _recaps.pop(agent_id, None)
    from . import opencode
    opencode.forget(agent_id)
