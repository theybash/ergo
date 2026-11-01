"""Nags: a task on their list keeps coming back until it is done.

The assistant reminded them of the printouts with a brief, then booked nothing, and the task went quiet.
A reminder must not be the end of it. So the follow-up lives on the task, as data, and not in a
note an agent may forget to write.

    brief       the owner's words for the nag runs: why it matters, what counts as done, how to
                remind. Written when the task is made, or later with task_nag. None means the
                house default
    next nag    when the next nag run comes. At first the due time; each run sets the next one,
                and when it sets none the daemon puts it an hour on. Never between 23:30 and
                08:00: a nag that falls in the night waits for the morning, and the owner is
                told so at once
    night_ok    the owner asked for this time themselves ("call me at 23:45"), so the night window
                does not apply. The night rule is for reminders agents start, not for ones they ask for
    nag run     a fresh Haiku run in the owner's folder, born for one task and gone when it ends.
                It reads the task, the brief and the log, reminds them once or stands down, writes
                one line to the log and sets the next nag. The owner calls it a Meeseeks
    log         one line from each run, on the task, so the next run knows what was done and what
                they said

It stops when they tick the task, drop it, or the owner turns its nags off. A new due time starts
them again from that time. Calm mode holds every nag for an hour.

"Agents decide, the daemon serves": the run decides call or brief, and when to look again. The
daemon only keeps the time and starts the run.
"""

import datetime as _dt
import queue
import threading

from . import clock, logs

log = logs.get(__name__)

HOUR = 3600
NIGHT_FROM = (23, 30)       # no nag run starts from here...
NIGHT_TO = (8, 0)           # ...until here, their local time
# An alarm of the task this near the nag's time is the reminder already (The owner, 28 October 2026:
# an alarm at 11:10, then the nag run's own at 11:12, "why did it ring 2 minutes later").
ALARM_NEAR = 30 * 60
GAP = 120                   # seconds between two runs, so a pile of late tasks is not one burst
TIMEOUT = 300               # a nag run that takes longer than this is stopped
MODEL = "haiku"

DEFAULT_BRIEF = (
    "No brief from the owner, so the house rule: the first reminder of the day is "
    "an alarm on their phone (set_alarm), later ones a call (call_owner) an hour apart, at most two "
    "calls a day for this task; after that, the next nag is tomorrow morning. Stop when they tick "
    "it, drops it, says stop, or gives a reason or a time."
)

PROMPT = """[nag run for task {id}, a fresh session]

You are a nag run for {owner}, in {owner}'s folder. You exist for one task on the owner's list, and
you end when this run ends. The task stays {owner}'s, and the next nag run reads what you write.

The task: "{title}" ({id}), due {due}.
The owner's brief: {brief}
What the earlier nag runs did, oldest first:
{log}
Now: {now}.

Do this, and nothing else:
1. Run task_list. If the task is gone or ticked, or the log or the task says they said stop or
   gave a reason or a time, do not remind them. Write that in the log, and for a time they gave, set
   the next nag after it.
2. Otherwise remind them once, so that their phone rings. A brief is not a reminder: it waits
   unheard until they next call the front desk, so never use brief to remind them, whatever the owner's
   brief says. The owner's brief says how to ring them, how often and when to stop: do exactly
   that. Add no reminder it does not ask for, and stop no sooner than it says; a brief with no
   limit has none. Where the brief does not say:
   - No alarm yet today for this task: the reminder is an alarm. set_alarm with task_id "{id}",
     the hour and minute two minutes from now, and a label of the task in a few words. It rings
     even on silent, and they can snooze it, stop it, or type an answer to {owner} on it.
   - An alarm already today: the reminder is a call, call_owner. Start it with "{owner}:" and say
     the task in a few words.
   Write "alarm" or "call" in your log line, so the next run can count. Never Telegram.
3. Before you finish, call task_nag with task_id "{id}", a note of one short line on what you did,
   and next_in_minutes for the next look: the gap the brief gives, or 60 when it gives none. If
   you did not remind them because the brief or the log says not to again today, the next look is
   tomorrow 09:00, never sooner: a run that only checks is noise.

Touch no other task, read no mail, and contact nobody but the owner.

Start your reply with one short line, under 60 characters, that says what you did. End your reply
with a line that is exactly `task succeeded` or `task failed`."""

_lock = threading.RLock()
_timer = None
_queue = queue.Queue()
_waiting = set()            # task ids queued or running, so one task never has two runs
_worker = None


# -- when --------------------------------------------------------------------------

def is_night(ts):
    t = _dt.datetime.fromtimestamp(ts)
    hm = (t.hour, t.minute)
    return hm >= NIGHT_FROM or hm < NIGHT_TO


def by_day(ts):
    """The time itself, or the next 08:00 when it falls in the night."""
    if not is_night(ts):
        return ts
    t = _dt.datetime.fromtimestamp(ts)
    morning = t.replace(hour=NIGHT_TO[0], minute=NIGHT_TO[1], second=0, microsecond=0)
    if (t.hour, t.minute) >= NIGHT_FROM:
        morning += _dt.timedelta(days=1)
    return morning.timestamp()


def waits_until(task):
    """When the night window holds this task's next nag, the 08:00 it waits for; else None."""
    when = due_at(task)
    if not when or task.get("night_ok") or not is_night(when):
        return None
    return by_day(when)


def due_at(task):
    """When this task's next nag run comes, or None for no nag."""
    if task.get("nag_off") or not task.get("due"):
        return None
    # Their own tasks only. An owed item is someone else's promise, not a thing to nag them about.
    if task.get("state") != "mine":
        return None
    return task.get("nag_at") or task["due"]


def _open():
    from . import owners
    return owners._his_open()


def _when(ts):
    return _dt.datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


# -- the timer ---------------------------------------------------------------------

def arm():
    """The one timer, for the next nag still to come."""
    global _timer
    now = clock.now()
    coming = [w for w in (due_at(t) for t in _open()) if w]
    with _lock:
        if _timer:
            _timer.cancel()
            _timer = None
        if not coming:
            return None
        wait = max(1.0, min(coming) - now + 1.0)
        _timer = threading.Timer(wait, check)
        _timer.daemon = True
        _timer.start()
        return wait


def check():
    """Every task whose nag is due: in the night or in calm mode it waits, otherwise it queues a
    run. In quiet time it moves to the end. The next nag is set before the run starts, so a run
    that dies still leaves one."""
    from . import calm, quiet
    now = clock.now()
    for task in _open():
        when = due_at(task)
        if not when or when > now or task["id"] in _waiting:
            continue
        night_ok = bool(task.get("night_ok"))
        if is_night(now) and not night_ok:
            _push(task, by_day(now))
            _tell_moved(task, by_day(now))
            continue
        if calm.on():
            _push(task, now + HOUR)
            continue
        end = quiet.until()
        if end:
            _push(task, end + 1)
            continue
        _push(task, now + HOUR if night_ok else by_day(now + HOUR))
        if _alarm_covers(task, now):
            continue
        with _lock:
            _waiting.add(task["id"])
        _queue.put(task["id"])
        _start_worker()
    arm()
    return True


def _push(task, ts):
    from . import owners, tasks
    try:
        tasks.set_nag(task["id"], owners.owner(task), next_at=ts)
    except (KeyError, ValueError):
        pass


def _alarm_covers(task, now):
    """An alarm of this task rings, or rang, near now: this nag is skipped and logged, and the
    next one comes an hour on, as after any run. Only an alarm set since the last nag run counts:
    the alarm a nag run sets itself is that run's reminder, and a brief of every five minutes
    must still ring every five minutes."""
    from . import owners, phone, tasks
    last = max([r.get("at", 0) for r in task.get("nag_log") or []] or [0])
    alarm = phone.rang_near(task["id"], now, ALARM_NEAR, set_after=last)
    if not alarm:
        return False
    try:
        tasks.set_nag(task["id"], owners.owner(task),
                      note=f"skipped: its {alarm['hour']:02d}:{alarm['minute']:02d} alarm is the reminder")
    except (KeyError, ValueError):
        pass
    return True


def _tell_moved(task, ts):
    """The owner hears it at the moment the nag moves, so it can tell them (The owner, 20 Oct 2026:
    the 23:45 call they asked for moved to 08:00 and nobody said so)."""
    from . import broker, owners
    try:
        broker.leave(owners.owner(task),
                     f"[Task report] The nag for \"{task['title']}\" ({task['id']}) moved to "
                     f"{_when(ts)} because of the night window (23:30 to 08:00). If the owner asked "
                     f"for that time themselves, set night_ok true with task_nag and the next "
                     f"nag time you want.")
    except Exception:
        log.exception("nags: the move of %s was not told", task["id"])


def _start_worker():
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_work, name="nags", daemon=True)
        _worker.start()


def _work():
    import time
    while True:
        try:
            task_id = _queue.get(timeout=5)
        except queue.Empty:
            return
        try:
            run(task_id)
        except Exception:
            log.exception("nags: the run for task %s failed", task_id)
        finally:
            with _lock:
                _waiting.discard(task_id)
        if not _queue.empty():
            time.sleep(GAP)


# -- the run -----------------------------------------------------------------------

def prompt(task, owner_name):
    lines = [f"- {_when(r['at'])}: {r['text']}" for r in (task.get("nag_log") or []) if r.get("at")]
    due = _when(task["due"]) + (" (overdue)" if task["due"] < clock.now() else "")
    return PROMPT.format(id=task["id"], owner=owner_name, title=task["title"], due=due,
                         brief=task.get("brief") or DEFAULT_BRIEF,
                         log="\n".join(lines) or "Nothing yet. This is the first nag.",
                         now=_when(clock.now()))


def run(task_id):
    """One nag run for one task. Returns the reply, or None when nothing ran."""
    from . import broker, config, cron, fresh, owners, registry, tasks
    task = tasks.get(task_id)
    # Looked at again as the run starts, not only when it was queued: a task moved to a later
    # time in between is not late any more, and a run then would set its own next nag and undo
    # the move. A nag is for a late task only.
    if not task or not due_at(task) or task["due"] > clock.now() + 60:
        return None
    agent = owners.owner(task)
    rec = registry.get(agent)
    if not rec:
        return None
    seen = len(task.get("nag_log") or [])
    job = {"id": f"nag-{task_id}", "agent": agent, "model": MODEL,
           "task": f"Nag: {task['title']}", "timeout": TIMEOUT}
    started = clock.now()
    got = fresh.run(job, prompt(task, rec.get("name") or agent), TIMEOUT)
    reply = got.get("reply") or ""
    last = (reply.strip().splitlines() or [""])[-1].strip().strip("`.").lower()
    ok = last == config.SUCCEEDED
    outcome = "succeeded" if ok else "failed"
    # The card first, then the log line: the log line is what tells the chat to look again.
    if reply or got.get("error"):
        cron._show(job, started, outcome, reply or f"The nag run did not finish: {got['error']}")
    after = tasks.get(task_id) or {}
    if len(after.get("nag_log") or []) == seen and after.get("state") == "mine":
        first = (reply.strip().splitlines() or [""])[0]
        note = first if reply else f"nag run failed: {got.get('error') or 'no reply'}"
        try:
            tasks.set_nag(task_id, agent, note=note)
        except (KeyError, ValueError):
            pass
    try:
        broker.leave(agent, f"[Nag run] on your task \"{task['title']}\" ({task_id}), "
                            f"{outcome}:\n{reply.strip() or got.get('error') or 'no reply'}")
    except Exception:
        log.exception("nags: the result for %s did not reach %s", task_id, agent)
    log.info("nags: task %s on %s: %s, $%s", task_id, agent, outcome, got.get("cost_usd"))
    arm()
    return reply


# -- start -------------------------------------------------------------------------

def spread():
    """Old late tasks with no nag yet get one at the next hour by day, a few minutes apart, so a
    pile of them never rings their phone at once. Once: after this each has its own time."""
    now = clock.now()
    late = sorted([t for t in _open() if due_at(t) and not t.get("nag_at") and t["due"] <= now],
                  key=lambda t: t["due"])
    base = by_day((int(now // HOUR) + 1) * HOUR)
    for i, task in enumerate(late):
        _push(task, by_day(base + i * 5 * 60))
    return len(late)


def on_task(event, payload):
    if (payload or {}).get("event") in ("mine", "owed", "moved", "ticked", "dismissed",
                                         "reopened", "nag"):
        arm()


def start():
    from . import tasks
    tasks.on_change(on_task)
    spread()
    arm()


def reset():
    """For tests."""
    global _timer
    with _lock:
        if _timer:
            _timer.cancel()
        _timer = None
        _waiting.clear()
    while not _queue.empty():
        _queue.get_nowait()
