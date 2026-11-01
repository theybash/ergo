#!/usr/bin/env bash
# Install Ergo on a Linux laptop.
#
#   ./install.sh                 check, build the web app, install and start a systemd user service
#   ./install.sh --no-service    check and build only; then run it yourself with ./ergo-run.sh
#
# Settings, all optional, read from the environment:
#   ERGO_PORT   the port the console listens on (default 8080)
#   ERGO_VAR    where Ergo keeps its state (default <repo>/var)
#   ERGO_OWNER  your name, used when agents talk about you (default "the owner")
#
# It never uses sudo. When something needs root, it stops and prints the command for you to run.

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
port="${ERGO_PORT:-8080}"
var="${ERGO_VAR:-$here/var}"
owner="${ERGO_OWNER:-}"
service=1
[ "${1:-}" = "--no-service" ] && service=0

say() { printf '%s\n' "$*"; }
die() { printf 'ergo install: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "Linux only for now."

# Python 3.12 or later, and three packages.
command -v python3 >/dev/null || die "python3 is missing. Install Python 3.12 or later."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
    || die "Python 3.12 or later is needed; this is $(python3 -V)."
if ! python3 -c 'import psutil, ptyprocess, PIL' 2>/dev/null; then
    say "Python packages missing. Trying pip --user."
    if python3 -m pip --version >/dev/null 2>&1; then
        python3 -m pip install --user -r "$here/requirements.txt" \
            || die "pip failed. On Ubuntu or Debian: sudo apt install python3-psutil python3-ptyprocess python3-pil"
    else
        die "No pip. On Ubuntu or Debian: sudo apt install python3-psutil python3-ptyprocess python3-pil"
    fi
fi

# Node 20 or later, to build the web app.
command -v npm >/dev/null || die "Node.js 20 or later is needed to build the web app (npm not found)."
node -e 'process.exit(+process.versions.node.split(".")[0] >= 20 ? 0 : 1)' \
    || die "Node.js 20 or later is needed; this is $(node -v)."

# Claude Code runs the agents. Ergo starts without it, but no agent can answer.
if ! command -v claude >/dev/null; then
    say "Warning: the claude command is not on PATH. Install Claude Code and sign in, or agents cannot run."
fi

say "Building the web app."
(cd "$here/web-react" && npm ci --no-audit --no-fund && npm run build) >/dev/null

mkdir -p "$var"

# One file that holds the settings, so the service and a manual run agree.
cat > "$here/ergo-run.sh" <<EOF
#!/usr/bin/env bash
# Written by install.sh. Starts Ergo in the foreground.
cd "$here"
export PLATFORM_PORT="$port"
export PLATFORM_BIND=127.0.0.1
export PLATFORM_VAR="$var"
export PLATFORM_WEB=web-react/dist
${owner:+export PLATFORM_OWNER="$owner"}
export PYTHONUNBUFFERED=1
[ -f "$var/secrets.env" ] && set -a && . "$var/secrets.env" && set +a
exec python3 "$here/run.py"
EOF
chmod +x "$here/ergo-run.sh"

if [ "$service" = 1 ]; then
    command -v systemctl >/dev/null || die "No systemd. Run ./ergo-run.sh yourself."
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$unit_dir"
    cat > "$unit_dir/ergo.service" <<EOF
[Unit]
Description=Ergo, a console for Claude Code agents
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$here/ergo-run.sh
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now ergo.service
    say "Ergo is running as a user service: systemctl --user status ergo"
    say "To keep it running after you log out: sudo loginctl enable-linger $USER"
else
    say "Built. Start it with: $here/ergo-run.sh"
fi

say "Open http://127.0.0.1:$port on this laptop."
say "To reach it from your phone, see 'Your phone' in README.md."
