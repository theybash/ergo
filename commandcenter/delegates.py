"""Delegates: agents that an agent makes for one topic, and that answer only to it.

One agent can end up carrying four topics, and the owner loses track of which is where. More
agents on the screen would only move the clutter. So an agent can make a delegate for a
topic, send it work, and hear back, and the delegate stays folded under its parent's card.

The rules, all his:

  - A delegate cannot make delegates of its own.
  - An agent has at most nine active delegates. It archives one to make room, and revives it
    later if there is room again. An archived delegate keeps its folder and its chat.
  - A delegate reaches the owner only through its parent: no calls, alarms, feed posts, tasks or
    drafts of its own. The daemon refuses them (see `refuse`), so this does not rest on the
    delegate reading its instructions.
  - Hiding a parent hides its delegates too.

A delegate lives in a folder under its parent's, and it gets a copy of the parent's .claude/settings.json, so it has the same credentials and the same
push guard. It can never have more than its parent has.
"""

import json
import os

from . import broker, registry

LIMIT = 9

CLAUDE_MD = """# {name}

You are {name}, a delegate of {parent}. {parent} made you for one topic:

{purpose}

## How you work

- You answer only to {parent}. Work comes to you as messages from it.
- When you finish something, or need a decision, tell {parent} with `report_to_parent`. Say what
  you did or what you need, in a few plain sentences.
- You never reach the owner yourself: no calls, alarms, feed posts, tasks or drafts. The daemon
  refuses them. Anything for the owner goes to {parent}, and {parent} decides what reaches him.
- The CLAUDE.md files in the folders above this one are {parent}'s and its house's. Their rules
  hold for you too, but you are not {parent}.
- Keep the state of your topic in `STATE.md` in this folder: where the work stands, the next
  step, open questions. Update it before each turn ends, so nothing is lost when your chat is
  compacted.
{handoff}"""


def _active(parent_id):
    return [d for d in registry.delegates(parent_id) if not d["archived"] and not d["hidden"]]


def _mine(parent_id, delegate_id):
    rec = registry.get(delegate_id)
    if not rec or rec.get("parent") != parent_id:
        raise KeyError(f"{delegate_id} is not a delegate of {parent_id}")
    return rec


def listing(parent_id):
    out = []
    for d in registry.delegates(parent_id):
        out.append({"id": d["id"], "name": d["name"], "archived": d["archived"],
                    "hidden": d["hidden"], "state": broker.state(d["id"]),
                    "folder": d["cwd"], "note": d.get("note")})
    return {"delegates": out, "active": len(_active(parent_id)), "limit": LIMIT}


def _settings(parent_cwd, folder):
    """The parent's settings, with this folder's own settings file added to the deny list."""
    src = os.path.join(parent_cwd, ".claude", "settings.json")
    try:
        with open(src) as fh:
            body = json.load(fh)
    except (OSError, ValueError):
        return False
    own = os.path.join(folder, ".claude", "settings.json")
    deny = body.setdefault("permissions", {}).setdefault("deny", [])
    for rule in (f"Edit({own})", f"Write({own})"):
        if rule not in deny:
            deny.append(rule)
    os.makedirs(os.path.dirname(own), exist_ok=True)
    with open(own, "w") as fh:
        json.dump(body, fh, indent=2)
        fh.write("\n")
    return True


def make(parent_id, name, purpose, handoff=None, model=None):
    parent = registry.get(parent_id)
    if not parent:
        raise KeyError(f"no agent called {parent_id}")
    if parent.get("parent"):
        raise PermissionError("a delegate cannot make delegates of its own. Ask "
                              f"{parent['parent']} to make one.")
    name, purpose = (name or "").strip(), (purpose or "").strip()
    if not name or not purpose:
        raise ValueError("give the delegate a name and a purpose")
    if len(_active(parent_id)) >= LIMIT:
        raise OverflowError(f"you have {LIMIT} active delegates, the most allowed. Archive one "
                            "with archive_delegate to make room.")
    want = registry.slug(f"{parent_id}-{name}")[:32].strip("-")
    folder = os.path.join(parent["cwd"], "delegates", registry.slug(name))
    if os.path.exists(os.path.join(folder, "CLAUDE.md")):
        raise FileExistsError(f"{folder} already holds a delegate. Revive it, or pick another "
                              "name.")
    os.makedirs(folder, exist_ok=True)
    copied = _settings(parent["cwd"], folder)
    rec = registry.create(name=name, cwd=folder, model=model or parent.get("model"),
                          permission_mode=parent.get("permission_mode"), agent_id=want,
                          engine=parent.get("engine"), parent=parent_id)
    with open(os.path.join(folder, "CLAUDE.md"), "w") as fh:
        fh.write(CLAUDE_MD.format(
            name=rec["name"], parent=parent_id, purpose=purpose,
            handoff=f"\n## What {parent_id} handed over\n\n{handoff.strip()}\n"
                    if (handoff or "").strip() else ""))
    return {"id": rec["id"], "name": rec["name"], "folder": folder, "settings_copied": copied}


def tell(parent_id, delegate_id, text):
    rec = _mine(parent_id, delegate_id)
    if rec["archived"]:
        raise PermissionError(f"{delegate_id} is archived. Revive it first.")
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to send")
    return broker.send(delegate_id, f"[From {parent_id}, your parent]\n{text}", wake=True)


def report(delegate_id, text):
    rec = registry.get(delegate_id)
    if not rec or not rec.get("parent"):
        raise PermissionError("only a delegate reports to a parent")
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to report")
    return broker.send(rec["parent"], f"[Report from {delegate_id}, your delegate]\n{text}",
                       wake=True)


def archive(parent_id, delegate_id):
    _mine(parent_id, delegate_id)
    broker.sleep(delegate_id, force=True)
    registry.update(delegate_id, archived=True)
    return listing(parent_id)


def revive(parent_id, delegate_id):
    rec = _mine(parent_id, delegate_id)
    if rec["archived"] and len(_active(parent_id)) >= LIMIT:
        raise OverflowError(f"you have {LIMIT} active delegates. Archive one first.")
    registry.update(delegate_id, archived=False, hidden=False)
    return listing(parent_id)


def set_hidden(parent_id, hidden):
    """A parent hidden or shown takes its delegates with it."""
    for d in registry.delegates(parent_id):
        registry.update(d["id"], hidden=hidden)


# What reaches the owner, and where the caller's id is in each request. A delegate is refused all of
# these: its parent decides what reaches him.
_REACH = {"/api/phone/alarm": "by", "/api/posts": "agent", "/api/tasks": "agent",
          "/api/tasks/mine": "agent", "/api/tasks/owed": "agent", "/api/mail/drafts": "agent",
          "/api/whatsapp/drafts": "agent", "/api/quiet": "by",
          "/api/phone/float": "agent"}


def refuse(path, body, call_of=None):
    """The sentence to refuse with when a delegate tries to reach the owner, or None."""
    who = call_of or (body.get(_REACH[path]) if path in _REACH and isinstance(body, dict) else None)
    rec = registry.get(who) if who else None
    if not rec or not rec.get("parent"):
        return None
    return (f"You are a delegate of {rec['parent']}, so you do not reach the owner yourself. Send "
            f"this to {rec['parent']} with report_to_parent, and it decides.")
