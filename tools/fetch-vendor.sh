#!/usr/bin/env bash
# Fetch the browser code the console needs, into web/vendor.
#
# Vendored rather than loaded from a content delivery network, because the console has to work
# with no internet: the laptop is the whole system, and a terminal that will not draw because a
# third party is down is a bad way to find that out. Not committed, because pinned copies of
# other people's code in a repository age badly.

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p web/vendor

XTERM=5.5.0
FIT=0.10.0
LINKS=0.11.0

get() { echo "  $2"; curl -fsSL "$1" -o "web/vendor/$2"; }

echo "fetching into web/vendor"
get "https://cdn.jsdelivr.net/npm/@xterm/xterm@$XTERM/lib/xterm.js"            xterm.js
get "https://cdn.jsdelivr.net/npm/@xterm/xterm@$XTERM/css/xterm.css"           xterm.css
get "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@$FIT/lib/addon-fit.js"      addon-fit.js
get "https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@$LINKS/lib/addon-web-links.js" addon-web-links.js

echo
du -h web/vendor/* | sed 's/^/  /'
