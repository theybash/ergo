"""Small talk for the wait: something to say while an agent works.

The owner sends an agent a task on a call and the agent comes back a minute or two later. He likes
his front desk a bit chatty, so in that gap the front desk says "by the way, did you hear" about a story
from Hacker News, science, tech or biotech, and now and then tells a joke instead.

    the pack     var/voice/smalltalk.json: the latest headlines, fetched at most once an hour
    the pick     one item per wait, a story five times in six and a joke once in six, and
                 never an item told in the last week
    the words    written here, as one instruction the browser hands the live voice

The fetch runs in a thread and the pick never waits on it: an old pack is used while a new one
arrives, and an empty pack falls back to a joke. The sources are public feeds, so a call in the
demo or work view gets them too. Nothing here reads anything of his.
"""

import json
import logging
import random
import threading
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from . import clock, config

log = logging.getLogger("cc.smalltalk")

HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_COUNT = 8

# kind, source as said aloud, feed address.
FEEDS = (
    ("science", "ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml"),
    ("science", "Quanta", "https://www.quantamagazine.org/feed/"),
    ("tech", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("biotech", "STAT", "https://www.statnews.com/feed/"),
)
PER_FEED = 4
PAYWALLED = ("STAT+",)

FRESH = 3600          # a pack younger than this is not fetched again
TOLD_FOR = 7 * 86400  # an item told is not told again for a week
JOKE_ODDS = 6         # one pick in this many is a joke
TIMEOUT = 6
AGENT = "commandcenter-smalltalk/1"

# His kind of joke: dry, short, about the work. Said in the front desk's own words.
JOKES = (
    "There are two hard problems in computer science: cache invalidation, naming things, and "
    "off by one errors.",
    "A programmer's partner says: buy a loaf of bread, and if they have eggs, get a dozen. He "
    "comes home with twelve loaves.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "A neutron walks into a bar and asks for the price. The barman says: for you, no charge.",
    "It works on my machine. Then we'll ship your machine.",
    "An SQL query walks into a bar, goes up to two tables and asks: may I join you?",
    "Never trust an atom. They make up everything.",
    "The best thing about a Boolean is that even if you are wrong, you are only off by a bit.",
    "A biologist, a chemist and a statistician go hunting. The statistician says they hit the "
    "deer on average.",
    "Why did the developer go broke? He used up all his cache.",
    "Schrodinger's cat walks into a bar. And doesn't.",
    "Eight bytes walk into a bar. The barman asks: can I get you anything? They say: make us a "
    "double.",
)

_lock = threading.Lock()
_fetching = threading.Event()


def _file():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "smalltalk.json"


def _load():
    try:
        got = json.loads(_file().read_text())
        if isinstance(got, dict):
            got.setdefault("items", [])
            got.setdefault("told", {})
            got.setdefault("at", 0)
            return got
    except (OSError, ValueError):
        pass
    return {"at": 0, "items": [], "told": {}}


def _save(pack):
    tmp = _file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pack))
    tmp.replace(_file())


def get(url):
    """One public address, as bytes. Its own function so a test can stand in for the network."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _hn():
    ids = json.loads(get(HN_TOP))[:HN_COUNT]
    out = []
    for raw in ids:
        try:
            item = json.loads(get(HN_ITEM.format(int(raw))))
        except (OSError, ValueError, TypeError):
            continue
        title = (item or {}).get("title")
        if title:
            out.append({"kind": "hn", "source": "Hacker News", "title": title.strip()})
    return out


def _feed(kind, source, url):
    """The first few titles of an RSS or Atom feed. Tags are matched by their last part, so a
    namespace on either format is no trouble."""
    root = ET.fromstring(get(url))
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        for child in el:
            if child.tag.rsplit("}", 1)[-1] == "title" and (child.text or "").strip():
                # A paywalled story is a headline he cannot read, so it is no use to mention.
                if child.text.strip().startswith(PAYWALLED):
                    break
                out.append({"kind": kind, "source": source, "title": " ".join(child.text.split())})
                break
        if len(out) >= PER_FEED:
            break
    return out


def fetch():
    """Every source at once, each on its own. A source that fails is left out and said once in
    the log; the pack keeps whatever the others brought."""
    jobs = [("hn", _hn, ())] + [(src, _feed, (kind, src, url)) for kind, src, url in FEEDS]
    items = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        running = [(name, pool.submit(fn, *args)) for name, fn, args in jobs]
        for name, fut in running:
            try:
                items.extend(fut.result(timeout=TIMEOUT * 3))
            except Exception as exc:  # a feed is someone else's server; anything can come back
                log.info("small talk: %s gave nothing, %s", name, type(exc).__name__)
    return items


def refresh(force=False):
    """Fetch a new pack if the one held is older than an hour. Runs where it is called."""
    with _lock:
        pack = _load()
    if not force and clock.now() - pack["at"] < FRESH:
        return pack
    items = fetch()
    with _lock:
        pack = _load()
        if items:
            pack["items"] = items
        pack["at"] = clock.now()
        _save(pack)
    log.info("small talk: %d items in the pack", len(pack["items"]))
    return pack


def refresh_soon():
    """Start a refresh in the background, unless one is already running. Called as a call opens,
    so the pack is fresh by the time the first task goes out."""
    if not config.SMALLTALK or _fetching.is_set():
        return
    _fetching.set()

    def run():
        try:
            refresh()
        except Exception:
            log.exception("small talk: the refresh failed")
        finally:
            _fetching.clear()

    threading.Thread(target=run, name="smalltalk", daemon=True).start()


def _key(item):
    return item["kind"] + ":" + item["title"][:120]


def pick(rng=random):
    """One item for this wait, marked told. A joke once in six, or when no story is left."""
    with _lock:
        pack = _load()
        now = clock.now()
        told = {k: at for k, at in pack["told"].items() if now - at < TOLD_FOR}
        stories = [it for it in pack["items"] if _key(it) not in told]
        jokes = [{"kind": "joke", "source": "", "title": j} for j in JOKES
                 if ("joke:" + j[:120]) not in told]
        if stories and (not jokes or rng.randrange(JOKE_ODDS)):
            item = rng.choice(stories)
        elif jokes:
            item = rng.choice(jokes)
        else:
            item = None
        if item:
            told[_key(item)] = now
        pack["told"] = told
        _save(pack)
    return item


def line():
    """POST /api/voice/smalltalk: the instruction for one small talk turn, or nothing."""
    item = pick()
    if not item:
        return {"say": "", "kind": None}
    if item["kind"] == "joke":
        say = ("While you both wait for the agent, lighten the wait with this joke, in your own "
               f"words and your own timing: {item['title']} One joke, then stop.")
    else:
        say = ("While you both wait for the agent, make small talk the way a colleague would: "
               "open with something like 'by the way, did you hear' and tell him about this, "
               f"from {item['source']}, in one or two sentences of your own: {item['title']} "
               "Add a dry thought of your own if one comes. Then stop and let him answer. If he "
               "asks for more, say you only saw the headline.")
    return {"say": say, "kind": item["kind"]}
