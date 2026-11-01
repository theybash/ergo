# Ubiquitous language

The words this project uses, what each one means, and which alternatives are forbidden.

Read it before planning and keep it open during. It matters more here than in most projects for one
reason: **agents write their own screens and read these documents**, so a word with two meanings
becomes two different implementations by two different agents on the same page.

Recorded from what is used, not designed. Where the code and the operator disagree, both are shown
and the gap is named rather than smoothed over.

---

## The agent

| Term | Means | Not | Lives in |
|---|---|---|---|
| **agent** | One Claude Code conversation, identified by its session id, living in a folder | Not a process: one agent outlives many processes. Not a folder: two agents can share one | `registry.py`, everywhere |
| **session id** | The uuid naming a conversation. The agent's identity | Not a login session, see the collision below | `registry.py`, `--session-id` |
| **green** | The terminal is open, holding context in memory, ready to answer | Not "busy". A green agent is usually idle | `broker.state` |
| **grey** | The terminal is closed, using nothing, resumable | Not an error, and never drawn red | `broker.state` |
| **working** | Producing output right now. Shown by motion, never colour | Not a state alongside green and grey. A working agent is green | `Terminal.working` |
| **wake** | Open a terminal for a grey agent, resuming its conversation | Not "start" or "restart": those suggest a blank conversation | `broker.start`, `/wake` |
| **sleep** | Close the terminal and sweep its process group. The conversation stays on disk | Not "kill" or "stop", which mean other things here | `broker.sleep`, `/sleep` |
| **stop** | Halt the current task and leave the agent alive, like escape in Claude Code | Not shutting the agent down. That is sleep | `broker.stop`, `/stop` |
| **hide** | Remove the card. Process and conversation untouched | Not delete. Deleting the record is `forget` | `registry.hide`, `/hide` |
| **forget** | Drop the registry record. The conversation on disk survives | Not deleting the conversation. Nothing deletes that | `registry.forget` |
| **turn** | One thing the agent said, counted from the session file | Not a message from the operator, which is not counted | `transcript.Reader` |

**The `session` collision, which has already caused trouble.** During the remote desktop work,
`session` meant an xrdp or lightdm login, and `loginctl` uses it that way. Here it always means a
Claude Code conversation. Say **login session** for the other one, never bare `session`.

---

## Talking to an agent

| Term | Means | Not | Lives in |
|---|---|---|---|
| **chat** | The button on a card that writes a message from the dashboard | Not the agent page, and not the terminal. Chat never navigates | `web/app.js` |
| **terminal** | The button and tab that open the live terminal | | `web/app.js` |
| **held** | A message waiting because the agent is asleep. Does not wake it | Not queued or failed. Held is a normal outcome | `mailbox.py`, code only |
| **undelivered** | What the card says about a held message. The same thing in plain words | | `web/app.js` |
| **mailbox** | Where held messages live, on disk, per agent | | `mailbox.py`, `var/mailbox.json` |
| **unhold** | Throw away everything held for an agent | Not delivering it | `/unhold` |
| **merged** | Several held messages delivered as one prompt with the age of each | | `mailbox.merged` |
| **outbox** | Where an agent puts a file it wants the operator to have | Not a mail outbox. No agent has a mailbox of its own | `<cwd>/.platform/outbox/<id>/` |
| **inbox** | Where a file sent by the operator lands | Not held messages. Those are the mailbox | `<cwd>/.platform/inbox/<id>/` |
| **offer** | An agent making a file downloadable, by writing it to its outbox | Not sending. Nothing leaves the laptop until the operator downloads it | `files.offer` |

---

## Cords

| Term | Means | Not | Lives in |
|---|---|---|---|
| **cord** | A connection the operator draws between two agents to move work | | `cords.py` |
| **transfer** | A cord carrying the sender's last message plus an instruction. One use | Not "push" or "pull". Those were merged: a drawn cord already has a direction | `cords.transfer` |
| **call** | A bounded conversation between two agents | | `cords.call` |
| **caller**, **callee** | The agent that started the call, and the one answering | The callee cannot end a call, so the two are not interchangeable | `cords._run_call` |
| **hangup** | The caller ending a call, with a summary of what it learned | Not a failure. A call that ends is the success case | `HANGUP:` line |
| **timed_out** | A call that hit its message budget instead of ending | Not "completed". A call that runs out was a badly framed question | `cords._finish` |
| **budget** | Messages allowed in one call, counting both agents. Eight | Not a token or money budget | `config.CALL_BUDGET` |

---

## The console

| Term | Means | Not | Lives in |
|---|---|---|---|
| **crodex** | The repository and the project. Version two of the original crodex | Say **crodex v1** for the predecessor, never bare crodex | repo name |
| **command center** | What the product calls itself on screen. The console and everything behind it | Not a second product. crodex is the repo, command center is the thing you look at | `commandcenter/` |
| **console** | The trusted web page: cards, health, terminals | See the `shell` collision below | `web/` |
| **card** | One agent's rectangle on the dashboard | | `web/app.js` |
| **view** | A screen an agent wrote for itself. One module per screen | Not the console's own pages. Only agents have views | `views/<id>/*.js` |
| **page** | One view among several. Gets a tab, titled and ordered by its filename | Not a browser page. It is a frame inside the agent page | `config.pages` |
| **default page** | The page the agent asks to have opened, named in `views/<id>/default` | Not the leftmost tab, which is always Terminal | `config.default_page` |
| **wrapper** | The page the platform owns that holds a view and sets its policy | | `web/view.html` |
| **capability** | Removed on 18 August. Agents hold their own keys and run their own servers | Do not reintroduce the word without reintroducing the design. `capabilities.py` was deleted on 19 August | gone |
| **master** | The one agent the operator talks to by default, which can drive the others | Not a privilege. It is the only agent given the fleet tools, and any agent could reach the same interface with curl | `registry`, `tools.py` |
| **tool** | Something an agent can call: an MCP server the daemon starts for it | Not a capability, and not the console's buttons. Say **the master's tools** for the fleet six | `tools.py` |
| **PA** | The agent holding the Google account. An ordinary agent, running as the operator | Not quarantined. It runs like every other agent, to keep things simple | `PRD.md`, unbuilt |
| **operator** | The one person the console belongs to | Used throughout instead of a name, so the documents stay publishable | all `*.md` |

**The `view` collision, made on 19 August and named the same day.** A **view** is a screen an agent
wrote, which is what the code and the `views/` directory have always meant. The operator's word for
work, demo and personal is also views: *"we need to bifurcate, work, personal, demo views"*.

**Decision: the agent's screen keeps `view`. Work, demo and personal are `view modes`.** The code
cannot move: the directory, `config.pages` and the wrapper all say view. On screen the button says
**Where it shows**, which needs no vocabulary at all, and the operator's own word stays right in
conversation because the context there is never ambiguous.

**The `shell` collision, and it is the worst one.** `PRD.md` and `DESIGN.md` use **shell** for the
trusted console page, which is standard in browser sandboxing. But `shell` also means a command
line, which is what an agent has and what this whole product drives, and the operator has used it
for a second machine's terminal too. Three meanings for one word in one project.

**Decision: the page is the console. The command line is the shell.** The code already agrees, since
the package is `commandcenter` and nothing in it is called shell. `PRD.md` and `DESIGN.md` were swept
on 18 August. The only remaining uses mean a command line, which is correct.

---

## Behind the console

| Term | Means | Not | Lives in |
|---|---|---|---|
| **daemon** | The one process on the laptop. Serves the console, owns the terminals, runs the scheduler, holds the bridges | Not the page. A page cannot own a process, and calling both "console" is what made this word necessary | `run.py` |
| **claude manager** | The part of the daemon that starts, wakes and sleeps agents | Not the whole daemon. The scheduler and the bridges are not part of it | `registry`, `broker`, `terminal` |
| **registry** | The list of which agents exist. Holds no liveness | Not a process list. Ask the broker what is running | `registry.py` |
| **broker** | The module that owns every terminal. See the collision below | | `broker.py` |
| **terminal** | One pseudo-terminal and everyone watching it | | `terminal.py` |
| **scheduler** | The part of the daemon that watches the clock and starts jobs | Not cron the Unix program, which nothing here uses | `cron.py` |
| **job** | One task, for one agent, due at a time of day or on an interval | Not a task an agent gives itself. A job is scheduled by the operator | `cron.py`, `var/jobs.json` |
| **missed** | A job that was due while the daemon was down, or interrupted by a restart | Not failed. A missed job did not run at all, and is never run late on its own | `cron.catch_up` |
| **rerun** | Running a missed or finished job now, from the page or from Telegram | | `cron.run_now` |
| **the code word** | `task succeeded` or `task failed`, on a line of its own, which the agent writes to end a job | Not a status the platform infers. Without it the run is recorded as unclear | `config.SUCCEEDED` |
| **bridge** | A way in from outside the tailnet. Telegram now, WhatsApp later | Not the view bridge, which is the channel to a screen. Say **view bridge** for that | `telegram.py` |
| **published data** | Something an agent offers its screens by name, as a file or on its own server | Not a capability. Nothing is granted and nothing is refused | `data.py` |
| **fetch** | The daemon retrieving an address because a screen has no network of its own | Not a permission and not a key holder. It attaches nothing and refuses no address | `fetch.py` |
| **agent server** | A program an agent declares and the daemon keeps running, on a port the daemon assigned | Not the daemon. It is the agent's own code, and it outlives the agent's terminal | `servers.py` |
| **port registry** | The daemon's record of which port belongs to which agent | Not a port an agent chose. Two agents choosing would collide at startup | `var/ports.json` |
| **status file** | What was awake and whose server was up, so a restart can put it back | Not liveness. Ask the broker what is running now | `status.py`, `var/status.json` |
| **wanted** | What somebody asked to be running. Survives a shutdown | Not `awake` or `up`, which are what was observed. A daemon stopping is not a decision | `status.py` |
| **wire** | A cord drawn as a cable over the card grid | Not the cord itself. The cord is the connection; the wire is how it looks | `web/app.js` |
| **view mode** | Work, demo or personal: which agents this page shows | Not a **view**, which is a screen an agent wrote. See the collision below | `web/app.js` |
| **verdict** | The one line naming which hop is hurting | | `network.verdict` |
| **subscriber** | One attached browser watching one terminal | | `terminal.Subscriber` |
| **session file** | The JSON-lines file Claude Code writes for a conversation | | `transcript.py` |
| **transcript** | The module that reads the session file for facts | Not the picture shown to the operator. That is the terminal | `transcript.py` |
| **ledger** | Cost and token counts, read from the session file | | `transcript.Reader` |
| **tokens read** | Every token the agent has read across all turns, added up | **Not the context window size.** Cached tokens are counted again each read | `context_tokens` |
| **the door** | The tailnet check that decides who may reach the console | Not a login. There is no login screen | `config.on_tailnet` |
| **pet** | crodex v1's word for a spawned Claude terminal | Not used here. The word is agent | v1 only |

**The `broker` collision, and it has shrunk to two.** In `broker.py` it owns terminals. In the
trading discussion it is a stockbroker. The third meaning, the key holder in `capabilities.py`, went
with the file on 19 August: agents hold their own keys, so there is nothing left to broker.

**Decision: bare `broker` means the module that owns terminals.** A trading venue is always **the
stockbroker**. `GRILL.md` and the older parts of `PRD.md` still contain the third meaning, and they
are records of a conversation rather than instructions, so they keep their own words.

---

## Tasks and their owners

| Term | Means | Not | Lives in |
|---|---|---|---|
| **his task** | A task on the operator's own list in Insights: one he does himself, or something owed to him | Not an agent's offer card. Those belong to the agent that offered them | `tasks.py`, `agent == "owner"` |
| **owner** (of a task) | The agent that keeps one of his tasks true: the agent that added it, else the home agent | Not who does the task. He does it; the owner keeps it current and reminds him | `owners.owner` |
| **home agent** | The owner of a task he added himself: personal-assistant | | `owners.HOME` |
| **slice** | The open tasks one owner owns | Not a view or a page | `owners.slice_lines` |
| **report** | A `[Task report]` message to the owner: what changed, then its slice, hot ones first | Not a brief, and not a job result. Never sent for a change the owner made itself | `owners.report` |
| **hot** | A task that is overdue, or due within **hot hours**. A report about a hot task wakes its owner | Not urgent. **urgent** is a brief that rings his phone | `owners.is_hot` |
| **hot hours** | How far ahead a task counts as hot. 24 unless he changes it on Config | | `tasks.hot_hours`, `var/tasks-settings.json` |
| **falls due** | A task's due time passes while it is not ticked. That is a change, so the owner gets a report | Not a poll. One timer waits for the next due time | `owners.due_check` |
| **take back** | An agent removing its own brief once it is out of date, so the front desk never says it | Not dismiss, which is his | `brief_take_back`, `/api/inbox/<id>/take_back` |
| **quiet** | A held message he never sees on the card, such as a fresh job's result or a report about a task that is not hot | Not undelivered. It waits on purpose | `mailbox.hold(quiet=True)`, `broker.leave` |

## Forbidden synonyms

Say **agent**, not pet, worker, instance, bot, or baby claude. Pet and baby claude belong to crodex
v1 and stay there.

Say **console**, not shell, dashboard, UI, or frontend, when meaning the trusted page. Dashboard is
allowed for the one page listing cards, which is narrower.

Say **shell**, not terminal, when meaning a command line. Say **terminal** for the pseudo-terminal
and the tab that shows it.

Say **session id** or **conversation**, not thread or chat history.

Say **login session**, not session, for anything `loginctl` reports.

Say **job**, not task, cron job, or reminder, for one scheduled thing. Task is what the agent is
asked to do, which is a field on a job and not the job itself.

Say **missed**, not skipped or late, for a job that was due while the daemon was down.

Say **published data**, or just the name of the thing, not endpoint, feed, or capability, for what a
screen asks the daemon for.

Say **agent server**, not microservice, backend, or API, for a program an agent declares. The word
API is fine in the operator's own sentences and is his; in code and documents it is an agent server.

Say **held**, not queued, pending, or stashed, for a message waiting on a sleeping agent, **in code
and in documents**. Stashed was used during the grill for deferred decisions and keeps that meaning.

**On screen, say undelivered.** The card first read "1 held" and the operator had to ask what it
meant, which is the only test a label has to pass. This is the general rule rather than one fix: an
internal name may be terse because the reader has the code in front of them, and a label may not,
because the reader has nothing but the label. Where they differ, record both here.

Say **view**, not widget, panel, plugin, or app, for an agent-authored screen.

Say **transfer**, not push or pull. Those were deliberately merged.

Say **sleep** and **wake**, not stop and start, for an agent's terminal.

Say **tokens read**, not context, for the cumulative number on a card.

---

## Unresolved

**`shell` in `PRD.md` and `DESIGN.md`.** The decision above is made and the sweep has not been done.
Fourteen occurrences across the documents, several of them quoting the operator, which should stay
as quotes. Offered twice and not yet approved.

**`chat` is doing two jobs.** It is the button that writes a message, and it was the name of the
tab now called Terminal. `GRILL.md` and older parts of `PRD.md` still say chat for the tab. The
button keeps the name because that is the operator's word for it.

**`master` versus v1's `main`.** crodex v1's rules say never kill a pet named `main`, and the
privileged agent here is `master`. Two words, two systems, adjacent meanings. Not a problem while v1
still runs, worth resolving when it sunsets.

**`stop`.** Halting a task in the console, and also `dev.sh stop` which stops the whole console.
Same word, two scopes. Probably fine because the context differs completely, but noting it rather
than deciding.
