"""An agent's own API server, started and kept up by the daemon.

An agent declares one by writing a file in its own folder. The daemon assigns it a port, writes the
number where the agent can read it, starts the command, and restarts it if it dies.

The decisions here are all from the daemon grill, and two of them are the opposite of the obvious:

    5   The daemon starts the server when the agent wakes.
    7   The server keeps running while the agent sleeps. So a grey agent's screen still has data,
        which is the whole reason this file is not simply part of waking up.
    11  Ports come from a registry the daemon owns, so two agents cannot pick the same number and
        find out at startup.
    18  The agent learns its port from a json the daemon writes.
    19  At boot the daemon brings back whatever was running.
    20  A server that will not start sends a Telegram, and the operator diagnoses it by hand.

There is no shared server, and that was argued rather than assumed. Five agents writing code into
one process means one syntax error breaks every screen and one infinite loop hangs every screen. It
was rejected for that, not for cost: a bare Python server is 13 MB against an awake agent's 400.

Most screens need none of this. An agent that writes a file gets a screen that works while it
sleeps, with no process to supervise. See `data.py`.
"""

import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import config, logs, status, telegram
from .terminal import clean_env

log = logs.get(__name__)

_lock = threading.RLock()
_running = {}            # agent id -> {"proc" or "pid", "port", "started"}
_restarts = {}           # agent id -> deque of timestamps
_wanted = set()          # agents whose server should be up
_stop = threading.Event()

SCAN_SECONDS = 3.0
READY_SECONDS = 5.0


def declaration_path(rec):
    return Path(rec["cwd"]) / ".platform" / "server.json"


def port_path(rec):
    return Path(rec["cwd"]) / ".platform" / "port.json"


def declaration(rec):
    """What the agent asked for, or None. Never raises: a broken file is a message, not a crash."""
    path = declaration_path(rec)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("%s has an unreadable server.json: %s", rec["id"], exc)
        return None
    command = raw.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        log.warning("%s server.json needs a command, a list or a string", rec["id"])
        return None
    return {
        "command": command,
        "enabled": raw.get("enabled", True) is not False,
        # A fixed port if the agent insists, otherwise the registry picks. Insisting is allowed
        # because a program with a hardcoded port is a real thing to meet.
        "port": raw.get("port"),
        "note": (raw.get("note") or "")[:200] or None,
    }


# -- the port registry -----------------------------------------------------------

def _ports():
    try:
        raw = json.loads(config.PORTS_FILE.read_text())
        return dict(raw.get("ports") or {})
    except Exception:
        return {}


def _write_ports(table):
    config.ensure_dirs()
    tmp = config.PORTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "ports": table}, indent=2))
    tmp.replace(config.PORTS_FILE)


def _free(port):
    """Can this process bind the port right now.

    Binding rather than connecting, because a connect probe is answered from the listen backlog
    only as many times as the backlog allows. Once it is full the probe times out, which looks
    exactly like a free port and made a held port read as free on macOS. A bind is the question
    that actually matters here: the next thing done with a free port is binding it.
    """
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def port_for(agent_id, wanted=None):
    """The port this agent uses. Assigned once and then kept.

    Kept rather than reassigned because the number ends up in the agent's own code and in whatever
    it wrote down, and a port that moves on every restart makes every one of those wrong.
    """
    with _lock:
        table = _ports()
        if wanted:
            wanted = int(wanted)
            owner = next((a for a, p in table.items() if p == wanted and a != agent_id), None)
            if owner:
                raise ValueError(f"port {wanted} is already {owner}'s")
            table[agent_id] = wanted
            _write_ports(table)
            return wanted
        if agent_id in table:
            return int(table[agent_id])
        taken = set(table.values())
        low, high = config.PORT_RANGE
        for port in range(low, high + 1):
            if port in taken or not _free(port):
                continue
            table[agent_id] = port
            _write_ports(table)
            log.info("port %d assigned to %s", port, agent_id)
            return port
        raise RuntimeError(f"no free port between {low} and {high}")


def release(agent_id):
    """Give up an agent's port when the agent is forgotten.

    Without this the range fills with assignments for agents that no longer exist, and the numbers
    are only kept in the first place because a port that moves on every restart makes every
    hardcoded copy of it wrong.
    """
    stop(agent_id)
    with _lock:
        table = _ports()
        if table.pop(agent_id, None) is None:
            return False
        _write_ports(table)
    _restarts.pop(agent_id, None)
    log.info("port released by %s", agent_id)
    return True


def _tell_agent(rec, port):
    """Write the port where the agent can read it. Answer 18.

    A file rather than an environment variable, because the agent is a conversation that outlives
    the process that read the variable. It can read this at any point in its life, and so can a
    tool call on its behalf.
    """
    path = port_path(rec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "agent": rec["id"], "port": port, "url": f"http://127.0.0.1:{port}",
        "assigned": time.time(),
        "note": "The daemon assigned this port. Serve on it, on 127.0.0.1 only. A screen reaches "
                "you through the daemon, never directly.",
    }, indent=2))


# -- starting and stopping -------------------------------------------------------

def _log_file(agent_id):
    logdir = config.VAR / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    return open(logdir / f"server-{agent_id}.log", "a", buffering=1)


def _alive(entry):
    proc = entry.get("proc")
    if proc is not None:
        return proc.poll() is None
    pid = entry.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running(agent_id):
    with _lock:
        entry = _running.get(agent_id)
        return bool(entry and _alive(entry))


def state(agent_id):
    """What the card shows. Cheap enough to call on every state request."""
    with _lock:
        entry = _running.get(agent_id)
        recorded = status.server_state(agent_id) or {}
        return {
            "up": bool(entry and _alive(entry)),
            "port": (entry or {}).get("port") or recorded.get("port"),
            "pid": (entry or {}).get("pid") or ((entry or {}).get("proc").pid
                                                if entry and entry.get("proc") else None),
            "restarts": len(_restarts.get(agent_id) or ()),
            "error": recorded.get("error"),
            "wanted": agent_id in _wanted,
        }


def _wait_ready(proc, port, seconds=None):
    """Wait for something to listen, or for the process to give up first.

    A port either accepts a connection or it does not, and that is the same question every server
    answers, so it needs no cooperation from the agent. A health path would need the agent to write
    one and to agree what it returns.

    Watching the process matters as much as watching the port. A command that dies in twenty
    milliseconds, which is what a missing import does, otherwise costs the full wait every time it
    is retried, and supervision is one loop: five wasted seconds here delay every other server's
    restart. That was measured, not guessed. It made the restart cap take a minute to reach.
    """
    # Read at call time, not bound at import, so a test can shorten it.
    deadline = time.time() + (READY_SECONDS if seconds is None else seconds)
    while time.time() < deadline:
        # Death is checked first. A process that has exited cannot be the thing listening on the
        # port, so a busy port after our own process died means somebody else is on it, and
        # answering "ready" there would point every screen at a stranger's program.
        if proc is not None and proc.poll() is not None:
            return False
        if not _free(port):
            return True
        time.sleep(0.1)
    return False


def ensure(agent_id, why="asked"):
    """Start this agent's server if it has declared one and it is not already up."""
    from . import registry
    rec = registry.get(agent_id)
    if not rec:
        return False
    decl = declaration(rec)
    if not decl or not decl["enabled"]:
        return False
    with _lock:
        _wanted.add(agent_id)
        if running(agent_id):
            return True
    # Written before the start, not after. A server that dies on its third restart at 4am is still
    # a server the operator wants running, and the record has to say so for the next daemon to know.
    status.server(agent_id, wanted=True)
    return _start(rec, decl, why)


def _start(rec, decl, why):
    agent_id = rec["id"]
    try:
        port = port_for(agent_id, decl.get("port"))
    except Exception as exc:
        return _gave_up(agent_id, f"no port: {exc}")
    _tell_agent(rec, port)
    # Recorded at assignment, not at success, so a server that will not start still shows which
    # port it tried. Without that, the card says "down" and nothing says where to look.
    status.server(agent_id, port=port)

    # An orphan from an unclean stop is adopted rather than fought with. Without this, a daemon
    # that was killed leaves a server holding the port, the new daemon cannot bind it, and every
    # screen is blank until somebody notices.
    recorded = status.server_state(agent_id) or {}
    if not _free(port):
        if recorded.get("pid") and _adopt(agent_id, recorded["pid"], port, decl):
            return True
        # Something else has the port and it is not ours. Starting anyway is worse than stopping
        # here: the command would fail to bind and die, while the readiness check would see the
        # foreign program listening and report the server as up. The screen would then be talking to
        # whatever that is. Say what happened instead.
        return _gave_up(agent_id, f"port {port} is held by a program this daemon did not start")

    env = clean_env()
    env["PORT"] = str(port)
    env["PLATFORM_AGENT"] = agent_id
    env["PLATFORM_PORT_FILE"] = str(port_path(rec))
    try:
        # The handle is closed as soon as the child has it. Leaving it open leaks one descriptor per
        # start, which a supervisor retrying a broken command every three seconds turns into
        # thousands. The child keeps its own copy, so its output still lands in the file.
        with _log_file(agent_id) as out:
            proc = subprocess.Popen(decl["command"], cwd=rec["cwd"], env=env,
                                    stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT)
    except Exception as exc:
        return _gave_up(agent_id, f"{decl['command'][0]}: {exc}")

    with _lock:
        _running[agent_id] = {"proc": proc, "pid": proc.pid, "port": port, "started": time.time()}
    log.info("%s server starting on %d, pid %s (%s): %s", agent_id, port, proc.pid, why,
             " ".join(decl["command"]))

    if not _wait_ready(proc, port):
        alive = proc.poll() is None
        if alive:
            # It has to be stopped, not just given up on. A program that starts and never listens,
            # because it is stuck on a lock or a name lookup, would otherwise be left running while
            # the supervisor starts another one beside it: five restarts, five orphans. The test
            # suite found three of them still running after it finished.
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # Alive but not listening is a different fault from dead, and the log file is where the
        # reason is. Saying which it was saves reading the wrong file.
        return _gave_up(agent_id, "started but nothing is listening on the port"
                        if alive else f"exited at once, status {proc.returncode}")

    status.server(agent_id, up=True, port=port, pid=proc.pid, error=None)
    return True


def _adopt(agent_id, pid, port, decl):
    """Take over a server this daemon started before it was restarted."""
    # psutil rather than /proc, because macOS has no /proc. It reads the same command line from
    # the kernel on Linux, so one path is enough and the exactness below is unchanged.
    try:
        import psutil
        cmdline = psutil.Process(pid).cmdline()
    except Exception:
        return False
    # The whole command line has to match, not just the program. `basename(cmdline[0])` alone means
    # "any python3", and after pid reuse that is a different program entirely being adopted as this
    # agent's server.
    #
    # The program itself is compared by its resolved path, not its spelling: macOS reports the
    # interpreter it actually ran (".../Python.app/Contents/MacOS/Python") where Linux reports the
    # argv[0] that was typed ("python3"). Every argument still has to match exactly, so a different
    # script is still a different server.
    import shutil
    wanted = list(decl["command"])
    program = wanted[0]
    if not os.path.isabs(program):
        program = shutil.which(program) or program
    # macOS loses argv[0] entirely: it reports the framework binary the /usr/bin/python3 shim
    # actually ran. So the names are compared as prefixes ("python3" resolves to "Python"), and
    # every argument still has to match exactly.
    got_name = os.path.basename(cmdline[0]).lower()
    want_name = os.path.basename(program).lower()
    same_program = got_name.startswith(want_name) or want_name.startswith(got_name)
    same = (len(cmdline) == len(wanted) and cmdline[1:] == wanted[1:] and same_program)
    if not same:
        return False
    with _lock:
        _running[agent_id] = {"proc": None, "pid": pid, "port": port, "started": time.time()}
    status.server(agent_id, up=True, port=port, pid=pid, error=None)
    log.info("%s server on %d already running as pid %s, adopted", agent_id, port, pid)
    return True


def _gave_up(agent_id, reason):
    """Mark a server down, say why, and tell the phone. Answer 20."""
    with _lock:
        _running.pop(agent_id, None)
    status.server(agent_id, up=False, error=reason)
    log.error("%s server: %s", agent_id, reason)
    telegram.notify(f"{agent_id}'s server will not start.\n{reason}\n"
                    f"Its own log: var/logs/server-{agent_id}.log")
    return False


def stop(agent_id, forget_intent=True):
    """Stop a server on purpose. Not called when an agent sleeps: answer 7 says it keeps running."""
    with _lock:
        entry = _running.pop(agent_id, None)
        if forget_intent:
            _wanted.discard(agent_id)
    # Two different facts. A deliberate stop clears the intent, so a restart leaves it down. The
    # daemon shutting down does not, so a restart brings it back.
    status.server(agent_id, up=False, **({"wanted": False} if forget_intent else {}))
    if not entry:
        return False
    proc, pid = entry.get("proc"), entry.get("pid")
    try:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        elif pid:
            os.kill(pid, 15)
    except Exception as exc:
        log.warning("%s server would not stop: %s", agent_id, exc)
    status.server(agent_id, up=False, pid=None)
    log.info("%s server stopped", agent_id)
    return True


# -- supervision -----------------------------------------------------------------

def _too_often(agent_id):
    now = time.time()
    marks = _restarts.setdefault(agent_id, deque(maxlen=config.SERVER_RESTARTS * 2))
    while marks and now - marks[0] > config.SERVER_WINDOW:
        marks.popleft()
    marks.append(now)
    return len(marks) > config.SERVER_RESTARTS


def _scan():
    from . import registry
    for agent_id in sorted(_wanted):
        if running(agent_id):
            continue
        rec = registry.get(agent_id)
        decl = declaration(rec) if rec else None
        if not rec or not decl or not decl["enabled"]:
            with _lock:
                _wanted.discard(agent_id)
            continue
        if _too_often(agent_id):
            with _lock:
                _wanted.discard(agent_id)
            _gave_up(agent_id, f"died {config.SERVER_RESTARTS} times in "
                               f"{int(config.SERVER_WINDOW)}s, not restarting again")
            continue
        log.warning("%s server is down, restarting it", agent_id)
        _start(rec, decl, "restart")


def _loop():
    while not _stop.is_set():
        try:
            _scan()
        except Exception:
            log.exception("server supervision failed")
        _stop.wait(SCAN_SECONDS)


def restore():
    """Bring back what was up when the daemon last stopped. Answer 19."""
    from . import registry
    back = []
    for agent_id in status.servers_wanted():
        if registry.get(agent_id) and ensure(agent_id, "restored"):
            back.append(agent_id)
    if back:
        log.info("restored server(s): %s", ", ".join(back))
    return back


def follow_broker():
    """Start a server when its agent wakes. Answer 5. Nothing stops one when an agent sleeps."""
    from . import broker

    def heard(event, payload):
        who = (payload or {}).get("agent")
        if event == "agent.awake" and who:
            threading.Thread(target=ensure, args=(who, "its agent woke"),
                             name=f"srv-{who}", daemon=True).start()

    broker.on_change(heard)


def start():
    _stop.clear()
    thread = threading.Thread(target=_loop, name="servers", daemon=True)
    thread.start()
    return thread


def stop_all():
    _stop.set()
    for agent_id in list(_running):
        # The intent is kept, so a restart brings these back. Answer 19 again: what was up before.
        stop(agent_id, forget_intent=False)


def reset():
    """Forget every supervised server without stopping it. For tests."""
    with _lock:
        _running.clear()
        _restarts.clear()
        _wanted.clear()
    _stop.set()
    _stop.clear()
