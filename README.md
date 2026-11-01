# Ergo

Ergo runs a small team of Claude Code agents on your Linux laptop, and lets you work with them
from your phone.

Each agent is one Claude Code conversation in its own folder. Ergo keeps them running, wakes them
on a schedule, lets them hand work to each other, and shows them all in one console: in a browser
on the laptop, or in the Ergo app on your Android phone. Agents can post to a feed, put tasks on
your list, ring an alarm on your phone, or call you.

**Linux only for now.** Mac and Windows are not supported yet.

## What you need

- Linux with systemd (tested on Ubuntu 24.04).
- Python 3.12 or later, with `psutil`, `ptyprocess` and `Pillow`. On Ubuntu or Debian:
  `sudo apt install python3-psutil python3-ptyprocess python3-pil`.
- Node.js 20 or later, to build the web app.
- [Claude Code](https://docs.claude.com/claude-code), installed and signed in. Your agents run on
  your own Claude account.
- [Tailscale](https://tailscale.com), on the laptop and on your phone, to reach Ergo from the phone.

## Install

```bash
git clone <this repository> ergo-app
cd ergo-app
./install.sh
```

The script checks what you have, builds the web app, and starts Ergo as a systemd user service.
It never uses sudo. When something is missing, it stops and tells you the command to run.

Then open `http://127.0.0.1:8080` on the laptop.

The steps, if you would rather do them yourself:

1. `cd web-react && npm ci && npm run build && cd ..`
2. `./install.sh --no-service` writes `ergo-run.sh` with your settings.
3. `./ergo-run.sh` runs Ergo in the foreground.

To keep Ergo running after you log out: `sudo loginctl enable-linger $USER`.

### Settings

`install.sh` reads these from the environment. All are optional.

| Variable | Default | What it does |
|---|---|---|
| `ERGO_PORT` | `8080` | The port the console listens on. |
| `ERGO_VAR` | `<repo>/var` | Where Ergo keeps its state: agents, jobs, the feed, logs. |
| `ERGO_OWNER` | none | Your name. Agents say it when they talk about you. Without it they say "the owner". |

Ergo looks for agent folders under `ERGO_HOME` (default `~/ergo`). Keys for the optional parts go
in `<ERGO_VAR>/secrets.env`, one `NAME=value` per line:

| Variable | For |
|---|---|
| `OPENAI_API_KEY` | Voice calls, voice notes and dictation. |
| `PLATFORM_TELEGRAM_TOKEN`, `PLATFORM_TELEGRAM_OPERATOR` | Talk to an agent over Telegram. The operator is your own Telegram user id. |
| `PLATFORM_GMAIL_TOKEN`, `PLATFORM_MAIL_FROM` | Agents draft email for you to send. |
| `TYPESAFE_API_KEY` | Better recall in voice calls. |

`docs/INTERNALS.md` lists every other setting.

## Who can reach it

There is no login screen. Ergo answers two kinds of address only: this laptop itself, and your
Tailscale network (100.64.0.0/10). Anyone else gets a 403. Being on your tailnet is the
authentication, so keep your tailnet to your own devices.

`install.sh` binds Ergo to 127.0.0.1. To reach it from your phone, publish it to your tailnet:

```bash
tailscale serve --bg --https 443 http://127.0.0.1:8080
```

Your laptop's address is then `https://<laptop name>.<your tailnet>.ts.net`.

**Never use `tailscale funnel` for Ergo.** Funnel publishes to the whole internet.

## Your phone

The Ergo app for Android is in `android/`. It has no address built in. On the first run it asks
for your laptop's address (the `ts.net` address above). To change it later, long-press the app
icon and choose "Change address", or use the button on the page that shows when the laptop
cannot be reached.

### Build the app

You need the Android SDK and JDK 21.

```bash
cd android
npm ci
npx cap sync android
cd android
ANDROID_HOME=~/Android/Sdk ./gradlew assembleDebug
```

The APK is at `android/android/app/build/outputs/apk/debug/app-debug.apk`. Install it with
`adb install` or copy it to your phone.

To bake your laptop's address into a build, add `-PdaemonUrl=https://my-laptop.my-tailnet.ts.net`.

### A release build, signed with your own key

Make a key once, and keep it out of the repository:

```bash
keytool -genkeypair -keystore ~/ergo.keystore -storetype PKCS12 -alias ergo \
  -keyalg RSA -keysize 4096 -validity 10000
```

Put these four lines in `~/.gradle/gradle.properties`, or set them as environment variables:

```
ERGO_STORE_FILE=/home/you/ergo.keystore
ERGO_STORE_PASSWORD=...
ERGO_KEY_ALIAS=ergo
ERGO_KEY_PASSWORD=...
```

Then `cd android && ./release.sh`. It writes `out/ergo.apk`. Without a key, it stops and says so.

## Your first agent

Make a folder for the agent, for example `~/ergo/helper`, and put a `CLAUDE.md` in it that says
what the agent is for. In the console, press **+** on the Fleet screen, give it a name and that
folder. Then open its chat and write to it.

Ergo starts every agent with Claude Code's permission mode `bypassPermissions` by default, so an
agent can run commands in its folder without asking. Change it per agent in the console, or for
all agents with `PLATFORM_PERMISSION_MODE=acceptEdits`.

## Tests

```bash
./test.sh
```

No packages beyond the three above. The suite never starts a real Claude Code, and never reaches
Telegram, OpenAI or your phone.

## More

- `docs/INTERNALS.md`: how the daemon and the console work.
- `DESIGN.md`: the interface rules.
- `UBIQUITOUS_LANGUAGE.md`: the words, each with one meaning.
- `web-react/API.md`: the HTTP API.

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE`.
