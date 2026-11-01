#!/usr/bin/env python3
"""Start the daemon.

    python3 run.py

Then publish it to the tailnet, which is what supplies both the name and the certificate:

    tailscale serve --bg --https 443 http://127.0.0.1:8080

Never `tailscale funnel`. That is the sibling command that publishes to the open internet, and
it would remove the only thing holding the door shut.

One process, and this file is the list of what is inside it: the console it serves, the terminals
it owns, the schedule, the bridges, and the supervision of the servers agents write. That was
decided against splitting it in two, on 18 August. A split costs a versioned interface, retries
across a network hop and boot ordering, and buys crash isolation that timeouts and threads mostly
provide. Timeouts before supervisors: split it when something actually hangs.
"""

import os
import signal
import threading

from commandcenter import (broker, config, cron, inbox, logs, server, servers, status, telegram,
                           watch)


def main():
    # Before anything else, so a failure during startup lands in the file rather than nowhere.
    logs.setup()
    log = logs.get("daemon")

    httpd = server.serve()
    stop = threading.Event()

    # The process writes its own pid. A wrapper script cannot: setsid forks, so the pid the
    # shell sees belongs to a process that has already gone, and every later stop signals
    # nothing. That cost twenty minutes of "address already in use".
    pidfile = os.environ.get("PLATFORM_PIDFILE")
    if pidfile:
        with open(pidfile, "w") as fh:
            fh.write(str(os.getpid()))

    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    from commandcenter import sampler
    sampler.install()

    log.info("daemon up on http://%s:%s", config.BIND, config.PORT)
    log.info("state %s, views %s, logs %s", config.VAR, config.VIEWS, logs.current_file())
    # Which frontend, every time. PLATFORM_WEB is set outside the process, so after a restart the
    # log is the only thing that says whether the old console or the React one came up.
    log.info("frontend %s", config.WEB)
    log.info("door: tailnet only%s", " plus a token" if config.TOKEN else "")

    # Two ears on the broker's announcements, attached before anything can wake. The first writes
    # down what is awake so a restart can put it back; the second starts an agent's own server when
    # the agent wakes, which is answer 5 of the daemon grill.
    status.follow_broker()
    servers.follow_broker()

    # The accept loop runs in its own thread and the main thread waits for a signal. The obvious
    # arrangement, serving in the main thread and shutting down from the handler, deadlocks:
    # shutdown() waits for the serving loop to finish, and the loop cannot finish because its
    # thread is inside the handler. The symptom is a process that ignores Ctrl-C and holds the
    # port, which cost an hour of "address already in use" before it was found.
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()

    # The bridge first, because everything below it reports through the bridge. A missed job with
    # nowhere to say so is the failure this whole ordering exists to avoid.
    telegram.start()

    # Then the last configuration, answer 19. Servers before agents: a server outlives its agent's
    # terminal, so the screens are right even while the agents are still loading their
    # conversations back.
    servers.start()
    servers.restore()
    broker.restore()

    # Say what was missed while the laptop was shut, and run none of it. Answer 1, and the one
    # decision here that a normal scheduler gets the other way round.
    cron.catch_up()
    cron.start()
    # Watch jobs arm again. A change made while the daemon was down is not replayed.
    watch.start()
    # The inbox's two pings, the morning brief and a "now" item, looked at every half minute.
    inbox.start()

    stop.wait()

    log.info("stopping")
    # Said before anything is closed. Everything after this is the shutdown, and the record has to
    # tell the difference: an agent slept on the way out is still an agent that was meant to be up.
    status.stopping()
    cron.stop()
    watch.stop()
    inbox.stop()
    telegram.stop()
    # The servers stop with the daemon, because they are its children and systemd sweeps the whole
    # group anyway. The record of what was wanted survives, so the next start brings them back.
    servers.stop_all()
    # Agents are put to sleep rather than left running. Their conversations are on disk, so
    # sleeping loses nothing, and a terminal with nobody reading it is unreachable.
    broker.shutdown_all()
    httpd.shutdown()
    httpd.server_close()
    log.info("stopped")


if __name__ == "__main__":
    main()
