"""Where everything lives, and the few numbers worth naming once.

The package is called commandcenter rather than platform because a Python package named
platform shadows the standard library module of the same name, and the failure is silent
until something deep in psutil asks for platform.system().
"""

import ipaddress
import json
import logging
import os
import re
from pathlib import Path

# Named by hand rather than through logs.get, because logs imports config for the log directory and
# importing back would be a cycle. This is the one module that cannot use the helper.
log = logging.getLogger("cc.config")

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "fonts"


def web_dir(raw):
    """Which built frontend the console serves, from what PLATFORM_WEB says.

    A relative value is joined onto the repository root, so PLATFORM_WEB=web-react/dist means
    the same thing from any working directory. An absolute value is taken as it is, which is how
    a test points the console at a temporary folder.
    """
    raw = (raw or "").strip()
    if not raw:
        return ROOT / "web"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


# The old console and the new React one sit side by side, and this variable is the whole rollback
# story: set it, restart, and the new frontend is up; unset it, restart, and the old one is back.
# No build step chooses, no file is moved, and nothing has to be undone in a hurry at midnight.
WEB = web_dir(os.environ.get("PLATFORM_WEB", ""))


def view_wrapper():
    """The page an agent's own screen is wrapped in.

    It lives in the frontend folder, and a Vite build does not ship one: the build emits
    index.html and a hashed bundle and nothing else. So the old web/ folder answers when the
    frontend in use has no wrapper of its own. The wrapper belongs to the platform rather than to
    the console UI, and an agent screen has to keep working while that UI is being replaced.
    """
    here = WEB / "view.html"
    return here if here.exists() else ROOT / "web" / "view.html"

# Agent-authored view modules, one directory per agent: views/<agent id>/view.js
VIEWS = Path(os.environ.get("PLATFORM_VIEWS", ROOT / "views"))

# Runtime state. Outside the repository in spirit, since the repository becomes public.
VAR = Path(os.environ.get("PLATFORM_VAR", ROOT / "var"))
AGENTS_FILE = VAR / "agents.json"
CORDS_FILE = VAR / "cords.json"
TRANSFER_LOG = VAR / "transfers.jsonl"
PRICES_FILE = VAR / "prices.json"

# What was up when the daemon last stopped, so a restart can put it back. Answer 19 of the daemon
# grill asked for exactly this file.
STATUS_FILE = VAR / "status.json"

# Scheduled work. One entry per job, each naming one agent.
JOBS_FILE = VAR / "jobs.json"
JOB_LOG = VAR / "jobs.jsonl"
# A job set to show in the chat leaves its result here, one jsonl per agent, for the owner to read.
# The agent never reads it.
SHOWN_DIR = VAR / "shown"

# Which agent owns which port. A registry rather than an agent picking for itself, so two agents
# cannot choose the same number and discover it at startup.
PORTS_FILE = VAR / "ports.json"

# The last Telegram update the bridge has seen. Kept so a restart does not replay this morning's
# messages.
TELEGRAM_FILE = VAR / "telegram.json"

# The voice front desk keeps two files: every turn of every call, and the minutes spent today.
VOICE_DIR = VAR / "voice"
# What the phone app sends and fetches: its notifications, and commands such as an alarm to set.
PHONE_DIR = VAR / "phone"

PORT = int(os.environ.get("PLATFORM_PORT", "8080"))
BIND = os.environ.get("PLATFORM_BIND", "0.0.0.0")

# Optional. The tailnet check below is the real door; this is for running the console on a
# laptop that is temporarily off the tailnet.
TOKEN = os.environ.get("PLATFORM_TOKEN", "")

CLAUDE_BIN = os.environ.get("PLATFORM_CLAUDE_BIN", "claude")
OPENCODE_BIN = os.environ.get("PLATFORM_OPENCODE_BIN", "opencode")

# Where OpenCode keeps its sessions. One SQLite database for every project on this machine, read
# read-only by transcript.py. PLATFORM_OPENCODE_DB is for a non-default install and for tests.
OPENCODE_DB = Path(os.environ.get("PLATFORM_OPENCODE_DB")
                   or Path.home() / ".local" / "share" / "opencode" / "opencode.db")

# The Codex CLI, for an agent whose engine is "codex". The daemon's PATH may not hold
# ~/.local/bin, so that folder is the fallback when the name alone is not found.
CODEX_BIN = os.environ.get("PLATFORM_CODEX_BIN", "codex")

# Where Codex keeps its conversations: sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl.
# The platform reads them and never writes into them.
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
# Claude Code's login. live_usage.py reads it to ask Anthropic for the plan limits, and never
# writes it: Claude Code renews it.
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
# Claude Code's own state file. The broker marks an agent's folder as trusted in it, see
# broker.trust_folder.
CLAUDE_STATE = Path.home() / ".claude.json"

# Where Claude Code keeps every conversation on this laptop, one folder per working directory.
# The platform reads it and never writes into it. The recruit screen lists what is there, so a
# conversation started by hand months ago can be adopted as an agent.
#
# PLATFORM_CLAUDE_PROJECTS is the name, in the same shape as every other setting here.
# CLAUDE_PROJECTS is the older spelling and still answers, because a shell somewhere already
# exports it and breaking that would move a fleet's transcripts out of sight.
PROJECTS = Path(os.environ.get("PLATFORM_CLAUDE_PROJECTS")
                or os.environ.get("CLAUDE_PROJECTS")
                or Path.home() / ".claude" / "projects")

# acceptEdits lets an agent write files but still stops to ask before running a command.
# bypassPermissions removes both. The console shows the question, so a stuck agent is
# visible rather than mysterious, which is why the cautious default is affordable.
DEFAULT_PERMISSION_MODE = os.environ.get("PLATFORM_PERMISSION_MODE", "bypassPermissions")

# Whose Ergo this is. Empty means "the owner" in the agents' tool descriptions; a name set here
# replaces that phrase, so the agents say "ask Sam" rather than "ask the owner".
OWNER = (os.environ.get("PLATFORM_OWNER") or "").strip()

# The home agent: the one that owns a task nobody else put on the owner's list, hears about an
# alarm nobody else set, and takes what is not addressed to anyone.
HOME_AGENT = (os.environ.get("PLATFORM_HOME_AGENT") or "personal-assistant").strip()

# The house: the folder the agents' own folders live under. A watch job may watch only inside it,
# and the Instructions editor lists the CLAUDE.md files found there.
HOME = Path(os.path.expanduser(os.environ.get("ERGO_HOME") or "~/ergo"))

# Register every agent with Remote Control, so each one appears in the operator's own Claude app and
# at claude.ai/code with its conversation synced both ways. The name given is the agent id, so the
# app lists them by name rather than by hostname.
#
# This is the one thing the platform cannot build for itself. Remote Control runs inside the session,
# so it holds each message before anything draws it; the console stands outside with a pseudo-terminal
# and can only read what was drawn. Worth turning on for that reason alone.
#
# It signs in with the operator's own claude.ai account, and the sessions appear only there.
# Off by default. Registering every agent puts every one of them in the operator's Claude app, which
# is not what was wanted: "i don't want every agent to come to me in remote session". Turn it on with
# PLATFORM_REMOTE_CONTROL=1 when you do, or type /remote-control in the one agent you want.
REMOTE_CONTROL = os.environ.get("PLATFORM_REMOTE_CONTROL", "0").lower() in (
    "1", "true", "yes", "on")

# How full an agent's conversation may get before Claude Code compacts it, passed as --autocompact.
# Claude Code's own default compacts near 967K tokens, and every wake after an hour asleep re-writes
# the whole conversation to the cache at twice the input price, so a conversation kept under this
# costs far less to wake. Empty leaves Claude Code's default.
AUTOCOMPACT = os.environ.get("PLATFORM_AUTOCOMPACT", "400k").strip()

# Work, demo and personal. Named here so the console and the record agree. They hide cards from
# somebody glancing at the screen; they are not a permission, and a hidden agent's jobs still run.
VIEW_NAMES = ("work", "demo", "personal")

# How long to wait for an agent's interface to stop drawing before typing into it, and how much
# silence counts as ready. A resumed session takes seconds to read its conversation back, and text
# typed into it before then is lost. The cap matters as much as the signal: an interface that keeps
# painting, or one waiting on a keypress, would otherwise hold a message for ever.
PROMPT_WAIT = float(os.environ.get("PLATFORM_PROMPT_WAIT", "8.0"))
PROMPT_QUIET = float(os.environ.get("PLATFORM_PROMPT_QUIET", "1.5"))

HEALTH_INTERVAL = 2.0     # seconds between health frames on the event stream
# A comment line on a quiet event stream, so nothing between here and the phone decides the
# connection is dead. About 40 bytes on the wire, so 45 s apart costs about 80 KB a day.
EVENTS_KEEPALIVE = 45.0
POLL_INTERVAL = 1.0       # seconds between transcript checks
CALL_BUDGET = 8           # messages in total across both agents, a PRD decision
CHAT_PAGE = 60            # messages returned on a first chat load

# -- the schedule ---------------------------------------------------------------
# Five jobs at 07:00 means five agents waking at 400 to 700 MB each, so four wake at once and the
# fifth waits for a slot.
WAKE_AT_ONCE = int(os.environ.get("PLATFORM_WAKE_AT_ONCE", "4"))

# Daily jobs get a few seconds of jitter, so five jobs set for 07:00 do not all fire in one tick
# and race each other for the four slots above. Derived from the job id, so it is the same few
# seconds every day and a job that looks late is late for a real reason.
JITTER_SECONDS = 20

# How long one job may take before it is given up on. Fifteen minutes is long for a question and
# short for a build, which is why a job may override it.
JOB_TIMEOUT = float(os.environ.get("PLATFORM_JOB_TIMEOUT", "900"))

# How often the scheduler looks at the clock. Ten seconds is fine for jobs measured in minutes,
# and it means a job set for 07:00 fires within ten seconds of it.
TICK_SECONDS = 10.0

# Watch jobs, see watch.py: a job that runs when files change, not at a time. A job may watch only
# inside these folders, comma separated, and the house is the default. A fresh watch run may read
# its watched folder only inside the house, see fresh.read_rules.
WATCH_ROOTS = [os.path.normpath(os.path.expanduser(part.strip()))
               for part in os.environ.get("PLATFORM_WATCH_ROOTS", str(HOME)).split(",")
               if part.strip()]
# A burst of writes runs the job once: it runs this many seconds after the last change.
WATCH_DEBOUNCE = 30.0
WATCH_MAX_JOBS = 20            # per agent
WATCH_MAX_FIRES_HOUR = 6       # one more run than this in an hour pauses the job
WATCH_MAX_DIRS = 5000          # folders one job may watch; a deeper tree is cut off here
WATCH_MAX_WATCHES = 20000      # folders the daemon watches in all, well under max_user_watches
# A job's check command, see cron._check: it must finish in this many seconds, or the run goes ahead.
CHECK_TIMEOUT = 60.0

# What the agent says when it is finished, answer 37. The same trick as the HANGUP: line in cords,
# because one convention the agents already know beats a second one they have to learn.
SUCCEEDED = "task succeeded"
FAILED = "task failed"

# -- fetching for screens -------------------------------------------------------
# A screen has no network of its own, so the daemon fetches for it. Answer 30 says any address.
# Neither limit below is about trust: they stop one screen holding a thread and a tab for ever.
FETCH_TIMEOUT = 10.0
FETCH_MAX_BYTES = 5 * 1024 * 1024

# -- agent servers --------------------------------------------------------------
# Ports handed out from here. Loopback only, and well clear of the console's own 8080.
PORT_RANGE = (8100, 8199)

# A server that dies is restarted, up to this many times inside the window, and then left down
# with a Telegram sent. Without a cap, a server that fails at startup restarts for ever and the
# log becomes one error repeated a thousand times.
SERVER_RESTARTS = 5
SERVER_WINDOW = 120.0

# How long the daemon waits for an agent's own server to answer before falling back to the file
# the agent wrote. Short: a screen is waiting on this.
SERVER_TIMEOUT = 5.0

# -- Telegram -------------------------------------------------------------------
# Read from the environment rather than a state file, so a token never lands in the repository.
# deploy/commandcenter.service reads var/secrets.env, which git ignores.
TELEGRAM_TOKEN = os.environ.get("PLATFORM_TELEGRAM_TOKEN", "")

# Answer 44: the operator's own user id and nobody else. A bot address is guessable, so without
# this the door is open to anyone who finds it.
TELEGRAM_OPERATOR = os.environ.get("PLATFORM_TELEGRAM_OPERATOR", "")
# Whether the daemon tells the operator things over Telegram on its own. Off by default: the
# operator writes to the assistant there and reads the answers. See telegram.notify.
TELEGRAM_ALERTS = os.environ.get("PLATFORM_TELEGRAM_ALERTS", "").strip().lower() in ("1", "true", "yes", "on")

# Telegram refuses a message longer than 4096 characters. 4000 leaves room for the marker that
# says a reply was cut into pieces.
TELEGRAM_CHUNK = 4000

# -- the voice front desk -------------------------------------------------------
# The owner talks to a British voice that knows what the fleet is doing. The model runs at
# OpenAI, so the daemon holds the key and the browser never sees it: the browser is handed a
# short lived client secret instead, minted per call.
#
# Read from the environment rather than a state file, the same rule as the Telegram token above.
# dev.sh and deploy/commandcenter.service both source var/secrets.env, which git ignores.
# Stripped, and that is not tidiness. A key written into var/secrets.env with a Windows line
# ending arrives with a trailing carriage return, and a header value cannot hold one, so urllib
# raises a ValueError whose text is the whole header. That error travelled to a browser once and
# put the key on the screen. Stripping here stops it happening; voice.py never repeats an error
# text that could hold the key, which stops it mattering if it happens again.
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# Which speech model holds the conversation. One name in one place, so moving to the next one is
# an edit here. GPT-Live rather than a Realtime model, because it is the only one that offers a
# British voice and the only one whose sessions can be opened without handing the browser a
# credential of its own.
VOICE_MODEL = os.environ.get("PLATFORM_VOICE_MODEL", "gpt-live-1")

# The written model the speech model hands the hard questions to. GPT-Live is two models: the one
# above listens and talks, this one thinks and runs the tools. Terra is the middle of the 5.6
# family and what the delegation guide says to start with. Luna is the cheaper one if this turns
# out to be more model than reading a fleet status aloud needs.
VOICE_BACKEND = os.environ.get("PLATFORM_VOICE_BACKEND", "gpt-5.6-terra")

# The voice itself. Vesper is the only one OpenAI's table describes as British, so it is the
# default, and this variable changes it without a code edit. It cannot be changed once a call has started.
VOICE_NAME = os.environ.get("PLATFORM_VOICE_NAME", "vesper")

# The voices a call can use, from OpenAI's GPT-Live guide (live-conversations, "Voice options").
# GPT-Live takes the Realtime voices and twelve of its own. The speech endpoint, which records an
# agent's opening line before the call, takes only the first group. So an agent picks from that
# group, and the line it rings with is in the same voice it then talks in. The front desk has no
# recorded line of that kind, so it may have any of them.
SPEECH_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse",
                 "marin", "cedar")
LIVE_ONLY_VOICES = ("quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian",
                    "bossa", "tempo", "beacon", "delta", "cinder")
LIVE_VOICES = SPEECH_VOICES + LIVE_ONLY_VOICES

# An agent's voice when nobody has picked one. Not the front desk's, so a call from an agent never
# sounds like the front desk. Cedar is one of the two the speech guide says sound best.
AGENT_VOICE = os.environ.get("PLATFORM_AGENT_VOICE", "cedar")

# The model that records an agent's opening line, and a voice preview on Config.
VOICE_TTS_MODEL = os.environ.get("PLATFORM_VOICE_TTS_MODEL", "gpt-4o-mini-tts")
VOICE_TTS_TIMEOUT = float(os.environ.get("PLATFORM_VOICE_TTS_TIMEOUT", "30"))

# The small model that looks at an image the owner pastes into the call. The live voice cannot see, so
# the daemon asks this one what the image shows and hands the voice the words. Luna is the small
# one of the 6 family; the name was checked against /v1/models before it was put here.
VOICE_VISION_MODEL = os.environ.get("PLATFORM_VOICE_VISION_MODEL", "gpt-6-luna")

# How long the daemon waits for that model. Longer than the call opening, because an image is a
# real piece of work, and the overlay says "Looking" the whole time.
VOICE_VISION_TIMEOUT = float(os.environ.get("PLATFORM_VOICE_VISION_TIMEOUT", "60"))

# Voice notes from the wheel: OpenAI writes the audio down with this model.
VOICE_TRANSCRIBE_MODEL = os.environ.get("PLATFORM_VOICE_TRANSCRIBE_MODEL", "gpt-transcribe")
VOICE_TRANSCRIBE_TIMEOUT = float(os.environ.get("PLATFORM_VOICE_TRANSCRIBE_TIMEOUT", "60"))
# Dictation in the chat: the live models, and the file that holds a Gemini key (GEMINI_API_KEY=...)
# when it is not in the environment.
DICTATE_OPENAI_MODEL = os.environ.get("PLATFORM_DICTATE_OPENAI_MODEL", "gpt-live-transcribe")
DICTATE_GEMINI_MODEL = os.environ.get("PLATFORM_DICTATE_GEMINI_MODEL", "gemini-3.5-transcribe-live")
# The night shift's request list. Agents add to it with the shift_request tool.
SHIFT_REQUESTS = Path(os.environ.get("PLATFORM_SHIFT_REQUESTS") or VAR / "night-shift" / "requests.md")
GEMINI_ENV = os.environ.get("PLATFORM_GEMINI_ENV") or str(VAR / "gemini.env")
# The backend may search the web itself. It is OpenAI's own hosted tool, so there is no
# second key and nothing to scrape: a search engine's html changes and a scraper then
# fails quietly, which is the worst way for a voice to be wrong.
VOICE_WEB_SEARCH = os.environ.get("PLATFORM_VOICE_WEB_SEARCH", "1").lower() not in (
    "0", "false", "no", "off")


# Two ceilings, because speech is billed by the minute and a forgotten tab is a bill. The daily
# one refuses a new call; the session one ends the call in hand, with a warning two minutes first.
VOICE_DAILY_MINUTES = float(os.environ.get("PLATFORM_VOICE_DAILY_MINUTES", "120"))
VOICE_SESSION_MINUTES = float(os.environ.get("PLATFORM_VOICE_SESSION_MINUTES", "30"))

# How long a proposed delegation stays confirmable. The voice reads a task back and waits for a
# yes; past this the yes is stale and the daemon refuses it, because "yes" heard ten minutes later
# is usually about something else.
VOICE_CONFIRM_SECONDS = float(os.environ.get("PLATFORM_VOICE_CONFIRM_SECONDS", "90"))

# How long the daemon waits for OpenAI to mint a secret. A person is holding a phone to their ear.
VOICE_MINT_TIMEOUT = float(os.environ.get("PLATFORM_VOICE_MINT_TIMEOUT", "15"))

# A call waiting on an agent pauses itself after this much quiet. The long silences in a real
# call are not the owner walking away, they are the owner waiting for an agent he delegated to,
# and one of those ran to two hundred and seventy nine seconds. Ending the call there would end
# it in the middle of the thing he was waiting for, so the call pauses instead: the session at
# OpenAI closes, the overlay stays, and it comes back when the agent does or when he speaks.
#
# It only ever happens while at least one agent owes him an answer. Zero turns it off. The
# overlay counts it, because the browser is the only thing that hears the silence.
VOICE_WAIT_PAUSE_SECONDS = float(os.environ.get("PLATFORM_VOICE_WAIT_PAUSE_SECONDS", "45"))

# Small talk while an agent works: public headlines fetched once an hour. Off turns the fetch off,
# and the tests turn it off so they never reach the internet.
SMALLTALK = os.environ.get("PLATFORM_SMALLTALK", "1") != "0"

# A call where nobody has spoken for this many minutes hangs up, the same way Hang up does. Only
# while nothing is working: a tool call, an image with the vision model, or an agent that owes an
# answer all keep it open, and a call waiting on an agent pauses long before this. The default;
# the Config page can set another, 1 to 30, kept in var/voice/settings.json.
VOICE_SILENCE_MINUTES = int(float(os.environ.get("PLATFORM_VOICE_SILENCE_MINUTES", "8")))

# Where the call log cuts one line and starts the next, in milliseconds of quiet from that
# speaker. It decides the shape of a written line and nothing else. It is NOT how GPT-Live decides
# when the voice may speak: that is turn detection, it happens in the audio stream at OpenAI, and
# the API offers no setting for it at all. The two used to share 1400 ms, which cut the owner mid
# sentence into three lines and had the voice answer each fragment. Generous on purpose.
VOICE_TURN_GAP_MS = int(float(os.environ.get("PLATFORM_VOICE_TURN_GAP_MS", "2500")))

# Where the voice may read a file from. Comma separated, and the default is the owner's home,
# which is where his work is. Every path is resolved with its symlinks followed before it is
# checked, so a link out of a root is out of the root. Names are refused on top of this, whatever
# root they sit under: a secrets file inside home is still a secrets file.
VOICE_READ_ROOTS = tuple(
    os.path.normpath(os.path.expanduser(part.strip()))
    for part in os.environ.get("PLATFORM_VOICE_READ_ROOTS", str(Path.home())).split(",")
    if part.strip())

# How many turns of the call log go into the backend briefing at the start of every call. This
# is the default; the Config page can set another number, which is kept in var/voice/settings.json
# and wins over this one. Always 1 to 100.
VOICE_MEMORY_TURNS = int(float(os.environ.get("PLATFORM_VOICE_MEMORY_TURNS", "30")))

# How many turns before and after each match the recall tool hands back, by line number. The
# default; the Config page can set another, 0 to 10, kept in var/voice/settings.json.
VOICE_RECALL_SPAN = int(float(os.environ.get("PLATFORM_VOICE_RECALL_SPAN", "2")))

# How many turns the recall tool's search keeps, closest by meaning first. These are the turns
# Jev judges, one yes or no each; the context round a yes is added afterwards.
VOICE_RECALL_K = int(float(os.environ.get("PLATFORM_VOICE_RECALL_K", "30")))

# The TypeSafe key. Jev says yes or no to what the voice's recall tool finds in the call log. Read from the
# environment and stripped, the same rule as the keys above. Without it, recall still works and
# hands back the closest matches by meaning, unjudged.
TYPESAFE_KEY = os.environ.get("TYPESAFE_API_KEY", "").strip()

# The ask check (askcheck.py): Jev reads each finished turn for an ask of the owner on no list. On
# unless this is 0. It needs the TypeSafe key too.
ASKCHECK = os.environ.get("PLATFORM_ASKCHECK", "1") != "0"

# How long a recall waits for Jev. He is waiting on the phone, so it is short, and a recall that
# times out still answers with the closest matches. The embedding call uses it too.
TYPESAFE_TIMEOUT = float(os.environ.get("PLATFORM_TYPESAFE_TIMEOUT", "12"))

# Tailscale hands out addresses from these ranges and nothing else does.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


def on_tailnet(addr):
    """True when a connection came from the tailnet or from this machine.

    Membership of the tailnet is the authentication, so this function is the door. It reads
    the socket's peer address, which the operating system supplies, never a header, because
    a header is written by whoever is calling.
    """
    try:
        ip = ipaddress.ip_address(addr.replace("::ffff:", "") if addr.count(":") > 1 and "." in addr else addr)
    except ValueError:
        return False
    return any(ip in net for net in TAILNET + LOOPBACK)


def project_dir(cwd):
    """The directory Claude Code keeps a folder's sessions in.

    It flattens the path by replacing every character that is not a letter or digit with a
    dash, so /home/you/my work/thing becomes -home-you-my-work-thing.
    """
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def prices():
    """Dollars per million tokens, per model, read at call time so edits take effect.

    Absent by default. Without it the console reports tokens, which is a fact, rather than
    a cost, which would be a guess.
    """
    try:
        return json.loads(PRICES_FILE.read_text())
    except Exception:
        return {}


def ensure_dirs():
    VAR.mkdir(parents=True, exist_ok=True)
    VIEWS.mkdir(parents=True, exist_ok=True)


def use_var(path, views=None):
    """Point every state file at a different directory.

    For tests, and it exists because the paths above are derived from VAR at import time. Setting
    VAR alone would leave nine paths still pointing at the real `var/`, and the failure would be a
    test that quietly wrote into the operator's own agent list.
    """
    global VAR, VIEWS, AGENTS_FILE, CORDS_FILE, TRANSFER_LOG, PRICES_FILE
    global STATUS_FILE, JOBS_FILE, JOB_LOG, PORTS_FILE, TELEGRAM_FILE, VOICE_DIR
    global PHONE_DIR, SHOWN_DIR, SHIFT_REQUESTS
    VAR = Path(path)
    VIEWS = Path(views) if views else VAR / "views"
    AGENTS_FILE = VAR / "agents.json"
    CORDS_FILE = VAR / "cords.json"
    TRANSFER_LOG = VAR / "transfers.jsonl"
    PRICES_FILE = VAR / "prices.json"
    STATUS_FILE = VAR / "status.json"
    JOBS_FILE = VAR / "jobs.json"
    JOB_LOG = VAR / "jobs.jsonl"
    SHOWN_DIR = VAR / "shown"
    PORTS_FILE = VAR / "ports.json"
    TELEGRAM_FILE = VAR / "telegram.json"
    VOICE_DIR = VAR / "voice"
    PHONE_DIR = VAR / "phone"
    SHIFT_REQUESTS = VAR / "night-shift" / "requests.md"
    ensure_dirs()
    return VAR


PAGE_NAME = re.compile(r"^(?:(\d+)[-_])?([a-z0-9][a-z0-9-]*)\.js$")

# Generous. See the note in pages() for why there is a number here at all.
MAX_PAGES = 24


def resolve_dir(raw):
    """Turn what a person typed into an absolute path, or say why it cannot be one.

    Only a leading ~ is expanded. A relative path is refused rather than joined onto whatever folder
    the daemon happens to be running in, which is never what the person meant and is how a typed
    "home/you/x" ends up as a directory nobody can find. Existence is not checked here: whether a
    missing folder is an error or a folder to create is the caller's decision.
    """
    raw = (raw or "").strip() or "~"
    path = os.path.normpath(os.path.expanduser(raw))
    if not os.path.isabs(path):
        raise ValueError("give a full path, one that starts with / or ~")
    return path


def pages(agent_id):
    """The screens one agent has written, in the order they should appear.

    Discovered by listing the directory rather than read from a manifest. An agent adds a page by
    writing one file, and the tab appears; a manifest would be a second thing to keep in step, and
    the first time it disagreed with the files the tab would point at nothing.

    The filename carries both the title and the order, because that is the only thing the agent
    controls with a single write:

        view.js             ->  "Agent view",  the default page, always first
        positions.js        ->  "Positions"
        20-pnl.js           ->  "Pnl",         ordered after 10-something.js, number not shown

    A number prefix exists because alphabetical order is arbitrary and an agent should be able to
    decide that positions comes before the research log.
    """
    found = []
    try:
        entries = sorted((VIEWS / agent_id).iterdir())
    except (OSError, FileNotFoundError):
        return found
    for path in entries:
        if not path.is_file():
            continue
        m = PAGE_NAME.match(path.name)
        if not m:
            continue
        order, slug = m.group(1), m.group(2)
        found.append({
            "slug": slug,
            "title": "Agent view" if slug == "view" else slug.replace("-", " ").capitalize(),
            "order": (-1 if slug == "view" else int(order or 500)),
            "file": path.name,
        })
    found.sort(key=lambda p: (p["order"], p["slug"]))

    # No artificial limit, but not unbounded either. An agent writes files, so an agent in a loop can
    # write five hundred of them, and a console with five hundred tabs is a console you cannot use.
    # The cap is generous enough that no honest design reaches it.
    total = len(found)
    if total > MAX_PAGES:
        # Never truncate quietly. A dropped tab that nothing mentions reads as a page the agent
        # failed to write, and you go looking in the wrong place for it. `total` is the real count
        # before cutting, which is the number worth reporting.
        log.warning("%s wrote %d pages, showing the first %d", agent_id, total, MAX_PAGES)
        found = found[:MAX_PAGES]
        found[-1] = dict(found[-1], truncated=total)
    return found


def default_page(agent_id):
    """Which page opens when you click into an agent, chosen by the agent.

    An agent states its choice by writing one line into `views/<id>/default`, holding a slug. With no
    such file, or one naming a page that does not exist, the first page by order opens.

    This is the one small manifest in the design and it earns its place. The alternative was for the
    leftmost tab to win, which stopped being the agent's choice the moment the terminal moved to the
    left. Its failure is harmless: an unrecognised name falls back instead of breaking.
    """
    available = pages(agent_id)
    if not available:
        return None
    try:
        wanted = (VIEWS / agent_id / "default").read_text().strip().lower()
    except (OSError, FileNotFoundError):
        wanted = ""
    if wanted:
        for p in available:
            if p["slug"] == wanted:
                return p["slug"]
        log.warning("%s asked for default page %r, which it has not written", agent_id, wanted)
    return available[0]["slug"]


def page_file(agent_id, slug):
    for p in pages(agent_id):
        if p["slug"] == slug:
            return VIEWS / agent_id / p["file"]
    return None
