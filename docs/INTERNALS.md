# Ergo internals

How the daemon and the console work, for someone changing the code. To install Ergo, read the
README at the top of the repository.

The interface rules are in `DESIGN.md`. The words are in `UBIQUITOUS_LANGUAGE.md`.

The vocabulary file is not pedantry. Agents write their own screens and read these documents, so a
word with two meanings becomes two implementations by two agents on one page.

---

## Run it

Needs Python 3.12 and three packages: `pip install -r requirements.txt` (psutil, ptyprocess,
Pillow). The old console needs no build step and no Node. Ergo, the React app, is built with
Node: `cd web-react && npm install && npm run build`, then start with `PLATFORM_WEB=web-react/dist`.

```bash
python3 run.py                 # http://127.0.0.1:8080
./dev.sh start|stop|restart|log
```

Publish it to the tailnet, which supplies both the name and the certificate:

```bash
tailscale serve --bg --https 443 http://127.0.0.1:8080
```

**Never run `tailscale funnel` on this.** That is the sibling command that publishes to the open
internet, and it removes the only thing holding the door shut.

### The door

There is no login screen. Membership of the tailnet is the authentication. The server reads the
connection's address from the socket, never from a header, and answers only the tailnet ranges
and this machine. `PLATFORM_TOKEN` adds a bearer token as well, for running the console while off
the tailnet.

This is stronger than a password and weaker in one specific way worth naming: anyone holding an
unlocked device already on the tailnet is already inside.

### Settings

| Variable | Default | What it does |
|---|---|---|
| `PLATFORM_PORT` | 8080 | |
| `PLATFORM_BIND` | 0.0.0.0 | |
| `PLATFORM_TOKEN` | empty | optional bearer token, checked as well as the tailnet |
| `PLATFORM_VAR` | `./var` | agent list, cords, transfer log |
| `PLATFORM_VIEWS` | `./views` | agent-authored screens |
| `PLATFORM_PERMISSION_MODE` | `acceptEdits` | what a new agent is allowed to do unasked |
| `PLATFORM_CLAUDE_BIN` | `claude` | |
| `PLATFORM_TELEGRAM_TOKEN` | empty | the bot token. Without it the bridge stays off |
| `PLATFORM_TELEGRAM_OPERATOR` | empty | the one Telegram user id that is answered |
| `PLATFORM_WAKE_AT_ONCE` | 4 | how many agents a schedule may wake at the same moment |
| `PLATFORM_REMOTE_CONTROL` | 1 | register each agent with Remote Control, so it reaches your phone |
| `PLATFORM_JOB_TIMEOUT` | 900 | seconds before a job is given up on |

The two Telegram values belong in `var/secrets.env`, which git ignores and which the systemd unit
reads. A token in the unit file is a token in the deploy directory.

---

## How it works

```
browser ──┬── HTTP  JSON            making and changing things
          ├── SSE   /api/events     "something changed", plus health every 2s
          └── WS    /ws/agent/<id>  one terminal, bytes both ways
                          │
                       broker ── owns every terminal
                          │
                    Claude Code, one process per awake agent
```

**Two channels per agent, and the split is the important part.**

Input goes through the pseudo-terminal, because that is the only way to put keystrokes into a
full-screen terminal interface. Output comes back the same way, unchanged, and the browser draws
it with a terminal emulator. Token streaming needs no special work: the interface already prints
each token as it arrives, so forwarding its output is forwarding tokens. Measured at 25 frames
over 4.6 seconds for a short reply, about 110ms apart.

Facts come from the session file Claude Code writes to `~/.claude/projects/<folder>/<id>.jsonl`.
That file is flushed at turn boundaries, so it can never stream, but it holds the conversation
already structured. The console reads it for cost, token counts, and the exact text of the last
message. Terminal for the eyes, session file for the ledger.

**An agent is a session id, not a process.** New agents start with `--session-id <uuid>`, so the
console decides what the conversation is called before anything runs. Waking uses
`--resume <uuid>`, chosen by whether the conversation exists on disk. Verified by planting a code
word, sleeping the agent, waking it, and asking for the word back: different process id, same
conversation.

**Liveness is free.** The broker owns each terminal, so a dead process closes its terminal, the
reading thread sees the end of the stream, and the card goes grey in the same instant. Nothing
polls and nothing is asked of the agent.

---

## The console

**Dashboard.** Machine health top right. One card per agent: a state dot, what it last said, turn
count, what it has cost, and buttons for chat, terminal, sleep or wake, cord, and hide.

**Chat writes a message from the dashboard.** It does not open the agent page, because answering
without leaving the list is the point of a button on a card. Terminal opens the full view.

**Agent page.** Two tabs. `Agent view` is whatever the agent built for itself, and it opens by
default. `Terminal` is the live terminal, and it connects the first time you look at it rather than
when the page opens, so reading a view does not attach to and resize somebody else's terminal. An
agent with no view opens on the terminal.

Green means the terminal is open. Grey means it is closed and can be resumed. Working is shown by
motion rather than colour, so movement always means something is happening.

Hiding an agent removes the card. It does not kill the process and does not touch the
conversation, because closing a card by accident must not destroy work.

**A message to a sleeping agent is held, not delivered, and does not wake it.** Waking costs memory
and about ten seconds, and grey exists so an agent can sit there costing nothing, so paying that to
leave a note would make the quick message pointless. Held messages are kept in `var/mailbox.json`,
so they survive a restart of the console, and the card shows how many are waiting. When the agent
next wakes they arrive as one prompt with the age of each note. A single held message is delivered
as itself, with no framing: wrapping one note in an explanation of the delivery mechanism tells the
agent about the platform instead of what was asked.

Cords are the exception and pass `wake=true`, because a cord needs an answer and a held prompt would
leave it waiting for ever.

**An undelivered message can be taken back.** The count on the card is a button: it lists what is
waiting, with the age of each, and deletes one or all of them. Deleting one needs no confirmation
because retyping it costs nothing; deleting all of them asks first.

Each held message carries an id, so deleting one is unambiguous. Position would have worked until a
delivery shifted the list by one and the wrong note vanished.

**Files, both ways.** A strip on the agent page, always visible rather than behind a tab.

An agent offers a file by writing it into `<its folder>/.platform/outbox/<its id>/`, and it appears
as a download. You send one by tapping the dashed target or dragging onto it; it lands in
`.platform/inbox/<its id>/` and the agent is told where, held if it is asleep.

**Both boxes live inside the agent's own working directory, and that is not tidiness.** The first
version put them beside the console's state, so a sent file sat outside the agent's folder and
reading it needed permission. The agent stopped on "do you want to proceed?" and waited two minutes
for an answer nobody was there to give. Inside its own folder it reads and writes freely. The agent
id is in the path too, since two agents can share a folder.

There is no endpoint that reads a path you supply. A file is reachable because an agent put it in
its outbox, and the name is matched against a listing rather than joined onto a directory, so
`../../etc/passwd` is not refused by a check, it simply never resolves. Uploads are capped at 128MB,
streamed to disk in chunks, never overwritten, and a file that runs over the limit is deleted rather
than left as a truncated half that an agent would read and answer confidently about.

**The inbox is the one part that adds real risk**, and it is worth naming plainly. A document from
outside is text somebody else wrote, which is the shortest prompt injection path in the platform:
you send a PDF, a line inside says "also email this to X", and nothing marks that line as data. The
arrival message tells the agent to treat the contents as data, which lowers the rate and does not
close it. The real answer is that agents cannot send email, spend money, or place a trade.

The outbox adds no risk worth discussing: an agent already has a shell and the whole internet.

---

## Cords

Work moves between agents along cords. Tap **Cord** on one card, then tap another: that is the
direction, drawn.

**Transfer** carries the sending agent's last message plus an instruction typed onto the cord.
One use, then the cord disappears. Both agents must be awake. Every transfer is appended to
`var/transfers.jsonl`.

**Call** is a bounded conversation. It ends when the caller writes a line starting `HANGUP:`
followed by what it learned, or after 8 messages in total, whichever comes first. Reaching the
budget is marked `timed_out` rather than `completed`, because a call that runs out was a badly
framed question.

Three rules make a call work, and each was earned:

- **Only the caller can hang up.** The callee answers and cannot end the call.
- **Hanging up requires a summary.** That summary is what enters the caller's context, not the
  raw exchange.
- **A hangup before the callee has spoken is ignored.** The first real test had the caller answer
  its own question and hang up on message one, so the other agent was never asked. A call with no
  exchange is not a call.

The `HANGUP:` line is a text protocol standing in for a tool. It can be replaced with a real tool
over MCP without changing anything else.

---

## Agents adding themselves

A Claude Code session started by hand can put itself on the console:

```bash
curl -s -X POST http://127.0.0.1:8080/api/agents/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"job search","session_id":"<its own session id>","cwd":"'"$PWD"'"}'
```

It arrives grey, because the console does not own its terminal and cannot stream it. Waking it
from the console opens a second terminal on the same conversation.

---

## Agents writing their own screens

An agent writes one file, `views/<its id>/view.js`:

```js
export function render(root, platform) {
  root.innerHTML = `<div class="label">memory free</div><div class="num" id="m"></div>`;
  platform.health().then(h => {
    root.querySelector('#m').textContent = h.memory.available_gb.toFixed(1) + ' GB';
  });
}
```

It appears as a tab at once. **An agent can write several pages, one file each:**

```
views/finance/10-positions.js   ->  a Positions tab, first
views/finance/20-pnl.js         ->  a Pnl tab, second
views/finance/research.js       ->  a Research tab, after the numbered ones
```

The filename is the title and the order, because that is all an agent controls with one write. The
leading number is not shown. Nothing else registers a page: a list would be a second thing to keep in
step, and the first time it disagreed with the files a tab would point at nothing.

**Terminal is the leftmost tab**, so it is always in the same place. The agent's pages follow it.

**The agent chooses which page opens**, by writing a slug into `views/<id>/default`:

```bash
echo positions > views/finance/default
```

With no such file the first page opens, and with no pages the terminal does. A name that matches
nothing falls back instead of breaking.

**Twenty-four pages are shown and the strip scrolls.** The cap exists because an agent writes files,
so an agent in a loop can write five hundred. Anything beyond it is reported on the page and in the
log rather than dropped quietly.

The platform owns the tabs and the agent owns each page. Letting each agent draw its own navigation
inside its frame was cheaper and was rejected, since that is ten agents inventing ten navigations on
one screen.

`examples/voice-latency.js` and `examples/voice-cost.js` are two pages for one agent, deliberately
different in density: one large state against a table. They should not look like siblings, only like
they come from the same place.

**Tell the agent to read `DESIGN.md` first, every time.** Ten agents with their own taste on one
page look like ten products, and each one drifts toward the same generic default. Use the CSS
tokens and never a literal colour: the console may change the palette and the view has to follow
without being edited.

### Giving a screen its numbers

A screen has no network. It asks the daemon by name, and the daemon works out where that name lives.
**Two ways, and most screens want the first.**

**Write a file.** The agent writes into its own folder:

```bash
mkdir -p .platform/data
echo '{"rows": [...], "updated_at": 1787000000}' > .platform/data/positions.json
```

```js
export function render(root, platform) {
  platform.data('positions').then(got => {
    root.textContent = `${got.value.rows.length} rows, ${Math.round(got.age)}s old`;
  });
}
```

`.json`, `.txt`, `.csv` and `.md` are read, up to four megabytes. No port, no process, nothing to
crash, and **it keeps working while the agent sleeps**, which is what most screens need.

**Or run a server**, for data that has to be live. The agent declares one in its own folder:

```json
// .platform/server.json
{ "command": ["python3", "api.py"], "note": "positions, live from the stockbroker" }
```

The daemon assigns a port, writes it to `.platform/port.json`, starts the command with `PORT` in its
environment, restarts it if it dies, and keeps it running while its agent sleeps. Serve `GET
/data/<name>` and answer 404 for names you do not have: the daemon falls back to the file, so an
agent can have both and the screen never learns which answered.

Five restarts in two minutes and it gives up, sends a Telegram, and stops trying. Its own output is
in `var/logs/server-<id>.log`.

**Every answer says how old it is.** `updated_at` and `age`, always. A file's age comes from the
file. A server's comes from the server if its JSON carries `updated_at`, because the server knows and
the clock does not: data fetched this second can still be an hour old upstream.

**And a screen can ask the daemon to fetch any address.**

```js
const got = await platform.fetch('https://api.example.com/quote?s=INFY');
```

Ten seconds and five megabytes, and neither limit is about trust. It sends nothing of the operator's:
no cookies, no token, no invented headers. What it does send is what the screen asked for, because a
screen is its own agent's code and a key in it is that agent's key.

This makes the laptop a proxy for anything the laptop can reach, which includes the tailnet and so
the operator's other devices. That is said here rather than left as a surprise.

### Why the view is locked

That code runs in the operator's browser, so there are two separate controls for two separate
threats. There were three, and the third went with the capability broker on 19 August.

**A sandbox with no same-origin permission stops reading.** The frame gets an origin that matches
nothing, so it cannot read the console and no view can read another. `allow-scripts` and
`allow-same-origin` are never combined, because a frame with both can remove its own sandbox.

**A content security policy stops calling out.** Sandboxing governs the document, not the
network. `connect-src 'none'`.

**What is deliberately not a control any more:** the daemon does not decide which addresses a
screen may reach. It will fetch any of them, which is answer 30 of the daemon grill. The two rules
above are about the browser, not about trust in the agent: they stop a stranger's code reading this
page and talking to its own server, and neither of them is weakened by the daemon fetching on the
screen's behalf, because the daemon attaches nothing of the operator's when it does.

Two consequences of that isolation, both deliberate:

- The agent's module is inlined into the wrapper page rather than fetched by it, because a
  document with an opaque origin cannot load a module script from this host.
- The typefaces are inlined as data, for the same reason. About 90KB per view page, which buys a
  view that looks like the rest of the console instead of bolted on.

This does not stop a view showing something wrong. A confused agent can draw a wrong number and
the frame will render it faithfully. That is a data problem, not a browser problem.

---

## The schedule

One job names one agent, a task, and either a time of day or an interval.

```bash
curl -s localhost:8080/api/jobs -H 'content-type: application/json' \
  -d '{"agent":"finance","task":"check the pipeline and report","at":"07:00"}'
```

The agent is woken when it is due, given the task, and **put back to sleep afterwards unless it was
already awake.** An agent that was up when the job fired stays up.

It ends by writing a line that is exactly `task succeeded` or `task failed`. Nothing else reads that
line. Without one the run is recorded as `unclear`, which is deliberately different from `failed`:
the job ran, and whether it worked is unknown.

**A job that was due while the laptop was shut is never run late.** It is reported as missed, with an
offer to run it now, from the page or from Telegram. Whether a late run still makes sense is a
judgement the platform cannot make. The same is true of a job interrupted by a restart: it did not
finish, so it is missed rather than done.

**Four agents wake at once.** Each costs 400 to 700 MB, so five jobs at 07:00 means four run and the
fifth waits for a slot. Daily jobs get a few seconds of jitter, derived from the job id, so they do
not all fire in one tick and race each other for the slots.

Success is reported as well as failure, because the answer is usually the point of the job. Pass
`{"quiet": true}` and only failures reach the phone.

---

## Telegram

Set two values in `var/secrets.env`, which git ignores:

```
PLATFORM_TELEGRAM_TOKEN=123456:abc...
PLATFORM_TELEGRAM_OPERATOR=<your numeric user id>
```

`dev.sh` loads that file too, so the bridge works whether the daemon is started by hand or by
systemd. Without it the bridge only ran as a service.

Polling, not webhooks, so nothing needs exposing. **Only that user id is answered**; anything else is
logged and dropped.

Anything typed goes to the master, always, and wakes it if it is asleep. Commands: `/agents`,
`/wake <id>`, `/sleep <id>`, `/jobs`, `/rerun <job>`, `/health`, `/help`.

Written against the HTTP interface directly, because there is no `pip` on this laptop and this would
be the project's only dependency. Long replies are cut at four thousand characters on a paragraph
boundary. A reply that Telegram will not parse as markdown is sent as plain text rather than lost. A
dead network is waited out, with the wait growing to thirty seconds.

---

## The chat tab

Built for a phone first, and deliberately asymmetric. **Only your own messages get a bubble.** What
the agent says is the page, not a card on it, because one side is a line or two and the other is four
paragraphs, and a bubble drawn around four paragraphs is a box around the thing you came to read.

There is no character cap on that prose. A 74 character measure was there first, on the usual
readability argument, and the operator asked twice for the text to run wider: it is his to decide,
since he is the one reading it. An agent's page is also wider than the dashboard, 1560 against 1180,
because a conversation wants the window and a grid of cards does not.

The bubble grey comes from the same stone scale as the rest of the console, so the theme carries it.

A tool call is one dim line, tappable for the rest. Nearly three quarters of a session's characters
are tool output, so it earns a line and not a card: 20 pixels rather than 40 and a border.

The input is one pill. A keyboard icon inside it opens the five keys a phone cannot give you: shift
Tab to cycle the permission mode, the two arrows, return and escape. Everything a dialog needs is
also a tap on the option itself.

**On an agent's page the top bar is that agent's header.** Back, name, path, and the agent's own
buttons. The row that used to hold all of that is gone, and the health readings step aside, because
they are a dashboard thing and the dashboard is one tap away. The file row appears only when the agent
has actually offered you a file; sending one is a button in the bar.

That is 111 pixels back on a laptop, where the chat log went from 382 to 638, and 217 on a phone,
where the tab used to begin 307 pixels down the screen and now begins at 90. A phone drops the path
and the buttons too, since neither is something you use there.

**The chat opens by default on a phone**, whatever the agent chose. A forty column terminal is not
somewhere you arrive willingly, and a screen an agent wrote is a considered choice rather than a
greeting.

**The chat tab measures its own room.** A constant in the stylesheet was wrong on one device or the
other: the stack above it comes to 258px on a laptop and 307px on a phone, where the breadcrumb
wraps. So the height is set from the element's own measured top, on open, on resize, and when the
visual viewport changes, which is what happens when a phone hides its address bar without firing a
resize. While the chat is open only the log scrolls, so the box you type into cannot be pushed off
the bottom.

Beside Terminal, a chat view of the same conversation. It reads from two places, because neither is
enough alone.

**History from the session file.** Claude Code's own record, so it holds every message and every tool
call, and it survives a restart. Tool calls arrive as their own rows, folded shut, because nearly
three quarters of a session's characters are tool output and showing it open buries the conversation.

**The message still arriving, from the terminal's screen.** The file gets an answer in one write at
the end of the turn: a 350 word reply landed 19 seconds after the question. The screen changes every
tenth of a second. So the last message is read out of the terminal's own grid, shown dimmed, and
replaced by the record once the turn lands.

It is the same live session the Terminal tab shows, so either can drive it and nothing diverges.

**It opens at the bottom and asks for older on demand.** Thirty rows to start, then sixty more each
time you scroll to the top, up to two hundred, keeping the line you were reading in place. And two
clocks rather than one: the arriving message is redrawn three times a second because it is local and
costs one element, while the record is fetched every two seconds because each call reads the tail of
a file. Rebuilding the whole list on every tick also threw away scroll position, text selection and
every tool row you had opened.

**A waiting question is read off the screen, not the record.** This was wrong here for a while and
the correction matters. Claude Code writes an `AskUserQuestion` call into the session file only when
the turn completes, and a waiting question suspends the turn, so the record holds the question exactly
when it is no longer any use. Checked against a real one: the question text appeared nowhere in a 1115
line session file while it sat on screen. Answered questions do appear, and the chat draws those from
the record too.

So the waiting one comes from the terminal's own screen, anchored on the dialog's footer, the line
that says how to answer it, and bounded above by the box's own checkbox header. Two things learned
reading it: the dialog has a rule between its own last two options, so stopping at the first rule
finds only "Chat about this"; and scanning past the header swallows the agent's previous sentence into
the question.

**The options are buttons.** Tapping one sends that option's number as a keypress, which Claude Code
takes as both the move and the confirmation: tested, and the agent read back the option that was
tapped. Walking there with the arrow keys was the first attempt and it is not safe, because the
"Type something" row draws its own `❯` prompt, so the marker on screen cannot be trusted to mean the
selection.

**A key bar, because a phone keyboard has no arrows.** Up, down, Enter, Esc, Tab and shift-Tab go
straight to the terminal as keypresses, which is what a dialog is waiting for. Sending text would
type into the dialog instead of answering it. That answers a multiple choice question and a
yes-or-no permission prompt alike. Permission prompts are the one thing not in the record: only the
mode is written there, never the prompt, so the keys are the only way to answer those.

Two honest limits. The live tail depends on Claude Code's own layout markers, a bullet at the start
of an agent message and two-space indents under it, so a redesign of their interface breaks the tail
until the markers are updated. History is unaffected. And the tail is only as long as the terminal's
own scrollback holds.

---

## Your agents on your phone

Every agent the daemon wakes registers itself with Remote Control, named by its id, so it appears in
the Code tab of your Claude app and at `claude.ai/code`. The conversation syncs both ways: send from
the terminal, the console, the phone or a browser, interchangeably.

**This is the one thing the platform cannot build for itself.** Remote Control runs inside the
session, so it holds each message before anything draws it. The console stands outside with a
pseudo-terminal, a fake screen and keyboard, so it can only read what was drawn. Measured: 80% of
those bytes are cursor moves, and the text arrives as cell updates at coordinates rather than as a
stream. Reading it back means keeping a screen, which is why the terminal tab uses xterm.js.

Set `PLATFORM_REMOTE_CONTROL=0` to turn it off. It signs in with your own claude.ai account, and the
sessions appear only there.

---

## The tools an agent has

Every agent gets three, and the master gets six more. They arrive as an MCP server the daemon starts
for each agent, `commandcenter/tools.py`, written against JSON-RPC over stdin and stdout because
there is no pip here.

| Tool | Who | What |
|---|---|---|
| `list_agents` | every agent | who exists, awake or asleep, and what each last said |
| `transfer_to` | every agent | hand your last message to another agent with an instruction |
| `call_agent` | every agent | a bounded conversation, ended with a `HANGUP:` summary |
| `wake`, `sleep`, `stop` | the master | drive another agent's terminal |
| `message` | the master | send a message, held by default if the agent is asleep |
| `read_agent` | the master | the last few things said in another agent's conversation |
| `create_agent` | the master | start a new agent in a folder, creating the folder if it is missing |
| `agents_health` | the master | memory, battery, disk, before waking several agents |

The split is usefulness, not permission. Any agent can already reach the same HTTP interface with
curl, because there are no walls between agents here.

`create_agent` is the one that deliberately differs from the console: the console refuses a folder
that does not exist, because that catches a typed path before an agent starts somewhere nobody meant,
while the tool creates it and says so in the answer. A tool that refused would only mean the caller
runs `mkdir` and tries again.

`--allowedTools mcp__platform` pre-approves these and nothing else, so the master does not stop for a
keypress on every call. Writing a file and running a command still ask, which is what `acceptEdits`
means.

---

## Network health

Pairs, never single numbers, because one figure is not a diagnosis. 90ms out with 1ms to the router
means the line is slow; 90ms to both means the router is the problem.

| Pair | Says |
|---|---|
| gateway latency beside internet latency | which hop is hurting |
| link rate beside signal strength | a weak signal or a busy channel |
| down beside up | which direction is capped |

Plus the wifi retry counter from `/proc/net/wireless`, which climbs before an access point gives up,
and disconnections since boot from NetworkManager's own log.

The free measurements are taken every twenty seconds and shared with every watcher. **The speed test
is a button**, because it moves five megabytes over the connection it is measuring. It uses
Cloudflare, which refuses a bare urllib request and answers a browser-shaped user agent, so that
header is load bearing rather than decoration.

---

## Tests

```
./test.sh              everything, about 40 seconds
./test.sh cron         one file
./test.sh -v           with each test named
```

154 tests, standard library `unittest`, no dependencies. Three seams make them possible:
`config.use_var()` gives each test its own state directory, `reset()` on each module clears what it
holds in memory, and `clock.py` lets a test pin the time, because waiting until 03:00 to find out
what happens at 03:00 is not a test.

Agents are faked at one seam, `cords.ask` and the three broker calls, so a test can prove an agent
was woken and put back without paying 700 MB for it. Servers are real processes, because every
interesting failure there is about processes: one that exits at once, one that runs but never
listens, one left behind by an unclean stop.

The suite was checked by breaking the code on purpose eleven times and confirming the right test
noticed each break. That found one test which only looked strong: `reset()` rebuilt the semaphore the
mutation had changed, so the cap appeared tested when it was not.

---

## API

| | |
|---|---|
| `GET /api/state` | agents, cords, jobs, health, limits |
| `GET /api/events` | server-sent events: `agents`, `cords`, `jobs`, `health` |
| `GET /api/health` | one health snapshot |
| `GET /api/whoami` | which tailnet member is calling |
| `POST /api/agents` | `{name, cwd, model?, permission_mode?, engine?, start?}`. engine is `claude` (default) or `codex`; a codex agent does not start a terminal unless start is true. A personal agent is refused codex, because Codex signs in with the work ChatGPT account, unless the daemon has PLATFORM_CODEX_PERSONAL=1 |
| `POST /api/agents/register` | `{session_id, name?, cwd?}` |
| `GET /api/agents/<id>` | one agent |
| `PATCH /api/agents/<id>` | name, model, schedule, note, permission_mode |
| `POST /api/agents/<id>/wake` `sleep` `stop` `hide` `show` `forget` | |
| `GET /api/agents/<id>/held` | undelivered messages, with ids and times |
| `POST /api/agents/<id>/unhold` | `{id}` deletes one, no body deletes all |
| `POST /api/agents/<id>/message` | `{text, wake?}`. Holds by default; returns `sent` or `held` |
| `DELETE /api/agents/<id>` | hides it |
| `GET /ws/agent/<id>?rows=&cols=&wake=` | the terminal |
| `GET /api/cords`, `POST /api/cords` | `{kind, from, to, instruction, budget?}` |
| `POST /api/cords/<id>/hangup` | `{summary}` |
| `POST /api/agents/<id>/server` | starts the agent's declared server. `{up:false}` stops it |
| `GET /api/agents/<id>/data` | what its screens can ask for, and whether a server is up |
| `GET /api/data/<id>/<name>` | one published thing. The daemon picks the server or the file |
| `POST /api/fetch` | `{url, method?, headers?, body?, timeout?}`. Any address |
| `GET /api/jobs`, `POST /api/jobs` | `{agent, task, at?, every?, timeout?, quiet?}` |
| `PATCH /api/jobs/<id>` | task, at, every, enabled, timeout, quiet |
| `POST /api/jobs/<id>/run` | run it now, off schedule |
| `DELETE /api/jobs/<id>` | remove it |
| `GET /view/<id>/` | the wrapper page for an agent's screen |

On the terminal socket, binary frames are terminal bytes in both directions. Text frames are JSON
control: `{type:"resize",rows,cols}`, `{type:"input",data}`, `{type:"submit",text}`,
`{type:"interrupt"}`.

`submit` exists next to a live terminal for one reason: a long message pasted into a terminal
interface gets submitted at its first newline. The server sends it as one bracketed paste
followed by a return, 140ms later, so it arrives whole.

---

## Cost

The console reports token counts always, and dollars only if `var/prices.json` exists:

```json
{ "claude-opus-5": { "input": 15, "output": 75, "cache_write": 18.75, "cache_read": 1.5 },
  "default":       { "input": 3,  "output": 15, "cache_write": 3.75,  "cache_read": 0.3 } }
```

Dollars per million tokens, per model id. Without the file the card shows context tokens, because
a token count is a fact and a guessed price is a number that looks like one.

---

## Things worth knowing before they bite

**A new folder asks whether you trust it.** Claude Code shows a one-off question the first time it
runs anywhere, and the answer is a keypress. You will see it in the terminal; press return.
Nothing here answers it for you: writing to `~/.claude.json` means editing a file that every
running Claude Code process rewrites, and parsing the screen does not work reliably because the
interface positions the cursor instead of writing spaces, so stripping escape sequences glues the
words together.

**The environment is scrubbed before an agent starts.** Everything beginning with `CLAUDE_` is
removed. If the console is started from inside a Claude Code session, an unscrubbed agent
inherits a marker that switches its session file off, which silently costs you cost, last
message, and the ability to resume. Two other inherited variables are a socket and a token for
the parent session's message bus.

**One terminal, one size.** Several people can watch one agent. The terminal is fitted to the
smallest attached screen, so a phone shows everything and a wide display has space at the edge.
The other way round wraps the phone's text into nonsense. Scrollback replayed to a new watcher
was drawn at whatever size the terminal had then, so old lines can look short. New frames are
correct.

**The terminal's colours are the application's own.** 1:1 means the console does not restyle what
Claude Code draws, so `DESIGN.md`'s ban on purple does not reach inside the terminal box. The
sixteen basic colours are mapped to the palette; anything using 256-colour or true-colour codes
comes through as the application chose.

**Sleeping kills the process group.** Claude Code starts helpers and can outlive a hangup signal,
so a plain terminate leaves those helpers running.

**An agent set to another Unix user cannot be started from here.** With `unix_user` set it needs a
password the console does not have, so start it by hand in that user's session and let it register
itself. Nothing uses this today: the PA was going to and that was reversed on 18 August, in favour of
every agent running as the operator. The mechanism stays because the kernel is the only wall left
that actually works.

**An agent's server outlives its agent.** Sleeping an agent does not stop the server it declared,
deliberately, so a grey agent's screen still has numbers. Stopping one is always something somebody
asked for. They do stop when the daemon stops, and the daemon starts them again when it comes back.

**A missed job stays missed.** Nothing catches up. Opening the laptop at nine does not start six
agents on work that was due at three, and the alert says how many runs were missed rather than
sending one message per occurrence.

---

## Tested

Verified by running it, not by reading it:

- terminal streams live, 25 frames over 4.6s, about 110ms apart
- terminal size matches the browser exactly, 31 rows by 148 columns
- sleep then wake resumes the same conversation, proven with a planted code word
- token and cost accounting read from the session file, with real numbers
- transfer cord carried one agent's answer to another, which acted on it
- call cord: asked, heard back, hung up with a summary, 3 of 8 messages
- three notes held for a sleeping agent survived a console restart, then arrived merged on wake:
  the agent replied with both planted words, so it read all three in one prompt
- deleting the middle one of three held messages removed exactly that one, an unknown id reported
  nothing deleted, and the remaining ids survived a restart
- agent view renders sandboxed, reaches the platform by name, follows the theme
- two pages for one agent: tabs Terminal, Latency, Cost, with Cost opened because the agent asked
  for it; switching swaps the frame and keeps exactly one
- an unknown page name is a 404, a traversal attempt is refused, a bad `default` falls back
- thirty page files on one agent showed twenty-four with "6 more" on the strip and a line in the log
- moving between agents unmounts the previous view and drops its socket: verified across five
  round trips, one frame and zero stray watchers each time
- files both ways: a sent brief was read without a permission prompt and answered in 13 seconds;
  140MB was refused with 413 and left nothing behind; a name carrying `../` was flattened; three
  traversal attempts on download resolved to nothing
- log rotation deletes the eighth day, checked by planting ten dated files and asking the handler
  which it would remove: the three oldest, keeping seven
- a scheduled job on a real agent: due at 60 seconds, the agent woke at 69, answered, said
  `task succeeded`, and was asleep again at 77. Recorded as succeeded, and it had been grey before
- the same job run by hand against an agent that was already awake left it awake
- a job due at 03:00 for two days with the daemon down was reported as missed twice and run zero
  times; a job planted as in flight was reported as interrupted; three further restarts reported
  nothing, because the check remembers how far it has looked
- four of five jobs ran at once and the fifth waited, with a stubbed reply rather than five real
  agents: this proves the cap, not that five sessions fit in memory
- an agent's server was started, killed with -9, and restarted by the supervisor within three
  seconds; a command that exits at once gave up after five tries in sixteen seconds and recorded
  why; a command that runs but never listens was called out as exactly that
- a daemon restart brought back the server that was running, on the same port, as a new pid
- data by name: the server answered for `positions`, the file answered for `notes` which the
  server does not have, and with the server stopped the file answered for both, saying its age.
  Half-written json is reported rather than drawn as nothing
- fetching for a screen: example.com in 26ms, an 8MB body cut to 5MB with `truncated` set, a host
  that never answers abandoned in 2s, a 429 handed back as an answer, a `Cookie` header dropped
  while the screen's own header was kept, and `file:///etc/passwd` refused
- the console page was loaded in headless Chrome through the DevTools protocol, which is the only
  way to execute its JavaScript on this laptop: no node, no test runner. The app ran, the card
  rendered with `SERVER up on 8100`, the schedule row read back correctly, the Schedule dialog
  opened with the agent listed, and there were zero console errors or exceptions
- Run now, clicked in that browser, woke a grey agent, ran the job, recorded what the agent said,
  and left it grey again. Its server stayed up throughout, which is the point of the server
  outliving the terminal
- forgetting an agent released its port, stopped its server, deleted its jobs and cleared its row
- 223 tests pass in 44 seconds, and twelve deliberate breakages were each caught by the test that
  should have caught them
- `claude agents --json` drives the working flag: an agent sitting at a prompt reads `idle` and one
  thinking reads `busy`, both checked against a real session
- network health on a quiet evening: 3ms to the router, 14ms out, 270 Mb/s at 70%, 0 drops, and a
  speed test from the browser measuring 28 down and 47 up
- a real transfer cord carried one agent's words to another, which replied, and the wire was drawn
  over the grid in the completed colour with no console errors
- work, demo and personal in a real browser: a personal-tagged agent vanished from work view, the
  page said one card was hidden, and its own address still opened it
- an agent that was awake before a daemon restart was woken again by the restart, which is answer 19
  and which did not work until the record learned to tell a shutdown from a decision
- the Telegram bridge against a stub: a reply Telegram would not parse as markdown arrived as
  plain text on the second attempt, 9000 characters became three pieces, an identical second send
  was skipped, a message from a stranger's user id was dropped, and six seconds of connection
  refused did not kill the thread, which answered as soon as the stub came up

Every slice in `PLAN.md` is built. What is left is not code: the systemd unit is written and not
installed, and the Telegram bridge has never spoken to Telegram, because both need the operator.

Not in the plan and still not here: WhatsApp, which was a separate Node program in v1; separating the
PA into its own Unix user, which was dropped for simplicity; dragging cards into a patchbay layout;
and an amber middle band on the health thresholds.
