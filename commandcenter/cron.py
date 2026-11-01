"""The schedule: work that happens when nobody is watching.

One job names one agent, a task, and either a time of day or an interval. When it is due the agent
is woken, given the task, and put back to sleep if it had been asleep.

Six answers from the daemon grill are built into this file, and each one is the difference between
this scheduler and the obvious one:

    1   A missed job never runs late on its own. It says it was missed and offers a rerun.
    9   Three failures send three messages. Each run reports for itself.
    32  Four agents wake at once, because each costs 400 to 700 MB.
    33  A job in flight when the daemon restarts is a missed run, not a run.
    37  The agent says `task succeeded` or `task failed`. The platform does not guess.
    41  A job wakes its agent, and puts it back to sleep afterwards.
    43  An agent that was already awake stays awake.

Answer 1 is the one most people would get wrong. A scheduler that catches up on start is the
normal design, and here it would mean opening the laptop at nine and having six agents wake at
once to do work whose moment has passed.
"""

import json
import math
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import clock, config, logs, status, telegram

log = logs.get(__name__)

_lock = threading.RLock()
_jobs = None
_stop = threading.Event()
_listeners = []

# Answer 32. Held for the whole run, not just the wake, because the memory is held for the whole
# run too. Five jobs at 07:00 means four run and the fifth waits for a slot.
_slots = threading.BoundedSemaphore(config.WAKE_AT_ONCE)

AT = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# The code word, answer 37. On a line of its own, because an agent that writes "I will end with
# task succeeded" in the middle of a plan has not finished anything yet. Backticks or bold around it
# still count: the brief shows the words in backticks, and a small model copies them.
OUTCOME = re.compile(r"^\s*[`*_]*task\s+(succeeded|failed)[`*_]*\s*[.!]?\s*$",
                     re.IGNORECASE | re.MULTILINE)


def on_change(fn):
    _listeners.append(fn)


def _emit():
    for fn in list(_listeners):
        try:
            fn("jobs", None)
        except Exception:
            pass


# -- the file ---------------------------------------------------------------------

def _load():
    global _jobs
    if _jobs is not None:
        return _jobs
    config.ensure_dirs()
    _jobs = {}
    try:
        raw = json.loads(config.JOBS_FILE.read_text())
        for job in raw.get("jobs", []):
            merged = _blank()
            merged.update(job)
            _jobs[merged["id"]] = merged
    except FileNotFoundError:
        pass
    except Exception as exc:
        bad = config.JOBS_FILE.with_suffix(".broken")
        try:
            config.JOBS_FILE.rename(bad)
            log.error("%s unreadable (%s); moved to %s", config.JOBS_FILE, exc, bad)
        except Exception:
            pass
    return _jobs


def _save():
    config.ensure_dirs()
    tmp = config.JOBS_FILE.with_suffix(".tmp")
    # A version field from the start. agents.json has one and mailbox.json does not, and the one
    # without it is the one that will break silently when a field changes.
    tmp.write_text(json.dumps({"version": 1, "jobs": list(_load().values())}, indent=2))
    tmp.replace(config.JOBS_FILE)


def _blank(**kw):
    job = {
        "id": None,
        "agent": None,
        "task": None,
        "at": None,            # "07:00", daily
        "every": None,         # seconds, an interval
        "once": None,          # a timestamp: runs once then, and never again
        # A path or glob: runs when files there change, see watch.py. It has no next time.
        "watch": None,
        "debounce": None,      # seconds after the last change; None is config.WATCH_DEBOUNCE
        "fires": None,         # when a watch job ran this past hour, for its brake
        "changes": None,       # the paths that changed before its last run, the first 20
        "changes_more": None,  # how many more changed than the list holds
        # A shell command the daemon runs first, in the agent's folder. When it prints nothing,
        # nobody wakes and the run costs nothing. When it prints, the run is told what it printed.
        # Built for jobs that poll: the meetings job ran a full session every 30 minutes to learn
        # that a script printed nothing, 192 sessions and $9 in four days.
        "check": None,
        "skips": 0,            # runs the check skipped, because it printed nothing
        # Which days it runs, checked by the daemon so a wrong day costs no wake at all. Weekdays
        # as 0 (Monday) to 6 (Sunday); None is every day. `start` and `until` are dates, inclusive.
        "days": None,
        "start": None,
        "until": None,
        # Runs again this many times, RETRY_SECONDS apart, after a run that did not succeed: a
        # site that was down, a timeout. Back to zero after a success.
        "retries": 0,
        "retry_count": 0,
        "retrying": False,
        # fresh: a new short `claude -p` in the agent's folder, see fresh.py. continuity: the
        # agent's main conversation, woken if it sleeps. A job written before fresh runs existed
        # has no kind, and loads as continuity so it behaves exactly as it did.
        "kind": "continuity",
        "model": None,         # fresh only: haiku, sonnet, opus or a full id
        "reason": None,        # why a repeating job needs the main conversation
        "enabled": True,
        "timeout": config.JOB_TIMEOUT,
        # Success is reported as well as failure, because the answer is usually the point of the
        # job. quiet silences success only; a failure always reports.
        "quiet": False,
        # Where a fresh run's result goes. context: the agent's mailbox, so it reaches the agent
        # with his next message. chat: the same, and also a card in the agent's chat for the owner,
        # so what he sees and what the agent knows are the same thing.
        "show": "context",
        "created": clock.now(),
        "next": None,
        "last_fire": None,
        # How far the missed-run check has looked. Without it, every restart counts the same missed
        # 3am run again and sends the same message again, and three restarts in one morning read as
        # three separate failures. last_fire cannot do this job: writing it would claim the job ran.
        "checked": None,
        "last_ran": None,      # did the run happen at all
        "last_outcome": None,  # succeeded, failed, unclear, no reply, missed
        "last_summary": None,
        "runs": 0,
        "fails": 0,
        "missed": 0,
    }
    job.update(kw)
    return job


def _record(entry):
    with open(config.JOB_LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


# -- when ------------------------------------------------------------------------

def _jitter(job_id):
    """A few seconds, the same few every day.

    Five jobs set for 07:00 would otherwise all fire in one tick and race for the four slots, and
    which one lost would change every morning. Derived from the id, so a job that looks late is
    late for a reason that stays the same.
    """
    return int(uuid.UUID(hex=job_id.ljust(32, "0")).int % config.JITTER_SECONDS)


RETRY_SECONDS = 15 * 60
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_days(value):
    """A list of weekdays, 0 for Monday, from "daily", "weekdays", "weekends", "mon,wed,fri", or a
    list of numbers or names. None means every day. Raises ValueError on anything else."""
    if value in (None, "", "daily", "every day", []):
        return None
    if value == "weekdays":
        return [0, 1, 2, 3, 4]
    if value == "weekends":
        return [5, 6]
    items = value.replace(" ", "").split(",") if isinstance(value, str) else list(value)
    out = set()
    for item in items:
        if isinstance(item, int) and 0 <= item <= 6:
            out.add(item)
        elif isinstance(item, str) and item.lower()[:3] in DAY_NAMES:
            out.add(DAY_NAMES.index(item.lower()[:3]))
        else:
            raise ValueError(f"days are daily, weekdays, weekends, or names like mon,wed,fri, not {item!r}")
    return sorted(out) or None


def parse_date(value, name):
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{name} is a date like 2026-10-20")


def parse_once(value, in_minutes=None):
    """A one-off time: "2026-10-16 09:00", an ISO time, a timestamp, or minutes from now."""
    if in_minutes is not None:
        minutes = float(in_minutes)
        if minutes < 1:
            raise ValueError("in_minutes is at least 1")
        return clock.now() + minutes * 60
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19 if fmt.endswith("%S") else 16], fmt).timestamp()
        except ValueError:
            continue
    raise ValueError("once is a time like 2026-10-16 09:00")


def _allowed(job, ts):
    """Whether a run at `ts` falls on one of the job's days and inside its dates."""
    day = datetime.fromtimestamp(ts)
    if job.get("days") and day.weekday() not in job["days"]:
        return False
    stamp = day.strftime("%Y-%m-%d")
    if job.get("start") and stamp < job["start"]:
        return False
    if job.get("until") and stamp > job["until"]:
        return False
    return True


def _over(job, ts):
    """Past the job's last date, so it will never run again."""
    return bool(job.get("until")) and datetime.fromtimestamp(ts).strftime("%Y-%m-%d") > job["until"]


# A job that will never run again leaves the list (The owner, 1 November 2026: "Remove the cron that
# never runs again and have run? What's the point of showing those jobs"). A one-off goes when its
# run ends; one that was missed, or a repeat past its last date, goes this long after its last
# moment, which also keeps a run still going safe from the sweep.
SPENT_AFTER = 6 * 3600


def _spent(job, now, margin=0):
    """Will never run again: a one-off whose time has passed, or a repeat past its last date. A
    retry still due, or a watch, is not spent."""
    if job.get("watch") or job.get("retrying") or job.get("next"):
        return False
    if job.get("once"):
        return float(job["once"]) <= now - margin
    return _over(job, now - margin)


def _sweep(now):
    """Take spent jobs off the list, each one long past. Returns how many went."""
    with _lock:
        jobs = _load()
        gone = [k for k, j in jobs.items() if _spent(j, now, SPENT_AFTER)]
        for k in gone:
            jobs.pop(k)
        if gone:
            _save()
    if gone:
        log.info("swept %d spent job(s)", len(gone))
        _emit()
    return len(gone)


def _next_after(job, after):
    """The first time this job is due, strictly after `after`, on an allowed day. None if never."""
    if job.get("once"):
        return float(job["once"]) if float(job["once"]) > after else None
    when = _next_raw(job, after)
    for _ in range(400):
        if when is None or _over(job, when):
            return None
        if _allowed(job, when):
            return when
        if job.get("every"):
            # The start of the next day, then the first step after it.
            nxt = datetime.fromtimestamp(when).replace(hour=0, minute=0, second=0,
                                                         microsecond=0) + timedelta(days=1)
            when = _next_raw(job, nxt.timestamp() - 1)
        else:
            when = _next_raw(job, when)
    return None


def _next_raw(job, after):
    """The next time by the clock alone, before days and dates are considered."""
    if job.get("at"):
        m = AT.match(job["at"])
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2))
        when = datetime.fromtimestamp(after).replace(
            hour=hour, minute=minute, second=0, microsecond=0) + timedelta(
            seconds=_jitter(job["id"]))
        if when.timestamp() <= after:
            when += timedelta(days=1)
        return when.timestamp()
    if job.get("every"):
        step = float(job["every"])
        base = job.get("last_fire") or job.get("created") or after
        if base > after:
            return base + step
        # How many whole steps have passed, so a daemon that was down for a day does not walk
        # through a thousand of them one at a time.
        return base + step * (math.floor((after - base) / step) + 1)
    return None


def _due_between(job, start, end):
    """How many times this job was due in (start, end]. Used to say how much was missed."""
    if not start or end <= start:
        return 0
    if job.get("once"):
        return 1 if start < float(job["once"]) <= end else 0
    if job.get("every") and not (job.get("days") or job.get("start") or job.get("until")):
        return int((end - start) // float(job["every"]))
    if job.get("at") or job.get("every"):
        # Walk the days. A daily job cannot be due more than once a day, so the count is small
        # even after a fortnight with the laptop shut.
        #
        # The jitter is added to the starting point, not to each step. Without it, a run recorded a
        # few seconds before its own jittered boundary counts that same boundary as missed, and the
        # operator gets a 9am message about a job that ran. A false alarm is worse than a quiet one
        # here: the page shows the outcome either way, and an alert that cries wolf stops being read.
        count, cursor = 0, start + _jitter(job["id"])
        while count < 400:
            cursor = _next_after(job, cursor)
            if cursor is None or cursor > end:
                break
            count += 1
        return count
    return 0


def _reschedule(job, after=None):
    job["next"] = _next_after(job, after or clock.now())


# -- the public list -------------------------------------------------------------

def all():
    with _lock:
        return [dict(j) for j in _load().values()]


def get(job_id):
    with _lock:
        job = _load().get(job_id)
        return dict(job) if job else None


KINDS = ("fresh", "continuity")
SHOWS = ("context", "chat")


def _show_for(show, kind):
    """Where a job's result goes, or a ValueError. Only a fresh run can keep out of the context:
    a run in the main conversation is in the context already."""
    show = str(show or "context").strip()
    if show not in SHOWS:
        raise ValueError("show is context or chat")
    if show == "chat" and kind != "fresh":
        raise ValueError("only a fresh job can show in the chat: a run in the main conversation "
                         "is in its context already")
    return show


def _kind_and_model(kind, model, reason, once):
    """The kind a job runs as, with its model, or a ValueError.

    Left out, a repeating job is fresh and a one-off is continuity. A one-off is nearly always a
    check-back on something said in the conversation ("has he answered?"), and only the
    conversation knows. A repeating job that wants the conversation must say why, because each of
    its runs costs a wake.
    """
    kind = (kind or ("continuity" if once else "fresh")).strip().lower()
    if kind not in KINDS:
        raise ValueError("kind is fresh or continuity")
    reason = (reason or "").strip() or None
    if kind == "continuity" and not once and not reason:
        raise ValueError("a repeating continuity job needs a reason: what in the conversation "
                         "must it read? If nothing, leave kind out and it runs fresh")
    model = (model or "").strip() or None
    if kind == "fresh":
        from . import fresh
        model = fresh.model_id(model)
    else:
        model = None           # the conversation runs on the agent's own model
    return kind, model, reason


def add(agent, task, at=None, every=None, timeout=None, quiet=False, days=None, start=None,
        until=None, once=None, in_minutes=None, retries=0, kind=None, model=None, reason=None,
        show=None, watch=None, debounce=None, check=None):
    from . import registry
    rec = registry.get(agent)
    if not rec:
        raise KeyError(f"no such agent: {agent}")
    task = (task or "").strip()
    if not task:
        raise ValueError("a job needs a task")
    once = parse_once(once, in_minutes)
    if sum(bool(x) for x in (at, every, once, watch)) != 1:
        raise ValueError("a job needs one of: a time of day, an interval, a one-off time, "
                         "or a path to watch")
    if watch:
        watch, debounce = _check_watch(rec, watch, debounce)
    if once and once <= clock.now():
        raise ValueError("a one-off time must be in the future")
    days, start, until = parse_days(days), parse_date(start, "start"), parse_date(until, "until")
    if start and until and until < start:
        raise ValueError("until is before start")
    retries = int(retries or 0)
    if not 0 <= retries <= 3:
        raise ValueError("retries is 0 to 3")
    if at and not AT.match(at):
        raise ValueError(f"a time of day looks like 07:00, not {at!r}")
    if every is not None and float(every) < 60:
        raise ValueError("an interval under a minute is a loop, not a schedule")
    kind, model, reason = _kind_and_model(kind, model, reason, once)
    show = _show_for(show, kind)
    check = _check_command(check)

    with _lock:
        if watch and sum(1 for j in _load().values()
                         if j["agent"] == agent and j.get("watch")) >= config.WATCH_MAX_JOBS:
            raise ValueError(f"an agent may have at most {config.WATCH_MAX_JOBS} watch jobs")
        job = _blank(id=uuid.uuid4().hex[:8], agent=agent, task=task, at=at,
                     every=float(every) if every else None, once=once, days=days,
                     start=start, until=until, retries=retries, kind=kind, model=model,
                     reason=reason, watch=watch, debounce=debounce,
                     timeout=float(timeout or config.JOB_TIMEOUT), quiet=bool(quiet),
                     show=show, check=check)
        _reschedule(job)
        _load()[job["id"]] = job
        _save()
    log.info("job %s added: %s for %s, %s", job["id"], task[:60], agent, _when_words(job))
    _emit()
    return dict(job)


def _check_watch(rec, watch, debounce):
    from . import watch as watcher
    path = watcher.check(rec, str(watch))
    if debounce in (None, ""):
        return path, None
    debounce = float(debounce)
    if not 1 <= debounce <= 86400:
        raise ValueError("debounce_seconds is 1 to 86400")
    return path, debounce


def _check_command(check):
    check = (check or "").strip() or None
    if check and len(check) > 2000:
        raise ValueError("a check is one short command, under 2000 characters")
    return check


def update(job_id, **fields):
    allowed = {"task", "at", "every", "enabled", "timeout", "quiet", "days", "start", "until",
               "retries", "kind", "model", "reason", "show", "check"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot set {sorted(bad)}")
    if fields.get("at") and not AT.match(fields["at"]):
        raise ValueError("a time of day looks like 07:00")
    if "check" in fields:
        fields["check"] = _check_command(fields["check"])
    if "days" in fields:
        fields["days"] = parse_days(fields["days"])
    for name in ("start", "until"):
        if name in fields:
            fields[name] = parse_date(fields[name], name)
    if "retries" in fields:
        fields["retries"] = int(fields["retries"] or 0)
        if not 0 <= fields["retries"] <= 3:
            raise ValueError("retries is 0 to 3")
    with _lock:
        job = _load().get(job_id)
        if not job:
            return None
        if {"kind", "model", "reason"} & set(fields):
            fields["kind"], fields["model"], fields["reason"] = _kind_and_model(
                fields.get("kind", job.get("kind")), fields.get("model", job.get("model")),
                fields.get("reason", job.get("reason")), job.get("once"))
        if "kind" in fields and "show" not in fields and fields["kind"] != "fresh":
            # A job moved into the main conversation is in the context, so it drops chat.
            fields["show"] = "context"
        if {"kind", "model", "reason", "show"} & set(fields):
            fields["show"] = _show_for(fields.get("show", job.get("show")),
                                       fields.get("kind", job.get("kind")))
        if fields.get("enabled") and job.get("watch") and not job.get("enabled"):
            # Turned on again by hand: the brake starts its hour over.
            job["fires"] = []
        job.update(fields)
        if "at" in fields:
            job["every"] = job["once"] = job["watch"] = None
        if "every" in fields and fields["every"]:
            job["at"] = job["once"] = job["watch"] = None
        _reschedule(job)
        _save()
    _emit()
    return dict(job)


def remove(job_id):
    with _lock:
        if _load().pop(job_id, None) is None:
            return False
        _save()
    _emit()
    return True


def _when_words(job):
    """When a job runs, in words: "daily 07:00, weekdays, until 2026-10-20"."""
    if job.get("once"):
        return "once, " + datetime.fromtimestamp(job["once"]).strftime("%a %d %b %H:%M")
    if job.get("watch"):
        return f"when files change in {job['watch']}"
    base = f"daily {job['at']}" if job.get("at") else f"every {int(job.get('every') or 0)}s"
    days = job.get("days")
    if days == [0, 1, 2, 3, 4]:
        base += ", weekdays"
    elif days == [5, 6]:
        base += ", weekends"
    elif days:
        base += ", " + ",".join(DAY_NAMES[d] for d in days)
    if job.get("start"):
        base += f", from {job['start']}"
    if job.get("until"):
        base += f", until {job['until']}"
    return base


def describe():
    """The schedule as one message, for the phone."""
    jobs = all()
    if not jobs:
        return "no jobs"
    lines = []
    for job in sorted(jobs, key=lambda j: j.get("next") or 0):
        when = _when_words(job)
        nxt = (datetime.fromtimestamp(job["next"]).strftime("%a %H:%M")
               if job.get("next") else "never")
        last = job.get("last_outcome") or "not yet run"
        state = "" if job["enabled"] else " [off]"
        lines.append(f"{job['id']}  {job['agent']}  {when}{state}\n"
                     f"    next {nxt} — last {last}\n    {job['task'][:80]}")
    return "\n".join(lines)


# -- running ---------------------------------------------------------------------

BRIEF = (
    "[scheduled job {job_id}]\n\n"
    "{task}\n\n"
    "When you have finished, end your reply with a line that is exactly `{ok}` or `{bad}`. "
    "Nothing but the platform reads that line, and without it the run is recorded as unclear "
    "rather than done."
)

# A fresh run starts with no conversation, so it is told so. Without this line a run reads "check
# whether he answered" and looks for an answer in a conversation it does not have.
FRESH_BRIEF = (
    "[scheduled job {job_id}, a fresh session]\n\n"
    "This is a new session in your folder, not your main conversation. You have your CLAUDE.md, "
    "your memory files and your tools, but none of the chat history. Nothing that asks for "
    "permission is allowed here, so do not try a command you would need to ask about.\n\n"
    "{task}\n\n"
    "Start your reply with one short line, under 60 characters, that says what happened, such as "
    "\"3 drafts made\" or \"Nothing new today\". The owner reads that line first, as the title of "
    "the result.\n\n"
    "When you have finished, end your reply with a line that is exactly `{ok}` or `{bad}`. "
    "Nothing but the platform reads that line, and without it the run is recorded as unclear "
    "rather than done."
)

# A fresh watch run may read the watched folder with these tools only, see fresh.read_rules.
WATCH_READ_HINT = ("You may read the watched folder: Read for a file, and a read-only command "
                   "such as ls or find to list it. Writing there is not allowed.\n")


def _task(job):
    """The task as the run reads it. A watch run is told first which files changed."""
    if job.get("watch"):
        from . import watch
        return watch.changes_text(job) + job["task"] + _check_text(job)
    return job["task"] + _check_text(job)


def _check_text(job):
    said = job.get("_checked")
    return f"\n\n{said}" if said else ""


def _check(job):
    """Run the job's check. None means skip the run; a string is what the run is told.

    A check that fails or hangs never skips: a broken check must cost a run, not hide the work.
    """
    import subprocess
    from . import registry
    rec = registry.get(job["agent"]) or {}
    cmd = job["check"]
    try:
        got = subprocess.run(["bash", "-c", cmd], cwd=rec.get("cwd") or None, text=True,
                             capture_output=True, timeout=config.CHECK_TIMEOUT,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return (f"The job's check `{cmd}` did not finish in {config.CHECK_TIMEOUT:g} seconds, "
                "so this run went ahead. Do the task in full.")
    except OSError as exc:
        return f"The job's check `{cmd}` could not start ({exc}). Do the task in full."
    out = (got.stdout or "").strip()
    if got.returncode != 0:
        err = (got.stderr or out).strip()[-1000:]
        return (f"The job's check `{cmd}` failed with exit code {got.returncode}, so this run "
                f"went ahead. Do the task in full.\n{err}")
    if not out:
        return None
    if len(out) > 4000:
        out = out[:4000] + "\n(cut here; the check printed more)"
    return f"The job's check `{cmd}` printed this, which is why this run is due:\n{out}"


def _skip(job, manual):
    """The check printed nothing: no wake, no report, one line in the log."""
    with _lock:
        live = _load().get(job["id"])
        if live:
            live["skips"] = live.get("skips", 0) + 1
            if not manual:
                live["retry_count"] = 0
                live["retrying"] = False
            _save()
    _record({"ts": clock.now(), "job": job["id"], "agent": job["agent"], "ran": False,
             "outcome": "skipped", "seconds": 0, "manual": manual,
             "kind": job.get("kind") or "continuity"})
    log.info("job %s on %s: skipped, its check printed nothing", job["id"], job["agent"])
    _emit()


def _outcome_of(reply):
    if not reply:
        return "no reply"
    found = OUTCOME.findall(reply)
    if not found:
        return "unclear"
    return "succeeded" if found[-1].lower() == "succeeded" else "failed"


def _report(job, outcome, reply):
    """Tell the operator, by the rule that a failure always speaks and success can be quiet."""
    head = f"{job['agent']} — job {job['id']} {outcome}\n{job['task'][:200]}"
    if outcome == "succeeded":
        if job.get("quiet"):
            return
        telegram.notify(f"{head}\n\n{(reply or '').strip()[:3000]}")
        return
    tail = (reply or "").strip()[:2000] or "nothing came back"
    telegram.notify(f"{head}\n\n{tail}\n\n/rerun {job['id']} to try again")
    # And a "today" item for the front desk, so the next call opens with it.
    from . import inbox
    inbox.job_failed(job, outcome)


def _run_fresh(job):
    """A fresh run in its own process. Returns (reply, outcome, extra for the log)."""
    from . import fresh
    try:
        task = _task(job)
        if job.get("watch"):
            from . import registry, watch
            hint = WATCH_READ_HINT if fresh.read_rules(registry.get(job["agent"]) or {}, job) else ""
            task = watch.changes_text(job) + hint + "\n" + job["task"] + _check_text(job)
        got = fresh.run(job, FRESH_BRIEF.format(job_id=job["id"], task=task,
                                                ok=config.SUCCEEDED, bad=config.FAILED),
                        timeout=job["timeout"])
    except Exception as exc:
        log.exception("job %s blew up", job["id"])
        return None, f"error: {exc}", {}
    reply = got.get("reply")
    if reply is None:
        outcome = f"error: {got['error']}" if got.get("error") else "no reply"
    else:
        outcome = _outcome_of(reply)
    extra = {k: got.get(k) for k in ("model", "cost_usd", "tokens", "turns", "file")}
    if got.get("error"):
        extra["error"] = got["error"]
    return reply, outcome, extra


def _run(job_id, manual=False):
    """One run, start to finish. Called on its own thread."""
    from . import broker, cords

    job = get(job_id)
    if not job:
        return
    agent = job["agent"]
    kind = job.get("kind") or "continuity"
    extra = {}
    if job.get("check"):
        said = _check(job)
        if said is None:
            _skip(job, manual)
            return
        job["_checked"] = said

    if kind == "fresh":
        # A fresh run leaves the agent alone: awake or asleep, its conversation is not touched.
        # It still takes a slot, because a claude process costs memory whoever it belongs to.
        with _slots:
            status.job_started(job_id, agent)
            started = clock.now()
            log.info("job %s starting fresh for %s on %s", job_id, agent, job.get("model"))
            reply, outcome, extra = _run_fresh(job)
            status.job_finished(job_id)
    else:
        reply, outcome, started = _run_continuity(job, broker, cords)
    _finish(job, manual, started, reply, outcome, kind, extra)


def _run_continuity(job, broker, cords):
    job_id, agent = job["id"], job["agent"]
    with _slots:
        # Answer 43. Checked before the wake, because after the wake everything is awake and the
        # difference between "was already up" and "we woke it" is gone.
        was_awake = broker.awake(agent)
        status.job_started(job_id, agent)
        started = clock.now()
        log.info("job %s starting on %s (%s)", job_id, agent,
                 "already awake" if was_awake else "waking it")
        try:
            reply = cords.ask(agent, BRIEF.format(job_id=job_id, task=_task(job),
                                                  ok=config.SUCCEEDED, bad=config.FAILED),
                              timeout=job["timeout"])
        except Exception as exc:
            log.exception("job %s blew up", job_id)
            reply, outcome = None, f"error: {exc}"
        else:
            outcome = _outcome_of(reply)
        status.job_finished(job_id)

        # Answer 41. Put back exactly as found: a 3am job otherwise leaves 600 MB held until
        # morning, and an agent the operator had up stays up.
        if not was_awake:
            broker.sleep(agent)
            log.info("job %s done, %s back to sleep", job_id, agent)
    return reply, outcome, started


def _finish(job, manual, started, reply, outcome, kind, extra):
    job_id, agent = job["id"], job["agent"]
    if kind == "fresh" and reply:
        # A fresh run's reply never passes the chat engine, so the ask check reads it here.
        # A continuity run's turn is read where the chat's turn ends (broker).
        try:
            from . import askcheck
            askcheck.turn_done(agent, reply, "job " + job_id, job=job)
        except Exception:
            log.exception("the ask check for job %s failed", job_id)
    ran = reply is not None
    with _lock:
        live = _load().get(job_id)
        if live:
            live["last_fire"] = started
            live["last_ran"] = ran
            live["last_outcome"] = outcome
            live["last_summary"] = (reply or "").strip()[:500] or None
            live["runs"] += 1
            if extra.get("cost_usd") is not None:
                live["last_cost"] = extra["cost_usd"]
            if outcome != "succeeded":
                live["fails"] += 1
            if not manual:
                _reschedule(live)
                if outcome == "succeeded":
                    live["retry_count"] = 0
                elif live.get("retry_count", 0) < live.get("retries", 0):
                    # Again in a quarter of an hour, before the next scheduled time.
                    live["retry_count"] = live.get("retry_count", 0) + 1
                    live["retrying"] = True
                    soon = clock.now() + RETRY_SECONDS
                    if not live.get("next") or soon < live["next"]:
                        live["next"] = soon
                if _spent(live, clock.now()):
                    _load().pop(job_id, None)
                    log.info("job %s will not run again, so it leaves the list", job_id)
            _save()
    _record({"ts": started, "job": job_id, "agent": agent, "ran": ran, "outcome": outcome,
             "seconds": round(clock.now() - started, 1), "manual": manual, "kind": kind,
             **extra})
    log.info("job %s on %s: %s in %.0fs", job_id, agent, outcome, clock.now() - started)
    # Written before the change goes out, so a chat that reads on the change finds the card.
    to_chat = kind == "fresh" and (live or job).get("show") == "chat"
    if to_chat:
        _show(job, started, outcome, reply)
    _emit()
    _report(job, outcome, reply)
    # The agent knows what he knows: a result shown to him in the chat reaches the agent too, with
    # his next message (The owner, 21 October 2026: "agent should know what i know, and vice versa").
    if kind == "fresh":
        _tell_agent(job, outcome, reply, extra.get("file"))


# Sentences that open many tasks and say how to run, not what the job is. A name skips them, or
# every Initech job was called "COLD START" in the chat.
_SETUP = re.compile(r"^(COLD START\b|This runs as a fresh session|Before anything else|You are\b|"
                    r"Use only the\b|(Work|Personal) side\b)", re.I)


def _name(task):
    """A short name for a job, from the opening of its task: the first sentence that says what
    the job is, cut short."""
    said = re.split(r"(?<=[.!?:])\s", " ".join((task or "").split()) or "a job")
    first = next((s for s in said if not _SETUP.match(s)), said[0])
    # "(job-search agent, Scout)" says whose job it is, and the chat it shows in says that already.
    first = re.sub(r"\s*\([^)]*\)", "", first).rstrip(".:")
    return first if len(first) <= 60 else first[:57].rstrip() + "..."


def _show(job, started, outcome, reply):
    """A fresh run's whole reply, kept for the owner to read in the agent's chat. The agent gets the
    same reply in its mailbox, from _tell_agent."""
    config.SHOWN_DIR.mkdir(parents=True, exist_ok=True)
    lines = (reply or "").strip().splitlines()
    # The code word at the end is for the scheduler, not for the owner.
    if lines and lines[-1].strip().strip("`.").lower() in (config.SUCCEEDED, config.FAILED):
        lines.pop()
    row = {"id": uuid.uuid4().hex[:10], "at": started, "shown_at": clock.now(), "job": job["id"],
           "name": _name(job["task"]), "outcome": outcome,
           "text": "\n".join(lines).strip() or "Nothing came back."}
    with open(config.SHOWN_DIR / f"{job['agent']}.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")


def shown(agent, count=50):
    """GET /api/agents/<id>/shown: the job results shown in this agent's chat, newest last."""
    try:
        lines = (config.SHOWN_DIR / f"{agent}.jsonl").read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-count:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _tell_agent(job, outcome, reply, transcript=None):
    """A fresh run's whole reply, left for the agent's main conversation.

    A fresh run keeps nothing, so without this the agent never learns what its own jobs found, and
    one day's findings cannot meet the next day's. It waits rather than wakes: a wake rereads the
    whole conversation, the cost the fresh run avoided. No limit on the length, by the owner's word on
    17 October 2026: shorten it later if it proves too long.
    """
    from . import broker
    # The opening of the task names the job. Many open with the same COLD START line, so a line
    # is not enough; 200 characters is.
    first = " ".join(job["task"].split())[:200]
    # Written by the agent's own fresh run, but it often quotes pages, mail and chats that run
    # read, and it arrives as a turn. So it is labeled like any other text from outside.
    from . import untrusted
    body = untrusted.wrap("your own fresh run, which may quote what it read",
                          (reply or "").strip() or "Nothing came back.")
    # The whole run is on disk. Its address is enough: the agent reads it only if it wants more.
    where = f"\nTranscript: {transcript}" if transcript else ""
    broker.leave(job["agent"], f"[Your scheduled job {job['id']}, a fresh run: {outcome}]\n"
                               f"Task: {first}{where}\n\n{body}")


def run_now(job_id):
    """Start a job at once, off schedule. Returns a line for the phone.

    It does not wait. The caller is a Telegram message or a button, and a job can take fifteen
    minutes.
    """
    job = get(job_id)
    if not job:
        return f"no job called {job_id}"
    threading.Thread(target=_run, args=(job_id, True), name=f"job-{job_id}",
                     daemon=True).start()
    return f"running {job_id} on {job['agent']} now"


# -- starting up -----------------------------------------------------------------

def catch_up():
    """Say what was missed while the daemon was down. Run nothing.

    Answer 1, and the whole reason this function exists rather than a catch-up loop. The laptop was
    shut at 3am and it is now 9am: the job says it was missed and offers a rerun, and the operator
    decides whether a late run still makes sense. Answer 10: "my judgement".
    """
    now = clock.now()
    notes = []

    # Answer 33 first: a job that was in flight when the daemon stopped did not finish, whatever
    # its last recorded outcome says.
    for job_id, row in status.jobs_interrupted().items():
        job = get(job_id)
        name = job["task"][:80] if job else job_id
        with _lock:
            live = _load().get(job_id)
            if live:
                live["last_outcome"] = "missed"
                live["missed"] += 1
                _save()
        notes.append(f"{row.get('agent')} — job {job_id} was interrupted by a restart\n{name}\n"
                     f"/rerun {job_id}")

    for job in all():
        if not job["enabled"]:
            continue
        since = max(job.get("last_fire") or 0, job.get("checked") or 0,
                    job.get("created") or 0)
        count = _due_between(job, since, now)
        if count:
            with _lock:
                live = _load().get(job["id"])
                if live:
                    live["last_outcome"] = "missed"
                    live["missed"] += count
                    live["checked"] = now
                    _reschedule(live, now)
                    _save()
            when = _when_words(job)
            # One message per job rather than one per occurrence. Answer 9's three messages are
            # three failures, which is a different thing: a laptop shut for a week would otherwise
            # send seven copies of the same news.
            notes.append(f"{job['agent']} — job {job['id']} missed "
                         f"{count} run{'s' if count > 1 else ''} ({when})\n{job['task'][:100]}\n"
                         f"/rerun {job['id']} to run it now")
        else:
            with _lock:
                live = _load().get(job["id"])
                if live:
                    live["checked"] = now
                    if not live.get("next"):
                        _reschedule(live, now)
                    _save()

    if notes:
        log.info("startup: %d missed or interrupted job(s)", len(notes))
        for note in notes:
            telegram.notify(note)
    _emit()
    return notes


def _tick():
    now = clock.now()
    _sweep(now)
    for job in all():
        if not job["enabled"] or not job.get("next"):
            continue
        if job["next"] > now:
            continue
        # Move the clock forward before starting, so a run that takes longer than the interval
        # cannot be started twice by the next tick.
        with _lock:
            live = _load().get(job["id"])
            if not live or live.get("next") != job["next"]:
                continue
            live["last_fire"] = now
            # A scheduled run starts a fresh round of retries; a retry keeps counting.
            if live.get("retrying"):
                live["retrying"] = False
            else:
                live["retry_count"] = 0
            _reschedule(live, now)
            _save()
        threading.Thread(target=_run, args=(job["id"],), name=f"job-{job['id']}",
                         daemon=True).start()


def _loop():
    # The wait is real time on purpose, even when the clock is frozen. A test drives _tick() itself
    # rather than running this loop, and a frozen clock here would spin the processor.
    while not _stop.is_set():
        try:
            _tick()
        except Exception:
            log.exception("scheduler tick failed")
        _stop.wait(config.TICK_SECONDS)


def start():
    _stop.clear()
    with _lock:
        for job in _load().values():
            if job["enabled"] and not job.get("next"):
                _reschedule(job)
        _save()
    thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
    thread.start()
    log.info("scheduler up, %d job(s), %d wake at once", len(all()), config.WAKE_AT_ONCE)
    return thread


def stop():
    _stop.set()


def reset():
    """Forget every job and rebuild the wake limit from config. For tests."""
    global _jobs, _slots
    with _lock:
        _jobs = None
    _listeners.clear()
    _stop.set()
    _stop.clear()
    _slots = threading.BoundedSemaphore(config.WAKE_AT_ONCE)
