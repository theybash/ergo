"""The CLAUDE.md files, editable from Config, so the owner can change and trim the rules.

Every agent reads three layers: the global file every Claude reads, its house's file, and the file
in its own folder. All of them are listed here, read from disk each time, so an edit made anywhere
else shows at once.

    save       writes the file. The text it replaced is kept as the previous version, so Put back
               the previous swaps the two, the same as the front desk's prompt
    the log    every save is one line in var/instructions/edits.jsonl: when, which file, the text
               before and after. The master agent reads it to see what the owner changed, and why a rule went

An agent reads its CLAUDE.md when its session starts, so an edit reaches a running agent at its
next start, or its next fresh run.
"""

import hashlib
import json
import re
import threading
from pathlib import Path

from . import clock, config, logs

log = logs.get(__name__)

LIMIT = 100_000
_lock = threading.Lock()

HOME = Path.home()


def _houses():
    """The house folder, from ERGO_HOME. One house, so one entry."""
    return (("Ergo", Path(config.HOME)),)


def _dir():
    d = config.VAR / "instructions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(path):
    """A short name for the address: the path from the home folder, in plain letters."""
    try:
        rel = str(Path(path).relative_to(HOME))
    except ValueError:
        rel = str(path)
    return re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")


def files():
    """Every CLAUDE.md, in the order an agent reads them: global, the houses, the agents."""
    out = [("Global, every Claude", HOME / ".claude" / "CLAUDE.md")]
    out += [(f"{side} house", root / "CLAUDE.md") for side, root in _houses()]
    for side, root in _houses():
        out += [(f"{side}: {f.parent.name}", f) for f in sorted(root.glob("*/CLAUDE.md"))]
    return [{"key": _key(p), "name": n, "path": str(p)} for n, p in out if p.is_file()]


def listing():
    rows = []
    for f in files():
        text = Path(f["path"]).read_text(encoding="utf-8")
        rows.append(dict(f, words=len(text.split())))
    return {"files": rows}


def _find(key):
    for f in files():
        if f["key"] == key:
            return f
    raise ValueError("There is no such CLAUDE.md.")


def _previous_file(key):
    return _dir() / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".previous.md")


def doc(key):
    f = _find(key)
    return dict(f, text=Path(f["path"]).read_text(encoding="utf-8"),
                previous=_previous_file(key).is_file(), limit=LIMIT)


def save(key, text):
    text = str(text or "")
    if not text.strip():
        raise ValueError("An empty file would wipe the agent's rules. Nothing was saved.")
    if len(text) > LIMIT:
        raise ValueError(f"That is over {LIMIT} characters.")
    f = _find(key)
    path = Path(f["path"])
    with _lock:
        before = path.read_text(encoding="utf-8")
        if before == text:
            return doc(key)
        _previous_file(key).write_text(before, encoding="utf-8")
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        with open(_dir() / "edits.jsonl", "a", encoding="utf-8") as out:
            out.write(json.dumps({"at": clock.now(), "key": key, "path": str(path),
                                  "before": before, "after": text}) + "\n")
    log.info("instructions: %s saved, %d to %d words", key, len(before.split()), len(text.split()))
    return doc(key)


def restore(key):
    """Put back the previous: the file and its previous version swap places."""
    prev = _previous_file(key)
    if not prev.is_file():
        raise ValueError("There is no previous version of this file.")
    return save(key, prev.read_text(encoding="utf-8"))
