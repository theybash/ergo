# src/data

Everything the React console asks the daemon, and everything the daemon tells it. One fetch
helper and four hooks. No screen calls `fetch`, and no screen opens a socket.

The reference implementation is `web/app.js`, the vanilla console that has been running on 8080.
Every rule here is ported from it, including the ones that look odd, because each odd one is an
incident that already happened once.

## api.js

`api(path, method = 'GET', body)` sends JSON and reads JSON back. On a non 2xx it throws an
`Error` carrying the daemon's own `error` string, which is the sentence the operator should read.
Above it sit named wrappers for every route the console uses: `state`, `agent`, `messages`,
`send`, `setMode`, `answer`, `setPermissionMode`, `wake`, `sleep`, `stop`, `hide`, `show`,
`forget`, `held`, `unhold`, `createAgent`, `updateAgent`, `jobs`, `createJob`, `runJob`,
`updateJob`, `deleteJob`, `cords`, `createCord`, `hangup`, `unfile`, `setServer`, `network`,
`speedTest`, `health`, the recruit screen's three: `recruits`, `bringBack`, `adoptSession`,
and the voice front desk's six: `voiceSession`, `voiceOffer`, `voiceTool`,
`voiceLog`, `voiceCalls`, `voiceEnd`. There is a default export with all of them on it, so a
screen can write `import cc from '../../data/api'` and keep its own `state` and `send`.

Three of them are address builders, not calls: `socketUrl(id, opts)`, `fileUrl(id, name)` and
`eventsUrl()`.

Same origin by default, because the daemon serves the built app. Set `VITE_CC_API` to point a
`vite dev` server at the live daemon.

## useFleet()

The whole fleet, kept current. It fetches `/api/state` on mount, then listens on `/api/events`.
The daemon's stream says what changed rather than sending the change, so `agents`, `cords` and
`jobs` each mean refetch. `health` is the exception: it carries its whole payload and lands
without a request. Reconnect is the browser's own, and `onerror` says nothing, because a red
line about a stream that is already reconnecting is noise.

| field | what it holds |
|---|---|
| `agents` | every visible agent, the daemon's `agent_view` shape |
| `jobs` | every cron job |
| `cords` | live cords between agents |
| `health` | the latest machine snapshot, from the stream or the last state fetch |
| `limits` | the thresholds health is judged against |
| `hidden` | ids of hidden agents, ids only |
| `views` | the view tags in use across agents, sorted |
| `loading` | true until the first state fetch finishes or fails |
| `error` | the last failure as a string, or null |
| `refresh` | fetch state again now |

`views` is not the view you are looking at. The daemon stores a view tag per agent and nothing
else, because the operator said the current view is browser state.

## useAgentSocket(id, { rows, cols, wake })

One WebSocket on `/ws/agent/<id>`, shared by every mounted consumer of the same agent. Text
frames carrying `t` are the chat engine, text frames carrying `type` are the terminal's own
notices, and binary frames are raw terminal bytes which go only to whoever registered for them.

| field | what it holds |
|---|---|
| `mode` | `chat`, `terminal` or `off` |
| `state` | `idle` or `thinking` |
| `permMode` | the permission mode the daemon reports, or an empty string when unknown |
| `live` | the whole reply so far, not a piece to append |
| `tools` | `{name, summary}` for each tool call in this turn |
| `asks` | pending ask frames, `perm` or `question`, deduped by id |
| `echo` | `{text, at}` for your last message, or null |
| `notes` | `{text, at}`, the daemon talking to the operator |
| `settledTick` | goes up once per settled turn |
| `terminalNotices` | the last ten of `connecting`, `attached`, `grey`, `exited`, `error`, `detached` |
| `send(bytes)` | keystrokes, as binary |
| `resize(rows, cols)` | tell the daemon the terminal's new size |
| `subscribeBinary(fn)` | receive terminal bytes, returns an unsubscribe |

The chat asks for 46 rows by 150 columns. A terminal is fitted to its smallest watcher, so
asking large never shrinks anybody else's screen.

Sharing is keyed by agent and by `wake`. `wake: true` is the terminal tab asking the daemon to
start a terminal, and the chat asks `wake: false` so that reading yesterday's conversation never
wakes a sleeping agent. Two chats on one agent share one socket. A chat and a terminal on one
agent do not, and must not, because they are asking the daemon for different things.

If the socket closes while a consumer is mounted, it reopens after 1.5 s. A phone that sleeps
loses its socket without saying so. The daemon resends mode, state, permission mode, the turn in
flight and every pending ask on each attach, so the hook clears `live`, `tools` and `asks` when
the socket opens and rebuilds them from frames alone.

If no `settled` frame arrives, the turn settles itself after 8 s. That fallback exists because a
reply that lost its last frame used to sit above its own settled twin, the same words twice.

## useHistory(id, settledTick)

The settled conversation, read from the agent's session file.

| field | what it holds |
|---|---|
| `rows` | the messages, each with a stable `key`, oldest first |
| `loadOlder()` | ask for 60 more, up to 200 |
| `hasMore` | true when there is more file to ask for |
| `refresh()` | fetch now, merge it into the list |

It fetches 30 first. Every answer after that is merged into what is on screen, never put in its
place. The daemon sends the last n rows of the file, and a working agent adds a row for every tool
call and every result, so each answer starts further down the file. Rows on screen that an answer
no longer reaches back to stay, and the next fetch asks for as many rows as are on screen, up to
200. A row's key is built from the row itself (kind, who, tool, id, time, length), never from its
place in the answer, so a row keeps its key and its object for as long as it is on screen. A
changed `settledTick` fetches again, because the last live reply is now a settled row. A slow
poll ticks every 2 s and fetches only when the last read is older than 8 s, because each call
reads the tail of a file on disk. An answer that lands after a newer one was drawn is dropped.

Row shapes come from the daemon: `{kind: 'text', who, at, text}`, `{kind: 'tool', who, at, tool,
id, input, inputRaw?, answer?, refused?}` and `{kind: 'result', who, at, id, error, text}`.
`inputRaw` comes with a question or a show card. `answer` is his pick on a choice card, and
`refused` marks a show card the tool turned down.

## echo.js

`useEcho(echo, rows)` returns the message to draw under the conversation, or null. The daemon
answers a send with an `echo` frame at once and the session file catches up a second later.
Without the echo, the message you typed went nowhere for that second and looked lost. Nothing is
stored and nothing is cleared: the answer is computed from the socket's echo and the history rows
on every render, so the echo stops being returned the moment the record holds the same words.
`echoLanded(rows, echo)` is the test on its own.

## The five things a screen must do

1. **Send on submit.** `await cc.send(id, text, true)` from the composer, then let the echo frame
   draw it. Do not add the message to the list yourself.
2. **Refetch on settled.** Pass the socket's `settledTick` into `useHistory`. Nothing else makes
   a finished turn appear in the record.
3. **Drop the echo when it lands.** Draw `useEcho(sock.echo, history.rows)` and nothing else.
   Drawing `sock.echo` directly shows the message twice.
4. **Resize the terminal.** Call `resize(rows, cols)` from the fit observer, and again when the
   `attached` notice arrives. A terminal drawing at the wrong width is a column of fragments.
5. **Clean up.** Nothing to do, as long as the hooks are called at the top of a component that
   unmounts. Unsubscribe whatever `subscribeBinary` returned inside your own effect cleanup.
