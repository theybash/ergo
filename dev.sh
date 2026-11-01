#!/usr/bin/env bash
# Start, stop or restart the console for development.
#
#   ./dev.sh start | stop | restart | log
#
# PLATFORM_WEB chooses the frontend and nothing else does. Unset it for the old console, or
#   PLATFORM_WEB=web-react/dist ./dev.sh restart
# for the React one. The variable is inherited by run.py, and var/secrets.env can hold it too, so
# going back is the same command without the prefix.
#
# It keeps a pid file rather than searching the process table by name. Searching by name is what
# it looks like it should do, and it does not work: the shell running the search has the pattern
# in its own command line, so a name search finds the searcher and kills that instead.

# Since 2026-09-23 the daemon runs as the user service `commandcenter` and starts at boot. Use
#   systemctl --user restart commandcenter
# rather than this script, or the two fight over port 8080.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
pidfile="${PLATFORM_PIDFILE:-$here/var/dev.pid}"
logfile="${PLATFORM_LOGFILE:-$here/var/dev.log}"
mkdir -p "$(dirname "$pidfile")"

running() { [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; }

stop() {
    if running; then
        pid="$(cat "$pidfile")"
        # Negative pid is the process group, so the agents' terminals go too rather than being
        # orphaned with nobody left to read them.
        kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do running || break; sleep 0.3; done
        running && kill -KILL "-$pid" 2>/dev/null
        echo "stopped $pid"
    else
        echo "not running"
    fi
    rm -f "$pidfile"
}

start() {
    running && { echo "already running as $(cat "$pidfile")"; exit 0; }
    rm -f "$pidfile"
    # setsid gives the console its own process group, which is what makes the group kill above
    # safe: it cannot reach anything that was not started by this script. The pid is written by
    # run.py rather than captured here, because setsid forks and the pid this shell sees belongs
    # to a process that has already exited.
    # The systemd unit reads var/secrets.env through EnvironmentFile, so this path has to do the
    # same or the bridges only work as a service. set -a exports everything the file defines, which
    # is what the daemon reads them from.
    if [ -f "$here/var/secrets.env" ]; then
        set -a
        . "$here/var/secrets.env"
        set +a
    fi
    cd "$here" && PLATFORM_PIDFILE="$pidfile" setsid python3 run.py >>"$logfile" 2>&1 </dev/null &
    for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$pidfile" ] && break; sleep 0.3; done
    running && echo "started $(cat "$pidfile")  ->  http://127.0.0.1:${PLATFORM_PORT:-8080}" \
            || { echo "failed to start; last lines:"; tail -20 "$logfile"; exit 1; }
}

case "${1:-restart}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    log)     tail -f "$logfile" ;;
    *)       echo "usage: $0 start|stop|restart|log" >&2; exit 2 ;;
esac
