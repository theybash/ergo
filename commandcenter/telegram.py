"""The Telegram bridge: the operator's phone, when the console is not open.

Two directions, and they matter for different reasons.

Outward is the important one. Everything this platform does that nobody is watching, it does at
3am: a job fires, a job is missed, a server refuses to start. Without a way to say so, all of that
fails into silence. That is why the bridge is built before the scheduler.

Inward is a convenience. A message from the phone goes to the master, always, and the master wakes
to read it. Answers 2 and 8 of the daemon grill: "the master, always, with the context of who
failed what", and a sleeping master is woken rather than left holding the message.

Polling, not webhooks. A webhook needs an address on the open internet, and the one thing this
platform never does is publish itself. Polling reaches out, so nothing needs exposing.

Written against the HTTP interface directly rather than with python-telegram-bot, because there is
no pip on this laptop and the library would be the only dependency in the project. The four ideas
worth keeping from the v1 bridge are here: chunking at four thousand characters, a formatting
fallback so a reply is never lost to a stray asterisk, the typing indicator on a four second loop,
and the duplicate check that stopped the same answer arriving twice. The two-lane streaming from v1
is not here, because its source is on the other machine and a guess at its shape would be worse
than the plain version.
"""

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, logs

log = logs.get(__name__)

BASE = "https://api.telegram.org/bot{token}/{method}"

_lock = threading.RLock()
_stop = threading.Event()
_last_sent = {}          # chat -> hash of the last text, the duplicate check
_warned = False


def configured():
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_OPERATOR)


def _call(method, params, timeout=20):
    """One request to Telegram. Returns the result, or raises.

    Errors are not smoothed over here. The caller decides whether a failure is worth retrying,
    and the two callers want opposite things: sending gives up and logs, polling waits and tries
    again for ever.
    """
    url = BASE.format(token=config.TELEGRAM_TOKEN, method=method)
    data = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or "telegram refused")
    return body.get("result")


# -- outward ---------------------------------------------------------------------

def _chunks(text):
    """Split a long reply on paragraph, then line, then anywhere.

    Telegram refuses anything over 4096 characters. Cutting mid-word is ugly but cutting mid-code
    block is worse, so the split prefers a blank line, then a newline, and only then the limit.
    """
    limit = config.TELEGRAM_CHUNK
    out = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    return out


STRIP = str.maketrans({"*": "", "_": "", "`": "", "[": "(", "]": ")"})


def send(text, chat=None, quiet_duplicates=True):
    """Send a message to the operator. Returns True if every piece arrived.

    The duplicate check is not tidiness. In v1 the same answer could arrive twice when a reply was
    both streamed and sent, and two identical messages on a phone read as two separate events, so
    the operator goes looking for the second thing that happened.
    """
    global _warned
    text = (text or "").strip()
    if not text:
        return False
    if not configured():
        if not _warned:
            _warned = True
            log.warning("telegram not configured: set PLATFORM_TELEGRAM_TOKEN and "
                        "PLATFORM_TELEGRAM_OPERATOR in var/secrets.env")
        return False

    chat = str(chat or config.TELEGRAM_OPERATOR)
    digest = hashlib.sha1(text.encode()).hexdigest()
    with _lock:
        if quiet_duplicates and _last_sent.get(chat) == digest:
            log.info("telegram: skipped a duplicate of the last message to %s", chat)
            return True
        _last_sent[chat] = digest

    ok = True
    for piece in _chunks(text):
        ok = _send_one(chat, piece) and ok
    return ok


def _send_one(chat, piece):
    """Three attempts, each giving up something rather than giving up the message.

    Markdown first, because agents write it and it reads better. Then the same text with no
    formatting, which is what fixes an unbalanced asterisk. Then the text with the formatting
    characters removed, which is what fixes an entity Telegram parsed and then rejected. The
    message surviving matters more than how it looks.
    """
    attempts = (
        {"parse_mode": "Markdown", "text": piece},
        {"text": piece},
        {"text": piece.translate(STRIP)},
    )
    for n, extra in enumerate(attempts, 1):
        try:
            _call("sendMessage", {"chat_id": chat, "disable_web_page_preview": "true", **extra})
            if n > 1:
                log.info("telegram: sent on attempt %d", n)
            return True
        except Exception as exc:
            log.warning("telegram send attempt %d failed: %s", n, exc)
    return False


def notify(text):
    """What the daemon calls to tell the operator something on its own: a job's report, a missed
    run, a server that will not start, an alarm that will not ring, an inbox ping.

    Off by the owner's word on 17 October 2026: "Telegram, my eyes don't go there." He keeps Telegram
    to write to the assistant and read the assistant's answers, and nothing else. So the daemon writes these to
    the log and sends nothing; the Ergo app's notices and the front desk's calls are how he hears. Set
    PLATFORM_TELEGRAM_ALERTS=1 to send them again. Returns whether it was sent.
    """
    if not config.TELEGRAM_ALERTS:
        log.info("telegram alerts are off, not sent: %s", " ".join(str(text or "").split())[:160])
        return False
    return send(text)


def typing(chat):
    try:
        _call("sendChatAction", {"chat_id": chat, "action": "typing"}, timeout=10)
    except Exception:
        pass


class Typing:
    """Keeps the typing indicator alive while an agent thinks.

    Telegram clears it after five seconds, so it has to be sent again every four. Without it a
    question that takes two minutes looks like a bridge that swallowed the message.
    """

    def __init__(self, chat):
        self.chat = chat
        self.done = threading.Event()

    def __enter__(self):
        def loop():
            while not self.done.wait(0):
                typing(self.chat)
                if self.done.wait(4.0):
                    return
        threading.Thread(target=loop, name="tg-typing", daemon=True).start()
        return self

    def __exit__(self, *_):
        self.done.set()


# -- inward ----------------------------------------------------------------------

def _offset(new=None):
    """Where the bridge got to. Kept on disk so a restart does not replay this morning."""
    if new is not None:
        config.ensure_dirs()
        config.TELEGRAM_FILE.write_text(json.dumps({"offset": new}))
        return new
    try:
        return int(json.loads(config.TELEGRAM_FILE.read_text()).get("offset", 0))
    except Exception:
        return 0


HELP = (
    "Anything you type goes to the master.\n\n"
    "/agents  who exists, and awake or asleep\n"
    "/wake <id>, /sleep <id>\n"
    "/jobs  the schedule, and what each did last\n"
    "/rerun <job>  run a job now\n"
    "/health  memory, battery, disk\n"
)


def _command(text):
    """Answer a command. Returns the reply, or None if this is not a command.

    Imports sit inside the function on purpose: the scheduler imports this module to send its
    alerts, so importing the scheduler at the top would be a cycle.
    """
    from . import broker, cron, health, registry

    word, _, rest = text.strip().partition(" ")
    word, rest = word.lower(), rest.strip()

    if word in ("/help", "/start"):
        return HELP

    if word == "/agents":
        lines = []
        for rec in registry.all():
            mark = "awake" if broker.awake(rec["id"]) else "asleep"
            role = f" ({rec['role']})" if rec.get("role") else ""
            lines.append(f"{rec['id']}{role} — {mark}")
        return "\n".join(lines) or "no agents registered"

    if word in ("/wake", "/sleep"):
        if not rest:
            return f"{word} needs an agent id"
        if not registry.get(rest):
            return f"no agent called {rest}"
        if word == "/wake":
            broker.start(rest)
            return f"{rest} is waking"
        return f"{rest} is asleep" if broker.sleep(rest) else f"{rest} was already asleep"

    if word == "/jobs":
        return cron.describe()

    if word == "/rerun":
        if not rest:
            return "/rerun needs a job id"
        return cron.run_now(rest)

    if word == "/health":
        snap = health.snapshot()
        mem = snap.get("memory") or {}
        bat = snap.get("battery") or {}
        press = snap.get("pressure") or {}
        root = next((d for d in (snap.get("disks") or []) if d.get("mount") == "/"), {})
        plug = "on mains" if bat.get("plugged") else "on battery"
        return (f"memory {mem.get('available_gb')} of {mem.get('total_gb')} GB free\n"
                f"battery {bat.get('percent')}% {plug}\n"
                f"disk {root.get('free_gb')} GB free on /\n"
                f"cpu pressure {press.get('cpu')}, memory {press.get('memory')}")

    if word.startswith("/"):
        return f"no such command: {word}\n\n{HELP}"
    return None


def _to_master(text, chat):
    """Hand a message to the master and send back what it says.

    Answer 2: the master, always. Answer 8: a sleeping master is woken, unlike a message typed on
    the dashboard, which is held. A message from the phone is a message the operator is waiting on.
    """
    from . import cords, registry

    master = next((r for r in registry.all() if r.get("role") == "master"), None)
    if not master:
        return ("No master is registered, so there is nobody to send that to. "
                "Register one on the console, or use /agents and /wake.")
    with Typing(chat):
        reply = cords.ask(master["id"], f"[from the owner, over Telegram]\n\n{text}")
    if reply is None:
        return f"{master['id']} did not answer. It may still be working; check the console."
    return reply


def _handle(update):
    message = update.get("message") or update.get("edited_message") or {}
    sender = str((message.get("from") or {}).get("id") or "")
    chat = str((message.get("chat") or {}).get("id") or "")
    text = (message.get("text") or "").strip()

    # Answer 44: the operator's user id only. A bot address is guessable and the bridge can wake
    # agents, so a stranger's message is logged and dropped rather than answered.
    if sender != str(config.TELEGRAM_OPERATOR):
        log.warning("telegram: ignored a message from %s", sender or "an unknown sender")
        return
    if not text:
        return

    log.info("telegram: %r", text[:120])
    try:
        reply = _command(text)
        if reply is None:
            reply = _to_master(text, chat)
    except Exception as exc:
        log.exception("telegram: handling %r failed", text[:60])
        reply = f"That failed: {exc}"
    # quiet_duplicates off: the same question asked twice deserves the same answer twice, and the
    # check exists for one reply arriving down two paths, not for two replies that agree.
    send(reply, chat=chat, quiet_duplicates=False)


# The first wait after a failed poll, in seconds. It doubles up to RETRY_MOST. The tests shorten it.
RETRY_FIRST = 1.0
RETRY_MOST = 30.0


def _poll():
    offset = _offset()
    backoff = RETRY_FIRST
    log.info("telegram bridge polling, from update %s", offset)
    while not _stop.is_set():
        try:
            updates = _call("getUpdates",
                            {"offset": offset, "timeout": 50, "allowed_updates": '["message"]'},
                            timeout=70) or []
            backoff = RETRY_FIRST
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                _offset(offset)
                # Each message on its own thread, so a question that takes two minutes does not
                # stop the bridge hearing the next one.
                threading.Thread(target=_handle, args=(update,),
                                 name="tg-handle", daemon=True).start()
        except Exception as exc:
            # A dead network is normal, not exceptional: the laptop's router falls over most
            # evenings. So this waits and tries again for ever, with the wait growing to half a
            # minute so a long outage is not a thousand log lines.
            log.warning("telegram poll failed (%s); retrying in %.0fs", exc, backoff)
            _stop.wait(backoff)
            backoff = min(backoff * 2, RETRY_MOST)


def start():
    if not configured():
        log.info("telegram bridge off: no token or no operator id")
        return None
    _stop.clear()
    thread = threading.Thread(target=_poll, name="telegram", daemon=True)
    thread.start()
    return thread


def stop():
    _stop.set()


def reset():
    """Forget the duplicate check and the warned-once flag. For tests."""
    global _warned
    with _lock:
        _last_sent.clear()
    _warned = False
    _stop.set()
    _stop.clear()
