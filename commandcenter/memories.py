"""What each agent remembers, read back for the Memory screen. Read only.

Two kinds of memory, and this module writes neither:

    an agent's       Claude Code keeps it in <projects>/<folder slug>/memory/: MEMORY.md is the
                     index, one line per file, and each file is one thing it learned
    the front desk's the call log, var/voice/calls.jsonl, every turn of every call; shown here one
                     day at a time

The folder slug is the agent's cwd with every character that is not a letter or a digit turned
into a dash. That is how Claude Code names it, so "/home/user/ergo/master" is
"-home-user-ergo-master".

The Memory screen asks in a view, and the view decides who it may see:

    personal, work   only the agents the view shows
    demo             nothing: the demo view is the one shown to strangers, and memory is the
                     most private thing an agent holds
    all, or none     every agent

The front desk's call log is in both personal and work, because past calls are free across those two
views (voice.FORGETS_PAST). The demo view forgets them, here too.

One file is read only when it sits directly inside that agent's own memory folder, by its real
path. A name with a slash, a dot file or a link that leads out is refused.
"""

import datetime
import json
import re

from . import clock, config, registry, vault, voice

VOICE_ID = "desk"

# A memory file name: one plain name ending in .md. No slash, so no other folder.
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# An index line: "- [Title](file.md)", a dash, then the hook. The dash can be a hyphen, a
# colon, or the long or short dash Claude Code writes, so the pattern spells those two as escapes.
INDEX_LINE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\(([^)\s]+)\)\s*(?:[\u2014\u2013:-]\s*)?(.*)$")

READ_MAX = 200_000
HOOK_MAX = 160
DAYS_MAX = 60


def slug(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd or ""))


def folder(rec):
    return config.PROJECTS / slug(rec.get("cwd")) / "memory"


def _agents(view):
    """The agents this view may see the memory of."""
    if view == "demo":
        return []
    out = []
    for rec in registry.all():
        if view in ("personal", "work") and not voice.in_view(rec, view):
            continue
        out.append(rec)
    return out


def _voice_shown(view):
    return view != "demo"


def _front(text):
    """The name and description of a file's front matter, when it has one."""
    out = {}
    if not text.startswith("---"):
        return out
    for line in text.splitlines()[1:40]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() in ("name", "description") and value.strip():
            out[key.strip()] = value.strip()
    return out


def _clip(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= HOOK_MAX else text[:HOOK_MAX - 1].rstrip() + "…"


def _index(rec):
    """The agent's memory list: MEMORY.md's lines first, then any file it does not name."""
    base = folder(rec)
    if not base.is_dir():
        return []
    rows, seen = [], set()
    try:
        index = (base / "MEMORY.md").read_text(errors="replace")
    except OSError:
        index = ""
    for line in index.splitlines():
        m = INDEX_LINE.match(line)
        if not m or not FILE_NAME.match(m.group(2)) or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        rows.append({"file": m.group(2), "title": m.group(1).strip(), "hook": _clip(m.group(3)),
                     "missing": not (base / m.group(2)).is_file()})
    try:
        rest = sorted(p for p in base.iterdir() if p.is_file() and FILE_NAME.match(p.name))
    except OSError:
        rest = []
    for path in rest:
        if path.name in seen or path.name == "MEMORY.md":
            continue
        try:
            head = _front(path.read_text(errors="replace")[:4000])
        except OSError:
            head = {}
        rows.append({"file": path.name, "title": head.get("name") or path.stem,
                     "hook": _clip(head.get("description")), "missing": False})
    return rows


def _rows():
    """The front desk's call log, spoken turns only, oldest first."""
    path = config.VOICE_DIR / "calls.jsonl"
    out = []
    try:
        with open(path, errors="replace") as fh:
            for raw in fh:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("who") in ("you", "voice") and row.get("text"):
                    out.append(row)
    except OSError:
        pass
    return out


def _day(at):
    return datetime.datetime.fromtimestamp(float(at))


def _telling(text):
    """True for a line worth showing as a day's hook: four words or more, not the greeting."""
    words = str(text or "").split()
    return len(words) >= 4 and not str(text).startswith("Welcome back")


def _voice_days():
    """One entry per day of calls, newest first. The hook is the first thing the owner said that is
    four words or more, or else the front desk's first such line, or else the first line of all."""
    days = {}
    for row in _rows():
        try:
            when = _day(row["at"])
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        key = when.strftime("%Y-%m-%d")
        held = days.setdefault(key, {"file": key, "title": when.strftime("%a %d %b"),
                                     "first": "", "his": "", "hers": ""})
        held["first"] = held["first"] or row["text"]
        if _telling(row["text"]):
            slot = "his" if row["who"] == "you" else "hers"
            held[slot] = held[slot] or row["text"]
    out = []
    for key in sorted(days, reverse=True)[:DAYS_MAX]:
        held = days[key]
        out.append({"file": key, "title": held["title"], "missing": False,
                    "hook": _clip(held["his"] or held["hers"] or held["first"])})
    return out


def listing(view=None):
    """Every agent this view may see, each with its memory list."""
    view = view if view in config.VIEW_NAMES else None
    agents = [{"id": rec["id"], "name": rec.get("name") or rec["id"], "side": vault.side_of(rec),
               "memory": _index(rec)} for rec in _agents(view)]
    if _voice_shown(view):
        agents.insert(0, {"id": VOICE_ID, "name": "The front desk", "side": None, "voice": True,
                          "memory": _voice_days()})
    return {"view": view, "agents": agents, "at": clock.now()}


def read(agent_id, name, view=None):
    """One memory file, or one day of the front desk's calls. LookupError when this view may not see it."""
    view = view if view in config.VIEW_NAMES else None
    name = str(name or "")
    if agent_id == VOICE_ID:
        if not _voice_shown(view) or not DAY.match(name):
            raise LookupError("no such memory")
        lines = []
        for row in _rows():
            try:
                when = _day(row["at"])
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            if when.strftime("%Y-%m-%d") == name:
                who = "The owner" if row["who"] == "you" else "The front desk"
                lines.append(f"{when.strftime('%H:%M')} {who}: {row['text']}")
        if not lines:
            raise LookupError("no such memory")
        return {"agent": VOICE_ID, "file": name, "text": "\n\n".join(lines)[:READ_MAX]}
    rec = next((r for r in _agents(view) if r["id"] == agent_id), None)
    if not rec or not FILE_NAME.match(name):
        raise LookupError("no such memory")
    base = folder(rec)
    try:
        real = (base / name).resolve(strict=True)
        if real.parent != base.resolve(strict=True) or not real.is_file():
            raise LookupError("no such memory")
        with open(real, errors="replace") as fh:
            text = fh.read(READ_MAX)
    except OSError:
        raise LookupError("no such memory") from None
    return {"agent": rec["id"], "file": name, "text": text}
