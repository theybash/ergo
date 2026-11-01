"""How a screen gets its numbers.

The screen asks by name. The daemon works out whether that name is a file the agent wrote or a
route on the agent's own server, and the screen never has to know which. That is answer 38 of the
daemon grill, asked badly the first time and answered "whichever the agent chooses? sometimes
both?".

The file is the ordinary case, and that came out of the operator pushing back on servers being
wasteful. Most screens do not need a process at all:

    the agent writes  <cwd>/.platform/data/positions.json
    the daemon serves it
    the screen reads it

No port, nothing to crash, and it keeps working while the agent sleeps. A server is for data that
has to be live, and an agent may have both: the server answers when it is up, and the file is what
is left when it is not.

Every answer carries when it was last written, answer 14. A screen showing yesterday's positions as
though they were this morning's is worse than a screen showing nothing.
"""

import json
import re
import time
from pathlib import Path

from . import config, fetch, logs, servers

log = logs.get(__name__)

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
READABLE = (".json", ".txt", ".csv", ".md")

# A file bigger than this is not a screen's data, it is a dataset, and putting it through the
# browser's message channel would lock the tab.
MAX_FILE = 4 * 1024 * 1024


def folder(rec):
    return Path(rec["cwd"]) / ".platform" / "data"


def listing(rec):
    """What this agent has published as files. Names, sizes and ages."""
    out = []
    try:
        entries = sorted(folder(rec).iterdir())
    except (OSError, FileNotFoundError):
        return out
    for path in entries:
        if not path.is_file() or path.suffix.lower() not in READABLE:
            continue
        if not NAME.match(path.name):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"name": path.stem, "file": path.name, "bytes": stat.st_size,
                    "updated_at": stat.st_mtime, "age": round(time.time() - stat.st_mtime, 1)})
    return out


def _from_file(rec, name):
    """Read a published file, matched against the listing rather than by joining paths.

    The name arrives from a browser, and a path built by joining a name onto a folder is a path
    the caller chooses. Matching against what is actually there means the worst a bad name can do
    is fail to match.
    """
    for entry in listing(rec):
        if entry["name"] != name:
            continue
        path = folder(rec) / entry["file"]
        if entry["bytes"] > MAX_FILE:
            raise ValueError(f"{entry['file']} is {entry['bytes']} bytes, too big for a screen")
        text = path.read_text(errors="replace")
        out = {"name": name, "source": "file", "file": entry["file"],
               "updated_at": entry["updated_at"], "age": entry["age"], "text": text}
        if path.suffix.lower() == ".json":
            try:
                out["value"] = json.loads(text)
                out["text"] = None
            except Exception as exc:
                # Half-written JSON is the normal way this fails: the agent was writing the file as
                # the screen read it. Saying so beats a screen that draws nothing.
                out["error"] = f"{entry['file']} is not valid json yet: {exc}"
        return out
    return None


def _from_server(rec, name):
    """Ask the agent's own server. Returns None if there is no server or it will not answer."""
    if not servers.running(rec["id"]):
        return None
    port = servers.state(rec["id"]).get("port")
    if not port:
        return None
    try:
        got = fetch.get(f"http://127.0.0.1:{port}/data/{name}", timeout=config.SERVER_TIMEOUT,
                        who=f"{rec['id']}/{name}")
    except ValueError as exc:
        log.info("%s server did not answer for %s: %s", rec["id"], name, exc)
        return None
    if got["status"] >= 400:
        return None
    value = got.get("json")
    out = {"name": name, "source": "server", "port": port,
           "updated_at": time.time(), "age": 0.0}
    if value is None:
        out["text"] = got.get("text")
    else:
        out["value"] = value
        # A server that says when its numbers are from is believed, because it knows and the clock
        # does not: data fetched now can still be an hour old upstream.
        if isinstance(value, dict) and isinstance(value.get("updated_at"), (int, float)):
            out["updated_at"] = float(value["updated_at"])
            out["age"] = round(time.time() - out["updated_at"], 1)
    return out


def resolve(rec, name):
    """One name in, one answer out. The server first, then the file.

    The server wins when it is up because it is the live one, and the file is what is left when the
    server is down or was never written. Which one answered is in the reply, so a screen can say
    "from the file, four hours old" rather than implying it is current.
    """
    if not NAME.match(name or ""):
        raise ValueError("a data name is lowercase letters, digits, dot, dash or underscore")
    got = _from_server(rec, name)
    if got:
        return got
    got = _from_file(rec, name)
    if got:
        return got
    raise KeyError(f"{rec['id']} has published nothing called {name!r}")


def available(rec):
    """What a screen can ask for: the files, plus whether a live server is there to ask."""
    return {"files": listing(rec), "server": servers.state(rec["id"])}
