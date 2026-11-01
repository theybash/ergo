"""The voice's memory: the call log, read back.

The memory is var/voice/calls.jsonl, every turn of every call, one JSON row each. Nothing here
trims it, rewrites it or summarises it. Two things read it:

    the window     the last N turns go into the briefing at the start of every call
    recall         a tool the voice calls when he asks about something older than the window

Recall is two steps. First a search by meaning: every turn is embedded once with OpenAI's small
embedding model, and the question is compared with all of them by cosine similarity. The K
closest turns are the hits, and each hit brings the turn before it and the turn after it, so a
hit carries its context. Then one request to Jev, TypeSafe's judgment model, which says yes or no
for every candidate: is this turn relevant and worth saying now. The search finds what is near;
Jev throws out what is near and useless. Code keeps the yeses, in the order they were said, up to
a budget of characters.

Without the TypeSafe key, or when Jev fails, the K closest go back unjudged and the answer says
so. When the embedding call fails there is no search at all, and the answer says that plainly.
The tool never raises.

N is a setting. The environment gives the default, the Config page can change it, and the
change is kept in var/voice/settings.json so it survives a restart.

Nothing here logs a key, a turn, a vector, or anything that was searched for.
"""

import array
import base64
import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.request

from . import clock, config, logs

log = logs.get(__name__)

_lock = threading.RLock()

# -- the settings ----------------------------------------------------------------
# Three numbers the Config page can change. The environment gives each default; a saved number,
# in var/voice/settings.json, wins over it and survives a restart.
#
#   memory_turns     turns of the call log in every call's briefing          1 to 100
#   recall_span      turns before and after each hit recall hands back       0 to 10
#   silence_minutes  minutes with nobody speaking before a call hangs up     1 to 30
#
# The third is not memory. It lives here because it is a voice setting the Config page changes,
# and one file for all of them is simpler than two.

LIMITS = {"memory_turns": (1, 100), "recall_span": (0, 10), "silence_minutes": (1, 30)}
DEFAULTS = {"memory_turns": 30, "recall_span": 2, "silence_minutes": 8}
ENV = {"memory_turns": "VOICE_MEMORY_TURNS", "recall_span": "VOICE_RECALL_SPAN",
       "silence_minutes": "VOICE_SILENCE_MINUTES"}

# Three times of day for the inbox, in the same file: when the morning ping may go, and the quiet
# hours in which a "now" item pings nobody. HH:MM, local.
TIMES = {"morning_brief": "07:15", "quiet_start": "23:30", "quiet_end": "07:00"}


def _settings_file():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "settings.json"


def _clamp(name, value):
    """A whole number inside the setting's range. Raises ValueError when it is not a number."""
    lo, hi = LIMITS[name]
    try:
        want = int(float(value))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a number from {lo} to {hi}") from None
    return max(lo, min(want, hi))


def _default(name):
    raw = getattr(config, ENV[name], DEFAULTS[name])
    try:
        return _clamp(name, raw)
    except ValueError:
        return DEFAULTS[name]


def _saved():
    try:
        held = json.loads(_settings_file().read_text())
        return held if isinstance(held, dict) else {}
    except (OSError, ValueError):
        return {}


def _setting(name):
    held = _saved()
    if name in held:
        try:
            return _clamp(name, held[name])
        except ValueError:
            pass
    return _default(name)


def _time(name, value):
    """A time of day as HH:MM. Raises ValueError when it is not one."""
    text = str(value if value is not None else "").strip()
    parts = text.split(":")
    try:
        if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) != 2:
            raise ValueError
        hours, minutes = int(parts[0]), int(parts[1])
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            raise ValueError
    except ValueError:
        raise ValueError(f"{name} must be a time of day like {TIMES[name]}") from None
    return f"{hours:02d}:{minutes:02d}"


def time_setting(name):
    """One of the inbox times, saved or default."""
    held = _saved()
    if name in held:
        try:
            return _time(name, held[name])
        except ValueError:
            pass
    return TIMES[name]


# Who sounds like what, in the same file. "desk" is the front desk; every other key is an agent
# id. A key that is not saved takes the default, so an agent nobody has set sounds like
# config.AGENT_VOICE and the front desk like config.VOICE_NAME, as before this setting existed.
DESK = "desk"

# Whether the phone opens an agent's call while it is still ringing, so it is up the moment he
# answers. On by default. Phase 1 keeps the switch and tells the phone; the phone acts on it later.
PREWARM_DEFAULT = True


def voice_choices(who):
    """The voices `who` may have: all of them for the front desk, the recordable ones for an agent."""
    return config.LIVE_VOICES if who == DESK else config.SPEECH_VOICES


def _default_voice(who):
    return config.VOICE_NAME if who == DESK else config.AGENT_VOICE


def voice_for(who=None):
    """The voice one call uses. `who` is an agent id, or nothing for the front desk."""
    who = who or DESK
    held = _saved().get("voices")
    picked = held.get(who) if isinstance(held, dict) else None
    if isinstance(picked, str) and picked in voice_choices(who):
        return picked
    return _default_voice(who)


def prewarm():
    """The "Open the call before I answer" switch."""
    held = _saved().get("prewarm")
    return held if isinstance(held, bool) else PREWARM_DEFAULT


def _voices(fields):
    """The voices `fields` asks for, checked. An empty name puts that one back to its default."""
    if not isinstance(fields, dict):
        raise ValueError("voices must be a map from 'desk' or an agent id to a voice name")
    out = {}
    for who, name in fields.items():
        who = str(who or "").strip()
        if not who or len(who) > 60:
            raise ValueError("a voice belongs to 'desk' or to an agent id")
        name = str(name or "").strip().lower()
        if name and name not in voice_choices(who):
            raise ValueError(f"{name} is not a voice {'The front desk' if who == DESK else 'an agent'}"
                             " can use")
        out[who] = name
    return out


def memory_turns():
    """How many turns go into the briefing."""
    return _setting("memory_turns")


def recall_span():
    """How many turns before and after each yes hit recall hands back."""
    return _setting("recall_span")


def silence_minutes():
    """How many minutes with nobody speaking hang a call up."""
    return _setting("silence_minutes")


def settings():
    """What the Config page shows: each value, and each one's default and range."""
    out = {name: _setting(name) for name in LIMITS}
    out["limits"] = {name: {"min": lo, "max": hi, "default": _default(name)}
                     for name, (lo, hi) in LIMITS.items()}
    out.update({name: time_setting(name) for name in TIMES})
    out["times"] = dict(TIMES)
    # Only the voices someone set, plus the front desk's. The page fills in each agent's default itself
    # from `agent_voice`, so a new agent needs no entry here to show a voice.
    saved = _saved().get("voices")
    out["voices"] = {who: voice_for(who) for who in (saved if isinstance(saved, dict) else {})}
    out["voices"][DESK] = voice_for(DESK)
    out["voice_choices"] = {"desk": list(config.LIVE_VOICES),
                            "agent": list(config.SPEECH_VOICES)}
    out["desk_voice"] = config.VOICE_NAME
    out["agent_voice"] = config.AGENT_VOICE
    out["prewarm"] = prewarm()
    out["record_calls"] = record_calls()
    return out


def save_settings(fields):
    """Save whichever of the settings `fields` names. Out of range is pulled into range; not a
    number is refused, and then nothing is saved."""
    fields = fields if isinstance(fields, dict) else {}
    want = {name: _clamp(name, fields[name]) for name in LIMITS if name in fields}
    want.update({name: _time(name, fields[name]) for name in TIMES if name in fields})
    voices = _voices(fields["voices"]) if "voices" in fields else {}
    if "prewarm" in fields:
        if not isinstance(fields["prewarm"], bool):
            raise ValueError("prewarm is true or false")
        want["prewarm"] = fields["prewarm"]
    if "record_calls" in fields:
        if not isinstance(fields["record_calls"], bool):
            raise ValueError("record_calls is true or false")
        want["record_calls"] = fields["record_calls"]
    if not want and not voices:
        raise ValueError("send memory_turns, recall_span, silence_minutes, morning_brief, "
                         "quiet_start, quiet_end, voices, prewarm or record_calls")
    with _lock:
        held = _saved()
        was_desk = voice_for(DESK)
        held.update(want)
        if voices:
            mine = dict(held.get("voices") if isinstance(held.get("voices"), dict) else {})
            for who, name in voices.items():
                if name:
                    mine[who] = name
                else:
                    mine.pop(who, None)
            held["voices"] = mine
        path = _settings_file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(held))
        tmp.replace(path)
    if voices and voice_for(DESK) != was_desk:
        # The recorded "Welcome back" is in his old voice. Dropped, so the next call takes it
        # again in the new one, rather than every call opening in a voice he no longer has.
        from . import voice
        voice.reset_greeting()
    said = dict(want, **{f"voice {who}": name or "default" for who, name in voices.items()})
    log.info("voice settings saved: %s",
             ", ".join(f"{k} {v}" for k, v in sorted(said.items())))
    return settings()


def record_calls():
    """Whether the app records each call, both voices, and sends it here. On unless he turned it
    off on Config: Android's own screen recorder leaves call audio out, so this is the record."""
    held = _saved().get("record_calls")
    return held if isinstance(held, bool) else True


def set_memory_turns(value):
    return save_settings({"memory_turns": value})


# -- the log ---------------------------------------------------------------------
# Every row of calls.jsonl, read once and then extended from where the reading stopped, so a
# recall on a large file reads only what was written since the last one. A file that got shorter
# was replaced, and it is read again from the top.
#
# Every row carries n, its 1 based line number in the file. The file is only ever appended to, so
# a turn's number never changes, and the voice can ask for turns by number.

class _Log:
    def __init__(self, path):
        self.path = str(path)
        self.offset = 0
        self.rows = []          # {n, at, who, text, key}, in file order
        self.lines = 0          # lines read so far, blank and broken ones included
        self.by_n = {}          # line number -> position in rows

    def extend(self):
        """Read whatever was written since last time. -1 when the file got shorter."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                if size < self.offset:
                    return -1
                if size == self.offset:
                    return 0
                fh.seek(self.offset)
                data = fh.read(size - self.offset)
        except OSError:
            return 0
        # Only whole lines. A row being written as this reads is picked up next time.
        end = data.rfind(b"\n")
        if end < 0:
            return 0
        self.offset += end + 1
        added = 0
        for raw in data[:end].split(b"\n"):
            self.lines += 1
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict):
                self._add(row, self.lines)
                added += 1
        return added

    def _add(self, row, n):
        text = " ".join(str(row.get("text") or "").split())
        try:
            at = float(row.get("at"))
        except (TypeError, ValueError):
            at = 0.0
        who = row.get("who")
        self.by_n[n] = len(self.rows)
        self.rows.append({"n": n, "at": at, "who": who, "text": text,
                          "key": row_key(at, who, text)})


def row_key(at, who, text):
    """A name for one row that does not depend on where it sits in the file."""
    return hashlib.sha1(f"{at!r}|{who}|{text}".encode()).hexdigest()[:20]


def spoken(row):
    """True for a turn one of them said. A note is the console, and is never searched."""
    return row["who"] in ("you", "voice") and bool(row["text"])


_log = None


def calls_log():
    """The call log as it stands now, extended or read again as needed."""
    global _log
    path = config.VOICE_DIR / "calls.jsonl"
    with _lock:
        if _log is None or _log.path != str(path):
            _log = _Log(path)
        if _log.extend() < 0:
            _log = _Log(path)
            _log.extend()
        return _log


# -- the vectors -----------------------------------------------------------------
# One vector per spoken turn, made once and never again. Kept in var/voice/embeddings.jsonl, one
# row per turn: {at, who, key, v}, where v is the vector as little endian float32, base64. Base64
# rather than a list of numbers because a list of 512 floats written as JSON text is about 10 kB
# a row and this is under 3 kB, and it stays one readable JSON row per turn that can be appended
# to and never rewritten. The vectors are also held in memory, unit length, so a cosine is a dot
# product.

EMBED_URL = "https://api.openai.com/v1/embeddings"
EMBED_MODEL = "text-embedding-3-small"

# The model's own vectors are 1536 long, and OpenAI documents asking for shorter ones with the
# `dimensions` field. 512 keeps most of the quality at a third of the disk and a third of the
# arithmetic, and the arithmetic matters here: the daemon is standard library only, so every
# cosine is a Python loop.
DIMENSIONS = 512

# Inputs per embedding request. The owner's number.
BATCH = 100

# A turn longer than this is embedded by its start. A monologue is about what it opens with.
EMBED_CHARS = 2000

_vectors = {}           # key -> array('f'), unit length
_vectors_path = None


def _embeddings_file():
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return config.VOICE_DIR / "embeddings.jsonl"


def _unit(values):
    vec = array.array("f", values)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array.array("f", (x / norm for x in vec))


def _pack(vec):
    return base64.b64encode(vec.tobytes()).decode()


def _unpack(text):
    vec = array.array("f")
    vec.frombytes(base64.b64decode(text))
    return vec


def _load_vectors():
    """The saved vectors, read once per file."""
    global _vectors, _vectors_path
    path = _embeddings_file()
    if _vectors_path == str(path):
        return _vectors
    _vectors, _vectors_path = {}, str(path)
    try:
        with open(path) as fh:
            for raw in fh:
                try:
                    row = json.loads(raw)
                    vec = _unpack(row["v"])
                except (ValueError, KeyError, TypeError):
                    continue
                if len(vec) == DIMENSIONS:
                    _vectors[row["key"]] = vec
    except OSError:
        pass
    return _vectors


def _say(row):
    return f"{_who(row['who'])}: {row['text'][:EMBED_CHARS]}"


# Seconds to wait for one embeddings reply before the one retry.
EMBED_TIMEOUT = 20


def post_embed(payload, key, timeout=None):
    """One POST to OpenAI's embeddings. Replaced wholesale in tests.

    The same transport the voice uses to open a call, so the key goes in a header and every
    error that comes out has been scrubbed of it.
    """
    from . import voice
    # OpenAI's embeddings usually answer in half a second and now and then take ten. So they get
    # their own, longer limit and one more try, rather than the twelve seconds meant for Jev: a
    # slow reply once cost a whole recall.
    last = None
    for _ in range(2):
        try:
            return voice.post_json(EMBED_URL, payload, key, timeout=timeout or EMBED_TIMEOUT)
        except RuntimeError as exc:
            last = exc
    raise RuntimeError(voice._safe(str(last).replace("the session", "the embedding"),
                                   key)) from None


def embed(texts):
    """Unit vectors for some texts, in order. Raises RuntimeError with a scrubbed sentence."""
    if not config.OPENAI_KEY:
        raise RuntimeError("There is no OpenAI key, so nothing can be embedded.")
    out = []
    for start in range(0, len(texts), BATCH):
        chunk = texts[start:start + BATCH]
        got = post_embed({"model": EMBED_MODEL, "input": chunk, "dimensions": DIMENSIONS},
                         config.OPENAI_KEY)
        from . import openai_log
        tin, _ = openai_log.tokens_of(got)
        openai_log.record("memory", EMBED_MODEL, tokens_in=tin or 0)
        items = sorted((got or {}).get("data") or [], key=lambda d: d.get("index", 0))
        if len(items) != len(chunk):
            raise RuntimeError("OpenAI answered the embedding with the wrong number of vectors.")
        for item in items:
            vec = item.get("embedding")
            if not isinstance(vec, list) or len(vec) != DIMENSIONS:
                raise RuntimeError("OpenAI answered the embedding with a vector of the wrong size.")
            out.append(_unit(vec))
    return out


def catch_up(lg):
    """Embed every spoken turn that has no vector yet, and save each one. Returns how many.

    Saved batch by batch, so a failure halfway keeps what was already paid for.
    """
    held = _load_vectors()
    missing = [r for r in lg.rows if spoken(r) and r["key"] not in held]
    seen, todo = set(), []
    for r in missing:
        if r["key"] not in seen:
            seen.add(r["key"])
            todo.append(r)
    done = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        vecs = embed([_say(r) for r in chunk])
        with open(_embeddings_file(), "a") as fh:
            for r, vec in zip(chunk, vecs):
                fh.write(json.dumps({"at": r["at"], "who": r["who"], "key": r["key"],
                                     "v": _pack(vec)}) + "\n")
                held[r["key"]] = vec
        done += len(chunk)
    return done


# -- the candidates --------------------------------------------------------------

# How much of one turn Jev reads.
CANDIDATE_CHARS = 700

# How many hits go back when Jev cannot judge them.
FALLBACK = 5



def _live_rows(lg):
    """How many rows at the end of the log belong to the call in hand.

    Nothing writes down the moment a call opens, so the window is the marker, the same rule the
    propose tool uses: a session runs for VOICE_SESSION_MINUTES at most.
    """
    cutoff = clock.now() - config.VOICE_SESSION_MINUTES * 60.0
    count = 0
    for row in reversed(lg.rows):
        if row["at"] < cutoff:
            break
        count += 1
    return count


def searchable_limit(lg):
    """The first row recall may not return: the call in hand and the window before it are
    already in front of the voice, so searching them finds what it can already see."""
    return max(0, len(lg.rows) - _live_rows(lg) - memory_turns())


def nearest(lg, query_vec, k=None):
    """[(cosine, row)] for the k spoken rows closest to the query, outside the window."""
    # config.VOICE_RECALL_K, PLATFORM_VOICE_RECALL_K. Hits, not candidates: each hit can bring
    # two neighbours with it.
    k = config.VOICE_RECALL_K if k is None else k
    held = _load_vectors()
    scored = []
    for i in range(searchable_limit(lg)):
        row = lg.rows[i]
        if not spoken(row):
            continue
        vec = held.get(row["key"])
        if vec is None:
            continue
        scored.append((sum(a * b for a, b in zip(query_vec, vec)), i))
    scored.sort(key=lambda p: (-p[0], -p[1]))
    return scored[:max(0, int(k))]


def around(lg, i):
    """The turns from n minus the span to n plus the span round the row at position i, notes
    left out, clamped to the file. Positions in rows, in the order they were said. The span is
    the recall_span setting, 2 unless the Config page says otherwise."""
    n = lg.rows[i]["n"]
    span = recall_span()
    out = []
    for m in range(max(1, n - span), n + span + 1):
        j = lg.by_n.get(m)
        if j is not None and spoken(lg.rows[j]):
            out.append(j)
    return out


def _date(at):
    t = time.localtime(at)
    return f"{time.strftime('%a', t)} {t.tm_mday} {time.strftime('%b', t)}"


def _who(who):
    return "The owner" if who == "you" else "The front desk"


def _turn(lg, i):
    row = lg.rows[i]
    who = "note" if row["who"] == "note" else _who(row["who"])
    return {"n": row["n"], "date": _date(row["at"]), "who": who, "text": row["text"]}


# -- Jev -------------------------------------------------------------------------
# One request to TypeSafe's HTTP API, https://docs.typesafe.ai/api.md. The state holds the live
# call, what the voice is trying to remember, and the candidates. The questions are one Noul per
# candidate, https://docs.typesafe.ai/primitives/noul.md, all in one request, so they run in
# parallel and come back together. A Noul is a probability of yes: it qualifies a turn, it does
# not rank it.

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"

# How much of the call in hand Jev reads, newest last.
LIVE_TURNS = 6

# A turn is kept when Jev puts yes above this. 0.5 is where yes becomes likelier than no; the
# docs say to start there and move it on real data.
KEEP_ABOVE = 0.5

# How many characters of remembered turns go back to the voice. Enough for several five turn
# ranges; past it, whole ranges are dropped from the weakest hit up.
BUDGET = 4000

CRITERIA = {
    "true": "The remembered turn itself says something the front desk can use now about what he is "
            "trying to remember: a fact, a number, a decision, a plan, a request, or what "
            "The owner said about it.",
    "false": "The remembered turn is about something else, or it has no content of its own: a "
             "greeting, a filler reply such as hmm, okay, sure or one moment, or a fragment. "
             "A turn is no even when it sits beside a useful one.",
}


def _question(n, turn):
    """One Noul for one candidate. The turn is in the instructions as well as in the state, so
    the question is anchored on its own words rather than on its neighbours'."""
    return {
        "type": "noul",
        "instructions": {
            "situation": "The owner and the front desk, his voice assistant, are on a call. `live_call` is "
                         "the last few turns of it, newest last. The front desk is searching his memory "
                         "of earlier calls, and `trying_to_remember` says what he is looking for.",
            "remembered_turn": turn,
            "question": f"Judge `remembered_turn`, which is `remembered_turns[{n}]` in the "
                        "state, on its own words. Given the live call and what the front desk is trying "
                        "to remember, is this remembered turn relevant and worth saying now?",
        },
        "criteria": CRITERIA,
    }


def jev_payload(live, about, turns):
    """The request body: one state, one Noul per candidate, keyed by the candidate's id."""
    remembered = [{"id": t["id"], "date": t["date"], "speaker": t["who"],
                   "text": t["text"][:CANDIDATE_CHARS]} for t in turns]
    state = {"live_call": live, "trying_to_remember": about, "remembered_turns": remembered}
    questions = {t["id"]: _question(n, {"speaker": t["speaker"], "text": t["text"]})
                 for n, t in enumerate(remembered)}
    return {"state": state, "model": JEV_MODEL, "questions": questions}


def _safe(text):
    """An error with the TypeSafe key taken out, the same way voice._safe does for OpenAI's."""
    from .voice import _safe as scrub
    return scrub(text, config.TYPESAFE_KEY)


def post_jev(payload, key, timeout=None):
    """One POST to TypeSafe. Replaced wholesale in tests, which is why it is a module function.

    The key goes in the Authorization header and nowhere else. Every error that leaves here has
    been scrubbed of it.
    """
    req = urllib.request.Request(
        JEV_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.TYPESAFE_TIMEOUT) as answer:
            return json.loads(answer.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            said = exc.read().decode()[:200]
        except Exception:
            said = ""
        raise RuntimeError(_safe(f"TypeSafe refused the request ({exc.code}): {said}")) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(_safe(f"TypeSafe could not be reached: {exc.reason}")) from None
    except ValueError:
        raise RuntimeError("The TypeSafe key is not a usable header value. "
                           "Check var/secrets.env for stray spaces or line endings.") from None
    except OSError as exc:
        raise RuntimeError(_safe(f"TypeSafe answered with something unreadable: {exc}")) from None


def _live_call(lg):
    """The last few turns of the call in hand, newest last, as Jev reads them."""
    cutoff = clock.now() - config.VOICE_SESSION_MINUTES * 60.0
    out = []
    for row in reversed(lg.rows):
        if row["at"] < cutoff or len(out) >= LIVE_TURNS:
            break
        if spoken(row):
            out.append({"speaker": _who(row["who"]), "text": row["text"][:CANDIDATE_CHARS]})
    return list(reversed(out))


def pick(lg, hits, answers):
    """The ranges round every hit Jev said yes to, merged, within BUDGET, in the order said.

    `hits` are positions in rows, closest by cosine first. Each yes brings its range; a range is
    kept whole or not at all, from the closest hit down, so the budget drops the weakest ranges
    rather than cutting a conversation in half. A turn two ranges share is counted once. Returns
    (turns, yes count).
    """
    yes = []
    for i in hits:
        got = (answers or {}).get(f"m{lg.rows[i]['n']}") or {}
        p = got.get("noul") if isinstance(got, dict) else None
        if isinstance(p, (int, float)) and p > KEEP_ABOVE:
            yes.append(i)
    kept, spent = set(), 0
    for i in yes:
        new = [j for j in around(lg, i) if j not in kept]
        size = sum(len(lg.rows[j]["text"]) for j in new)
        if spent + size > BUDGET and kept:
            continue
        kept.update(new)
        spent += size
    marked = set(yes)
    out = []
    for j in sorted(kept):
        turn = _turn(lg, j)
        if j in marked:
            turn["hit"] = True
        out.append(turn)
    # The one range that alone overruns the budget is cut, turn by turn, rather than dropped.
    if spent > BUDGET and out:
        each = max(80, BUDGET // len(out))
        for turn in out:
            if len(turn["text"]) > each:
                turn["text"] = turn["text"][:each - 4] + " ..."
    return out, len(yes)


# -- reading by number -----------------------------------------------------------

# The most turns one call of the turns tool reads.
MAX_READ = 60


def _number(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def turns(start, end=None):
    """Turns n=start..end inclusive, notes marked, clamped to the file, 60 at most.

    Never raises: a number that is not a number is read as the nearest end of the file.
    """
    lg = calls_log()
    last = lg.lines
    if not last:
        return {"turns": [], "note": "the call log is empty"}
    lo = max(1, min(_number(start, 1), last))
    hi = max(1, min(_number(end, lo) if end is not None else lo, last))
    if hi < lo:
        lo, hi = hi, lo
    out, capped = [], None
    for m in range(lo, hi + 1):
        j = lg.by_n.get(m)
        if j is None:
            continue
        if len(out) >= MAX_READ:
            capped = m
            break
        out.append(_turn(lg, j))
    answer = {"turns": out, "last": last}
    if capped:
        answer["note"] = f"capped, ask for n={capped} onward"
    log.info("voice turns: %d to %d, %d turns%s", lo, hi, len(out), ", capped" if capped else "")
    return answer


# -- recall ----------------------------------------------------------------------

NOTHING = "nothing relevant"
UNRANKED = ("unranked: these are the closest matches by meaning, not checked for relevance, "
            "because the relevance check was not available")
UNAVAILABLE = "memory search is unavailable right now"


def recall(keywords, about=None):
    """What the voice remembers about something: {"memories": [...], "note"?: ...}.

    Never raises. The voice is mid sentence and a tool that fails there costs more than a tool
    that says it found nothing.
    """
    started = time.monotonic()
    keywords = " ".join(str(keywords or "").split())
    about = " ".join(str(about or "").split())
    embedded = yes = 0
    embed_ms = jev_ms = 0.0
    ran = False
    answer = {"memories": [], "note": UNAVAILABLE}
    try:
        with _lock:
            lg = calls_log()
            query = None
            t = time.monotonic()
            try:
                embedded = catch_up(lg)
                query = embed([" ".join(p for p in (keywords, about) if p) or "?"])[0]
            except Exception as exc:
                from . import voice
                log.info("voice recall: memory search unavailable, %s",
                         voice._safe(str(exc), config.OPENAI_KEY).split(":")[0][:80])
            embed_ms = (time.monotonic() - t) * 1000
            if query is not None:
                hits = [i for _, i in nearest(lg, query)]
                candidates = [dict(_turn(lg, i), id=f"m{lg.rows[i]['n']}") for i in hits]
                live = _live_call(lg)
        if query is not None:
            answer = None
            if not hits:
                answer = {"memories": [], "note": NOTHING}
            elif config.TYPESAFE_KEY:
                t = time.monotonic()
                try:
                    got = post_jev(jev_payload(live, about or keywords, candidates),
                                   config.TYPESAFE_KEY)
                    ran = True
                    found, yes = pick(lg, hits,
                                      got.get("answers") if isinstance(got, dict) else None)
                    answer = {"memories": found} if found else {"memories": [], "note": NOTHING}
                except Exception as exc:
                    # The reason up to its colon. What follows can be the service's own words
                    # about the request, and the request holds his turns.
                    log.info("voice recall: Jev failed, %s", _safe(str(exc)).split(":")[0][:80])
                jev_ms = (time.monotonic() - t) * 1000
            if answer is None:
                top = sorted(hits[:FALLBACK])
                answer = {"memories": [dict(_turn(lg, i), hit=True) for i in top],
                          "note": UNRANKED}
    except Exception as exc:
        log.info("voice recall: failed, %s", type(exc).__name__)
        answer = {"memories": [], "note": UNAVAILABLE}
    log.info("voice recall: %d rows embedded, k %d, %d yes, %d turns returned, jev %s, "
             "embed %d ms, jev %d ms, %d ms in all",
             embedded, config.VOICE_RECALL_K, yes, len(answer["memories"]),
             "ran" if ran else "did not run", round(embed_ms), round(jev_ms),
             round((time.monotonic() - started) * 1000))
    return answer


def reset():
    """Forget the log and the vectors held in memory. For tests."""
    global _log, _vectors, _vectors_path
    with _lock:
        _log = None
        _vectors, _vectors_path = {}, None
