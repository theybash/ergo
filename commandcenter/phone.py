"""The phone as a place the fleet can reach: alarms to set, and the notifications it receives.

Two directions, both through the phone app, which already asks the daemon for state every thirty
seconds.

    commands        the daemon queues a thing for the phone to do, the phone fetches it, does it,
                    and says how it went. Today there is one kind: an alarm, set in the phone's own
                    Clock app with no screen shown.
    notifications   with notification access granted on the phone, every notification it receives
                    is sent here and kept, one JSON line each, in var/phone/notifications.jsonl.

The notifications are other people's words: messages from WhatsApp, mail previews, anything. They
stay on this laptop. Nothing here sends them anywhere, and reading them back is a separate route.

    POST /api/phone/alarm                 {hour, minute, label?, task_id?}  -> {ok, id, task, said}
    POST /api/phone/alarm/<id>/stopped                             -> {ok}
    POST /api/phone/alarm/<id>/prompt     {text}                   -> {ok, to}
    POST /api/phone/alarm/<id>/done                                -> {ok, said}
    POST /api/phone/alarm/<id>/cancel                              -> {ok, cancelled, said}
    POST /api/phone/alarms/cancel         {task_id}                -> {ok, cancelled, said}
    GET  /api/phone/commands                                       -> {commands: [...]}
    POST /api/phone/commands/<id>/done    {ok, error?}             -> {ok}
    POST /api/phone/notifications         {items: [...]}           -> {ok, kept}
    GET  /api/phone/notifications?n=50                             -> {items: [...]}
"""

import json
import logging
import threading
import uuid
from collections import deque

from . import clock, config

log = logging.getLogger("cc.phone")

# A command the phone has not fetched in this long is dropped: an alarm asked for last night and
# delivered this afternoon would be the wrong alarm.
COMMAND_TTL = 6 * 3600
TEXT_CHARS = 2000
TITLE_CHARS = 200
# Keys of notifications already kept, so the phone resending a batch after a dropped answer does
# not write the same line twice.
SEEN = 2000

_lock = threading.Lock()
_listeners = []
_listening = None


def on_change(fn):
    """Called with no arguments when a command is queued or done, so the phone hears at once."""
    _listeners.append(fn)


def on_listening(fn):
    """`fn()` says whether Ergo holds the phone's stream open now. Alarms reach the phone only
    on that stream, so an alarm queued while nobody listens is checked within a minute."""
    global _listening
    _listening = fn


def _phone_listens():
    try:
        return _listening is None or bool(_listening())
    except Exception:
        return True


def _changed():
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass
_seen = deque(maxlen=SEEN)
_seen_set = set()


def _dir():
    config.PHONE_DIR.mkdir(parents=True, exist_ok=True)
    return config.PHONE_DIR


def _commands_file():
    return _dir() / "commands.json"


def _notes_file():
    return _dir() / "notifications.jsonl"


def _load():
    try:
        got = json.loads(_commands_file().read_text())
        return got if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def _save(rows):
    tmp = _commands_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows))
    tmp.replace(_commands_file())


def _clean(value, count):
    return " ".join(str(value or "").split())[:count]


# -- alarms ----------------------------------------------------------------------

# How long before an alarm is due the daemon checks that the phone has taken it. On 9 October a
# 15:59 alarm reached the phone at 16:00, because its live channel had died quietly, and nobody
# was told. Now a miss is said on Telegram, which does not depend on that channel.
CHECK_BEFORE = 30
# On 18 October the 10:00, 10:05 and 11:00 alarms waited for hours: Ergo's stream was down while
# its notifications still came in, and the check 30 s before each alarm was the first anyone heard.
# With nobody on the stream the check comes this soon instead. Ergo reconnects within a minute.
DEAF_CHECK = 90


def _due(hour, minute, at):
    """When the alarm rings: the next time this hour and minute comes round after `at`."""
    import datetime
    start = datetime.datetime.fromtimestamp(at)
    due = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due.timestamp() <= at:
        due += datetime.timedelta(days=1)
    return due.timestamp()


def _report(row, why):
    """Say once that an alarm will not ring: to the owner by a call, and to the agent that set it.

    A call, not a Telegram message: he does not look at Telegram, and an alarm
    that will not ring is the one thing that cannot wait for him to notice."""
    with _lock:
        rows = _load()
        mine = next((r for r in rows if r.get("id") == row["id"]), None)
        if mine is None or mine.get("reported"):
            return
        mine["reported"] = True
        _save(rows)
    when = f"{row['hour']:02d}:{row['minute']:02d}"
    what = f" ({row['label']})" if row.get("label") else ""
    text = f"The {when} alarm{what} will not ring: {why}. Set it yourself."
    log.info("phone: alarm %s reported as not ringing", row["id"])
    by = row.get("by")
    try:
        from . import inbox, registry
        caller = by if by and registry.get(by) else config.HOME_AGENT
        # The item is about the alarm, so it ends near the alarm's time: when the ring is no
        # longer offered to the phone, and not 2 hours later. This check runs 30 seconds before
        # the alarm, so the ring needs its own time on top.
        due = _due(row["hour"], row["minute"], row.get("at") or clock.now())
        inbox.add(caller, text, urgency="urgent", when=due,
                  expires=max(due, clock.now()) + inbox.RING_SECONDS)
    except Exception:
        log.exception("phone: the call about alarm %s failed", row["id"])
    if by and by != "desk":
        try:
            from . import broker, registry
            if registry.get(by):
                broker.send(by, f"[Alarm failed] {text}", wake=False)
        except Exception:
            log.exception("phone: telling %s about alarm %s failed", by, row["id"])


def _check(command_id):
    """Shortly before it is due: has the phone taken the alarm? If not, say so now."""
    with _lock:
        row = next((r for r in _load() if r.get("id") == command_id), None)
    if not row or row.get("state") != "waiting":
        return
    listens = _phone_listens()
    due = _due(row["hour"], row["minute"], row.get("at", 0))
    left = due - clock.now() - CHECK_BEFORE
    if listens and left > 1:
        # The early check, and Ergo is back: it gets the alarm, so look again near the time.
        _arm_check(command_id, left)
        return
    _report(row, "it has not reached your phone" if listens else
            "Ergo on your phone is not connected to the laptop")


def _arm_check(command_id, wait):
    timer = threading.Timer(wait, _check, args=(command_id,))
    timer.daemon = True
    timer.start()


def _alarms_file():
    return _dir() / "alarms.json"


def _alarms():
    try:
        got = json.loads(_alarms_file().read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_alarms(rows):
    # An alarm is kept three days past its time: long enough for a snooze, a stop and the check.
    now = clock.now()
    rows = {k: v for k, v in rows.items() if now - v.get("due", now) < KEEP_ALARM}
    tmp = _alarms_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows))
    tmp.replace(_alarms_file())


def _task_for(title, task_id, who, due):
    """The task on his list this alarm is for: the one asked for, or a new one, or the open one
    that already has this title. Every alarm has one."""
    from . import tasks
    if task_id:
        task = tasks.get(_clean(task_id, 32))
        if not task or task.get("agent") != tasks.OWNER_ID:
            raise ValueError(f"There is no task of his called {task_id}.")
        return task
    try:
        return tasks.add_mine(title, by=who, due=due)
    except KeyError:
        return tasks.add_mine(title, due=due)
    except ValueError:
        same = tasks.find_mine(title)
        if same:
            return same
        raise


def alarm(hour, minute, label=None, by=None, task_id=None):
    """Queue an alarm for the phone to set. The Clock app sets it for the next time that hour and
    minute comes round, today or tomorrow, so the phone never has to know the date.

    An alarm is linked to a task on his list. With a task_id it is that task; with a label, a task
    with the label, due when it rings, is made for it. The alarm screen shows the task. A bare
    wake-up, with no label and no task_id, has no task: a call every morning at half past six
    asking whether he woke up would be noise."""
    try:
        hour, minute = int(hour), int(minute)
    except (TypeError, ValueError):
        raise ValueError("An alarm needs an hour and a minute, as numbers.")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("An alarm needs an hour from 0 to 23 and a minute from 0 to 59.")
    label = _clean(label, 60)
    when = f"{hour:02d}:{minute:02d}"
    by = _clean(by, 64)
    at = clock.now()
    due = _due(hour, minute, at)
    moved = ""
    from . import quiet
    end = quiet.until()
    if end and due < end:
        # Quiet time: the alarm rings when it ends, on the next whole minute.
        import datetime
        ring = datetime.datetime.fromtimestamp(end + 59).replace(second=0, microsecond=0)
        moved = f" The owner asked for quiet until {quiet.hhmm(end)}, so it rings at {ring:%H:%M} instead of {when}."
        hour, minute = ring.hour, ring.minute
        when = f"{hour:02d}:{minute:02d}"
        due = _due(hour, minute, at)
    who = by if by and by != "desk" else None
    task = _task_for(label, task_id, who, due) if (label or task_id) else None
    row = {"id": uuid.uuid4().hex[:10], "kind": "alarm", "hour": hour, "minute": minute,
           "label": label or (task["title"][:60] if task else ""), "at": at, "state": "waiting",
           "by": by, "task": task["id"] if task else "", "title": task["title"] if task else ""}
    with _lock:
        rows = [r for r in _load() if clock.now() - r.get("at", 0) < COMMAND_TTL]
        rows.append(row)
        _save(rows)
        kept = _alarms()
        kept[row["id"]] = {"id": row["id"], "hour": hour, "minute": minute, "label": row["label"],
                           "by": by, "task": row["task"], "title": row["title"], "due": due,
                           "set": at}
        _save_alarms(kept)
    log.info("phone: alarm %s queued for %s", row["id"], when)
    _changed()
    # The check runs CHECK_BEFORE seconds before it is due, or in 20 s for an alarm a minute away.
    wait = max(20.0, due - at - CHECK_BEFORE)
    said = f"The alarm for {when} goes to the phone within half a minute."
    if not _phone_listens():
        wait = min(wait, DEAF_CHECK)
        log.info("phone: alarm %s queued while Ergo is not connected", row["id"])
        said = (f"The alarm for {when} is queued, but Ergo on the phone is not connected now. "
                "If it is still not connected in a minute and a half, the owner is told.")
    _arm_check(row["id"], wait)
    if task:
        said += f" It is linked to the task \"{task['title']}\"."
    return {"ok": True, "id": row["id"], "task": row["task"], "said": said + moved}


def quiet():
    """Tell the phone when quiet time ends, or that it is over: Ergo moves the alarms it has
    booked in the window to the end. A newer one replaces the one not yet fetched."""
    from . import quiet as q
    end = q.until()
    row = {"id": uuid.uuid4().hex[:10], "kind": "quiet", "until": int(end * 1000) if end else 0,
           "at": clock.now(), "state": "waiting"}
    with _lock:
        rows = [r for r in _load() if clock.now() - r.get("at", 0) < COMMAND_TTL
                and not (r.get("kind") == "quiet" and r.get("state") == "waiting")]
        rows.append(row)
        _save(rows)
    log.info("phone: quiet %s queued", row["until"] or "off")
    _changed()
    return row


# -- after it rings: the prompt box, and the check half an hour after Stop -------

# How long after Stop the daemon looks at the alarm's task. Not ticked by then, the front desk calls.
CHECK_AFTER = 30 * 60
KEEP_ALARM = 3 * 86400


def _base(alarm_id):
    """A snooze rings under "<id>-snooze-<ms>"; it is still the same alarm."""
    return _clean(alarm_id, 80).split("-snooze-")[0]


def _setter(entry):
    """The agent that set the alarm, or the home agent when the front desk or nobody did."""
    from . import registry
    by = (entry or {}).get("by")
    return by if by and by != "desk" and registry.get(by) else config.HOME_AGENT


def prompt(alarm_id, text):
    """What he typed or said in the alarm screen's box. It goes to the agent that set the alarm."""
    text = " ".join(str(text or "").split())[:TEXT_CHARS]
    if not text:
        raise ValueError("Say something first.")
    with _lock:
        entry = _alarms().get(_base(alarm_id))
    if not entry:
        raise ValueError("That alarm is not known here any more.")
    to = _setter(entry)
    when = f"{entry['hour']:02d}:{entry['minute']:02d}"
    what = (f"The alarm is for his task \"{entry['title']}\", task {entry['task']}"
            if entry.get("task") else "The alarm has no task")
    from . import broker, registry
    if not registry.get(to):
        raise ValueError(f"There is no agent called {to} to take it.")
    broker.send(to, f"[the owner, from the {when} alarm screen. {what}] {text}", wake=True)
    log.info("phone: a prompt from alarm %s went to %s", entry["id"], to)
    return {"ok": True, "to": to}


def stopped(alarm_id):
    """He pressed Stop. In CHECK_AFTER the task is looked at; not ticked, its owner is told."""
    key = _base(alarm_id)
    with _lock:
        kept = _alarms()
        entry = kept.get(key)
        if not entry:
            return {"ok": False, "error": "no such alarm"}
        entry["stopped"] = clock.now()
        entry.pop("checked", None)
        _save_alarms(kept)
    _arm(key, CHECK_AFTER)
    log.info("phone: alarm %s stopped", key)
    return {"ok": True}


def ticked(alarm_id):
    """He pressed Done (The owner, 28 October 2026: "In alarm give me option of done"). The alarm's
    task is ticked as his own act, so its owner is told, its other alarms are taken back and its
    nags end. The alarm counts as stopped and checked, so nobody asks him about it again."""
    from . import tasks
    key = _base(alarm_id)
    with _lock:
        kept = _alarms()
        entry = kept.get(key)
        if not entry:
            raise ValueError("That alarm is not known here any more.")
        if not entry.get("task"):
            raise ValueError("This alarm has no task to tick.")
        entry["stopped"] = entry["checked"] = clock.now()
        _save_alarms(kept)
    task = tasks.get(entry["task"])
    if not task:
        raise ValueError("The alarm's task is gone.")
    tasks.tick(task["id"])
    log.info("phone: alarm %s done, task %s ticked", key, task["id"])
    return {"ok": True, "said": f"Ticked: {task['title']}"}


def rang_near(task_id, at, window, set_after=0):
    """An alarm of this task, not taken back, due within `window` seconds of `at` and set after
    `set_after`, or None."""
    with _lock:
        rows = [a for a in _alarms().values() if a.get("task") == task_id
                and not a.get("cancelled") and abs(a.get("due", 0) - at) <= window
                and a.get("set", 0) > set_after]
    return min(rows, key=lambda a: abs(a.get("due", 0) - at)) if rows else None


def _arm(key, wait):
    timer = threading.Timer(max(1.0, wait), stop_check, args=(key,))
    timer.daemon = True
    timer.start()


def rearm():
    """At start: every stopped alarm whose check has not run is armed again."""
    with _lock:
        kept = _alarms()
    now = clock.now()
    for key, entry in kept.items():
        # A check the daemon missed by more than an hour, while it was down, is dropped: a call
        # at start-up about an alarm from hours ago would come at any hour.
        if entry.get("stopped") and not entry.get("checked") \
                and now - entry["stopped"] < CHECK_AFTER + 3600:
            _arm(key, entry["stopped"] + CHECK_AFTER - now)


def stop_check(key):
    """Half an hour after Stop. If the task is still open, its owner is told and woken, and
    decides whether to call. Once only. The owner can tick it on his yes;
    a call from here could not."""
    from . import owners, tasks
    with _lock:
        kept = _alarms()
        entry = kept.get(key)
        if not entry or not entry.get("task") or not entry.get("stopped") or entry.get("checked") \
                or entry.get("cancelled"):
            return None
        if clock.now() - entry["stopped"] < CHECK_AFTER - 5:
            return None
        entry["checked"] = clock.now()
        _save_alarms(kept)
    task = tasks.get(entry["task"])
    if not task or task.get("state") != "mine":
        return None
    to = owners.alarm_unticked(task, entry.get("label") or entry["title"])
    log.info("phone: alarm %s not done 30 minutes after stop, %s told", key, to)
    return to


def alarm_for_task(task_id):
    """The newest alarm linked to this task, or None."""
    with _lock:
        rows = [a for a in _alarms().values() if a.get("task") == task_id]
    return max(rows, key=lambda a: a.get("due", 0)) if rows else None


# -- taking an alarm back ----------------------------------------------------------

# An alarm due this long ago may still have a snooze booked on the phone, so it still counts as
# not rung yet. Snoozes are ten minutes; an hour covers a few of them.
SNOOZE_WINDOW = 3600


def _open(entry, now):
    """Booked on the phone and not finished: not stopped, not taken back, not long past."""
    return not entry.get("stopped") and not entry.get("cancelled") \
        and entry.get("due", 0) > now - SNOOZE_WINDOW


def open_alarms(task_id):
    """Every alarm linked to this task that has not rung yet, soonest first."""
    now = clock.now()
    with _lock:
        rows = [a for a in _alarms().values() if a.get("task") == task_id and _open(a, now)]
    return sorted(rows, key=lambda a: a.get("due", 0))


def cancel(alarm_id):
    """Take an alarm back. The phone gets a cancel_alarm command: Ergo unbooks it and any snooze
    of it. An alarm command the phone has not fetched yet is never handed out."""
    key = _base(alarm_id)
    with _lock:
        kept = _alarms()
        entry = kept.get(key)
        if not entry:
            raise ValueError("That alarm is not known here any more.")
        when = f"{entry['hour']:02d}:{entry['minute']:02d}"
        if entry.get("cancelled"):
            return {"ok": True, "cancelled": [key], "said": f"The {when} alarm was already cancelled."}
        entry["cancelled"] = clock.now()
        _save_alarms(kept)
        rows = [r for r in _load() if clock.now() - r.get("at", 0) < COMMAND_TTL]
        for r in rows:
            # Not fetched yet: it never reaches the phone, and its check says nothing.
            if r.get("id") == key and r.get("state") == "waiting":
                r["state"] = "cancelled"
        # Sent anyway: the phone may hold the alarm without having said so yet.
        rows.append({"id": uuid.uuid4().hex[:10], "kind": "cancel_alarm", "alarm": key,
                     "at": clock.now(), "state": "waiting"})
        _save(rows)
    log.info("phone: alarm %s cancelled", key)
    _changed()
    return {"ok": True, "cancelled": [key], "said": f"The {when} alarm is cancelled."}


def cancel_for_task(task_id, keep_due=None):
    """Take back every alarm of this task that has not rung yet. With keep_due, one set for that
    time stays: the task moved there, so it is not stale."""
    gone = []
    for entry in open_alarms(_clean(task_id, 32)):
        if keep_due is not None and abs(entry.get("due", 0) - keep_due) < 60:
            continue
        gone += cancel(entry["id"])["cancelled"]
    said = (f"{len(gone)} alarm{'s' if len(gone) != 1 else ''} cancelled." if gone
            else "No alarm of this task is still to ring.")
    return {"ok": True, "cancelled": gone, "said": said}


def on_task(event, payload):
    """A task ticked or dropped needs no alarm; a task moved keeps only one at its new time.
    Without this he was woken for things already done."""
    payload = payload or {}
    task = payload.get("task") or {}
    what = payload.get("event")
    if not task.get("id") or what not in ("ticked", "dismissed", "moved"):
        return
    try:
        cancel_for_task(task["id"], task.get("due") if what == "moved" else None)
    except Exception:
        log.exception("phone: cancelling the alarms of task %s failed", task["id"])


def start():
    """At daemon start: listen to the tasks, so a finished task takes its alarms back."""
    from . import tasks
    tasks.on_change(on_task)
    rearm()


# -- float ---------------------------------------------------------------------

def float_agent(agent):
    """Float this agent's face over every app on the phone, as the Float button does. The agent
    asks for it with float_me (The owner, 31 October 2026: "Do agents have a tool to become
    bubble?" ... "Yeah, both": when he asks, and on their own)."""
    from . import registry
    rec = registry.get(agent) if agent else None
    if not rec:
        raise ValueError("no such agent")
    row = {"id": uuid.uuid4().hex[:10], "kind": "float", "agent": agent,
           "name": _clean(rec.get("name") or agent, 80), "at": clock.now(), "state": "waiting"}
    with _lock:
        # One waiting float per agent is enough.
        rows = [r for r in _load() if clock.now() - r.get("at", 0) < COMMAND_TTL
                and not (r.get("kind") == "float" and r.get("agent") == agent and r.get("state") == "waiting")]
        rows.append(row)
        _save(rows)
    log.info("phone: float %s queued", agent)
    _changed()
    return {"ok": True, "id": row["id"],
            "said": "Your face goes over his screen the next time his phone hears from Ergo, "
                    "if he has let Ergo draw over other apps. Tap it and he is in your chat."}


# -- notices -------------------------------------------------------------------

def notice(title, text):
    """A full-screen notice on the phone, quiet, with one button. The quota alert uses it."""
    row = {"id": uuid.uuid4().hex[:10], "kind": "notice", "title": _clean(title, 80),
           "text": _clean(text, 400), "at": clock.now(), "state": "waiting"}
    with _lock:
        rows = [r for r in _load() if clock.now() - r.get("at", 0) < COMMAND_TTL]
        rows.append(row)
        _save(rows)
    log.info("phone: notice %s queued", row["id"])
    _changed()
    return {"ok": True, "id": row["id"]}


def commands():
    """What the phone should do now: every command still waiting, oldest first."""
    with _lock:
        now = clock.now()
        rows = _load()
        live = [r for r in rows if now - r.get("at", 0) < COMMAND_TTL]
        if len(live) != len(rows):
            _save(live)
        return {"commands": [r for r in live if r.get("state") == "waiting"]}


def done(command_id, body):
    """The phone did it, or could not. Either way it is not handed out again.

    "held" means the phone has it but its screen was off or locked, and Android drops a locked
    phone's request to the Clock app without an error. The phone sets it at the next unlock and
    then says done again. Until then the command is neither waiting nor done."""
    body = body if isinstance(body, dict) else {}
    ok = bool(body.get("ok"))
    held = ok and bool(body.get("held"))
    with _lock:
        rows = _load()
        for r in rows:
            if r.get("id") == command_id:
                r["state"] = "held" if held else "done" if ok else "failed"
                r["error"] = _clean(body.get("error"), 200) if not ok else ""
                r["finished"] = clock.now()
                _save(rows)
                log.info("phone: %s %s %s", r.get("kind"), command_id,
                         r["state"] if ok else f"failed, {r['error']}")
                _changed()
                if not ok and r.get("kind") == "alarm":
                    threading.Thread(target=_report, args=(dict(r), r["error"] or "the phone refused it"),
                                     daemon=True).start()
                return {"ok": True}
    return {"ok": False, "error": "no such command"}


def last_command(command_id):
    """One command as it stands, for a caller that wants to know whether it went."""
    with _lock:
        for r in _load():
            if r.get("id") == command_id:
                return r
    return None


# -- notifications ---------------------------------------------------------------

def _prime():
    """The keys already on disk, so a restart does not forget what it kept."""
    if _seen:
        return
    try:
        lines = _notes_file().read_text().splitlines()[-SEEN:]
    except OSError:
        return
    for line in lines:
        try:
            key = json.loads(line).get("key")
        except ValueError:
            continue
        if key and key not in _seen_set:
            _seen.append(key)
            _seen_set.add(key)


def _remember(key):
    if len(_seen) == _seen.maxlen:
        _seen_set.discard(_seen[0])
    _seen.append(key)
    _seen_set.add(key)


def keep(body):
    """Append the phone's notifications, one line each, skipping any already kept."""
    body = body if isinstance(body, dict) else {}
    items = body.get("items")
    if not isinstance(items, list):
        raise ValueError("Send the notifications as a list under items.")
    kept = 0
    now = clock.now()
    with _lock:
        _prime()
        lines = []
        for it in items[:200]:
            if not isinstance(it, dict):
                continue
            pkg = _clean(it.get("package"), 120)
            posted = it.get("posted")
            key = _clean(it.get("key"), 200) or f"{pkg}:{posted}:{_clean(it.get('title'), 40)}"
            key = f"{key}:{posted}"
            if key in _seen_set:
                continue
            row = {"key": key, "package": pkg, "app": _clean(it.get("app"), 80),
                   "title": _clean(it.get("title"), TITLE_CHARS),
                   "text": _clean(it.get("text"), TEXT_CHARS),
                   "posted": posted, "received": now}
            if it.get("conversation"):
                row["conversation"] = _clean(it.get("conversation"), TITLE_CHARS)
            if not (row["title"] or row["text"]):
                continue
            lines.append(json.dumps(row, ensure_ascii=False))
            _remember(key)
            kept += 1
        if lines:
            with _notes_file().open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    if kept:
        # The count only. The words are other people's and the log is not the place for them.
        log.info("phone: %d notifications kept", kept)
    return {"ok": True, "kept": kept}


def recent(count=50):
    """The newest notifications kept, newest last."""
    try:
        lines = _notes_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"items": []}
    out = []
    for line in lines[-max(1, min(int(count), 500)):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return {"items": out}


def reset():
    """For tests: forget the keys held in memory."""
    global _listening
    _listening = None
    with _lock:
        _seen.clear()
        _seen_set.clear()
