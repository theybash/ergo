"""The ask check: Jev reads each finished turn for something the owner must do that is on no list.

The house rule is that every ask of the owner is a task, and agents keep it unevenly. An evening
check once read a call, wrote "The owner said 'I'll note it' ... Neither has a date", and added no
task. Jev, given that reply, flagged that line at 0.85 and nothing else above 0.42.

How it works, once per finished turn (a chat turn, or a fresh job's run):

    lines     code cuts the agent's reply into lines; Jev cannot see what code did not offer
    question  one Noul per line, in one request: does this line hold something the owner must do,
              answer, send or decide, that nothing on the owner's list covers? The state is the
              whole reply and the list: open tasks and owed items, the owner's alarms still to
              ring, and the agent's own one-off jobs, which hold meeting reminders
    flag      a line above FLAG, not flagged for this agent before
    then      a fresh run of the same agent gets the flagged lines and decides. It adds the task
              with its own nag brief, or leaves it. Jev never adds a task: a task needs an owner
              and a brief, and the agent that missed it owns it ("You own your tasks").

Agents decide, the daemon serves: this only points. A delegate is not checked; it reaches the owner
through its parent, which is. The fix run itself is not checked, so a check never feeds itself.
Every check is a line in var/askcheck/log.jsonl, so a week of it shows whether it earns its keep.
"""

import hashlib
import json
import re
import threading

from . import clock, config, logs

log = logs.get(__name__)

FLAG = 0.75             # the real miss scored 0.83 to 0.88; a proposed step 0.66, a question 0.19
MIN_CHARS = 80          # a shorter reply holds no list worth checking
MAX_LINES = 40
LINE_CHARS = 400
TURN_CHARS = 12000
REASON = "askcheck"     # the reason on a fix run's job, so its own reply is not checked again

QUESTION_SITUATION = ("An agent of the owner's finished a turn. `turn` is its whole reply. "
                      "`his_list` is everything on the owner's to-do list, what others owe him, his "
                      "alarms still to ring and the agent's own reminders.")
CRITERIA = {
    "true": "The line names a thing the owner must do, answer, send or decide, with or without a "
            "date, and nothing in `his_list` is the same thing.",
    "false": "The line asks nothing of the owner; or it reports news, a decision by others, or work "
             "the agent did or offered to do itself; or the same thing is already in "
             "`his_list`; or the line says the agent already added it to his list or to what "
             "others owe him; or it is a question or a choice the agent itself puts to the owner "
             "in this reply, such as \"Want me to build it?\", which he answers in the chat; or "
             "it is a step of a plan the agent only proposes in this reply, which the owner has "
             "not yet agreed to build.",
}

FIX_TASK = """ASK CHECK. Jev read your last turn and thinks these lines hold something the owner must do, answer, send or decide that is on no list:

{lines}

The house rule: an ask of the owner is a task, with or without a date. For each line:
1. Call task_list and look for the same thing. If it is there, leave it.
2. If it is truly an ask of the owner and missing, add it with owner_task_add: a due time and a nag brief, as your CLAUDE.md says. A meeting is a reminder job, not a task.
3. If Jev is wrong (it is news, your own work, already handled, a question you asked him, or a step of a plan you only proposed and he has not agreed to), do nothing.
Post nothing to the feed. End with one line: what you added, or "nothing missing"."""

_seen = {}              # agent -> hashes of lines already flagged
_lock = threading.Lock()


def _dir():
    d = config.VAR / "askcheck"
    d.mkdir(parents=True, exist_ok=True)
    return d


def enabled():
    return bool(config.TYPESAFE_KEY) and config.ASKCHECK


def lines_of(text):
    """The reply's lines, as Jev judges them: list marks off, headings and short lines out."""
    out = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", raw).strip()
        if len(line) < 25 or re.fullmatch(r"#+.*|\*\*[^*]+\*\*:?", line):
            continue
        out.append(line[:LINE_CHARS])
    return out[:MAX_LINES]


def his_list(agent_id, side):
    """What the line is checked against: open tasks and owed items of the side, alarms still to
    ring, and the agent's own one-off jobs."""
    from . import cron, phone, tasks
    rows = []
    with tasks._lock:
        for t in tasks._held()["tasks"].values():
            if t["agent"] == tasks.OWNER_ID and t["state"] in ("mine", "owed") and t["side"] == side:
                # The id too: an agent's reply often says "covered by <id>".
                rows.append({"id": t["id"], "title": t["title"],
                             "kind": "owed to him" if t["state"] == "owed" else "his task"})
    now = clock.now()
    for a in phone._alarms().values():
        if not a.get("stopped") and float(a.get("due") or 0) > now:
            rows.append({"kind": "alarm", "title": a.get("label") or a.get("title") or ""})
    for j in cron.all():
        if j.get("agent") == agent_id and j.get("once") and j.get("enabled", True):
            rows.append({"kind": "reminder job", "title": (j.get("task") or "").split("\n")[0][:200]})
    return rows


def payload(text, lines, listed):
    questions = {}
    for n, line in enumerate(lines):
        questions[f"l{n}"] = {
            "type": "noul",
            "instructions": {
                "situation": QUESTION_SITUATION,
                "line": line,
                "question": f"Judge `line`, which is `lines[{n}]` in the state. Does it record "
                            "something the owner must do, answer, send or decide (asked of him by "
                            "anyone, or promised by him), that no item in `his_list` covers?"},
            "criteria": CRITERIA,
        }
    from . import memory
    return {"state": {"turn": text[:TURN_CHARS], "lines": lines, "his_list": listed},
            "model": memory.JEV_MODEL, "questions": questions}


def _key(line):
    return hashlib.sha1(re.sub(r"\W+", " ", line.lower()).strip().encode()).hexdigest()[:16]


def turn_done(agent_id, text, source="chat", job=None):
    """A turn ended. Checked in the background, so the turn's end is not held up."""
    if not enabled() or not agent_id or len((text or "").strip()) < MIN_CHARS:
        return
    if job and job.get("reason") == REASON:
        return
    threading.Thread(target=check, args=(agent_id, text, source), name="askcheck",
                     daemon=True).start()


def check(agent_id, text, source="chat"):
    """One check. Returns the flagged lines, for the tests and the log."""
    from . import cron, memory, registry, vault
    try:
        rec = registry.get(agent_id)
        if not rec or rec.get("parent"):
            return []
        lines = lines_of(text)
        if not lines:
            return []
        listed = his_list(agent_id, vault.side_of(rec))
        got = memory.post_jev(payload(text, lines, listed), config.TYPESAFE_KEY,
                              timeout=config.TYPESAFE_TIMEOUT)
        answers = (got or {}).get("answers") or {}
        scored = [(line, float((answers.get(f"l{n}") or {}).get("noul") or 0))
                  for n, line in enumerate(lines)]
        with _lock:
            seen = _seen.setdefault(agent_id, set())
            flagged = [(line, p) for line, p in scored if p > FLAG and _key(line) not in seen]
            seen.update(_key(line) for line, _ in flagged)
        job = None
        if flagged:
            task = FIX_TASK.format(lines="\n".join(f"- {line}" for line, _ in flagged))
            job = cron.add(agent_id, task, in_minutes=1, kind="fresh", reason=REASON, quiet=True,
                           timeout=900)["id"]
        with (_dir() / "log.jsonl").open("a") as fh:
            fh.write(json.dumps({"at": clock.now(), "agent": agent_id, "source": source,
                                 "lines": len(lines), "top": round(max(p for _, p in scored), 2),
                                 "flagged": [{"line": l, "p": round(p, 2)} for l, p in flagged],
                                 "job": job, "usage": (got or {}).get("usage")}) + "\n")
        if flagged:
            log.info("askcheck: %s missed %d ask(s), fix run %s", agent_id, len(flagged), job)
        return flagged
    except Exception:
        log.exception("askcheck: the check for %s failed", agent_id)
        return []
