"""What was up, so a restart can put it back.

Answer 19 of the daemon grill asked for one file holding which agents were awake and whose server
was up, and for the daemon to bring back the last configuration at boot. This is that file.

It is deliberately not the truth about now. Liveness comes from the broker, which owns the
terminals and knows the instant one closes. This is a record of *intent*: what was running before
the daemon stopped, which is the only question a restart needs answered and the one question the
broker cannot answer, because a broker that has just started owns nothing.

Written on every change rather than on a timer. The file is small and the failure it guards
against is a crash, which does not wait for a timer.
"""

import json
import threading
import time

from . import config, logs

log = logs.get(__name__)

_lock = threading.RLock()
_state = None

# What the file said when the daemon started, kept apart from what has happened since. Restoring
# reads this, never the live copy, and the reason is a bug that would be very hard to see: shutting
# down puts every agent to sleep, which records every agent as asleep, so a daemon that read the
# live copy would restore nothing and would look like it was working.
_boot = None

# Set once, by the daemon, on its way out. Everything that happens after it is the shutdown rather
# than a decision, and the difference is the whole point: sleeping every agent on the way out must
# not tell the next daemon that nobody was meant to be awake.
_stopping = False


def _blank():
    return {"version": 1, "agents": {}, "servers": {}, "running_jobs": {}, "written": 0.0}


def stopping():
    """The daemon is shutting down. Record what follows as observation, not intent."""
    global _stopping
    _stopping = True


def _load():
    global _state
    if _state is not None:
        return _state
    config.ensure_dirs()
    try:
        raw = json.loads(config.STATUS_FILE.read_text())
        _state = _blank()
        if isinstance(raw, dict):
            for key in ("agents", "servers", "running_jobs"):
                if isinstance(raw.get(key), dict):
                    _state[key] = raw[key]
    except FileNotFoundError:
        _state = _blank()
    except Exception as exc:
        # A restart with an unreadable status file should start clean rather than refuse to start.
        # Nothing here is irreplaceable: at worst an agent that was awake stays grey until asked.
        log.error("%s unreadable (%s); starting with nothing restored", config.STATUS_FILE, exc)
        _state = _blank()
    global _boot
    _boot = json.loads(json.dumps(_state))
    return _state


def _save():
    state = _load()
    state["written"] = time.time()
    tmp = config.STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(config.STATUS_FILE)


def snapshot():
    with _lock:
        state = _load()
        return json.loads(json.dumps(state))


# -- agents ----------------------------------------------------------------------

def agent(agent_id, awake):
    """Record that an agent woke or slept.

    Two facts, exactly as for servers. `awake` is what was observed. `wanted` is what somebody asked
    for, and it does not change during a shutdown, because a daemon stopping is not a decision that
    an agent should be grey tomorrow.

    That distinction was missing at first, and the frozen boot snapshot hid it: reads inside one
    daemon's life were right, and the file handed to the next daemon said everything was asleep. So
    nothing was ever restored, and it looked like it worked.
    """
    with _lock:
        state = _load()
        row = state["agents"].setdefault(agent_id, {})
        if row.get("awake") == awake and (_stopping or row.get("wanted") == awake):
            return
        row["awake"] = bool(awake)
        if not _stopping:
            row["wanted"] = bool(awake)
        row["at"] = time.time()
        _save()


def were_awake():
    """Which agents the last daemon was meant to have awake. Read from the boot snapshot.

    `wanted` rather than `awake`, so a clean shutdown does not erase the answer. Older files have
    only `awake`, and reading that as the intent is right for them: it was awake, so it was wanted.
    """
    with _lock:
        _load()
        rows = (_boot or {}).get("agents", {}).items()
        return sorted(a for a, row in rows
                      if row.get("wanted", row.get("awake")) and row.get("wanted") is not False)


# -- servers ---------------------------------------------------------------------

def server(agent_id, **fields):
    with _lock:
        state = _load()
        row = state["servers"].setdefault(agent_id, {})
        row.update(fields)
        row["at"] = time.time()
        _save()


def servers_wanted():
    """Which agents' servers should be running. Restored whether or not the agent is awake.

    `wanted` and `up` are two different facts and the difference is the whole point of this file.
    `up` is what was observed, and a clean shutdown sets it false for everything. `wanted` is what
    the operator and the agents asked for, and it survives the shutdown, which is what answer 19
    means by bringing back the last configuration.

    A server outlives its agent's terminal, answer 7, so a grey agent's screen still has data.
    """
    with _lock:
        rows = _load()["servers"].items()
        # Files written before `wanted` existed only recorded `up`. Reading it as the intent is
        # right for them: it was up, so it was wanted.
        return sorted(a for a, row in rows
                      if row.get("wanted", row.get("up")) and row.get("wanted") is not False)


def server_state(agent_id):
    with _lock:
        row = _load()["servers"].get(agent_id)
        return dict(row) if row else None


# -- jobs ------------------------------------------------------------------------

def job_started(job_id, agent_id):
    """Mark a job as in flight.

    Answer 33: a job halfway through when the daemon restarts is a missed run, not a run. The only
    way to know that on the next start is to have written down that it began, because the process
    that was going to write down the ending is gone.
    """
    with _lock:
        _load()["running_jobs"][job_id] = {"agent": agent_id, "started": time.time()}
        _save()


def job_finished(job_id):
    with _lock:
        if _load()["running_jobs"].pop(job_id, None) is not None:
            _save()


def jobs_interrupted():
    """Jobs that were in flight when the daemon stopped, and clear them."""
    with _lock:
        state = _load()
        was = dict(state["running_jobs"])
        if was:
            state["running_jobs"] = {}
            _save()
        return was


def forget(agent_id):
    with _lock:
        state = _load()
        gone = state["agents"].pop(agent_id, None), state["servers"].pop(agent_id, None)
        if any(g is not None for g in gone):
            _save()


# -- wiring ----------------------------------------------------------------------

def follow_broker():
    """Record wake and sleep as they happen.

    A listener rather than a call inside the broker, so the broker keeps knowing nothing about
    files. It already announces both events for the console's benefit; this is a second ear on the
    same announcement.
    """
    from . import broker

    def heard(event, payload):
        who = (payload or {}).get("agent")
        if not who:
            return
        if event == "agent.awake":
            agent(who, True)
        elif event == "agent.asleep":
            agent(who, False)

    broker.on_change(heard)


def reset():
    """Forget the loaded state and the boot snapshot. For tests."""
    global _state, _boot, _stopping
    with _lock:
        _state, _boot, _stopping = None, None, False
