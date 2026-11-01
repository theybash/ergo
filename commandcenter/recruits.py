"""Claude Code and OpenCode conversations on this laptop, offered as agents.

An agent is a session id and a folder, and nothing else. So every conversation either engine has
ever held here is already most of an agent: the transcript holds the words, and the record the
console keeps holds the name. This module reads Claude Code's folder of files and OpenCode's
database, and describes what is in them, so the console can offer them.

Two kinds of thing end up on the recruit screen and they are not the same kind:

    a hidden agent     the console made it, then a delete hid the record. Bringing it back is
                       one flag on a record that never left.
    a loose session    Claude Code made it, in a terminal, with no record here at all. Adopting
                       it writes a record that points at the file.

Nothing here starts a process. Adoption is a decision about the list of agents, and waking one
is a separate press, the same as for every other card.

Reading is deliberately cheap. One session file on this laptop is thirty one megabytes, there
can be hundreds of them, and this is a screen the owner opens rarely. So the list is chosen by
`stat` alone, only the newest files are opened at all, and the read of each one stops early.
"""

import datetime
import json
import os
import re

from . import config, logs, registry

log = logs.get(__name__)

# How much of the head of a file is parsed. The name and the first thing the owner typed are
# both written in the first few rows, so this is generous rather than tight.
HEAD_BYTES = 64 * 1024

# Where the scan gives up. Turns are a rough size signal on a row, not an accounting figure, so
# counting stops at five hundred, and it stops again at eight megabytes for a file whose rows
# are enormous.
MAX_TURNS = 500
SCAN_BYTES = 8 * 1024 * 1024

# The newest this many, and nothing under a kilobyte. A file that small is a session that opened
# and closed with nothing said in it.
MAX_SESSIONS = 200
MIN_BYTES = 1024

# One line on a card. Cut here rather than in the browser, so the wire carries a line and not a
# whole first briefing.
FIRST_CHARS = 160

# A folder Claude Code made for a scratch directory. The owner never wants those as agents, and
# there are dozens of them.
TMP_SLUG = "-tmp-"

# Two rows can carry a name the owner chose. custom-title is what /title writes; agent-name is
# what a session started by the platform's own tooling writes.
NAME_ROWS = ("custom-title", "agent-name")

_SPACES = re.compile(r"\s+")

# A session id arrives from a browser, so it is checked before it is joined onto a folder. This
# is the check that stops a name with a slash in it reading a file somewhere else.
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _iso(when):
    """A file time as the browser reads every other time here: an ISO string in UTC."""
    return datetime.datetime.fromtimestamp(when, datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _clean(text):
    """One line, cut to length. Newlines become spaces because the row has one line to show."""
    line = _SPACES.sub(" ", str(text or "")).strip()
    if len(line) <= FIRST_CHARS:
        return line or None
    return line[:FIRST_CHARS - 1].rstrip() + "…"


def _said(message):
    """What the person typed in one user row, or nothing if the row is machinery.

    A user row is not always a person. Tool results come back as user rows too, and so do the
    notes Claude Code injects, so only text blocks count.
    """
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return _clean(content)
    if not isinstance(content, list):
        return None
    parts = [b.get("text") for b in content
             if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return _clean(" ".join(parts)) if parts else None


def read_file(path):
    """The few facts one session file carries: its name, its first words, its folder, its size.

    One pass, and it stops as soon as it has what it needs. Past the head only user rows matter,
    and those are found with a substring test before anything is parsed, because Claude Code
    writes some files compactly and some with spaces and both are expensive to parse in full.
    """
    name = None
    first = None
    started = None
    cwd = None
    turns = 0
    read = 0
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                read += len(raw)
                past_head = read > HEAD_BYTES
                if past_head and (turns >= MAX_TURNS or read > SCAN_BYTES):
                    break
                if past_head and b'"user"' not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                kind = row.get("type")
                if kind == "user":
                    turns += 1
                    if started is None:
                        started = row.get("timestamp")
                    if cwd is None:
                        cwd = row.get("cwd")
                    if first is None and not row.get("isMeta"):
                        first = _said(row.get("message"))
                elif name is None and kind in NAME_ROWS:
                    said = (row.get("customTitle") or row.get("name")
                            or row.get("agentName") or "")
                    name = said.strip() or None
    except OSError:
        return None
    return {"name": name, "first": first, "started": started, "cwd": cwd, "turns": turns}


def _candidates():
    """Every session file worth looking at, newest first, with its size and its slug.

    Only the files sitting directly in a project folder. A folder named after a session id holds
    that session's subagents and its tool results, and none of those is a conversation the owner
    can adopt.
    """
    found = []
    try:
        slugs = sorted(config.PROJECTS.iterdir())
    except OSError:
        return found
    for folder in slugs:
        if folder.name.startswith(TMP_SLUG) or not folder.is_dir():
            continue
        try:
            entries = sorted(folder.glob("*.jsonl"))
        except OSError:
            continue
        for path in entries:
            try:
                info = path.stat()
            except OSError:
                continue
            if not os.path.isfile(path) or info.st_size < MIN_BYTES:
                continue
            found.append((info.st_mtime, info.st_size, folder.name, path))
    found.sort(key=lambda row: -row[0])
    return found[:MAX_SESSIONS]


def sessions():
    """Every adoptable conversation, newest first.

    Claude Code writes files; OpenCode writes a database. Both are offered here as the same
    kind of row, with `engine` saying which one will run it. `registered` says the console
    already has a record for this session, hidden or not. Those rows stay in the list on
    purpose: seeing that a session is already an agent is the answer to "where did that
    conversation go", and hiding them would leave the question open.
    """
    out = []
    for mtime, size, slug, path in _candidates():
        facts = read_file(path)
        if facts is None:
            continue
        session_id = path.stem
        cwd = facts["cwd"]
        out.append({
            "session_id": session_id,
            "cwd": cwd,
            "slug": slug,
            "name": facts["name"],
            "first": facts["first"],
            "started": facts["started"],
            "last": _iso(mtime),
            "size": size,
            "turns": facts["turns"],
            "engine": "claude",
            "registered": registry.by_session(session_id) is not None,
        })
    out.extend(_opencode_sessions())
    out.sort(key=lambda row: row.get("last") or "", reverse=True)
    return out


def _opencode_sessions():
    """The OpenCode half of the list, in the same shape as the Claude half."""
    from . import opencode
    out = []
    for row in opencode.sessions():
        row = dict(row)
        row["first"] = _clean(row.get("first"))
        row["name"] = (row.get("name") or "").strip() or None
        row["registered"] = registry.by_opencode_session(row["session_id"]) is not None
        out.append(row)
    return out


def find(session_id, engine="claude"):
    """One session by its id, or nothing at all.

    The file is looked for by name rather than found in the listing above, because the listing
    stops at the newest two hundred and a session the owner asked for by id is a session he
    means, however old it is. OpenCode is the same question asked of its database.
    """
    if (engine or "claude").strip() == "opencode":
        from . import opencode
        row = opencode.find(session_id)
        if row is None:
            return None
        row["first"] = _clean(row.get("first"))
        row["registered"] = registry.by_opencode_session(session_id) is not None
        return row
    if not SESSION_ID.match(session_id or ""):
        return None
    try:
        slugs = sorted(config.PROJECTS.iterdir())
    except OSError:
        return None
    for folder in slugs:
        if folder.name.startswith(TMP_SLUG) or not folder.is_dir():
            continue
        path = folder / f"{session_id}.jsonl"
        try:
            info = path.stat()
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        facts = read_file(path)
        if facts is None:
            continue
        return {
            "session_id": session_id,
            "cwd": facts["cwd"],
            "slug": folder.name,
            "name": facts["name"],
            "first": facts["first"],
            "started": facts["started"],
            "last": _iso(info.st_mtime),
            "size": info.st_size,
            "turns": facts["turns"],
            "engine": "claude",
            "registered": registry.by_session(session_id) is not None,
        }
    return None


def default_name(row, given=None):
    """What to call an adopted session.

    What the owner typed wins, then the name in the transcript, then the last part of the folder
    it ran in. The folder is the useful fallback because a conversation is nearly always about
    the thing it was held in.
    """
    for candidate in (given, row.get("name") if row else None):
        if candidate and candidate.strip():
            return candidate.strip()
    cwd = (row or {}).get("cwd") or ""
    last = os.path.basename(str(cwd).rstrip("/"))
    return last or (row or {}).get("session_id", "session")[:8]


def bring_back(agent_id):
    """Show an agent the console hid, and write down that it came back.

    Hiding is the whole of what delete does here, so this is the undo, and it is one flag on a
    record that never left.
    """
    rec = registry.show(agent_id)
    if rec:
        log.info("brought back %s", agent_id)
    return rec


def adopt(session_id, cwd, name=None, found=None, engine="claude"):
    """Make an agent out of a conversation that already exists.

    The session id is the identity, so the record simply points at it and the first wake
    resumes the conversation. Claude's id goes in `session_id`; OpenCode's goes in
    `opencode_session`, because its `session_id` is a console-made uuid. Nothing is started
    here.
    """
    engine = (engine or "claude").strip()
    who = default_name(found, name)
    if engine == "opencode":
        rec = registry.create(
            name=who,
            cwd=str(cwd),
            engine="opencode",
            opencode_session=session_id,
            self_registered=True,
        )
    else:
        rec = registry.create(
            name=who,
            cwd=str(cwd),
            session_id=session_id,
            self_registered=True,
        )
    log.info("adopted %s session %s as %s in %s", engine, session_id, rec["id"], cwd)
    return rec
