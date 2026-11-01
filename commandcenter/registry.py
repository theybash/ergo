"""Which agents exist.

An agent appears on the console because it registered, never because a process matched a
pattern. That is the whole reason this file exists as its own thing: the list of agents is a
decision the operator made, not an observation of the process table.

The record holds identity and intent. It holds no liveness, because liveness belongs to
whoever owns the terminal. Ask the broker whether an agent is awake; ask this file whether it
exists at all.
"""

import json
import logging
import os
import re
import threading
import time
import uuid

from . import config

log = logging.getLogger("cc.registry")

_lock = threading.RLock()
_agents = {}
_loaded = False

VALID_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Which engines an agent may run: Claude Code, the Codex CLI, or OpenCode.
ENGINES = ("claude", "codex", "opencode")


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:32] or "agent"


def _blank(**kw):
    now = time.time()
    rec = {
        "id": None,
        "name": None,
        "session_id": None,
        # OpenCode's own session id. Not the same thing as session_id above: Claude Code's id is
        # a uuid chosen by the console, OpenCode's is one OpenCode made and the first turn reports.
        "opencode_session": None,
        "cwd": str(os.path.expanduser("~")),
        "model": None,
        "role": None,            # "master", "pa", or nothing
        "unix_user": None,       # set only for an agent quarantined as another user
        "permission_mode": config.DEFAULT_PERMISSION_MODE,
        "created": now,
        "last_activity": now,
        # Unused on purpose. It is here so heartbeats from agents on other machines can be
        # added later without changing the shape of the record.
        "last_seen": None,
        "hidden": False,
        "schedule": None,        # a cron expression, or nothing
        # Which of work, demo and personal this agent appears in. Nothing means everywhere, which is
        # what every agent made before this field existed wants, and what a new agent wants too.
        "views": None,
        "self_registered": False,
        "note": None,            # one line the agent may write about itself
        # What runs the agent: "claude", Claude Code, "codex", the Codex CLI, or "opencode". Set
        # when the agent is made. It changes only while the agent sleeps, because each engine
        # keeps a different conversation.
        "engine": "claude",
        # The Codex conversation id. Codex names it on the first turn, so it is empty until then.
        "codex_thread": None,
        # A delegate's parent: the agent that made it and the only one it reports to. Nothing for
        # an ordinary agent. A delegate never reaches the owner itself.
        "parent": None,
        # An archived delegate keeps its folder and its chat but does not count toward its
        # parent's nine. Its parent revives it when it needs it again.
        "archived": False,
    }
    rec.update(kw)
    return rec


def _load():
    global _loaded
    if _loaded:
        return
    config.ensure_dirs()
    try:
        raw = json.loads(config.AGENTS_FILE.read_text())
        for rec in raw.get("agents", []):
            merged = _blank()
            merged.update(rec)
            _agents[merged["id"]] = merged
    except FileNotFoundError:
        pass
    except Exception as exc:
        # A corrupt file must not stop the console from starting, but losing the list
        # silently would be worse than either, so keep the bad copy and say so.
        bad = config.AGENTS_FILE.with_suffix(".broken")
        try:
            config.AGENTS_FILE.rename(bad)
            log.error("%s unreadable (%s); moved to %s", config.AGENTS_FILE, exc, bad)
        except Exception:
            pass
    _loaded = True


def _save():
    """Write through a temporary file so a crash mid-write cannot leave half a list."""
    config.ensure_dirs()
    tmp = config.AGENTS_FILE.with_suffix(".tmp")
    payload = {"version": 1, "agents": list(_agents.values())}
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(config.AGENTS_FILE)


def all(include_hidden=False):
    with _lock:
        _load()
        out = [dict(a) for a in _agents.values() if include_hidden or not a["hidden"]]
    out.sort(key=lambda a: (a["role"] != "master", -a["last_activity"]))
    return out


def get(agent_id):
    with _lock:
        _load()
        rec = _agents.get(agent_id)
        return dict(rec) if rec else None


def by_session(session_id):
    with _lock:
        _load()
        for rec in _agents.values():
            if rec["session_id"] == session_id:
                return dict(rec)
    return None


def by_opencode_session(session_id):
    """The agent that already owns this OpenCode conversation, if any.

    OpenCode ids and Claude ids are different namespaces, and an OpenCode record's
    `session_id` is a console-made uuid rather than the engine's id. So this is a separate
    lookup, not a second spelling of `by_session`.
    """
    with _lock:
        _load()
        for rec in _agents.values():
            if rec.get("opencode_session") == session_id:
                return dict(rec)
    return None


def create(name, cwd, model=None, role=None, permission_mode=None, unix_user=None,
           session_id=None, self_registered=False, agent_id=None, engine=None,
           opencode_session=None, parent=None):
    """Add an agent and hand back the record.

    The session id is generated here rather than discovered later. Claude Code accepts
    --session-id, so the console decides what the conversation will be called and then starts
    a process to hold it. That ordering is what lets the record survive the process.
    """
    engine = (engine or "claude").strip()
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}")
    with _lock:
        _load()
        base = agent_id or slug(name)
        if not VALID_ID.match(base):
            raise ValueError(f"bad agent id: {base!r}")
        wanted, n = base, 2
        while wanted in _agents:
            wanted, n = f"{base}-{n}", n + 1
        rec = _blank(
            id=wanted,
            name=name.strip() or wanted,
            session_id=session_id or str(uuid.uuid4()),
            cwd=str(cwd),
            model=model,
            role=role,
            unix_user=unix_user,
            # Never plan at birth: a plan agent has no shell and no edits,
            # and Loki spent a day stuck that way. Full access unless a mode was chosen.
            permission_mode=(permission_mode if permission_mode not in (None, "", "plan")
                             else "bypassPermissions"),
            self_registered=self_registered,
            engine=engine,
            opencode_session=opencode_session,
            parent=parent,
        )
        if role == "master" and any(a["role"] == "master" for a in _agents.values()):
            raise ValueError("there is already a master")
        _agents[wanted] = rec
        _save()
        return dict(rec)


def update(agent_id, **fields):
    allowed = {"name", "model", "schedule", "note", "hidden", "permission_mode",
               "last_activity", "last_seen", "session_id", "role", "views", "cwd", "autocompact",
               "codex_thread", "engine", "opencode_session", "archived"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot set {sorted(bad)}")
    if "engine" in fields:
        wanted = (fields["engine"] or "").strip()
        if wanted not in ENGINES:
            raise ValueError(f"engine must be one of {', '.join(ENGINES)}")
        fields["engine"] = wanted
    if "cwd" in fields:
        # The folder decides where Claude Code looks for the conversation, because the name of the
        # transcript directory is made from the folder path. A relative path here would point at
        # whatever folder the daemon happens to run in, and the agent would come back with no
        # memory. Refuse it rather than guess what the caller meant.
        #
        # Moving the files is not this function's job. It records where the agent now lives; the
        # folder and the transcript are moved by whoever asked for the move.
        wanted = str(fields["cwd"] or "").strip()
        if not os.path.isabs(wanted):
            raise ValueError("cwd must be a full path, one that starts with /")
        fields["cwd"] = os.path.normpath(wanted)
    if fields.get("role") == "master":
        # `create` has always refused a second master and `update` did not, so promoting two agents
        # would have made two. Which one Telegram then talked to would depend on the order the file
        # happened to be written in.
        with _lock:
            _load()
            other = next((a for a in _agents.values()
                          if a["role"] == "master" and a["id"] != agent_id), None)
        if other:
            raise ValueError(f"{other['id']} is already the master; demote it first")
    if "views" in fields:
        # This arrives from a browser, so it is checked rather than trusted. Storing an unknown name
        # would make an agent invisible in every view, which looks exactly like an agent that has
        # gone.
        wanted = fields["views"] or []
        if not isinstance(wanted, list) or any(v not in config.VIEW_NAMES for v in wanted):
            raise ValueError(f"views can only be {', '.join(config.VIEW_NAMES)}")
        # All three is the same thing as none, and none is the simpler record to read.
        picked = sorted(set(wanted))
        fields["views"] = None if len(picked) == len(config.VIEW_NAMES) else (picked or None)
    with _lock:
        _load()
        rec = _agents.get(agent_id)
        if not rec:
            return None
        rec.update(fields)
        _save()
        return dict(rec)


def touch(agent_id, ts=None):
    with _lock:
        _load()
        rec = _agents.get(agent_id)
        if rec:
            rec["last_activity"] = ts or time.time()
            _save()


def hide(agent_id):
    """Remove an agent from the console without touching its process or its conversation.

    Hiding and killing are separate actions because closing a card by accident must not
    destroy work. The conversation stays on disk and the agent can be shown again.
    """
    return update(agent_id, hidden=True)


def show(agent_id):
    return update(agent_id, hidden=False)


def forget(agent_id):
    """Drop the record entirely. The conversation on disk is untouched."""
    with _lock:
        _load()
        if _agents.pop(agent_id, None) is None:
            return False
        _save()
        return True


def delegates(parent_id):
    """The delegates an agent made, hidden and archived ones too, oldest first."""
    with _lock:
        _load()
        out = [dict(a) for a in _agents.values() if a.get("parent") == parent_id]
    out.sort(key=lambda a: a["created"])
    return out


def reset():
    """Forget everything held in memory and read the files again on next use. For tests."""
    global _loaded
    with _lock:
        _agents.clear()
        _loaded = False
