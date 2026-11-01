"""Tasks: work an agent offers to take off the owner's plate, as a card they answer.

Agents read their meetings, mail and chats, and their own work. When one notices something it could
do or prepare, it offers it in a sentence or two. They answer on the card: Yes, No, Plan, or Prompt.
When they get to work, most of the work is prepped.

Tokens matter more than anything else here. An offer is a title, where it came from, and what they get. No plan and no research happen before they ask, so a useless offer costs one sentence. A plan
is written only when they press Plan, and work starts only when they say Yes.

No database. One append-only event log, var/tasks.jsonl, one JSON row per event:

    offered     {id, agent, side, title, source, deliverable}   a new card
    plan_asked  {id}              they pressed Plan and the agent was asked
    planned     {id, steps}       the agent attached its plan
    remind      {id, what}        the agent was busy; tell them when it is free. what is yes or plan
    reminded    {id}              they were told, once
    yes         {id, mode}        they said yes and the agent was told
    no          {id, why?}        they said no
    why         {id, why}         a reason they gave after the no
    dismissed   {id}              the small x: off the list, the agent is told nothing
    done        {id, result, file?}  the agent finished; it waits for their tick
    closed      {id}              they agreed it is done (Yes on the finished task)
    reopened    {id}              they took a closed or finished task up again, so it is open again
    mine        {id, title, by, due?}  a task of the owner's own: added by them, by an agent, or by an
                                  alarm with a label. `by` says which
    ticked      {id}              they ticked one of their own tasks off
    moved       {id, due}         one of their own tasks given a new time, by them or by the front desk
    asked       {id}              the front desk asked them once whether it is done or a double; never again
    owed        {id, title, who, by, due?}  something another person promised them, found by an
                                  agent in their meetings, mail or chats. Ticked when it arrives

The daemon is the only writer. The first read folds the log into cards in memory; after that each
event is applied as it is written, the same way the inbox works.

Rules, all in code:

    ten a day       an agent may offer at most ten cards in one local day
    no repeats      the same title from the same agent is refused while that card is open
    own cards only  only the agent that offered a card may plan it or close it
    three days      answered cards (done, no, dismissed) leave the listing after three days. The
                    file keeps them.

Nothing here logs a title, a plan or a result.
"""

import json
import re
import threading
import uuid

from . import clock, config, logs

log = logs.get(__name__)

_lock = threading.RLock()

STATES = ("offered", "yes", "waiting_remind", "done", "closed", "no", "dismissed", "mine",
          "ticked", "talking")
# "talking": they answered an offer with Prompt and words rather than Yes or No. The card leaves
# Waiting and waits for Jev, who reads their words and the agent's reply. See prompted().
OPEN = ("offered", "yes", "waiting_remind", "talking")
ANSWERED = ("done", "no", "dismissed")
# Shown until they act on them, however old: a finished task waits for their tick, their own task waits
# for them.
STAYS = ("done", "mine", "owed")
# The Done list on Insights, paged ten at a time: agents' tasks they closed, and their own ticked off.
FINISHED = ("closed", "ticked")
PAGE = 10

# Who a task of their own belongs to, in the agent field. Never a registered agent's id.
OWNER_ID = "owner"
PLAN_STATES = ("none", "asked", "ready")

PER_DAY = 10
SHOW_ANSWERED = 3 * 86400

TITLE_CHARS = 160
SOURCE_CHARS = 200
DELIVERABLE_CHARS = 200
STEP_CHARS = 200
MIN_STEPS, MAX_STEPS = 2, 8
RESULT_CHARS = 2000
WHY_CHARS = 300
BRIEF_CHARS = 1000
NOTE_CHARS = 200
NAG_LOG = 20            # lines kept on a task; the nag run reads them all

# What the agent reads when they press Plan. They can change it on the Config page.
DEFAULT_PLAN_PROMPT = (
    "The owner wants a plan for this task before deciding. Write 2 to 8 short steps: what you will "
    "do, what you will read or change, and what they get at the end. Do not start the work. "
    "Attach the plan with task_plan.")
PROMPT_CHARS = 4000

# The rule every Yes carries, word for word, because a yes to the task is not a yes to everything
# the task might touch.
STILL_ASK = ("Anything that sends, pays, deletes, posts, contacts someone or pushes code still "
             "needs their OK at that moment.")

_state = None           # {"path", "tasks": {id: task}}
_listeners = []


# -- the files -------------------------------------------------------------------

def _log_file():
    config.VAR.mkdir(parents=True, exist_ok=True)
    return config.VAR / "tasks.jsonl"


def _settings_file():
    config.VAR.mkdir(parents=True, exist_ok=True)
    return config.VAR / "tasks-settings.json"


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
    at = ev.get("at") or 0.0
    return {"id": ev["id"], "agent": ev.get("agent") or "", "side": ev.get("side") or "personal",
            "title": ev.get("title") or "", "source": ev.get("source") or "",
            "deliverable": ev.get("deliverable") or "", "plan": [], "plan_state": "none",
            "state": "offered", "created": at, "updated": at, "why": "", "result": "",
            "result_file": "", "remind": None, "reminded_at": None, "mode": None,
            # The nag (nags.py): the owner's brief for the nag runs, when the next one comes, and
            # one line from each run, so the next one knows the story.
            "brief": "", "nag_at": None, "nag_off": False, "nag_log": [],
            # The owner asked for this time themselves, so the night window does not hold its nags.
            "night_ok": False}


def _apply(tasks, ev):
    """One event onto the cards. An event for a card that is not there changes nothing."""
    kind = ev.get("event")
    if kind == "offered":
        if ev.get("id") and ev["id"] not in tasks:
            tasks[ev["id"]] = _blank(ev)
        return
    if kind in ("mine", "owed"):
        if ev.get("id") and ev["id"] not in tasks:
            task = _blank({**ev, "agent": OWNER_ID, "side": ev.get("side") or "personal"})
            task.update({"state": kind, "kind": kind, "by": ev.get("by") or OWNER_ID,
                         "due": ev.get("due"), "who": ev.get("who") or "",
                         "brief": ev.get("brief") or "",
                         "night_ok": bool(ev.get("night_ok"))})
            if ev.get("owner"):
                task["owner"] = ev["owner"]
            tasks[ev["id"]] = task
        return
    task = tasks.get(ev.get("id"))
    if not task:
        return
    task["updated"] = ev.get("at") or task["updated"]
    if kind == "plan_asked":
        task["plan_state"] = "asked"
        if task["remind"] == "plan":
            task["remind"] = None
    elif kind == "planned":
        task["plan"] = [str(s) for s in (ev.get("steps") or [])]
        task["plan_state"] = "ready"
    elif kind == "remind":
        task["remind"] = ev.get("what") if ev.get("what") in ("yes", "plan") else "yes"
        if task["remind"] == "yes":
            task["state"] = "waiting_remind"
    elif kind == "reminded":
        task["remind"] = None
        task["reminded_at"] = task["updated"]
        if task["state"] == "waiting_remind":
            task["state"] = "offered"
    elif kind == "talking":
        task["state"] = "talking"
        task["remind"] = None
    elif kind == "yes":
        task["state"] = "yes"
        task["remind"] = None
        task["mode"] = ev.get("mode")
    elif kind == "no":
        task["state"] = "no"
        task["remind"] = None
        task["why"] = ev.get("why") or ""
    elif kind == "why":
        task["why"] = ev.get("why") or ""
    elif kind == "dismissed":
        task["state"] = "dismissed"
        task["remind"] = None
    elif kind == "done":
        task["state"] = "done"
        task["remind"] = None
        task["result"] = ev.get("result") or ""
        task["result_file"] = ev.get("file") or ""
    elif kind == "closed":
        task["state"] = "closed"
        task["closed_at"] = task["updated"]
    elif kind == "ticked":
        task["state"] = "ticked"
        task["closed_at"] = task["updated"]
        task["closed_by"] = ev.get("actor") or OWNER_ID
    elif kind == "moved":
        task["due"] = ev.get("due")
        # A new time starts the nags again from it.
        task["nag_at"] = None
    elif kind == "nag":
        if "brief" in ev:
            task["brief"] = ev.get("brief") or ""
        if "next" in ev:
            task["nag_at"] = ev.get("next")
        if "off" in ev:
            task["nag_off"] = bool(ev.get("off"))
        if "night_ok" in ev:
            task["night_ok"] = bool(ev.get("night_ok"))
    elif kind == "nag_logged":
        task["nag_log"] = (task.get("nag_log") or [])[-(NAG_LOG - 1):] + [
            {"at": ev.get("at"), "text": ev.get("text") or ""}]
    elif kind == "said":
        # The exchange about an offer, kept on its card: what they wrote through Prompt and what
        # the agent answered (The owner, 1 November 2026: "I have no idea what this is").
        task["thread"] = (task.get("thread") or [])[-(THREAD - 1):] + [
            {"at": ev.get("at"), "who": "owner" if ev.get("who") == "owner" else "agent",
             "text": ev.get("text") or ""}]
    elif kind == "asked":
        task["asked"] = True
    elif kind == "reopened":
        task["state"] = (task.get("kind") or "mine") if task["agent"] == OWNER_ID else "yes"
        task["closed_at"] = None


def _read(path):
    tasks = {}
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
                    _apply(tasks, ev)
    except OSError:
        pass
    return tasks


def _held():
    """The folded state, read once per log file. A test that moves VAR gets a fresh one."""
    global _state
    path = str(_log_file())
    if _state is None or _state["path"] != path:
        _state = {"path": path, "tasks": _read(path)}
    return _state


def _write(st, ev):
    ev = dict(ev)
    ev.setdefault("at", clock.now())
    with open(st["path"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")
    _apply(st["tasks"], ev)
    task = st["tasks"].get(ev.get("id"))
    if task:
        _emit("tasks", {"event": ev["event"], "task": _view(task), "actor": ev.get("actor")})
    return ev


# -- reading ---------------------------------------------------------------------

def _name(agent):
    """The name the owner gave the agent, which is what they read. The id when there is none."""
    from . import registry
    if agent == OWNER_ID:
        return "You"
    rec = registry.get(agent)
    return (rec or {}).get("name") or agent


def _view(task):
    out = dict(task)
    out["agent_name"] = _name(task["agent"])
    if task.get("by"):
        out["by_name"] = _name(task["by"])
    out["open"] = task["state"] in OPEN
    if task["agent"] == OWNER_ID:
        from . import nags, owners
        out["owner"] = owners.owner(task)
        # Said at once, so the owner can tell them: a nag that falls in the night waits.
        out["nag_waits_until"] = nags.waits_until(task)
    return out


def _visible(task, now):
    if task["state"] in OPEN or task["state"] in STAYS:
        return True
    if task["state"] in FINISHED:
        # The Done list pages these in itself, so the main listing does not carry them.
        return False
    return now - task["updated"] <= SHOW_ANSWERED


def listing(side=None):
    """GET /api/tasks: open cards first, newest first, then the answered ones of the last three
    days, most recent first. `offered` counts the cards waiting on them."""
    with _lock:
        st = _held()
        now = clock.now()
        rows = [t for t in st["tasks"].values()
                if (not side or t["side"] == side) and _visible(t, now)]
        open_ = sorted([t for t in rows if t["state"] in OPEN], key=lambda t: -t["created"])
        done = sorted([t for t in rows if t["state"] not in OPEN], key=lambda t: -t["updated"])
        return {"tasks": [_view(t) for t in open_ + done],
                "offered": sum(1 for t in open_ if t["state"] == "offered")}


def finished(before=None, limit=PAGE):
    """GET /api/tasks/done: the Done list, newest first, `limit` at a time, older than `before`."""
    try:
        before = float(before) if before not in (None, "") else None
    except (TypeError, ValueError):
        before = None
    limit = max(1, min(int(limit or PAGE), 50))
    with _lock:
        rows = [t for t in _held()["tasks"].values() if t["state"] in FINISHED]
        rows.sort(key=lambda t: -(t.get("closed_at") or t["updated"]))
        if before is not None:
            rows = [t for t in rows if (t.get("closed_at") or t["updated"]) < before]
        page = rows[:limit]
        return {"tasks": [_view(t) for t in page], "more": len(rows) > limit}


def get(task_id):
    with _lock:
        task = _held()["tasks"].get(task_id)
        return _view(task) if task else None


def _task(st, task_id):
    task = st["tasks"].get(task_id)
    if not task:
        raise KeyError(f"there is no task called {task_id}")
    return task


# -- the agents' side ------------------------------------------------------------

def _clean(text, count):
    return " ".join(str(text or "").split())[:count]


def _same_day(a, b):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(a).date() == _dt.datetime.fromtimestamp(b).date()


def offer(agent, title, source, deliverable, plan_steps=None):
    """A new card from an agent. ValueError for a bad offer or a repeat, PermissionError past
    ten in a day."""
    from . import registry, vault
    agent = _clean(agent, 64)
    rec = registry.get(agent)
    if not rec:
        raise KeyError(f"there is no agent called {agent}")
    title = _clean(title, TITLE_CHARS)
    source = _clean(source, SOURCE_CHARS)
    deliverable = _clean(deliverable, DELIVERABLE_CHARS)
    if not title:
        raise ValueError("a task needs a title: what you would do, in one sentence")
    if not source:
        raise ValueError("say where it came from, like: Meeting with Sam, Thursday 15:00")
    if not deliverable:
        raise ValueError("say what the owner gets at the end, like: yes or no, with page references")
    now = clock.now()
    with _lock:
        st = _held()
        mine = [t for t in st["tasks"].values() if t["agent"] == agent]
        if any(t["state"] in OPEN and t["title"].lower() == title.lower() for t in mine):
            raise ValueError("you already offered this task and it is still open")
        if sum(1 for t in mine if _same_day(t["created"], now)) >= PER_DAY:
            raise PermissionError(f"you have offered {PER_DAY} tasks today, which is the limit. "
                                  "Offer the rest tomorrow if they still matter")
        ev = _write(st, {"event": "offered", "id": uuid.uuid4().hex[:10], "agent": agent,
                         "side": vault.side_of(rec), "title": title, "source": source,
                         "deliverable": deliverable})
        task = _view(st["tasks"][ev["id"]])
    log.info("tasks: %s offered %s", agent, task["id"])
    # The owner's call, 9 October: an offer may carry its plan, so the card can be answered at once.
    if plan_steps:
        task = plan(task["id"], agent, plan_steps)
    return task


def _own(task, agent):
    if task["agent"] != agent:
        raise PermissionError(f"task {task['id']} belongs to {task['agent']}, not to you")


def plan(task_id, agent, steps):
    """The agent's plan, two to eight short steps, on its own card: with the offer, or when they ask."""
    if not isinstance(steps, list):
        raise ValueError("steps is a list of short sentences")
    steps = [_clean(s, STEP_CHARS) for s in steps]
    steps = [s for s in steps if s]
    if not MIN_STEPS <= len(steps) <= MAX_STEPS:
        raise ValueError(f"a plan is {MIN_STEPS} to {MAX_STEPS} short steps")
    with _lock:
        st = _held()
        task = _task(st, task_id)
        _own(task, agent)
        if task["state"] not in OPEN:
            raise ValueError(f"task {task_id} is {task['state']}, so it needs no plan")
        # A plan nobody asked for used to be refused, to save tokens. The owner dropped that on
        # 9 October: an offer may come with its plan, and Plan still asks for one when it does not.
        _write(st, {"event": "planned", "id": task_id, "steps": steps})
        return _view(task)


def done(task_id, agent, result, file=None):
    """The agent finished its own task. The file, if any, must already be in its outbox."""
    from . import files, registry
    result = str(result or "").strip()[:RESULT_CHARS]
    if not result:
        raise ValueError("say what you found or did, in a sentence or two. For a simple check "
                         "the answer itself is the result")
    name = str(file or "").strip()
    with _lock:
        st = _held()
        task = _task(st, task_id)
        _own(task, agent)
        if task["state"] != "yes":
            raise ValueError(f"task {task_id} is {task['state']}. Only a task the owner said yes "
                             "to can be done")
        if name and files.resolve(registry.get(agent), name, "outbox") is None:
            raise ValueError(f"there is no file called {name} in your outbox. Put it there "
                             "first, then call task_done again")
        ev = {"event": "done", "id": task_id, "result": result}
        if name:
            ev["file"] = name
        _write(st, ev)
        return _view(task)


# -- the owner's side ---------------------------------------------------------------

def busy(agent):
    """True while the agent is working, asked the same way the console's spinner is."""
    from . import broker, live, registry
    rec = registry.get(agent)
    if not rec:
        return False
    return live.working(rec, broker.get(agent) or broker.chat(agent))


def _tell(agent, text, wake):
    from . import broker
    return broker.send(agent, text, wake=wake)


def _plan_lines(task):
    return "\n".join(f"{n}. {s}" for n, s in enumerate(task["plan"], 1))


def _facts(task):
    return (f"Task id: {task['id']}\nFrom: {task['source']}\n"
            f"They get: {task['deliverable']}")


def yes_message(task):
    if task["plan_state"] == "ready" and task["plan"]:
        how = "The plan they saw:\n" + _plan_lines(task)
    else:
        how = "There is no plan, so work out the steps yourself."
    return (f"[Task yes] {task['title']}\n{_facts(task)}\n\n{how}\n\n"
            f"The owner said yes. When done, call task_done({task['id']}, result, file). {STILL_ASK}")


def plan_message(task):
    return (f"[Task plan] {task['title']}\n{_facts(task)}\n\n{plan_prompt()}\n\n"
            f"Call task_plan({task['id']}, steps).")


def _answerable(task):
    if task["state"] not in ("offered", "waiting_remind", "talking"):
        raise ValueError(f"task {task['id']} is {task['state']} already")


def _busy_answer(task):
    return {"busy": True, "agent": task["agent"], "agent_name": _name(task["agent"]),
            "task": _view(task)}


def yes(task_id, mode=None):
    """Their yes. With the agent working and no mode, nothing changes and the answer says busy.
    mode now sends anyway, mode remind waits until the agent is free and then tells them."""
    with _lock:
        st = _held()
        task = _task(st, task_id)
        _answerable(task)
        if mode not in (None, "", "now", "remind"):
            raise ValueError("mode is now or remind")
        if not mode and busy(task["agent"]):
            return _busy_answer(task)
        if mode == "remind":
            _write(st, {"event": "remind", "id": task_id, "what": "yes"})
            agent = task["agent"]
        else:
            outcome = _tell(task["agent"], yes_message(task), wake=True)
            if not outcome:
                raise RuntimeError(f"{_name(task['agent'])} could not be told. Try again")
            _write(st, {"event": "yes", "id": task_id, "mode": mode or None})
            return _view(task)
    # Outside the lock: an agent that is free already gets its reminder now.
    free(agent)
    return get(task_id)


def ask_plan(task_id, mode=None):
    """They pressed Plan. The agent gets the plan prompt and the task, and attaches the plan."""
    with _lock:
        st = _held()
        task = _task(st, task_id)
        _answerable(task)
        if mode not in (None, "", "now", "remind"):
            raise ValueError("mode is now or remind")
        if task["plan_state"] == "asked" and task["remind"] != "plan":
            return _view(task)
        if not mode and busy(task["agent"]):
            return _busy_answer(task)
        if mode == "remind":
            _write(st, {"event": "remind", "id": task_id, "what": "plan"})
            agent = task["agent"]
        else:
            outcome = _tell(task["agent"], plan_message(task), wake=True)
            if not outcome:
                raise RuntimeError(f"{_name(task['agent'])} could not be told. Try again")
            _write(st, {"event": "plan_asked", "id": task_id})
            return _view(task)
    free(agent)
    return get(task_id)


def no(task_id, why=None):
    """Their no. The agent hears of it only when they give a reason: a bare no is a no, and telling the
    agent cost a line in its chat and a turn of tokens every time."""
    why = _clean(why, WHY_CHARS)
    with _lock:
        st = _held()
        task = _task(st, task_id)
        _answerable(task)
        ev = {"event": "no", "id": task_id}
        if why:
            ev["why"] = why
        _write(st, ev)
        if why:
            _tell(task["agent"], f"[Task no] {task['title']}\nTheir reason: {why}", wake=False)
        return _view(task)


def because(task_id, why):
    """A reason they gave after the no. Kept on the card and passed on, without waking anyone."""
    why = _clean(why, WHY_CHARS)
    if not why:
        raise ValueError("a reason is a few words")
    with _lock:
        st = _held()
        task = _task(st, task_id)
        if task["state"] != "no":
            raise ValueError(f"task {task_id} is {task['state']}, not a no")
        _write(st, {"event": "why", "id": task_id, "why": why})
        _tell(task["agent"], f"[Task no, their reason] {task['title']}\nTheir reason: {why}",
              wake=False)
        return _view(task)


def dismiss(task_id, actor=None):
    """The small x. Off the list. On one of their own tasks, the owner hears of it (owners.py)."""
    with _lock:
        st = _held()
        task = _task(st, task_id)
        if task["state"] != "dismissed":
            _write(st, {"event": "dismissed", "id": task_id, "actor": actor or OWNER_ID})
        return _view(task)


# -- the task list: the owner's own tasks, and closing or reopening -----------------------

def add_mine(title, by=None, due=None, side=None, brief=None, night_ok=False, owner=None):
    """A task of the owner's own. Added by the owner, by an agent for them, or by an alarm with a
    label.

    It carries a side, and it is always the one side there is (vault.SIDE). `side` is accepted
    for older callers and ignored.

    `owner` is the agent the owner picks to look after it when adding it. None leaves it to the
    home agent (owners.py).
    """
    from . import registry, vault
    title = _clean(title, TITLE_CHARS)
    if not title:
        raise ValueError("a task needs a title")
    by = _clean(by, 64) or OWNER_ID
    if by != OWNER_ID and not registry.get(by):
        raise KeyError(f"there is no agent called {by}")
    owner = _clean(owner, 64) or None
    if owner:
        rec = registry.get(owner)
        if not rec or rec.get("parent"):
            raise KeyError(f"there is no agent called {owner}")
    side = vault.SIDE
    try:
        due = float(due) if due not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError("due is a time in seconds")
    with _lock:
        st = _held()
        if any(t["state"] == "mine" and t["title"].lower() == title.lower()
               for t in st["tasks"].values()):
            raise ValueError("that task is already on their list")
        ev = {"event": "mine", "id": uuid.uuid4().hex[:10], "title": title, "by": by,
              "side": side, "actor": by}
        if due:
            ev["due"] = due
        if _clean(brief, BRIEF_CHARS):
            ev["brief"] = _clean(brief, BRIEF_CHARS)
        if night_ok is True:
            ev["night_ok"] = True
        if owner:
            ev["owner"] = owner
        ev = _write(st, ev)
        return _view(st["tasks"][ev["id"]])


def find_mine(words, side=None):
    """One open task of theirs by its id, or by its title or a part of it, on one side if given.
    None when no task, or more than one, fits."""
    words = _clean(words, TITLE_CHARS).lower()
    if not words:
        return None
    with _lock:
        mine = [t for t in _held()["tasks"].values() if t["state"] == "mine"
                and (side is None or t["side"] == side)]
    for t in mine:
        if t["id"] == words or t["title"].lower() == words:
            return _view(t)
    some = [t for t in mine if words in t["title"].lower()]
    return _view(some[0]) if len(some) == 1 else None


def add_owed(title, who, by, due=None):
    """Something another person promised the owner, found by an agent. It takes the agent's side."""
    from . import registry, vault
    title = _clean(title, TITLE_CHARS)
    who = _clean(who, 80)
    if not title or not who:
        raise ValueError("say what was promised, and who promised it")
    rec = registry.get(_clean(by, 64))
    if not rec:
        raise KeyError(f"there is no agent called {by}")
    try:
        due = float(due) if due not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError("due is a time in seconds")
    with _lock:
        st = _held()
        if any(t["state"] == "owed" and t["title"].lower() == title.lower()
               for t in st["tasks"].values()):
            raise ValueError("that is already on the owed list")
        ev = {"event": "owed", "id": uuid.uuid4().hex[:10], "title": title, "who": who,
              "by": rec["id"], "side": vault.side_of(rec)}
        if due:
            ev["due"] = due
        ev = _write(st, ev)
        return _view(st["tasks"][ev["id"]])


def tick(task_id, actor=None):
    """One of their own tasks, ticked off: by them, or by an agent they told it was done. `actor` is who
    ticked it, so its owner is told unless it ticked it itself."""
    with _lock:
        st = _held()
        task = _task(st, task_id)
        if task["agent"] != OWNER_ID:
            raise ValueError(f"task {task_id} is {_name(task['agent'])}'s. They close it with Yes")
        if task["state"] in ("mine", "owed"):
            _write(st, {"event": "ticked", "id": task_id, "actor": actor or OWNER_ID})
        return _view(task)


def move(task_id, due, actor=None):
    """One of their own tasks, given a new time."""
    try:
        due = float(due)
    except (TypeError, ValueError):
        raise ValueError("due is a time in seconds")
    with _lock:
        st = _held()
        task = _task(st, task_id)
        if task["agent"] != OWNER_ID or task["state"] != "mine":
            raise ValueError(f"task {task_id} is not an open task of theirs")
        _write(st, {"event": "moved", "id": task_id, "due": due, "actor": actor or OWNER_ID})
        return _view(task)


def set_nag(task_id, agent, brief=None, next_in_minutes=None, next_at=None, off=None,
            note=None, night_ok=None):
    """The owner, or a nag run in its name, keeping one of their tasks' nags: a new brief, when the
    next nag comes, nags off or on again, and one line for the log. Only the task's owner."""
    from . import owners
    with _lock:
        st = _held()
        task = _task(st, task_id)
        if task["agent"] != OWNER_ID or task["state"] not in ("mine", "owed"):
            raise ValueError(f"task {task_id} is not an open task of theirs")
        if agent != owners.owner(task):
            raise ValueError(f"task {task_id} is {_name(owners.owner(task))}'s to nag about")
        ev = {"event": "nag", "id": task_id, "actor": agent}
        if brief is not None:
            ev["brief"] = _clean(brief, BRIEF_CHARS)
        if next_in_minutes not in (None, ""):
            try:
                minutes = float(next_in_minutes)
            except (TypeError, ValueError):
                raise ValueError("next_in_minutes is a number of minutes")
            if not 1 <= minutes <= 14 * 1440:
                raise ValueError("next_in_minutes is between 1 and 20160")
            ev["next"] = clock.now() + minutes * 60
        elif next_at not in (None, ""):
            ev["next"] = float(next_at)
        if off is not None:
            ev["off"] = bool(off)
        if night_ok is not None:
            ev["night_ok"] = night_ok is True
        if len(ev) > 3:
            _write(st, ev)
        if _clean(note, NOTE_CHARS):
            _write(st, {"event": "nag_logged", "id": task_id, "actor": agent,
                        "text": _clean(note, NOTE_CHARS)})
        return _view(task)


def asked(task_id):
    """The front desk asked about it once. They are not asked again."""
    with _lock:
        st = _held()
        task = _task(st, task_id)
        if not task.get("asked"):
            _write(st, {"event": "asked", "id": task_id})
        return _view(task)


# Words that say nothing about which task it is, left out when two titles are compared.
_PLAIN = set("the a an and or to of for in on at with from by my their her it is be get set up "
             "call do make take".split())


def _words(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _PLAIN and len(w) > 2}


def maybe_same(rows):
    """Pairs of their open tasks whose titles share most of their words, such as "Replace the jet
    shower" and "Install the jet shower". Only pairs the front desk has not asked about."""
    out = []
    rows = [t for t in rows if not t.get("asked")]
    for i, a in enumerate(rows):
        wa = _words(a["title"])
        for b in rows[i + 1:]:
            wb = _words(b["title"])
            both = len(wa & wb)
            if both >= 2 and both / min(len(wa), len(wb)) >= 0.75:
                out.append((a, b))
    return out


def for_desk(side="personal"):
    """Their open tasks as the front desk reads them: what is due today or overdue first, then the rest,
    and the pairs that look like the same task."""
    import datetime as _dt
    now = clock.now()
    end = _dt.datetime.fromtimestamp(now).replace(hour=23, minute=59, second=59).timestamp()
    with _lock:
        rows = [dict(t) for t in _held()["tasks"].values()
                if t["agent"] == OWNER_ID and t["state"] == "mine" and t["side"] == side]
    rows.sort(key=lambda t: (t.get("due") or 9e12, t["created"]))

    def line(t):
        out = {"id": t["id"], "title": t["title"]}
        if t.get("due"):
            out["due"] = _dt.datetime.fromtimestamp(t["due"]).strftime("%a %d %b %H:%M")
            if t["due"] < now:
                out["overdue"] = True
        return out
    due = [line(t) for t in rows if t.get("due") and t["due"] <= end]
    rest = [line(t) for t in rows if not (t.get("due") and t["due"] <= end)][:20]
    same = [[line(a), line(b)] for a, b in maybe_same(rows)][:5]
    return {"due_or_overdue": due, "others": rest, "maybe_same": same}


def close(task_id):
    """Their Yes on a task an agent finished: it is done. The agent is not told; it already knows."""
    with _lock:
        st = _held()
        task = _task(st, task_id)
        if task["state"] != "done":
            raise ValueError(f"task {task_id} is {task['state']}, not waiting for their tick")
        _write(st, {"event": "closed", "id": task_id})
        return _view(task)


def reopen(task_id):
    """They took a finished task up again, from its No or from the Done list. The message they send
    with it goes to the agent's chat, so the agent learns of it from them, not from here."""
    with _lock:
        st = _held()
        task = _task(st, task_id)
        if task["state"] in ("done", "closed", "ticked"):
            _write(st, {"event": "reopened", "id": task_id, "actor": OWNER_ID})
        return _view(task)


def for_agent(agent):
    """The task_list tool: the agent's own tasks, and the owner's own open ones."""
    with _lock:
        rows = list(_held()["tasks"].values())
    def line(t):
        bits = [t["id"], t["state"], t["title"]]
        if t.get("due"):
            import datetime as _dt
            bits.append("due " + _dt.datetime.fromtimestamp(t["due"]).strftime("%a %d %b %H:%M"))
        if t.get("result"):
            bits.append("result: " + t["result"][:300])
        return " | ".join(bits)
    yours = sorted([t for t in rows if t["agent"] == agent and t["state"] not in ("dismissed",)],
                   key=lambda t: -t["updated"])[:30]
    from . import registry, vault
    rec = registry.get(agent)
    side = vault.side_of(rec) if rec else "personal"
    owed = sorted([t for t in rows if t["agent"] == OWNER_ID and t["state"] == "owed"
                   and t["side"] == side], key=lambda t: (t.get("due") or 9e12, t["created"]))
    his = sorted([t for t in rows if t["agent"] == OWNER_ID and t["state"] == "mine"
                  and t["side"] == side],
                 key=lambda t: (t.get("due") or 9e12, t["created"]))
    from . import owners
    import datetime as _dt

    def his_line(t):
        who = owners.owner(t)
        if who != agent:
            return line(t) + f" | owner {_name(who)}"
        out = line(t) + " | yours"
        if t.get("nag_off"):
            return out + " | nags off"
        from . import nags
        when = nags.due_at(t)
        if when:
            out += " | next nag " + _dt.datetime.fromtimestamp(when).strftime("%a %d %b %H:%M")
        out += " | brief: " + (t.get("brief") or "none, the default")
        if t.get("nag_log"):
            out += " | last nag: " + t["nag_log"][-1]["text"]
        return out

    def owed_line(t):
        return his_line(t) + f" | from {t.get('who') or '?'}"
    # What was finished on its own slice in the last seven days, and by whom. For a session that
    # starts without the reports, like a fresh job run.
    since = clock.now() - 7 * 86400
    gone = sorted([t for t in rows if t["agent"] == OWNER_ID and t["state"] == "ticked"
                   and (t.get("closed_at") or 0) >= since and owners.owner(t) == agent],
                  key=lambda t: -(t.get("closed_at") or 0))
    import datetime as _dt

    def done_line(t):
        by = t.get("closed_by") or OWNER_ID
        who = "The owner" if by == OWNER_ID else _name(by)
        when = _dt.datetime.fromtimestamp(t["closed_at"]).strftime("%a %d %b %H:%M")
        return f"{t['id']} | {t['title']} | ticked {when} by {who}"
    return {"your_tasks": [line(t) for t in yours], "owner_tasks": [his_line(t) for t in his],
            "owed_to_owner": [owed_line(t) for t in owed],
            "done_recently": [done_line(t) for t in gone]}


# -- the reminder ----------------------------------------------------------------

def free(agent, sure=False):
    """The agent may have stopped working. Each card waiting on that moment tells them once.

    Called when a chat turn ends, when an agent falls asleep, and when the console's feed sees
    the spinner stop. `sure` skips asking again: a turn that just ended is the answer, and the
    live status can lag it by a second or two.
    """
    with _lock:
        st = _held()
        waiting = [t for t in st["tasks"].values()
                   if t["agent"] == agent and t["remind"] and t["state"] in OPEN]
        if not waiting:
            return 0
    if not sure and busy(agent):
        return 0
    told = 0
    for task in waiting:
        with _lock:
            st = _held()
            now = st["tasks"].get(task["id"])
            if not now or not now["remind"]:
                continue
            what = now["remind"]
            # Written first, so a second call in the same instant finds nothing to say.
            _write(st, {"event": "reminded", "id": task["id"]})
        _notify(agent, task, what)
        told += 1
    return told


def _notify(agent, task, what):
    """Through the inbox: the phone shows each new brief as a notification, a tap opens the agent's
    chat (their choice: the agent that is free is where they want to be), and the front desk says it
    on the next call. A today item, so it lasts until midnight and pings nobody on Telegram."""
    from . import calm, inbox
    if calm.on():
        return
    verb = "Start" if what == "yes" else "Plan"
    text = f"{_name(agent)} is free. {verb} '{task['title']}'?"
    try:
        inbox.add(agent, text, urgency="today", link=f"?screen=chat#/a/{agent}", judge=False,
                  kind="free")
    except Exception:
        log.exception("tasks: the reminder for %s did not reach the inbox", task["id"])


# -- the settings ----------------------------------------------------------------

def _saved():
    try:
        held = json.loads(_settings_file().read_text())
        return held if isinstance(held, dict) else {}
    except (OSError, ValueError):
        return {}


def plan_prompt():
    text = _saved().get("plan_prompt")
    return text.strip() if isinstance(text, str) and text.strip() else DEFAULT_PLAN_PROMPT


def hot_hours():
    try:
        hours = float(_saved().get("hot_hours"))
        return hours if 0 <= hours <= 24 * 14 else 24.0
    except (TypeError, ValueError):
        return 24.0


def settings():
    return {"plan_prompt": plan_prompt(), "default_plan_prompt": DEFAULT_PLAN_PROMPT,
            "limit": PROMPT_CHARS, "hot_hours": hot_hours()}


def save_settings(fields):
    """The plan prompt from the Config page, and the hours that make a task hot. Empty puts the
    default prompt back."""
    fields = fields if isinstance(fields, dict) else {}
    if "plan_prompt" not in fields and "hot_hours" not in fields:
        raise ValueError("send plan_prompt or hot_hours")
    if "hot_hours" in fields:
        try:
            hours = float(fields["hot_hours"])
        except (TypeError, ValueError):
            raise ValueError("hot_hours is a number of hours")
        if not 0 <= hours <= 24 * 14:
            raise ValueError("hot_hours is between 0 and 336")
        with _lock:
            held = _saved()
            held["hot_hours"] = hours
            path = _settings_file()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(held))
            tmp.replace(path)
        if "plan_prompt" not in fields:
            return settings()
    text = fields["plan_prompt"]
    if text is not None and not isinstance(text, str):
        raise ValueError("plan_prompt is text")
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) > PROMPT_CHARS:
        raise ValueError(f"The prompt is longer than {PROMPT_CHARS} characters.")
    with _lock:
        held = _saved()
        if text and text != DEFAULT_PLAN_PROMPT:
            held["plan_prompt"] = text
        else:
            held.pop("plan_prompt", None)
        path = _settings_file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(held))
        tmp.replace(path)
    return settings()


def reset():
    """Forget the folded state and the listeners. For tests."""
    global _state
    with _lock:
        _state = None
    _listeners.clear()
    _prompts.clear()


# -- Prompt: their words instead of a button ------------------------------------------------------
# The owner's design, 14 October. Prompt on an offer card opens the agent's chat with the card quoted,
# and what they write goes to the agent as usual. The card leaves Waiting at once, as "talking".
# When the agent's turn ends, Jev reads their words and the agent's reply together and says what
# became of the offer: started, declined, or still being discussed. Started is a yes, declined is
# a no with their words as the reason, and anything Jev is unsure of stays "talking", where the
# card's own buttons still answer it. Jev never sends anything; it only moves the card.

ABOUT = re.compile(r"^\[About task ([0-9a-f]{6,16})[:\]]")
THREAD = 20          # turns kept on a card
SAID_CHARS = 2000
SURE = 0.85
_prompts = {}

PROMPT_QUESTION = {
    "type": "choice",
    "instructions": {
        "situation": "An agent offered the owner a task (`offer`). Instead of pressing Yes or No they "
                     "wrote to the agent about it (`owner_wrote`), and the agent answered "
                     "(`agent_replied`).",
        "question": "After this exchange, what has become of the offer?",
    },
    "criteria": {
        "started": "The agent is now doing the task. The owner agreed, or gave an instruction for "
                   "how to do it (for example \"check the PR first\" or \"use the March numbers\"), "
                   "and the agent replied that it is starting or has started.",
        "declined": "The owner turned the offer down or told the agent to drop it.",
        "discussing": "The agent is not doing the task yet: The owner only asked a question or raised "
                      "a doubt, or the agent replied with a question and waits for their answer.",
    },
}


def prompted(agent, text):
    """Their message to an agent. When it quotes an open offer of that agent's, the card leaves
    Waiting, and the next end of the agent's turn asks Jev what became of it."""
    m = ABOUT.match(text or "")
    if not m:
        return None
    with _lock:
        st = _held()
        task = st["tasks"].get(m.group(1))
        if task is None:
            return None
        if task.get("agent") != agent or task.get("state") not in ("offered", "waiting_remind",
                                                                   "talking"):
            return None
        if task["state"] != "talking":
            _write(st, {"event": "talking", "id": task["id"]})
        words = str(text).split("]", 1)[-1].strip()
        if words:
            _write(st, {"event": "said", "id": task["id"], "who": "owner",
                        "text": words[:SAID_CHARS]})
        _prompts[agent] = {"id": task["id"], "text": str(text)[:1500]}
        return task["id"]


def prompt_settled(agent):
    """The agent's turn ended. If it was answering a prompted offer, Jev reads the exchange, in
    the background so the turn's end is not held up."""
    held = _prompts.pop(agent, None)
    if not held:
        return
    threading.Thread(target=_judge_prompt, args=(agent, held), name="task-prompt",
                     daemon=True).start()


def _judge_prompt(agent, held):
    try:
        from . import config, memory, registry, transcript
        rec = registry.get(agent)
        reply = (transcript.last_message(rec) if rec else "") or ""
        task = get(held["id"])
        if task and task.get("state") == "talking" and str(reply).strip():
            with _lock:
                _write(_held(), {"event": "said", "id": held["id"], "who": "agent",
                                 "text": str(reply).strip()[:SAID_CHARS]})
        if not task or task.get("state") != "talking" or not config.TYPESAFE_KEY:
            return
        offer = {"title": task.get("title") or "", "you_get": task.get("deliverable") or "",
                 "from": task.get("source") or "", "plan": task.get("plan") or []}
        payload = {"state": {"offer": offer, "owner_wrote": held["text"],
                             "agent_replied": str(reply)[:3000]},
                   "model": memory.JEV_MODEL, "questions": {"fate": PROMPT_QUESTION}}
        got = memory.post_jev(payload, config.TYPESAFE_KEY, timeout=config.TYPESAFE_TIMEOUT)
        answer = ((got or {}).get("answers") or {}).get("fate") or {}
        pick = answer.get("choice")
        sure = (answer.get("probabilities") or {}).get(pick, 0)
        log.info("task %s: Jev read the prompt as %s (%.2f)", held["id"], pick, sure)
        if sure < SURE or pick not in ("started", "declined"):
            return
        with _lock:
            st = _held()
            now = _task(st, held["id"])
            if now["state"] != "talking":
                return
            if pick == "started":
                # No message to the agent: it is already at work, it said so.
                _write(st, {"event": "yes", "id": held["id"], "mode": "prompt"})
            else:
                why = _clean(held["text"].split("]", 1)[-1], WHY_CHARS)
                _write(st, {"event": "no", "id": held["id"], **({"why": why} if why else {})})
    except Exception:
        log.exception("task %s: the prompt was not judged", held.get("id"))
