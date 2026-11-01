"""A fresh run: one scheduled job in a new, short `claude -p` session of its own.

The other kind of job, continuity, asks the agent's main conversation. That costs a wake, and on a
cold cache the wake rereads the whole conversation at the write price. Most jobs never read that
conversation: they check a site, read mail, or write a letter. Those run here instead.

What makes it the same agent is the folder, not the session. CLAUDE.md and the memory files load
by folder, and the tools come from the agent's own MCP config, so the platform still knows which
agent is calling. What makes it cheap is the rest: a small model, no history, and nothing kept
afterwards except the event stream, which goes to var/job-runs/ for anyone who wants to look.

It never asks. `--permission-prompts none` refuses anything that would ask, so a 3am run cannot
sit at a question until morning. The agent's permission mode still decides what is allowed.
"""

import json
import os
import shutil
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from . import clock, config, logs

log = logs.get(__name__)

# The short names an agent may give in add_job, and what they mean today. A full model id is
# accepted as it is, so a new model needs no change here.
MODELS = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}

# No ceiling on one run in dollars by default: a fixed cap cut long runs off halfway. A job still
# ends at its timeout, and the daily quota alert is what tells the owner spending runs ahead. Set PLATFORM_FRESH_BUDGET to bring one back.
BUDGET = os.environ.get("PLATFORM_FRESH_BUDGET", "").strip()


def model_id(name):
    name = (name or "").strip()
    return MODELS.get(name.lower(), name) or MODELS["haiku"]


def runs_dir():
    folder = config.VAR / "job-runs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def read_rules(rec, job):
    """For a watch job, the rules that let the run read the watched folder, and nothing more.

    A fresh run starts in the agent's folder, and a watched folder often sits elsewhere in the
    house, so without these the run may not even list it. Read rules, not --add-dir: an added
    folder takes the agent's permission mode too, and acceptEdits would let the run write there.
    The rules cover the Read tool and read-only commands such as ls and find, seen in a live run.
    The brief tells the run so.

    A watch may also sit in the platform repo, whose var folder holds the keys. So the rules come
    only for a folder inside the house (config.HOME), checked on the real path; any other watch
    runs as before, with the file names in its brief.
    """
    if not (job or {}).get("watch"):
        return []
    from . import watch
    real = watch.base_of(job["watch"]).resolve()
    house = Path(config.HOME).resolve()
    if real != house and house not in real.parents:
        return []
    # The rule syntax marks an absolute path with a leading //.
    return [f"Read(/{real})", f"Read(/{real}/**)"]


def argv_for(rec, model, tools_config, job=None):
    exe = shutil.which(config.CLAUDE_BIN) or config.CLAUDE_BIN
    argv = [exe, "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", model_id(model),
            "--permission-prompts", "none",
            "--no-session-persistence",
            "--mcp-config", str(tools_config),
            # The platform's own tools, pre-approved for the same reason as in broker.argv_for.
            "--allowedTools", "mcp__platform", *read_rules(rec, job)]
    if BUDGET:
        argv += ["--max-budget-usd", BUDGET]
    if rec.get("permission_mode"):
        argv += ["--permission-mode", rec["permission_mode"]]
    return argv


def _kill(proc):
    """The run's own process group, by its pid. Never a pattern."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run(job, prompt, timeout):
    """Run one job fresh, and wait for it. Returns what the scheduler records.

    {"reply", "model", "cost_usd", "tokens", "turns", "file", "error"}. `reply` is None when no
    result came back at all: a crash, a timeout, or a claude that would not start.
    """
    from . import broker, registry
    from .terminal import clean_env

    rec = registry.get(job["agent"])
    if not rec:
        raise KeyError(job["agent"])
    model = model_id(job.get("model"))
    stamp = datetime.fromtimestamp(clock.now()).strftime("%Y%m%d-%H%M%S")
    path = runs_dir() / f"{job['id']}-{stamp}.jsonl"
    err_path = path.with_suffix(".err")
    out = {"reply": None, "model": model, "cost_usd": None, "tokens": None, "turns": None,
           "file": str(path), "error": None}

    env = clean_env()
    env["PLATFORM_AGENT_ID"] = rec["id"]
    tools_config = broker.write_tools_config(rec, {"job": job["id"], "transcript": str(path)})
    argv = argv_for(rec, model, tools_config, job)
    timed_out = threading.Event()
    with open(path, "w") as stream, open(err_path, "w") as err:
        try:
            # A process group of its own, so the timeout ends the run and everything it started,
            # and nothing of the daemon's.
            proc = subprocess.Popen(argv, cwd=rec["cwd"], env=env, start_new_session=True,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                    text=True)
        except OSError as exc:
            out["error"] = f"could not start claude: {exc}"
            return out
        timer = threading.Timer(timeout, lambda: (timed_out.set(), _kill(proc)))
        timer.daemon = True
        timer.start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                stream.write(line)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "result":
                    _take_result(event, out)
                elif event.get("type") == "rate_limit_event":
                    from . import quota
                    quota.seen(event)
            proc.wait()
        except BrokenPipeError:
            proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()

    try:
        if err_path.stat().st_size == 0:
            err_path.unlink()
    except OSError:
        pass
    try:
        tools_config.unlink()
    except OSError:
        pass
    if timed_out.is_set():
        out["error"] = f"timed out after {int(timeout)}s"
    elif out["reply"] is None and not out["error"]:
        tail = ""
        try:
            tail = err_path.read_text()[-300:].strip()
        except OSError:
            pass
        out["error"] = f"claude exited {proc.returncode} with no result" + (f": {tail}" if tail else "")
    log.info("fresh run %s on %s (%s): %s, $%s", job["id"], rec["id"], model,
             out["error"] or "done", out["cost_usd"])
    return out


def _take_result(event, out):
    usage = event.get("usage") or {}
    out["cost_usd"] = event.get("total_cost_usd")
    out["turns"] = event.get("num_turns")
    out["tokens"] = {
        "in": usage.get("input_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "out": usage.get("output_tokens", 0),
    }
    if event.get("is_error") and not event.get("result"):
        # A run stopped by its budget or by an API error ends in a result with no text.
        out["error"] = event.get("subtype") or "error"
        return
    out["reply"] = event.get("result")
