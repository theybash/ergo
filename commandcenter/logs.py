"""Where the daemon writes down what happened.

One file a day, seven kept, the eighth deleted. That is `TimedRotatingFileHandler` doing exactly what
was asked, so there is no rotation code here to get wrong.

This is the first slice of the plan for a reason that is easy to underrate. Everything after it runs
while nobody is watching: a job at 3am, a bridge recovering from a dead network, a server restarted
after a crash. Without a file to read, all of those fail into silence, and the only evidence is that
something you expected did not happen.

crodex v1 had named loggers. Losing them was a regression, and this restores them.
"""

import logging
import logging.handlers
import os
import sys

from . import config

# One logger tree. Every module asks for `logs.get(__name__)` and gets a child of this, so a single
# level change affects everything and the module name appears on every line.
ROOT = "cc"

# The name is what you grep for, so it comes early and at a fixed width. The rest reads in the order
# you need it when something has gone wrong: when, how bad, where, what.
FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_ready = False


def get(name):
    """A logger for one module. Pass `__name__`."""
    short = name.rsplit(".", 1)[-1]
    return logging.getLogger(f"{ROOT}.{short}")


def setup(level=None):
    """Attach the file, and the terminal if a person is watching.

    Called once, early, before anything else can log. Calling it twice is harmless and does nothing,
    because adding the same handler twice writes every line twice and that is a genuinely confusing
    bug to read later.
    """
    global _ready
    if _ready:
        return
    _ready = True

    logdir = config.VAR / "logs"
    logdir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger(ROOT)
    root.setLevel(getattr(logging, (level or os.environ.get("PLATFORM_LOG_LEVEL", "INFO")).upper(),
                          logging.INFO))
    # Without this a message also travels to Python's own root logger, which prints to the terminal
    # with a different format, so every line appears twice in two shapes.
    root.propagate = False

    fmt = logging.Formatter(FORMAT, datefmt=DATEFMT)

    # midnight plus seven backups is the whole of "one file a day, keep seven". The eighth is deleted
    # by the handler, not by us.
    to_file = logging.handlers.TimedRotatingFileHandler(
        logdir / "daemon.log", when="midnight", backupCount=7, encoding="utf-8", utc=False)
    to_file.suffix = "%Y-%m-%d"
    to_file.setFormatter(fmt)
    root.addHandler(to_file)

    # Only echo to the terminal when a person is actually looking at one. Under systemd or dev.sh the
    # output is captured anyway, so echoing would write everything twice in two places.
    if sys.stderr.isatty():
        to_term = logging.StreamHandler(sys.stderr)
        to_term.setFormatter(fmt)
        root.addHandler(to_term)

    return root


def current_file():
    return config.VAR / "logs" / "daemon.log"
