"""Outside agents: agents on the owner's other machines (a laptop, a server, a cloud session) that
hand work to Ergo's agents and take it back.

A handoff protocol, not a remote control. An outside agent connects to
one MCP server the daemon serves over the tailnet, at /mcp, with its own token, and gets:

    list_agents     Ergo's agents: name, what each does, awake or asleep
    send_message    a message into an agent's chat, as if the owner typed it, signed with its name
    send_file       a file into an agent's inbox
    list_files      the files in an agent's folder, or one folder inside it
    get_file        one file from an agent's folder: notes, code, memory, outbox
    handoff         a task and its files in one go, with an id so the answer finds its way back
    replies         what Ergo's agents sent back; waits up to a minute for something to arrive

Ergo's own agents answer with their reply_outside tool, which lands here in the outside agent's
mailbox, var/outside/<id>.jsonl.

An outside agent sees every agent that is not hidden, and those agents' folders. Records still
carry a side, and it is always the one side there is (vault.SIDE).

A session is the MCP session: the daemon gives one out at initialize, in the Mcp-Session-Id
header, and Claude Code and Codex send it back on every request. Each session's client name
(Claude or Codex, from its clientInfo) is kept in var/outside-sessions.json.

The token is shown once, when the agent is added, and only its hash is kept. Being on the tailnet is
what reaches the daemon at all; the token is what says which outside agent is calling.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import broker, clock, config, files, logs, registry, vault

log = logs.get(__name__)

SESSION_DAYS = 7
VERSION = "2025-06-18"
WAIT_MAX = 55.0
FILE_MAX = 10 * 1024 * 1024
LIST_MAX = 500
TEXT_MAX = 20000
# Folders no one hands off: big, generated, or a browser profile full of cookies.
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".chrome-profile", ".cache",
        "dist", "build", ".next"}
ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")

_lock = threading.RLock()
_arrived = threading.Condition(_lock)


# -- who is let in ------------------------------------------------------------------

def _file():
    return config.VAR / "outside.json"


def _box(oid):
    folder = config.VAR / "outside"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{oid}.jsonl"


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _saved():
    try:
        got = json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _save(held):
    path = _file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(held, indent=1))
    tmp.replace(path)


def listing():
    """What Config shows: every outside agent, never a token."""
    with _lock:
        return [{"id": oid, "name": r["name"], "side": r["side"], "created": r.get("created"),
                 "seen": r.get("seen")} for oid, r in sorted(_saved().items())]


def add(name, side=None):
    """A new outside agent. Returns its record and its token, which is never shown again.

    `side` is accepted for older callers and ignored: there is one side."""
    name = " ".join(str(name or "").split())[:40]
    if not name:
        raise ValueError("give the outside agent a name")
    side = vault.SIDE
    oid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "outside"
    with _lock:
        held = _saved()
        base, n = oid, 2
        while oid in held or registry.get(oid):
            oid, n = f"{base}-{n}", n + 1
        token = "ergo_" + secrets.token_urlsafe(32)
        held[oid] = {"name": name, "side": side, "token": _hash(token), "created": clock.now(),
                     "seen": None}
        _save(held)
    log.info("outside agent %s added", oid)
    return {"id": oid, "name": name, "side": side, "token": token}


def remove(oid):
    with _lock:
        held = _saved()
        if held.pop(oid, None) is None:
            raise KeyError(oid)
        _save(held)
    log.info("outside agent %s removed", oid)
    return {"ok": True}


def who(token):
    """The outside agent a bearer token belongs to, or None. Notes when it was last seen."""
    if not token:
        return None
    digest = _hash(token)
    with _lock:
        held = _saved()
        for oid, r in held.items():
            if secrets.compare_digest(r.get("token") or "", digest):
                now = clock.now()
                if not r.get("seen") or now - r["seen"] > 60:
                    r["seen"] = now
                    _save(held)
                return {"id": oid, "name": r["name"], "side": vault.SIDE}
    return None


# -- sessions -----------------------------------------------------------------------

def _sessions_file():
    return config.VAR / "outside-sessions.json"


def _sessions():
    try:
        got = json.loads(_sessions_file().read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _save_sessions(held):
    cutoff = clock.now() - SESSION_DAYS * 86400
    held = {k: v for k, v in held.items() if (v.get("at") or 0) >= cutoff}
    path = _sessions_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(held, indent=1))
    tmp.replace(path)


def client_name(info):
    """Claude or Codex, from the clientInfo a client sends at initialize."""
    raw = str((info or {}).get("name") or "").strip()
    low = raw.lower()
    if "claude" in low:
        return "Claude"
    if "codex" in low:
        return "Codex"
    return raw[:30] or None


def attach(me, sid):
    """The outside agent as this session sees it, with its client filled in.

    A session id the daemon does not know is taken as a new session, rather than refused.
    """
    me = dict(me, side=vault.SIDE, fixed=True, session=sid or None, client=None)
    if sid:
        with _lock:
            row = _sessions().get(sid)
        if row and row.get("oid") == me["id"]:
            me["client"] = row.get("client")
    return me


def _remember(me, **change):
    if not me.get("session"):
        return
    with _lock:
        held = _sessions()
        row = held.get(me["session"]) or {"oid": me["id"]}
        row.update(change, at=clock.now())
        held[me["session"]] = row
        _save_sessions(held)


# -- the mailbox --------------------------------------------------------------------

def deliver(to, sender, text, file=None):
    """A Ergo agent's answer to an outside agent. `file` is a path in the sender's folder."""
    with _lock:
        if to not in _saved():
            raise KeyError(f"no outside agent called {to}")
    rec = registry.get(sender)
    if not rec:
        raise KeyError(f"no agent called {sender}")
    row = {"at": clock.now(), "from": sender, "from_name": rec.get("name") or sender,
           "side": vault.side_of(rec), "text": str(text or "")[:TEXT_MAX]}
    if file:
        path = Path(file).expanduser()
        if not path.is_absolute():
            path = Path(rec["cwd"]) / path
        path = path.resolve()
        if not path.is_file() or not _inside(path, Path(rec["cwd"])):
            raise ValueError("the file must be in your own folder")
        if path.stat().st_size > FILE_MAX:
            raise ValueError("a file over 10 MB")
        row["file"] = {"name": path.name, "path": str(path.relative_to(Path(rec["cwd"]).resolve()))}
    with _arrived:
        with open(_box(to), "a") as fh:
            fh.write(json.dumps(row) + "\n")
        _arrived.notify_all()
    return {"ok": True, "to": to}


def _rows(oid):
    try:
        lines = _box(oid).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# -- the tools ----------------------------------------------------------------------

def _inside(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _agent(me, agent):
    rec = registry.get(agent or "")
    if not rec or rec.get("hidden"):
        raise ValueError(f"no agent called {agent}. list_agents shows who is here")
    return rec


def _place(rec, path):
    root = Path(rec["cwd"]).resolve()
    target = (root / (path or "")).resolve()
    if not _inside(target, root):
        raise ValueError("that path is outside the agent's folder")
    return root, target


def t_list_agents(me):
    out = []
    for rec in registry.all():
        if rec.get("hidden"):
            continue
        state = broker.state(rec["id"])
        out.append({"id": rec["id"], "name": rec.get("name") or rec["id"],
                    "note": rec.get("note") or "", "state": state})
    return {"agents": out, "you": me["id"],
            "note": "Messages you send are signed with your name. Answers arrive in replies()."}


def _sign(me, text):
    who = ", ".join(x for x in (me["name"], me.get("client")) if x)
    return (f"[From {who}, an outside agent on the owner's tailnet. Answer it with "
            f"reply_outside, to=\"{me['id']}\".]\n\n{text}")


def t_send_message(me, agent, text, wake=True):
    rec = _agent(me, agent)
    if not str(text or "").strip():
        raise ValueError("nothing to send")
    outcome = broker.send(rec["id"], _sign(me, str(text)[:TEXT_MAX]), wake=bool(wake))
    return {"ok": bool(outcome), "outcome": outcome or "failed"}


def _put(rec, name, content=None, content_base64=None):
    if content_base64 is not None:
        data = base64.b64decode(content_base64)
    elif content is not None:
        data = str(content).encode()
    else:
        raise ValueError("send content (text) or content_base64")
    if len(data) > FILE_MAX:
        raise ValueError("a file over 10 MB")
    import io
    path, size = files.receive(rec, name, io.BytesIO(data), len(data))
    return path, size


def t_send_file(me, agent, name, content=None, content_base64=None, note=None):
    rec = _agent(me, agent)
    path, size = _put(rec, name, content, content_base64)
    broker.send(rec["id"], _sign(me, files.arrival_message(
        str(path), size, note, by=f"{me['name']}, an outside agent")), wake=False)
    return {"ok": True, "path": str(Path(path).relative_to(Path(rec["cwd"]).resolve()))
            if _inside(Path(path), Path(rec["cwd"])) else Path(path).name, "bytes": size}


def t_list_files(me, agent, path=""):
    rec = _agent(me, agent)
    root, target = _place(rec, path)
    if not target.is_dir():
        raise ValueError("not a folder")
    rows = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith(".chrome"))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            try:
                st = p.stat()
            except OSError:
                continue
            rows.append({"path": str(p.relative_to(root)), "bytes": st.st_size,
                         "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
            if len(rows) >= LIST_MAX:
                return {"files": rows, "more": True,
                        "note": f"First {LIST_MAX}. Ask for a folder inside to see the rest."}
    return {"files": rows, "more": False}


def t_get_file(me, agent, path):
    rec = _agent(me, agent)
    _, target = _place(rec, path)
    if not target.is_file():
        raise ValueError("no such file")
    size = target.stat().st_size
    if size > FILE_MAX:
        raise ValueError("a file over 10 MB")
    data = target.read_bytes()
    try:
        return {"path": path, "bytes": size, "text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": path, "bytes": size, "content_base64": base64.b64encode(data).decode()}


def t_handoff(me, agent, task, files_=None, wake=True):
    rec = _agent(me, agent)
    hid = "h" + secrets.token_hex(4)
    sent = []
    for f in files_ or []:
        if not isinstance(f, dict) or not f.get("name"):
            raise ValueError("each file is {name, content} or {name, content_base64}")
        path, _ = _put(rec, f["name"], f.get("content"), f.get("content_base64"))
        sent.append(str(path))
    body = f"HANDOFF {hid}\n\n{task}"
    if sent:
        body += "\n\nFiles, in your inbox:\n" + "\n".join(f"- {p}" for p in sent)
    body += (f"\n\nWhen it is done, or if you need something, answer with reply_outside, "
             f"to=\"{me['id']}\", and start the text with {hid}.")
    outcome = broker.send(rec["id"], _sign(me, body), wake=bool(wake))
    return {"ok": bool(outcome), "handoff": hid, "files": len(sent),
            "note": "The answer arrives in replies(), starting with the handoff id."}


def t_replies(me, after=None, wait_seconds=60):
    """Everything since `after` (the `next` value of the last call), waiting for the first."""
    oid = me["id"]
    try:
        after = int(after or 0)
    except (TypeError, ValueError):
        after = 0
    wait = max(0.0, min(float(wait_seconds or 0), WAIT_MAX))
    deadline = time.time() + wait
    with _arrived:
        while True:
            rows = _rows(oid)
            if len(rows) > after or time.time() >= deadline:
                break
            _arrived.wait(timeout=max(0.1, deadline - time.time()))
    new = rows[after:]
    for r in new:
        if r.get("file"):
            r["file"]["get_file"] = {"agent": r["from"], "path": r["file"]["path"]}
    return {"replies": new, "next": len(rows)}


TOOLS = {
    "list_agents": (t_list_agents,
        "Ergo's agents: id, name, what each does. Ergo is the owner's house of AI agents on "
        "their own machine. Hand work to these agents when it needs that machine: the owner's "
        "files, logged-in browser and accounts.",
        {"type": "object", "properties": {}}),
    "send_message": (t_send_message,
        "Send a message into one Ergo agent's chat, signed with your name. It reads it like a "
        "message from the owner. Its answer comes back through replies().",
        {"type": "object", "required": ["agent", "text"], "properties": {
            "agent": {"type": "string", "description": "the agent's id from list_agents"},
            "text": {"type": "string"},
            "wake": {"type": "boolean", "description": "wake it if asleep. Default true"}}}),
    "send_file": (t_send_file,
        "Put a file in one Ergo agent's inbox. Text goes in content; anything else base64 in "
        "content_base64. Up to 10 MB. The agent is told it arrived, not told to act on it.",
        {"type": "object", "required": ["agent", "name"], "properties": {
            "agent": {"type": "string"}, "name": {"type": "string"},
            "content": {"type": "string"}, "content_base64": {"type": "string"},
            "note": {"type": "string", "description": "one line on what it is"}}}),
    "list_files": (t_list_files,
        "List the files in a Ergo agent's folder, or in one folder inside it: notes, code, "
        "memory, its inbox and its outbox (.platform/outbox/<agent>/).",
        {"type": "object", "required": ["agent"], "properties": {
            "agent": {"type": "string"},
            "path": {"type": "string", "description": "a folder inside the agent's folder"}}}),
    "get_file": (t_get_file,
        "Read one file from a Ergo agent's folder. Text comes back as text, anything else as "
        "base64. Up to 10 MB.",
        {"type": "object", "required": ["agent", "path"], "properties": {
            "agent": {"type": "string"},
            "path": {"type": "string", "description": "as list_files shows it"}}}),
    "handoff": (t_handoff,
        "Hand a task to a Ergo agent: the instructions and any files in one go. Returns a "
        "handoff id; the agent's answer comes back in replies() starting with that id.",
        {"type": "object", "required": ["agent", "task"], "properties": {
            "agent": {"type": "string"},
            "task": {"type": "string", "description": "what to do, written so a cold reader can"},
            "files": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "content": {"type": "string"},
                "content_base64": {"type": "string"}}}},
            "wake": {"type": "boolean"}}}),
    "replies": (t_replies,
        "What Ergo's agents sent you. Pass `after` as the `next` from your last call. If nothing "
        "has arrived it waits up to wait_seconds (max 55) for the first answer. A reply with a "
        "file names it; read it with get_file.",
        {"type": "object", "properties": {
            "after": {"type": "integer"},
            "wait_seconds": {"type": "number"}}}),
}


def call_tool(me, name, args):
    me = me if "fixed" in me else attach(me, None)
    row = TOOLS.get(name)
    if row is None:
        raise LookupError(f"no tool called {name}")
    args = dict(args or {})
    if name == "handoff" and "files" in args:
        args["files_"] = args.pop("files")
    log.info("outside %s: %s", me["id"], name)
    return row[0](me, **args)


def rpc(me, msg):
    """One JSON-RPC message from an outside agent. Returns the answer, or None for a notification."""
    method, msg_id = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if msg_id is None:
        return None
    if "fixed" not in me:
        me = attach(me, None)
    if method == "initialize":
        asked = params.get("protocolVersion")
        if not me.get("session"):
            # A new session. The server hands this id back in the Mcp-Session-Id header.
            me["session"] = "s" + secrets.token_hex(12)
            me["new_session"] = True
        me["client"] = client_name(params.get("clientInfo"))
        _remember(me, client=me["client"])
        text = ("Ergo is the owner's house of AI agents on their own machine. Use it to hand work "
                "to those agents and take their answers back.")
        result = {"protocolVersion": asked if isinstance(asked, str) and asked else VERSION,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "ergo", "version": "1"},
                  "instructions": text}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s}
                            for n, (_, d, s) in TOOLS.items()]}
    elif method == "tools/call":
        try:
            value = call_tool(me, params.get("name"), params.get("arguments"))
            result = {"content": [{"type": "text", "text": json.dumps(value, indent=1, default=str)}]}
        except LookupError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": str(exc)}}
        except TypeError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": str(exc)}}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"no method called {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}
