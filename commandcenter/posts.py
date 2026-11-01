"""The feed: what the agents post for the owner to see, drawn like a timeline (The owner, 21 October
2026: "make like a standardized feed feature which all agents can use for different things").

A job's result used to be one quiet folded card in the agent's chat, the same card as every daemon
message, and his eye skipped it. A post is drawn by its parts, so a brief, a pick and a draft each
look like what they are:

    text     what the agent wants him to know, in a sentence or three. Always there.
    kind     a short label after the agent's name: "HN brief", "job feed", "done"
    link     a quoted card for the source: {title, url, meta, file}. file is a .md the agent
             wrote in its own outbox, which the app opens as a page to read (The owner, 21 October
             2026: "Simple markdown files So agent can create my feeds")
    draft    words he may send himself: {to, text, url}. The app gives it a Copy button
    card     one big headline: {headline, sub, url, label}, for a pick or a result
    media    up to four images or videos: [{url or file, kind, alt}]. A url is on the web; a file
             is a name in the agent's outbox, served by the daemon's own file route
    job, transcript   set by the daemon on a post from a job's fresh run: the job's id and the
             path of the run's transcript

A link gets the page's own picture (its og:image) when the page has one: fetched once, in the
background, after the post lands, so a slow site never holds a post up.

Every part but text is optional, and a part that is not well formed is dropped rather than refused,
so an agent's post always lands. Nothing here sends anything anywhere: a draft is copied by him.

A post is something an agent shows him, not a task: he hides one he is done looking at, with the
x on it, and Undo takes the mark back. Done and skipped are the marks of the first version, still
read. The file is one JSON line per event, folded on read,
and the newest KEEP posts are kept.
"""

import json
import re
import threading
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import clock, config, logs

log = logs.get(__name__)

KEEP = 500
TEXT = 1200
SHORT = 200
MARKS = ("", "hidden", "done", "skipped")
MEDIA = 4
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")
PAGE_BYTES = 600_000
PAGE_SECONDS = 6

_lock = threading.Lock()
_state = None
_listeners = []


def _file():
    return Path(config.VAR) / "posts.jsonl"


def on_change(fn):
    _listeners.append(fn)


def _emit(post):
    for fn in list(_listeners):
        try:
            fn("post", {"post": post})
        except Exception:
            pass


def _clip(value, n):
    return " ".join(str(value or "").split())[:n]


def _url(value):
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) and len(url) <= 2000 else ""


def _part(value, fields):
    """One optional part, its fields cut to size, or None when nothing useful is left."""
    if not isinstance(value, dict):
        return None
    out = {}
    for name, n in fields.items():
        got = _url(value.get(name)) if name == "url" else _clip(value.get(name), n)
        if got:
            out[name] = got
    return out or None


def _link(agent, value):
    """The link part. Its file is kept only when it names a markdown file that is really in this
    agent's own outbox, so a post can never open another agent's file or one that is not there."""
    got = _part(value, {"title": SHORT, "url": 0, "meta": SHORT})
    name = str(value.get("file") or "").strip() if isinstance(value, dict) else ""
    if name and "/" not in name and "\\" not in name and name.lower().endswith(".md"):
        from . import files, registry
        if (files.outbox(registry.get(agent)) / name).is_file():
            got = dict(got or {}, file=name)
    return got


def _media(value):
    """Up to MEDIA images or videos. Each needs a web url or an outbox file name, and a kind, which
    the name's ending gives when the agent left it out."""
    if isinstance(value, dict):
        value = [value]
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        url = _url(item.get("url"))
        name = str(item.get("file") or "").strip()
        name = name if name and "/" not in name and "\\" not in name and len(name) <= 200 else ""
        where = (url or name).lower().split("?")[0]
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in ("image", "video"):
            kind = "video" if where.endswith(VIDEO_EXT) else "image" if where.endswith(IMAGE_EXT) else ""
        if not kind or not (url or name):
            continue
        got = {"kind": kind, "url": url} if url else {"kind": kind, "file": name}
        alt = _clip(item.get("alt"), SHORT)
        if alt:
            got["alt"] = alt
        out.append(got)
        if len(out) == MEDIA:
            break
    return out


def _fetch_page(url):
    """The start of a page, as text. Tests stand in for it."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Ergo feed preview)",
                                               "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=PAGE_SECONDS) as resp:
        if "html" not in (resp.headers.get("Content-Type") or ""):
            return ""
        return resp.read(PAGE_BYTES).decode("utf-8", "replace")


_OG = re.compile(r"""<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)(?::src)?["'][^>]*>""", re.I)
_CONTENT = re.compile(r"""content=["']([^"']+)["']""", re.I)


def _picture(url):
    """The page's own picture, or an empty string."""
    try:
        page = _fetch_page(url)
    except Exception:
        return ""
    tag = _OG.search(page or "")
    got = _CONTENT.search(tag.group(0)) if tag else None
    if not got:
        return ""
    return _url(urllib.parse.urljoin(url, got.group(1).replace("&amp;", "&")))


def _preview(post_id, url):
    image = _picture(url)
    if not image:
        return
    with _lock:
        if post_id not in _held()["posts"]:
            return
        _write({"event": "preview", "id": post_id, "image": image, "at": clock.now()})
        got = dict(_held()["posts"][post_id])
    _emit(got)


def _spawn(fn, *args):
    """A background thread. Tests run it inline."""
    threading.Thread(target=fn, args=args, name="post-preview", daemon=True).start()


def _apply(posts, ev):
    if ev.get("event") == "post" and ev.get("id"):
        posts[ev["id"]] = {k: v for k, v in ev.items() if k != "event"}
        posts[ev["id"]].setdefault("state", "")
    elif ev.get("event") == "mark" and ev.get("id") in posts:
        posts[ev["id"]]["state"] = ev.get("state") or ""
    elif ev.get("event") == "preview" and ev.get("id") in posts and posts[ev["id"]].get("link"):
        posts[ev["id"]]["link"] = dict(posts[ev["id"]]["link"], image=ev.get("image") or "")


def _held():
    global _state
    where = str(_file())
    if _state is None or _state["file"] != where:
        posts = {}
        try:
            lines = _file().read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                _apply(posts, json.loads(line))
            except ValueError:
                continue
        _state = {"file": where, "posts": posts}
    return _state


def _write(ev):
    st = _held()
    _file().parent.mkdir(parents=True, exist_ok=True)
    with open(_file(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")
    _apply(st["posts"], ev)
    if len(st["posts"]) > KEEP:
        for old in sorted(st["posts"].values(), key=lambda p: p["at"])[:-KEEP]:
            st["posts"].pop(old["id"], None)


def post(agent, text, kind=None, link=None, draft=None, card=None, media=None, job=None,
         transcript=None):
    """One post from an agent. Returns it. A post from a job's fresh run carries the job's id and
    the path of the run's transcript, so a reply about the post can find what made it."""
    from . import registry
    agent = str(agent or "").strip()
    if not registry.get(agent):
        raise ValueError("say which agent is posting")
    text = str(text or "").strip()[:TEXT]
    if not text:
        raise ValueError("a post needs text: what he should know, in a sentence or three")
    ev = {"event": "post", "id": uuid.uuid4().hex[:10], "at": clock.now(), "agent": agent,
          "text": text, "kind": _clip(kind, 40)}
    for name, value in (("link", _link(agent, link)),
                        ("draft", _part(draft, {"to": SHORT, "text": TEXT, "url": 0})),
                        ("card", _part(card, {"headline": SHORT, "sub": SHORT * 2, "url": 0,
                                              "label": 40}))):
        if value:
            ev[name] = value
    if "draft" in ev and not ev["draft"].get("text"):
        del ev["draft"]
    shown = _media(media)
    if shown:
        ev["media"] = shown
    if job and re.match(r"^[A-Za-z0-9_-]{1,40}$", str(job)):
        ev["job"] = str(job)
    if transcript and str(transcript).startswith(str(config.VAR)) and str(transcript).endswith(".jsonl"):
        ev["transcript"] = str(transcript)
    with _lock:
        _write(ev)
        got = dict(_held()["posts"][ev["id"]])
    log.info("posts: %s posted %s", agent, ev["id"])
    _emit(got)
    if got.get("link", {}).get("url"):
        _spawn(_preview, got["id"], got["link"]["url"])
    return got


def mark(post_id, state):
    """He hid a post, or took the mark back."""
    state = str(state or "").strip()
    if state not in MARKS:
        raise ValueError("a mark is hidden, or empty")
    with _lock:
        if post_id not in _held()["posts"]:
            raise KeyError(post_id)
        _write({"event": "mark", "id": post_id, "state": state, "at": clock.now()})
        got = dict(_held()["posts"][post_id])
    _emit(got)
    return got


def listing(count=100, days=None, before=None):
    """The newest posts first. With days, only the posts of that many days (The owner, 29 October
    2026: "don't load the entire feed at once... you can load three days"). With before, the days
    end there instead of now: the feed loads older days that way as he scrolls down. `next` is the
    before for the page after this one, and `more` says whether there is one."""
    with _lock:
        # Newest first, and in the same second the later post first: the sort keeps the order of
        # equal times, so it is given the posts newest first to begin with.
        posts = sorted(list(_held()["posts"].values())[::-1], key=lambda p: p["at"], reverse=True)
    if before:
        posts = [p for p in posts if p["at"] < float(before)]
    count = max(1, min(int(count or 100), KEEP))
    if days:
        since = float(before or clock.now()) - float(days) * 86400
        if posts and before and posts[0]["at"] < since:
            # Nothing in those days: the days start at the next older post instead, so a quiet
            # week never shows as an empty page.
            since = posts[0]["at"] - float(days) * 86400
        page = [p for p in posts if p["at"] >= since]
        more = len(page) > count or len(page) < len(posts)
        page = page[:count]
        nxt = page[-1]["at"] if len(page) == count else since
    else:
        page = posts[:count]
        more = len(posts) > count
        nxt = page[-1]["at"] if page else None
    return {"posts": [dict(p) for p in page], "more": more, "next": nxt}


def reset():
    """For tests."""
    global _state
    with _lock:
        _state = None
