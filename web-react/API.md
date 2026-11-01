# API.md

The contract between the command center daemon and its browser UI. Read off
`commandcenter/server.py`, `broker.py`, `chat.py`, `transcript.py`, `registry.py`, `cron.py`,
`cords.py`, `network.py`, `files.py`, `data.py`, `fetch.py`, `health.py`, and `web/app.js`, with
live GET responses captured from the daemon on port 8080 on 2026-09-19.

Everything here is fact from the code. Where the code is ambiguous, the document says so.

---

## 1. Orientation

One daemon, one port. It serves the static page, the JSON API, one server-sent event stream, and
one WebSocket per agent.

- **REST over HTTP** does every action and every read. Actions are `POST` to
  `/api/agents/<id>/<action>`. Reads are `GET /api/state` and friends.
- **One WebSocket per agent** at `/ws/agent/<id>` carries two things on the same socket. Binary
  frames are raw pseudo-terminal bytes in both directions. Text frames are JSON: terminal notices
  carry a `type` key, chat frames carry a `t` key.
- **One SSE stream** at `/api/events` says "something changed". It never carries the new state.
  The client refetches `/api/state` when it hears `agents`, `cords` or `jobs`.

The door is the tailnet. `Handler.allowed()` reads the peer address from the socket. A caller off
the tailnet may send `Authorization: Bearer <token>` when `config.TOKEN` is set. Every failure is
`403 {"error": "not on the tailnet"}`. `/fonts/*` and `/vendor/*` are answered before the door.

---

## 2. The agent record

One object per agent. `GET /api/state` returns a list of them under `agents`.
`GET /api/agents/<id>` returns exactly one of them. Both are built by `server.agent_view(rec)`,
so the shape is identical.

### 2.1 Stored fields

These come from `registry.py` and live in `agents.json`. They survive a daemon restart.

| field | type | meaning | live? |
|---|---|---|---|
| `id` | string | The slug. Matches `^[a-z0-9][a-z0-9-]{0,31}$`. Never changes. | static |
| `name` | string | Display name. Editable with `PATCH`. | static |
| `session_id` | string | UUID of the Claude Code conversation. The agent's real identity. | static |
| `cwd` | string | Absolute working directory. Editable with `PATCH`, but only while the agent is asleep. | changes |
| `model` | string or null | Passed to `claude --model` untouched. null means the CLI default. | static |
| `role` | string or null | `"master"`, `"pa"`, or null. At most one master exists. | static |
| `unix_user` | string or null | Set only for an agent quarantined as another Unix user. Non null means the daemon refuses to start it. | static |
| `permission_mode` | string | What the record says. `acceptEdits`, `plan`, `bypassPermissions`, or an older value such as `default`. | changes |
| `created` | float | Unix seconds. | static |
| `last_activity` | float | Unix seconds. Touched on every message and wake. Sorts the list. | changes |
| `last_seen` | null | Declared and never written. Reserved for heartbeats from other machines. | static |
| `hidden` | bool | Hidden agents are left out of `state.agents`. | changes |
| `schedule` | string or null | A cron expression. Stored, editable, and not read by `cron.py`. | static |
| `views` | array or null | Subset of `["work","demo","personal"]`. null means all three. | changes |
| `self_registered` | bool | True when the agent added itself with `POST /api/agents/register`. | static |
| `note` | string or null | One line the agent may write about itself. | static |
| `engine` | string | `claude` or `codex`. Set when the agent is made. A codex agent runs `codex exec` per chat turn and never sends `ask` frames. | static |
| `codex_thread` | string or null | The Codex conversation id, written after the first turn. | changes |

### 2.2 Computed fields

These are worked out on every request. None of them are stored.

| field | type | source | meaning | live? |
|---|---|---|---|---|
| `state` | `"green"` or `"grey"` | `broker.state` | green when either engine is alive. There is no third state. | changes |
| `mode` | `"chat"`, `"terminal"`, `"off"` | `broker.mode` | Which engine runs. Held in memory only, so a daemon restart puts every agent back to `off`. | changes |
| `working` | bool | `live.working` | Asked of `claude agents --json` by session id. Falls back to the running engine's own guess. | changes |
| `pid` | int or null | terminal only | The pseudo-terminal process. **null when the chat engine is the one running.** | changes |
| `watchers` | int | terminal only | Open WebSockets attached to the terminal. 0 when no terminal. | changes |
| `size` | `[rows, cols]` or null | terminal only | The terminal's size, which is its smallest attached screen. | changes |
| `turns` | int | `transcript.summary` | Assistant text messages in the session file, sidechains excluded. | changes |
| `last_message` | string | `transcript` then `for_display` | The agent's last words. Trimmed to 2000 characters. `HANGUP:` lines and `**` are stripped for display only. | changes |
| `last_message_at` | string or null | session file | ISO 8601 with `Z`, for example `"2026-09-19T01:33:48.629Z"`. Not a Unix float. | changes |
| `tokens` | object | session file | `{input, output, cache_write, cache_read}`, all ints, summed over the whole conversation. | changes |
| `context_tokens` | int | derived | `input + cache_write + cache_read`. This is a lifetime bill, **not** the size of the context window. | changes |
| `cost_usd` | float or null | `config.prices()` | null when no price list exists, which is the case on this laptop today. | changes |
| `pages` | array | `config.pages` | Screens the agent wrote. See below. | changes |
| `default_page` | string or null | `config.default_page` | Slug the agent asked to open. null when it has no pages. | changes |
| `files` | array | `files.listing(rec,"outbox")` | Files the agent offered. `[{name, bytes, modified}]`, newest first. `modified` is Unix seconds. | changes |
| `inbox_count` | int | `files.listing(rec,"inbox")` | How many files were sent to it. The list itself is not exposed. | changes |
| `resumable` | bool | `transcript.session_file` | True when a session file exists on disk. | changes |
| `waiting` | int | `mailbox.waiting` | Undelivered held messages. Fetch the text with `/held`. | changes |
| `server` | object | `servers.state` | The agent's own server. See below. | changes |
| `data` | array | `data.listing` | Published data files. `[{name, file, bytes, updated_at, age}]`. `name` is the stem, `file` the filename. | changes |

`pages` entry:

```json
{ "slug": "tasks", "title": "Tasks", "order": 10, "file": "10-tasks.js" }
```

`slug` `"view"` is special: it gets `title: "Agent view"` and `order: -1`, so it sorts first. If
the agent wrote more than `config.MAX_PAGES` (24) pages, the last entry carries an extra
`"truncated": <total>` key and the rest are cut.

`server` object:

```json
{ "up": true, "port": 8100, "pid": 2590, "restarts": 0, "error": null, "wanted": true }
```

### 2.3 A real agent, captured live

```json
{
  "id": "personal-assistant",
  "name": "Personal Assistant",
  "session_id": "ce756955-5daf-4a47-be05-21878d90401e",
  "cwd": "/home/user/agents/personal-assistant",
  "model": "opus",
  "role": "master",
  "unix_user": null,
  "permission_mode": "bypassPermissions",
  "created": 1787137687.9528298,
  "last_activity": 1789801509.1345947,
  "last_seen": null,
  "hidden": false,
  "schedule": null,
  "views": null,
  "self_registered": false,
  "note": null,
  "state": "green",
  "mode": "terminal",
  "working": false,
  "pid": 2603,
  "watchers": 0,
  "size": [32, 120],
  "turns": 9,
  "last_message": "Letters shipped at 07:09. ...",
  "last_message_at": "2026-09-19T01:33:48.629Z",
  "tokens": { "input": 186, "output": 52305, "cache_write": 553611, "cache_read": 8718388 },
  "context_tokens": 9272185,
  "cost_usd": null,
  "pages": [
    { "slug": "tasks", "title": "Tasks", "order": 10, "file": "10-tasks.js" },
    { "slug": "week",  "title": "Week",  "order": 20, "file": "20-week.js" }
  ],
  "default_page": "tasks",
  "files": [],
  "inbox_count": 0,
  "resumable": true,
  "waiting": 0,
  "server": { "up": true, "port": 8100, "pid": 2590, "restarts": 0, "error": null, "wanted": true },
  "data": [
    { "name": "tasks", "file": "tasks.json", "bytes": 4594,
      "updated_at": 1787378052.976802, "age": 2429959.9 }
  ]
}
```

---

## 3. REST routes

### 3.1 Errors, everywhere

Every error body is `{"error": "<text>"}`. The UI's `api()` helper throws `new Error(data.error)`.

| code | when |
|---|---|
| 400 | Bad input. Raised by `fail(msg)` with no code, and by `KeyError` or `ValueError` inside a POST. |
| 403 | Off the tailnet with no token. Also `PermissionError`, which is the quarantined agent refusing to start. |
| 404 | No such agent, job, cord, file, page or action. Also any unmatched path. |
| 409 | `RuntimeError` inside a POST. In practice: a cord to a sleeping agent. |
| 413 | An upload over 128 MB. |
| 500 | Anything else. The daemon prints the traceback. |
| 502 | `POST /api/fetch` could not reach the address. |
| 503 | Something the daemon talks to has no key. The voice front desk answers this way. |

Two non errors that look like errors:

- `POST /api/agents` may answer `200 {"needs_dir": true, "path": "..."}`. That is a question, not
  a failure.
- `POST /api/agents/<id>/answer` may answer `200 {"ok": false, "error": "no such ask"}`. The UI
  treats `ok === false` as a failure and puts the text in the notes box.

### 3.2 State and events

| method | path | body | returns |
|---|---|---|---|
| GET | `/api/state` | none | the whole console state |
| GET | `/api/health` | none | `health.snapshot()` |
| GET | `/api/whoami` | none | `{addr, user, device}` |
| GET | `/api/events` | none | `text/event-stream`, never closes |

`GET /api/state`:

```json
{
  "agents": [ /* agent records, hidden ones left out, master first then by last_activity */ ],
  "hidden": ["scratch"],
  "cords": [],
  "jobs": [ /* job records */ ],
  "health": { /* see below */ },
  "limits": { "mem_avail_gb": 1.5, "battery_pct": 20, "disk_free_pct": 10, "pressure": 1.0 },
  "server_time": 1789808012.8457892
}
```

`hidden` is a list of ids only, not records. `server_time` is Unix seconds, and the UI uses it to
age cords without worrying about browser clock skew.

The UI calls `/api/state` on load and on every `agents`, `cords` or `jobs` event.

`health.snapshot()`, from `healthd.py`, cached for one second:

```json
{
  "ts": 1789808012.099675,
  "host": "my-laptop",
  "uptime_s": 6823,
  "cpu": { "cores": 12, "percent": 3.8, "load": [1.21, 0.73, 0.52] },
  "memory": { "total_gb": 15.19, "available_gb": 12.33, "used_gb": 2.86,
              "percent": 18.8, "swap_total_gb": 16.0, "swap_used_gb": 0.0 },
  "pressure": { "cpu": 0.0, "memory": 0.0, "io": 0.0 },
  "temps_c": { "acpitz": 27.8, "Package id 0": 41.0 },
  "battery": { "percent": 100.0, "plugged": true, "minutes_left": null },
  "disks": [ { "mount": "/", "fstype": "ext4", "total_gb": 130.4,
               "used_gb": 41.6, "free_gb": 82.1, "percent": 33.6 } ],
  "network": { "tailscale_ip": "100.64.0.1", "lan_ip": "192.168.1.20",
               "interfaces": [ { "name": "wlan0", "ip": "192.168.1.20" } ] },
  "sessions": [ { "id": "c1", "user": "lightdm" } ]
}
```

`limits` in `/api/state` says where a number turns bad. The UI compares
`health.memory.available_gb` against `limits.mem_avail_gb`, and so on.

`GET /api/whoami` returns `{"addr": "127.0.0.1", "user": null, "device": null}` on loopback. On
the tailnet, `user` is the login name and `device` the short hostname, from `tailscale whois`.
The current UI does not call it.

### 3.3 Agents

| method | path | body | returns | when the UI calls it |
|---|---|---|---|---|
| GET | `/api/agents/<id>` | none | one agent record | never today, state carries them |
| GET | `/api/agents/<id>/messages?n=<n>` | none | `{messages: [...]}` | chat history, on mount and on `settled` |
| GET | `/api/agents/<id>/held` | none | `{held: [...]}` | when the undelivered dialog opens |
| GET | `/api/agents/<id>/data` | none | `{files: [...], server: {...}}` | `platform.available` from a screen |
| POST | `/api/agents` | see below | the new record, 201 | the new agent dialog |
| POST | `/api/agents/register` | `{session_id, name?, cwd?, model?}` | the record, 201 or 200 | never, agents call it |
| PATCH | `/api/agents/<id>` | fields | the updated record | model picker, "where it shows" |
| DELETE | `/api/agents/<id>` | none | `{"hidden": true}` | never, the UI posts `/hide` |
| PUT | `/api/agents/<id>/inbox/<name>` | raw bytes | `{ok, path, bytes, told_agent}`, 201 | file upload |

**There is no `GET /api/agents`.** It returns `404 {"error": "not found"}`. Confirmed live. The
list of agents comes only from `/api/state`.

`POST /api/agents` body:

| key | type | required | note |
|---|---|---|---|
| `name` | string | yes | Slugged into the id. |
| `cwd` | string | yes in practice | Run through `config.resolve_dir`. |
| `model` | string or null | no | |
| `permission_mode` | string or null | no | Defaults to `config.DEFAULT_PERMISSION_MODE`, which is `acceptEdits`. **Not validated here.** |
| `role` | string or null | no | The UI does not send it. A second `"master"` is refused with 400. |
| `unix_user` | string or null | no | The UI does not send it. |
| `create_dir` | bool | no | Answer to the `needs_dir` question. |
| `start` | bool | no | Defaults to true. A quarantined agent is never started. |

If the folder is missing and `create_dir` is absent, the answer is `200 {"needs_dir": true,
"path": "/the/full/path"}`. The UI shows the path in a `confirm()` and posts again with
`create_dir: true`.

`PATCH /api/agents/<id>` accepts only `name`, `model`, `schedule`, `note`, `permission_mode`,
`views`, `role`, `cwd`. Anything else is silently dropped by the server before `registry.update`
sees it.

`cwd` moves an agent, and it has its own rules because a folder change is not like the others.

| rule | answer |
|---|---|
| The agent must be asleep. | 409 `{"error": "put the agent to sleep before you move it"}` |
| The path must be absolute. A leading `~` is expanded. | 400 `{"error": "give a full path, one that starts with / or ~"}` |
| The folder must exist, or you say to make it. | 200 `{"needs_dir": true, "path": "/the/full/path"}`, then send again with `create_dir: true` |

**The daemon moves no files.** It records the new folder and nothing else. Claude Code names the
transcript directory after the folder the session ran in, so whoever moves the agent also moves
the folder contents and the session file. Without that the agent wakes with no memory.
`views` must be a list drawn from `work`, `demo`, `personal`, or the answer is 400. All three is
stored as `null`.

`GET /api/agents/<id>/messages?n=<n>`: `n` is clamped to `1..200`, default 20. The UI asks for 30
first (`CHAT_FIRST`), then 90, then 150, then 200 as you scroll up (`CHAT_MORE` 60, `CHAT_MOST`
200). Rows are read from the tail of the session file, sidechains skipped, newest last.

Row shapes, from `transcript.recent`:

| `kind` | fields |
|---|---|
| `text` | `who` (`"agent"` or `"operator"`), `at` (ISO string), `text` |
| `tool` | `who` always `"agent"`, `at`, `tool` (name), `id` (tool_use id), `input` (trimmed to 600 chars), plus `inputRaw` only when `tool === "AskUserQuestion"` |
| `result` | `who` always `"agent"`, `at`, `id` (the tool_use id it answers), `error` (bool), `text` (trimmed to 1200 chars) |

`inputRaw` is a JSON **string**, not an object. The UI does `JSON.parse(row.inputRaw)` and reads
`.questions` to draw the question inline in the history.

Live example, `?n=3`:

```json
{"messages": [
  {"kind":"tool","who":"agent","at":"2026-09-19T01:33:39.089Z","tool":"Bash",
   "id":"toolu_01K2aqkmZgFzib77HKbM9ksr","input":"command: cd \"/home/...\""},
  {"kind":"result","who":"agent","at":"2026-09-19T01:33:40.364Z",
   "id":"toolu_01K2aqkmZgFzib77HKbM9ksr","error":false,"text":"True"},
  {"kind":"text","who":"agent","at":"2026-09-19T01:33:48.629Z","text":"Letters shipped at 07:09..."}
]}
```

An oversized trim ends with a literal line `… <n> more characters`.

`GET /api/agents/<id>/held` returns `{"held": [{"id": "a1b2c3d4e5", "ts": 1789800000.0,
"text": "..."}]}`, oldest first. `id` is 10 hex characters.

`PUT /api/agents/<id>/inbox/<name>`: the body is the file, the name is in the URL,
`Content-Type` is whatever the browser gives. No multipart. An optional `X-Platform-Note` header
is passed on to the agent. The daemon writes the file, then sends the agent a message saying
where it landed, held if the agent is asleep. Answer:

```json
{ "ok": true, "path": "/home/.../.platform/inbox/<id>/report.pdf",
  "bytes": 51234, "told_agent": "held" }
```

`told_agent` is `"sent"`, `"held"` or `"failed"`. The limit is 128 MB, over which the answer is
413. The name is tidied: only the last path component survives, unsafe characters become `-`,
leading dots go, and an existing name gets `-2`, `-3` and so on.

### 3.4 Agent actions

Every one is `POST /api/agents/<id>/<action>` with a JSON body. An unknown action is 404
`{"error": "no such action"}`. An unknown agent is 404 `{"error": "no such agent"}`.

| action | body | success | note |
|---|---|---|---|
| `wake` | `{}` | `{"ok": true, "pid": 2603}` | Opens a terminal, never a chat engine. 403 for a quarantined agent. |
| `sleep` | `{"force": false}` | `{"ok": true}` | Closes whichever engine runs. `ok` is false when nothing was running. |
| `stop` | `{}` | `{"ok": true}` | Halts the task, leaves the agent alive. Escape in a terminal, `interrupt` in a chat engine. **Publishes no event.** |
| `mode` | `{"mode": "chat"\|"terminal"}` | `{"mode": "chat"}` | 400 on any other word. Never wakes or sleeps an agent. Answers with the mode in force now. |
| `answer` | `{"id": "...", "allow": bool}` or `{"id": "...", "answers": {question: answer}}` | `{"ok": true}` | `id` is required, else 400. `answers` must be an object, else 400. Unknown id gives `200 {"ok": false, "error": "no such ask"}`. |
| `permission_mode` | `{"mode": "acceptEdits"\|"plan"\|"bypassPermissions"}` | `{"mode": "acceptEdits"}` | 400 on anything else. Writes the record and tells a live chat engine. Answers with the mode in force, which may differ from what was asked. |
| `message` | `{"text": "...", "wake": false}` | `{"ok": true, "outcome": "sent", "waiting": 0}` | Empty text is 400. `outcome` is `"sent"`, `"held"` or `"failed"`. |
| `server` | `{}` or `{"up": false}` | `{"ok": true, "server": {...}}` | Starts or stops the agent's declared server. |
| `hide` | `{}` | `{"ok": true}` | Same as `DELETE /api/agents/<id>`. |
| `show` | `{}` | `{"ok": true}` | Unhides. |
| `unfile` | `{"name": "...", "which": "outbox"}` | `{"ok": true}` | Deletes one offered file. `which` defaults to `outbox`. |
| `unhold` | `{"id": "..."}` or `{}` | `{"ok": true, "deleted": 3, "waiting": 0}` | With an id, one message goes. Without, all of them. |
| `forget` | `{}` | `{"ok": true}` | Sleeps, drops held messages, forgets the transcript reader, releases the server and port, removes the status row, deletes every job for the agent, then deletes the record. The conversation file on disk is untouched. |

Notes on `message`. Held is the default, deliberately. A message to a sleeping agent waits in the
mailbox unless `wake: true`. The chat composer always sends `wake: true`, because the person is
sitting there. The terminal side composer sends no `wake` at all, so it holds.

Notes on `permission_mode`. `POST .../permission_mode` validates the value and tells a live engine.
`PATCH /api/agents/<id>` with `{"permission_mode": "..."}` writes the same field with **no
validation and no message to the engine**. Use the POST.

### 3.5 Chat

Chat is not its own namespace. It is five of the routes above plus the WebSocket.

| thing | route |
|---|---|
| settled history | `GET /api/agents/<id>/messages?n=<n>` |
| send a message | `POST /api/agents/<id>/message` `{text, wake}` |
| choose the engine | `POST /api/agents/<id>/mode` `{mode}` |
| answer an ask | `POST /api/agents/<id>/answer` `{id, allow}` or `{id, answers}` |
| change permissions | `POST /api/agents/<id>/permission_mode` `{mode}` |
| halt the turn | `POST /api/agents/<id>/stop` |
| the live turn | `WS /ws/agent/<id>` text frames with `t` |

### 3.6 Jobs

| method | path | body | returns |
|---|---|---|---|
| GET | `/api/jobs` | none | `{"jobs": [...]}` |
| POST | `/api/jobs` | `{agent, task, at?, every?, timeout?, quiet?}` | the job, 201 |
| PATCH | `/api/jobs/<id>` | any of `task, at, every, enabled, timeout, quiet` | the job |
| DELETE | `/api/jobs/<id>` | none | `{"ok": true}` |
| POST | `/api/jobs/<id>/run` | `{}` | `{"ok": true, "said": "running 7f15667f on personal-assistant now"}` |

Job id is 8 hex characters. `JOB_PATH` is `^/api/jobs/([a-f0-9]+)(?:/(\w+))?$`.

Job record, captured live:

```json
{
  "id": "7f15667f",
  "agent": "personal-assistant",
  "task": "Write the owner's five morning letters ...",
  "at": "07:00",
  "every": null,
  "enabled": true,
  "timeout": 900.0,
  "quiet": true,
  "created": 1789442406.5880563,
  "next": 1789867812.0,
  "last_fire": 1789781417.1926188,
  "checked": 1789801509.1281297,
  "last_ran": true,
  "last_outcome": "succeeded",
  "last_summary": "Letters shipped at 07:09. ...",
  "runs": 3,
  "fails": 1,
  "missed": 1
}
```

| field | meaning |
|---|---|
| `at` | `"HH:MM"`, daily. Exclusive with `every`. |
| `every` | seconds, a float. Minimum 60. Exclusive with `at`. |
| `timeout` | seconds. Defaults to `config.JOB_TIMEOUT`, 900. |
| `quiet` | Silences success reports only. A failure always reports. |
| `next` | Unix seconds of the next due run, or null when disabled. Includes a per job jitter of a few seconds. |
| `last_fire` | Unix seconds of the last actual run. |
| `checked` | How far the missed-run check has looked. Not the same as `last_fire`. |
| `last_outcome` | `succeeded`, `failed`, `unclear`, `no reply`, `missed`, or null. |
| `runs`, `fails`, `missed` | counters |

`POST /api/jobs` refuses a job with both `at` and `every`, or neither, with 400. An unknown agent
is 404. `every` under 60 is 400. `at` that is not `HH:MM` is 400.

`PATCH` with `at` clears `every`, and `every` clears `at`. Any other key is 400.

`POST /api/jobs/<id>/run` does not wait for the job. It always answers `{"ok": true, "said": ...}`,
even for an unknown id, where `said` is `"no job called <id>"`.

The UI has one strip of job rows with three buttons: Run, On/Off (`PATCH {enabled}`), and a cross
(`DELETE`). The new job dialog posts `{agent, task, at, every, quiet}`.

### 3.7 Cords

| method | path | body | returns |
|---|---|---|---|
| GET | `/api/cords` | none | `{"cords": [...]}` |
| POST | `/api/cords` | `{kind, from, to, instruction, budget?}` | the cord, 201 |
| POST | `/api/cords/<id>/hangup` | `{"summary": "..."}` | `{"ok": true}` |

Cord id is 12 hex characters. `CORD_PATH` is `^/api/cords/([a-f0-9]+)/(\w+)$`, so `hangup` is the
only sub path and a bare `/api/cords/<id>` does not route.

`kind` is `"transfer"` or `"call"`, defaulting to `"transfer"`. Anything else is 400. An empty
`instruction` is 400. Either agent asleep is 409. The same agent twice is 400.

Transfer cord:

```json
{
  "id": "a1b2c3d4e5f6", "kind": "transfer",
  "from": "research", "to": "coder",
  "instruction": "take this and write the patch",
  "state": "completed", "created": 1789808000.0, "finished": 1789808001.2,
  "messages": [ { "role": "payload", "text": "the sender's last message" } ]
}
```

Call cord:

```json
{
  "id": "a1b2c3d4e5f6", "kind": "call",
  "from": "research", "to": "coder",
  "instruction": "ask them how the parser handles empty input",
  "state": "running", "budget": 8, "used": 2, "summary": null,
  "created": 1789808000.0,
  "messages": [ { "role": "caller", "text": "...", "ts": 1789808001.0 } ]
}
```

| field | meaning |
|---|---|
| `state` | `running`, `completed`, `failed`, `timed_out` |
| `finished` | Unix seconds, present only once the cord ends |
| `budget` | calls only. Total messages across both agents. Default `config.CALL_BUDGET`, 8. |
| `used` | calls only |
| `summary` | calls only. What the caller wrote after `HANGUP:`. |
| `error` | present only when a call raised |

A finished cord is dropped from the list 20 seconds later. The UI draws it for 30 seconds
(`TRACE_MS`) using `server_time` minus `finished`, and keeps its own copy after the daemon forgets
it, so a trace that lingers is the browser and not the daemon.

`hangup` answers `{"ok": false}` for an unknown cord or one that is not running. Not an error code.

### 3.8 Network and health

| method | path | body | returns |
|---|---|---|---|
| GET | `/api/network` | none | the snapshot below |
| POST | `/api/network/speed` | `{}` | `{down_mbit, up_mbit, at, error}` |
| GET | `/api/health` | none | `health.snapshot()`, the same object `/api/state` carries |

`GET /api/network`, captured live:

```json
{
  "at": 1789808020.826665,
  "gateway": { "address": "192.168.1.1", "device": "wlan0" },
  "latency": {
    "gateway":  { "avg_ms": 8.0,  "max_ms": 15.6, "loss_pct": 0.0 },
    "internet": { "avg_ms": 11.7, "max_ms": 18.0, "loss_pct": 0.0 }
  },
  "wifi": { "ssid": "PATH", "signal_pct": 67, "rate_mbit": 270,
            "level_dbm": -53, "retries": 160, "device": "wlan0" },
  "drops": { "carrier_changes": 16, "disconnects": 0 },
  "speed": null,
  "verdict": "fine: 8ms to the router, 12ms out"
}
```

Any of `gateway.address`, `gateway.device`, the `latency` members, and every `wifi` field may be
null when the measurement could not be taken. The snapshot is cached and costs about half a second
when cold. The UI refetches it every 20 seconds on its own timer, not from an event.

`POST /api/network/speed` moves about 5 MB and takes seconds. It is a button and never a timer.
It writes its result into the cached snapshot's `speed` field, so a later `GET /api/network`
carries it. On failure it still answers 200 with `error` set and the two numbers null.

### 3.9 Files

| method | path | purpose |
|---|---|---|
| GET | `/file/<agent>/<name>` | download a file the agent offered |
| PUT | `/api/agents/<id>/inbox/<name>` | send a file to the agent |
| POST | `/api/agents/<id>/unfile` | delete one offered file |

**There is no JSON listing route for either box.** The outbox listing arrives inside the agent
record as `files`, and the inbox is only a count, `inbox_count`. `files.listing` exists in the code
but nothing exposes the inbox list over HTTP.

`GET /file/<agent>/<name>` answers the bytes with `Content-Disposition: attachment` and the guessed
media type, so it always downloads and never renders on the console's own origin. The name is
matched against the outbox listing, so a name that is not already a file there is 404. Traversal
never resolves rather than being refused.

Both boxes live at `<agent cwd>/.platform/{outbox,inbox}/<agent id>/`.

### 3.10 Data

| method | path | returns |
|---|---|---|
| GET | `/api/agents/<id>/data` | `{files: [...], server: {...}}` |
| GET | `/api/data/<agent>/<name>` | one resolved value |

`DATA_PATH` is `^/api/data/([a-z0-9-]+)/([A-Za-z0-9._-]+)$`. The name must then match
`^[a-z0-9][a-z0-9._-]{0,63}$` or the answer is 400.

The daemon asks the agent's own server first, then falls back to a file in
`<cwd>/.platform/data/`. The caller does not learn which until it reads `source`.

From a server:

```json
{ "name": "tasks", "source": "server", "port": 8100,
  "updated_at": 1789808060.0, "age": 0.7, "value": { "lists": [], "tasks": [] } }
```

From a file:

```json
{ "name": "tasks", "source": "file", "file": "tasks.json",
  "updated_at": 1787378052.97, "age": 2429959.9,
  "value": { }, "text": null }
```

`value` holds parsed JSON and `text` is then null. For `.txt`, `.csv` and `.md` the opposite is
true: `text` holds the content and there is no `value`. Half written JSON sets an extra `error`
key and leaves `text` filled. A file over 4 MB is 400. Nothing published under that name is 404.

Readable extensions are `.json`, `.txt`, `.csv`, `.md`.

### 3.11 Fetch

`POST /api/fetch`, body `{url, method?, headers?, body?, timeout?, agent?}`.

The daemon fetches an address on behalf of an agent-authored screen, which has no network of its
own. `agent` is a label for the log line, never a permission. Every screen may ask for every
address.

```json
{ "status": 200, "url": "https://example.com/x",
  "headers": { "content-type": "application/json" },
  "bytes": 1234, "truncated": false, "ms": 210,
  "json": { }, "text": null }
```

A non JSON text response fills `text` and has no `json`. Binary fills `base64` and sets
`text: null`. An HTTP error status is a normal 200 answer with that `status`, because "429, try
later" is more use than "the fetch failed". Only a connection failure is 502. A scheme other than
http or https, or a method outside `GET POST PUT PATCH DELETE HEAD`, is 400.

### 3.12 Pages

`GET /view/<agent>/<slug>/` serves one of the agent's own screens, wrapped in a page the platform
owns, with a strict CSP and the fonts inlined as data. The UI mounts it in an
`<iframe sandbox="allow-scripts">` and talks to it with `postMessage`. Unknown slug is 404.

### 3.13 The voice front desk

A British voice the owner talks to. The speech model is OpenAI's GPT-Live, the audio goes browser
to OpenAI on a WebRTC peer connection, and the daemon holds the key, the briefing and the tools.

GPT-Live has **no ephemeral client secret**, so the browser never holds a credential. It hands its
connection offer to the daemon, the daemon opens the session with the project key, and what comes
back is an SDP answer, which is an address rather than a permission.

| method | path | body | returns |
|---|---|---|---|
| POST | `/api/voice/session` | none | may a call be made, and on what terms |
| POST | `/api/voice/offer` | `{sdp, resume?, back?, sent?}` | `{sdp, session_id, model, voice, budget, session_minutes, wait_pause_seconds, turn_gap_ms, silence_minutes, report}` |
| POST | `/api/voice/tool` | `{name, args}` | `{result}` |
| POST | `/api/voice/judge` | `{kind: "reply", proposal_id, line}` or `{kind: "claim", line}` | `{decision, confidence}` or `{claimed, p}` |
| POST | `/api/voice/log` | `{who, text}` | `{ok, row}` |
| GET | `/api/voice/log?n=` | none | `{calls, budget}` |
| POST | `/api/voice/end` | `{session_id, seconds, why?}` | the budget after charging |
| POST | `/api/voice/image` | `{image, text?}` | `{said}` |
| GET | `/api/voice/settings` | none | `{memory_turns, recall_span, silence_minutes, morning_brief, quiet_start, quiet_end, limits: {memory_turns: {min, max, default}, recall_span: {min, max, default}, silence_minutes: {min, max, default}}, times: {morning_brief, quiet_start, quiet_end}}` |
| POST | `/api/voice/settings` | `{memory_turns?, recall_span?, silence_minutes?, morning_brief?, quiet_start?, quiet_end?}` | the same, after saving |

**The memory.** `var/voice/calls.jsonl` is the memory and is never trimmed or rewritten. Every turn has a number `n`, its 1 based line number in that file, which never changes because the file is only appended to. Two settings, each with an environment default and kept in `var/voice/settings.json` once the Config page changes it: `memory_turns` (`PLATFORM_VOICE_MEMORY_TURNS`, 30, 1 to 100), the last turns read into every call's briefing, and `recall_span` (`PLATFORM_VOICE_RECALL_SPAN`, 2, 0 to 10), the turns before and after each recall match. A third setting is kept in the same file and is not memory: `silence_minutes` (`PLATFORM_VOICE_SILENCE_MINUTES`, 8, 1 to 30), the minutes with nobody speaking before a running call hangs up. `POST /api/voice/settings` saves whichever it is sent, pulls a number out of range into range, and answers 400, saving nothing, for something that is not a number. Recall never searches the briefing's turns or the call in hand.

**`POST /api/voice/session`** is the door and it calls OpenAI not at all, so it answers before the
browser asks for the microphone.

```json
{
  "ok": true,
  "model": "gpt-live-1",
  "voice": "vesper",
  "backend": "gpt-5.6-terra",
  "budget": {"used_minutes": 4.2, "daily_minutes": 120, "left_minutes": 115.8, "session_minutes": 30},
  "session_minutes": 30,
  "wait_pause_seconds": 45,
  "turn_gap_ms": 2500,
  "silence_minutes": 8
}
```

`wait_pause_seconds` and `turn_gap_ms` are counted in the browser, because the browser is the only
thing that hears the silence. The first pauses a call that is waiting on an agent; the second
decides where the call log cuts one line and starts the next. `silence_minutes` hangs up a running
call when nobody has spoken for that long and nothing is working: no tool call, no image with the
vision model, and no agent that owes an answer. A paused call is not running, so the wait pause
always wins. A browser whose daemon omits it uses 8.

**`POST /api/voice/end`** charges one session. `why` is optional and says how it ended, for the
daemon's log line: `hangup`, `back`, `pagehide`, `hidden`, `silence`, `cap`, `pause` or `wait`.
Anything else is left out of the line. A second end for a session id already charged adds nothing
and is logged as `ended again, ignored`; only a larger number tops it up. The browser also sends
this route with `navigator.sendBeacon` when the page goes away: a plain POST of the JSON body as
an `application/json` Blob, with no other headers. The route reads the body whatever its content
type, so a text/plain beacon works too. A beacon cannot carry an Authorization header, so off the
tailnet with only a token, the beacon is refused and those seconds are not counted.

Two refusals, and both carry the sentence to show the operator:

| code | when |
|---|---|
| 503 | No `OPENAI_API_KEY`. "There is no OpenAI key. Put OPENAI_API_KEY in var/secrets.env and restart the daemon." |
| 402 | The daily minutes are spent. "The voice has used its 120 minutes for today. It comes back at midnight." |

**`POST /api/voice/offer`** is the call itself. Both refusals above are checked again, 400 with no
offer in the body, and 502 when OpenAI refuses. The daemon posts to `v1/live/sessions` with the
session config and `transport: {type: "webrtc", sdp}`, and passes `transport.sdp` back.

`resume` is optional, and it is a list of `{who, text}`: the turns of a call the owner paused. A
pause closes the session at OpenAI, so going again is a new session, and `resume` is what makes
the two read as one conversation. It changes three things in the payload:

- the front of house prompt gains `voice.NO_GREETING`, because the persona tells him to open with
  "Welcome back, sir" and that is right once and wrong twice
- the backend briefing becomes `voice.resume_brief(turns)`: the whole briefing plus a
  `## This call so far` section and the same instruction. Turns already read out under that
  heading are left out of the previous calls section, so nothing is said twice
- the session gains `input`, which is up to 128 conversation items. It is the documented way to
  seed a session, and it puts the turns in the live model's own conversation rather than in a
  description of it

`back` is optional, and it is one `{agent, kind, asking?}` or a list of them: which agents came
back and have not been told yet, and whether each finished (`kind: "settled"`) or stopped to ask
something (`kind: "ask"`, with `asking` as one short line). A call that is waiting on an agent
pauses itself, so the agent coming back is what opens it again. A pause can also land before she
said it, so the browser keeps every fact not yet told and sends them all on every resume, manual
or automatic. A fact counts as told once her next output transcript line starts after the words
were handed to her; a hang up clears the list. `voice.came_back(back)` writes one short sentence
per agent, the newest fact per agent, and `## While you were paused` goes to both prompts with an
instruction to report it at once and not to greet him. With news to tell, the resume section drops
its last sentence, "If he has said nothing yet, wait for him in silence.", because the two
instructions together kept her quiet. Anything that is not an agent and a kind yields nothing.

`report` in the answer is that sentence, or `""`. On `session.started` of a resumed session with a
report, the browser sends one `session.instructions.append` (`delegation_id: null`) carrying the
report as a fact and "say this to him now in one sentence, then wait", then `response.create`,
the same shape as a typed line. A prompt alone did not make her speak first.

`sent` is optional, and it is a list of `{agent, at}`: every agent given work on this call and
when the confirm went out, in seconds. The browser keeps it for the whole call. Both prompts on a
resume then gain `## Delegated on this call, as it stands now`, one line per agent read from the
daemon at that moment: stopped on an ask, finished since the task went out (its last message is
newer than the task and than anything said to it since, and it is not busy), or still working.

The browser sends the turns and the two facts, and nothing else. Every word of a prompt is written
in the daemon, which has the persona, the fleet and the recaps, and a prompt assembled in two
places is a prompt that drifts.

**`POST /api/voice/tool`** is one door for every tool the voice has, so the browser forwards a tool
call without knowing what any of them mean. An unknown name is a 200 whose result is
`"no such tool: <name>"`, because a model mistake is not a console fault and the voice can recover
in the same breath. The tools:

| tool | what it answers |
|---|---|
| `fleet_status()` | every agent, awake or asleep, working, asks waiting, the last thing it said |
| `agent_history(agent, n=8)` | the last few turns of one conversation, tools as one line each |
| `agent_recap(agent)` | the newest `away_summary` that agent wrote, cut to 60 words |
| `pending_asks()` | every ask waiting across the awake agents, with ids |
| `answer_ask(agent, ask_id, allow)` | the owner's own yes or no, carried to the broker |
| `insights()` | turns, tokens and last seen per agent, the schedule, the network verdict |
| `agent_pages(agent)` | the pages that agent built and the data names it publishes |
| `page_data(agent, name)` | the data behind one page, cut at about 2 KB with a note |
| `calls(n=12)` | turns from previous voice calls |
| `recall(keywords, about)` | `{memories: [{n, date, who, text, hit?}], note?}`: earlier turns that matter now. Every spoken turn is embedded once (OpenAI `text-embedding-3-small`, 512 dimensions, kept in `var/voice/embeddings.jsonl`). The query is the keywords and `about` together, and the `PLATFORM_VOICE_RECALL_K` (30) closest turns by cosine are the hits. One Jev request asks exactly one Noul per hit. Each hit above 0.5 brings the turns from n minus `recall_span` to n plus `recall_span`, notes left out, clamped to the file; overlapping ranges merge, the hit is marked `hit: true`, and all of it comes back in order of n. Past about 4000 characters, whole ranges are dropped from the weakest hit up. `note` is "nothing relevant"; or says the matches are unranked, the top 5 by cosine, when TypeSafe has no key or failed; or is "memory search is unavailable right now" when the embedding call fails |
| `turns(from, to)` | `{turns: [{n, date, who, text}], last, note?}`: the turns numbered from..to inclusive, notes included with who `note`. Out of range numbers clamp to the file, and 60 is the most per call: a bigger range returns the first 60 and `note: "capped, ask for n=<next> onward"` |
| `propose(agent, task, turns=0)` | writes a proposal down and returns `{id, readback, turns}`. Runs nothing. |
| `confirm(id)` | runs that proposal, once, if it is still fresh |
| `cancel(id)` | drops it |
| `read_file(path, start=1, lines=200)` | a page of one text file, behind two locks |

Agent names arrive through speech recognition, so they are matched loosely: by id, by name, then
by both with every non letter dropped, then by prefix. The answer carries `resolved_to` so the
voice says which agent it landed on.

**Propose then confirm** is the whole safety story. `propose` writes `{id, agent, task, at}` into
memory and returns the sentence to read aloud. Only `confirm`, with that id, inside
`PLATFORM_VOICE_CONFIRM_SECONDS`, reaches `broker.send(agent, task, wake=True)`. The proposal is
taken out of the dictionary before the send, so the same yes twice sends once. A `confirm` for an
id that was never proposed returns `{ok: false, said: "That proposal has expired or was never
made..."}` and does nothing. `propose` also takes `agent: "new"` with `name` and `cwd`, and then
`confirm` calls `registry.create` before sending.

**`turns` carries the conversation with the task.** A task alone loses the call that led to it, so
an agent reading "do the second one" has no second one. The voice chooses how many of this call's
recent turns go with the task: nought by default, a hundred at most, and a larger number is cut
down to a hundred rather than refused. The readback then ends "with the last N turns of this call
attached", so he hears how much is going out before he says yes.

The turns are read at the `confirm`, not at the `propose`, so what goes out is the call as it stood
when he said yes. `confirm` takes the last N rows of `calls.jsonl` that belong to the call in hand,
leaves the notes out, and builds the message the agent receives: the task, a blank line, the
heading `From the call with the owner, last N turns`, then one line per turn, `The owner:` for his and
`The front desk:` for the voice. The heading says the real count, because a call two turns old cannot hand
over twenty. Nothing writes down the moment a session opens, so a row belongs to the call in hand
when it is inside the last `PLATFORM_VOICE_SESSION_MINUTES`, which keeps a paused call and its
resume together. The `said` that comes back gains "with N turns", so the Sent row on screen says it
too. The daemon log carries the count and never a word of the turns.

**`read_file`** is the one tool that reaches outside the platform's own records, so it has two
locks and they are independent of each other.

1. The path is resolved with its symlinks followed, and the result has to sit inside one of
   `PLATFORM_VOICE_READ_ROOTS`. A link out of a root is out of the root.
2. No component of the path may be `secrets.env`, `.env`, `.creds`, `.ssh` or `.gnupg`, and
   nothing may sit under `var/secrets`. This is checked on what was asked for and again on what
   it resolved to, so a link called `notes.txt` cannot launder the name it points at.

Anything refused answers one sentence and no content at all. A file whose first 8 kB holds a NUL
byte is not text and is refused too. The answer is `{path, start, end, total_lines, text}` with
200 lines and 16 kB per call, whichever comes first, plus a `note` saying how many lines are left
and which line to ask for next. The tool description tells the model to read the shape first and
offer the rest, because a file read aloud is a page at a time.

**`/api/voice/log`** is the call log, `var/voice/calls.jsonl`, one `{at, who, text}` per line. The
browser posts each finished turn as it is transcribed, so the history is written while the
conversation happens and is read back into the next call's briefing. A turn of only whitespace or
only punctuation is refused with `{ok: false}` and written nowhere: speech recognition emits
fragments that are one full stop, and those are not turns.

`who` is one of three. `you` is the owner, `voice` is the front desk, and `note` is the console
saying what it did: "Sent to personal-assistant." or "Nothing was sent to personal-assistant."
The browser writes a note the moment a confirm answers, whichever way the confirm was reached, so
the screen and the log both say a task went out rather than only the voice saying so. Anything
else in `who` is filed as `voice`, which is what an older browser gets.

A note is read back differently from a turn. `voice.briefing()` labels it `Note:` rather than
`You:`, because a note read back as something the voice said is a voice that believes it sent
something it did not, and `voice.resume_input()` leaves it out of the conversation items
entirely: it is a fact about the call, not a thing either of them said out loud.

**Every tool call leaves one line in the daemon log.** The name, the agent it resolved to, and
whether it was `ok`, `failed`, or `no such tool`. `confirm` writes a second line saying whether a
send happened and to whom. Neither line carries a tool's answer: a `propose` line carries the
task cut to 80 characters and a `read_file` line carries the line numbers and nothing of the
file. Two nights running the voice said "sent" without calling confirm and the log could not
settle it either way, because nothing wrote a tool call down at all.

**Where a turn ends** is the browser's decision and only the browser's. The docs are plain about
it: "Transcript deltas have no item ID or event that marks a completed conversational turn, so
your application decides how to group them." This is not the same question as when the voice may
speak, which is turn detection, happens in the audio stream at OpenAI, and has no setting in the
API at all. The two used to share one 1400 ms timer, which cut a question into three lines and had
the voice answer each fragment. Now `PLATFORM_VOICE_TURN_GAP_MS` decides the log alone, the other
speaker starting no longer closes a line, and both of you can have a line open at once.

**`POST /api/voice/judge`** is the referee (`commandcenter/judge.py`). The voice once said "Sent"
after his "yeah" and never called confirm, so the console no longer takes her word for it. Jev
(TypeSafe, `memory.post_jev`, the same key, scrub and transport, a 6 second timeout) answers one
question per request, and the browser acts on it through the same confirm and cancel the buttons
use. The route answers; it never sends anything itself.

- `kind: "reply"`: one Choice. The state is `{proposal: {readback, action, agent, task},
  owner_said}`, the proposal read from the held one by `proposal_id` and left held. The options
  are `confirmed` (a clear yes to the proposal exactly as read), `modified` (a yes with a change,
  or a change of agent, recipient, task, time or scope), `cancelled` (a no or a drop) and
  `unrelated` (filler, a sound, anything else), each with a meaning and his own phrasings as
  examples. Answers `{decision, confidence}`.
- `kind: "claim"`: one Noul on `{desk_said}`: does her line claim an action was carried out, as
  opposed to proposing, asking or reporting? Answers `{claimed, p}`, claimed above 0.5.

No key, a Jev failure, an unknown or expired proposal, an empty line or an answer that is not an
option: `{decision: "unrelated", confidence: 0}` or `{claimed: false, p: 0}`, so nothing acts on a
fault. A `kind` that is neither is 400. One daemon log line per judgement: the kind, the decision
or p, the latency, and the line cut to 40 characters.

The browser acts at 0.8 and above. Five real judgements on 24 September 2026, 0.8 to 1.1 s each:
"Yeah" confirmed 1.0, "yes but also send it to initech" modified 1.0, "no wait" cancelled
1.0, "mm" unrelated 0.99, and the claim "I did, Acme has it." p 0.94.

**`/api/voice/end`** charges one session to `var/voice/usage.json`. The browser reports the
seconds, because it is the only thing that knows when the audio stopped, and it prefers OpenAI's
own `session.usage.updated` number over the wall clock. A call now ends more than once: a pause
ends a session and a resume opens another. So the seconds are remembered per `session_id` and only
the part above what that session has already been charged is added. The same post twice adds
nothing; two sessions on one call are both charged.

**`POST /api/voice/image`** is how the voice hears about an image he pasted. The live model cannot
see, so the daemon asks a small vision model and the browser hands the voice the words. `image` is
a data URL (PNG, JPEG, GIF or WebP) and `text` is anything he typed beside it. The daemon posts one
request to `https://api.openai.com/v1/responses` with `model` set to `PLATFORM_VOICE_VISION_MODEL`,
`store: false`, and one user message holding an `input_text` (his words, then: describe what
matters for a spoken answer, in plain sentences, under 120 words, and read any text exactly) and an
`input_image`. The answer is `{"said": "<the model's words>"}`.

| code | when |
|---|---|
| 400 | no image, not a data URL, not an image type, or damaged base64 |
| 413 | over 8 MB decoded. "That image is too large. The limit is 8 MB." Refused before OpenAI. |
| 502 | OpenAI refused or could not be reached, or said nothing. The key is scrubbed from the sentence. |
| 503 | no key |

One daemon log line per image: its size in kB, the model, ok or failed. Never the image and never
the description. The image is not kept anywhere; the call log gets his words and ` [image]`, which
the browser writes through `/api/voice/log`. A daemon older than this route answers 404, and the
overlay says "This daemon cannot read images yet. It needs a restart." in the transcript.

**Settings**, all environment driven, all in `config.py`:

| variable | default | what it decides |
|---|---|---|
| `OPENAI_API_KEY` | none | no key, no voice. Read from `var/secrets.env`. |
| `PLATFORM_VOICE_MODEL` | `gpt-live-1` | the model that listens and talks |
| `PLATFORM_VOICE_BACKEND` | `gpt-5.6-terra` | the model that thinks and runs the tools |
| `PLATFORM_VOICE_NAME` | `vesper` | the voice. The only one OpenAI describes as British. |
| `PLATFORM_VOICE_VISION_MODEL` | `gpt-6-luna` | the small model that looks at an image he pastes |
| `PLATFORM_VOICE_VISION_TIMEOUT` | 60 | seconds the daemon waits for that model |
| `PLATFORM_VOICE_DAILY_MINUTES` | 120 | past this, a new call is refused |
| `PLATFORM_VOICE_SESSION_MINUTES` | 30 | the browser ends the call here, warning two minutes first |
| `PLATFORM_VOICE_CONFIRM_SECONDS` | 90 | how long a proposal stays confirmable |
| `PLATFORM_VOICE_WAIT_PAUSE_SECONDS` | 45 | a call waiting on an agent pauses itself after this much quiet. 0 turns it off. |
| `PLATFORM_VOICE_TURN_GAP_MS` | 2500 | this much quiet from one speaker cuts a line in the call log |
| `PLATFORM_VOICE_READ_ROOTS` | the owner's home | comma separated folders `read_file` may read inside |

**The overlay's controls** are Mute, Pause or Resume, and Hang up, left to right, all at least
44px and all keyboard reachable. Escape still hangs up.

- **Mute** disables the microphone track and nothing else. The session stays open, the voice keeps
  talking, the meter keeps running, and the orb follows her alone. `session.input_audio.mute` goes
  to the session as well, which is the documented event, so GPT-Live does not read a silent stream
  as the owner having stopped mid sentence.
- **Pause** closes the session cleanly and posts its seconds, exactly as Hang up does. The overlay
  stays, the transcript stays, and the clock stops because no session is running. **Resume** opens
  a new session with `resume`.
- **Waiting** is the call pausing itself. Every long silence in a real call came straight after
  "I have sent that to personal-assistant": he is not away, he is waiting, and an open session
  bills every second. So while an agent owes an answer, nothing is running here and neither side
  has spoken for `wait_pause_seconds`, the call pauses. The state line reads "Paused, waiting on
  personal-assistant" and one line under it says "Talking wakes it, or the agent coming back."
- **Waking** happens two ways. The agent comes back, and the call resumes with `back`, so the
  first thing the voice says is what arrived. Or he talks: while paused the browser opens a local
  microphone that reaches no session, feeds it to the same meter so the orb still breathes, and
  resumes when the level holds for about 300 ms. A muted microphone is not opened at all.
- **Nothing ends a paused call but Hang up.** There are no timers on it.
- **Ask or Auto** is the delegation switch, kept in `localStorage` under `cc.voice.mode`. Ask is
  the old behaviour: propose, read it back, wait for a yes. Auto confirms the proposal in the
  browser the moment it lands, through the same `/api/voice/tool` route, so the daemon still
  decides and still sends exactly once. The answer handed back to the model is the confirm's, not
  the proposal's, so the model never sees the "Shall I?" at the end of the readback.
- **The typed box** is one line at the bottom of the overlay. Enter sends. It is for the things
  speech recognition gets wrong: a spelling, a name, a correction. It is drawn and logged as his
  turn, with a small "typed" mark. A paused call resumes first and the line is sent once the new
  session says it has started.

**Anything mode dependent reaches the live model as an instruction.** A session's `instructions`
are immutable after it starts, so on every `session.started`, first call and resume alike, the
browser sends one `session.instructions.append` with `delegation_id: null` saying which mode is
on. The same event carries a typed line and the Auto mode's "that was sent already". The persona
file says both modes exist and that the daemon will say which one is running.


### 3.13a The inbox

What the front desk has to tell the owner when a call opens (`commandcenter/inbox.py`). No database: an append-only event log, `var/voice/inbox.jsonl`, one row per event (`added`, `told`, `handled`, `later`, `dismissed`, `opened`, `replaced`, `expired`, `removed`, `restored`). The daemon is the only writer and folds the log on its first read.

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/api/inbox` | none | `{waiting, told, summary}`. `told` is every item that is not waiting and changed in the last 24 hours, with its last `decision` and `by` |
| POST | `/api/inbox` | `{agent, text, urgency?, expires?, link?}` | the item, with `duplicate` |
| POST | `/api/inbox/<id>/<action>` | `{by?}` | the item. Action is `told`, `handled`, `later`, `dismissed`, `opened`, `removed` or `restored`. 404 for no such item |
| POST | `/api/inbox/clear` | `{confirm: true}` | `{removed}`. 400 without the confirm |
| POST | `/api/inbox/judge` | `{moment: "told", line, ids}` or `{moment: "reply", line, ids, desk}` | `{decisions: [{id, decision, p, agent, link?}], ran}` |

An item is `{id, agent, text, urgency, expires, link, added, status, decision, by, p, changed}`. Urgency is `now` (expires after 2 hours), `today` (at local midnight) or `fyi` (after 24 hours), unless `expires` gives a time. Expiry is applied when the inbox is read. The same text from the same agent within an hour is the same item. `later` puts an item back to waiting. Once a month, items closed more than 30 days ago move to `var/voice/inbox-YYYY-MM.jsonl`.

Automatic items: a new chat ask is a `now` item linked to that agent's chat, handled when the ask is answered or gone; a failed cron job is a `today` item. Agents add their own with the MCP tool `brief(text, urgency, expires)`.

`GET /api/state` carries `inbox: {untold, urgent, at}`. The event stream sends `event: inbox` with `{event, id, by, summary}` on every change.

`POST /api/voice/offer` puts the waiting items, most urgent first, in both startup prompts, and answers `brief: [{id, agent, text, urgency}]` and, on a resume, `brief_say`, the line the browser hands the new session so he says them first.

Jev decides three things, one request each, and acts only at 0.8 or above: after her line, which untold items she conveyed (one Noul per item, `told`); after his line, what he wants for each told item (one Choice per item: handled, later, dismissed, open, unrelated); and when an item is added, whether it replaces a waiting item from the same agent (one Noul per item). With no key or a failure nothing changes.

Pings: `var/voice/settings.json` holds `morning_brief` (07:15), `quiet_start` (23:00) and `quiet_end` (07:00). In the ten minutes after the morning time, and when a `now` item has waited a minute outside the quiet hours, Telegram gets one line, "The front desk has N things, open the app", only when something is untold, once per trigger (`var/voice/inbox-pings.json`). The phone app does not read the inbox yet; the next APK can read `inbox` from the `/api/state` poll it already makes.

### 3.13b Agent calls

When an agent rings the owner (an `urgent` inbox item, from `brief` or `call_owner`), the agent itself is on the line, not the front desk (`commandcenter/agentcall.py`). The ring in the phone digest now carries `call`, the id of this one ring. The daemon records the agent's opening line ("Hi the owner, it's Juno. ...") in its voice at ring time, in the background, as `var/voice/calls/<call>.mp3`. Each agent's calls are an event log, `var/voice/calls/<agent>.jsonl` (`rang`, `clip`, `answered`, `turn`, `session`, `missed`, `ended`, `told`).

| Method | Path | Body | Answer |
|---|---|---|---|
| POST | `/api/voice/session` | `{caller?, resume?}` | as before, plus `caller: {call, agent, name, opening, voice, clip}` when this is an agent's call. A fresh call made while an agent's ring is up is that agent's call; `resume: true` never picks up a ring |
| POST | `/api/voice/offer` | as before, plus `caller?` | `caller` names the call (its id, or the ring's item id) and wins. `"desk"` says it is the front desk's call whatever is ringing. With no `caller`, a fresh offer during an agent's ring is that agent's call. An agent's answer adds `caller` and `greeted`, the line to send once its recorded opening has played |
| POST | `/api/voice/tool` | as before, plus `session_id` | when the session is an agent's call, its tools run: `tell_agent {text}` and `ask_agent {question}`. The agent is the call's, never an argument |
| POST | `/api/voice/log` | as before, plus `caller` | with `caller`, the line goes to that call's record and not to the front desk's call log |
| POST | `/api/voice/end` | as before | an agent's session that ends by anything but `pause` or `wait` ends the call, and the agent gets the transcript as one message |
| GET | `/api/agents/<id>/calls` | none | `{calls: [{id, at, reason, opening, outcome, how, seconds, answered_at, ended_at, turns, audio}]}`, newest last, 50 at most. `outcome` is `ringing`, `answered`, `outgoing` or `missed`; `how` says why missed: `unanswered`, `declined` or `callback` |
| GET | `/api/agents/<id>/calls/<call>/audio` | none | the opening line as `audio/mpeg`. 404 for a call that is not this agent's or has no recording |
| POST | `/api/agents/<id>/call` | `{reason}` | the agents' `call_owner` tool. The urgent item, with `duplicate`. 429 with a sentence after three rings in an hour |
| POST | `/api/phone/decline` | `{id}` | Decline on the call screen: the ring stops and the call is missed. `{ok}` |
| GET | `/api/voice/preview/<voice>` | none | a few seconds of that voice as `audio/mpeg`, made once. 404 for a voice that can only be heard live |

The owner can also call an agent, from the call button in its chat composer. The browser names it `caller: "agent:<id>"` in both the session and the offer, validated against the registry. There is no ring, no inbox item and no recorded line: the agent greets him live. The offer writes a `dialed` event and answers `caller` with the new call id, which the browser then uses for lines, tools and a resume. The record's `outcome` is `outgoing`, the transcript reaches the agent headed "[the owner called you, 14:32, 2 min]", and these calls do not count against `call_owner`'s three an hour.

`GET /api/voice/settings` also answers `voices` (`desk` and any agent with its own), `voice_choices: {desk, agent}`, `desk_voice`, `agent_voice` (the defaults) and `prewarm`. `POST` takes `{voices: {<desk or agent id>: name or ""}}` and `{prewarm: bool}`. An agent picks only from the voices the speech endpoint can record, so its opening line and its call sound the same. A new front desk voice drops the recorded greeting so the next call takes it again.


### 3.14 Recruiting

Two ways to put a card back on the Fleet screen. Delete only hides a record, and Claude Code
holds conversations on this laptop the console never made, so both are recruitable.

| method | path | body | returns | when the UI calls it |
|---|---|---|---|---|
| GET | `/api/recruits` | none | `{hidden: [...], sessions: [...]}` | the Recruit screen, on mount |
| POST | `/api/recruits/show` | `{id}` | the agent record | Bring back |
| POST | `/api/recruits/adopt` | `{session_id, name?, cwd?, create_dir?}` | the agent record, 201 or 200 | Adopt |

`GET /api/recruits`:

```json
{
  "hidden": [ /* full agent records, the same shape as /api/state agents, hidden ones only */ ],
  "sessions": [
    { "session_id": "11111111-1111-1111-1111-111111111111",
      "cwd": "/home/user/ergo",
      "slug": "-home-user-ergo",
      "name": "night shift",
      "first": "read the overnight log and tell me what broke",
      "started": "2026-08-01T09:00:00Z",
      "last": "2026-09-21T04:25:56Z",
      "size": 10835,
      "turns": 41,
      "registered": false }
  ]
}
```

`hidden` here carries whole records, not ids. `/api/state` carries ids under the same word, and
the two are deliberately different: the Fleet screen needs a count, this screen needs a name and
a last line to show.

One session row is one `*.jsonl` file under `config.PROJECTS`, which is
`PLATFORM_CLAUDE_PROJECTS` and defaults to `~/.claude/projects`. What the fields mean, from
`commandcenter/recruits.py`:

| key | note |
|---|---|
| `session_id` | The filename without `.jsonl`. This is what `claude --resume` takes. |
| `cwd` | From the first `user` row. `null` when no user row carried one. |
| `slug` | The folder the file sits in, which is the working directory with every character that is not a letter or a digit turned into a dash. Lossy, so it is shown and never parsed. |
| `name` | From a `custom-title` or `agent-name` row, else `null`. |
| `first` | The first `user` row's text, whitespace collapsed to single spaces and cut to 160 characters with an ellipsis. `null` when nothing was typed. Rows marked `isMeta` and tool results do not count. |
| `started` | The first `user` row's timestamp, an ISO string as Claude Code wrote it. |
| `last` | The file's mtime, as an ISO string in UTC ending in `Z`. |
| `size` | Bytes. |
| `turns` | `user` rows, counted up to 500 and no further. |
| `registered` | True when a registry record already holds this session id, hidden or not. |

What the listing leaves out, and why:

- **Folders whose name starts with `-tmp-`.** Scratch directories. There are dozens and none of
  them is an agent.
- **Files under 1 kB.** A session that opened and closed with nothing said in it.
- **Anything inside a folder named after a session id.** Those hold that session's subagent
  transcripts and tool results, and only the files sitting directly in a project folder are
  listed.

Newest `last` first, capped at 200. Nothing is cached: the screen is opened rarely and a stale
row would offer a session that has already been adopted.

`POST /api/recruits/show` is `registry.show` and one `agents` event. An id nobody knows is 404
`{"error": "no such agent"}`.

`POST /api/recruits/adopt` writes a record pointing at an existing conversation. **It starts no
engine.** The new agent arrives asleep, like every other card, and is woken from the chat.

| key | type | required | note |
|---|---|---|---|
| `session_id` | string | yes | Missing or empty is 400. A session this laptop does not hold is 404. |
| `name` | string | no | Wins over the name in the transcript, which wins over the last part of the folder. |
| `cwd` | string | no | Run through `config.resolve_dir`, the same as `POST /api/agents`. Overrides the folder in the transcript. |
| `create_dir` | bool | no | Answer to the `needs_dir` question. |

Adopting twice is not an error. When a record already holds that session id the answer is `200`
with that record, shown again if it was hidden, so a second press cannot make a second agent on
one conversation.

If the folder has gone, the answer is `200 {"needs_dir": true, "path": "/the/full/path"}`,
exactly as `POST /api/agents` answers. The UI shows the path in a `confirm()` and posts again
with `create_dir: true`, or sends another `cwd`.

---

### 3.15 Usage

What the fleet has used, for the Usage section of the Config screen. Every number is read from
files on this laptop. Nothing is fetched.

| method | path | body | returns | when the UI calls it |
|---|---|---|---|---|
| GET | `/api/usage` | none | the object below | when Config opens |

```json
{
  "plan": {
    "five_hour": { "used_percentage": 37, "resets_at": 1790150400 },
    "seven_day": { "used_percentage": 41, "resets_at": 1790409600 },
    "at": 1790140000,
    "stale": false
  },
  "voice": { "used_minutes": 12.5, "cap_minutes": 120 },
  "agents": [
    { "id": "master", "name": "master", "cost_usd": 41.27, "tokens_out": 612000,
      "model": "claude-opus-5", "session_started": 1790001234.5 }
  ]
}
```

- `plan` comes from `var/plan-usage.json`, which Claude Code's status line writes after a reply
  in any session. It is null when the file is missing or is not JSON. A window it does not carry
  is null. `stale` is true when `at` is more than 30 minutes old, or missing.
- `voice` is today's minutes against `PLATFORM_VOICE_DAILY_MINUTES`. A usage file from an earlier
  day counts as nought.
- `agents` holds every agent `/api/state` lists, most spent first. The numbers come from the last
  `cost-state` row in the agent's session file: `cost_usd` is its `totalCostUSD`, `tokens_out` is
  the sum of `outputTokens` over its models, `session_started` is its `startTime` in seconds.
  `model` is the model of the last assistant row. Any of these is null when the file does not
  say, and an agent with no `cost_usd` sorts last.
- `cost_usd` is what the tokens would cost on the API. On a subscription it is not a bill.
- The agent list is kept for 60 seconds. A session file is read from the end, and is not read
  again while its size and time stay the same.

---

## 4. The WebSocket

### 4.1 URL

```
ws://<host>/ws/agent/<id>?rows=<n>&cols=<n>&wake=<0|1>
```

`WS_PATH` is `^/ws/agent/([a-z0-9-]+)$`. Unknown agent is 404. A request without the upgrade
headers is 400.

| query | meaning |
|---|---|
| `rows`, `cols` | The size this browser wants. Applied only if a terminal is attached. 0 or absent means "do not resize". A terminal is fitted to its **smallest** watcher, so asking large never shrinks anybody. |
| `wake` | `1` asks the daemon to start a terminal if none is running. Refused when the mode is `chat`, because a pty beside the chat engine would put two claude processes on one session file. |

The current UI opens two sockets on the same agent at different moments, one per tab, and never
both at once.

| opener | query | why |
|---|---|---|
| chat tab | `rows=46&cols=150&wake=0` | Reading a conversation must not wake an agent. 150 columns is safe because of the smallest-watcher rule. |
| terminal tab | `rows=<term.rows>&cols=<term.cols>&wake=<0 or 1>` | `wake=1` only when the person pressed Wake. |

### 4.2 The attach sequence

The server registers the frame listener **before** it sends anything, so a live frame can arrive
interleaved with the sequence below. Order of what it sends on attach:

1. `{"t": "mode", "mode": "chat" | "terminal" | "off"}`
2. `{"t": "state", "state": "idle" | "thinking"}` (`"idle"` when no chat engine exists)
3. `{"t": "perm_mode", "mode": "acceptEdits"}`
4. Only if a turn is in flight: `{"t": "live", "phase": "delta", "text": "<the whole reply so far>"}`
   **There is no preceding `start` frame on a reattach.** The client must handle a bare delta.
5. Zero or more `{"t": "ask", ...}` frames, one per ask Claude is still waiting on, oldest first,
   byte for byte the frames that were sent the first time. A reconnecting phone gets its card back.
6. Then exactly one of:
   - a terminal is alive: `{"type": "attached", "agent": "<id>", "pid": 2603, "rows": 32,
     "cols": 120, "history": 40960}` followed by one **binary** frame holding the scrollback, if
     any. `history` is the byte length of that frame.
   - `wake=1` was asked and the start failed: `{"type": "error", "message": "..."}`
   - nothing running: `{"type": "grey", "agent": "<id>"}`

Reattaching is the whole catch up. A phone that slept and lost its socket only has to reconnect.

### 4.3 Frames the server sends

Binary frames are pseudo-terminal bytes, unchanged, escape sequences and all. Write them straight
into the emulator.

Text frames are JSON. A frame with `type` is a terminal notice. A frame with `t` is a chat frame.
Both travel on the same socket, and the current UI reads only its own kind per tab.

**Terminal notices, keyed `type`:**

| frame | fields | meaning |
|---|---|---|
| `attached` | `agent`, `pid`, `rows`, `cols`, `history` | A terminal is attached. Send a resize next. |
| `grey` | `agent` | Nothing is running. The agent is asleep. |
| `exited` | `status` | The terminal process ended. **The socket is deliberately left open**, because it still carries chat frames. |
| `error` | `message` | Waking failed. |

**Chat frames, keyed `t`:**

| frame | JSON | meaning |
|---|---|---|
| `mode` | `{"t":"mode","mode":"chat"}` | Which engine runs. `off` is the resting state, not a fault. |
| `state` | `{"t":"state","state":"thinking"}` | Only `idle` and `thinking`, and only when it changed. |
| `live` start | `{"t":"live","phase":"start"}` | The agent began writing. |
| `live` delta | `{"t":"live","phase":"delta","text":"the whole reply so far"}` | **The full text, not an increment.** A dropped frame repairs itself on the next one. |
| `live` end | `{"t":"live","phase":"end"}` | Writing stopped. The record has not caught up yet. |
| `tool` | `{"t":"tool","name":"Bash","summary":"command: ls -la"}` | One tool call. `summary` is trimmed to 600 characters. No id, so it cannot be paired with a result. |
| `settled` | `{"t":"settled"}` | The turn is in the session file. Refetch `/messages` and drop the live element. |
| `echo` | `{"t":"echo","text":"what the operator typed"}` | Sent by the broker the instant a chat message goes to the engine, because stdin is never echoed on stdout. |
| `note` | `{"t":"note","text":"..."}` | A one-off sentence for the person. See the list below. |
| `ask` perm | `{"t":"ask","id":"<rid>","kind":"perm","tool":"Bash","summary":"command: rm -rf /tmp/x","input":{"command":"rm -rf /tmp/x"}}` | Claude wants permission and waits with no time limit. |
| `ask` question | `{"t":"ask","id":"<rid>","kind":"question","questions":[...]}` | `AskUserQuestion`. |
| `ask_done` | `{"t":"ask_done","id":"<rid>"}` | That ask is answered or lost. Drop the card without a word. |
| `perm_mode` | `{"t":"perm_mode","mode":"plan"}` | Sent on attach, and again when a live engine accepts a change. |

A question ask carries the tool's own input. Each entry of `questions` is what the current UI
reads:

```json
{
  "t": "ask", "id": "req_42", "kind": "question",
  "questions": [
    {
      "question": "Which branch should I push to?",
      "header": "Branch",
      "multiSelect": false,
      "options": [
        { "label": "main", "description": "the default branch" },
        { "label": "chat-engine", "description": "the feature branch" }
      ]
    }
  ]
}
```

`header`, `description` and `multiSelect` are optional. The shape comes from Claude Code, not from
this daemon, so treat every field as possibly absent.

The `note` texts the daemon sends, verbatim from `broker.py`:

| constant | when |
|---|---|
| `SWAP_NOTE` | A typed chat message closed a live terminal and the chat engine took over. |
| `BUSY_NOTE` | The terminal was mid task, so it was not closed. The message is held. |
| `TERMINAL_PERM_NOTE` | Permission mode changed while a terminal runs. It takes effect at the next start. Shift+Tab changes it now. |
| `NO_ACK_NOTE` | The chat engine did not acknowledge the permission change within 3 seconds. |
| `LOST_ASK_NOTE` | The engine died while waiting on an ask, so the card is gone. |
| the exit line | The chat engine exited. Carries the last 20 lines of its stderr when it failed. |

### 4.4 Frames the client sends

| form | payload | meaning |
|---|---|---|
| binary | raw bytes | Keystrokes. Written straight to the pty. A paste with a newline travels as bytes rather than an accidental submit. |
| text | `{"type":"resize","rows":32,"cols":120}` | Both fields are required and are read with `int()`. A missing key raises and the receive loop ends the socket. |
| text | `{"type":"input","data":"..."}` | Same as binary, as a string. |
| text | `{"type":"submit","text":"..."}` | The whole message as one piece, with the newline added by the server. This is how the terminal side composer avoids a paste submitting at its first newline. |
| text | `{"type":"interrupt"}` | Escape. The terminal's own interrupt. |

**Every text control frame is dropped when no terminal was attached at the time this socket
opened.** The server holds `term` as a local, so a terminal started after the socket attached is
invisible to it. Reconnect after a wake.

There is no way to send a chat message over the socket. A chat message is
`POST /api/agents/<id>/message`.

---

## 5. The event stream

`GET /api/events` is `text/event-stream` and never ends. It opens with the comment `: open` and
sends `: keepalive` every 15 seconds of silence. There is no `id:` field, so there is no
`Last-Event-ID` replay. `EventSource` reconnects on its own and the UI does nothing on error.

A client whose queue passes 256 frames is dropped by the server.

| event | payload | what the client should do |
|---|---|---|
| `health` | the full `health.snapshot()` object | Repaint the health readout. This is the one event that carries its data. Sent every `config.HEALTH_INTERVAL` seconds on a timer. |
| `agents` | `{}` from an HTTP action, or a broker payload | **Refetch `/api/state`.** Never trust the payload. |
| `cords` | `{"event": "cord.started" \| "cord.message" \| "cord.finished" \| "cord.gone"}` from the broker bridge, or `{}` from hangup | Refetch `/api/state`. |
| `jobs` | `{}` | Refetch `/api/state`. |

The broker payloads that reach `agents` are `{"agent": id, ...}` shapes: `agent.awake`
(`resumed`, `pid`), `agent.asleep` (`exit_status`), `agent.message` (`delivered` when held mail was
flushed), `agent.held` (`waiting`). The event **name** is always the literal string `agents`, the
broker's own event name is lost. The UI ignores the payload entirely and calls `refresh()`.

**`cron.on_change` is registered with the hub (fixed 2026-09-19).** It was defined and never wired; `server.serve()` now registers it, so a job that runs on schedule publishes a `jobs` event.
So the `jobs` event fires only from the HTTP handler: `POST /api/jobs`, `PATCH /api/jobs/<id>`,
`DELETE /api/jobs/<id>`. A job that fires on schedule, finishes, or is rescheduled sends **no
event at all**. The UI learns about it on the next refresh caused by something else. A port that
wants a live schedule must poll `/api/jobs`.

`POST /api/agents/<id>/stop` also publishes nothing.

---

## 6. Flows

### 6.1 Open the chat tab for an agent

1. `POST /api/agents/<id>/mode` `{"mode": "chat"}`. Failure is ignored and logged to the console.
   This never wakes or sleeps the agent.
2. Open `ws://.../ws/agent/<id>?rows=46&cols=150&wake=0`, `binaryType = "arraybuffer"`. Ignore
   binary frames. Handle only text frames that carry `t`.
3. `GET /api/agents/<id>/messages?n=30` and paint it.
4. Set the permission switcher from the record's `permission_mode` at once, then correct it when
   the `perm_mode` frame lands a moment later.
5. Start a 2 second timer that refetches `/messages` when the last fetch is more than 8 seconds
   old. This is a safety net for a turn whose last frame went missing, and for a record changed by
   a cord or by somebody typing in the terminal.
6. On socket close, if this agent is still the one on screen, reconnect after 1500 ms.

### 6.2 Send a message

1. Draw the echo locally **before** the request. That is the whole feel of a chat.
2. `POST /api/agents/<id>/message` `{"text": text, "wake": true}`.
3. Read `outcome`. `"held"` means it is in the mailbox and will never appear in the record until
   the agent wakes, so mark the echo held rather than waiting for it.
4. The daemon also sends a `{"t":"echo"}` frame on the socket. The two are the same message.
5. Refetch `/messages` at once, then again at 600 ms and 1600 ms. The session file needs a moment.
6. Drop the local echo once a settled `text` row from `operator` matches it after whitespace is
   collapsed.

### 6.3 Answer a permission ask

1. An `{"t":"ask","kind":"perm"}` frame arrives. Ignore it if a card with that `id` is already on
   screen, because the server resends every waiting ask on each attach.
2. Show `input.command` in full when it exists, else `summary`. Never truncate a command you are
   asking somebody to approve.
3. Disable the buttons, then `POST /api/agents/<id>/answer` `{"id": rid, "allow": true}` or
   `false`.
4. On `{"ok": true}` remove the card at once. Do not wait for `ask_done`, so the tap feels
   finished.
5. On `{"ok": false}` or an error, leave the card and show the reason. The ask is not lost.
6. `ask_done` may arrive from another browser answering first. Remove the card silently.

A denial sends Claude the tool result `"The operator said no."`

### 6.4 Answer a question ask

1. An `{"t":"ask","kind":"question"}` frame arrives with `questions`. An ask with an empty
   `questions` array is a bug, and the current UI writes a note and drops it.
2. Draw one block per question. Options are buttons.
3. `multiSelect: false`: tapping one option answers immediately.
   `multiSelect: true`: taps collect, and the free text form's Send submits them. Picked labels
   and typed text are joined with `", "` in the order given.
4. `POST /api/agents/<id>/answer` `{"id": rid, "answers": {"<the question text exactly>": "<the answer>"}}`.
   The key must be `q.question` copied byte for byte. The engine matches on it.
5. Remove that one block. Remove the whole card only when the last block is answered.
6. Skip is `{"id": rid, "allow": false}` with no `answers`. The daemon denies the question tool
   with the tool result `"The operator chose not to answer. Go on without the answer, or ask in
   words."` No answer is invented. The turn goes on or ends as the agent decides.

The daemon builds the reply as `{"behavior": "allow", "updatedInput": {...original input...,
"answers": {...}}}`. Sending the answers without the original input would not work, which is why
the browser sends only the answers and the daemon rebuilds the rest.

### 6.5 Switch between chat and terminal

Opening a tab must never start or stop an agent. `mode` records the choice and tells every
attached browser, and that is nearly all it does.

- To chat: `POST .../mode {"mode":"chat"}`. A live terminal keeps running behind the chat tab.
  Nothing is killed. `send()` is what kills it, and only when somebody types.
- To terminal: `POST .../mode {"mode":"terminal"}`. **This is the one swap a switch may make.** If
  a chat engine is alive and no terminal is, the engine is ended, the daemon waits for the process
  to really be gone, then spawns a pty. If nothing is alive, nothing starts and the agent stays
  grey.

Each tab owns its own socket. Leaving a tab closes that socket, because a socket left open is
still a watcher and would hold the real terminal at this browser's width for everybody else.

### 6.6 Page older history

`chatWant` starts at 30. When the log scrolls above 40 px from the top, add 60, capped at 200,
then refetch `/messages?n=<chatWant>` with a forced full repaint and restore the scroll offset by
the height the log grew. The server clamps `n` at 200, so there is no point asking for more.

### 6.7 Wake and sleep

- Wake: `POST /api/agents/<id>/wake` `{}`. Answers `{"ok": true, "pid": N}`. It opens a
  **terminal**, never a chat engine. A quarantined agent (`unix_user` set) is 403. Then refresh
  the state. Reconnect the terminal socket only if the terminal tab is the one being looked at.
- Sleep: `POST /api/agents/<id>/sleep` `{}` or `{"force": true}`. Closes whichever engine runs.
  The conversation stays on disk and `resumable` stays true.
- An agent in chat mode with nobody typing runs no process at all. The chat engine spawns on the
  first message.

### 6.8 Create a new agent

```
POST /api/agents
{ "name": "research", "cwd": "~/work/research",
  "model": "claude-opus-5", "permission_mode": "acceptEdits", "role": null }
```

If the answer is `{"needs_dir": true, "path": "/home/user/agents/research"}`, show the full path,
ask, and post again with `"create_dir": true`. Seeing the full path is the moment a mistyped `~`
is caught.

Success is 201 with the full agent record. The UI then refreshes state and navigates to
`#/a/<id>`.

### 6.9 Hide and forget

- Hide: `POST /api/agents/<id>/hide` `{}`, or `DELETE /api/agents/<id>`. The card goes. The
  process and the conversation are untouched. The id appears in `state.hidden`.
- Show: `POST /api/agents/<id>/show` `{}`.
- Forget: `POST /api/agents/<id>/forget` `{}`. Destructive in the daemon and only in the daemon.
  It sleeps the agent, drops held mail, releases the server and its port, deletes every job for
  the agent, and removes the record. The session file on disk survives. There is no undo and the
  daemon asks for no confirmation, so the UI must.

### 6.9a Recruit

- Open: `GET /api/recruits`. Two lists in one answer, hidden agents and loose sessions.
- Bring back: `POST /api/recruits/show` `{id}`. The record comes back with it.
- Adopt: `POST /api/recruits/adopt` `{session_id}`, plus `name` when the session has none.
  Handle `needs_dir` the way the new agent dialog does. Nothing is started, so the next step is
  the chat, where Wake is.

### 6.10 Jobs

- Run now: `POST /api/jobs/<id>/run` `{}`. Show `said`. It does not wait.
- Toggle: `PATCH /api/jobs/<id>` `{"enabled": true}` or `false`.
- Delete: `DELETE /api/jobs/<id>`.
- Create: `POST /api/jobs` `{agent, task, at, every, quiet}`, with exactly one of `at` and `every`
  and the other explicitly `null`.

After any of them, refetch `/api/state`. Only create, patch and delete publish a `jobs` event.

---

## 7. Things the UI must not rely on

- **`pid` is the terminal's pid only.** When the chat engine is the running engine, `pid` is null
  and `state` is still `green`. A UI that reads `pid` as "is it alive" will call a working agent
  asleep. Use `state`.
- **`watchers` and `size` are terminal only** and are 0 and null whenever there is no pty.
- **`working` is best effort.** It asks `claude agents --json` and matches by session id. If the
  CLI cannot be reached, or has not listed the agent yet, which is normal for the first second or
  two of a wake, it falls back to the running engine's own guess and says nothing about having
  done so. It can therefore be wrong in both directions for a moment.
- **`mode` lives in memory.** A daemon restart puts every agent back to `"off"` even though the
  terminal may have been restored and `state` is green.
- **`messages` lags the live frames.** The session file is flushed at turn boundaries. `settled`
  is the signal that the record has caught up. Until then the live text on the socket is the only
  copy. Between `live end` and `settled` the reply must stay on screen, or a finished answer blanks
  for a second and looks like a bug. If `settled` never arrives, settle anyway after 8 seconds.
- **The live delta is the whole text, every time.** Do not append.
- **A `live` delta can arrive with no `start`**, on every reattach mid turn.
- **`tool` frames have no id**, so a live tool call cannot be paired with its result. They are
  thrown away on `settled` and the record's `tool` and `result` rows, which do share an `id`,
  replace them.
- **`cost_usd` is null** when no price list is configured, which is the case on this laptop.
  `tokens` is always real.
- **`context_tokens` is not the context window.** It is the lifetime sum of everything read, cached
  reads counted again each time.
- **`last_message` is cleaned for display.** `HANGUP:` prefixes and `**` markers are stripped, and
  it is cut at 2000 characters. The stored message is untouched and the cord logic reads that one.
- **`schedule` on the agent record is dead weight.** It is stored and editable and `cron.py` never
  reads it. The schedule lives in jobs.
- **`last_seen` is always null.**
- **The `jobs` event fires on a scheduled run since 2026-09-19.** `useFleet` refetches on it; no poll is needed for the schedule strip.
- **`stop` publishes an `agents` event since 2026-09-19.** It used to be the one action that changed an agent and told nobody.
- **`answer` failing is not an HTTP error.** Check `ok === false` in a 200 body. The normal cause
  is two phones on one agent and the other answered first.
- **Terminal control frames are dropped** unless a terminal was attached when the socket opened.
- **The socket is not closed when the terminal exits.** It still carries chat frames.
- **A cord vanishes from `/api/cords` about 20 seconds after it ends.** Hold your own copy if you
  want a trace, and age it against `state.server_time`, not the browser clock.
- **SSE has no replay.** A reconnect after a gap misses everything that happened in it, so refetch
  `/api/state` when the stream reopens.
- **`PATCH /api/agents/<id>` with `permission_mode` does not validate and does not reach a live
  engine.** Use `POST .../permission_mode`.

---

## 8. Parity notes for the port

Routes the old UI uses, mapped to whether the new design has a screen for them. This is a list of
what exists, not advice about what to build.

| route | old UI uses it for | in the React design |
|---|---|---|
| `GET /api/state` | everything | yes, the shell owns it |
| `GET /api/events` | live refresh | yes |
| `WS /ws/agent/<id>` | chat and terminal | yes, `screens/chat` and `screens/chat/Terminal.jsx` |
| `GET /api/agents/<id>/messages` | chat history | yes |
| `POST .../message`, `.../answer`, `.../permission_mode`, `.../mode`, `.../stop` | chat | yes, Composer, ApprovalCard, QuestionCard |
| `POST .../wake`, `.../sleep` | the agent header buttons | yes, the agent card header |
| `GET /file/<agent>/<name>`, `PUT .../inbox/<name>` | the files strip | yes, addition 5 |
| `POST .../unfile` | the cross on a file row | **no screen named.** The route exists and the design lists a files strip with no delete. |
| `GET .../held`, `POST .../unhold` | the undelivered dialog | **no screen.** `waiting` is on the card, but nothing reads or deletes the held text. |
| `GET /api/cords`, `POST /api/cords` | drawing a cord between two cards | partly. Addition 6 is "Cords on Fleet". |
| `POST /api/cords/<id>/hangup` | the stop button on a running call | **no screen named.** |
| `GET /api/jobs`, `POST /api/jobs`, `PATCH`, `DELETE`, `POST .../run` | the schedule strip | **no screen named** in COMPONENTS.md. |
| `PATCH /api/agents/<id>` `{model}` | the model picker in the agent bar | **no screen named.** There is no dedicated model route. The model is a `PATCH` field and nothing else, and it takes effect at the next wake. |
| `PATCH /api/agents/<id>` `{views}` | the "Where it shows" dialog | **no screen named.** |
| `POST /api/agents` | the new agent dialog | **no screen named.** |
| `POST /api/agents/<id>/hide`, `/show`, `/forget` | the card menu | hide is implied, `forget` has no screen. |
| `GET /api/agents/<id>/data`, `GET /api/data/<agent>/<name>` | the screen bridge, `platform.available` and `platform.data` | yes, `PageFrame.jsx` needs the same bridge. |
| `POST /api/fetch` | the screen bridge, `platform.fetch` | same bridge. |
| `GET /view/<agent>/<slug>/` | the sandboxed iframe | yes, addition 3, agent pages as sub tabs. |
| `GET /api/network`, `POST /api/network/speed` | the network readout and its button | **no screen named.** |
| `GET /api/health` | not called, health arrives in state and on the SSE | n/a |
| `GET /api/whoami` | not called by the current UI | n/a |
| `POST /api/agents/register` | not called by the UI. Agents call it themselves. | n/a |
| `POST /api/agents/<id>/server` | not called by the current UI, though `server` is shown on the card | n/a |
| `GET /api/agents/<id>` | not called by the current UI | n/a |
| `GET /api/agents` | **does not exist**, 404 | n/a |

The screen bridge, if the React port keeps agent-authored pages, is four local answers plus three
routes. The local four are served from state the shell already has: `platform.state`,
`platform.agents`, `platform.health`, `platform.limits`. The three routes are
`platform.data` to `GET /api/data/<agent>/<name>`, `platform.available` to
`GET /api/agents/<id>/data`, and `platform.fetch` to `POST /api/fetch`. Anything else the frame
asks for must be refused by name.
