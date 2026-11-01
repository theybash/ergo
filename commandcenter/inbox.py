"""The inbox: what the front desk has to tell the owner, so that they can speak first.

The owner wants the console to behave like Jarvis: a call opens and the voice already has the news.
There is a second reason, and it decides where this lives. OpenAI's GPT-Live docs say that on a long
call a replacement voice engine keeps only the startup instructions and about 8k tokens of recent
talk. So what the front desk must say cannot live only in the conversation. It lives here, and each call is
told what is still untold at the moment it opens.

No database. One append-only event log, var/voice/inbox.jsonl, one JSON row per event:

    added       {id, agent, text, urgency, expires, link, when?, ask?, job?}   a new item;
                                     when is the time of its event, an alarm or a meeting
    told        {id, by, p}          the front desk said it (by "jev", with Jev's p, or by a route)
    handled     {id, by, p?}         done with; an ask item is handled when the ask goes
    later       {id, by, p?}         back to waiting, for the next brief
    dismissed   {id, by, p?}         not wanted
    opened      {id, by, p?}         they went into that agent's chat
    replaced    {id, by}             a newer item from the same agent covers it; by is the new id
    expired     {id}                 past its time; written by the read that noticed
    removed     {id, by: "config"}   taken off on the Config page
    restored    {id}                 put back to waiting from the Config page

The daemon is the only writer. On the first read it folds the log into three kinds of item in
memory: waiting, told and closed. After that it appends, and applies each event as it writes it.

Rules, all in code:

    one item      the same text from the same agent within an hour is the same item
    expiry        soon after its event time when it has one; else now after 2 hours, today at local
                  midnight, fyi after 24 hours, unless the item says its own expiry. A
                  backstop, applied when the inbox is read
    later         puts an item back to waiting, so the next brief reads it again
    compaction    once a month, items closed more than 30 days ago move to
                  var/voice/inbox-YYYY-MM.jsonl, by the month they closed

Three moments ask Jev, each one request, and act only at 0.8 or above. Below that, or with no key,
or when Jev fails, nothing changes; expiry still applies.

    told      after each finished the front desk line: one Noul per untold item of the call. The
              items the daemon pushed into the opening skip Jev: a finished line that
              shares a carrying word with one marks it told, by "opening"
    reply     after each finished the owner line: one Choice per item told on the call
    replace   when an item is added: one Noul per waiting item from the same agent

Two triggers send one Telegram line, "The front desk has N things, open the app", only when something is
untold: the morning brief time, and a "now" item outside quiet hours. Each at most once.

Nothing here logs an item's text or a line that was said.
"""

import datetime as _dt
import json
import threading
import time
import uuid

from . import clock, config, logs, memory, telegram

log = logs.get(__name__)

_lock = threading.RLock()

URGENCIES = ("urgent", "now", "today", "fyi")
# The order a brief reads them in.
RANK = {"urgent": -1, "now": 0, "today": 1, "fyi": 2}

# Urgent is the one level that calls them: their phone rings with the front desk, day or night, quiet hours or
# not, every time. The owner set it on 5 October 2026, and on 6 October said no limit on how often.
# How long a ring stays offered to the phone: long enough for a phone on a flaky connection to
# reconnect and still ring. The phone rings once per item, so a long offer never rings twice.
RING_SECONDS = 10 * 60

# What a route may do to an item, and what each one leaves it as.
ACTIONS = ("told", "handled", "later", "dismissed", "opened", "removed", "restored")
CLOSING = ("handled", "dismissed", "opened", "replaced", "expired", "removed")

DEDUPE_SECONDS = 3600
NOW_LIFE = 2 * 3600
FYI_LIFE = 24 * 3600
# What the Config page and the told list show: changes in the last day.
RECENT = 24 * 3600
KEEP_DAYS = 30

TEXT_CHARS = 300
AGENT_CHARS = 60

# Jev acts at or above this, and not below. The same margin as the referee.
THRESHOLD = 0.8

# A "now" item that is still waiting after this is worth a ping. An ask answered at the desk in
# ten seconds is not news on the phone.
NOW_GRACE = 60

# The morning ping goes in the ten minutes after the set time or not at all. The house rule: a
# missed run never fires late, and a laptop opened at noon is not a morning.
MORNING_WINDOW_MINUTES = 10

TICK_SECONDS = 30

_state = None           # {"path", "items": {id: item}, "month"}
_pushed = {}            # {id: when}: items a call opened with, not yet marked told
_listeners = []
_stop = threading.Event()


# -- the files -------------------------------------------------------------------

def _dir():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR


def _log_file():
    return _dir() / "inbox.jsonl"


def _pings_file():
    return _dir() / "inbox-pings.json"


def _month_file(month):
    return _dir() / f"inbox-{month}.jsonl"


def on_change(fn):
    _listeners.append(fn)


def _emit(event, payload):
    for fn in list(_listeners):
        try:
            fn(event, payload)
        except Exception:
            pass


# -- the fold --------------------------------------------------------------------

def _blank(ev):
    return {"id": ev["id"], "agent": ev.get("agent") or "", "text": ev.get("text") or "",
            "urgency": ev.get("urgency") if ev.get("urgency") in URGENCIES else "today",
            "expires": ev.get("expires"), "link": ev.get("link") or "",
            "when": ev.get("when"),
            "ask": ev.get("ask"), "job": ev.get("job"), "kind": ev.get("kind"),
            "added": ev.get("at") or 0.0, "status": "waiting", "decision": None,
            "by": None, "p": None, "told_at": None, "closed": None, "replaced_by": None,
            "changed": ev.get("at") or 0.0}


def _apply(items, ev):
    """One event onto the items. An event for an item that is not there changes nothing."""
    kind = ev.get("event")
    if kind == "added":
        if ev.get("id") and ev["id"] not in items:
            items[ev["id"]] = _blank(ev)
        return
    item = items.get(ev.get("id"))
    if not item:
        return
    at = ev.get("at") or item["changed"]
    item["changed"] = at
    item["by"] = ev.get("by")
    item["p"] = ev.get("p")
    if kind == "told":
        if item["status"] == "waiting":
            item["status"] = "told"
            item["told_at"] = at
        item["decision"] = "told"
    elif kind in ("later", "restored"):
        item["status"] = "waiting"
        item["decision"] = kind
        item["closed"] = None
        item["replaced_by"] = None
        if kind == "restored":
            item["told_at"] = None
            # A restored item that had run out gets a fresh life, or the next read expires it
            # again and the button did nothing.
            if item.get("expires") is not None and item["expires"] <= at:
                item["expires"] = ev.get("expires")
    elif kind == "escalated":
        item["urgency"] = "urgent"
        item["escalated"] = True
    elif kind in CLOSING:
        item["status"] = "closed"
        item["decision"] = kind
        item["closed"] = at
        if kind == "replaced":
            item["replaced_by"] = ev.get("by")


def _read(path):
    items = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    _apply(items, ev)
    except OSError:
        pass
    return items


def _held():
    """The folded state, read once per log file. A test that moves VAR gets a fresh one."""
    global _state
    path = str(_log_file())
    if _state is None or _state["path"] != path:
        _state = {"path": path, "items": _read(path), "month": None}
    month = clock.local().strftime("%Y-%m")
    if _state["month"] != month:
        _compact(_state)
        _state["month"] = month
    return _state


def _write(st, ev):
    ev = dict(ev)
    ev.setdefault("at", clock.now())
    with open(st["path"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")
    _apply(st["items"], ev)
    _emit("inbox", {"event": ev["event"], "id": ev.get("id"), "by": ev.get("by"),
                    "summary": _summary(st)})
    return ev


# -- compaction ------------------------------------------------------------------

def _compact(st):
    """Items closed more than KEEP_DAYS ago go to the file for the month they closed in."""
    cutoff = clock.now() - KEEP_DAYS * 86400
    old = {i: it for i, it in st["items"].items()
           if it["status"] == "closed" and (it["closed"] or 0) < cutoff}
    if not old:
        return 0
    try:
        with open(st["path"], encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    keep, moved = [], {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        item = old.get(ev.get("id"))
        if item:
            month = _dt.datetime.fromtimestamp(item["closed"] or 0).strftime("%Y-%m")
            moved.setdefault(month, []).append(line if line.endswith("\n") else line + "\n")
        else:
            keep.append(line if line.endswith("\n") else line + "\n")
    for month, rows in moved.items():
        with open(_month_file(month), "a", encoding="utf-8") as fh:
            fh.writelines(rows)
    tmp = st["path"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(keep)
    import os
    os.replace(tmp, st["path"])
    for i in old:
        st["items"].pop(i, None)
    log.info("inbox compacted: %d items moved to %s", len(old), ", ".join(sorted(moved)))
    return len(old)


# -- expiry and the asks ---------------------------------------------------------

def _midnight(at):
    day = _dt.datetime.fromtimestamp(at).date() + _dt.timedelta(days=1)
    return _dt.datetime.combine(day, _dt.time()).timestamp()


def default_expiry(urgency, at):
    if urgency in ("now", "urgent"):
        return at + NOW_LIFE
    if urgency == "fyi":
        return at + FYI_LIFE
    return _midnight(at)


def _when(value, at):
    """An expiry as given: epoch seconds, or an ISO date and time read as local. None is none."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        return stamp / 1000.0 if stamp > 1e11 else stamp
    try:
        got = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("expires must be a time like 2026-09-25T18:00") from None
    return got.timestamp()


def _sweep(st):
    """The backstop. Run on every read: an item past its time expires, and an ask item whose ask
    is answered or gone is handled."""
    now = clock.now()
    for item in list(st["items"].values()):
        if item["status"] == "closed":
            continue
        if item.get("expires") is not None and item["expires"] <= now:
            _write(st, {"event": "expired", "id": item["id"]})
    open_asks = [it for it in st["items"].values() if it["status"] != "closed" and it.get("ask")]
    if not open_asks:
        return
    from . import broker
    for item in open_asks:
        try:
            ids = {f.get("id") for f in broker.pending_asks(item["agent"])}
        except Exception:
            continue
        if item["ask"] not in ids:
            _write(st, {"event": "handled", "id": item["id"], "by": "ask"})


# -- reading ---------------------------------------------------------------------

def _view(item):
    out = {k: item[k] for k in ("id", "agent", "text", "urgency", "expires", "link", "added",
                                "status", "decision", "by", "p", "changed")}
    if item.get("replaced_by"):
        out["replaced_by"] = item["replaced_by"]
    if item.get("when"):
        out["when"] = item["when"]
    if item.get("ask"):
        out["ask"] = item["ask"]
    if item.get("job"):
        out["job"] = item["job"]
    if item.get("kind"):
        out["kind"] = item["kind"]
    return out


def _order(items):
    return sorted(items, key=lambda it: (RANK.get(it["urgency"], 1), it["added"]))


def _summary(st):
    waiting = [it for it in st["items"].values() if it["status"] == "waiting"]
    last = max([it["changed"] for it in st["items"].values()] or [0.0])
    return {"untold": len(waiting),
            "urgent": sum(1 for it in waiting if it["urgency"] in ("now", "urgent")),
            "at": last}


def summary():
    """What GET /api/state carries: {untold, urgent, at}."""
    with _lock:
        st = _held()
        _sweep(st)
        return _summary(st)


def listing():
    """GET /api/inbox: every waiting item, and every other item changed in the last day."""
    with _lock:
        st = _held()
        _sweep(st)
        now = clock.now()
        waiting = [_view(it) for it in _order(
            [it for it in st["items"].values() if it["status"] == "waiting"])]
        told = [_view(it) for it in sorted(
            [it for it in st["items"].values()
             if it["status"] != "waiting" and now - it["changed"] <= RECENT],
            key=lambda it: -it["changed"])]
        return {"waiting": waiting, "told": told, "summary": _summary(st)}


def for_call():
    """The untold items a call opens with, most urgent first, then oldest first."""
    with _lock:
        st = _held()
        _sweep(st)
        return [{"id": it["id"], "agent": it["agent"], "text": it["text"],
                 "urgency": it["urgency"], "added": it["added"], "when": it.get("when")}
                for it in _order([it for it in st["items"].values()
                                  if it["status"] == "waiting"])]


def get(item_id):
    with _lock:
        item = _held()["items"].get(item_id)
        return _view(item) if item else None


# -- writing ---------------------------------------------------------------------

def _clean(text, count):
    return " ".join(str(text or "").split())[:count]


def chat_link(agent):
    return f"?screen=chat#/a/{agent}"


def add(agent, text, urgency="today", expires=None, link=None, ask=None, job=None,
        judge=True, when=None, kind=None):
    """One item. Returns it, with `duplicate` true when it was already there.

    `when` is the time of the item's event, an alarm or a meeting. The item expires soon after it,
    unless `expires` says otherwise. On 18 October the front desk said "set the 10:05 one" at 12:03, because an
    urgent item lived 2 hours from when it was added, not until its alarm.

    The same text from the same agent within the hour is the item already held, whatever became
    of it, so an agent that says the same thing twice is heard once.
    """
    agent = _clean(agent, AGENT_CHARS)
    text = _clean(text, TEXT_CHARS)
    if not agent:
        raise ValueError("an item needs the agent it is from")
    if not text:
        raise ValueError("an item needs one or two sentences of text")
    urgency = (urgency or "today").strip().lower() if isinstance(urgency, str) else "today"
    if urgency not in URGENCIES:
        raise ValueError("urgency must be urgent, now, today or fyi")
    now = clock.now()
    event = _when(when, now)
    until = _when(expires, now)
    if until is None:
        # A ring lasts RING_SECONDS, so the item outlives its event by that much: a call that rings
        # at 09:59 for a 10:00 meeting still has it when they answer at 10:01. An event already
        # past when the item is added keeps the usual expiry, so the front desk can say it was missed.
        if event is not None and event > now:
            until = event + RING_SECONDS
        else:
            until = default_expiry(urgency, now)
    with _lock:
        st = _held()
        same = text.lower()
        for item in st["items"].values():
            if ask and item.get("ask") == ask and item["agent"] == agent:
                return dict(_view(item), duplicate=True)
            if (item["agent"] == agent and item["text"].lower() == same
                    and now - item["added"] < DEDUPE_SECONDS):
                return dict(_view(item), duplicate=True)
        ev = {"event": "added", "id": uuid.uuid4().hex[:10], "agent": agent, "text": text,
              "urgency": urgency, "expires": until, "link": link or chat_link(agent)}
        if event is not None:
            ev["when"] = event
        if ask:
            ev["ask"] = ask
        if job:
            ev["job"] = job
        # "free": an agent is free for a task they held. The phone posts it on its own loud
        # channel, because a reminder that waits for a the front desk call comes too late.
        if kind:
            ev["kind"] = kind
        ev = _write(st, ev)
        item = _view(st["items"][ev["id"]])
    log.info("inbox: %s added %s (%s)", agent, item["id"], urgency)
    if urgency == "urgent":
        ring_check()
    if judge and not ask:
        threading.Thread(target=judge_replace, args=(item["id"],), name="inbox-replace",
                         daemon=True).start()
    return dict(item, duplicate=False)


def act(item_id, action, by=None, p=None):
    """One route action on one item. KeyError for no such item, ValueError for no such action."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    with _lock:
        st = _held()
        item = st["items"].get(item_id)
        if not item:
            raise KeyError(f"no inbox item called {item_id}")
        ev = {"event": action, "id": item_id}
        if by is not None:
            ev["by"] = by
        if p is not None:
            ev["p"] = round(float(p), 3)
        if action == "removed" and by is None:
            ev["by"] = "config"
        if action == "restored" and item.get("expires") is not None \
                and item["expires"] <= clock.now():
            ev["expires"] = default_expiry(item["urgency"], clock.now())
        _write(st, ev)
        return _view(st["items"][item_id])


def clear(confirm=False):
    """Remove every waiting item. Refuses without confirm, because it is a whole list."""
    if confirm is not True:
        raise ValueError("clearing the inbox needs confirm: true")
    with _lock:
        st = _held()
        ids = [i for i, it in st["items"].items() if it["status"] == "waiting"]
        for i in ids:
            _write(st, {"event": "removed", "id": i, "by": "config"})
    log.info("inbox cleared: %d removed", len(ids))
    return {"removed": len(ids)}


# -- automatic items -------------------------------------------------------------

def _ask_text(frame):
    if frame.get("kind") == "question":
        titles = [q.get("question") or q.get("header") or ""
                  for q in (frame.get("questions") or []) if isinstance(q, dict)]
        said = "; ".join(t for t in titles if t)
        return f"is asking you a question: {said}" if said else "is asking you a question."
    tool = frame.get("tool") or "a tool"
    summary = _clean(frame.get("summary"), 120)
    return (f"wants permission to run {tool}: {summary}" if summary
            else f"wants permission to run {tool}.")


def ask_opened(agent, frame):
    """A new pending ask becomes a "now" item, closed when the ask is answered or gone."""
    if not isinstance(frame, dict) or not frame.get("id"):
        return None
    try:
        item = add(agent, _ask_text(frame), urgency="now", ask=frame["id"])
    except Exception:
        log.exception("inbox: the ask from %s was not added", agent)
        return None
    # One look a minute from now, so the call comes at a minute and not up to a tick later.
    timer = threading.Timer(ASK_CALL_AFTER + 1, _ask_due)
    timer.daemon = True
    timer.start()
    return item


def _ask_due():
    try:
        if escalate_asks():
            ring_check()
    except Exception:
        log.exception("inbox: the waiting box was not escalated")


def ask_closed(agent, ask_id):
    with _lock:
        st = _held()
        for item in list(st["items"].values()):
            if item.get("ask") == ask_id and item["agent"] == agent \
                    and item["status"] != "closed":
                _write(st, {"event": "handled", "id": item["id"], "by": "ask"})


def job_failed(job, outcome):
    """A failed cron job becomes a "today" item."""
    try:
        task = _clean(job.get("task"), 90)
        return add(job.get("agent") or "schedule",
                   f"The scheduled job {job.get('id')} {outcome}: {task}",
                   urgency="today", job=job.get("id"))
    except Exception:
        log.exception("inbox: the failed job was not added")
        return None


# -- the Jev moments -------------------------------------------------------------

def _about(item):
    return {"from_agent": item["agent"], "text": item["text"]}


TOLD_CRITERIA = {
    "true": {"meaning": "The front desk conveyed the substance of the item, briefly or in other words. "
                        "Naming the agent is not needed.",
             "examples": ["Personal assistant says the dentist moved to four.",
                          "Your flight tomorrow is delayed an hour, per job search."]},
    "false": {"meaning": "The front desk did not say this item. They greeted, counted the items, asked "
                         "which to go into, spoke about a different item, or only hinted at it.",
              "examples": ["Welcome back. Three things.", "Which would you like first?",
                           "Morning, the owner."]},
}


def told_question(item):
    return {"type": "noul",
            "instructions": {
                "situation": "The owner is on a voice call with the front desk, their voice assistant. The front desk "
                             "is reading them a short brief: one sentence per item left by their "
                             "agents. `desk_said` is one line the front desk just said aloud.",
                "item": _about(item),
                "question": "Did `desk_said` tell the owner the news in `item`, so that they have "
                            "now heard it?"},
            "criteria": TOLD_CRITERIA}


REPLY_CRITERIA = {
    "handled": {"meaning": "They say it is done, dealt with, noted, or needs nothing more.",
                "examples": ["done", "got it", "already sorted", "thanks, noted", "fine"]},
    "later": {"meaning": "They want it again later, or to be reminded.",
              "examples": ["remind me tonight", "later", "come back to that tomorrow",
                           "not now, ask me after lunch"]},
    "dismissed": {"meaning": "They do not want it at all.",
                  "examples": ["drop it", "ignore that", "not interested", "delete that one"]},
    "open": {"meaning": "They want to go into it now, in that agent's chat.",
             "examples": ["open it", "show me", "take me there", "let's look at that one"]},
    "unrelated": {"meaning": "Not about this item: another item, another subject, filler, or "
                             "too unclear to act on.",
                  "examples": ["mm", "what time is it", "and the other one?"]},
}

REPLY_ACTIONS = {"handled": "handled", "later": "later", "dismissed": "dismissed",
                 "open": "opened"}


def reply_question(item):
    return {"type": "choice",
            "instructions": {
                "situation": "The owner is on a voice call with the front desk, their voice assistant. The front desk "
                             "has told them `item`, one of the things their agents left for them. "
                             "`desk_said` is the front desk's last line and `owner_said` is what "
                             "The owner said next, as speech recognition heard it.",
                "item": _about(item),
                "question": "What does `owner_said` mean for `item`?"},
            "criteria": REPLY_CRITERIA}


REPLACE_CRITERIA = {
    "true": {"meaning": "The new item updates, corrects or covers the older one, so the older "
                        "one is out of date and need not be said.",
             "examples": ["older: the build is running. new: the build passed.",
                          "older: meeting at 3. new: meeting moved to 4."]},
    "false": {"meaning": "They are about different things, or both are still worth saying.",
              "examples": ["older: dentist at 4. new: the invoice is overdue."]},
}


def replace_question(item):
    return {"type": "noul",
            "instructions": {
                "situation": "An agent leaves short items for the owner to hear by voice. "
                             "`new_item` was just added by the same agent that left `older_item`.",
                "older_item": {"text": item["text"], "urgency": item["urgency"]},
                "question": "Does `new_item` replace `older_item`?"},
            "criteria": REPLACE_CRITERIA}


def _jev(payload):
    """The referee's transport: the same key, scrub and timeout. (answers or None, why)."""
    from . import judge
    return judge._ask(payload)


def _p(answer, field="noul"):
    value = answer.get(field) if isinstance(answer, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return 0.0


def _line(text):
    return " ".join(str(text or "").split())[:400]


def _items(ids, statuses):
    with _lock:
        st = _held()
        _sweep(st)
        out = []
        for i in (ids if isinstance(ids, list) else [])[:12]:
            item = st["items"].get(str(i))
            if item and item["status"] in statuses and item not in out:
                out.append(dict(item))
        return out


def told_payload(line, items):
    return {"state": {"desk_said": _line(line)}, "model": memory.JEV_MODEL,
            "questions": {it["id"]: told_question(it) for it in items}}


def reply_payload(line, desk, items):
    return {"state": {"desk_said": _line(desk), "owner_said": _line(line)},
            "model": memory.JEV_MODEL,
            "questions": {it["id"]: reply_question(it) for it in items}}


def replace_payload(new, olders):
    return {"state": {"new_item": {"text": new["text"], "urgency": new["urgency"]}},
            "model": memory.JEV_MODEL,
            "questions": {it["id"]: replace_question(it) for it in olders}}


# How long an item stays pushed. A call that opened and never spoke is long over by then.
PUSHED_SECONDS = 3600


def pushed(ids):
    """The items a call opened with. The front desk's line that carries one marks it told."""
    now = clock.now()
    with _lock:
        for i, at in list(_pushed.items()):
            if now - at > PUSHED_SECONDS:
                _pushed.pop(i, None)
        for i in ids or []:
            _pushed[str(i)] = now


# Words of five letters or more that an opening line says whatever the items are: greetings, the
# owner's name, the count, the time of day. They carry no item. "Good evening, sir, I have three things"
# must not mark "Dinner this evening" or "Interview at three" told, or a call that drops after
# the greeting loses them.
COMMON = frozenset(
    "morning afternoon evening night tonight today tomorrow yesterday hello there owner "
    "welcome things thing items about which would could should still other others waiting "
    "agent agents minutes hours three first second third right "
    "everything nothing something alright quick update updates".split())


def _keys(text):
    """The words that carry an item: a time like 10:05, a number of two digits or more, and words
    of five letters or more that are not COMMON."""
    import re
    out = set()
    for w in re.findall(r"\d{1,2}:\d{2}|[a-z0-9]+", str(text or "").lower()):
        if ":" in w or (w.isdigit() and len(w) >= 2):
            out.add(w)
        elif not w.isdigit() and len(w) >= 5 and w not in COMMON:
            out.add(w)
    return out


def _carries(line, item):
    """Whether a line shares a carrying word with an item. A greeting alone shares none."""
    return bool(_keys(line) & _keys(item.get("text")))


def judge_told(line, ids):
    """After a front desk line. Every untold item of the call they conveyed is marked told.

    The items pushed into the opening are marked told by the daemon itself, without Jev. Jev marks
    a line told only when it is sure: on 18 October it marked 3 of 8 after the opening, so 2 items
    came back on the next call, 13 minutes later. A pushed item counts only when the line shares
    a carrying word with it, so a first line that is only a greeting marks nothing told. The
    other pushed items stay pushed for their next line, and Jev still judges them."""
    started = time.monotonic()
    items = _items(ids, ("waiting",))
    decisions, why = [], ""
    if not items or not _line(line):
        return {"decisions": [], "ran": False}
    now = clock.now()
    with _lock:
        mine = [it for it in items if it["id"] in _pushed
                and now - _pushed[it["id"]] <= PUSHED_SECONDS and _carries(line, it)]
        for item in mine:
            _pushed.pop(item["id"], None)
    for item in mine:
        try:
            act(item["id"], "told", by="opening")
        except KeyError:
            continue
        decisions.append({"id": item["id"], "decision": "told", "p": None,
                          "agent": item["agent"]})
    items = [it for it in items if it not in mine]
    if not items:
        log.info("inbox told: %d by the opening", len(decisions))
        return {"decisions": decisions, "ran": False}
    answers, why = _jev(told_payload(line, items))
    for item in items:
        p = _p((answers or {}).get(item["id"]))
        if p >= THRESHOLD:
            try:
                act(item["id"], "told", by="jev", p=p)
            except KeyError:
                continue
            decisions.append({"id": item["id"], "decision": "told", "p": round(p, 3),
                              "agent": item["agent"]})
    log.info("inbox judge told: %d of %d, %d ms%s", len(decisions), len(items),
             round((time.monotonic() - started) * 1000), f", {why}" if why else "")
    return {"decisions": decisions, "ran": answers is not None}


def judge_reply(line, ids, desk=""):
    """After a the owner line. Each item told on the call is handled, put off, dismissed or opened.

    Only one item is opened, the surest, because one chat can be on the screen.
    """
    started = time.monotonic()
    items = _items(ids, ("told",))
    if not items or not _line(line):
        return {"decisions": [], "ran": False}
    answers, why = _jev(reply_payload(line, desk, items))
    picked = []
    for item in items:
        got = (answers or {}).get(item["id"])
        choice = got.get("choice") if isinstance(got, dict) else None
        conf = _p(got, "confidence")
        if choice in REPLY_ACTIONS and conf >= THRESHOLD:
            picked.append((item, REPLY_ACTIONS[choice], conf))
    opens = [row for row in picked if row[1] == "opened"]
    if len(opens) > 1:
        best = max(opens, key=lambda row: row[2])
        picked = [row for row in picked if row[1] != "opened" or row is best]
    decisions = []
    for item, action, conf in picked:
        try:
            act(item["id"], action, by="jev", p=conf)
        except KeyError:
            continue
        decisions.append({"id": item["id"], "decision": action, "p": round(conf, 3),
                          "agent": item["agent"], "link": item["link"]})
    log.info("inbox judge reply: %s of %d, %d ms%s",
             ", ".join(d["decision"] for d in decisions) or "none", len(items),
             round((time.monotonic() - started) * 1000), f", {why}" if why else "")
    return {"decisions": decisions, "ran": answers is not None}


def judge_replace(new_id):
    """When an item is added. A waiting item from the same agent that it covers is replaced."""
    started = time.monotonic()
    with _lock:
        st = _held()
        new = st["items"].get(new_id)
        if not new:
            return {"decisions": [], "ran": False}
        new = dict(new)
        olders = [dict(it) for it in _order(st["items"].values())
                  if it["id"] != new_id and it["agent"] == new["agent"]
                  and it["status"] == "waiting" and not it.get("ask")][:12]
    if not olders:
        return {"decisions": [], "ran": False}
    answers, why = _jev(replace_payload(new, olders))
    decisions = []
    for item in olders:
        p = _p((answers or {}).get(item["id"]))
        if p < THRESHOLD:
            continue
        with _lock:
            st = _held()
            live = st["items"].get(item["id"])
            if not live or live["status"] != "waiting":
                continue
            _write(st, {"event": "replaced", "id": item["id"], "by": new_id,
                        "p": round(p, 3)})
        decisions.append({"id": item["id"], "decision": "replaced", "by": new_id,
                          "p": round(p, 3)})
    log.info("inbox judge replace: %d of %d, %d ms%s", len(decisions), len(olders),
             round((time.monotonic() - started) * 1000), f", {why}" if why else "")
    return {"decisions": decisions, "ran": answers is not None}


def judge(body):
    """POST /api/inbox/judge. {moment: told|reply, line, ids, desk?}."""
    body = body if isinstance(body, dict) else {}
    moment = str(body.get("moment") or "").strip()
    if moment == "told":
        return judge_told(body.get("line"), body.get("ids"))
    if moment == "reply":
        return judge_reply(body.get("line"), body.get("ids"), body.get("desk") or "")
    raise ValueError("moment must be told or reply")


# -- the brief, as the front desk is told it ---------------------------------------------

# The most items an opening carries. A call is a narrow channel: The owner holds about three things
# and decides in ten seconds if the call is worth it. On 18 October an opening of 8 items and 5
# tasks ran to 200 words, and they hung up 5 seconds after its end. The rest wait until they ask.
OPENING_MAX = 3


def _count(n):
    return "one thing" if n == 1 else f"{n} things"


def opening(items):
    """The items an opening says: the first OPENING_MAX, in the order given."""
    return list(items or [])[:OPENING_MAX]


def _hm(ts):
    """A time as they would read it: 10:05 today, or the day and 10:05 on another day."""
    if not ts:
        return ""
    at = _dt.datetime.fromtimestamp(float(ts))
    if at.date() == clock.local().date():
        return at.strftime("%H:%M")
    return at.strftime("%a %d %b %H:%M")


def brief_lines(items):
    out = []
    for n, it in enumerate(items, 1):
        left = f", left {_hm(it.get('added'))}" if it.get("added") else ""
        event = f", for {_hm(it.get('when'))}" if it.get("when") else ""
        out.append(f"{n}. {it['agent']} ({it['urgency']}{left}{event}): {it['text']}")
    return out


def _more(n):
    if not n:
        return ""
    return (f" There {'is one more item' if n == 1 else f'are {n} more items'} waiting. Do not "
            "read them. You may say the count once, in a few words. If they ask for them, the "
            "backend reads them with the inbox tool.")


def waiting_view(items):
    """Untold items as the front desk's inbox tool reads them: plain fields, times as they would hear them."""
    out = []
    for it in items:
        row = {"agent": it["agent"], "text": it["text"], "urgency": it["urgency"],
               "left": _hm(it.get("added"))}
        if it.get("when"):
            row["for"] = _hm(it["when"])
        out.append(row)
    return out


def brief_block(items, resuming=False):
    """The section both models get when a call opens with untold items. At most OPENING_MAX of
    them, each with its time, and a count of the rest."""
    if not items:
        return []
    said = opening(items)
    each = ("say it in one sentence." if len(said) == 1
            else "say each one in one sentence, in this order.")
    if resuming:
        how = f"This call is resuming, so do not greet them. Now {each}"
    else:
        how = f"After your opening line, {each}"
    return ["", "## Things to tell them", "",
            "Left by their agents, most urgent first, each with the time it was left, and the "
            "time of its event when it has one. These are untold.", ""] + brief_lines(said) + [
            "", how + " Use your own words, and say each one once. If its time shows it is "
                      "stale, say it was missed, or leave it out. Then stop and wait."
            + _more(len(items) - len(said))]


def brief_say(items):
    """The line the browser hands a resumed session, so they speak first. Written here."""
    if not items:
        return ""
    said = opening(items)
    return ("Tell them now: " + " ".join(f"{it['agent']}: {it['text']}" for it in said)
            + (" One sentence." if len(said) == 1 else " One sentence each.")
            + " Then stop and wait." + _more(len(items) - len(said)))


# -- the Telegram triggers -------------------------------------------------------

def _hhmm(value):
    hours, minutes = str(value).split(":")
    return int(hours) * 60 + int(minutes)


def quiet(at=None):
    """Whether a local time is inside the quiet hours. The hours may cross midnight."""
    local = at or clock.local()
    try:
        start, end = _hhmm(memory.time_setting("quiet_start")), \
            _hhmm(memory.time_setting("quiet_end"))
    except ValueError:
        return False
    cur = local.hour * 60 + local.minute
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def _pings():
    try:
        held = json.loads(_pings_file().read_text())
        return held if isinstance(held, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pings(held):
    path = _pings_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(held))
    tmp.replace(path)


def _ping(text):
    """One line to the phone. Not the duplicate check: the same count twice is two triggers."""
    return telegram.notify(text)


def ping_text(n):
    return f"The front desk has {_count(n).replace('one thing', '1 thing')}, open the app"


# -- the front desk calls ---------------------------------------------------------------

def ring_check():
    """Ring for urgent items not yet rung for. Returns the ring, or None. The ring itself reaches
    the phone in its stream digest (see ringing). In quiet time nothing rings; quiet calls this
    again when it ends."""
    from . import quiet
    if quiet.on():
        return None
    now = clock.now()
    with _lock:
        st = _held()
        waiting = [it for it in st["items"].values()
                   if it["status"] == "waiting" and it["urgency"] == "urgent"]
        held = _pings()
        rung = set(held.get("rung") or [])
        # "Call me back in ten minutes": an item asked back rings again once its time comes.
        back = held.get("callback") or {}
        due = {i for i, at in back.items() if float(at) <= now}
        fresh = [it for it in waiting if it["id"] not in rung or it["id"] in due]
        held["callback"] = {i: at for i, at in back.items() if i not in due
                            and any(it["id"] == i for it in waiting)}
        if not fresh:
            _save_pings(held)
            return None
        newest = max(fresh, key=lambda it: it["added"])
        # `call` names this one ring. The item id alone cannot: "call me back" rings the same item
        # a second time, and each ring is its own call in the agent's call log.
        held["ring"] = {"id": newest["id"], "agent": newest["agent"], "text": newest["text"],
                        "count": len(waiting), "at": now, "call": uuid.uuid4().hex[:12]}
        held["last_ring"] = now
        held["rung"] = sorted(rung | {it["id"] for it in fresh})[-200:]
        _save_pings(held)
        ring = held["ring"]
    log.info("inbox: ringing the owner for %s (%d urgent waiting)", ring["agent"], ring["count"])
    # The agent's own call record, and its opening line recorded in its voice. The recording runs
    # in the background, and nothing in it may stop the ring.
    try:
        from . import agentcall
        agentcall.rang(ring)
    except Exception:
        log.exception("inbox: the call record for %s was not written", ring["agent"])
    # Telegram only if the call goes unanswered: a Telegram alert at the same moment covered the
    # call's banner on their phone. Answering means the front desk tells the item, so it is no longer waiting.
    timer = threading.Timer(TELEGRAM_AFTER, _telegram_if_unanswered, args=(ring["id"], ring))
    timer.daemon = True
    timer.start()
    try:
        from . import server
        server.feed.poke()
    except Exception:
        pass
    return ring


TELEGRAM_AFTER = 60.0


def _telegram_if_unanswered(item_id, ring):
    with _lock:
        item = _held()["items"].get(item_id)
        waiting = bool(item and item["status"] == "waiting")
    if not waiting:
        return
    # An agent that rang as itself is named as the caller. Anything else rang as the front desk.
    from . import registry
    rec = registry.get(ring.get("agent") or "")
    text = (f"Missed call from {rec.get('name') or rec['id']}: {ring['text']}" if rec else
            f"Missed call from the front desk: {ring['agent']} says this is urgent. {ring['text']}")
    try:
        telegram.notify(text)
    except Exception:
        log.exception("inbox: the urgent line did not reach Telegram")


CALLBACK_SECONDS = 10 * 60


def callback(item_id, seconds=CALLBACK_SECONDS):
    """The owner tapped "Call me back in 10 minutes": the same item rings again then. The ring now
    is withdrawn, and the item stays waiting."""
    with _lock:
        item = _held()["items"].get(item_id)
        if not item or item["status"] != "waiting":
            raise ValueError("no waiting item with that id")
        held = _pings()
        held.setdefault("callback", {})[item_id] = clock.now() + seconds
        if (held.get("ring") or {}).get("id") == item_id:
            held["ring"] = None
        _save_pings(held)
    log.info("inbox: The owner asked the front desk to call back about %s in %d minutes", item_id, seconds // 60)
    # Not now is a call not taken. The agent's log keeps it as missed, with its voicemail.
    _call_missed(item_id, "callback")
    try:
        from . import server
        server.feed.poke()
    except Exception:
        pass
    return {"ok": True, "rings_at": clock.now() + seconds}


def _call_missed(item_id, how):
    try:
        from . import agentcall
        agentcall.missed_item(item_id, how)
    except Exception:
        log.exception("inbox: the missed call for %s was not written", item_id)


def decline(item_id):
    """The owner tapped Decline on the phone. The ring stops now; the item stays waiting, so the front desk
    still reads it on their next call."""
    with _lock:
        held = _pings()
        if (held.get("ring") or {}).get("id") == item_id:
            held["ring"] = None
            _save_pings(held)
    log.info("inbox: The owner declined the call about %s", item_id)
    _call_missed(item_id, "declined")
    try:
        from . import server
        server.feed.poke()
    except Exception:
        pass
    return {"ok": True}


def answered(item_id, call_id=None):
    """An agent's ring was answered and the agent is on the line. The ring stops, a call back
    for the same item is dropped, and the item counts as told: the agent says it itself, so
    the front desk must not read it again on their next call."""
    with _lock:
        held = _pings()
        ring = held.get("ring") or {}
        if ring.get("id") == item_id and (not call_id or ring.get("call") in (None, call_id)):
            held["ring"] = None
        (held.get("callback") or {}).pop(item_id, None)
        _save_pings(held)
        item = _held()["items"].get(item_id)
        if item and item["status"] == "waiting":
            _write(_held(), {"event": "told", "id": item_id, "by": "call"})
    try:
        from . import server
        server.feed.poke()
    except Exception:
        pass


def ringing():
    """The ring the phone should show now, for RING_SECONDS after it was made, or None."""
    ring = (_pings().get("ring") or None)
    if ring and clock.now() - float(ring.get("at") or 0) < RING_SECONDS:
        return ring
    return None


# An agent's question or permission box that waits this long rings their phone. The owner's rule, 14
# October: The front desk calls once, says which agent is waiting and for what, and answering in the app
# first closes the item, so the call never comes. Quiet hours hold it until they end. Several
# boxes waiting together are one call: ring_check rings for the newest and marks them all rung, and
# the call tells every waiting item.
ASK_CALL_AFTER = 60.0


def escalate_asks(now=None):
    """Turn each ask open ASK_CALL_AFTER seconds into an urgent item, once. Returns their ids."""
    now = now or clock.now()
    if quiet():
        return []
    with _lock:
        st = _held()
        due = [it for it in st["items"].values()
               if it.get("ask") and it["status"] == "waiting" and it["urgency"] == "now"
               and not it.get("escalated") and now - it["added"] >= ASK_CALL_AFTER]
        for it in due:
            _write(st, {"event": "escalated", "id": it["id"]})
    for it in due:
        log.info("inbox: %s's box waited a minute, calling the owner", it["agent"])
    return [it["id"] for it in due]


def tick():
    """One look at the two triggers. Returns the line sent, or None."""
    escalate_asks()
    ring_check()
    # A ring nobody answered in its time is a missed call in that agent's log.
    try:
        from . import agentcall
        agentcall.sweep()
    except Exception:
        log.exception("inbox: the missed calls were not swept")
    now = clock.now()
    local = clock.local()
    with _lock:
        st = _held()
        _sweep(st)
        waiting = [it for it in st["items"].values() if it["status"] == "waiting"]
        if not waiting:
            return None
        held = _pings()
        reasons = []
        today = local.date().isoformat()
        try:
            morning = _hhmm(memory.time_setting("morning_brief"))
        except ValueError:
            morning = None
        cur = local.hour * 60 + local.minute
        if (morning is not None and held.get("morning") != today
                and morning <= cur < morning + MORNING_WINDOW_MINUTES):
            reasons.append("morning")
            held["morning"] = today
        pinged = set(held.get("now") or [])
        fresh = [it["id"] for it in waiting if it["urgency"] == "now"
                 and it["id"] not in pinged and now - it["added"] >= NOW_GRACE]
        if fresh and not quiet(local):
            reasons.append("now")
            pinged.update(fresh)
        if not reasons:
            return None
        held["now"] = sorted(i for i in pinged if i in st["items"]
                             and st["items"][i]["status"] != "closed")
        _save_pings(held)
        text = ping_text(len(waiting))
    ok = _ping(text)
    log.info("inbox ping (%s): %d untold, %s", " and ".join(reasons), len(waiting),
             "sent" if ok else "not sent")
    return text


def _loop():
    while not _stop.is_set():
        try:
            tick()
        except Exception:
            log.exception("inbox tick failed")
        _stop.wait(TICK_SECONDS)


def start():
    _stop.clear()
    thread = threading.Thread(target=_loop, name="inbox", daemon=True)
    thread.start()
    return thread


def stop():
    _stop.set()


def reset():
    """Forget the folded state and the listeners. For tests."""
    global _state
    with _lock:
        _state = None
        _pushed.clear()
    _listeners.clear()
    _stop.set()
    _stop.clear()
