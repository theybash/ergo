"""Email drafts: what an agent would send, for the owner to send with a tap.

An agent drafts with email_draft: to, subject, body, an optional cc, files from its own outbox to
attach, and the time it would send it. Nothing is sent. The card shows in Insights, in the
agent's chat and in the feed, and only the owner's tap sends:

    Send          "Are you sure?", then it waits UNDO_SECONDS so the owner can take it back, then
                  it goes
    the clock     one tap: at the time the agent suggested; a long press: a time the owner picks.
                  The same "Are you sure?" first. The daemon holds it and sends it at its time

The mail goes out through Gmail from PLATFORM_MAIL_FROM, with the OAuth token in the file
PLATFORM_GMAIL_TOKEN names (default var/mail/gmail-token.json: client_id, client_secret,
refresh_token, and optionally token_uri). The token needs the gmail.compose scope. It is read and
refreshed in memory; the file is never written here.

A queued mail survives a restart: serve() calls rearm(). One whose time passed while the laptop
was off still goes, unless it is more than LATE_SECONDS late; then it is a draft again, marked as
missed, because a mail sent hours after its moment may be worse than none.

Nothing here logs a body. The log has the draft id, the agent and the outcome.
"""

import base64
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from . import config, logs, registry

log = logs.get(__name__)

FROM = os.environ.get("PLATFORM_MAIL_FROM", "").strip()
TOKEN = os.environ.get("PLATFORM_GMAIL_TOKEN", "").strip()
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

UNDO_SECONDS = 60
LATE_SECONDS = 6 * 3600
MAX_BODY = 20000
MAX_ATTACH = 20 * 1024 * 1024
ADDRESS = re.compile(r"^[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+$")

_lock = threading.Lock()
_timers = {}
_listeners = []
_access = {"token": None, "until": 0}


def _file():
    return Path(config.VAR) / "mail" / "drafts.json"


def _rows():
    try:
        got = json.loads(_file().read_text())
        return got if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def _save(rows):
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1))
    tmp.replace(path)


def on_change(fn):
    _listeners.append(fn)


def _emit():
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            log.exception("mail: a listener failed")


def _addresses(value):
    items = value if isinstance(value, list) else str(value or "").replace(";", ",").split(",")
    out = [str(a).strip() for a in items if str(a).strip()]
    bad = [a for a in out if not ADDRESS.match(a)]
    if bad:
        raise ValueError(f"not an email address: {bad[0]}")
    return out


def _outbox(rec):
    from . import files
    return files.outbox(rec)


def draft(agent_id, to, subject, body, cc=None, attach=None, send_at=None):
    """An agent's email for the owner to send. Stored; nothing is sent."""
    rec = registry.get(str(agent_id or ""))
    if not rec:
        raise ValueError("no such agent")
    to = _addresses(to)
    if not to:
        raise ValueError("say who it is to")
    cc = _addresses(cc)
    body = str(body or "").strip()
    if not body:
        raise ValueError("the email is empty")
    if len(body) > MAX_BODY:
        raise ValueError(f"an email is at most {MAX_BODY} characters")
    names = attach if isinstance(attach, list) else ([attach] if attach else [])
    files, total = [], 0
    for name in names:
        name = str(name or "").strip()
        path = _outbox(rec) / name
        if not name or "/" in name or "\\" in name or not path.is_file():
            raise ValueError(f"no file called {name or '(none)'} in your outbox to attach")
        total += path.stat().st_size
        files.append(name)
    if total > MAX_ATTACH:
        raise ValueError("the attachments are over 20 MB together")
    suggest = None
    if send_at not in (None, ""):
        suggest = _when(send_at)
    item = {"id": f"m{int(time.time() * 1000)}", "agent": rec["id"], "to": to, "cc": cc,
            "subject": str(subject or "").strip()[:300], "body": body, "attach": files,
            "suggest_at": suggest, "state": "draft", "send_at": None, "at": int(time.time())}
    with _lock:
        _save(_rows() + [item])
    log.info("mail: %s drafted %s", rec["id"], item["id"])
    _emit()
    return item


def _when(value):
    """A time as a number of seconds, or local "YYYY-MM-DD HH:MM"."""
    if isinstance(value, (int, float)):
        return int(value)
    import datetime as _dt
    try:
        return int(_dt.datetime.strptime(" ".join(str(value).split()), "%Y-%m-%d %H:%M").timestamp())
    except ValueError:
        raise ValueError("give the time as YYYY-MM-DD HH:MM, the owner's local time")


def listing():
    names = {a["id"]: a.get("name") or a["id"] for a in registry.all(include_hidden=True)}
    return {"drafts": [dict(d, agent_name=names.get(d.get("agent"), d.get("agent")))
                       for d in _rows()]}


def _edit(row, edits):
    edits = edits or {}
    if "to" in edits:
        row["to"] = _addresses(edits["to"]) or row["to"]
    if "cc" in edits:
        row["cc"] = _addresses(edits["cc"])
    if "subject" in edits:
        row["subject"] = str(edits["subject"] or "").strip()[:300]
    if "body" in edits and str(edits["body"] or "").strip():
        row["body"] = str(edits["body"]).strip()[:MAX_BODY]


def schedule(draft_id, when=None, edits=None):
    """The owner's tap. when None is Send: now plus the undo time. Otherwise a moment they confirmed."""
    now = time.time()
    at = int(now + UNDO_SECONDS) if when in (None, "") else _when(when)
    if at < now + UNDO_SECONDS:
        at = int(now + UNDO_SECONDS)
    with _lock:
        rows = _rows()
        row = next((r for r in rows if r.get("id") == draft_id), None)
        if not row:
            raise KeyError("no such draft")
        _edit(row, edits)
        row.update(state="queued", send_at=at, error=None)
        _save(rows)
    _arm(draft_id, at)
    log.info("mail: %s queued for %s", draft_id, time.strftime("%H:%M", time.localtime(at)))
    _emit()
    return dict(row)


def cancel(draft_id):
    """Undo, or take a scheduled mail back. It is a draft again."""
    with _lock:
        rows = _rows()
        row = next((r for r in rows if r.get("id") == draft_id), None)
        if not row:
            raise KeyError("no such draft")
        if row.get("state") == "sending":
            raise ValueError("it is already going out")
        row.update(state="draft", send_at=None)
        _save(rows)
    t = _timers.pop(draft_id, None)
    if t:
        t.cancel()
    _emit()
    return dict(row)


def discard(draft_id):
    with _lock:
        rows = _rows()
        if not any(r.get("id") == draft_id for r in rows):
            raise KeyError("no such draft")
        _save([r for r in rows if r.get("id") != draft_id])
    t = _timers.pop(draft_id, None)
    if t:
        t.cancel()
    _emit()
    return {"ok": True}


def _arm(draft_id, at):
    old = _timers.pop(draft_id, None)
    if old:
        old.cancel()
    t = threading.Timer(max(0.0, at - time.time()), _fire, args=(draft_id,))
    t.daemon = True
    _timers[draft_id] = t
    t.start()


def rearm():
    """At start: every queued mail gets its timer back. One far past its time is a draft again."""
    now = time.time()
    late = []
    with _lock:
        rows = _rows()
        for r in rows:
            if r.get("state") in ("queued", "sending"):
                if now - (r.get("send_at") or now) > LATE_SECONDS:
                    r.update(state="draft", send_at=None,
                             error="It missed its time while the laptop was off. Send it again.")
                    late.append(r["id"])
                else:
                    r["state"] = "queued"
        _save(rows)
    for r in rows:
        if r.get("state") == "queued":
            _arm(r["id"], r.get("send_at") or now)
    if late:
        log.info("mail: %d queued mails missed their time", len(late))
        _emit()


def _fire(draft_id):
    with _lock:
        rows = _rows()
        row = next((r for r in rows if r.get("id") == draft_id), None)
        if not row or row.get("state") != "queued" or (row.get("send_at") or 0) > time.time() + 1:
            return
        row["state"] = "sending"
        _save(rows)
    _timers.pop(draft_id, None)
    try:
        sent = _send(row)
    except Exception as exc:
        log.warning("mail: %s did not go: %s", draft_id, exc)
        with _lock:
            rows = _rows()
            for r in rows:
                if r.get("id") == draft_id:
                    r.update(state="draft", send_at=None, error=f"Not sent: {exc}")
            _save(rows)
        _emit()
        return
    with _lock:
        _save([r for r in _rows() if r.get("id") != draft_id])
    log.info("mail: %s sent for %s", draft_id, row["agent"])
    _emit()
    _tell(row, sent)


def _tell(row, sent):
    """The agent hears it went, so a job agent knows the application is in."""
    from . import broker
    try:
        broker.send(row["agent"], f"[Email sent] the owner sent your draft to {', '.join(row['to'])}: "
                                  f"\"{row['subject']}\". Gmail id {sent.get('id', '?')}.",
                    wake=False)
    except Exception:
        log.exception("mail: could not tell %s", row["agent"])


def _message(row):
    msg = EmailMessage()
    if FROM:
        msg["From"] = FROM
    msg["To"] = ", ".join(row["to"])
    if row.get("cc"):
        msg["Cc"] = ", ".join(row["cc"])
    msg["Subject"] = row.get("subject") or ""
    msg.set_content(row["body"])
    rec = registry.get(row["agent"])
    for name in row.get("attach") or []:
        path = _outbox(rec) / name
        kind = (mimetypes.guess_type(name)[0] or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=kind[0], subtype=kind[1], filename=name)
    return msg


def token_file():
    """The Gmail OAuth token file: PLATFORM_GMAIL_TOKEN, or var/mail/gmail-token.json."""
    return Path(TOKEN or Path(config.VAR) / "mail" / "gmail-token.json")


def _token():
    if _access["token"] and time.time() < _access["until"] - 60:
        return _access["token"]
    held = json.loads(token_file().read_text())
    form = urllib.parse.urlencode({"client_id": held["client_id"],
                                   "client_secret": held["client_secret"],
                                   "refresh_token": held["refresh_token"],
                                   "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request(held.get("token_uri") or
                                "https://oauth2.googleapis.com/token", data=form),
                                timeout=20) as r:
        got = json.loads(r.read())
    _access.update(token=got["access_token"], until=time.time() + int(got.get("expires_in", 3000)))
    return _access["token"]


def _send(row):
    raw = base64.urlsafe_b64encode(_message(row).as_bytes()).decode()
    req = urllib.request.Request(SEND_URL, data=json.dumps({"raw": raw}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {_token()}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def reset():
    for t in _timers.values():
        t.cancel()
    _timers.clear()
    _listeners.clear()
    _access.update(token=None, until=0)
