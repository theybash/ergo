"""An agent's open list: what was talked about and never done, in OPEN.md in its own folder.

The owner, 22 October 2026: "sometimes there are things we talked about but never did. That should
be there somewhere easy to pick up". The agent keeps the file; the daemon only reads it. Each list
line is one item, with the heading above it and its line number, so a tap in the Open tab can
quote it in the chat and the agent finds it again.
"""

import re
from pathlib import Path

NAME = "OPEN.md"
MAX_BYTES = 200_000

ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*$")


def parse(text):
    """The list lines of a markdown file. A line indented under an item continues it, until a
    blank line or a heading ends it."""
    items, section, going = [], "", False
    for n, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = raw.rstrip()
        head = HEADING.match(line)
        item = ITEM.match(line)
        if head:
            section, going = head.group(1), False
        elif item:
            items.append({"n": n, "section": section, "text": item.group(1).strip()})
            going = True
        elif going and line.strip() and raw[:1] in (" ", "\t"):
            items[-1]["text"] += " " + line.strip()
        else:
            going = False
    return items


def listing(rec):
    """GET /api/agents/<id>/open."""
    path = Path(rec.get("cwd") or "") / NAME
    try:
        with open(path, "rb") as fh:
            text = fh.read(MAX_BYTES).decode("utf-8", "replace")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return {"exists": False, "path": str(path), "items": []}
    return {"exists": True, "path": str(path), "items": parse(text)}
