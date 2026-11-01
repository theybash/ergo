#!/usr/bin/env bash
# ./run.sh start | stop | restart | log   Serves the built app (dist/) on 8083 across the tailnet.
here="$(cd "$(dirname "$0")" && pwd)"; pid="$here/.pid"; log="$here/.log"; export PATH=$HOME/.local/bin:$PATH
running() { [ -f "$pid" ] && kill -0 "$(cat "$pid")" 2>/dev/null; }
case "${1:-start}" in
  start)   running && { echo "already up $(cat "$pid")"; exit 0; }
           cd "$here" && setsid python3 serve.py >>"$log" 2>&1 </dev/null & echo $! >"$pid"; sleep 2
           running && echo "react mock up  ->  http://localhost:8083" || { echo failed; tail -5 "$log"; exit 1; } ;;
  stop)    running && kill -- -"$(cat "$pid")" 2>/dev/null || kill "$(cat "$pid")" 2>/dev/null; rm -f "$pid"; echo stopped ;;
  restart) "$0" stop; "$0" start ;;
  log)     tail -30 "$log" ;;
esac
