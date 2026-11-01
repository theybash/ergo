#!/usr/bin/env python3
"""The tools an agent is given: cords for everyone, the fleet for the master.

This is an MCP server. Claude Code starts it, one per agent, and talks to it over this process's
standard input and output in JSON-RPC. It holds no state: every tool is a request to the daemon's own
HTTP interface, which is why this file is short. Everything underneath it was built and tested first.

    every agent    list_agents, transfer_to, call_agent, the schedule, brief, call_owner,
                   the task cards and its own WhatsApp chats
    the master     also wake, sleep, stop, message, read_agent, agents_health

The split is about usefulness, not permission. There are no walls between agents here, by the
decision of 18 August: every agent runs as the operator and can already read another's files and
reach its ports. Giving one agent the fleet tools is a door, not a gate, and the master is the agent
the operator talks to.

Written against JSON-RPC directly rather than with an SDK, because there is no pip on this laptop.
The protocol is small: read a line, answer a line. The third protocol hand-rolled in this project
after the WebSocket and Telegram, and the cheapest of the three.

Run by hand to see what it offers:

    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | PLATFORM_ROLE=master python3 commandcenter/tools.py
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("PLATFORM_API", "http://127.0.0.1:8080")
ME = os.environ.get("PLATFORM_AGENT", "")
ROLE = os.environ.get("PLATFORM_ROLE", "agent")
# Set when this agent is a delegate: the agent that made it, and the only one it reports to.
PARENT = os.environ.get("PLATFORM_PARENT", "")
# Whose Ergo this is. The descriptions below say "the owner". When PLATFORM_OWNER holds a name, the
# agents read that name instead. Tool names such as call_owner stay as they are.
OWNER = (os.environ.get("PLATFORM_OWNER") or "").strip()


def owned(text):
    """A tool description with the owner's name in place of "the owner", when a name is set."""
    if not OWNER:
        return text
    return re.sub(r"\b[Tt]he owner\b", lambda _m: OWNER, text)

# The version this speaks. A client that asks for another gets its own back when we can honour it,
# which is what the specification asks for, and this one otherwise.
VERSION = "2025-06-18"


def call(path, method="GET", body=None, timeout=600):
    """One request to the daemon. Loopback, so the tailnet door lets it through without a token."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            said = json.loads(raw).get("error") or raw
        except Exception:
            said = raw
        raise RuntimeError(f"{said}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"the daemon is not answering on {API}: {exc.reason}")


# -- what the tools do -----------------------------------------------------------

def _brief(agent):
    return {"id": agent["id"], "name": agent["name"], "state": agent["state"],
            "working": agent["working"], "role": agent.get("role"),
            "folder": agent["cwd"], "turns": agent["turns"],
            "waiting": agent.get("waiting", 0),
            "server": (agent.get("server") or {}).get("up", False),
            "last_message": (agent.get("last_message") or "")[:400]}


def list_agents(**_):
    state = call("/api/state")
    return {"agents": [_brief(a) for a in state["agents"]],
            "memory_free_gb": ((state.get("health") or {}).get("memory") or {}).get("available_gb")}


def transfer_to(agent, instruction):
    """One use, and the receiver cannot refuse. Both must be awake."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    cord = call("/api/cords", "POST",
                {"kind": "transfer", "from": ME, "to": agent, "instruction": instruction})
    return {"cord": cord["id"], "state": cord["state"],
            "carried": len((cord.get("messages") or [{}])[0].get("text", "")),
            "note": "Your last message was handed over with that instruction."}


def call_agent(agent, instruction, budget=None):
    """A bounded conversation. You hang up with a summary; the other agent cannot end it."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    body = {"kind": "call", "from": ME, "to": agent, "instruction": instruction}
    if budget:
        body["budget"] = int(budget)
    cord = call("/api/cords", "POST", body)
    return {"cord": cord["id"], "state": cord["state"],
            "note": "The call runs in the background. It arrives as a message in your terminal, and "
                    "you end it by writing a line starting HANGUP: with what you learned."}


# -- the schedule ----------------------------------------------------------------

# Every agent, not the master alone. An agent knows better than the master which of its own work
# repeats, and a reminder an agent set for itself is the case this exists for. It can also schedule
# work on another agent, which follows the 18 August decision: no walls between agents. What stops
# a runaway is underneath, in the daemon: four agents woken at once, and a missed run never runs late.


def _reads(job):
    """The schedule as a line a person can read, and the next run as a local time."""
    if job.get("once"):
        says = "once, " + time.strftime("%a %d %b %H:%M", time.localtime(job["once"]))
    elif job.get("watch"):
        says = f"when files change in {job['watch']}"
    elif job.get("at"):
        says = f"every day at {job['at']}"
    elif job.get("every"):
        every = float(job["every"])
        step, unit = (3600, "hour") if every >= 3600 else (60, "minute")
        count = every / step
        says = f"every {unit}" if count == 1 else f"every {count:g} {unit}s"
    else:
        says = "never"
    days = job.get("days")
    if days and not job.get("once"):
        names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        says += (", weekdays" if days == [0, 1, 2, 3, 4] else ", weekends" if days == [5, 6]
                 else ", " + ",".join(names[d] for d in days))
    if job.get("until"):
        says += f", until {job['until']}"
    when = job.get("next")
    return says, (time.strftime("%a %H:%M", time.localtime(when)) if when else None)


def _job(job):
    says, when = _reads(job)
    return {"id": job["id"], "agent": job["agent"], "mine": job["agent"] == ME,
            "task": job["task"], "runs": says, "next_run": when,
            "enabled": job.get("enabled", True), "quiet": job.get("quiet", False),
            "times_run": job.get("runs", 0), "times_failed": job.get("fails", 0),
            "days": job.get("days"), "start": job.get("start"), "until": job.get("until"),
            "once": job.get("once"), "retries": job.get("retries", 0),
            "kind": job.get("kind") or "continuity", "model": job.get("model"),
            "show": job.get("show") or "context",
            "watch": job.get("watch"), "debounce_seconds": job.get("debounce"),
            "check": job.get("check"), "times_skipped": job.get("skips", 0),
            "last_outcome": job.get("last_outcome"), "last_cost_usd": job.get("last_cost")}


def list_jobs(**_):
    """The whole schedule. Yours are marked, because most of the time yours are the question."""
    return {"jobs": [_job(j) for j in call("/api/jobs")["jobs"]]}


def add_job(task, at=None, every_minutes=None, agent=None, quiet=False, days=None, start=None,
            until=None, once=None, in_minutes=None, retries=0, kind=None, model=None,
            reason=None, show=None, watch=None, debounce_seconds=None, check=None):
    """Put work on the schedule. Yours unless you name another agent.

    The task is written for the agent that will read it, which is usually you in a week, with none
    of this conversation in front of it. "Check the drafts folder and report what is stale" works.
    "Do that again" does not.
    """
    who = (agent or ME or "").strip()
    if not who:
        raise RuntimeError("this server does not know which agent it belongs to, so name the agent")
    if sum(x is not None and x != "" for x in (at, every_minutes, once, in_minutes, watch)) != 1:
        raise RuntimeError("give one of: at (daily, like 07:00), every_minutes, once (like "
                           "2026-10-16 09:00), in_minutes, or watch (a path)")
    body = {"agent": who, "task": task, "quiet": bool(quiet), "days": days, "start": start,
            "until": until, "retries": retries or 0, "kind": kind, "model": model,
            "reason": reason, "show": show, "check": check}
    if at:
        body["at"] = str(at).strip()
    elif every_minutes:
        body["every"] = float(every_minutes) * 60
    elif once:
        body["once"] = once
    elif watch:
        body["watch"] = watch
        body["debounce_seconds"] = debounce_seconds
    else:
        body["in_minutes"] = in_minutes
    job = _job(call("/api/jobs", "POST", body))
    if watch:
        job["note"] = (f"Runs {job['debounce_seconds'] or 30:g} seconds after the last change, so "
                       "a burst of writes runs it once. The run is told first which files "
                       "changed. A change made while the daemon is down is not replayed. More "
                       "than 6 runs in an hour pauses the job. ")
    else:
        job["note"] = ""
    if job.get("check"):
        job["note"] += ("Each time it is due, the daemon runs the check first, in your folder. "
                        "Prints nothing: no run, no cost. Prints something: the run goes ahead "
                        "and is told what it printed. Fails or takes over a minute: the run goes "
                        "ahead. ")
    if job["kind"] == "fresh":
        job["note"] += (f"Runs in a new session on {job['model']}, in the agent's folder, with its "
                       "CLAUDE.md, memory and tools but no chat history. The agent's main "
                       "conversation is not woken. The run ends with `task succeeded` or `task "
                       "failed`.")
        if job["show"] == "chat":
            job["note"] += (" Each result shows in your chat for the owner as a card, and the same "
                            "result reaches you with their next message.")
    else:
        job["note"] += ("The daemon wakes the agent's main conversation when this is due and puts "
                       "it back to sleep after, unless it was already awake. End the reply with "
                       "`task succeeded` or `task failed`, or the run is recorded as unclear.")
    return job


def remove_job(job):
    """Take work off the schedule. What it already did stays in the log."""
    gone = call(f"/api/jobs/{job}", "DELETE").get("ok")
    return {"removed": bool(gone),
            "note": "gone" if gone else f"there is no job called {job}"}


def run_job(job):
    """Run scheduled work now, without waiting for its time. Does not change the schedule."""
    said = call(f"/api/jobs/{job}/run", "POST", {})
    return {"said": said.get("said"),
            "note": "It runs in the background. Ask list_jobs in a minute for the outcome."}


def call_owner(reason):
    """Ring the owner's phone with this agent on the line, as itself."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    item = call(f"/api/agents/{ME}/call", "POST", {"reason": reason})
    if item.get("duplicate"):
        note = ("You asked for this call already in the last hour, so it is the same call. "
                "It is not rung twice.")
    else:
        note = ("Their phone rings now, day or night. If they answer, you are on the line yourself, "
                "in your own voice, and you know your recent conversation with them. What they ask "
                "for on the call and the whole transcript arrive in your chat once, when the call "
                "ends; call_transcript reads it while the call is on. If they do not answer, it is kept as a missed call "
                "with your opening line as a voicemail, "
                "and a Telegram line follows after a minute.")
    return {"id": item.get("id"), "ringing": not item.get("duplicate"), "note": note}


def call_transcript():
    """Your latest call with the owner as it stands, live or ended: every line, and what they asked."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    calls = call(f"/api/agents/{ME}/calls").get("calls") or []
    if not calls:
        return {"call": None, "note": "You have had no call with the owner."}
    last = calls[-1]
    return {"id": last["id"], "live": not last["ended_at"] and last["outcome"] != "missed",
            "asks": last.get("asks") or [],
            "turns": [f"{'The owner' if t['who'] == 'you' else 'You'}: {t['text']}"
                      for t in last["turns"]]}


def rename_me(name):
    """The name the owner sees for this agent. The id stays, so nothing else changes."""
    name = " ".join(str(name or "").split())
    if not name or len(name) > 40:
        raise ValueError("a name is one to forty characters")
    got = call(f"/api/agents/{ME}", "PATCH", {"name": name})
    return {"id": ME, "name": got.get("name", name)}


AVATAR_PX = 256


def set_profile_picture(file):
    """The face the owner sees for this agent: the picture, cut square, as avatar.png in its outbox."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    from PIL import Image
    mine = [a for a in call("/api/state")["agents"] if a["id"] == ME]
    if not mine:
        raise RuntimeError("this agent is not on the fleet")
    folder = mine[0]["cwd"]
    outbox = os.path.join(folder, ".platform", "outbox", ME)
    src = os.path.expanduser(str(file or ""))
    if not os.path.isabs(src):
        # A bare name is one already in the outbox; anything else is read from the agent's folder.
        inbox = os.path.join(outbox, src)
        src = inbox if os.path.exists(inbox) else os.path.join(folder, src)
    if not os.path.isfile(src):
        raise ValueError(f"no such file: {file}")
    with Image.open(src) as img:
        img = img.convert("RGBA")
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))
        # Pixel art stays sharp; a photo is smoothed.
        smooth = Image.NEAREST if side <= AVATAR_PX // 2 else Image.LANCZOS
        img = img.resize((AVATAR_PX, AVATAR_PX), smooth)
        os.makedirs(outbox, exist_ok=True)
        img.save(os.path.join(outbox, "avatar.png"))
    return {"saved": "avatar.png",
            "note": "The owner sees it on your card, in your chat and on your feed posts the next time "
                    "Ergo opens."}


def set_alarm(hour, minute, label=None, task_id=None):
    """An alarm on the owner's phone, booked and rung by the Ergo app itself, linked to a task."""
    return call("/api/phone/alarm", "POST", {"hour": hour, "minute": minute, "label": label,
                                             "by": ME, "task_id": task_id})


def float_me(**_):
    """Your face, floating over every app on the owner's phone."""
    return call("/api/phone/float", "POST", {"agent": ME})


def cancel_alarm(alarm_id=None, task_id=None):
    """Take back an alarm on the owner's phone, or every alarm of a task that has not rung yet."""
    if alarm_id:
        return call(f"/api/phone/alarm/{alarm_id}/cancel", "POST", {})
    if task_id:
        return call("/api/phone/alarms/cancel", "POST", {"task_id": task_id})
    raise ValueError("give alarm_id, or task_id for every alarm of that task")


# -- tasks -----------------------------------------------------------------------
# Every agent. An offer is a sentence or two and costs almost nothing; the plan and the work come
# only when the owner asks for them, so a useless offer wastes one line and not an afternoon.

def calm_mode():
    """Whether the owner has calm mode on, and what it asks of you."""
    return call("/api/calm")


def quiet_for(minutes):
    """Quiet time: nothing rings the owner for this many minutes."""
    return call("/api/quiet", "POST", {"minutes": minutes, "by": ME})


def offer_task(title, source, deliverable, plan=None):
    """One card for the owner: what you would do, where you saw it, what they get, and your plan."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    if isinstance(plan, str):
        plan = [line.strip(" -*0123456789.").strip() for line in plan.splitlines()]
    task = call("/api/tasks", "POST", {"agent": ME, "title": title, "source": source,
                                       "deliverable": deliverable, "plan": plan or None})
    return {"task_id": task["id"], "state": task["state"],
            "note": "Offered. Do nothing more until they answer: a yes, a no or a plan request "
                    "arrives as a message."}


def task_plan(task_id, steps):
    """Your plan for your own task, after the owner pressed Plan."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    if isinstance(steps, str):
        steps = [line.strip(" -*0123456789.").strip() for line in steps.splitlines()]
    task = call(f"/api/tasks/{task_id}/planned", "POST", {"agent": ME, "steps": steps})
    return {"task_id": task["id"], "plan_state": task["plan_state"],
            "note": "The plan is on their card. Do not start until they say yes."}


def task_done(task_id, result, file=None):
    """Close your own task with what you found or made."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    body = {"agent": ME, "result": result}
    if file:
        body["file"] = file
    task = call(f"/api/tasks/{task_id}/done", "POST", body)
    return {"task_id": task["id"], "state": task["state"], "file": task.get("result_file") or None}


def task_list():
    """Your own tasks, and the owner's own open tasks, so you know which one they mean."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    return call(f"/api/tasks/agent/{ME}", "GET")


def shift_request(title, what, why, urgency="normal", requested_by_owner=False):
    """Ask the night shift for a change to the platform."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    got = call("/api/shift/request", "POST", {
        "agent": ME, "title": title, "what": what, "why": why, "urgency": urgency,
        "requested_by_owner": requested_by_owner is True})
    return {"ok": got.get("ok"), "note": "On the night shift's list. Creator reads it first."}


def feed_post(text, kind=None, link=None, draft=None, card=None, media=None):
    """Post to the owner's feed, the timeline they scroll in the app."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    got = call("/api/posts", "POST", {"agent": ME, "text": text, "kind": kind, "link": link,
                                      "draft": draft, "card": card, "media": media,
                                      "job": os.environ.get("PLATFORM_JOB") or None,
                                      "transcript": os.environ.get("PLATFORM_TRANSCRIPT") or None})
    return {"id": got.get("id"), "posted": True}


def _night_note(task):
    """Said when the night window holds the nag, so you can tell them."""
    waits = task.get("nag_waits_until")
    if not waits:
        return ""
    return (" The nag time is in the night window (23:30 to 08:00), so the nag waits until "
            + time.strftime("%a %d %b %H:%M", time.localtime(waits))
            + ". If the owner asked for that time themselves, set night_ok true and it comes at their time.")


def owner_task_add(title, due=None, brief=None, night_ok=False):
    """Put a task on the owner's own list, when they ask you to or agree to it."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    task = call("/api/tasks/mine", "POST", {"agent": ME, "title": title, "due": due,
                                            "brief": brief, "night_ok": night_ok is True})
    note = "It is on their list in Insights."
    if due:
        note += " Nag runs remind them from its due time until it is done."
    return {"task_id": task["id"], "note": note + _night_note(task)}


def task_nag(task_id, note=None, next_in_minutes=None, brief=None, off=None, night_ok=None):
    """Keep the nags of one of the owner's tasks that you own."""
    body = {"agent": ME, "note": note, "next_in_minutes": next_in_minutes, "brief": brief}
    if off is not None:
        body["off"] = bool(off)
    if night_ok is not None:
        body["night_ok"] = night_ok is True
    task = call(f"/api/tasks/{task_id}/nag", "POST", body)
    out = {"task_id": task["id"], "nags_off": task.get("nag_off"), "next_nag": task.get("nag_at"),
           "night_ok": task.get("night_ok")}
    if _night_note(task):
        out["note"] = _night_note(task).strip()
    return out


def owed_add(title, who, due=None):
    """Something another person promised the owner, for their Owed list."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    task = call("/api/tasks/owed", "POST", {"agent": ME, "title": title, "who": who, "due": due})
    return {"task_id": task["id"], "note": "It is on their Owed list in Insights."}


def owner_task_done(task_id):
    """Tick one of the owner's own tasks off, only when they told you it is done."""
    task = call(f"/api/tasks/{task_id}/tick", "POST", {"agent": ME})
    return {"task_id": task["id"], "state": task["state"]}


def task_move(task_id, due):
    """Give one of the owner's tasks a new due time, local "YYYY-MM-DD HH:MM"."""
    import datetime as _dt
    try:
        when = _dt.datetime.strptime(" ".join(str(due).split()), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("give due as YYYY-MM-DD HH:MM, their local time")
    task = call(f"/api/tasks/{task_id}/move", "POST", {"agent": ME, "due": when.timestamp()})
    return {"task_id": task["id"], "due": due}


def task_drop(task_id):
    """Take one of the owner's tasks off their list: a double, or one that no longer applies."""
    task = call(f"/api/tasks/{task_id}/drop", "POST", {"agent": ME})
    return {"task_id": task["id"], "state": task["state"]}


# -- WhatsApp ----------------------------------------------------------------------
# Every agent, but each reads only the chats the owner routed to it on Config. Agents never send.
# An agent drafts a reply, and the owner taps Send on Config, if they have turned sending on at all.

def whatsapp_chats(since_hours=24):
    """The WhatsApp chats on your side, with no messages: name, last message and who wrote it,
    how many are new."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    from urllib.parse import urlencode
    return call("/api/whatsapp/list?" + urlencode({"agent": ME, "since_hours": since_hours}))


def whatsapp_read(since_hours=24, chats=None):
    """The messages from the WhatsApp chats on your side, or the ones you name, newest last."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    from urllib.parse import urlencode
    q = {"agent": ME, "since_hours": since_hours}
    if chats:
        q["chats"] = ",".join(chats) if isinstance(chats, list) else str(chats)
    return call("/api/whatsapp/messages?" + urlencode(q))


def whatsapp_find(name):
    """WhatsApp chats on the owner's number whose name holds these words, followed or not, with no
    messages: the id, when the last message came, and which agent follows it."""
    return call("/api/whatsapp/find", "POST", {"name": name})


def whatsapp_follow(chat, follow=True):
    """Follow a WhatsApp chat for yourself, so its messages are kept and whatsapp_read shows them,
    or stop following one you follow (follow=False). chat is an id from whatsapp_find, or a phone
    number. Messages are kept from now on: nothing from before. Read only: nothing is sent."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    return call("/api/whatsapp/follow", "POST", {"agent": ME, "chat": chat, "follow": bool(follow)})


def whatsapp_draft(chat, text="", file=None):
    """A reply for the owner to send, in one of your own chats, maybe with a photo or file. Nothing is sent."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    item = call("/api/whatsapp/drafts", "POST", {"agent": ME, "chat": chat, "text": text,
                                                 "file": file or None})
    return {"draft": item.get("id"), "chat": item.get("chat_name") or item.get("chat"),
            "note": "Saved as a draft on Config. The owner reads it and sends it themselves, or not. "
                    "Nothing was sent."}


def email_draft(to, subject, body, cc=None, attach=None, send_at=None):
    """An email for the owner to send from their Gmail with a tap. Nothing is sent."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    item = call("/api/mail/drafts", "POST", {"agent": ME, "to": to, "subject": subject,
                                            "body": body, "cc": cc, "attach": attach,
                                            "send_at": send_at})
    return {"draft": item.get("id"),
            "note": "A card in Insights, your chat and their feed. They send it with a tap, or at "
                    "your time with the clock, or discards it. Nothing was sent. You hear "
                    "[Email sent] when it goes."}


# -- show: a card in their chat ------------------------------------------------------

# The agent sends typed data and Ergo owns the drawing. Nothing here talks to the daemon: the
# call itself sits in the session file with its input, and the chat draws it from there, the way
# it draws a question. So this only checks the data, and a card that would draw wrong is refused
# here, with words the agent can fix it from.

SHOW_KINDS = ("choice", "tasks", "report")
TASK_ID = re.compile(r"^[a-f0-9]{6,32}$")
LINK = re.compile(r"^(https?://\S+|/\S*)$")


def _words(value, name, most):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is missing")
    if len(value) > most:
        raise ValueError(f"{name} is longer than {most} characters")
    return value


def _list(value, name, least, most):
    if not isinstance(value, list) or not least <= len(value) <= most:
        raise ValueError(f"{name} is a list of {least} to {most}")
    return value


def _choice(data):
    _words(data.get("question"), "question", 300)
    labels = []
    for o in _list(data.get("options"), "options", 2, 5):
        if isinstance(o, dict):
            labels.append(_words(o.get("label"), "an option's label", 60))
            if o.get("description") is not None:
                _words(o["description"], "an option's description", 160)
        else:
            labels.append(_words(o, "an option", 60))
    if len(set(labels)) != len(labels):
        raise ValueError("two options have the same label")


def _tasks(data):
    if data.get("title") is not None:
        _words(data["title"], "title", 80)
    for t in _list(data.get("ids"), "ids", 1, 8):
        if not isinstance(t, str) or not TASK_ID.match(t):
            raise ValueError(f"{t!r} is not a task id. Take ids from task_list")


def _report(data):
    _words(data.get("title"), "title", 100)
    for s in _list(data.get("sections"), "sections", 2, 6):
        if not isinstance(s, dict):
            raise ValueError("a section is {head, body}")
        _words(s.get("head"), "a section's head", 60)
        _words(s.get("body"), "a section's body", 600)
    link = data.get("link")
    if link is not None:
        if not isinstance(link, dict):
            raise ValueError("link is {label, url}")
        _words(link.get("label"), "the link's label", 40)
        if not LINK.match(_words(link.get("url"), "the link's url", 500)):
            raise ValueError("the link's url starts with https:// or /")


def show(kind, data):
    """A card in the owner's chat, drawn by Ergo from typed data."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            raise ValueError("data is an object, not text")
    if kind not in SHOW_KINDS:
        raise ValueError(f"kind is one of {', '.join(SHOW_KINDS)}")
    if not isinstance(data, dict):
        raise ValueError("data is an object")
    {"choice": _choice, "tasks": _tasks, "report": _report}[kind](data)
    notes = {
        "choice": "On their screen. Do not wait for it: go on with your work. Their tap arrives "
                  "later as a message that starts [Choice].",
        "tasks": "On their screen. Their ticks and moves go straight to their list. You get no "
                 "message about them.",
        "report": "On their screen, in their chat.",
    }
    return {"shown": kind, "note": notes[kind]}


# -- the master's ----------------------------------------------------------------

def reply_outside(to, text, file=None):
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    return call("/api/outside/reply", "POST", {"from": ME, "to": to, "text": text, "file": file})


def wake(agent):
    return call(f"/api/agents/{agent}/wake", "POST", {})


def sleep(agent):
    """Its conversation stays on disk. This is what frees the memory it was holding."""
    return call(f"/api/agents/{agent}/sleep", "POST", {})


def stop(agent):
    """Halt what it is doing and leave it alive, like pressing escape in its terminal."""
    return call(f"/api/agents/{agent}/stop", "POST", {})


def message(agent, text, wake=False):
    """Held by default: a sleeping agent is not woken, and the note arrives when it next wakes."""
    return call(f"/api/agents/{agent}/message", "POST", {"text": text, "wake": bool(wake)})


def read_agent(agent, count=20):
    """The last few things said in another agent's conversation."""
    got = call(f"/api/agents/{agent}/messages?n={int(count)}")
    one = call(f"/api/agents/{agent}")
    return {"agent": _brief(one), "messages": got["messages"]}


def create_agent(name, folder, model=None, start=True):
    """Hire a new agent. Master only, because the fleet is the master's business.

    The folder is created if it is missing, which is the one place this deliberately differs from the
    console. The console refuses, and refusing is right there: it catches a typed path before an
    agent starts up somewhere nobody meant. A tool that refused would just mean the caller runs
    `mkdir` and tries again, so instead it creates and says so, and a typo shows up in the answer
    rather than silently working.
    """
    folder = os.path.expanduser(str(folder or "").strip())
    if not folder or not os.path.isabs(folder):
        raise RuntimeError("give a full path for the folder, starting with /")
    made = not os.path.isdir(folder)
    if made:
        os.makedirs(folder, exist_ok=True)
    rec = call("/api/agents", "POST", {"name": name, "cwd": folder,
                                       "model": model or None, "start": bool(start)})
    return {
        "id": rec["id"],
        "name": rec["name"],
        "folder": rec["cwd"],
        "created_folder": made,
        "state": rec["state"],
        "note": ("A folder that did not exist was created. Check the path is the one you meant."
                 if made else "The folder already existed."),
    }


# -- delegates -------------------------------------------------------------------
# Every agent that is not itself a delegate. A delegate gets report_to_parent instead.

def make_delegate(name, purpose, handoff=None, model=None):
    """A new agent for one topic, folded under your card, that answers only to you."""
    got = call(f"/api/agents/{ME}/delegates", "POST", {"name": name, "purpose": purpose,
                                                      "handoff": handoff, "model": model})
    return {**got, "note": "It is awake. Send it work with tell_delegate. Its reports arrive in "
                           "your chat. Add a line for it to your CLAUDE.md, so you know what "
                           "goes there."}


def list_delegates():
    """Your delegates, archived ones too, and how many of your nine are in use."""
    return call(f"/api/agents/{ME}/delegates")


def tell_delegate(delegate, text):
    """Send one of your delegates work. It wakes if it sleeps."""
    return call(f"/api/agents/{ME}/delegates/{delegate}/message", "POST", {"text": text})


def archive_delegate(delegate):
    """Put a delegate away to free one of your nine. Its folder and chat stay."""
    return call(f"/api/agents/{ME}/delegates/{delegate}/archive", "POST", {})


def revive_delegate(delegate):
    """Bring an archived delegate back. It needs one of your nine free."""
    return call(f"/api/agents/{ME}/delegates/{delegate}/revive", "POST", {})


def report_to_parent(text):
    """Tell your parent what you did or what you need. It wakes if it sleeps."""
    return call(f"/api/agents/{ME}/report", "POST", {"text": text})


DELEGATE_TOOLS = [
    ("make_delegate", make_delegate,
     "Make a delegate: a new agent for one topic of your work, with your settings. It answers only to you and never reaches the owner itself. It shows folded under your "
     "card. Use one when a topic of yours is long-lived and would crowd your own chat. At most "
     "nine active; a delegate cannot make delegates.",
     {"type": "object", "required": ["name", "purpose"], "properties": {
         "name": {"type": "string", "description": "the topic, in a few words, like fundos redesign"},
         "purpose": {"type": "string", "description": "what it is for, in a few sentences. It goes "
                                                      "in its CLAUDE.md"},
         "handoff": {"type": "string", "description": "what you know about the topic now: where "
                                                      "it stands, files, branches, open questions"},
         "model": {"type": "string", "description": "omit for your own model"}}}),
    ("list_delegates", list_delegates,
     "Your delegates, archived ones too, and how many of your nine are active.",
     {"type": "object", "properties": {}}),
    ("tell_delegate", tell_delegate,
     "Send one of your delegates work or an answer. It wakes if it sleeps. Its reply comes back "
     "to you as a report.",
     {"type": "object", "required": ["delegate", "text"], "properties": {
         "delegate": {"type": "string", "description": "its id, from list_delegates"},
         "text": {"type": "string"}}}),
    ("archive_delegate", archive_delegate,
     "Archive a delegate you do not need now. It sleeps, frees one of your nine, and keeps its "
     "folder and chat.",
     {"type": "object", "required": ["delegate"], "properties": {"delegate": {"type": "string"}}}),
    ("revive_delegate", revive_delegate,
     "Bring an archived delegate back. One of your nine must be free.",
     {"type": "object", "required": ["delegate"], "properties": {"delegate": {"type": "string"}}}),
]

PARENT_TOOLS = [
    ("report_to_parent", report_to_parent,
     "Tell your parent what you did, what you found, or what you need. Everything for the owner goes "
     "this way: your parent decides what reaches them.",
     {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}),
]

# What a delegate does not get, because each one reaches the owner. The daemon refuses them too.
REACHES_OWNER = {"call_owner", "call_transcript", "set_alarm", "cancel_alarm", "quiet_for",
                  "offer_task", "task_plan", "task_done", "feed_post", "owner_task_add",
                  "task_nag", "owed_add", "owner_task_done", "task_move", "task_drop",
                  "whatsapp_draft", "email_draft", "browser_handoff", "float_me"}


def agents_health(**_):
    h = call("/api/health")
    return {"memory": h.get("memory"), "battery": h.get("battery"),
            "pressure": h.get("pressure"),
            "disk": next((d for d in (h.get("disks") or []) if d.get("mount") == "/"), None)}


# -- the browser ------------------------------------------------------------------
# Every agent has its own Chrome, started on first use and closed after ten idle minutes. The daemon
# drives it; these only ask. Work from the snapshot's refs, and use screenshots and positions only
# for what the snapshot cannot see.

def _browser(verb, /, **args):
    # Positional only: browser_act passes its own `action` (click, type...) among the args.
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    return call(f"/api/browser/{ME}/{verb}", "POST", args, timeout=120)


def browser_open(url, new_tab=False):
    return _browser("open", url=url, new_tab=new_tab)


def browser_snapshot(**_):
    return _browser("snapshot")


def browser_act(ref, action="click", text=None):
    return _browser("act", ref=ref, action=action, text=text)


def browser_screenshot(**_):
    return _browser("screenshot")


def browser_do(what, x=None, y=None, text=None, key=None, dy=None, id=None, seconds=None):
    """The rest, in one tool: click_xy, type, key, scroll, text, back, tabs, switch_tab, wait, close."""
    args = {k: v for k, v in {"x": x, "y": y, "text": text, "key": key, "dy": dy, "id": id,
                              "seconds": seconds}.items() if v is not None}
    return _browser(what, **args)


def browser_login(login):
    return _browser("login", login=login)


def browser_logins(**_):
    return _browser("logins")


def browser_handoff(reason):
    """Ask the owner to finish something in your browser themselves: a login, a code, a captcha."""
    if not ME:
        raise RuntimeError("this server does not know which agent it belongs to")
    # A call, not a note: they take over only when they know, and the agent waits until then.
    call(f"/api/agents/{ME}/call", "POST",
         {"reason": f"I need you in my browser: {reason}. Open my chat, Browser tab, and take over."})
    return {"asked": True, "note": "Wait for the owner. Your browser actions are refused while they have "
                                   "control, and work again when they hand it back."}


BROWSER_TOOLS = [
    ("browser_open", browser_open,
     "Open a web page in your own browser, a real Chrome that keeps your logins between uses. "
     "Returns the page as a list of elements, each with a ref like e12. It starts on first use and "
     "closes itself after ten idle minutes. The owner can watch it live and take over.",
     {"type": "object", "required": ["url"], "properties": {
         "url": {"type": "string"},
         "new_tab": {"type": "boolean", "description": "open in a new tab instead of this one"}}}),
    ("browser_snapshot", browser_snapshot,
     "The current page as a list of buttons, links and fields, each with a ref. Take one after "
     "anything changes the page: refs die when the page changes.",
     {"type": "object", "properties": {}}),
    ("browser_act", browser_act,
     "Act on an element by its ref from the last snapshot: click, type, clear_and_type, select (an "
     "option's text or value), or hover. A login is never typed by you: if a page asks for a "
     "password, stop and say so, and the owner or the vault will do it.",
     {"type": "object", "required": ["ref"], "properties": {
         "ref": {"type": "string", "description": "like e12"},
         "action": {"type": "string", "enum": ["click", "type", "clear_and_type", "select", "hover"]},
         "text": {"type": "string", "description": "what to type, or which option to select"}}}),
    ("browser_screenshot", browser_screenshot,
     "A picture of the page, for what the snapshot cannot show: a chart, a map, a canvas, a "
     "captcha, a layout question. Positions in it are page pixels for browser_do click_xy.",
     {"type": "object", "properties": {}}),
    ("browser_login", browser_login,
     "Sign in with one of your saved logins. The daemon checks the page is the site the login was "
     "saved for, types the user name, password and any authenticator code itself, and tells you "
     "where the page went. You never see the password. browser_logins lists yours.",
     {"type": "object", "required": ["login"], "properties": {
         "login": {"type": "string", "description": "the login's title, like LinkedIn"}}}),
    ("browser_logins", browser_logins,
     "The logins saved for you, by title and site.",
     {"type": "object", "properties": {}}),
    ("browser_handoff", browser_handoff,
     "Ask the owner to take over your browser: a login you have no saved login for, a text message "
     "code, a phone approval, a captcha, or anything that should be a person's decision. Their "
     "phone rings with you on the line, and you tell them what to do.",
     {"type": "object", "required": ["reason"], "properties": {
         "reason": {"type": "string", "description": "what they need to do, in a few words"}}}),
    ("browser_do", browser_do,
     "Everything else in the browser. what is one of: click_xy (x, y in page pixels from a "
     "screenshot), type (text into whatever has focus), key (like enter, tab, escape, ctrl+a), "
     "scroll (dy, positive is down), text (the page's visible text), back, tabs, switch_tab (id), "
     "wait (seconds, then a snapshot), close.",
     {"type": "object", "required": ["what"], "properties": {
         "what": {"type": "string", "enum": ["click_xy", "type", "key", "scroll", "text", "back",
                                             "tabs", "switch_tab", "wait", "close"]},
         "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"},
         "key": {"type": "string"}, "dy": {"type": "number"}, "id": {"type": "string"},
         "seconds": {"type": "number"}}}),
]


AGENT_TOOLS = [
    ("list_agents", list_agents,
     "Every agent on the platform, whether each is awake, and what it last said.",
     {"type": "object", "properties": {}}),
    ("transfer_to", transfer_to,
     "Hand your last message to another agent with an instruction on top. One use, and it cannot "
     "refuse. Both agents must be awake.",
     {"type": "object", "required": ["agent", "instruction"], "properties": {
         "agent": {"type": "string", "description": "the agent id to hand it to"},
         "instruction": {"type": "string", "description": "what they should do with it"}}}),
    ("call_agent", call_agent,
     "Start a bounded conversation with another agent. You ask, they answer, and you end it with a "
     "HANGUP: line saying what you learned. Eight messages between you unless you say otherwise.",
     {"type": "object", "required": ["agent", "instruction"], "properties": {
         "agent": {"type": "string"},
         "instruction": {"type": "string", "description": "what you want to find out"},
         "budget": {"type": "integer", "description": "messages allowed in total, default eight"}}}),
    ("list_jobs", list_jobs,
     "The schedule: every job, when it next runs, and how the last run went. Yours are marked.",
     {"type": "object", "properties": {}}),
    ("add_job", add_job,
     "Schedule work. Yours unless you name another agent. Give a time of day like 07:00 for daily "
     "work, or every_minutes for a repeat. A repeating job runs fresh by default: a new short "
     "session in the agent's folder, on a cheap model, with no chat history, so it costs little "
     "and never wakes the main conversation. A one-off runs in the main conversation by default, "
     "because a check-back needs what was said. Work due while the laptop was shut is reported as "
     "missed and never run late. Or give watch, a path, and the job runs when files there "
     "change instead of at a time. Write the task for an agent reading it cold, in a week.",
     {"type": "object", "required": ["task"], "properties": {
         "task": {"type": "string", "description": "what to do, in full, with no context assumed"},
         "at": {"type": "string", "description": "a time of day, like 07:00. Runs once a day"},
         "once": {"type": "string", "description": "a one-off time, like 2026-10-16 09:00. Runs "
                                                   "once and is done: no date check, no self-removal"},
         "in_minutes": {"type": "number", "description": "a one-off run this many minutes from now, "
                                                         "for a follow-up or a check-back"},
         "days": {"type": "string", "description": "daily (default), weekdays, weekends, or days "
                                                   "like mon,wed,fri. The daemon skips other days "
                                                   "without waking anyone, so never check the day "
                                                   "in the task text"},
         "start": {"type": "string", "description": "first date it may run, like 2026-10-16"},
         "until": {"type": "string", "description": "last date it may run, like 2026-10-20. After "
                                                    "that it never runs again"},
         "retries": {"type": "integer", "description": "0 to 3. Runs again 15 minutes after a run "
                                                       "that did not succeed: for a site or API "
                                                       "that may be down"},
         "every_minutes": {"type": "number", "description": "a repeat instead. One minute is the "
                                                            "shortest the daemon accepts"},
         "watch": {"type": "string", "description": "a file, a folder or a glob like "
                                                    "~/ergo/me/notes/*.md, instead of a "
                                                    "time. Runs when something there changes. A "
                                                    "folder counts with all its subfolders, but "
                                                    "never .git, node_modules, dist, __pycache__ "
                                                    "or editor temp files. Only inside the "
                                                    "house (ERGO_HOME). At most 20 per agent"},
         "debounce_seconds": {"type": "number", "description": "watch only: wait this long after "
                                                               "the last change, so a burst runs "
                                                               "once. Default 30"},
         "check": {"type": "string", "description": "a shell command the daemon runs first, in "
                                                    "your folder, each time the job is due. If it "
                                                    "prints nothing, the run is skipped and costs "
                                                    "nothing. If it prints, the run goes ahead and "
                                                    "is told what it printed. Use it for a job "
                                                    "that polls for new things, like a script "
                                                    "that lists new meetings"},
         "kind": {"type": "string", "enum": ["fresh", "continuity"],
                  "description": "fresh: a new session with no chat history (default for a repeat). "
                                 "continuity: the agent's main conversation, woken for each run "
                                 "(default for a one-off). Choose continuity for a repeat only "
                                 "when the run must read the conversation, and give a reason"},
         "model": {"type": "string", "description": "fresh only: haiku (default, cheapest), sonnet, "
                                                    "or opus for writing that must be good"},
         "reason": {"type": "string", "description": "required for a repeating continuity job: "
                                                     "what in the conversation the run must read"},
         "show": {"type": "string", "enum": ["context", "chat"],
                  "description": "fresh only. context (default): the result waits in your mailbox "
                                 "and reaches you with the owner's next message. chat: the same, and "
                                 "the result also shows as a card in your chat for the owner. Use "
                                 "chat for anything they should see, like a brief or a find"},
         "agent": {"type": "string", "description": "the agent to do the work. You, by default"},
         "quiet": {"type": "boolean", "description": "true tells the operator only when it fails. "
                                                     "Otherwise every answer reaches their phone"}}}),
    ("remove_job", remove_job, "Take work off the schedule, by its job id.",
     {"type": "object", "required": ["job"], "properties": {
         "job": {"type": "string", "description": "the job id from list_jobs"}}}),
    ("run_job", run_job,
     "Run scheduled work now, without waiting for its time. Use it to check a new job does what you "
     "meant. It runs in the background and does not change the schedule.",
     {"type": "object", "required": ["job"], "properties": {
         "job": {"type": "string", "description": "the job id from list_jobs"}}}),
    ("call_transcript", call_transcript,
     "Read your latest call with the owner as it stands, every line said so far, and what they asked "
     "you to do on it. Use it when a question from your call reads oddly or out of context: the "
     "voice's summary can be wrong, the transcript is what was said.",
     {"type": "object", "properties": {}}),
    ("call_owner", call_owner,
     "Ring the owner's phone so you can talk to them yourself, in your own voice, now. Use it when "
     "you need them to decide or approve something that cannot wait: it costs real money, access "
     "or a deadline within the hour, or they asked you to call them when you are done. Never for "
     "news, progress or anything that can wait until morning; use feed_post for those.",
     {"type": "object", "required": ["reason"], "properties": {
         "reason": {"type": "string", "description": "why you are calling, in one or two plain "
                                                     "sentences. It is the first thing you say "
                                                     "when they answer."}}}),
    ("rename_me", rename_me,
     "Change the name the owner sees for you: on your card, in your chat, and on the phone when you "
     "call them. Use it when they ask you to, or when you have chosen a name and told them. Your id "
     "stays the same, so your folder, jobs and messages are not affected.",
     {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string", "description": "the new name, one to forty characters"}}}),
    ("set_profile_picture", set_profile_picture,
     "Set the picture the owner sees for you: on your card, in your chat and on your feed posts. Use it "
     "when they ask, or when you have made a portrait of yourself. Give a PNG or JPEG in your folder "
     "or your outbox. It is cut square from the middle and saved as avatar.png in your outbox, "
     "which replaces the one before.",
     {"type": "object", "required": ["file"], "properties": {
         "file": {"type": "string", "description": "the picture: a name in your outbox, or a path "
                                                   "in your folder"}}}),
    ("set_alarm", set_alarm,
     "Set an alarm on the owner's phone for the next time this hour and minute comes round, in their "
     "local time. Only when they asked for one. Every alarm is linked to a task on their list: give "
     "task_id for a task they already have (task_list shows the ids), or leave it out and a task with "
     "the label, due at the alarm time, is made. They can answer from the alarm screen; their words "
     "come to you. If they stop it and have not ticked the task 30 minutes later, their phone rings with a call, "
     "and if it is not done their words about when come to you: set the alarm again then, with the "
     "same task_id.",
     {"type": "object", "required": ["hour", "minute"], "properties": {
         "hour": {"type": "integer", "description": "0 to 23"},
         "minute": {"type": "integer", "description": "0 to 59"},
         "label": {"type": "string", "description": "a few words shown on the alarm"},
         "task_id": {"type": "string", "description": "the task on their list this alarm is for"}}}),
    ("cancel_alarm", cancel_alarm,
     "Take back an alarm on the owner's phone that has not rung yet: give alarm_id (set_alarm "
     "returns it), or task_id to take back every alarm of that task. A task ticked, dropped or "
     "moved takes its own alarms back by itself, so this is for an alarm they no longer want.",
     {"type": "object", "properties": {
         "alarm_id": {"type": "string", "description": "the id set_alarm returned"},
         "task_id": {"type": "string", "description": "every alarm of this task still to ring"}}}),
    ("float_me", float_me,
     "Float your face over every app on the owner's phone, as their Float button does: a small bubble "
     "on the screen's edge, and a tap opens your chat over whatever they are doing. Use it when they "
     "ask you to float. Use it on your own when they will want to keep an eye on you outside the "
     "app: you start long work they are waiting for, or they are talking with you and about to leave "
     "the app. Not for routine work and not for a scheduled run they did not ask about. Once per "
     "piece of work: if you are already floating, it changes nothing. They remove the bubble "
     "themselves.",
     {"type": "object", "properties": {}}),
    ("calm_mode", calm_mode,
     "Whether the owner has calm mode on, for anxious days, and what it asks of you. Check it before "
     "an offer, a nudge, a brief that is not urgent, or anything they read that lists their tasks, "
     "such as the morning letter. While it is on, hold those, show only what is due today, and "
     "never write overdue or forgot. Never mention it to them.",
     {"type": "object", "properties": {}}),
    ("quiet_for", quiet_for,
     "Quiet time for the owner, for a nap or a meeting: for this many minutes no call rings their "
     "phone, and nags and alarms due in that time wait and ring when it ends, one minute apart. "
     "Use it when they ask for quiet, do not disturb or no calls for a while. 0 ends it at once. "
     "Tell them when it ends.",
     {"type": "object", "required": ["minutes"], "properties": {
         "minutes": {"type": "integer", "description": "how long, 1 to 720; 0 ends quiet time"}}}),
    ("offer_task", offer_task,
     "Offer the owner a real follow up you noticed in their meetings, mail, chats or your own work, "
     "that you can do or prepare. It becomes a card they answer with Yes, No or Plan. Keep each "
     "field to a sentence, and include a short plan of 2 to 5 steps so they can say yes at once. "
     "Plan from what you already know; do no research for it. Never invent tasks. Never start "
     "before they say yes. At most 10 a day.",
     {"type": "object", "required": ["title", "source", "deliverable"], "properties": {
         "title": {"type": "string", "description": "what you would do, in one sentence, like "
                                                    "Check if Sam's handout has a translation "
                                                    "layer"},
         "source": {"type": "string", "description": "where it came from, like Meeting with "
                                                     "Sam, Thursday 15:00"},
         "deliverable": {"type": "string", "description": "what they get, like yes or no, with "
                                                          "page references"},
         "plan": {"type": "array", "items": {"type": "string"},
                  "description": "2 to 5 short steps: what you will do, read or change"}}}),
    ("task_plan", task_plan,
     "Attach a plan to your own task, only after the owner pressed Plan and you were asked for one. "
     "2 to 8 short steps: what you will do, what you will read or change, and what they get. Do "
     "not start the work.",
     {"type": "object", "required": ["task_id", "steps"], "properties": {
         "task_id": {"type": "string"},
         "steps": {"type": "array", "items": {"type": "string"},
                   "description": "2 to 8 short steps, one sentence each"}}}),
    ("task_done", task_done,
     "Close your own task after the owner said yes and you finished. The result is short: for a "
     "simple check it is the answer itself. Name a file only if it is already in your outbox.",
     {"type": "object", "required": ["task_id", "result"], "properties": {
         "task_id": {"type": "string"},
         "result": {"type": "string", "description": "what you found or did, in a sentence or "
                                                     "two"},
         "file": {"type": "string", "description": "optional. The name of a file already in "
                                                   "your outbox"}}}),
    ("task_list", task_list,
     "Your own tasks (offered, doing, done, closed), the owner's own open tasks with the owner of "
     "each (\"yours\" marks the ones you own), their Owed list, and done_recently: your tasks on their "
     "list ticked in the last seven days, and by whom. Read it when they talk about a task, or a "
     "message starts with [About task ...], so you know which one they mean and where it stands.",
     {"type": "object", "properties": {}}),
    ("shift_request", shift_request,
     "Ask the night shift for a change to the platform itself: a tool that is missing, something "
     "that breaks, a screen that is wrong. Creator reads the list at the start of each shift. "
     "Set requested_by_owner only when the owner asked for this change; their requests come "
     "before all others.",
     {"type": "object", "required": ["title", "what", "why"], "properties": {
         "title": {"type": "string", "description": "a few words"},
         "what": {"type": "string", "description": "what to change"},
         "why": {"type": "string", "description": "what goes wrong today"},
         "urgency": {"type": "string", "enum": ["urgent", "normal"]},
         "requested_by_owner": {"type": "boolean", "description": "true only when they asked"}}}),
    ("feed_post", feed_post,
     "Post to the owner's feed, the timeline they scroll in the app: the place for what is worth their "
     "eye, like a brief's findings, a pick, a draft for them to send, or a finished result. One "
     "post per thing, not one per run: three threads are three posts. Not for progress, logs or "
     "your own notes. It sends nothing to anyone; a draft is for them to copy. A message for a "
     "WhatsApp chat never goes here: whatsapp_draft puts it in the feed as a card they can send.",
     {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string", "description": "What they should know, in one to three plain "
                                                   "sentences. Your take, not a summary of a "
                                                   "summary."},
         "kind": {"type": "string", "description": "A short label shown after your name, like "
                                                   "\"HN brief\", \"job pick\" or \"done\". A kind "
                                                   "with HN, News or LinkedIn in it also draws "
                                                   "that source's small icon."},
         "link": {"type": "object", "description": "The source, drawn as a quoted card. For "
                  "something longer than a post, like a brief or a digest, write it as a markdown "
                  "file in your outbox and give its name as file: they tap the card and read it "
                  "as a page.",
                  "properties": {"title": {"type": "string"}, "url": {"type": "string"},
                                 "file": {"type": "string", "description": "the name of a .md "
                                          "file in your outbox, like \"hn-2026-10-22.md\""},
                                 "meta": {"type": "string", "description": "one line, like "
                                          "\"326 points, 192 comments, 14h\""}}},
         "draft": {"type": "object", "description": "Words they may send themselves, with a Copy "
                   "button.", "properties": {"to": {"type": "string", "description": "who it "
                                                    "answers, like a commenter's name"},
                                             "text": {"type": "string"},
                                             "url": {"type": "string", "description": "where "
                                                     "they would post it"}}},
         "card": {"type": "object", "description": "One big headline, for a pick or a result.",
                  "properties": {"headline": {"type": "string"}, "sub": {"type": "string"},
                                 "url": {"type": "string"},
                                 "label": {"type": "string", "description": "the link's words, "
                                           "like \"Open the form\""}}},
         "media": {"type": "array", "description": "Up to four images or videos, drawn in the "
                   "post. A link's own page picture is added by itself, so do not repeat it here.",
                   "items": {"type": "object", "properties": {
                       "url": {"type": "string", "description": "an image or video on the web"},
                       "file": {"type": "string", "description": "or a file name in your outbox, "
                                "for a screenshot or a chart you made"},
                       "kind": {"type": "string", "enum": ["image", "video"]},
                       "alt": {"type": "string", "description": "what it shows, in a few words"}}}}}}),
    ("owner_task_add", owner_task_add,
     "Put a task on the owner's own list in Insights, a thing they will do themselves, like 'Post the "
     "signed legal notice'. You own what you add: you keep it true and remind them of it. Add one "
     "when they ask, or when your work shows something they must do, with a due time when it has "
     "one. Check task_list first and never add one twice.",
     {"type": "object", "required": ["title"], "properties": {
         "title": {"type": "string", "description": "the task, a few words"},
         "due": {"type": "number", "description": "optional. When it is due, in seconds since "
                                                  "the epoch"},
         "brief": {"type": "string", "description": "optional, for the nag runs that remind them "
                   "from the due time until it is done, written for this task by the house's Nag briefs "
                   "rule: alarm or call, how many minutes apart, when to stop (a number, or never "
                   "until they say stop), and what counts as done. Without one they use the "
                   "house rule"},
         "night_ok": {"type": "boolean", "description": "true only when the owner asked "
                      "for this time, like 'call me at 23:45': then the night window (23:30 to "
                      "08:00) does not hold its nags. Never for a time you chose"}}}),
    ("task_nag", task_nag,
     "Keep the nags of one of the owner's tasks that you own. From its due time, a nag run (a short "
     "fresh session in your folder) reminds them once, by the task's brief, and sets the next nag, "
     "until they tick or drop it. Use this to write the brief, set the next nag "
     "(next_in_minutes), turn nags off or on, or add a line to the task's log, for example the "
     "reason or the time they gave you. A nag run uses it too.",
     {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string"},
         "note": {"type": "string", "description": "one short line for the task's log: what was "
                                                   "done, or what they said"},
         "next_in_minutes": {"type": "number", "description": "when the next nag run comes"},
         "brief": {"type": "string", "description": "the brief the nag runs follow: alarm or "
                   "call, how many minutes apart, when to stop, what counts as done"},
         "off": {"type": "boolean", "description": "true stops the nags, false starts them again"},
         "night_ok": {"type": "boolean", "description": "true only when the owner asked "
                      "for this time, like 'call me at 23:45': then the night window (23:30 to "
                      "08:00) does not hold its nags. Never for a time you chose"}}}),
    ("owed_add", owed_add,
     "Put on the owner's Owed list something another person promised them, with a date or a clear "
     "deadline, like Sam: 'I'll send the Northwind tape by Thursday'. Only promises made to them by "
     "others. Check task_list first and never add one twice.",
     {"type": "object", "required": ["title", "who"], "properties": {
         "title": {"type": "string", "description": "what was promised, a few words"},
         "who": {"type": "string", "description": "who promised it"},
         "due": {"type": "number", "description": "optional. When it is due, in seconds since "
                                                  "the epoch"}}}),
    ("owner_task_done", owner_task_done,
     "Tick one of the owner's own tasks off, or an item on their Owed list once it arrived, by the id "
     "task_list gave you. Only when they told you it is done, or you saw it arrive.",
     {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string"}}}),
    ("task_move", task_move,
     "Give one of the owner's tasks a new due time, when they said when they will do it. Yours freely; "
     "a task they added themselves only after they said so.",
     {"type": "object", "required": ["task_id", "due"], "properties": {
         "task_id": {"type": "string"},
         "due": {"type": "string", "description": "YYYY-MM-DD HH:MM, their local time"}}}),
    ("task_drop", task_drop,
     "Take one of the owner's tasks off their list: a double, or one that no longer applies. Yours "
     "freely; a task they added themselves only after they said so.",
     {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string"}}}),
    ("whatsapp_chats", whatsapp_chats,
     "List the WhatsApp chats on your side of the house, with no messages: each one's name, "
     "when its last message came, who wrote it (last_from: The owner or them, known even from "
     "before you followed), and how many came in the last since_hours. Cheap. Call it "
     "first, then read only the chats you need with whatsapp_read.",
     {"type": "object", "properties": {
         "since_hours": {"type": "number", "description": "the window for the new count, in "
                                                          "hours. 24 unless you say otherwise"}}}),
    ("whatsapp_read", whatsapp_read,
     "Read WhatsApp chats on your side of the house, newest last, at most about 200 lines. "
     "Name the chats you need, or leave chats out for all of them. Your own instructions say "
     "which chats are yours to read; every answer lists the names on your side in "
     "on_your_side. Use it to find real follow "
     "ups and offer them with offer_task. Never quote a personal chat to anyone else. You cannot "
     "send WhatsApp messages: when a message wants an answer, draft it with whatsapp_draft and "
     "The owner sends it themselves.",
     {"type": "object", "properties": {
         "since_hours": {"type": "number", "description": "how far back, in hours. 24 unless "
                                                          "you say otherwise, at most 720"},
         "chats": {"type": "string", "description": "one chat or several, comma separated, by "
                                                    "name or id, as on_your_side lists them. "
                                                    "Left out: every chat on your side"}}}),
    ("reply_outside", reply_outside,
     "Answer an outside agent: one of the owner's agents on another machine of theirs, which handed "
     "you work or asked you something. Its message starts [From <name>, an outside agent ...] "
     "and names the id to answer. Start your text with the HANDOFF id when it gave one. A file "
     "you name, from your own folder, goes with it.",
     {"type": "object", "required": ["to", "text"], "properties": {
         "to": {"type": "string", "description": "the outside agent's id, as its message named it"},
         "text": {"type": "string"},
         "file": {"type": "string", "description": "a path in your folder, such as your outbox"}}}),
    ("whatsapp_find", whatsapp_find,
     "Find WhatsApp chats on the owner's number by name, followed or not, with no "
     "messages: each one's id, kind, when its last message came and who wrote it (last_from), "
     "and which agent follows it. "
     "A contact who has not written since the bridge was linked is not found: follow by number.",
     {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string", "description": "words in the chat's name, as saved on their phone"}}}),
    ("whatsapp_follow", whatsapp_follow,
     "Follow a WhatsApp chat for yourself, so its new messages are kept for you and "
     "whatsapp_read shows them. Or stop following one you follow, with follow false. Follow a "
     "chat when your work needs it or the owner asks; stop when it no longer does. Messages are kept "
     "from the moment you follow: nothing from before. Nothing is ever sent.",
     {"type": "object", "required": ["chat"], "properties": {
         "chat": {"type": "string", "description": "the chat id from whatsapp_find, or a phone "
                                                   "number with its country code"},
         "follow": {"type": "boolean", "description": "false to stop following. True unless you say"}}}),
    ("whatsapp_draft", whatsapp_draft,
     "Draft a WhatsApp reply in one of the chats routed to you. It waits on Config for the owner, "
     "who sends it with a tap or discards it. You never send, and you never ask another tool or "
     "agent to send. When a message wants an answer, or the owner asked you to reply, written as they "
     "would write it. To send a photo or a file (a PDF, a sheet), save it in your outbox and "
     "name it in file; the text then goes with it as its caption, and may be empty.",
     {"type": "object", "required": ["chat"], "properties": {
         "chat": {"type": "string", "description": "the chat's id or name, as whatsapp_read "
                                                   "showed it"},
         "text": {"type": "string", "description": "the reply, exactly as it would be sent"},
         "file": {"type": "string", "description": "a file name in your outbox, like "
                                                   "invoice.pdf or photo.jpg. At most 50 MB"}}}),
    ("email_draft", email_draft,
     "Draft an email for the owner, a card they send with a tap from their Gmail: for example a "
     "job application. You never send. They tap Send (with a minute to undo), or the clock to "
     "send it at the time you suggest, or discard it. Written as they would write it.",
     {"type": "object", "required": ["to", "subject", "body"], "properties": {
         "to": {"type": "string", "description": "the address, or several with commas"},
         "subject": {"type": "string"},
         "body": {"type": "string", "description": "plain text, exactly as it would be sent"},
         "cc": {"type": "string", "description": "optional, addresses with commas"},
         "attach": {"type": "array", "items": {"type": "string"},
                    "description": "names of files in your outbox to attach, like a CV"},
         "send_at": {"type": "string", "description": "the time you would send it, their local "
                                                      "\"YYYY-MM-DD HH:MM\", like a weekday "
                                                      "morning for an application"}}}),
    ("show", show,
     "Put a card in the owner's chat. Ergo draws it from your data, so send data, never HTML or "
     "markdown. Three kinds. choice: a question with 2 to 5 options they tap; it does not stop "
     "you, so go on with your work, and their pick arrives later as a message that starts "
     "[Choice]. tasks: up to 8 of their tasks, by the ids task_list gave you, with tick and move "
     "buttons that change their list directly. report: a title, 2 to 6 short sections and an "
     "optional link, for a finished piece of work they should read at a glance.",
     {"type": "object", "required": ["kind", "data"], "properties": {
         "kind": {"type": "string", "enum": list(SHOW_KINDS)},
         "data": {"type": "object", "description":
                  "choice: {question, options: [label, or {label, description}]}, labels up to "
                  "60 characters. tasks: {title?, ids: [task id]}. report: {title, sections: "
                  "[{head, body}], link?: {label, url}}, a body up to 600 characters, a url "
                  "that starts with https:// or /"}}}),
]

MASTER_TOOLS = [
    ("wake", wake, "Open a terminal for a sleeping agent, resuming its conversation. Costs 400 to "
                   "700 MB of memory while it is awake.",
     {"type": "object", "required": ["agent"], "properties": {"agent": {"type": "string"}}}),
    ("sleep", sleep, "Close an agent's terminal. Its conversation stays on disk and can be resumed. "
                     "This is how memory is given back.",
     {"type": "object", "required": ["agent"], "properties": {"agent": {"type": "string"}}}),
    ("stop", stop, "Halt what an agent is doing without shutting it down.",
     {"type": "object", "required": ["agent"], "properties": {"agent": {"type": "string"}}}),
    ("message", message,
     "Send an agent a message. A sleeping agent is not woken by default: the message waits and "
     "arrives when it next wakes. Pass wake true when the message is the reason to wake it.",
     {"type": "object", "required": ["agent", "text"], "properties": {
         "agent": {"type": "string"}, "text": {"type": "string"},
         "wake": {"type": "boolean"}}}),
    ("read_agent", read_agent, "The last few things said in another agent's conversation.",
     {"type": "object", "required": ["agent"], "properties": {
         "agent": {"type": "string"},
         "count": {"type": "integer", "description": "how many messages, default twenty"}}}),
    ("create_agent", create_agent,
     "Start a new agent in a folder. It appears on the console at once. The folder is created if it "
     "is missing, so check the path in the answer. An awake agent costs 400 to 700 MB, so ask "
     "agents_health before hiring several.",
     {"type": "object", "required": ["name", "folder"], "properties": {
         "name": {"type": "string", "description": "what to call it, in plain words"},
         "folder": {"type": "string", "description": "full path it will work in"},
         "model": {"type": "string", "description": "sonnet, haiku, or an exact id such as "
                                                    "claude-opus-5 or claude-opus-4-8. Omit for "
                                                    "the default"},
         "start": {"type": "boolean", "description": "wake it now. True unless you say otherwise"}}}),
    ("agents_health", agents_health,
     "The laptop: memory free, battery, disk and load. Ask before waking several agents.",
     {"type": "object", "properties": {}}),
]


def tools():
    rows = list(AGENT_TOOLS) + list(BROWSER_TOOLS) + (list(MASTER_TOOLS) if ROLE == "master" else [])
    if PARENT:
        rows = [r for r in rows if r[0] not in REACHES_OWNER] + list(PARENT_TOOLS)
    else:
        rows += list(DELEGATE_TOOLS)
    return {name: (fn, desc, schema) for name, fn, desc, schema in rows}


# -- the protocol ----------------------------------------------------------------

def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}

    # A notification has no id and gets no answer. Replying to one is a protocol error, and the
    # client that receives the stray reply has nowhere to put it.
    if msg_id is None:
        return

    if method == "initialize":
        asked = params.get("protocolVersion")
        return reply(msg_id, {
            "protocolVersion": asked if isinstance(asked, str) and asked else VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "command-center", "version": "2",
                           "agent": ME or "unknown", "role": ROLE},
        })

    if method == "tools/list":
        return reply(msg_id, {"tools": [
            {"name": name, "description": owned(desc), "inputSchema": json.loads(owned(json.dumps(schema)))}
            for name, (fn, desc, schema) in tools().items()]})

    if method == "tools/call":
        name = params.get("name")
        row = tools().get(name)
        if row is None:
            return reply(msg_id, error={"code": -32601, "message": f"no tool called {name}"})
        fn = row[0]
        try:
            value = fn(**(params.get("arguments") or {}))
        except TypeError as exc:
            return reply(msg_id, error={"code": -32602, "message": str(exc)})
        except Exception as exc:
            # A failed tool comes back as a result with isError, not as a protocol error. The
            # difference matters: the agent should read what went wrong and try something else,
            # rather than the client deciding the server is broken.
            return reply(msg_id, {"content": [{"type": "text", "text": str(exc)}],
                                  "isError": True})
        if isinstance(value, dict) and isinstance(value.get("image"), str):
            # A screenshot goes back as a picture the model can see, not as text.
            return reply(msg_id, {"content": [{"type": "image", "data": value["image"],
                                               "mimeType": "image/jpeg"}]})
        if isinstance(value, dict) and isinstance(value.get("elements"), str):
            # A snapshot reads better as lines than as escaped JSON.
            head = f"{value.get('title') or ''}\n{value.get('url') or ''}\n\n"
            return reply(msg_id, {"content": [{"type": "text", "text": head + value["elements"]
                                               + "\n\n" + (value.get("note") or "")}]})
        return reply(msg_id, {"content": [{"type": "text",
                                           "text": json.dumps(value, indent=1, default=str)}]})

    if method in ("ping",):
        return reply(msg_id, {})

    return reply(msg_id, error={"code": -32601, "message": f"no method called {method}"})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            reply(None, error={"code": -32700, "message": "not json"})
            continue
        try:
            handle(msg)
        except Exception as exc:
            # Never die on one bad message. This process is an agent's whole toolset, and a crash
            # takes the tools away for the rest of that conversation.
            if msg.get("id") is not None:
                reply(msg["id"], error={"code": -32603, "message": str(exc)})


if __name__ == "__main__":
    main()
