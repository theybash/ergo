"""The console's web server.

Three kinds of traffic, each with the transport that suits it:

    ordinary requests   JSON over HTTP, for making and changing things
    /api/events         server-sent events: what changed, as the change itself, when it changes
    /ws/agent/<id>      a WebSocket per terminal, because bytes travel both ways forever

Standard library only, threaded, one port. One port matters more than it sounds: the console is
published with a single `tailscale serve` target, and every extra port would be another thing to
publish and another thing to forget.
"""

import base64
import gzip
import json
import mimetypes
import os
import queue
import re
import subprocess
import threading
import time
import traceback
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import (reads, broker, calm, quiet, config, cords, cron, data, fetch, files, health, inbox,
               judge, live, mailbox, memory, network, recruits, registry, servers, status, tasks,
               transcript, untrusted, usage, voice)
from .ws import WebSocket, is_upgrade

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}

# What each kind of file is called on the wire. mimetypes knows most of these, but not the same
# way on every machine, and it does not know .webmanifest at all. A browser refuses a module
# script that arrives as text/plain, so the React build would simply not start. Writing the table
# out means the answer does not depend on the /etc/mime.types of whatever box this runs on.
MEDIA = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}

# Worth compressing, and nothing else. The React bundle is 263 KB of text and 80 KB gzipped, and
# the phone pulls it over Tailscale, so the saving is seconds rather than bytes. Fonts and images
# are already compressed: gzipping a woff2 spends processor time to make the file slightly bigger.
COMPRESSIBLE = (".html", ".js", ".mjs", ".css", ".json", ".map", ".webmanifest", ".svg", ".txt")


# -- events ----------------------------------------------------------------------

class EventHub:
    """Fans one change out to every open console and phone.

    Each client names the topics it wants, and gets nothing else:

        state    what changed in the fleet, as the change itself; the whole state first
        phone    the few facts the phone's notices need, whole, each time one of them changes
        inbox    an inbox item added or told, with its own summary
        tasks    a task card that changed, whole, for the cards on Insights and the tab's count
        posts    a feed post that is new or newly marked, whole
        health   the machine snapshot every 2 s, for the one screen that draws it
        codex    the Codex sign-in status, when a sign-in ends
        legacy   the old "agents", "cords", "jobs" pings, which make a client fetch everything

    A client that names none is an old console and gets legacy, inbox and health, as before.
    """

    LEGACY = frozenset(("legacy", "inbox", "health"))

    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()

    def add(self, topics=None):
        q = queue.Queue(maxsize=256)
        q.topics = frozenset(topics) if topics else self.LEGACY
        q.dropped = False
        with self.lock:
            self.clients.append(q)
        return q

    def drop(self, q):
        q.dropped = True
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def wants(self, topic):
        with self.lock:
            return any(topic in q.topics for q in self.clients)

    def publish(self, event, payload=None, topic=None):
        """Send to every client of `topic`. The old pings go to legacy clients, and each one also
        tells the feed to look now rather than at its next tick."""
        if topic is None:
            topic = "inbox" if event == "inbox" else "health" if event == "health" else "legacy"
            if topic != "health":
                feed.poke()
        frame = (event, payload or {})
        with self.lock:
            targets = [q for q in self.clients if topic in q.topics]
        for q in targets:
            try:
                q.put_nowait(frame)
            except queue.Full:
                # A console that stopped reading is gone. Dropping it is better than growing a
                # queue for a browser that closed three hours ago.
                self.drop(q)


hub = EventHub()


def _health_loop():
    while True:
        try:
            if hub.wants("health"):
                hub.publish("health", health.snapshot(), topic="health")
        except Exception:
            pass
        time.sleep(config.HEALTH_INTERVAL)


# -- the feed ----------------------------------------------------------------------

def _keyed_diff(old, new):
    """What changed in a list of records with ids: {id: None} for a record gone, {id: {field:
    value}} for the fields that changed, the whole record for a new one. None when nothing did."""
    was = {r.get("id"): r for r in old}
    out = {}
    for rec in new:
        rid = rec.get("id")
        before = was.pop(rid, None)
        if before is None:
            out[rid] = rec
            continue
        fields = {k: v for k, v in rec.items() if before.get(k, _MISSING) != v}
        fields.update({k: None for k in before if k not in rec})
        if fields:
            out[rid] = fields
    out.update({rid: None for rid in was})
    return out or None


_MISSING = object()
FEED_KEYED = ("agents", "jobs")
FEED_WHOLE = ("hidden", "cords", "limits", "inbox")


def state_diff(old, new):
    """The change from one fleet state to the next, or None. Lists of records go by id and by
    field, and their order only when it changed. Everything else goes whole when it changed."""
    out = {}
    for key in FEED_KEYED:
        a, b = old.get(key) or [], new.get(key) or []
        changed = _keyed_diff(a, b)
        if changed:
            out[key] = changed
        order = [r.get("id") for r in b]
        if order != [r.get("id") for r in a]:
            out[key + "_order"] = order
    for key in FEED_WHOLE:
        if old.get(key) != new.get(key):
            out[key] = new.get(key)
    return out or None


def phone_digest(state):
    """Only what the phone's notices compare between one moment and the next: who is working and
    the line each agent last said, each job's last run, the open asks, the front desk's briefs, and the
    phone's own commands. A few KB, sent whole when any of it changes."""
    from . import phone
    # The voice tool's own list, without the tool call: that writes a log line each time.
    asks = voice._pending_asks()
    waiting = inbox.listing().get("waiting") or []
    return {
        "agents": {a["id"]: [bool(a.get("working")), " ".join((a.get("last_message") or "").split())[:120]]
                   # Not delegates: they work for their parent, so a
                   # delegate finishing a turn rings nothing. Its parent's turn does.
                   for a in state.get("agents") or [] if not a.get("parent")},
        "jobs": {j["id"]: [j.get("last_fire") or 0, j.get("last_outcome") or "", j.get("agent") or ""]
                 for j in state.get("jobs") or []},
        "asks": asks if isinstance(asks, list) else [],
        "briefs": [{"id": i.get("id"), "agent": i.get("agent"), "urgency": i.get("urgency"),
                    "text": i.get("text"), "kind": i.get("kind") or "", "added": i.get("added") or 0}
                   for i in waiting if not (i.get("ask") or i.get("job"))],
        "commands": phone.commands()["commands"],
        # An urgent item: the phone rings while this is here, under the name the owner gave the agent.
        "ring": _ring_named(inbox.ringing()),
    }


def _ring_named(ring):
    """The ring with the agent's own name in place of its id: the call screen shows "Juno", not
    "Portfolio manager". The id stays in the inbox; only the phone's copy changes."""
    if not ring:
        return ring
    rec = registry.get(ring.get("agent") or "")
    # The id too, for Prompt on the call screen, which opens that agent's chat.
    ring = dict(ring, agent_id=ring.get("agent") or "")
    return dict(ring, agent=rec["name"]) if rec and rec.get("name") else ring


class Feed:
    """Looks at the fleet on this laptop, where looking is free, and sends only what changed.

    The daemon builds its own state in a few milliseconds, so the looking happens here, every
    1.5 s and at once after any change the daemon knows of. Nothing crosses the network unless
    something differs from the last look. Some changes have no event of their own, such as an
    agent starting or stopping work, which is why there is a tick at all. With nobody listening
    it does nothing."""

    TICK = 1.5

    def __init__(self):
        self.woken = threading.Event()
        self.lock = threading.Lock()
        self.state = None
        self.digest = None
        self.version = 0

    def poke(self):
        self.woken.set()

    def current(self):
        """The whole state and the phone digest as the last look saw them, looked at now."""
        self.look()
        with self.lock:
            return self.version, self.state, self.digest

    def look(self):
        wants_state, wants_phone = hub.wants("state"), hub.wants("phone")
        if not (wants_state or wants_phone):
            with self.lock:
                self.state = self.digest = None
            return
        new = full_state(with_health=False)
        new.pop("server_time", None)
        digest = phone_digest(new) if wants_phone else None
        with self.lock:
            old, self.state = self.state, new
            diff = state_diff(old, new) if old is not None else None
            if diff:
                self.version += 1
                diff["v"] = self.version
            said = digest if digest is not None and digest != self.digest else None
            if digest is not None:
                self.digest = digest
        if diff:
            hub.publish("state", diff, topic="state")
        if said is not None:
            hub.publish("phone", said, topic="phone")
        if old is not None:
            _stopped_working(old, new)

    def run(self):
        while True:
            self.woken.wait(self.TICK)
            self.woken.clear()
            try:
                self.look()
            except Exception:
                traceback.print_exc()


feed = Feed()


def _stopped_working(old, new):
    """Each agent whose spinner stopped between two looks may have a task card or an urgent held
    message waiting on it. The look is already made for the console, so asking here costs
    nothing more."""
    was = {a.get("id"): a.get("working") for a in old.get("agents") or []}
    for agent in new.get("agents") or []:
        if was.get(agent.get("id")) and not agent.get("working"):
            try:
                tasks.free(agent["id"])
            except Exception:
                traceback.print_exc()
            # And a message sent with wake while it worked goes in now, not with his next one.
            broker.turn_ended(agent["id"])


# -- identity --------------------------------------------------------------------

_whois = {}


def whois(addr):
    """Which tailnet member a connection belongs to.

    Membership of the tailnet is the authentication, so this is not a permission check. It is
    the hook for the day one card should be visible to someone else, and it is useful now for
    telling the phone from the laptop.
    """
    ip = addr.replace("::ffff:", "")
    if ip in _whois:
        return _whois[ip]
    who = None
    try:
        out = subprocess.run(["tailscale", "whois", "--json", ip],
                             capture_output=True, text=True, timeout=3).stdout
        data = json.loads(out)
        who = {
            "user": (data.get("UserProfile") or {}).get("LoginName"),
            "device": (data.get("Node") or {}).get("Name", "").split(".")[0],
        }
    except Exception:
        who = {"user": None, "device": None}
    _whois[ip] = who
    return who


# -- state ------------------------------------------------------------------------

def agent_view(rec):
    """One agent as the console needs it: the record, plus what is true right now."""
    term = broker.get(rec["id"])
    facts = transcript.summary(rec)
    from . import vault
    return {
        **rec,
        # The one side there is. Kept in the record so older consoles still read it.
        "side": vault.side_of(rec),
        "state": broker.state(rec["id"]),
        # Which engine is running: chat, terminal, or off. The broker holds it in memory, so a
        # daemon restart puts everybody back to off, which is the resting state anyway.
        "mode": broker.mode(rec["id"]),
        # Asked of Claude Code, which knows, rather than guessed from recent output. An agent
        # thinking quietly for thirty seconds used to read as idle. The fallback is whichever
        # engine is running, because in chat mode there is no terminal to fall back to.
        "working": live.working(rec, term or broker.chat(rec["id"])),
        "pid": term.pid if term and term.alive() else None,
        "watchers": term.snapshot()["watchers"] if term and term.alive() else 0,
        # The terminal's own size, which is the smallest attached screen. Worth showing because a
        # terminal drawing to the wrong width is the difference between a readable console and a
        # column of wrapped fragments, and guessing at it wastes an afternoon.
        "size": [term.rows, term.cols] if term and term.alive() else None,
        "turns": facts["turns"],
        # The newest of its last reply and its last job result shown in the chat. A 13:30 check
        # card under an older reply is the news.
        **newest_said(rec["id"], facts["last_message"], facts["last_message_at"]),
        # When he last opened this agent's chat. A last message newer than this is unread.
        "read_at": reads.read_at(rec["id"]),
        "tokens": facts["tokens"],
        "context_tokens": facts["context_tokens"],
        "cost_usd": facts["cost_usd"],
        # Every screen the agent has written, not just one. The console draws a tab per page, so
        # the navigation stays the platform's and each page stays the agent's.
        "pages": config.pages(rec["id"]),
        # Which page the agent wants opened. The console honours it rather than picking for itself.
        "default_page": config.default_page(rec["id"]),
        # Files the agent has offered for download, and files sent to it. Listed rather than
        # counted, because the name is the useful part on a card that has room for one line.
        "files": files.listing(rec, "outbox"),
        "inbox_count": len(files.listing(rec, "inbox")),
        "resumable": facts["exists"],
        # Messages typed while it was asleep, still waiting. Shown on the card so a note
        # left last night is visibly pending rather than apparently ignored.
        "waiting": mailbox.waiting(rec["id"]),
        # The agent's own server, if it declared one. Shown even when down, with the reason,
        # because a blank screen and a card saying nothing sends you to the wrong file.
        "server": servers.state(rec["id"]),
        # What its screens can ask for by name. Files only: a server's routes cannot be listed,
        # so `server.up` above is what says whether there is a live one to ask.
        "data": data.listing(rec),
    }


def full_state(with_health=True):
    return {
        "agents": [agent_view(r) for r in registry.all()],
        "hidden": [r["id"] for r in registry.all(include_hidden=True) if r["hidden"]],
        "cords": cords.all(),
        "jobs": cron.all(),
        **({"health": health.snapshot()} if with_health else {}),
        "limits": health.LIMITS,
        # What the front desk has not told yet: {untold, urgent, at}. The orb glows on it, and the phone
        # can read it from the poll it already makes.
        "inbox": inbox.summary(),
        # Calm mode, for anxious days: every agent and the phone can read it here.
        "calm": calm.on(),
        # Quiet time, for a nap: when it ends, or null.
        "quiet": quiet.until(),
        "server_time": time.time(),
    }


# -- handler ----------------------------------------------------------------------

HANGUP_LINE = re.compile(r"^\s*HANGUP\s*:\s*", re.IGNORECASE | re.MULTILINE)
EMPHASIS = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def newest_said(agent_id, text, at):
    """The card's line and its time: the last reply, or a job result shown in the chat after it.
    The reply's time is ISO with a Z; a shown result's is seconds."""
    last = cron.shown(agent_id, 1)
    if last:
        # When it reached the chat, not when its job started; older rows have only the start.
        shown_at = last[0].get("shown_at") or last[0].get("at") or 0
        try:
            said = datetime.fromisoformat((at or "").replace("Z", "+00:00")).timestamp() if at else 0
        except ValueError:
            said = 0
        if shown_at > said:
            text = last[0].get("text") or ""
            at = datetime.fromtimestamp(shown_at, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"last_message": for_display(text)[:2000], "last_message_at": at}


def for_display(text):
    """Tidy an agent's last message for a card, without touching what is stored.

    Two things leak otherwise. The cord protocol's HANGUP marker is machinery and belongs in the
    cord, not on a card. And agents write markdown, so asterisks arrive as asterisks in a place
    that renders plain text.

    Only the display is changed. The stored message stays exact, because the cord logic reads it
    to find the marker and a summary must be what the agent actually wrote.
    """
    return EMPHASIS.sub(r"\1", HANGUP_LINE.sub("", text or "")).strip()


AGENT_PATH = re.compile(r"^/api/agents/([a-z0-9-]+)(?:/(\w+))?$")
WS_PATH = re.compile(r"^/ws/agent/([a-z0-9-]+)$")
BROWSER_WS = re.compile(r"^/ws/browser/([a-z0-9-]+)$")
BROWSER_PATH = re.compile(r"^/api/browser/([a-z0-9-]+)/(\w+)$")
CORD_PATH = re.compile(r"^/api/cords/([a-f0-9]+)/(\w+)$")
JOB_PATH = re.compile(r"^/api/jobs/([a-f0-9]+)(?:/(\w+))?$")
# A data name is matched loosely here and resolved against a listing in data.py, the same rule as
# a filename: this pattern routes, it does not decide.
DATA_PATH = re.compile(r"^/api/data/([a-z0-9-]+)/([A-Za-z0-9._-]+)$")
VIEW_PATH = re.compile(r"^/view/([a-z0-9-]+)/([a-z0-9-]*)/?$")
# The filename is matched loosely here and resolved against a listing later, so this pattern is a
# router rather than a security check. The check is that the name must already be a file.
FILE_PATH = re.compile(r"^/file/([a-z0-9-]+)/(.+)$")

# An agent's greeting, "Hi the owner, it's the assistant.", in its own live voice. The file is found from
# the agent's record in agentcall.greeting, never from the address.
# A parent's delegates: the list, a new one, and archive, revive or a message for one of them.
DELEGATE_PATH = re.compile(
    r"^/api/agents/([a-z0-9-]+)/delegates(?:/([a-z0-9-]+)/(archive|revive|message))?$")
AGENT_GREETING = re.compile(r"^/api/agents/([a-z0-9-]+)/greeting$")

# One voice's preview for Config, by its name.
VOICE_PREVIEW = re.compile(r"^/api/voice/preview/([a-z]{2,20})$")

# One inbox item and what to do to it. The id is the inbox's own, ten hex characters.
INBOX_PATH = re.compile(r"^/api/inbox/([a-f0-9]{6,32})/(\w+)$")

# One task card and what to do to it. The id is the task's own, ten hex characters.
TASK_PATH = re.compile(r"^/api/tasks/([a-f0-9]{6,32})/(\w+)$")



class Handler(BaseHTTPRequestHandler):
    server_version = "commandcenter"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass    # the access log is noise; failures are printed where they happen

    # -- plumbing ---------------------------------------------------------------

    @property
    def peer(self):
        return self.client_address[0]

    def allowed(self):
        """The door. The tailnet is the authentication.

        The address comes from the socket, which the operating system knows, and never from a
        header, which the caller writes. A token is honoured too, for running the console on a
        laptop that is temporarily off the tailnet.
        """
        if config.on_tailnet(self.peer):
            return True
        if config.TOKEN:
            sent = self.headers.get("Authorization", "")
            return sent == f"Bearer {config.TOKEN}"
        return False

    def outside_mcp(self):
        """The MCP server for outside agents: JSON-RPC over HTTP POST, one answer per request.

        The bearer token says which outside agent this is; without one there is nothing here. A
        notification is answered 202 with no body, as the streamable HTTP transport asks."""
        from . import outside
        auth = self.headers.get("Authorization", "")
        me = outside.who(auth[7:] if auth.startswith("Bearer ") else "")
        if not me:
            return self.fail("an outside agent token is needed. Add one on Config", 401)
        me = outside.attach(me, self.headers.get("Mcp-Session-Id"))
        msg = self.body_json()
        batch = msg if isinstance(msg, list) else [msg]
        answers = [a for a in (outside.rpc(me, m) for m in batch if isinstance(m, dict)) if a]
        if not answers:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        # A new session's id goes back in this header, and the client sends it on every request
        # after. It is what an "either" token's side is remembered by.
        extra = {"Mcp-Session-Id": me["session"]} if me.get("new_session") else None
        return self.send_json(answers if isinstance(msg, list) else answers[0], headers=extra)

    def send_json(self, obj, code=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        # Compressed when it is worth it. The fleet state is 79 KB as text and about 23 KB
        # gzipped, and on the phone's mobile data every byte of it is paid for.
        if len(body) > 1024 and self.gzip_ok():
            body = gzip.compress(body, compresslevel=6, mtime=0)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_audio(self, data, mime, cache):
        """A short recording, whole. Small enough that a range request is not worth answering."""
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def gzip_ok(self):
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def from_console(self):
        """True for the console over the tailnet, false for an agent on this laptop.

        Tailscale's HTTPS proxy connects from loopback too, and it says so in these headers. An
        agent calling the daemon directly comes on loopback without them."""
        proxied = self.headers.get("X-Forwarded-For") or self.headers.get("Tailscale-User-Login")
        return bool(proxied) or not self.peer.startswith(("127.", "::1", "::ffff:127."))

    def send_text(self, text, code=200, ctype="text/plain; charset=utf-8", extra=None):
        body = text.encode() if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def fail(self, msg, code=400):
        try:
            self.send_json({"error": str(msg)}, code)
        except (BrokenPipeError, ConnectionResetError):
            # The client left before the answer. There is nobody to tell.
            pass

    # -- routes -----------------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        path, qs = unquote(url.path), parse_qs(url.query)

        # Fonts are answered before the door, because a browser does not attach credentials to
        # a font request and a console with no typeface looks broken rather than locked.
        if path.startswith("/fonts/") or path.startswith("/vendor/"):
            return self.static_file(path.lstrip("/"))

        if not self.allowed():
            return self.fail("not on the tailnet", 403)

        if path in STATIC:
            name, ctype = STATIC[path]
            return self.static_file(name, ctype)

        m = WS_PATH.match(path)
        if m:
            return self.terminal_socket(m.group(1), qs)

        m = BROWSER_WS.match(path)
        if m:
            return self.browser_socket(m.group(1))

        m = VIEW_PATH.match(path)
        if m:
            return self.agent_page(m.group(1), m.group(2) or "view")

        m = FILE_PATH.match(path)
        if m:
            # The outbox by default. ?box=inbox is a file the owner sent the agent, so the chat can show
            # the screenshot he attached rather than its path.
            box = "inbox" if (qs.get("box") or [""])[0] == "inbox" else "outbox"
            small = (qs.get("thumb") or [""])[0] == "1"
            return self.download(m.group(1), m.group(2), box, small)

        if path == "/api/assist/agents":
            from . import assist
            return self.send_json({"agents": assist.agents()})
        if path == "/api/assist/reply":
            # The bar's wait for an answer: held open until the agent has spoken, at most 45 s.
            from . import assist
            try:
                return self.send_json(assist.reply((qs.get("agent") or [""])[0],
                                                   (qs.get("turns") or ["0"])[0]))
            except ValueError as exc:
                return self.fail(exc, 400)
        if path == "/api/state":
            return self.send_json(full_state())
        if path == "/api/health":
            return self.send_json(health.snapshot())
        if path == "/api/codex/models":
            # The Codex picker's list, the one the signed-in account may use. See codex.models.
            from . import codex
            return self.send_json({"models": codex.models()})
        if path == "/api/codex/login":
            from . import codex
            return self.send_json(codex.login_status())
        if path == "/api/opencode/models":
            # The OpenCode picker's list, with the free ones marked. See opencode.models.
            from . import opencode
            return self.send_json({"models": opencode.models()})

        if path == "/api/integrations":
            # What the Integrations screen may say: only what the daemon can check here.
            from . import integrations
            return self.send_json(integrations.snapshot())
        if path == "/api/network":
            # Pairs, not numbers: which hop is hurting is the only question worth answering here.
            return self.send_json(network.snapshot())
        if path == "/api/whoami":
            return self.send_json({"addr": self.peer, **whois(self.peer)})
        if path == "/api/events":
            return self.events(qs)
        if path == "/api/cords":
            return self.send_json({"cords": cords.all()})
        if path == "/api/recruits":
            # Every agent the console hid, and every Claude Code conversation on this laptop.
            # Read from disk on each ask and cached nowhere: it is a screen opened rarely, and a
            # stale list here would offer a session that has already been adopted.
            return self.send_json({
                "hidden": [agent_view(r) for r in registry.all(include_hidden=True)
                           if r["hidden"]],
                "sessions": recruits.sessions(),
            })
        if path == "/api/memory":
            # The Memory screen, read only: each agent's memory list, and the front desk's days of calls.
            # memories.py says who each view may see.
            from . import memories
            view = (qs.get("view") or [""])[0]
            name = (qs.get("file") or [""])[0]
            if not name:
                return self.send_json(memories.listing(view))
            try:
                return self.send_json(memories.read((qs.get("agent") or [""])[0], name, view))
            except LookupError as exc:
                return self.fail(exc, 404)
        if path == "/api/jobs":
            return self.send_json({"jobs": cron.all()})
        if path == "/api/outside":
            # The outside agents for Config, never a token.
            from . import outside
            return self.send_json({"outside": outside.listing()})
        if path == "/mcp":
            # No server-sent stream here: every answer comes back on its own POST.
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        if path == "/api/usage":
            # The Usage section of Config. Read from files on this laptop; the per agent part is
            # kept for a minute, because it opens session files that can be very large.
            return self.send_json(usage.snapshot())
        if path == "/api/voice/log":
            try:
                count = max(1, min(int((qs.get("n") or [40])[0]), 400))
            except ValueError:
                count = 40
            return self.send_json({"calls": voice.calls(count), "budget": voice.budget()})

        if path == "/api/voice/greeting":
            got = voice.greeting()
            if not got:
                return self.fail("no greeting recorded yet", 404)
            data, mime = got
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return None

        m = VOICE_PREVIEW.match(path)
        if m:
            # A few seconds of one voice, for the play button on Config. Made once and kept.
            from . import agentcall
            try:
                return self.send_audio(agentcall.preview(m.group(1)), "audio/mpeg",
                                       "private, max-age=86400")
            except ValueError as exc:
                return self.fail(exc, 404)
            except PermissionError as exc:
                return self.fail(exc, 503)
            except RuntimeError as exc:
                return self.fail(exc, 502)

        m = AGENT_GREETING.match(path)
        if m:
            from . import agentcall
            found = agentcall.greeting(m.group(1))
            if not found:
                return self.fail("no greeting recorded yet", 404)
            return self.send_audio(found, "audio/wav", "no-cache")

        if path == "/api/voice/persona":
            # The front desk's standing instructions, for the Config page to edit.
            return self.send_json(voice.persona())

        if path == "/api/instructions" or path.startswith("/api/instructions/"):
            # The CLAUDE.md files, for the Config page to edit: the list, or one file whole.
            from . import instructions
            key = path[len("/api/instructions/"):]
            try:
                return self.send_json(instructions.doc(key) if key else instructions.listing())
            except ValueError as exc:
                return self.fail(exc, 404)

        if path == "/api/voice/agent-prompt":
            # How every agent's voice speaks on a call, for the Config page to edit.
            from . import agentcall
            return self.send_json(agentcall.voice_prompt_doc())

        if path == "/api/vault":
            # The saved logins on both sides: agent, title, site. Never a user name or a password.
            from . import vault
            return self.send_json({side: vault.listing(side) for side in vault.SIDES})

        if path == "/api/mail/drafts":
            # The email drafts the agents wrote, for the cards in Insights, the chat and the feed.
            from . import mail
            return self.send_json(mail.listing())

        if path in ("/api/whatsapp/status", "/api/whatsapp/chats", "/api/whatsapp/drafts"):
            # For Config: linked or the QR, the chats seen with where each goes, and the replies
            # the agents drafted with whether sending is on. No messages.
            from . import whatsapp
            if path.endswith("status"):
                return self.send_json(dict(whatsapp.status(), **whatsapp.settings()))
            return self.send_json(whatsapp.chats() if path.endswith("chats") else whatsapp.drafts())

        if path == "/api/whatsapp/list":
            # The agents' whatsapp_chats tool: the chats on its side, with no messages.
            from . import whatsapp
            try:
                return self.send_json(whatsapp.list_chats((qs.get("agent") or [""])[0],
                                                          (qs.get("since_hours") or [24])[0]))
            except ValueError as exc:
                return self.fail(exc, 400)

        if path == "/api/whatsapp/messages":
            # The agents' whatsapp_read tool. An agent asks for its own id and gets the chats it
            # owns, or the ones on its side that it names.
            from . import whatsapp
            try:
                return self.send_json(whatsapp.read((qs.get("agent") or [""])[0],
                                                    (qs.get("since_hours") or [24])[0],
                                                    (qs.get("chats") or [None])[0]))
            except ValueError as exc:
                return self.fail(exc, 400)

        if path == "/api/browser":
            # Which agents have a browser open, who is watching, and when each idles shut.
            from . import browsers
            return self.send_json(browsers.status())

        if path == "/api/dictate":
            # Which engine the chat's microphone uses.
            from . import dictation
            return self.send_json(dictation.settings())

        if path == "/api/voice/settings":
            # The memory window: how many turns of the call log go into every call's briefing.
            return self.send_json(memory.settings())

        if path == "/api/openai/usage":
            from . import openai_log
            return self.send_json(openai_log.summary())

        if path == "/api/calm":
            return self.send_json(calm.for_agent())

        if path == "/api/quiet":
            return self.send_json(quiet.for_agent())

        if path == "/api/inbox":
            # What the front desk has to tell: every waiting item, and the last day of the others.
            return self.send_json(inbox.listing())

        if path == "/api/posts":
            # The feed, newest first.
            from . import posts
            try:
                count = int((qs.get("n") or ["100"])[0])
            except ValueError:
                count = 100
            try:
                days = float((qs.get("days") or ["0"])[0]) or None
            except ValueError:
                days = None
            try:
                before = float((qs.get("before") or ["0"])[0]) or None
            except ValueError:
                before = None
            return self.send_json(posts.listing(count, days, before))

        if path == "/api/tasks":
            # The task cards: open ones first, then the last three days of answered ones.
            side = (qs.get("side") or [""])[0]
            if side and side not in ("personal", "work"):
                return self.fail("side is personal or work")
            return self.send_json(tasks.listing(side or None))

        m = re.match(r"^/api/tasks/agent/([a-z0-9-]{1,60})$", path)
        if m:
            # The task_list tool: the agent's own tasks and the owner's open ones.
            return self.send_json(tasks.for_agent(m.group(1)))

        if path == "/api/tasks/done":
            # The Done list, ten at a time: closed agent tasks and his own ticked ones.
            return self.send_json(tasks.finished((qs.get("before") or [""])[0],
                                                 (qs.get("limit") or [""])[0] or tasks.PAGE))

        if path == "/api/tasks/settings":
            return self.send_json(tasks.settings())

        if path == "/api/phone/commands":
            # What the phone app should do now, such as an alarm to set. The phone gets these on
            # its event stream; this is for the moment it opens.
            from . import phone
            return self.send_json(phone.commands())

        if path == "/api/phone/notifications":
            from . import phone
            try:
                count = int((qs.get("n") or [50])[0])
            except ValueError:
                count = 50
            return self.send_json({"warning": untrusted.note(
                "phone notifications, written by other people and apps"), **phone.recent(count)})

        m = DATA_PATH.match(path)
        if m:
            return self.agent_data(m.group(1), m.group(2))

        m = DELEGATE_PATH.match(path)
        if m and not m.group(2):
            from . import delegates
            return self.send_json(delegates.listing(m.group(1)))

        m = AGENT_PATH.match(path)
        if m and m.group(2) == "data":
            rec = registry.get(m.group(1))
            if not rec:
                return self.fail("no such agent", 404)
            return self.send_json(data.available(rec))

        m = AGENT_PATH.match(path)
        if m and m.group(2) == "messages":
            rec = registry.get(m.group(1))
            if not rec:
                return self.fail("no such agent", 404)
            try:
                count = max(1, min(int((qs.get("n") or [20])[0]), 10000))
            except ValueError:
                count = 20
            return self.send_json(transcript.page(rec, count))

        m = AGENT_PATH.match(path)
        if m and m.group(2) == "tool":
            # One tool call whole, for the panel a tap on a command or a result opens.
            rec = registry.get(m.group(1))
            if not rec:
                return self.fail("no such agent", 404)
            got = transcript.tool_call(rec, (qs.get("id") or [""])[0])
            return self.send_json(got) if got else self.fail("no such tool call", 404)

        m = AGENT_PATH.match(path)
        if m and m.group(2) == "calls":
            # The agent's calls with the owner, answered and missed, for its chat log.
            if not registry.get(m.group(1)):
                return self.fail("no such agent", 404)
            from . import agentcall
            return self.send_json({"calls": agentcall.listing(m.group(1))})

        m = AGENT_PATH.match(path)
        if m and m.group(2) == "shown":
            # Job results set to show in the chat, for the owner. The agent never reads them.
            if not registry.get(m.group(1)):
                return self.fail("no such agent", 404)
            return self.send_json({"shown": cron.shown(m.group(1))})

        m = AGENT_PATH.match(path)
        if m and m.group(2) == "open":
            # What the agent and the owner talked about and never did, from OPEN.md in its folder.
            rec = registry.get(m.group(1))
            if not rec:
                return self.fail("no such agent", 404)
            from . import openlist
            return self.send_json(openlist.listing(rec))

        m = AGENT_PATH.match(path)
        if m and m.group(2) == "held":
            if not registry.get(m.group(1)):
                return self.fail("no such agent", 404)
            return self.send_json({"held": mailbox.waiting_list(m.group(1))})

        m = AGENT_PATH.match(path)
        if m and not m.group(2):
            rec = registry.get(m.group(1))
            return self.send_json(agent_view(rec)) if rec else self.fail("no such agent", 404)

        return self.frontend(path)

    def do_POST(self):
        if not self.allowed():
            return self.fail("not on the tailnet", 403)
        path = unquote(urlparse(self.path).path)
        if path == "/mcp":
            return self.outside_mcp()
        body = self.body_json()

        # A delegate reaches the owner only through its parent. Checked here,
        # once, for every route that rings, posts or lists something for him.
        from . import delegates
        m = AGENT_PATH.match(path)
        said = delegates.refuse(path, body, m.group(1) if m and m.group(2) == "call" else None)
        if said:
            return self.fail(said, 403)
        m = DELEGATE_PATH.match(path)
        if m or (AGENT_PATH.match(path) and path.endswith("/report")):
            return self.delegate_route(m, path, body)

        try:
            if path == "/api/codex/login":
                # A Codex sign-in with a one-time code. The end of it
                # comes back on the "codex" topic, so the screen does not poll.
                from . import codex
                if not self.from_console():
                    return self.fail("Codex is signed in from the console only", 403)
                return self.send_json(codex.login_start(
                    lambda st: hub.publish("codex", st, topic="codex")))
            if path == "/api/outside":
                # A new outside agent, from Config. The token in the answer is shown once.
                from . import outside
                if not self.from_console():
                    return self.fail("outside agents are added from the console only", 403)
                return self.send_json(outside.add(body.get("name"), body.get("side")), 201)
            if path == "/api/outside/reply":
                # A Ergo agent's answer to an outside agent, through its reply_outside tool.
                from . import outside
                try:
                    return self.send_json(outside.deliver(body.get("to"), body.get("from"),
                                                          body.get("text"), body.get("file")))
                except KeyError as exc:
                    return self.fail(str(exc).strip("'"), 404)
            if path == "/api/agents":
                return self.create_agent(body)
            if path == "/api/agents/register":
                return self.register_agent(body)
            if path == "/api/cords":
                return self.create_cord(body)
            if path == "/api/jobs":
                return self.create_job(body)
            if path == "/api/fetch":
                return self.fetch_for_screen(body)
            if path == "/api/recruits/show":
                return self.bring_back(body)
            if path == "/api/recruits/adopt":
                return self.adopt_session(body)
            if path == "/api/network/speed":
                # A button, never a timer. It costs real bandwidth on the connection it measures.
                return self.send_json(network.speed())

            if path in ("/api/vault", "/api/vault/remove"):
                # Logins are added and removed by the owner, from the console over the tailnet, never by
                # an agent on the laptop: an agent's call comes on loopback without the proxy's headers.
                from . import vault
                proxied = self.headers.get("X-Forwarded-For") or self.headers.get("Tailscale-User-Login")
                if not proxied and self.peer.startswith(("127.", "::1", "::ffff:127.")):
                    return self.fail("logins are changed from the console only", 403)
                return self.send_json(vault.add(body) if path == "/api/vault" else vault.remove(body))

            if path == "/api/whatsapp/changed":
                # The bridge says a file changed. The daemon reads the file itself and pushes it to
                # the console, so Config never asks on a timer. Nothing in the request is trusted.
                from . import whatsapp
                if body.get("what") == "mention":
                    # Someone wrote @ergo. The daemon finds it in the files itself.
                    threading.Thread(target=whatsapp.mentions, daemon=True).start()
                    return self.send_json({"ok": True})
                if body.get("what") == "deaf":
                    # The bridge heard nothing for 20 minutes while linked, and is restarting.
                    whatsapp.went_deaf()
                    return self.send_json({"ok": True})
                what = "chats" if body.get("what") == "chats" else "status"
                hub.publish("whatsapp", {"what": what, "status": whatsapp.status()},
                            topic="whatsapp")
                return self.send_json({"ok": True})

            if path.startswith("/api/whatsapp/"):
                return self.whatsapp_route(path[len("/api/whatsapp/"):], body)

            if path.startswith("/api/mail/"):
                return self.mail_route(path[len("/api/mail/"):], body)

            m = BROWSER_PATH.match(path)
            if m:
                return self.browser_action(m.group(1), m.group(2), body)

            if path in ("/api/dictate", "/api/dictate/session"):
                from . import dictation
                try:
                    return self.send_json(dictation.save_settings(body) if path == "/api/dictate"
                                          else dictation.session(body))
                except PermissionError as exc:
                    return self.fail(exc, 503)
                except RuntimeError as exc:
                    return self.fail(exc, 502)

            if path in ("/api/assist/route", "/api/assist/send"):
                # Assistant mode: the bar Ergo opens when he holds the power key.
                from . import assist
                if path == "/api/assist/route":
                    return self.send_json(assist.route(
                        body.get("text"), body.get("app"),
                        screen=assist._screen(body.get("screen_text"), body.get("url"))))
                try:
                    return self.send_json(assist.send(body))
                except PermissionError as exc:
                    return self.fail(exc, 503)
                except ValueError as exc:
                    return self.fail(exc, 400)
                except RuntimeError as exc:
                    return self.fail(exc, 502)

            if path.startswith("/api/voice/"):
                return self.voice_route(path[len("/api/voice/"):], body)

            if path.startswith("/api/phone/"):
                return self.phone_route(path[len("/api/phone/"):], body)

            if path == "/api/inbox" or path.startswith("/api/inbox/"):
                return self.inbox_route(path, body)

            if path == "/api/openai/used":
                # The browser's dictation minutes: the audio goes straight to OpenAI, so only the
                # browser knows how long it streamed.
                from . import openai_log
                try:
                    seconds = min(600.0, max(0.0, float(body.get("seconds") or 0)))
                except (TypeError, ValueError):
                    return self.fail("seconds is a number", 400)
                if seconds:
                    openai_log.record("dictation", config.DICTATE_OPENAI_MODEL, seconds=seconds)
                return self.send_json({"ok": True})

            if path == "/api/shift/request":
                # The agents' shift_request tool, on loopback with its own agent id.
                from . import shift
                try:
                    return self.send_json(shift.request(
                        body.get("agent"), body.get("title"), body.get("what"), body.get("why"),
                        body.get("urgency") or "normal", body.get("requested_by_owner") is True))
                except KeyError as exc:
                    return self.fail(exc.args[0] if exc.args else "no such agent", 404)
                except ValueError as exc:
                    return self.fail(exc, 400)

            m = re.match(r"^/api/instructions/([a-z0-9-]{1,200})(/previous)?$", path)
            if m:
                # The owner's edit, from the console only: an agent on loopback must not rewrite
                # another agent's rules through the daemon.
                if not self.from_console():
                    return self.fail("the CLAUDE.md files are edited from the console only", 403)
                from . import instructions
                try:
                    return self.send_json(instructions.restore(m.group(1)) if m.group(2)
                                          else instructions.save(m.group(1), body.get("text")))
                except ValueError as exc:
                    return self.fail(exc, 400)

            if path == "/api/quiet":
                # The agents' quiet_for tool, and the console. Any agent may set it: he asks
                # whichever one is in front of him.
                try:
                    return self.send_json(quiet.set_for(body.get("minutes"), body.get("by")))
                except ValueError as exc:
                    return self.fail(exc, 400)

            if path == "/api/calm":
                if not self.from_console():
                    return self.fail("calm mode is changed from the console only", 403)
                try:
                    return self.send_json(calm.save(body))
                except ValueError as exc:
                    return self.fail(exc, 400)

            if path == "/api/tasks" or path.startswith("/api/tasks/"):
                return self.tasks_route(path, body)

            if path == "/api/posts" or path.startswith("/api/posts/"):
                return self.posts_route(path, body)

            m = JOB_PATH.match(path)
            if m and m.group(2) == "run":
                # Answer 1 of the daemon grill: a missed job never runs itself late, and this is
                # the other half of that. The offer to rerun has to land somewhere.
                return self.send_json({"ok": True, "said": cron.run_now(m.group(1))})

            m = CORD_PATH.match(path)
            if m and m.group(2) == "hangup":
                ok = cords.hangup(m.group(1), (body.get("summary") or "").strip())
                hub.publish("cords")
                return self.send_json({"ok": ok})

            m = AGENT_PATH.match(path)
            if m and m.group(2) == "call":
                # The agents' call_owner tool, over loopback with its own id. A fourth call in
                # an hour is refused with 429 and a sentence saying what to do instead.
                from . import agentcall
                try:
                    return self.send_json(agentcall.call_owner(m.group(1), body.get("reason")))
                except PermissionError as exc:
                    return self.fail(exc, 429)
            if m:
                return self.agent_action(m.group(1), m.group(2), body)
        except (KeyError, ValueError) as exc:
            return self.fail(exc, 400)
        except PermissionError as exc:
            return self.fail(exc, 403)
        except RuntimeError as exc:
            return self.fail(exc, 409)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # Most often the phone or a tab closed the connection before the answer went, and
            # fail() then drops the answer quietly. A pipe to something else that broke still
            # gets its 500, so the client is never left waiting.
            return self.fail(exc, 500)
        except Exception as exc:
            traceback.print_exc()
            return self.fail(exc, 500)

        return self.fail("not found", 404)

    def do_PUT(self):
        """Receive a file. The body is the file and the name is in the address.

        A raw PUT rather than a multipart form, on purpose. Multipart needs a parser, the standard
        library's was removed in 3.13, and a browser can send a File object as a raw body in one
        line of fetch. Fewer moving parts for exactly the same result.
        """
        if not self.allowed():
            return self.fail("not on the tailnet", 403)
        rec_m = re.match(r"^/api/voice/recording/([A-Za-z0-9_-]+)$", unquote(urlparse(self.path).path))
        if rec_m:
            # A call's recording, from the app when a session closes. The console only: an agent
            # has no business writing his call audio.
            if not self.from_console():
                return self.fail("call recordings come from the console only", 403)
            length = self.headers.get("Content-Length")
            # ?part=N: one ten-second piece, sent during the call.
            part = parse_qs(urlparse(self.path).query).get("part", [None])[0]
            try:
                part = int(part) if part is not None else None
                saved = voice.save_recording(rec_m.group(1), self.rfile, int(length) if length else 0,
                                             part=part)
            except ValueError as exc:
                return self.fail(exc, 400)
            except OSError as exc:
                return self.fail(exc, 500)
            return self.send_json({"ok": True, "file": saved.name}, 201)
        m = re.match(r"^/api/agents/([a-z0-9-]+)/inbox/(.+)$", unquote(urlparse(self.path).path))
        if not m:
            return self.fail("not found", 404)
        agent_id, name = m.group(1), m.group(2)
        rec = registry.get(agent_id)
        if not rec:
            return self.fail("no such agent", 404)

        length = self.headers.get("Content-Length")
        try:
            path, size = files.receive(rec, name, self.rfile, int(length) if length else None)
        except ValueError as exc:
            return self.fail(exc, 413)
        except OSError as exc:
            return self.fail(exc, 500)

        # The agent is told where the file landed, held if it is asleep. It is not told to read it:
        # what to do with a document is the operator's next message, and a file that arrives with
        # its own instructions is the injection path this platform assumes will be used.
        # X-Platform-Quiet: the chat composer uploads first and names the file inside the message
        # it sends next, so a separate arrival note would reach the agent as a second message.
        if self.headers.get("X-Platform-Quiet") == "1":
            hub.publish("agents")
            return self.send_json({"ok": True, "path": str(path), "bytes": size,
                                   "told_agent": "quiet"}, 201)
        note = self.headers.get("X-Platform-Note") or None
        outcome = broker.send(agent_id, files.arrival_message(str(path), size, note),
                              wake=False)
        hub.publish("agents")
        return self.send_json({"ok": True, "path": str(path), "bytes": size,
                               "told_agent": outcome or "failed"}, 201)

    def do_PATCH(self):
        if not self.allowed():
            return self.fail("not on the tailnet", 403)
        path = unquote(urlparse(self.path).path)

        m = JOB_PATH.match(path)
        if m and not m.group(2):
            try:
                job = cron.update(m.group(1), **{k: v for k, v in self.body_json().items()
                                                 if k in ("task", "at", "every", "enabled",
                                                          "timeout", "quiet", "days", "start",
                                                          "until", "retries", "kind", "model",
                                                          "reason", "show", "check")})
            except ValueError as exc:
                return self.fail(exc, 400)
            hub.publish("jobs")
            return self.send_json(job) if job else self.fail("no such job", 404)

        m = AGENT_PATH.match(path)
        if not (m and not m.group(2)):
            return self.fail("not found", 404)
        agent_id = m.group(1)
        body = self.body_json()
        fields = {k: v for k, v in body.items()
                  if k in ("name", "model", "schedule", "note", "permission_mode", "views",
                           "role", "cwd", "autocompact", "engine")}
        if "engine" in fields and broker.awake(agent_id):
            return self.fail("put the agent to sleep before you change its engine", 409)
        if "cwd" in fields and self.move_answered(agent_id, fields, body):
            return
        try:
            rec = registry.update(agent_id, **fields)
        except ValueError as exc:
            return self.fail(exc, 400)
        if not rec:
            return self.fail("no such agent", 404)
        hub.publish("agents")
        return self.send_json(agent_view(rec))

    def do_DELETE(self):
        if not self.allowed():
            return self.fail("not on the tailnet", 403)
        path = unquote(urlparse(self.path).path)

        m_out = re.match(r"^/api/outside/([a-z0-9-]+)$", path)
        if m_out:
            from . import outside
            if not self.from_console():
                return self.fail("outside agents are removed from the console only", 403)
            try:
                return self.send_json(outside.remove(m_out.group(1)))
            except KeyError:
                return self.fail("no such outside agent", 404)

        m = JOB_PATH.match(path)
        if m and not m.group(2):
            gone = cron.remove(m.group(1))
            hub.publish("jobs")
            return self.send_json({"ok": gone})

        m = AGENT_PATH.match(path)
        if not (m and not m.group(2)):
            return self.fail("not found", 404)
        # Removing an agent hides it. Its conversation and its process are untouched, because
        # closing a card by accident must not destroy work.
        rec = registry.hide(m.group(1))
        hub.publish("agents")
        return self.send_json({"hidden": bool(rec)})

    # -- agents -----------------------------------------------------------------

    def move_answered(self, agent_id, fields, body):
        """Check a folder change before the record takes it.

        Returns True when it has already sent an answer, which means the move must not go on.

        The daemon records where an agent lives. It never moves a single file. Claude Code names
        the transcript directory after the folder the session ran in, so the folder and the
        conversation have to travel together, and only the person doing the move knows what else
        is in there.

        Asleep first. A running engine already holds the old folder open, and it would go on
        writing the conversation under the old name while the record said something else. Waking
        it later in the new folder would then start an empty conversation.
        """
        if not registry.get(agent_id):
            self.fail("no such agent", 404)
            return True
        if broker.awake(agent_id):
            self.fail("put the agent to sleep before you move it", 409)
            return True
        try:
            cwd = config.resolve_dir(fields["cwd"])
        except ValueError as exc:
            self.fail(exc, 400)
            return True
        # The same question POST /api/agents asks, and for the same reason. A folder that is not
        # there is more often a typed mistake than an instruction to make one, and the person sees
        # the full path in the question before saying yes.
        if not os.path.isdir(cwd):
            if not body.get("create_dir"):
                self.send_json({"needs_dir": True, "path": cwd})
                return True
            try:
                os.makedirs(cwd, exist_ok=True)
            except OSError as exc:
                self.fail(f"could not create {cwd}: {exc}")
                return True
        fields["cwd"] = cwd
        return False

    def delegate_route(self, m, path, body):
        """An agent's delegate tools, over loopback. See delegates.py for the rules."""
        from . import delegates
        try:
            if not m:
                outcome = delegates.report(AGENT_PATH.match(path).group(1), body.get("text"))
                return self.send_json({"ok": bool(outcome), "outcome": outcome or "failed"})
            parent, one, act = m.groups()
            if not one:
                made = delegates.make(parent, body.get("name"), body.get("purpose"),
                                      body.get("handoff"), body.get("model"))
                if (registry.get(made["id"]) or {}).get("engine") == "claude":
                    broker.start(made["id"])
                hub.publish("agents")
                return self.send_json(made, 201)
            if act == "message":
                outcome = delegates.tell(parent, one, body.get("text"))
                hub.publish("agents")
                return self.send_json({"ok": bool(outcome), "outcome": outcome or "failed"})
            out = (delegates.archive if act == "archive" else delegates.revive)(parent, one)
            hub.publish("agents")
            return self.send_json(out)
        except KeyError as exc:
            return self.fail(exc.args[0] if exc.args else "no such agent", 404)
        except PermissionError as exc:
            return self.fail(exc, 403)
        except OverflowError as exc:
            return self.fail(exc, 409)
        except (ValueError, FileExistsError) as exc:
            return self.fail(exc, 400)

    def create_agent(self, body):
        name = (body.get("name") or "").strip()
        if not name:
            return self.fail("a name is needed")
        engine = (body.get("engine") or "claude").strip().lower()
        if engine not in registry.ENGINES:
            return self.fail("engine must be one of " + ", ".join(registry.ENGINES), 400)
        try:
            cwd = config.resolve_dir(body.get("cwd"))
        except ValueError as exc:
            return self.fail(exc, 400)
        # A folder that is not there is a question, not an error: the console asks, then sends the
        # request back with create_dir set. The tool that agents use creates without asking, because
        # an agent has no one to ask; a person does. This is also the guard against a mistyped path,
        # which is how "~" left in the field ahead of an absolute path became a doubled directory: the
        # person sees the full path in the question and can say no.
        if not os.path.isdir(cwd):
            if not body.get("create_dir"):
                return self.send_json({"needs_dir": True, "path": cwd})
            try:
                os.makedirs(cwd, exist_ok=True)
            except OSError as exc:
                return self.fail(f"could not create {cwd}: {exc}")
        try:
            rec = registry.create(
                name=name, cwd=cwd,
                model=(body.get("model") or None),
                role=(body.get("role") or None),
                permission_mode=(body.get("permission_mode") or None),
                unix_user=(body.get("unix_user") or None),
                engine=engine,
            )
        except ValueError as exc:
            return self.fail(exc, 400)
        # Codex and OpenCode agents wait for their first chat message rather than opening a
        # terminal. Each names its conversation on the first turn, and the chat records that name.
        if body.get("start", engine == "claude") and not rec.get("unix_user"):
            broker.start(rec["id"])
        hub.publish("agents")
        return self.send_json(agent_view(registry.get(rec["id"])), 201)

    def register_agent(self, body):
        """A Claude Code session started by hand, adding itself to the console.

        The session id is the one thing it must supply, because that is its identity. It arrives
        grey: the console does not own its terminal, so it cannot stream it. Waking it from here
        opens a second terminal on the same conversation, which is the honest thing to do.
        """
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return self.fail("session_id is needed")
        existing = registry.by_session(sid)
        if existing:
            registry.show(existing["id"])
            hub.publish("agents")
            return self.send_json(agent_view(registry.get(existing["id"])))
        rec = registry.create(
            name=(body.get("name") or "").strip() or sid[:8],
            cwd=os.path.expanduser((body.get("cwd") or "~").strip()),
            model=body.get("model") or None,
            session_id=sid,
            self_registered=True,
        )
        hub.publish("agents")
        return self.send_json(agent_view(rec), 201)

    def bring_back(self, body):
        """Show an agent the console hid. The record never left, so this is one flag."""
        agent_id = (body.get("id") or "").strip()
        rec = recruits.bring_back(agent_id) if agent_id else None
        if not rec:
            return self.fail("no such agent", 404)
        hub.publish("agents")
        return self.send_json(agent_view(registry.get(agent_id)))

    def adopt_session(self, body):
        """Take a Claude Code or OpenCode conversation and make it an agent.

        Nothing is started. The record points at the session id, so the first wake resumes the
        conversation exactly as the broker resumes any other, and the owner decides when.

        Adopting twice is not an error. The second call shows the record the first one made and
        hands it back, because two phones on the same list is normal and the second press must
        not make a second agent on the same conversation.
        """
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return self.fail("session_id is needed")
        engine = (body.get("engine") or "").strip().lower()
        if engine and engine not in registry.ENGINES:
            return self.fail("engine must be one of " + ", ".join(registry.ENGINES), 400)
        if engine:
            existing = (registry.by_opencode_session(sid) if engine == "opencode"
                        else registry.by_session(sid))
            found = recruits.find(sid, engine=engine)
        else:
            # A client that predates the engine field sends only the id. Claude ids and
            # OpenCode ids are different namespaces, so ask Claude's ledger first and fall
            # back to OpenCode's rather than refusing the session the page just showed.
            existing = registry.by_session(sid)
            found = recruits.find(sid, engine="claude")
            engine = "claude"
            if not existing and not found:
                existing = registry.by_opencode_session(sid)
                found = recruits.find(sid, engine="opencode")
                engine = "opencode"
        if existing:
            registry.show(existing["id"])
            hub.publish("agents")
            return self.send_json(agent_view(registry.get(existing["id"])))

        if not found:
            return self.fail("no such session", 404)
        given = (body.get("cwd") or "").strip()
        try:
            cwd = config.resolve_dir(given) if given else (found["cwd"] or "")
        except ValueError as exc:
            return self.fail(exc, 400)
        if not cwd:
            return self.fail("that session never said which folder it ran in")
        # The same question POST /api/agents asks. A conversation outlives the folder it was
        # held in, so the folder may have been moved or deleted since, and the answer is either
        # to make it again or to point the agent somewhere else.
        if not os.path.isdir(cwd):
            if not body.get("create_dir"):
                return self.send_json({"needs_dir": True, "path": cwd})
            try:
                os.makedirs(cwd, exist_ok=True)
            except OSError as exc:
                return self.fail(f"could not create {cwd}: {exc}")
        rec = recruits.adopt(sid, cwd, name=body.get("name"), found=found, engine=engine)
        hub.publish("agents")
        return self.send_json(agent_view(rec), 201)

    def agent_action(self, agent_id, action, body):
        if not registry.get(agent_id):
            return self.fail("no such agent", 404)

        if action == "read":
            # He opened this agent's chat: everything it has said so far is read.
            got = reads.mark(agent_id)
            hub.publish("agents")
            return self.send_json(got)

        if action == "wake":
            term = broker.start(agent_id)
            hub.publish("agents")
            return self.send_json({"ok": True, "pid": term.pid})

        if action == "sleep":
            ok = broker.sleep(agent_id, force=bool(body.get("force")))
            hub.publish("agents")
            return self.send_json({"ok": ok})

        if action == "stop":
            # Halts the task, leaves the agent alive and ready for the next message.
            ok = broker.stop(agent_id)
            hub.publish("agents")
            return self.send_json({"ok": ok})

        if action == "mode":
            # Which engine the agent runs: the terminal in a pseudo-terminal, or the chat engine
            # headless. Switching never wakes a sleeping agent, so opening a tab costs nothing.
            want = (body.get("mode") or "").strip()
            if want not in ("chat", "terminal"):
                return self.fail("mode must be chat or terminal")
            now = broker.switch(agent_id, want)
            hub.publish("agents")
            return self.send_json({"mode": now})

        if action == "answer":
            # Claude stopped and asked. This is the yes, the no, or the chosen option. The broker
            # looks the id up among that agent's pending asks, which is what says whether it is a
            # permission ask or a question ask; the browser does not have to remember.
            request_id = (body.get("id") or "").strip()
            if not request_id:
                return self.fail("which ask")
            answers = body.get("answers")
            if answers is not None and not isinstance(answers, dict):
                return self.fail("answers must be an object of question to answer")
            if not broker.answer(agent_id, request_id, allow=bool(body.get("allow")),
                                 answers=answers):
                # Not an error worth a status code. The usual cause is two phones on one agent and
                # the other one answered first, so the card here is simply out of date.
                return self.send_json({"ok": False, "error": "no such ask"})
            return self.send_json({"ok": True})

        if action == "permission_mode":
            # What Claude may do without asking. Always written to the record, so the next start
            # is what was asked for, and sent to a live chat engine as well, which takes it
            # without restarting.
            want = (body.get("mode") or "").strip()
            if want not in broker.PERMISSION_MODES:
                return self.fail("permission mode must be one of "
                                 + ", ".join(broker.PERMISSION_MODES))
            now = broker.set_permission_mode(agent_id, want)
            hub.publish("agents")
            return self.send_json({"mode": now})

        if action == "message":
            text = (body.get("text") or "").strip()
            if not text:
                return self.fail("nothing to send")
            # Held by default. A quick note from the dashboard should not wake an agent, because
            # waking costs memory and the caller usually does not want to pay it just to leave a
            # message. wake=true is for when the message is the reason to wake it.
            outcome = broker.send(agent_id, text, wake=bool(body.get("wake", False)))
            # His words about an offer, sent from Prompt: the card leaves Waiting, and Jev reads
            # the agent's reply when its turn ends. Only the console quotes a task this way.
            if outcome and self.from_console():
                tasks.prompted(agent_id, text)
            hub.publish("agents")
            return self.send_json({"ok": bool(outcome), "outcome": outcome or "failed",
                                   "waiting": mailbox.waiting(agent_id)})

        if action == "server":
            # Nothing stops a server when its agent sleeps, answer 7, so stopping one is always a
            # decision somebody made. Which is why it is here and not in the sleep path.
            if body.get("up") is False:
                return self.send_json({"ok": servers.stop(agent_id),
                                       "server": servers.state(agent_id)})
            started = servers.ensure(agent_id, "the operator asked")
            hub.publish("agents")
            return self.send_json({"ok": started, "server": servers.state(agent_id)})

        if action == "hide":
            registry.hide(agent_id)
            from . import delegates
            delegates.set_hidden(agent_id, True)
            hub.publish("agents")
            return self.send_json({"ok": True})

        if action == "show":
            registry.show(agent_id)
            from . import delegates
            delegates.set_hidden(agent_id, False)
            hub.publish("agents")
            return self.send_json({"ok": True})

        if action == "unfile":
            gone = files.remove(registry.get(agent_id), (body.get("name") or ""),
                                body.get("which") or "outbox")
            hub.publish("agents")
            return self.send_json({"ok": gone})

        if action == "sendheld":
            # The owner's Send on one held message, 15 October. A message held because the terminal
            # was busy waits for a start that may never come, since the agent is already up. This
            # sends it now, waking the agent if it sleeps. Still busy means held again, and said.
            one = (body.get("id") or "").strip()
            msg = mailbox.take_one(agent_id, one) if one else None
            if not msg:
                return self.fail("that message is no longer waiting", 404)
            outcome = broker.send(agent_id, msg["text"], wake=True)
            if not outcome:
                mailbox.hold(agent_id, msg["text"])
            hub.publish("agents")
            return self.send_json({"ok": outcome not in (None, "held"), "outcome": outcome or "failed",
                                   "waiting": mailbox.waiting(agent_id)})

        if action == "unhold":
            # With an id, one message goes. Without one, all of them do. Deleting the lot needs no
            # confirmation here because the console asks before it calls this.
            one = (body.get("id") or "").strip()
            if one:
                gone = 1 if mailbox.drop_one(agent_id, one) else 0
            else:
                gone = mailbox.drop(agent_id, keep_quiet=True)
            hub.publish("agents")
            return self.send_json({"ok": gone > 0, "deleted": gone,
                                   "waiting": mailbox.waiting(agent_id)})

        if action == "forget":
            broker.sleep(agent_id)
            mailbox.drop(agent_id)
            transcript.forget(agent_id)
            # Everything the daemon was holding on this agent's behalf goes too: its server, its
            # port, and its row in the status file. Leaving those behind means the port range fills
            # with names that no longer exist and a restart tries to bring back a server for an
            # agent that has gone.
            servers.release(agent_id)
            status.forget(agent_id)
            for job in cron.all():
                if job["agent"] == agent_id:
                    cron.remove(job["id"])
            registry.forget(agent_id)
            hub.publish("agents")
            return self.send_json({"ok": True})

        return self.fail("no such action", 404)

    # -- the voice front desk -----------------------------------------------------

    def phone_route(self, what, body):
        """The phone app's posts: an alarm to queue, a command done, notifications to keep."""
        from . import phone
        try:
            if what == "alarm":
                return self.send_json(phone.alarm(body.get("hour"), body.get("minute"),
                                                  body.get("label"), body.get("by"),
                                                  body.get("task_id")))
            m = re.match(r"^alarm/([A-Za-z0-9-]{4,200})/cancel$", what)
            if m:
                return self.send_json(phone.cancel(m.group(1)))
            if what == "alarms/cancel":
                return self.send_json(phone.cancel_for_task(body.get("task_id")))
            if what == "float":
                return self.send_json(phone.float_agent(body.get("agent")))
            m = re.match(r"^alarm/([A-Za-z0-9-]{4,200})/(stopped|prompt|done)$", what)
            if m:
                # From the phone only, which comes through the tailnet. An agent on loopback must
                # not speak as the owner to another agent.
                if not self.from_console():
                    return self.fail("the alarm screen speaks from the phone only", 403)
                if m.group(2) == "stopped":
                    return self.send_json(phone.stopped(m.group(1)))
                if m.group(2) == "done":
                    return self.send_json(phone.ticked(m.group(1)))
                return self.send_json(phone.prompt(m.group(1), body.get("text")))
            if what == "notifications":
                return self.send_json(phone.keep(body))
            if what == "callback":
                # "Call me back in 10 minutes" from the call screen.
                return self.send_json(inbox.callback(str(body.get("id") or "")))
            if what == "decline":
                # Decline on the call screen. The ring stops; the call is a missed one.
                return self.send_json(inbox.decline(str(body.get("id") or "")))
            m = re.match(r"^commands/([a-f0-9]{6,32})/done$", what)
            if m:
                return self.send_json(phone.done(m.group(1), body))
        except ValueError as exc:
            return self.fail(exc, 400)
        return self.fail("no such phone route", 404)

    def voice_offer(self, body):
        """Open a call in the view the console is in, so the front desk keeps to it."""
        with voice.scoped(body.get("view")):
            return voice.open_call(body.get("sdp"), body.get("resume"),
                                   body.get("back"), body.get("sent"), body.get("timing"),
                                   body.get("caller"))

    def voice_route(self, what, body):
        """Four posts: open a call, run a tool, write a turn down, close a call.

        The tool route is one door for every tool the voice has, so the browser forwards whatever
        the model asked for without knowing what any of it means. That matters more than it looks:
        a tool added here needs no change in the browser at all.
        """
        if what in ("session", "offer"):
            # Two steps, because GPT-Live has no ephemeral secret to hand a browser. `session` is
            # the door and calls OpenAI not at all, so a console with no key says so before it
            # asks for the microphone. `offer` swaps the browser's connection offer for OpenAI's
            # answer, which is the moment the call starts and the only moment the key is used.
            try:
                return self.send_json(
                    voice.start_session(body.get("caller"), fresh=not body.get("resume"))
                    if what == "session" else self.voice_offer(body))
            except PermissionError as exc:
                # 503 rather than 500. The console is working; the thing behind it is not
                # configured, and the overlay says exactly that sentence to the operator.
                return self.fail(exc, 503)
            except ValueError as exc:
                return self.fail(exc, 400)
            except RuntimeError as exc:
                # 402 is the payment one, and a spent budget is what it means here. The overlay
                # reads the sentence rather than the number. Anything else went wrong at OpenAI,
                # which is a bad gateway rather than a bad console.
                spent = "used its" in str(exc)
                return self.fail(exc, 402 if spent else 502)

        if what == "heard":
            # What he said while the call was still connecting, written down for the front desk.
            from . import voicenote
            try:
                return self.send_json(voicenote.transcribe(body))
            except PermissionError as exc:
                return self.fail(exc, 503)
            except ValueError as exc:
                return self.fail(exc, 400)
            except RuntimeError as exc:
                return self.fail(exc, 502)

        if what in ("greeting", "greeting/reset"):
            # The front desk's own "Welcome back", recorded once from a real call and played at
            # the tap, so the greeting has no wait.
            try:
                if what == "greeting" and body.get("agent"):
                    # An agent's "Hi the owner, it's the assistant.", taken from its own first call.
                    from . import agentcall
                    return self.send_json(agentcall.save_greeting(body["agent"], body))
                return self.send_json(voice.save_greeting(body) if what == "greeting"
                                      else voice.reset_greeting())
            except ValueError as exc:
                return self.fail(exc, 400)

        if what in ("note", "note/send"):
            # A voice note from the wheel: written down by OpenAI, then sent to the agent, at
            # once or after he checks the words.
            from . import voicenote
            try:
                return self.send_json(voicenote.note(body) if what == "note"
                                      else voicenote.send_text(body))
            except PermissionError as exc:
                return self.fail(exc, 503)
            except ValueError as exc:
                return self.fail(exc, 400)
            except RuntimeError as exc:
                return self.fail(exc, 502)

        if what == "smalltalk":
            # One small talk turn while an agent works: the words, from the daemon.
            from . import smalltalk
            return self.send_json(smalltalk.line())

        if what in ("agent-prompt", "agent-prompt/previous"):
            from . import agentcall
            try:
                return self.send_json(agentcall.save_voice_prompt(body.get("text"))
                                      if what == "agent-prompt" else agentcall.restore_voice_prompt())
            except ValueError as exc:
                return self.fail(exc, 400)

        if what in ("persona", "persona/previous"):
            try:
                return self.send_json(voice.save_persona(body.get("text")) if what == "persona"
                                      else voice.restore_persona())
            except ValueError as exc:
                return self.fail(exc, 400)

        if what == "settings":
            # The Config page's two number fields. Out of range is pulled into range; not a number is
            # refused with a sentence the field shows under itself.
            try:
                return self.send_json(memory.save_settings(body))
            except ValueError as exc:
                return self.fail(exc, 400)

        if what == "tool":
            name = (body.get("name") or "").strip()
            args = body.get("args")
            # An agent's call has its own two tools, tied to the agent by the session the daemon
            # opened. The browser names the session; it never names the agent.
            from . import agentcall
            if agentcall.session_call(body.get("session_id")):
                return self.send_json({"result": agentcall.tool(body.get("session_id"), name, args)})
            with voice.scoped(body.get("view")):
                return self.send_json({"result": voice.call(name, args)})

        if what == "judge":
            # The referee. Jev reads his line against the card, or her line for a claim that
            # something was done. It answers; the browser acts through confirm and cancel.
            try:
                return self.send_json(judge.judge(body))
            except ValueError as exc:
                return self.fail(exc, 400)

        if what == "log":
            if body.get("caller"):
                # A line of an agent's call goes to that call's record, not the front desk's memory.
                from . import agentcall
                row = agentcall.turn(body.get("caller"), body.get("who"), body.get("text"))
                return self.send_json({"ok": bool(row), "row": row})
            row = voice.say_down(body.get("who"), body.get("text"))
            return self.send_json({"ok": bool(row), "row": row})

        if what == "end":
            # The browser also sends this with navigator.sendBeacon as the tab closes. A beacon is
            # a plain POST with a JSON body and no other headers, and body_json reads it the same.
            left = voice.end_session(body.get("session_id"), body.get("seconds") or 0,
                                     body.get("why"))
            # An agent's call is written down as it ends, and the agent gets the transcript.
            from . import agentcall
            agentcall.session_ended(body.get("session_id"), body.get("seconds") or 0,
                                    body.get("why"))
            return self.send_json(left)

        if what == "image":
            # The live voice cannot see, so a small vision model looks and the voice is handed
            # the words. Every refusal is a sentence the overlay puts in the transcript.
            try:
                return self.send_json(voice.see_image(body.get("image"), body.get("text")))
            except PermissionError as exc:
                return self.fail(exc, 503)
            except ValueError as exc:
                return self.fail(exc, 413 if "too large" in str(exc) else 400)
            except RuntimeError as exc:
                return self.fail(exc, 502)

        return self.fail("no such voice route", 404)

    # -- the inbox ---------------------------------------------------------------

    def inbox_route(self, path, body):
        """Add an item, clear the waiting ones, ask Jev, or act on one item."""
        if path == "/api/inbox":
            # The agents' brief tool comes here, over loopback, with its own agent id.
            urgency = str(body.get("urgency") or "today").strip().lower()
            if calm.on() and urgency != "urgent":
                return self.fail("Calm mode is on, so the owner hears only urgent briefs. Hold this "
                                 "one until he turns it off. Do not mention calm mode.", 409)
            return self.send_json(inbox.add(body.get("agent"), body.get("text"),
                                            body.get("urgency") or "today",
                                            body.get("expires"), body.get("link"),
                                            when=body.get("when")))
        if path == "/api/inbox/clear":
            return self.send_json(inbox.clear(body.get("confirm")))
        if path == "/api/inbox/judge":
            # The voice asks after each finished line. Off the audio path: nothing waits on it.
            return self.send_json(inbox.judge(body))
        m = INBOX_PATH.match(path)
        if not m:
            return self.fail("no such inbox route", 404)
        try:
            if m.group(2) == "take_back":
                # An agent taking back its own brief once it is out of date. Its own only.
                item = inbox.get(m.group(1))
                if not item or item["agent"] != body.get("agent"):
                    return self.fail("that is not one of your briefs", 403)
                return self.send_json(inbox.act(m.group(1), "removed", by=body["agent"]))
            return self.send_json(inbox.act(m.group(1), m.group(2), by=body.get("by")))
        except KeyError as exc:
            return self.fail(exc.args[0] if exc.args else "no such item", 404)

    # -- tasks -------------------------------------------------------------------

    def posts_route(self, path, body):
        """An agent posts from its tools on loopback. Marks come from the owner on the console only."""
        from . import posts
        try:
            if path == "/api/posts":
                return self.send_json(posts.post(body.get("agent"), body.get("text"),
                                                 body.get("kind"), body.get("link"),
                                                 body.get("draft"), body.get("card"),
                                                 body.get("media"), body.get("job"),
                                                 body.get("transcript")), 201)
            m = re.match(r"^/api/posts/([0-9a-f]{6,16})/mark$", path)
            if m:
                if not self.from_console():
                    return self.fail("only the owner marks a post", 403)
                return self.send_json(posts.mark(m.group(1), body.get("state")))
        except KeyError:
            return self.fail("no such post", 404)
        except ValueError as exc:
            return self.fail(exc, 400)
        return self.fail("no such route", 404)

    def tasks_route(self, path, body):
        """Offers, plans and results come from the agents' tools on loopback. Answers come from
        the owner on the console only, the same rule as /api/vault: an agent cannot say yes to its
        own offer."""
        try:
            if path == "/api/tasks":
                if calm.on():
                    return self.fail("Calm mode is on, so offers are held. Offer this after he "
                                     "turns it off. Do not mention calm mode.", 409)
                return self.send_json(tasks.offer(body.get("agent"), body.get("title"),
                                                  body.get("source"), body.get("deliverable"),
                                                  body.get("plan")))
            if path == "/api/tasks/mine":
                # His own task: from the console, or an agent adding one for him on loopback.
                by = None if self.from_console() else body.get("agent")
                if not self.from_console() and not by:
                    return self.fail("say which agent is adding it", 400)
                return self.send_json(tasks.add_mine(body.get("title"), by, body.get("due"),
                                                     body.get("side"), body.get("brief"),
                                                     body.get("night_ok") is True,
                                                     body.get("owner") if self.from_console()
                                                     else None))
            if path == "/api/tasks/owed":
                # Something another person promised him, added by the agent that found it.
                return self.send_json(tasks.add_owed(body.get("title"), body.get("who"),
                                                     body.get("agent"), body.get("due")))
            if path == "/api/tasks/settings":
                if not self.from_console():
                    return self.fail("task settings are changed from the console only", 403)
                return self.send_json(tasks.save_settings(body))
            m = TASK_PATH.match(path)
            if not m:
                return self.fail("no such task route", 404)
            task_id, action = m.group(1), m.group(2)
            if action == "planned":
                return self.send_json(tasks.plan(task_id, body.get("agent"), body.get("steps")))
            if action == "done":
                return self.send_json(tasks.done(task_id, body.get("agent"), body.get("result"),
                                                 body.get("file")))
            if action == "tick":
                # His own task off the list: his tap, or an agent he told it was done.
                return self.send_json(tasks.tick(task_id, body.get("agent") or None))
            if action == "nag" and body.get("agent"):
                # The owner, or a nag run in its name, keeping the task's nags (nags.py).
                return self.send_json(tasks.set_nag(task_id, body["agent"], body.get("brief"),
                                                    body.get("next_in_minutes"),
                                                    off=body.get("off"), note=body.get("note"),
                                                    night_ok=body.get("night_ok")))
            if action in ("move", "drop") and body.get("agent"):
                # An owner keeping its slice of his list true.
                if action == "drop":
                    return self.send_json(tasks.dismiss(task_id, body["agent"]))
                return self.send_json(tasks.move(task_id, body.get("due"), body["agent"]))
            if action == "move" and self.from_console():
                # His own tap on a time, from a tasks card in a chat.
                return self.send_json(tasks.move(task_id, body.get("due")))
            if action not in ("yes", "plan", "no", "why", "dismiss", "close", "reopen"):
                return self.fail("no such task action", 404)
            if not self.from_console():
                return self.fail("tasks are answered from the console only", 403)
            mode = body.get("mode") or None
            if action == "yes":
                return self.send_json(tasks.yes(task_id, mode))
            if action == "plan":
                return self.send_json(tasks.ask_plan(task_id, mode))
            if action == "no":
                return self.send_json(tasks.no(task_id, body.get("why")))
            if action == "why":
                return self.send_json(tasks.because(task_id, body.get("why")))
            if action == "close":
                return self.send_json(tasks.close(task_id))
            if action == "reopen":
                return self.send_json(tasks.reopen(task_id))
            return self.send_json(tasks.dismiss(task_id))
        except KeyError as exc:
            return self.fail(exc.args[0] if exc.args else "no such task", 404)
        except PermissionError as exc:
            # Someone else's card, or the sixth offer of the day.
            return self.fail(exc, 429 if "limit" in str(exc) else 403)

    # -- the schedule, data and fetching -----------------------------------------

    def create_job(self, body):
        try:
            job = cron.add(agent=(body.get("agent") or "").strip(),
                           task=body.get("task"), at=(body.get("at") or "").strip() or None,
                           every=body.get("every"), timeout=body.get("timeout"),
                           quiet=bool(body.get("quiet")), days=body.get("days"),
                           start=body.get("start"), until=body.get("until"),
                           once=body.get("once"), in_minutes=body.get("in_minutes"),
                           retries=body.get("retries") or 0, kind=body.get("kind"),
                           model=body.get("model"), reason=body.get("reason"),
                           show=body.get("show"),
                           watch=body.get("watch"), debounce=body.get("debounce_seconds"),
                           check=body.get("check"))
        except KeyError as exc:
            return self.fail(exc, 404)
        except ValueError as exc:
            return self.fail(exc, 400)
        hub.publish("jobs")
        return self.send_json(job, 201)

    def agent_data(self, agent_id, name):
        """One published thing, by name. The daemon decides whether that is a file or a server.

        Answer 38. The screen asking does not know which, and that is the point: an agent can move
        from a file to a live server without its own screen changing a line.
        """
        rec = registry.get(agent_id)
        if not rec:
            return self.fail("no such agent", 404)
        try:
            return self.send_json(data.resolve(rec, name))
        except KeyError as exc:
            return self.fail(exc, 404)
        except ValueError as exc:
            return self.fail(exc, 400)
        except OSError as exc:
            return self.fail(exc, 500)

    def fetch_for_screen(self, body):
        """The daemon fetching an address for a screen that has no network. Answer 30: any address.

        The caller is named in the log line and nowhere else. It is not a permission: there is
        nothing to permit, because every screen may ask for everything. It is so that a fetch in
        the log can be traced back to the screen that wanted it.
        """
        try:
            got = fetch.get(body.get("url"), method=body.get("method"),
                            headers=body.get("headers"), body=body.get("body"),
                            timeout=body.get("timeout"),
                            who=(body.get("agent") or "a screen")[:40])
        except fetch.Failed as exc:
            return self.fail(exc, 502)
        except ValueError as exc:
            return self.fail(exc, 400)
        return self.send_json(got)

    def create_cord(self, body):
        kind = (body.get("kind") or "transfer").strip()
        src, dst = (body.get("from") or "").strip(), (body.get("to") or "").strip()
        instruction = (body.get("instruction") or "").strip()
        if not instruction:
            return self.fail("a cord needs an instruction")
        if kind == "transfer":
            cord = cords.transfer(src, dst, instruction)
        elif kind == "call":
            cord = cords.call(src, dst, instruction, budget=body.get("budget"))
        else:
            return self.fail(f"unknown cord kind: {kind}")
        hub.publish("cords")
        return self.send_json(cord, 201)

    # -- streams ----------------------------------------------------------------

    def mail_route(self, rest, body):
        """Email drafts. An agent drafts; only the owner on the console sends, schedules, takes one
        back or discards it, the same rule as WhatsApp."""
        from . import mail
        if rest == "drafts":
            return self.send_json(mail.draft(body.get("agent"), body.get("to"), body.get("subject"),
                                             body.get("body"), cc=body.get("cc"),
                                             attach=body.get("attach"),
                                             send_at=body.get("send_at")))
        if not self.from_console():
            return self.fail("email is sent from the console only", 403)
        m = re.match(r"^drafts/([A-Za-z0-9]+)/(send|schedule|cancel|discard)$", rest)
        if not m:
            return self.fail("no such route", 404)
        draft_id, action = m.groups()
        edits = {k: body[k] for k in ("to", "cc", "subject", "body") if k in body}
        if action == "send":
            return self.send_json(mail.schedule(draft_id, None, edits))
        if action == "schedule":
            return self.send_json(mail.schedule(draft_id, body.get("at"), edits))
        if action == "cancel":
            return self.send_json(mail.cancel(draft_id))
        return self.send_json(mail.discard(draft_id))

    def whatsapp_route(self, rest, body):
        """The WhatsApp writes. An agent may draft, find a chat, and follow or unfollow one for
        itself. Everything else is the owner on the console:
        an agent on loopback without the proxy's headers gets 403, the same rule as /api/vault."""
        from . import whatsapp
        if rest == "drafts":
            # The agents' whatsapp_draft tool. Stored for the owner; nothing is sent.
            item = whatsapp.draft(body.get("agent"), body.get("chat"), body.get("text"),
                                  body.get("file"))
            hub.publish("whatsapp", {"what": "drafts"}, topic="whatsapp")
            return self.send_json(item)
        if rest == "find":
            return self.send_json(whatsapp.find(body.get("name")))
        if rest == "follow":
            # The agents' whatsapp_follow tool: a chat for the agent itself, never for another.
            got = whatsapp.follow(body.get("agent"), body.get("chat"), body.get("follow", True) is not False)
            hub.publish("whatsapp", {"what": "drafts"}, topic="whatsapp")
            return self.send_json(got)
        if not self.from_console():
            return self.fail("WhatsApp is changed and sent from the console only", 403)
        if rest == "chats":
            got = whatsapp.route(body)
            hub.publish("whatsapp", {"what": "drafts"}, topic="whatsapp")
            return self.send_json(got)
        if rest == "settings":
            return self.send_json(whatsapp.save_settings(body))
        if rest == "send":
            return self.send_json(whatsapp.send(body.get("chat"), body.get("text")))
        m = re.match(r"^drafts/([A-Za-z0-9]+)/(send|discard)$", rest)
        if m:
            draft_id, action = m.groups()
            got = (whatsapp.send_draft(draft_id, body.get("text")) if action == "send"
                   else whatsapp.discard_draft(draft_id))
            hub.publish("whatsapp", {"what": "drafts"}, topic="whatsapp")
            return self.send_json(got)
        return self.fail("no such route", 404)

    def events(self, qs=None):
        """The stream. ?topics=state,phone,inbox,health names what this client wants; see
        EventHub. A state client gets the whole state first and then only changes, a phone client
        the digest first and then each new one, so neither ever asks again while it is connected.

        Compressed as one gzip stream when the client takes gzip, flushed after every frame. One
        stream, so each frame is compressed against everything sent before it: a digest that
        differs from the last in one line costs about that line."""
        topics = {t for t in ",".join((qs or {}).get("topics", [])).split(",") if t}
        q = hub.add(topics or None)
        packer = zlib.compressobj(6, zlib.DEFLATED, 31) if self.gzip_ok() else None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        if packer:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        # Nothing in this path buffers today, but a proxy added later would break the stream
        # silently, and this header is how you tell one not to.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(text):
            raw = text.encode()
            if packer:
                raw = packer.compress(raw) + packer.flush(zlib.Z_SYNC_FLUSH)
            self.wfile.write(raw)
            self.wfile.flush()

        try:
            send(": open\n\n")
            if {"state", "phone"} & q.topics:
                version, state, digest = feed.current()
                if "state" in q.topics and state is not None:
                    whole = dict(state, v=version, server_time=time.time())
                    if "health" in q.topics:
                        whole["health"] = health.snapshot()
                    send(f"event: full\ndata: {json.dumps(whole)}\n\n")
                if "phone" in q.topics and digest is not None:
                    send(f"event: phone\ndata: {json.dumps(digest)}\n\n")
            while True:
                try:
                    event, payload = q.get(timeout=config.EVENTS_KEEPALIVE)
                    frame = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    # Dropped for falling behind: close, so the client reconnects and gets the
                    # whole state again, rather than wait on a queue nobody fills.
                    if q.dropped:
                        break
                    frame = ": keepalive\n\n"
                send(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.drop(q)

    def browser_action(self, agent_id, action, body):
        """One step in an agent's browser. Only from this laptop: an agent's tools call it over
        loopback, and a browser on the tailnet has the live view for that, not this."""
        from . import browsers
        # Tailscale's HTTPS proxy also connects from loopback, and it says so in these headers.
        proxied = self.headers.get("X-Forwarded-For") or self.headers.get("Tailscale-User-Login")
        if proxied or not self.peer.startswith(("127.", "::1", "::ffff:127.")):
            return self.fail("browser actions come from an agent on the laptop only", 403)
        if not registry.get(agent_id):
            return self.fail("no such agent", 404)
        try:
            if action in ("login", "logins"):
                from . import vault
                return self.send_json(vault.login(agent_id, body.get("login")) if action == "login"
                                      else {"logins": vault.entries_for(agent_id)})
            return self.send_json(browsers.run(agent_id, action, body))
        except ValueError as exc:
            return self.fail(exc, 400)
        except RuntimeError as exc:
            return self.fail(exc, 409)

    def browser_socket(self, agent_id):
        """The live view of one agent's browser, and takeover. Frames go out as JSON with the JPEG
        in base64; taps, typing and keys come back, and count only while he has control."""
        from . import browsers
        if not is_upgrade(self.headers):
            return self.fail("expected a websocket", 400)
        if not registry.get(agent_id):
            return self.fail("no such agent", 404)
        sock = WebSocket.handshake(self)
        lock = threading.Lock()

        # Frames reach the phone from a thread of their own, newest only. The sink is called on
        # the thread that reads everything Chrome says, and sending an 80 KB frame down a slow
        # mobile link there held up every reply from that Chrome: the page froze for him and for
        # the agent alike. Now the reader only drops the frame in a slot and goes back to Chrome;
        # a frame the link had no time for is replaced by the next one.
        newest = {"frame": None, "done": False}
        ready = threading.Condition()

        def sink(data, meta):
            with ready:
                newest["frame"] = (data, meta)
                ready.notify()

        def pump():
            while True:
                with ready:
                    while newest["frame"] is None and not newest["done"]:
                        ready.wait(10)
                    if newest["done"] or sock.closed:
                        return
                    data, meta = newest["frame"]
                    newest["frame"] = None
                with lock:
                    sock.send_json({"t": "frame", "data": data, "w": meta.get("deviceWidth"),
                                    "h": meta.get("deviceHeight")})

        threading.Thread(target=pump, name=f"frames-{agent_id}", daemon=True).start()

        def told(msg):
            if not sock.closed:
                with lock:
                    sock.send_json(msg)

        # Saving a login he typed is a vault write, so it is taken only from the console over the
        # tailnet, the same rule as /api/vault: never from an agent on loopback.
        console = bool(self.headers.get("X-Forwarded-For") or self.headers.get("Tailscale-User-Login")) \
            or not self.peer.startswith(("127.", "::1", "::ffff:127."))

        try:
            b = browsers.watch(agent_id, sink)
            b.event_sinks.add(told)
            with lock:
                sock.send_json({"t": "state", "control": b.control})
        except Exception as exc:
            sock.send_json({"t": "error", "message": str(exc)})
            sock.close()
            return None
        try:
            while True:
                got = sock.recv()
                if got is None:
                    break
                try:
                    msg = json.loads(got[1].decode() if isinstance(got[1], bytes) else got[1])
                except Exception:
                    continue
                if msg.get("t") in ("fit", "unfit"):
                    try:
                        if msg.get("t") == "fit":
                            browsers.fit(agent_id, msg.get("w"), msg.get("h"), msg.get("mobile", True))
                        else:
                            b2 = browsers._browsers.get(agent_id)
                            if b2:
                                browsers.unfit(b2)
                    except Exception as exc:
                        with lock:
                            sock.send_json({"t": "error", "message": str(exc)})
                    continue
                if msg.get("t") == "save_login":
                    try:
                        if not console:
                            raise RuntimeError("logins are saved from the console only")
                        told({"t": "saved", **browsers.answer_offer(agent_id, bool(msg.get("yes")))})
                    except Exception as exc:
                        told({"t": "error", "message": f"The login was not saved: {exc}"})
                    continue
                if msg.get("t") == "control":
                    state = browsers.control(agent_id, msg.get("on"))
                    with lock:
                        sock.send_json({"t": "state", **state})
                else:
                    try:
                        browsers.human(agent_id, msg)
                    except Exception as exc:
                        with lock:
                            sock.send_json({"t": "error", "message": str(exc)})
        finally:
            with ready:
                newest["done"] = True
                ready.notify()
            b2 = browsers._browsers.get(agent_id)
            if b2:
                b2.event_sinks.discard(told)
            browsers.unwatch(agent_id, sink)
        return None

    def terminal_socket(self, agent_id, qs):
        """One browser watching one agent, 1:1. Both engines travel on this one socket.

        Everything the terminal produced goes out as binary frames, unchanged, escape sequences
        and all. Keystrokes come back the same way. Nothing is interpreted in between, which is
        the whole point: what the operator sees is what is on the terminal, including the parts
        that are only there to draw a box.

        The chat engine has no bytes to draw, so it travels as JSON text frames on the same
        socket: which mode is running, whether it is thinking, the message being written this
        instant, the tools it called, and the moment a turn is settled on disk. One socket rather
        than two because the browser already has this one open, and because a phone that loses
        its connection then reconnects has one thing to redo rather than two.
        """
        if not is_upgrade(self.headers):
            return self.fail("expected a websocket", 400)
        rec = registry.get(agent_id)
        if not rec:
            return self.fail("no such agent", 404)

        sock = WebSocket.handshake(self)

        def relay(frame):
            if not sock.closed:
                sock.send_json(frame)

        broker.listen(agent_id, relay)
        try:
            self.agent_socket(agent_id, sock, qs)
        finally:
            broker.unlisten(agent_id, relay)

    def agent_socket(self, agent_id, sock, qs):
        # What is true at this instant, sent before anything else. A phone that reconnects in the
        # middle of a turn has missed every frame so far, and a live delta carries the whole
        # reply rather than the next piece, so one frame puts it back where it was.
        engine = broker.chat(agent_id)
        sock.send_json({"t": "mode", "mode": broker.mode(agent_id)})
        sock.send_json({"t": "state", "state": engine.state if engine else "idle"})
        sock.send_json({"t": "perm_mode", "mode": broker.permission_mode(agent_id)})
        if engine and engine.live_text:
            sock.send_json({"t": "live", "phase": "delta", "text": engine.live_text})
        # Anything Claude is still waiting on, sent again. The ask frame went out once, when it
        # arrived, and a phone that locked its screen since then has nothing on it to answer. The
        # engine is still waiting, so the card has to come back.
        for frame in broker.pending_asks(agent_id):
            sock.send_json(frame)

        term = broker.get(agent_id)
        if not (term and term.alive()):
            # wake=1 is the terminal tab asking for a terminal. It is refused in chat mode: the
            # operator chose the chat engine, and starting a pseudo-terminal beside it would put
            # two claude processes on one conversation, which is the one thing that breaks the
            # session file they share. The chat engine starts on the first message instead.
            if qs.get("wake", ["0"])[0] == "1" and broker.mode(agent_id) != "chat":
                try:
                    term = broker.start(agent_id)
                    hub.publish("agents")
                except Exception as exc:
                    sock.send_json({"type": "error", "message": str(exc)})
                    term = None
            else:
                sock.send_json({"type": "grey", "agent": agent_id})

        sub = None
        if term and term.alive():
            rows = int(qs.get("rows", [0])[0] or 0) or None
            cols = int(qs.get("cols", [0])[0] or 0) or None
            sub, history = term.attach(rows, cols)

            sock.send_json({"type": "attached", "agent": agent_id, "pid": term.pid,
                            "rows": term.rows, "cols": term.cols, "history": len(history)})
            if history:
                sock.send_bytes(history)

            def pump():
                """Terminal to browser."""
                while not sock.closed and not sub.dead:
                    data = sub.drain(timeout=0.5)
                    if data and not sock.send_bytes(data):
                        break
                # The socket is not closed here, deliberately. It carries the chat engine too, and
                # the commonest way a terminal ends is the operator switching to chat, where
                # closing the socket would take the chat frames away at the moment they start.
                if not sock.closed:
                    sock.send_json({"type": "exited", "status": term.exit_status})

            threading.Thread(target=pump, name=f"ws-out-{agent_id}", daemon=True).start()

        try:
            while True:
                frame = sock.recv()
                if frame is None:
                    break
                opcode, data = frame
                # Keystrokes and terminal control are dropped when no terminal is running. In
                # chat mode there is nothing to type into: a chat message is an ordinary POST and
                # goes through the broker, which routes it by mode.
                if opcode == 0x2:                      # binary: keystrokes
                    if term:
                        term.write(data)
                else:                                   # text: control
                    try:
                        msg = json.loads(data)
                    except Exception:
                        continue
                    if not term:
                        continue
                    if msg.get("type") == "resize":
                        rows, cols = msg.get("rows"), msg.get("cols")
                        if rows and cols:
                            term.set_size(sub, int(rows), int(cols))
                    elif msg.get("type") == "input":
                        term.write(msg.get("data", ""))
                    elif msg.get("type") == "submit":
                        term.submit(msg.get("text", ""))
                    elif msg.get("type") == "interrupt":
                        term.interrupt()
        except Exception:
            pass
        finally:
            sock.closed = True
            if term and sub:
                term.detach(sub)

    # -- files ------------------------------------------------------------------

    def download(self, agent_id, name, box="outbox", small=False):
        """Send a file the agent offered, or one the owner sent it (box "inbox").

        The name is resolved against that box's listing, so a name that is not already a file in
        there matches nothing. Traversal is not refused by a check; it simply never resolves.
        """
        rec = registry.get(agent_id)
        if not rec:
            return self.fail("no such agent", 404)
        path = files.resolve(rec, name, box)
        if path is None:
            return self.fail("no such file", 404)
        if small:
            # The chat's thumbnail: small, and the same file every time, so the phone keeps it.
            thumb = files.thumbnail(path)
            if thumb:
                return self.send_text(thumb, ctype="image/jpeg",
                                      extra={"Cache-Control": "private, max-age=604800"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        try:
            body = path.read_bytes()
        except OSError as exc:
            return self.fail(exc, 500)
        # attachment, so a PDF or an HTML file downloads instead of rendering on the console's own
        # origin, where an agent-written page would sit next to the trusted one.
        return self.send_text(body, ctype=ctype, extra={
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "Cache-Control": "no-store"})

    def cache_rule(self, rel):
        """How long the browser may keep this file, decided by its name.

        Three answers. Vite puts a content hash in every name under assets/, so the bytes behind
        one of those names never change and the phone can keep them for a year; that is the point
        of the hash and it is what keeps a Tailscale reload cheap. index.html and sw.js name the
        hashed files, so they have to be checked on every load or a new build would never arrive.
        Everything else is the old frontend, which has no hashes, and it keeps the answer it has
        always had.
        """
        name = rel.rsplit("/", 1)[-1]
        if name in ("index.html", "sw.js"):
            return "no-cache"
        if rel.startswith(("assets/", "fonts/", "vendor/")):
            return "public, max-age=31536000, immutable"
        return "no-store"

    def static_file(self, rel, ctype=None):
        # Typefaces sit beside the code rather than inside web/, because healthd.py served them
        # first and moving them would break the page that has been working for a week. It is also
        # what lets a second frontend work at all: both builds ask for /fonts/inter-400.woff2 and
        # both get the same file, whichever folder the rest of the page came from.
        cache = self.cache_rule(rel)
        if rel.startswith("fonts/"):
            base, rel = config.FONTS, rel[len("fonts/"):]
        else:
            base = config.WEB
        base = base.resolve()
        target = (base / rel).resolve()
        # The check is on the resolved path, so a name that climbs out with .. lands outside the
        # folder and is refused here. is_relative_to compares whole path segments, which a string
        # prefix does not: /home/x/websomething starts with /home/x/web and is not inside it.
        if not target.is_relative_to(base):
            return self.fail("no", 403)
        if not target.is_file():
            return self.fail("not found", 404)
        suffix = target.suffix.lower()
        ctype = ctype or MEDIA.get(suffix) or mimetypes.guess_type(str(target))[0] \
            or "application/octet-stream"
        body = target.read_bytes()
        extra = {"Cache-Control": cache}
        if suffix in COMPRESSIBLE:
            # Vary, so a cache in the middle never hands a gzipped body to a client that did not
            # ask for one. mtime=0 keeps the same file compressing to the same bytes every time.
            extra["Vary"] = "Accept-Encoding"
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, mtime=0)
                extra["Content-Encoding"] = "gzip"
        return self.send_text(body, ctype=ctype, extra=extra)

    def frontend(self, path):
        """Whatever the API did not claim belongs to the page.

        A built React app owns its own addresses. The screen is a query today, ?screen=chat, but a
        client side path is one small change away and a deep link into it must survive a reload.
        So a path that does not name a file gets index.html, and the page works out what to show.

        A path that does name a file is that file or it is nothing. Answering index.html to a
        missing .js turns a broken build into a blank screen with no error in the log, and you
        lose an hour before you look at the network tab.
        """
        if path.startswith("/api/") or path.startswith("/ws/"):
            return self.fail("not found", 404)
        # Refused before the fallback rather than after it. static_file checks again, but the
        # fallback below never reaches static_file, so a traversal would otherwise be answered
        # with a cheerful 200 and the page, and nothing would say it had been tried.
        if ".." in path.split("/"):
            return self.fail("no", 403)
        rel = path.lstrip("/")
        if rel and "." in rel.rsplit("/", 1)[-1]:
            return self.static_file(rel)
        return self.static_file("index.html", "text/html; charset=utf-8")

    def agent_page(self, agent_id, slug):
        """One of an agent's own screens, wrapped in a page the platform owns.

        The wrapper is not decoration. It sets the policy below and provides the bridge, and if
        the agent wrote the whole page it would set its own policy and every rule would be a
        suggestion.

        The agent's module is put inside the wrapper rather than fetched by it. The frame is
        sandboxed without allow-same-origin, which gives it an origin that matches nothing, and
        a document with that kind of origin cannot load a module script from this host: module
        scripts need permission from the server they came from, and the word for "my own origin"
        in a policy matches nothing when the origin is opaque. Inlining sidesteps both, and
        keeps the rule that a view may not touch the network completely intact.
        """
        # Resolved through config.pages, never by joining the slug onto a path. A slug arrives from
        # the browser, and a path built from it is a path an attacker chooses.
        module = config.page_file(agent_id, slug)
        if module is None:
            pages = config.pages(agent_id)
            if slug == "view" and pages:
                # The bare address means "this agent's screen". Which file that is depends on what
                # the agent wrote, so it is the first page rather than a fixed name.
                module = config.VIEWS / agent_id / pages[0]["file"]
            elif pages or slug != "view":
                # An unknown page is a 404. Answering 200 with an empty frame would make a typo
                # look like an agent that had written nothing, which sends you looking in the
                # wrong place.
                return self.fail(f"no page {slug!r} for {agent_id}", 404)

        wrapper = config.view_wrapper().read_text().replace("/*FONTS*/", embedded_fonts())
        if module and module.exists():
            code = module.read_text()
            # The last line is what makes the PRD's contract work unchanged. An inline module
            # may declare exports but nothing can import it, so render is handed over through
            # the global instead. The agent still writes `export function render(...)`.
            block = ('<script type="module">\n' + code
                     + '\ntry{globalThis.__render=render}catch(e){globalThis.__render=null}\n'
                     + '</script>')
        else:
            block = ""
        page = wrapper.replace("<!--VIEW-->", block)

        # Sandboxing governs the document, not the network. Without this a view could still
        # reach any address in the world, which is the one thing a stranger's code must not do.
        csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
               "img-src data:; font-src data:; connect-src 'none'; form-action 'none'; "
               "base-uri 'none'; frame-ancestors 'self'")
        return self.send_text(page, ctype="text/html; charset=utf-8",
                              extra={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


_fonts_css = None

FACES = (
    ("Instrument Serif", 400, "instrument-serif-400.woff2"),
    ("Inter", 400, "inter-400.woff2"),
    ("Inter", 500, "inter-500.woff2"),
    ("JetBrains Mono", 400, "jetbrains-mono-400.woff2"),
)


def embedded_fonts():
    """The typefaces, written into the view wrapper as data.

    A view frame has an origin of its own that matches nothing, so it cannot ask this server for
    a font file: fonts always need the server's permission across origins, and there is no origin
    here to grant it to. The alternative would be opening the frame's network policy, which is
    the one rule worth keeping absolute.

    Putting the bytes in the page keeps the policy at connect-src 'none' and still lets a view
    look like the rest of the console, which DESIGN.md treats as load bearing rather than
    cosmetic: a view in a different typeface reads as bolted on. About ninety kilobytes, built
    once and held.
    """
    global _fonts_css
    if _fonts_css is None:
        out = []
        for family, weight, name in FACES:
            path = config.FONTS / name
            if not path.exists():
                continue
            b64 = base64.b64encode(path.read_bytes()).decode()
            out.append(f'@font-face{{font-family:"{family}";font-weight:{weight};'
                       f'font-display:swap;src:url(data:font/woff2;base64,{b64})format("woff2")}}')
        _fonts_css = "\n".join(out)
    return _fonts_css


def serve():
    config.ensure_dirs()
    mimetypes.add_type("font/woff2", ".woff2")

    broker.on_change(lambda event, payload: hub.publish("agents", payload))
    cords.on_change(lambda event, payload: hub.publish("cords", {"event": event}))
    # A job firing at 3am changes the schedule strip as much as a person editing it does. This
    # listener was defined in cron and never registered, so the strip only ever refreshed when
    # something else on the page did.
    cron.on_change(lambda *a: hub.publish("jobs"))
    # The inbox event carries its own summary, so the orb needs no second request.
    inbox.on_change(lambda event, payload: hub.publish("inbox", payload))
    # A task card that changed goes out whole, so an open console redraws it without asking.
    tasks.on_change(lambda event, payload: hub.publish("tasks", payload, topic="tasks"))
    from . import posts
    posts.on_change(lambda event, payload: hub.publish("posts", payload, topic="posts"))
    from . import mail
    mail.on_change(lambda: hub.publish("mail", {"what": "drafts"}, topic="mail"))
    mail.rearm()
    # An agent that fell asleep is free: a card waiting on that moment tells the owner.
    broker.on_change(lambda event, payload: tasks.free(payload.get("agent"), sure=True)
                     if event == "agent.asleep" and payload.get("agent") else None)
    threading.Thread(target=_health_loop, name="health", daemon=True).start()
    threading.Thread(target=feed.run, name="feed", daemon=True).start()
    # An alarm queued or done changes the phone's digest, and nothing else publishes it.
    from . import phone
    phone.on_change(feed.poke)
    # Only Ergo's own service asks for the phone topic, so a listener on it is the phone.
    phone.on_listening(lambda: hub.wants("phone"))
    calm.on_change(feed.poke)
    phone.start()
    from . import nags, owners
    owners.start()
    nags.start()
    # Quiet time starts or ends: the state changes, and its end needs a timer.
    quiet.on_change(feed.poke)
    quiet.arm()

    httpd = ThreadingHTTPServer((config.BIND, config.PORT), Handler)
    httpd.daemon_threads = True
    return httpd
