"""WhatsApp chats for the agents: the daemon's half of the bridge.

The bridge is a small Node service (whatsapp/bridge.mjs, the user unit ergo-whatsapp). It is a
linked device on the owner's personal number. It writes everything it knows into var/whatsapp, and
this module reads those files and writes the ones the owner controls:

    status.json        linked or not, since when, and the last error
    qr.txt             the raw pairing QR string, only while pairing
    chats-seen.json    every chat the bridge has seen: id, name, kind, the last message's time
                       and whether the owner wrote it (last_me). Never what was said
    chats.json         the allowlist, written here: chat id -> {name, agent, side}
    messages/<id>.jsonl  the followed chats, one line per message, kept 30 days by the bridge
    settings.json      {"sending": false}. Off unless the owner turns it on in Config
    drafts.json        replies the agents drafted, waiting for the owner to send or discard
    sent.json          the times of recent sends, for the hourly limit. Never what was sent
    bridge.json        the bridge's loopback port and the token the daemon sends with

Each followed chat goes to one agent, and every agent may read every routed chat. The owner routes
a chat from Config, and an agent may also follow a chat for itself with whatsapp_follow, and
unfollow one it follows. It cannot take a chat another agent follows. The route still stores a
side, and it is always the one side there is.

Sending exists but is off by default, by the owner's choice: better to have it and not need it.
Agents never send. An agent drafts a reply for one of its own chats, and the owner taps Send on
Config. A send is console only, needs the switch on at that moment, needs the chat in the
allowlist, and is limited to 20 an hour across all chats. The bridge checks the switch and the
allowlist again itself before it sends.

Nothing here logs a chat name or a message. Message text lives only in the messages folder and
in a draft until it is sent or discarded.
"""

import json
import os
import re
import threading
import time
import shutil
from pathlib import Path

from . import config, logs, registry, untrusted

log = logs.get(__name__)

_lock = threading.Lock()

# About a day of a busy group, and still small enough to read in one go.
MAX_LINES = 200
KEEP_DAYS = 30

# WhatsApp bans numbers that send like a bot. Twenty an hour is far above what a person taps.
SENDS_PER_HOUR = 20
MAX_TEXT = 4000
# A photo or file sent with a draft: from the agent's outbox, at most this big (WhatsApp's own
# limit for a document is larger; this keeps a send quick on a home line).
MAX_FILE_MB = 50
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _dir():
    return config.VAR / "whatsapp"


def safe_id(chat):
    """A chat id as a file name. The same rule as safeId() in whatsapp/bridge.mjs."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(chat))


def _read(name, fallback):
    try:
        return json.loads((_dir() / name).read_text())
    except (FileNotFoundError, ValueError):
        return fallback


def _write_allowlist(allow):
    # The bridge reads this file to decide what to keep, so write then rename: it never reads half.
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    tmp = d / "chats.json.tmp"
    tmp.write_text(json.dumps(allow, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(d / "chats.json")


def allowlist():
    got = _read("chats.json", {})
    return got if isinstance(got, dict) else {}


# -- what Config shows -------------------------------------------------------------

def status():
    """Linked or not, and the QR string while pairing. The QR is only shown when not linked."""
    st = _read("status.json", {})
    linked = bool(st.get("linked"))
    qr = None
    if not linked:
        try:
            qr = (_dir() / "qr.txt").read_text().strip() or None
        except FileNotFoundError:
            pass
    return {"linked": linked, "since": st.get("since") if linked else None, "qr": qr,
            "qr_at": st.get("qr_at") if qr else None, "error": st.get("error")}


def chats():
    """The chats seen, newest first, each with the agent it goes to, and the agents to pick from."""
    allow = allowlist()
    seen = _read("chats-seen.json", [])
    rows, ids = [], set()
    for c in seen if isinstance(seen, list) else []:
        cid = c.get("id")
        if not cid or cid in ids:
            continue
        ids.add(cid)
        route = allow.get(cid) or {}
        rows.append({"id": cid, "name": c.get("name") or route.get("name"), "kind": c.get("kind"),
                     "last_at": c.get("last_at"), "agent": route.get("agent")})
    # A followed chat stays on the list even if the bridge forgot it, so it can be unfollowed.
    for cid, route in allow.items():
        if cid not in ids:
            rows.append({"id": cid, "name": route.get("name"),
                         "kind": "group" if cid.endswith("@g.us") else "dm",
                         "last_at": None, "agent": route.get("agent")})
    rows.sort(key=lambda r: (r["agent"] is None, -(r["last_at"] or 0), (r["name"] or "").lower()))
    agents = [{"id": a["id"], "name": a.get("name") or a["id"]} for a in registry.all()]
    agents.sort(key=lambda a: a["name"].lower())
    return {"chats": rows, "agents": agents}


def route(body):
    """Follow a chat for an agent, change its agent, or stop following it (agent null)."""
    from . import vault
    cid = str((body or {}).get("id") or "").strip()
    if not cid or len(cid) > 200 or not re.match(r"^[^/\\\s]+@[a-z.]+$", cid):
        raise ValueError("give the chat id")
    agent = (body or {}).get("agent")
    with _lock:
        allow = allowlist()
        if not agent:
            if allow.pop(cid, None) is not None:
                _write_allowlist(allow)
                # Not followed means not kept. What was stored for this chat goes with it.
                try:
                    (_dir() / "messages" / f"{safe_id(cid)}.jsonl").unlink()
                except FileNotFoundError:
                    pass
                left = [d for d in _drafts() if d.get("chat") != cid]
                if len(left) != len(_drafts()):
                    _write("drafts.json", left)
                log.info("whatsapp: a chat was unfollowed")
            return chats()
        rec = registry.get(str(agent))
        if not rec:
            raise ValueError("no such agent")
        seen = {c.get("id"): c for c in (_read("chats-seen.json", []) or []) if isinstance(c, dict)}
        name = (seen.get(cid) or {}).get("name") or (allow.get(cid) or {}).get("name")
        allow[cid] = {"name": name, "agent": rec["id"], "side": vault.side_of(rec)}
        _write_allowlist(allow)
        log.info("whatsapp: a chat now goes to %s (%s)", rec["id"], allow[cid]["side"])
    return chats()


# -- an agent follows a chat itself -----------------------------------------------------

def _chat_id(chat):
    """A chat id from what the agent gave: an id as it is, or a phone number as a direct chat.
    A ten digit number is taken as Indian."""
    chat = str(chat or "").strip()
    if "@" in chat:
        return chat
    digits = re.sub(r"\D", "", chat)
    if len(digits) == 10:
        digits = "91" + digits
    return digits + "@s.whatsapp.net" if 11 <= len(digits) <= 15 and chat.lstrip("+ ").replace(" ", "").replace("-", "").isdigit() else ""


def last_from(me):
    """Who wrote a chat's newest message: "The owner", "them", or None when it is not known."""
    return None if me is None else ("The owner" if me else "them")


def find(name):
    """The chats the bridge has seen whose name holds these words, with no messages: the id, the
    kind, when the last message came, and which agent follows it. A chat with no message since
    the bridge was linked has no time."""
    words = [w for w in str(name or "").lower().split() if w]
    if not words:
        raise ValueError("give a name to look for")
    allow = allowlist()
    out = []
    for c in _read("chats-seen.json", []) or []:
        n = (c.get("name") or "").lower()
        if c.get("id") and all(w in n for w in words):
            route = allow.get(c["id"]) or {}
            out.append({"id": c["id"], "name": c.get("name"), "kind": c.get("kind"),
                        "last": _when(c["last_at"]) if c.get("last_at") else None,
                        "last_from": last_from(c.get("last_me")) if c.get("last_at") else None,
                        "followed_by": route.get("agent")})
    out.sort(key=lambda r: r["name"].lower())
    got = {"chats": out[:20]}
    if not out:
        got["note"] = ("No chat by that name has been seen. A contact who has not written since "
                       "the bridge was linked is not on the list: follow them by phone number.")
    return got


def follow(agent_id, chat, on=True):
    """An agent follows a chat for itself, or stops following one of its own. A chat another agent
    follows is not taken from it, and only the owner unfollows a chat they routed to someone else. Messages are kept from the moment of the follow: WhatsApp gives
    no history to a quiet linked device."""
    rec = registry.get(str(agent_id or ""))
    if not rec:
        raise ValueError("no such agent")
    cid = _chat_id(chat)
    if not cid:
        raise ValueError("give the chat id from whatsapp_find, or a phone number")
    held = allowlist().get(cid) or {}
    if on:
        if held.get("agent") and held["agent"] != rec["id"]:
            raise ValueError(f"{held['agent']} already follows this chat; you can read it with "
                             "whatsapp_read")
        route({"id": cid, "agent": rec["id"]})
    else:
        if held.get("agent") != rec["id"]:
            raise ValueError("you do not follow this chat")
        route({"id": cid, "agent": None})
    log.info("whatsapp: %s %s a chat", rec["id"], "followed" if on else "unfollowed")
    return {"id": cid, "following": bool(on), "name": (allowlist().get(cid) or {}).get("name") or held.get("name")}


# -- what an agent reads -------------------------------------------------------------

def _when(at):
    return time.strftime("%a %d %b %H:%M", time.localtime(at))


def _photo_for(rec, media):
    """A photo or a file from a followed chat, copied into this agent's own inbox so it can open it.

    The bridge's file is named by the bridge, and only a file inside the media folder is taken, so
    a line cannot point an agent at anything else on the laptop. Copied once; the path is returned.
    """
    if not media:
        return None
    from . import files
    folder = (_dir() / "media").resolve()
    try:
        src = Path(media).resolve()
    except (OSError, ValueError):
        return None
    if src.parent != folder or not src.is_file():
        return None
    dest_dir = files.inbox(rec) / "whatsapp"
    dest = dest_dir / src.name
    if not dest.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    return str(dest)


def kind_of(cid):
    """A group or a person, from the chat's id: groups end in @g.us. A person is @s.whatsapp.net
    by phone number, or @lid, WhatsApp's hidden id for someone whose number is not shown."""
    return "group" if str(cid).endswith("@g.us") else "person"


def labels(side_chats):
    """Each chat's name as agents see it. Two chats with one name, such as a group and the
    business account that posts in it, get their kind in brackets: "Northwind (group)"."""
    count = {}
    for cid, r in side_chats.items():
        low = (r.get("name") or cid).lower()
        count[low] = count.get(low, 0) + 1
    out = {}
    for cid, r in side_chats.items():
        name = r.get("name") or cid
        out[cid] = f"{name} ({kind_of(cid)})" if count[name.lower()] > 1 else name
    return out


def pick(rec, side_chats, chats=None):
    """Which of the routed chats a read covers, and the names asked for that matched none.

    The agent decides what it reads, from its own instructions and the brief of the job that
    woke it; the daemon only serves it. No names is every routed chat.
    Names, one or several, comma separated, by name, id or a part of a name, are those chats."""
    asked = chats if isinstance(chats, list) else str(chats or "").split(",")
    asked = [str(a).strip() for a in asked if str(a).strip()]
    if not asked:
        return dict(side_chats), []
    got, missing = {}, []
    label = labels(side_chats)
    for a in asked:
        low = a.lower()
        hit = [cid for cid in side_chats if cid == a or label[cid].lower() == low]
        if not hit:
            # The plain name of chats that share it reads them all.
            hit = [cid for cid, r in side_chats.items() if (r.get("name") or "").lower() == low]
        if not hit:
            # A part of a name works too, when it points at one chat only.
            hit = [cid for cid, r in side_chats.items() if low in (r.get("name") or "").lower()]
            if len(hit) > 1:
                missing.append(f"{a} (matches {len(hit)} chats, use the full name)")
                continue
        if hit:
            # Two chats can share a name, such as a group and its community: a name reads both.
            for cid in hit:
                got[cid] = side_chats[cid]
        else:
            missing.append(a)
    return got, missing


def list_chats(agent_id, since_hours=24):
    """Every routed chat, with no messages: its name, when its last message came and who wrote it
    (the owner or them), and how many came in the last since_hours. Busiest news first, so an
    agent can read only the chats that moved."""
    from . import vault
    rec = registry.get(str(agent_id or ""))
    if not rec:
        raise ValueError("no such agent")
    try:
        hours = float(since_hours)
    except (TypeError, ValueError):
        raise ValueError("since_hours is a number")
    hours = max(0.1, min(hours, KEEP_DAYS * 24))
    side = vault.side_of(rec)
    cutoff = time.time() - hours * 3600
    rows = []
    side_chats = dict(allowlist())
    label = labels(side_chats)
    # The bridge notes every chat's newest message, followed or not, so the last time and who
    # wrote it are known even from before the follow, back to when the bridge was linked.
    seen = {c.get("id"): c for c in _read("chats-seen.json", []) or [] if isinstance(c, dict)}
    for cid, r in side_chats.items():
        s = seen.get(cid) or {}
        last, me, new = s.get("last_at") or 0, s.get("last_me"), 0
        try:
            raw = (_dir() / "messages" / f"{safe_id(cid)}.jsonl").read_text()
        except FileNotFoundError:
            raw = ""
        for row in raw.splitlines():
            try:
                m = json.loads(row)
            except ValueError:
                continue
            at = m.get("at") or 0
            if at >= last:
                last, me = at, m.get("me", m.get("from") == "The owner")
            new += at >= cutoff
        rows.append({"name": label[cid], "kind": kind_of(cid), "id": cid, "new": new,
                     "last": _when(last) if last else None,
                     "last_from": last_from(me) if last else None, "_at": last})
    rows.sort(key=lambda x: (-x["new"], -x["_at"], x["name"].lower()))
    for x in rows:
        del x["_at"]
    out = {"side": side, "since_hours": hours, "chats": rows}
    if not rows:
        out["note"] = "The owner has not routed any WhatsApp chats yet."
    return out


def sender(m):
    """Who wrote a line. The bridge marks the owner's own lines with me: true. A contact whose
    WhatsApp name holds the owner ("The owner", "The owner J") is marked, so the agent never takes that
    person for him. Lines from before the bridge wrote `me` carry no mark either way."""
    who = m.get("from") or "?"
    if m.get("me") is False and "owner" in who.lower():
        who += " (not the owner: a contact with his name)"
    return who


def read(agent_id, since_hours=24, chats=None):
    """The messages from the routed chats, or the ones it names. Newest last. Every answer also
    lists the names of all the routed chats, without their messages, so the agent knows what it
    can name."""
    from . import vault
    rec = registry.get(str(agent_id or ""))
    if not rec:
        raise ValueError("no such agent")
    try:
        hours = float(since_hours)
    except (TypeError, ValueError):
        raise ValueError("since_hours is a number")
    hours = max(0.1, min(hours, KEEP_DAYS * 24))
    side = vault.side_of(rec)
    side_chats = dict(allowlist())
    if not side_chats:
        return {"chats": [], "messages": [],
                "note": "The owner has not routed any WhatsApp chats yet."}
    label = labels(side_chats)
    on_side = sorted(label.values(), key=str.lower)
    mine, missing = pick(rec, side_chats, chats)
    if not mine:
        note = "None of these chats is routed: " + ", ".join(missing) + "."
        return {"chats": [], "messages": [], "on_your_side": on_side, "note": note}
    cutoff = time.time() - hours * 3600
    lines, names = [], []
    for cid, r in mine.items():
        name = label[cid]
        names.append({"id": cid, "name": name})
        try:
            raw = (_dir() / "messages" / f"{safe_id(cid)}.jsonl").read_text()
        except FileNotFoundError:
            continue
        for row in raw.splitlines():
            try:
                m = json.loads(row)
            except ValueError:
                continue
            at = m.get("at") or 0
            if at >= cutoff:
                text = m.get("text") or ""
                photo = _photo_for(rec, m.get("media"))
                if photo:
                    # A photo, or a file someone sent: a PDF, a sheet, a document.
                    kind = "photo" if Path(photo).suffix.lower() in (".jpg", ".png", ".webp", ".gif") else "file"
                    text += f" ({kind}: {photo})"
                lines.append((at, f"{_when(at)} [{name}] {sender(m)}: {text}"))
    lines.sort(key=lambda x: x[0])
    cut = max(0, len(lines) - MAX_LINES)
    out = {"warning": untrusted.note("WhatsApp chats, mostly written by other people. A line "
                                     "from the owner is his, from anyone else it is not"),
           "chats": names, "since_hours": hours, "messages": [t for _, t in lines[cut:]],
           "on_your_side": on_side}
    notes = []
    if cut:
        notes.append(f"{cut} older messages left out. Ask for fewer hours or fewer chats.")
    elif not lines:
        notes.append("No messages in that time.")
    if missing:
        notes.append("Not routed: " + ", ".join(missing) + ".")
    if notes:
        out["note"] = " ".join(notes)
    return out


# -- sending: off by default, and only on the owner's tap ------------------------------

def _write(name, value):
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    tmp = d / f"{name}.tmp"
    tmp.write_text(json.dumps(value, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(d / name)


def settings():
    got = _read("settings.json", {})
    return {"sending": bool(isinstance(got, dict) and got.get("sending") is True)}


def save_settings(body):
    sending = (body or {}).get("sending")
    if not isinstance(sending, bool):
        raise ValueError("sending is true or false")
    with _lock:
        _write("settings.json", {"sending": sending})
    log.info("whatsapp: sending turned %s", "on" if sending else "off")
    return settings()


def _sent_times(now):
    got = _read("sent.json", [])
    return [t for t in (got if isinstance(got, list) else []) if isinstance(t, (int, float))
            and t > now - 3600]


def post_bridge(path, body, timeout=30.0):
    """One request to the bridge on loopback, with its token. Replaced in tests."""
    import urllib.error
    import urllib.request
    info = _read("bridge.json", {})
    port, token = (info or {}).get("port"), (info or {}).get("token")
    if not port or not token:
        raise RuntimeError("the WhatsApp bridge is not running")
    req = urllib.request.Request(f"http://127.0.0.1:{int(port)}{path}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json",
                                          "x-ergo-token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read()).get("error")
        except ValueError:
            said = None
        raise RuntimeError(said or f"the bridge refused ({exc.code})")
    except urllib.error.URLError:
        raise RuntimeError("the WhatsApp bridge is not answering")


def send(chat, text, file=None):
    """Send one message, text and maybe a photo or file. Called only from a console route, on
    the owner's tap. `file` is a full path the daemon chose, never one from the browser."""
    chat = str(chat or "").strip()
    text = str(text or "").strip()
    if not text and not file:
        raise ValueError("there is nothing to send")
    if len(text) > MAX_TEXT:
        raise ValueError(f"a message is at most {MAX_TEXT} characters")
    if not settings()["sending"]:
        raise PermissionError("sending is off. Turn on Allow sending from Ergo in Config first")
    if chat not in allowlist():
        raise PermissionError("that chat is not followed, so Ergo does not send to it")
    with _lock:
        now = time.time()
        times = _sent_times(now)
        if len(times) >= SENDS_PER_HOUR:
            raise PermissionError(f"{SENDS_PER_HOUR} messages sent in the last hour. Wait a while")
        # Counted before it goes, so two taps at once cannot both slip under the limit.
        _write("sent.json", times + [now])
    body = {"chat": chat, "text": text}
    if file:
        # The bridge takes a file only from its own outgoing folder, so a copy goes there first.
        out = _dir() / "outgoing"
        out.mkdir(parents=True, exist_ok=True)
        copy = out / f"{int(time.time() * 1000)}{Path(file).suffix.lower()}"
        shutil.copyfile(file, copy)
        body.update(file=str(copy), file_name=Path(file).name)
    try:
        got = post_bridge("/send", body, timeout=120.0 if file else 30.0)
    finally:
        if file:
            copy.unlink(missing_ok=True)
    log.info("whatsapp: one message sent%s", " with a file" if file else "")
    return {"ok": True, "id": got.get("id")}


# -- drafts: what an agent would say, for the owner to send --------------------------

def _drafts():
    got = _read("drafts.json", [])
    return got if isinstance(got, list) else []


def drafts():
    names = {a["id"]: a.get("name") or a["id"] for a in registry.all(include_hidden=True)}
    allow = allowlist()
    out = []
    for d in _drafts():
        out.append(dict(d, agent_name=names.get(d.get("agent"), d.get("agent")),
                        chat_name=(allow.get(d.get("chat")) or {}).get("name") or d.get("chat_name")
                        or d.get("chat")))
    return {"drafts": out, "sending": settings()["sending"]}


def _outbox_file(rec, name):
    """A file in the agent's own outbox, by name, checked. Nothing outside the outbox is taken."""
    from . import files
    box = files.outbox(rec).resolve()
    path = (box / str(name or "").strip()).resolve()
    if path.parent != box or not path.is_file():
        raise ValueError(f"no file called {name!r} in your outbox. Save it there first")
    if path.stat().st_size > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"the file is over {MAX_FILE_MB} MB")
    return path


def draft(agent_id, chat, text, file=None):
    """An agent's reply for a routed chat, maybe with a photo or file from its outbox. Stored
    for the owner; nothing is sent."""
    from . import vault
    rec = registry.get(str(agent_id or ""))
    if not rec:
        raise ValueError("no such agent")
    text = str(text or "").strip()
    attached = _outbox_file(rec, file) if file else None
    if not text and not attached:
        raise ValueError("the draft is empty")
    if len(text) > MAX_TEXT:
        raise ValueError(f"a draft is at most {MAX_TEXT} characters")
    want = str(chat or "").strip()
    side = vault.side_of(rec)
    mine = dict(allowlist())
    # The agent may name the chat by id or by the name whatsapp_read showed it.
    found = want if want in mine else next(
        (cid for cid, r in mine.items() if (r.get("name") or "").lower() == want.lower()), None)
    if not found:
        raise PermissionError("you can draft only for the WhatsApp chats the owner routed")
    item = {"id": f"d{int(time.time() * 1000)}", "agent": rec["id"], "chat": found,
            "chat_name": mine[found].get("name"), "text": text, "at": int(time.time())}
    if attached:
        item["file"] = attached.name
        item["photo"] = attached.suffix.lower() in PHOTO_EXT
    with _lock:
        _write("drafts.json", _drafts() + [item])
    log.info("whatsapp: %s drafted a reply", rec["id"])
    return item


def _take(draft_id):
    with _lock:
        rows = _drafts()
        hit = next((d for d in rows if d.get("id") == draft_id), None)
        if not hit:
            raise KeyError("no such draft")
        _write("drafts.json", [d for d in rows if d.get("id") != draft_id])
    return hit


def send_draft(draft_id, text=None):
    """Send a draft, as the owner edited it, then drop it. A refused send keeps the draft."""
    hit = next((d for d in _drafts() if d.get("id") == draft_id), None)
    if not hit:
        raise KeyError("no such draft")
    attached = None
    if hit.get("file"):
        rec = registry.get(hit.get("agent") or "")
        if not rec:
            raise ValueError("the agent that drafted this is gone, and its file with it")
        attached = _outbox_file(rec, hit["file"])
    got = send(hit["chat"], text if text is not None else hit["text"], attached)
    _take(draft_id)
    return got


def discard_draft(draft_id):
    hit = _take(draft_id)
    # Logged like a send, so a draft that went missing can be traced.
    log.info("whatsapp: a draft by %s was discarded", hit.get("agent"))
    return {"ok": True}


def reset():
    """Nothing is held in memory. Here for the test harness's habit."""


# -- the bridge going deaf -------------------------------------------------------------
# 15 October: after a reconnect the bridge stayed linked and heard nothing, and an @ergo was
# missed. It now restarts itself when that happens, and the owner hears of it each time, because a
# deaf bridge is a missed message. The third time in a day says the fix is not holding.

_deaf = {"day": None, "count": 0}


def went_deaf():
    from . import telegram
    today = time.strftime("%Y-%m-%d")
    if _deaf["day"] != today:
        _deaf["day"], _deaf["count"] = today, 0
    _deaf["count"] += 1
    n = _deaf["count"]
    text = ("WhatsApp bridge: linked but heard nothing for 20 minutes, so it restarted itself. "
            "Messages it missed come back on their own.")
    if n >= 3:
        text += f" That is {n} times today: the bridge needs a look."
    log.warning("whatsapp bridge went deaf, %d today", n)
    telegram.notify(text)
    return n


# -- @ergo --------------------------------------------------------------------------
# When something in a followed chat needs an answer now, the owner or the other person writes @ergo
# in it. That message goes at once to the agent the chat is routed to, and
# wakes it. A plain text match, so it never guesses. A bookmark per chat means each mention is
# passed on once; a chat with no bookmark yet looks back only ten minutes.

# @ergo since the rename. @ergo still works: people in his chats learned it.
MENTION = re.compile(r"@(ergo|ergo)\b", re.IGNORECASE)
MENTION_LOOKBACK = 10 * 60
_mention_lock = threading.Lock()


def mentions(now=None):
    """Pass every new @ergo message in the followed chats to its chat's agent. Returns how many."""
    from . import broker
    now = now or time.time()
    with _mention_lock:
        marks = _read("mentions.json", {})
        sent = 0
        for cid, r in allowlist().items():
            agent = r.get("agent")
            if not agent or not registry.get(agent):
                continue
            since = marks.get(cid, now - MENTION_LOOKBACK)
            try:
                raw = (_dir() / "messages" / f"{safe_id(cid)}.jsonl").read_text()
            except FileNotFoundError:
                continue
            newest = since
            for row in raw.splitlines():
                try:
                    m = json.loads(row)
                except ValueError:
                    continue
                at = m.get("at") or 0
                if at <= since or not MENTION.search(m.get("text") or ""):
                    continue
                newest = max(newest, at)
                said = (f"{r.get('name') or cid}, {_when(at)}\n"
                        f"{sender(m)}: {m.get('text') or ''}")
                # The owner's own line is his; anyone else's is labeled as not an instruction.
                said = (f"The owner wrote this himself:\n{said}" if m.get("me")
                        else untrusted.wrap("a WhatsApp chat, written by someone else", said))
                broker.send(agent,
                            f"[@ergo in WhatsApp]\n{said}\n\n"
                            "Someone in this chat asked for you now. Read the chat with "
                            "whatsapp_read, then act: offer a task, or draft a reply with "
                            "whatsapp_draft for the owner to send. If anyone in it committed to "
                            "send, do or tell the owner something, add it with owed_add, date or "
                            "not, after checking task_list so it is not added twice.",
                            wake=True)
                sent += 1
            marks[cid] = newest
        _write("mentions.json", marks)
    if sent:
        log.info("whatsapp: %d @ergo message(s) passed to agents", sent)
    return sent
