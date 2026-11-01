"""Task owners: every task on the owner's list has an owner agent.

The owner agent keeps its slice of the list true. It creates, moves, ticks and drops its tasks, and
it reminds the owner when one is late. The rules for that live in the house CLAUDE.md; this module
only tells the owner agent what changed, and wakes it when the change cannot wait.

    owner agent the agent that put the task on the list. A task the owner made, or one an alarm
                made with nobody behind it, belongs to the home agent (config.HOME_AGENT)
    report      one message to the owner agent: what changed, then its open tasks, hot ones
                first. Never for a change the owner agent made itself: it knows
    hot         overdue, or due within hot_hours (24 unless Config says otherwise). A report
                about a hot task wakes the owner agent. Any other report waits in its mailbox for
                its next message, quietly, like a fresh job's result
    due time    passing is a change too. One timer, set for the next due time and set again after
                every change. Not a poll. At start the same check catches what passed while the
                daemon was down

Nothing here decides anything for the agent. Agents decide, the daemon serves.
"""

import datetime as _dt
import json
import threading

from . import clock, config, logs

log = logs.get(__name__)

# The agent that owns a task nobody else put there.
def home():
    return config.HOME_AGENT


DEFAULT_HOT_HOURS = 24
# Changes reported, by task event. "mine" is a new task, reported only when someone other than the
# owner made it: the owner typed it on Insights, and the home agent should know.
SAYS = {"mine": "The owner added it to the list", "ticked": "It was ticked off",
        "moved": "It was given a new time", "dismissed": "It was dropped from the list",
        "reopened": "The owner took it up again: it is not done"}
WHO = {"owner": "by the owner", "desk": "by the front desk, from the owner's answer on a call"}

_lock = threading.RLock()
_timer = None


# -- who and when ------------------------------------------------------------------

def owner(task):
    from . import registry, tasks
    if task.get("agent") and task["agent"] != tasks.OWNER_ID:
        who = task["agent"]
    elif task.get("owner"):
        # The owner picked it when adding the task.
        who = task["owner"]
    else:
        who = task.get("by")
    if who and who not in (tasks.OWNER_ID, "desk") and registry.get(who):
        return who
    return home()


def hot_hours():
    from . import tasks
    try:
        hours = float(tasks.hot_hours())
    except (TypeError, ValueError):
        hours = DEFAULT_HOT_HOURS
    return max(0.0, hours)


def is_hot(task, now=None):
    due = task.get("due")
    return bool(due) and due <= (now or clock.now()) + hot_hours() * 3600


def _his_open():
    from . import tasks
    with tasks._lock:
        return [dict(t) for t in tasks._held()["tasks"].values()
                if t["agent"] == tasks.OWNER_ID and t["state"] in ("mine", "owed")]


def _when(due):
    return _dt.datetime.fromtimestamp(due).strftime("%a %d %b %H:%M")


def _line(task, now):
    bits = [task["id"], task["title"]]
    if task.get("due"):
        bits.append("due " + _when(task["due"]) + (" (overdue)" if task["due"] < now else ""))
    if task["state"] == "owed":
        bits.append(f"owed by {task.get('who') or '?'}")
    return ("HOT | " if is_hot(task, now) else "") + " | ".join(bits)


def slice_lines(agent, now=None):
    """This agent's open tasks on the owner's list, hot first, then by due time."""
    now = now or clock.now()
    mine = [t for t in _his_open() if owner(t) == agent]
    mine.sort(key=lambda t: (not is_hot(t, now), t.get("due") or 9e12, t["created"]))
    return [_line(t, now) for t in mine]


# -- the report --------------------------------------------------------------------

def report_text(agent, changes, now=None):
    """changes: [(what happened, task)]."""
    now = now or clock.now()
    lines = ["[Task report] You own these tasks on the owner's list."]
    for what, task in changes:
        due = f", due {_when(task['due'])}" if task.get("due") else ""
        lines.append(f"- {what}: \"{task['title']}\" ({task['id']}{due})")
    open_ = slice_lines(agent, now)
    lines.append("")
    lines.append("Your open tasks on the owner's list, hot first:" if open_ else
                 "You have no open tasks on the owner's list.")
    lines.extend("- " + line for line in open_)
    lines.append("")
    lines.append("A task with a due time has nag runs: each one reminds the owner once, by the task's "
                 "brief, and sets the next. Give a task a brief, a time or nags off with task_nag.")
    lines.append("Act on it as your house CLAUDE.md says under \"You own your tasks\".")
    return "\n".join(lines)


def _deliver(agent, text, hot):
    """Off the caller's thread: a send can wait seconds for a terminal to be ready."""
    from . import broker

    def go():
        try:
            if hot:
                broker.send(agent, text, wake=True)
            else:
                broker.leave(agent, text)
        except Exception:
            log.exception("owners: the report to %s did not go", agent)
    threading.Thread(target=go, name=f"owners-{agent}", daemon=True).start()


def report(task, what, hot=None):
    """One change to one task, reported to its owner."""
    agent = owner(task)
    hot = is_hot(task) if hot is None else hot
    _deliver(agent, report_text(agent, [(what, task)]), hot)
    log.info("owners: %s told of task %s (%s)", agent, task["id"], "woken" if hot else "waits")
    return agent


def on_task(event, payload):
    """The tasks listener: every change to a task on the owner's list, unless its owner agent made it."""
    from . import tasks
    kind = (payload or {}).get("event")
    task = (payload or {}).get("task") or {}
    if task.get("agent") != tasks.OWNER_ID:
        return
    if kind in SAYS:
        actor = payload.get("actor") or tasks.OWNER_ID
        agent = owner(task)
        if actor != agent and not (kind == "mine" and actor != tasks.OWNER_ID):
            what = SAYS[kind]
            if kind == "moved" and task.get("due"):
                what += f", {_when(task['due'])}"
            if kind != "mine":
                what += " " + WHO.get(actor, f"by {tasks._name(actor)}")
            report(task, what)
    if kind in ("mine", "owed", "moved", "ticked", "dismissed", "reopened"):
        arm()


def alarm_unticked(task, label):
    """Half an hour after the owner stopped a task's alarm, the task is still open. The owner
    agent decides."""
    return report(task, f"The owner stopped its alarm \"{label}\" 30 minutes ago and has not ticked it",
                  hot=True)


# -- the due time ------------------------------------------------------------------

def _told_file():
    config.VAR.mkdir(parents=True, exist_ok=True)
    return config.VAR / "task-due-told.json"


def _told():
    try:
        got = json.loads(_told_file().read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_told(told):
    tmp = _told_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(told))
    tmp.replace(_told_file())


def due_check():
    """Every open task whose due time passed and was not reported for that time yet. One report
    per owner, however many fell due at once."""
    now = clock.now()
    with _lock:
        told = _told()
        late = [t for t in _his_open() if t.get("due") and t["due"] <= now
                and told.get(t["id"]) != t["due"]]
        for t in late:
            told[t["id"]] = t["due"]
        if late:
            open_ids = {t["id"] for t in _his_open()}
            _save_told({k: v for k, v in told.items() if k in open_ids})
    by_owner = {}
    for t in late:
        by_owner.setdefault(owner(t), []).append(("Its due time passed and it is not ticked", t))
    from . import nags
    for agent, changes in by_owner.items():
        # A task with nags on has a nag run coming for it now, and that run reminds the owner. The
        # owner agent hears quietly, so the owner is not reminded twice. A task with nags off wakes
        # the owner agent.
        _deliver(agent, report_text(agent, changes, now),
                 any(nags.due_at(t) is None for _, t in changes))
        log.info("owners: %s told %d task(s) fell due", agent, len(changes))
    arm()
    return sum(len(c) for c in by_owner.values())


def arm():
    """The one timer, for the next due time still to come."""
    global _timer
    now = clock.now()
    told = _told()
    coming = [t["due"] for t in _his_open() if t.get("due") and t["due"] > now
              and told.get(t["id"]) != t["due"]]
    with _lock:
        if _timer:
            _timer.cancel()
            _timer = None
        if not coming:
            return None
        wait = min(coming) - now + 1.0
        _timer = threading.Timer(wait, due_check)
        _timer.daemon = True
        _timer.start()
        return wait


def start():
    """At daemon start: listen to the tasks, and report what fell due while it was down."""
    from . import tasks
    tasks.on_change(on_task)
    threading.Thread(target=due_check, name="owners-start", daemon=True).start()


def reset():
    """For tests."""
    global _timer
    with _lock:
        if _timer:
            _timer.cancel()
        _timer = None
