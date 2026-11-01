# COMPONENTS.md

The contract between the app shell and the screens. A screen builds against this file only.

## Rules for a screen

1. A screen lives in `src/screens/<name>/`. `name` is one of `fleet`, `chat`, `insights`, `memory`,
   `config`, `integrations`, `health`, `recruit`.
2. `src/screens/<name>/index.jsx` has the default export. App imports it as `import Fleet from './screens/fleet'`.
3. Fake data goes in `src/screens/<name>/data.js`. Subcomponents go in the same folder.
4. A screen never edits a file outside its own folder. Not `src/App.jsx`, not `src/shell/`, not
   `src/primitives/`, not `src/styles/app.css`, not `index.html`.
5. If a screen needs a primitive that does not exist here, it writes a local one inside its own
   folder and names it in its report. It does not add to `src/primitives/`.
6. `src/styles/app.css` is frozen. If a class is missing for what you want, reuse the mock's markup
   more faithfully. A rule that cannot be avoided goes in a CSS file inside your own folder, and
   every class in it starts with your own prefix: `sh-` for the shell in `src/shell/shell.css`,
   `fl-` for Fleet in `src/screens/fleet/fleet.css`, and so on. Nothing new goes in `app.css`.
7. Every clickable element is a `<button>` or an `<a>`. An icon only button carries `aria-label`.
8. Never an em dash or an en dash, in JSX text, comments, strings or markdown. Commas, colons or
   full stops.

## State the shell owns

App holds three pieces of state and nothing else.

| state | values | default | persisted |
|---|---|---|---|
| `screen` | `fleet`, `chat`, `insights`, `memory`, `config`, `integrations`, `health`, `recruit` | `fleet` | no |
| `theme` | `dark`, `light` | `dark` | yes, `localStorage` key `cc-theme`, in try/catch |
| `view` | `work`, `demo`, `personal` | `personal` | yes, `localStorage` key `cc-view`, in try/catch |
| `voice` | boolean | `false` | no |

The count of asks is no longer App state. The Fleet screen owns it and publishes it, see
"The asks waiting on you" below.

App writes `data-theme` onto `<html>`, updates `meta[name="theme-color"]`, and adds the class
`motion` to `<html>` after the first frame. Nothing else may touch those.

Anything else a screen holds is its own: the selected agent, a filter, an open card, a deck page.

### The asks waiting on you

The count of asks is shared, and none of the places that show it may disagree:

| place | what it shows |
|---|---|
| the Fleet list heading | `.needs-count`, the number |
| an agent card | `.ac-count`, a red bubble with the number in it |
| the bottom tab bar | `.tb-badge` on Fleet |

The asks arrive on one WebSocket per awake agent, and the Fleet screen opens those sockets. So
Fleet owns the number and publishes it in `src/screens/fleet/needsStore.js`:

| export | what it does |
|---|---|
| `subscribe(fn)` | calls `fn` when the number changes, returns the function that stops it |
| `getPending()` | the number waiting right now, a plain integer |
| `setPending(n)` | Fleet only. Nothing else calls it |

App and TabBar read it with `useSyncExternalStore(subscribe, getPending)` and nothing else.
App passes the number on to Chat as `needs`.

`src/shell/needsFallback.js` is the stand in, with the same `subscribe` and `getPending` and a
count of zero. If `needsStore.js` is ever missing, the swap is one import line in `src/App.jsx`
and one in `src/shell/TabBar.jsx`.

## The one exception to "a screen gets no other prop"

Two screens take one extra prop each, and only because of the shared count and the shared
voice overlay.

| screen | extra props |
|---|---|
| `fleet` | `view` (`'work'`, `'demo'` or `'personal'`) and `onView` (its setter) |
| `chat` | `needs` (the number of asks waiting) and `onVoice` |

Every other screen still gets `onScreen` alone.

```jsx
export default function Fleet({ onScreen, view, onView }) { }
export default function Health({ onScreen }) { }
```

## How a screen switches screen

App passes one prop to the active screen:

```jsx
<Screen onScreen={goTo} />
```

`onScreen(name, hash)` takes a screen name and switches to it, and pushes one history entry,
so back returns to where you were. It also scrolls the page to the top, the way the mock does.
`hash` is optional: an agent card passes the chat's `#/a/<id>` with the screen, so the screen and
the agent are one entry and one back. Left out, the hash stays. A tap on the screen already open
pushes nothing.

```jsx
export default function Fleet({ onScreen }) {
  function open(id) {
    onScreen('chat', '#/a/' + id)
  }
}
```

Never set `window.location.hash` directly. The browser pushes its own entry for that, and the
screen would then take two backs to leave.

A screen gets no other prop, apart from the two exceptions listed above. Screens are self
contained.

## The address bar

Two things in the address bar, and each belongs to one owner.

| part | owner | meaning |
|---|---|---|
| `?screen=<name>` | App | which screen is open. Left out for `fleet`. An unknown name is corrected to `fleet` in place |
| `#/a/<id>` | the Chat screen | which agent the chat has open |

**The address decides.** App reads the screen from the address on load and again after every
back, so a reload, a pasted link and a back all land on what the address says.

**App never clears the hash.** Switching screen keeps it, so `?screen=chat#/a/job-search` opens
that agent's chat and coming back to Chat from another screen lands on the same agent.

## Back goes back one level

`src/shell/nav.js` keeps real browser history. The Android app's back gesture calls the WebView's
goBack while there is history and closes the app when there is none, so this is what makes back
walk the owner back the way he came.

| what | history | back |
|---|---|---|
| a screen change, by tab, pill, card or link | one entry, `go(screen, hash)` | the screen before |
| an agent picked in the rail | one entry, the new hash | the agent before |
| a dialog or a menu opens: New agent, Held messages, the view menu, the plus menu, the kebab | one entry with a `ccLayer:<n>` marker, same address, `useBackClose(open, close)` | closes it, stays on the screen |
| the voice overlay | one entry with the `ccVoice` marker, `shell/voiceHistory.js`, pushed again when the bar restores it | minimises, the call keeps running |
| a correction: an agent hidden or forgotten, an unknown `?screen=` | none, the entry is replaced | not a step |

Rules the file keeps:

- **One popstate handler, in App.** It calls `popped()` first, which tells nav.js which layer
  back took off and calls that layer's close, and then App reads the screen from the address.
  The chat hears the same change through `onRoute` and reads its hash again.
- **One back at a time.** A layer closed by its own button takes its entry off with
  `history.back()`, and that lands later. Every push, replace and drop waits in line until it
  has landed, or a push made in the gap would be the entry the back removes.
- **A stale layer entry is skipped.** A dialog that closes under a newer entry, as New agent does
  when the new agent's chat opens, leaves its entry behind. Back lands on it and steps back once
  more at once, so one back is still one level.
- **Nothing behind the first screen.** The page the session opened on has no entry of its own
  behind it, so back from there leaves the page, which in the app closes the app.

## The eight screens and the five tabs

| screen | desktop pill | phone tab |
|---|---|---|
| `fleet` | yes | yes |
| `chat` | yes | yes |
| `insights` | yes | yes |
| `memory` | yes | yes |
| `config` | yes | yes |
| `integrations` | **no**, Config holds it | **no** |
| `health` | **no**, Config holds it | **no** |
| `recruit` | **no** | **no** |

The desktop strip and the phone bar hold the same five. Config replaced Integrations on the
phone bar and replaced both Integrations and Health on the desktop strip. `?screen=integrations`
and `?screen=health` still open those screens on their own, and the Config tab is the one lit
while they are open: `src/shell/tabs.js` exports `tabOf(screen)`, which maps both to `config`,
and TopBar and TabBar each light the tab it returns.

The Fleet head has no Health link any more. `button.fl-health` and its CSS are gone.

Recruit has neither. It is opened from the Fleet head, `button.fl-recruit`, which calls
`onScreen('recruit')`, and by `?screen=recruit`. A seventh pill would cost every other pill
width for a screen the owner opens rarely.

`src/screens/fleet/HealthStrip.jsx` is gone. It drew the machine's memory and disk above the
grid, and the owner asked for it to go: health is a screen, and a strip repeating two of its
numbers was a second place to read them.

## Recruit

`src/screens/recruit/index.jsx`, with `recruit.css` beside it. Every rule in it is prefixed
`.rc-`.

One screen, two lists, one filter over both. An agent is a session id and a folder, so both
lists are one press from a card:

| list | what it holds | the press |
|---|---|---|
| Hidden | the agents a delete hid. `GET /api/recruits` returns whole records, not ids. | Bring back, `cc.bringBack(id)` |
| Sessions | every Claude Code conversation on this laptop, newest first, capped at 200 | Adopt, `cc.adoptSession({session_id, name?})` |

A row is the fleet card face in a line: the name in the display serif, the one line that says
what it is under it, and the quiet mono facts under that. Folder, age, turns, and for a session
its size. No chips, no tags, no colour.

- **A session already registered** shows the word `registered` in muted text and no button.
  Seeing that a conversation is already an agent is the answer to "where did that go".
- **The name is asked for only when the file has none.** The row grows an input, filled with the
  last part of the folder. Enter adopts, Escape drops it. A session with a `custom-title` is
  adopted on the first press with no question.
- **`needs_dir`** is handled the same way `NewAgentDialog` handles it: the full path in a
  `confirm()`, then the same body again with `create_dir: true`.
- **After either press** the new agent's chat opens, which is what the new agent dialog does
  today. The daemon has already published `agents`, so the card is on the Fleet screen behind it.
- **Empty states** are one quiet sentence in `.needs-empty`, and they say which kind of empty it
  is: nothing hidden at all, or nothing hidden that matches the filter.

Nothing here starts an engine. A recruited agent arrives asleep, like every other card.

## Config

`src/screens/config/index.jsx`, with `AgentRows.jsx` and `config.css` beside it. Every rule in
it is prefixed `.cf-`, and `index.jsx` imports the CSS, because it is the one component always
drawn for this screen.

Three sections, in this order, each under a quiet `h2.cf-head`:

| section | what it draws |
|---|---|
| Agents | `AgentRows`, one row per agent |
| Integrations | `IntegrationsBody` from `src/screens/integrations/` |
| Health | `HealthBody` from `src/screens/health/` |

The two bodies are the existing screens without their heads. `screens/integrations/index.jsx`
and `screens/health/index.jsx` now draw their head and the same body, so the old deep links show
what they showed before. `HealthBody` takes `fleet` as a prop and `health.css` is imported by
`HealthBody`, so it ships whichever screen draws it. Config calls `useFleet()` once and passes
it to both the rows and the Health body, so the page opens one event stream.

An agent row, one per agent in the daemon's order, which is the order the Fleet grid draws. It
lists every agent, whatever view is on, because this is where the view tags are set.

| control | saves with | notes |
|---|---|---|
| Work, Demo, Personal checkboxes | `PATCH {views}` | All three checked is sent as `null`, shows everywhere. The last checked box is disabled, because no tags means everywhere to the daemon. |
| model select | `PATCH {model}` | `MODELS` from `src/data/api.js` plus "Default model", which is `null`. Applies on the next wake. |
| permission select | `POST .../permission_mode` | `PERMISSION_MODES` from `src/data/api.js`. Not the PATCH field: API.md says that one is written unchecked and never reaches a live engine. The record is what the next wake uses; a live chat engine also takes it at once. |

Every control saves on change. There is no Save button and no success message. A failure puts
one line, `p.cf-err`, under that row, and the control goes back to what the daemon holds. A
model or mode the lists do not know is shown as it stands, the same rule as the chat's plus menu.

`MODELS` moved from `Composer.jsx` to `src/data/api.js`, beside `PERMISSION_MODES`, so the chat
and Config read one list.

---

# Shell

## TopBar

`import TopBar from './shell/TopBar'` (shell only, a screen never renders it)

| prop | type | notes |
|---|---|---|
| `screen` | string | the active screen name |
| `onScreen` | function(name) | switches screen |
| `theme` | `'dark'` or `'light'` | |
| `onTheme` | function() | flips the theme |
| `onVoice` | function() | opens the voice overlay |
| `view` | `'work'`, `'demo'` or `'personal'` | which view of the fleet is on |
| `onView` | function(view) | changes it |

Renders `header.topbar`, `.brand`, `.dot.up`, `.brand-name`, `.topbar-spacer`, the view switcher
in `div.sh-viewslot.sh-wide-only`, `nav.navtabs` with `.glider` and five buttons,
`button.orb.orb-top` for voice, and `button.iconbtn` for the theme. The CSS hides the orb under
720px, where the composer holds one instead.

The static `span.brand-sub` reading "personal" is gone. It named the view and could not change
it, so the switcher took its place.

The theme button shows a moon in dark and a sun in light, and its `aria-label` reads
"Switch to the light theme" or "Switch to the dark theme".

TopBar also owns `--topbar-h`. It measures its own height on mount, on resize and once the fonts
land, then writes the pixel value onto `<html>`. The agent page is `100vh` minus that value, so
the number has to be the real one. No screen writes it.

## TabBar

`import TabBar from './shell/TabBar'` (shell only)

| prop | type | notes |
|---|---|---|
| `screen` | string | the active screen name |
| `onScreen` | function(name) | switches screen |

Renders `nav.tabbar`, `.glider`, five buttons each with an SVG and a `<span>` label, and
`.tb-badge` on Fleet. The CSS fixes the bar to the bottom under 720px and hides it above.

There is no `needs` prop any more. TabBar reads the count itself with
`useSyncExternalStore(subscribe, getPending)` from `src/screens/fleet/needsStore`, so the badge
and the Fleet list read one number. Five tabs, never six: Fleet, Chat, Insights, Memory, Config.
The Config icon is two slider lines, drawn at the same 1.7 stroke as the other four.

## Rail

`import Rail from '../../shell/Rail'` (the chat screen renders this one)

| prop | type | notes |
|---|---|---|
| `agents` | array of `{ id, name, state, needs }` | `state` is a `Dot` state, `needs` is a number |
| `active` | string | the id of the open agent |
| `onPick` | function(id) | the screen changes its own state |

Renders `nav.rail`, `p.rail-head`, `.rail-wrap`, `.glider`, `ul.rail-list`, and one
`button.rail-item` per agent with a `Dot` and `span.count` when `needs` is above zero.
The active item carries `aria-current="true"`. The CSS hides the rail under 720px.

Rail is unchanged. Its count is per agent and comes from the `agents` array the Chat screen
passes, not from the shared store, which holds one number for the whole fleet.

```jsx
<Rail agents={agents} active={openId} onPick={setOpenId} />
```

## ViewSwitch

`import ViewSwitch from './shell/ViewSwitch'` (the shell renders one, the Fleet screen renders
the other)

| prop | type | notes |
|---|---|---|
| `view` | `'work'`, `'demo'` or `'personal'` | which one is pressed |
| `onView` | function(view) | called with the name that was pressed |

Renders `div.subtabs.sh-viewswitch` with `role="tablist"`, `aria-label="View"`, the `.glider`
span and three `button role="tab"` with `aria-selected`. It is the same segmented look as the
Chat screen's sub tabs, and it uses `useGlider` the same way.

`src/shell/views.js` holds the three names and their labels, so nothing lists them twice.

**One control, two homes.** App owns the `view` state, persists it under `cc-view` and passes it
to both:

- above 720px the top bar renders it, inside `div.sh-viewslot.sh-wide-only`;
- under 720px the Fleet screen renders it in its own head, so the phone keeps five tabs. Fleet
  hides it above 720px with its own `.fl-viewswitch` rule. `src/shell/shell.css` also carries a
  `.sh-phone-only` helper for the same job.

The rule the switcher drives, copied from the old console's `showsIn()`:

```js
/* An agent with no tags at all shows everywhere. That is the default for every agent the platform
 * has ever made, and a new agent vanishing from the page until it is tagged would read as a bug. */
function showsIn(agent, which) {
  const tags = agent.views;
  if (!Array.isArray(tags) || !tags.length) return true;
  return tags.includes(which);
}
```

The view is browser state and nothing else. It changes which agent cards Fleet draws. It never
reaches Telegram and it never stops a job running: a work tagged agent with a job at 2pm runs it
while you are in personal view. The threat is somebody glancing at the screen, not an attacker.

## Voice

`import Voice from './shell/Voice'` (shell only. App renders it once, above every screen)

| prop | type | notes |
|---|---|---|
| `onClose` | function(why) | every way out calls it; `why` is hangup, pagehide, hidden, silence or cap |
| `minimised` | boolean | the overlay is off the screen and the bar holds the call |
| `onMinimise` | function(why) | the chevron (`why` button) or Back (`why` back) |
| `onRestore` | function() | the bar or the top bar orb was tapped |

A real call, not a picture of one. Mounting it opens a voice session; unmounting it hangs up, so
closing the overlay and ending the call are the same act and neither can happen without the other.

**Every way out ends the call.** Hang up, Escape, the session ceiling and the silence hang up call
`onClose` and the unmount hangs up. The first way out wins and names the end in the daemon's log.

**Minimise is not a way out.** `button.btn.vc-round.vc-min#voice-min`, a chevron at the top left,
or Back, calls `onMinimise`. The component stays mounted with `div.voice` `hidden`, so the session,
her audio and the microphone carry on untouched. `div.vc-bar#voice-bar` is portalled into
`#vc-bar-slot`, which App keeps between the top bar and `main`, sticky under the top bar and 52px
tall, so it pushes the screen down and never covers the composer or the tab bar (the desktop agent
page is one bar shorter while it shows). It holds `button.vc-bar-open#voice-bar-open` (the small
orb on the voice level, "The front desk", the call time, and one state word: Listening, Speaking, Paused,
or "Waiting on you" in the accent while a card is up), then Mute (`#voice-bar-mute`) and Hang up
(`#voice-bar-stop`). Tapping the open button, or the top bar orb, calls `onRestore`, which pushes
the history entry again. Escape does nothing while minimised.

- **Back.** App pushes one history entry when the overlay opens (`shell/voiceHistory.js`, a
  `ccVoice` marker in the state, same address, so `?screen=` and `#/a/<id>` stay). Back pops it,
  App's popstate handler hands it to `shell/nav.js`, and the overlay hears it through
  `onVoiceBack` and minimises. While minimised there is no entry, so Back walks the screens and the
  call keeps running. Any other close takes the entry off with `history.back()`, only
  while the entry in front is still ours. The entry is a layer like any dialog's, so it composes
  with the screen and agent entries under it.
- **The page going away.** On `pagehide`, or when the tab stays hidden for 20 seconds, the call
  ends at once with `call.endNow(why)`: `session.close` on the channel, the connection closed, and
  the seconds sent with `navigator.sendBeacon`. A brief hide, such as a notification, does nothing.
  A tab closed during a Hang up's goodbye is caught too, and still posts only once.
- **Silence.** A running call with nobody speaking for `silence_minutes` (from the daemon, 8 if it
  says nothing) and nothing working asks to be hung up through `call.onEnd`. The front desk says nothing
  first. A paused call is never hung up by silence.
- **Hung up while connecting.** The session stops at its next step and opens nothing.

Renders `div.voice` with `role="dialog"`, `aria-modal="true"` and `aria-labelledby="voice-state"`,
then `.voice-inner`, the `canvas.orb.orb-big`, `.voice-state`, the rolling transcript `.vc-log`,
the proposal card `.vc-card` when one is pending, the clock `.vc-clock`, and the control row
`.vc-controls`.

The state line is the session's own state, not a timer: Connecting, Listening, Thinking, Looking,
Speaking, Paused, "Paused, waiting on <agent>", Ended, Not connected.

The transcript is the real one. The last six turns, yours right aligned and quiet in `.vc-you`,
hers left and at full strength in `.vc-them`, and the turn being transcribed carries `.vc-part`.
A part finished turn is replaced in place rather than stacked, so a line grows as it is heard.

A third kind of row is neither of yours. `.vc-note` is the console saying what it did: "Sent to
personal-assistant." when a confirm answers, "Nothing was sent to personal-assistant." when it
did not. Centred, mono, faint, no colour and no pill, because it is a fact rather than a
warning. It goes to the call log under `who: "note"`, so the next call's briefing sees it.

A line you keyed in rather than said carries a small `.vc-keyed` mark reading "typed". A line
sent with an image carries the same mark reading "image" instead.

The proposal card appears when the voice calls `propose`. It holds the readback sentence and two
buttons, Confirm and Cancel, which go through the same `/api/voice/tool` route the voice uses, so
a tap and a spoken yes are the same thing to the daemon. The voice is told which one you tapped.

**The referee.** While a card is up in Ask mode, each finished line of his that started after the
card appeared goes to `/api/voice/judge` (kind reply), off the audio path. At confidence 0.8 or
more: `confirmed` runs the tap's confirm and draws "Confirmed by your yes."; `modified` cancels,
tells the voice "The owner changed the proposal: <his line>. It was not sent. Propose again with his
change and read it back." and draws "Changed. Not sent; the front desk will propose again."; `cancelled`
cancels and draws "Cancelled."; anything else does nothing. A judgement for a card that has gone
or been replaced is dropped. Her own confirm waits up to 3 seconds for the judgement of his answer
to that card, and a change or a no stops it before it reaches the daemon.

Each finished line of hers, unless a send went through in the last 15 seconds, goes to the judge
as a claim. At p 0.8 or more, with the card still up or nothing sent since the newest proposal,
she is told "You have not sent anything. The card is still waiting for the owner." and the row "Not
sent yet." is drawn.

A tap, his yes and her confirm can race for one card. Confirms are counted per card id; a second
one while one is on its way goes nowhere, and a confirm the daemon refuses because another path
sent the card draws nothing. The daemon's confirm pops the proposal first, so it sends once
whatever the browser does. Auto mode is never judged, because Auto sends on propose.

**The typed box** is `form.vc-type#voice-box` with `input#voice-type`, above the controls. It is
hidden on every open and nothing remembers it: the keyboard button in the controls shows it and
gives the input the focus, and pressed again hides it. One line, Enter sends, placeholder "Type to
The front desk". The picture button, the input and the chip sit inside one border, `.vc-field`. It is for the things speech recognition gets wrong: a
spelling, a name, a correction. The text reaches the live model as an instruction and is drawn
and logged as your turn. A paused call resumes first and sends once the new session has started.

**An image** goes in from `button.vc-pick#voice-pick` at the left edge of the box, which opens a
file picker (`accept="image/*"`), or from a paste into the input. It waits as `.vc-chip#voice-chip`,
a 32px thumbnail with an x, `.vc-chip-x`, that removes it. Send or Enter sends the words and the
image together, and an image alone is allowed. A file over 8 MB is refused in the box with one
sentence before anything is uploaded. On send the state line says Looking while the daemon asks
the vision model (`/api/voice/image`), and that counts as work, so the call cannot pause itself in
the middle. Then the voice gets one instruction with what the image shows and his words, and a
`response.create`. Any failure is one `.vc-note` sentence in the transcript and never takes the
call down.

Escape hangs up while the box is hidden. While it shows, Escape still hangs up, except from inside
the box with something in it. The box empty is still a way out.

The input is 16px, because anything smaller makes a phone zoom the whole page on focus. While it
has the focus the overlay carries `.vc-typing`, and at phone width the orb shrinks and the
transcript gives up height, so the keyboard cannot push the orb off the top of the screen.

**The controls** are Ask or Auto, Mute, the keyboard, Pause or Resume, and Hang up, left to
right, each at least 44px.

- **Ask or Auto** is `button.btn.vc-mode#voice-mode`, `aria-pressed` when Auto. Ask is the old
  behaviour: the voice proposes, reads it back, and waits for your yes. Auto confirms the
  proposal the moment it lands, through the same route, so the daemon still sends it once. The
  switch is kept in `localStorage` under `cc.voice.mode` and read straight into the first render,
  and every read and write of storage is wrapped, because a private window throws on the
  accessor itself. The live model is told which mode is on at the start of every session and
  again the moment the switch changes.
- **Mute** is `button.btn.vc-round#voice-mute`, round because it is a state rather than an act. It
  disables the microphone track and nothing else: the session stays open, the voice keeps talking,
  and the orb follows her alone. `.vc-flag` says "Microphone off" in words as well.
- **The keyboard** is `button.btn.vc-round#voice-keys`, round like Mute with the same stroke
  weight, `aria-pressed` while the box shows. Pressed, it carries `.vc-shown`: the ring and the
  glyph come up to full strength and nothing else changes.
- **Pause** is `button.btn.vc-hold#voice-pause` and it reads Resume while the call is paused, for
  either reason. Pause closes the session and charges its seconds; Resume opens another carrying
  this call's turns. The button reads `call.isPaused()` rather than its own memory, because the
  call also pauses itself.
- **Hang up** is `button.btn.voice-stop#voice-stop`. It ends the call, paused or not.

**Waiting** is the call pausing itself. While an agent it delegated to owes an answer, nothing is
running, and neither side has spoken for `wait_pause_seconds`, the session closes: he is waiting,
not away, and an open session bills every second of the wait. The state line names the agents from
`call.waitingOn()` and `.vc-hint#voice-waiting` says "Talking wakes it, or the agent coming back."
Talking opens it again, the agent coming back opens it again. Silence never ends a paused call.

The clock says the minutes on this call and the minutes left today. It freezes while paused,
because `elapsed()` has no open session to count. The session ends itself at
`PLATFORM_VOICE_SESSION_MINUTES` and the voice says so two minutes before.

Hang up takes the focus when the overlay opens. App remembers which orb opened it and puts the
focus back there on close. A second tap on the orb while a call is running focuses Hang up rather
than starting a second call.

Nothing in here animates, so `prefers-reduced-motion` needs only `voice.css` to switch the
overlay's own fade off.

It renders one invisible `Delegated` per agent the voice gave work to this call. Each holds that
agent's socket through `useAgentSocket` and, on a settled turn or a new ask, calls `call.wake()`
with the agent and which of the two happened. They stay mounted across a pause, because `watched`
is what they are keyed on and a pause does not touch it; that is what lets a paused call hear an
agent finish.

App renders it only while it is open, rather than keeping it in the DOM with `hidden`. The
`.voice` rule sets `display:grid`, which would beat the `[hidden]` default.

## voiceSession

`import { createVoiceSession } from './shell/voiceSession'` (Voice.jsx is its only caller)

One call, from the microphone to OpenAI and back. The audio does not go through the daemon.

```js
const call = createVoiceSession()
await call.start()        // the door, the microphone, the offer, the answer
call.stop()               // says goodbye, waits for the final usage, charges it
call.pause()              // closes the session, banks the seconds, listens for his voice
call.resume(back)         // opens another one with this call's turns, and what woke it
call.setMuted(on)         // the microphone track off, the session left alone
call.waitOn(agent)        // that agent owes an answer, so a silence is a wait
call.cameBack(agent)      // it owes nothing now
call.waitingOn()          // the agent ids the call is waiting on
call.wake(back)           // {agent, kind: 'settled'|'ask', asking}: resume, or one line
call.isPaused()           // paused by the button or by the wait, the same thing
call.tell(text)           // one sentence into the call, spoken in the next gap
call.runTool(name, args)  // the Confirm and Cancel buttons
call.typed(text)          // a line he keyed in, as his words
call.image(text, dataUrl) // an image and any words: the vision model looks, the voice is told
call.mode()               // 'ask' or 'auto'
call.setMode(next)        // switches it, remembers it, tells the live model
call.elapsed()            // seconds so far, OpenAI's number while it has one
call.onState(fn)          // connecting, listening, thinking, looking, speaking, paused, waiting, ended, error
call.onTranscript(fn)     // {who: 'you'|'voice'|'note', text, partial, typed, image, at}
call.onProposal(fn)       // the proposal to draw, or null when it is settled
call.onDelegated(fn)      // an agent id, when a confirm sent work to it
```

`start()` is four steps in order: `POST /api/voice/session` for permission, `getUserMedia`, the
peer connection and the data channel named `oai-events`, then `POST /api/voice/offer` to swap the
SDP. The channel is created before the offer, or it is not in the offer at all.

It feeds `level.js` from two analysers, the microphone and her track, and takes the louder, so
the orb moves when either of you speaks.

Transcript fragments arrive with no end of turn, so this file decides: a speaker quiet for
`turn_gap_ms` has stopped, and the turn is written down with `POST /api/voice/log`. That number
shapes the written line and nothing else; it is not turn detection, which happens at OpenAI.

While paused there is no session, so `pause()` opens a local microphone instead. It reaches
nothing but the meter, so the orb still breathes and no second is billed, and the call resumes
when the level holds above a small threshold for about 300 ms. It is released on resume, on hang
up and on unmount, and a muted microphone is never opened.

Tool calls arrive from the backend model wrapped in a `response.event` envelope. The finished call
is read from the nested `response.output_item.done`, forwarded whole to `/api/voice/tool`, and the
result goes back as `response.item.create` with a `function_call_output`, then `response.create`.

`readMode()` is exported beside `createVoiceSession`, so the overlay can draw the right switch
on its first render without waiting for a session.

A session's `instructions` are immutable once it starts, so anything that depends on a switch he
can flip reaches the live model as `session.instructions.append` with `delegation_id: null`.
Three things use it: the mode on every `session.started`, a typed line, and Auto mode's "that
was sent already, say it in one line". GPT-Live has no other client event that carries words
into a conversation in progress.

In Auto mode a `propose` result is confirmed here before the model is answered, and what goes
back as the function call output is the confirm's answer rather than the proposal's. The model
never sees the "Shall I?" it would otherwise read out.

An insecure page fails with one sentence: "The microphone needs https. On the laptop use
localhost."

## still

`import still from './shell/still'`

```js
still()   // true when the reader has asked for no motion
```

One line over `matchMedia('(prefers-reduced-motion: reduce)')`. The fold timers, the scroll to
the needs list and the voice transcript all ask it, so the question is answered in one place.

## useGlider

`import useGlider from '../../shell/useGlider'`

```js
useGlider(ref, activeKey, options)
```

| argument | type | notes |
|---|---|---|
| `ref` | React ref | the element that holds the `.glider` span |
| `activeKey` | any | the hook re places the pill when this changes |
| `options.axis` | `'h'` or `'v'` | default `'h'` |
| `options.selector` | string | how to find the active child, default `'[aria-current="page"]'` |
| `options.parent` | boolean | measure the active child's parent instead, default `false` |

Returns the place function, if you want to call it after a layout change of your own.
It measures before paint, re measures on resize, and again once the fonts land.

The chat screen's Chat and Terminal subtabs use it like this:

```jsx
const subtabs = useRef(null)
useGlider(subtabs, sub, { selector: '[aria-selected="true"]' })

<div className="subtabs" role="tablist" aria-label="Agent view" ref={subtabs}>
  <span className="glider" aria-hidden="true"></span>
  <button type="button" role="tab" aria-selected={sub === 'chat'} onClick={() => setSub('chat')}>Chat</button>
  <button type="button" role="tab" aria-selected={sub === 'term'} onClick={() => setSub('term')}>Terminal</button>
</div>
```

---

# Primitives

Each file has one default export.

## Dot

`import Dot from '../../primitives/Dot'`

| prop | type | notes |
|---|---|---|
| `state` | `up`, `sleep`, `working`, `down` | default `sleep` |

Renders `<span className="dot up" aria-hidden="true">`. The stylesheet carries three looks.
`working` maps to `up`, because a working agent is a live agent and the mock shows it green.
`down` is red, and the stylesheet has it.

`waiting` is deprecated. The amber dot is gone from both mocks, so the value falls back to
`sleep` and the stylesheet no longer paints it. Do not use it in new code.

```jsx
<Dot state="up" />
```

## Badge

`import Badge from '../../primitives/Badge'`

| prop | type | notes |
|---|---|---|
| `kind` | `needs`, `ok`, `run`, `fail`, `quiet`, `master`, `work` | |
| `children` | node | the label |
| `id` | string | optional, for a badge the stylesheet names |
| `onClick` | function | optional. With it the badge is a `<button>`, not a `<span>` |

Two shapes under one name, because the mock marks two different things.

| kind | renders | meaning |
|---|---|---|
| `needs` | `span.badge.needs` | red, an agent wants you |
| `ok` | `span.badge.ok` | green, added lines |
| `fail` | `span.badge.fail` | red, removed lines or a failure |
| `quiet` | `span.badge.quiet` | grey, asleep or hidden |
| `master` | `span.ac-tag` | the square identity tag, not a pill |
| `work` | `span.ac-tag` | the same square tag |

`master` and `work` carry no colour of their own. The class is `.ac-tag`, which the mock uses for
the "master" and "work" labels beside an agent name. The CSS uppercases the text, so pass it
lower case, as the mock does.

A badge that takes you somewhere is a control, so `onClick` makes it a real button. Nothing on
the Fleet card uses that any more: its count is `.ac-count`, built in AgentCard itself.

```jsx
<Badge kind="needs">not connected</Badge>
<Badge kind="master">master</Badge>
<Badge kind="quiet">planned</Badge>
```

## Chip

`import Chip from '../../primitives/Chip'`

| prop | type | notes |
|---|---|---|
| `verb` | string | the tool name, rendered mono. With a verb the chip is `.chip.tool` |
| `children` | node | the rest of the line |
| `running` | boolean | accepted and ignored. It used to add the amber `.dot.waiting`, which is gone |

Without `verb` it is a plain `span.chip`, which the mock uses for "wakes on message", "7 days",
"turn 288" and the terminal bar.

```jsx
<Chip verb="Bash">python3 -m unittest, 320 passed</Chip>
<Chip verb="Bash" running>fetching 12 listings</Chip>
<Chip>wakes on message</Chip>
```

## Sparkline

`import Sparkline from '../../primitives/Sparkline'`

| prop | type | notes |
|---|---|---|
| `points` | array of numbers | at least one |
| `width` | number | default `280`, the viewBox width |
| `height` | number | default `56`, the viewBox height |
| `stroke` | `'accent'` or `'warn'` | default `'accent'`, `warn` paints it amber |
| `label` | string | becomes `aria-label` on the SVG |

Renders `svg.spark` with `preserveAspectRatio="none"` and `role="img"`, then `path.area`,
`path.line` and a `circle` at the last point, exactly the three shapes the mock draws. The stroke
width, colour and rounding all come from the stylesheet, so there is nothing to pass.
Geometry: 4px of side room, the highest value at y=10, the lowest at y=height minus 4. The area
closes down to the bottom edge.

```jsx
<Sparkline points={[6, 5, 8, 7, 10, 11, 14, 15]} label="Applications per week, rising from 6 to 14" />
<Sparkline points={[12, 10, 11, 7, 4, 1, 0, 0]} stroke="warn" label="Turns per day, falling to zero" />
```

## Receipt

`import Receipt from '../../primitives/Receipt'`

| prop | type | notes |
|---|---|---|
| `verdict` | string | "Approved", "Denied", "Skipped", "Answered" |
| `detail` | string | optional, rendered mono inside `<code>` |
| `negative` | boolean | default `false`, adds the `no` class, which paints the verdict red |

Renders `p.receipt` holding `span.verdict` and an optional `code`. Put it inside the ask card and
add `done` to that card, so the card folds to this one line.

```jsx
<section className="ask centre done" aria-labelledby="ask-bash">
  <Receipt verdict="Approved" detail="curl -s -m 5 -o /dev/null -w OK https://example.com" />
</section>
```

The stylesheet has the receipt rules now, so an answered card and an answered needs row both
fold to this one line. Wave one's gap is closed.

```jsx
<li className="needs-row done">
  <Receipt verdict="Denied" negative />
</li>
```

## DiffRows

`import DiffRows from '../../primitives/DiffRows'`

| prop | type | notes |
|---|---|---|
| `rows` | array of `{ kind, gut, src }` | `kind` is `hunk`, `context`, `added` or `removed` |

Renders `div.diff-body` and one `div.diff-row` per row, each holding `span.gut` and `span.src`.
`added` adds `add`, `removed` adds `del`, `hunk` adds `hunk`.

Two screens show the same rows, so they live here once. Insights wraps them in the framed
`.diff` with a `.diff-head`. The chat wraps them in the folded `.edit`.

```jsx
<div className="diff">
  <div className="diff-head">
    <span>src/broker.py</span>
    <Badge kind="ok">+4</Badge>
    <Badge kind="fail">-3</Badge>
  </div>
  <DiffRows rows={DIFF.rows} />
</div>
```

## IconButton

`import IconButton from '../../primitives/IconButton'`

| prop | type | notes |
|---|---|---|
| `label` | string | required, becomes `aria-label` |
| `children` | node | the SVG |
| `className` | string | optional, appended to `iconbtn`, for example `send`, `plus`, `keyb` |
| `onClick` | function | optional |
| any other prop | | passed to the `<button>`, for example `style` or `aria-pressed` |

Renders `button.iconbtn` with `type="button"`.

```jsx
<IconButton label="Send" className="send" onClick={send}>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 19V5M6 11l6-6 6 6"></path>
  </svg>
</IconButton>
```

---

# Notes for wave two

- SVG attributes in JSX are camel case: `strokeWidth`, `strokeLinecap`, `strokeLinejoin`,
  `clipPath`. `viewBox`, `cx`, `cy`, `rx`, `d` and `fill` stay as they are.
- `class` is `className`. `for` on a label is `htmlFor`. `hidden` is a real React prop and works.
- `aria-current` takes a string: `aria-current={active ? 'page' : undefined}`. Do not pass `false`,
  it renders as the string "false", which counts as set.
- Inline `style` is an object: `style={{ marginLeft: 'auto' }}`.
- The mock's `&middot;` is a plain `·` in JSX. `&times;` is `×`. `&#9679;` is `●`.
- Text with a stray `{` or `}` needs `{'{'}`, and a literal `>` inside a `<pre>` is fine as is.
- Fonts, the icon, the manifest, the service worker and the chat screenshot all sit in `public/`.
  Reference them from the root: `/fonts/inter-400.woff2`, `/icon.svg`, `/paste.jpg`.
- Anything in the mock's `app.js` that is one screen's behaviour belongs inside that screen as
  React state. Examples: the Desk toggle, the two step cron delete, the thinking tabs, the insight
  deck, the memory filters, the answered ask, the composer growing with the text.

---

# The agent page

The chat screen's markup carries the class names the stylesheet now uses. Keep them.

| element | class | why |
|---|---|---|
| the outer box | `.agent-page` | the rail and the column sit side by side above 720px |
| the column | `.agent-main` | it fills the width to the right edge |
| each tab panel | `.agent-panel` | a flex column, so the log can take the room that is left |
| the log box | `.chat-scroll`, `id="chat-scroll"` | the one scroller above 720px |
| the terminal footnote | `.term-note` | it stays put while the terminal scrolls |
| the switcher slot | `.ch-facedock` | it holds the strip's height when the strip floats on the phone |
| the face switcher | `.subtabs.ch-faces` | Chat, Terminal and the agent's pages, in one pill |

Above 720px the stylesheet drops the page width cap and hides the Design notes with
`main:has(#screen-chat:not([hidden]))`. React renders the active screen only, so the section is
there only on Chat and it carries no `hidden` attribute. The rule needs no help.

The chat screen scrolls `#chat-scroll` to the bottom on mount and whenever the Chat subtab comes
back, once at once and once inside `requestAnimationFrame`, because the log has no height until
the panel is laid out.

## The scroll rule

`src/screens/chat/Log.jsx`. **The log never moves the reader.** One rule, and every place that
used to set `scrollTop` now asks it first.

1. A scroll handler records how far the reader is from the bottom. Under 80px counts as at the
   bottom. That answer is the state before any content changed, because content changing does
   not scroll anything by itself.
2. New content goes to the bottom **only** when that answer was yes. A live delta, an ask card,
   a settled turn and a daemon note all obey it.
3. When the answer was no, nothing moves. A small pill, `button.ch-new` reading "New below",
   appears instead. Tapping it goes to the bottom, and it goes the moment the reader is there.
4. Three things may still jump: arriving on the screen, coming back to the Chat subtab, and a
   message the reader sent. The last one is the `sendTick` prop, a counter the Chat screen
   raises once per send.

Above 720px the log box is the scroller. Under it the box has no height of its own and the page
scrolls, so `Log` listens on `window` as well as on the box and measures whichever is moving.
The pill follows: sticky inside the box above 720px, fixed to the viewport under it, clear of
the composer and the tab bar, because `body` is a scroll container there and sticky has nothing
to stick to.

## The face switcher on the phone

`src/screens/chat/faces.js`. **The switcher behaves like a phone browser's toolbar.** In a long
conversation the strip sat at the top of the log, so reaching Terminal meant scrolling the whole
conversation back up. Now it follows the reader.

1. The strip sits in a slot, `div.ch-facedock`. The slot keeps the strip's 42px whatever the
   strip does, so the log never moves under a reader who is in the middle of it.
2. While the slot is on screen, or the page is within 40px of its top, the strip stays in the
   slot and is always shown. This is the old behaviour, unchanged.
3. Once the slot goes off the top of the screen the strip floats: `position: fixed`, 8px down
   from the top edge, the same shadow the "New below" pill uses.
4. A move down the page of more than 24px slides it away, `translateY` and opacity over 180ms.
   Any move up of more than 8px brings it back. The two numbers are far apart so a thumb that
   trembles cannot flicker it.
5. **Only the reader may move it.** The direction counts only while a wheel, a finger, a pointer
   or a key was seen in the last 300ms. The log scrolls the window itself on arrival, on coming
   back to the Chat tab and after a send, and the terminal shortens the page under the reader.
   None of that is somebody scrolling, and a switcher that vanished because the page changed
   height would read as a fault.
6. Changing the tab re-reads everything, so the strip is never hidden by the page settling after
   the tap that changed it.

The reading is done in a `requestAnimationFrame` off a passive scroll listener, and the hook
only sets state when the answer changes, so a scroll that changes nothing costs no render.

Above 720px none of it applies. `.ch-facedock` is `display: contents`, so the strip is still the
flex child app.css pins above the log, and the log is its own scroller there. The hook is quiet
behind the same 720px query. `prefers-reduced-motion` needs nothing of its own: app.css already
turns every transition off, so the strip goes and comes with no slide.

---

## The agent card

`src/screens/fleet/AgentCard.jsx`. **The whole card is the link.** It is an `<article>` with
`role="link"` and `tabIndex={0}`, and a click, Enter or Space all set `#/a/<id>` and call
`onScreen('chat')`. The focus ring is the stylesheet's own `:focus-visible`. `.fl-open` adds the
pointer and nothing else.

A card holds three things and no more:

| part | class | what it is |
|---|---|---|
| the header row | `.ac-top` | the status dot, the name, the identity tags, a spacer, then at most one indicator in `.ac-badges`. One row, never wrapping. The name carries `min-width:0` and an ellipsis |
| the last line it said | `.ac-last` | two lines at most, then an ellipsis |
| undelivered mail | `.fl-waiting` | a quiet line under the last message, only when `agent.waiting` is above zero. It opens the held messages dialog |

| indicator | class | when |
|---|---|---|
| the count of asks | `.ac-count` | a `<button>` with `aria-label="2 need you"` that scrolls to the Needs list. It goes at zero |
| working | `.ac-spin` | a 16px ring turning once a second, `role="img"` and `aria-label="Working"`. It does not turn under reduced motion |

An asleep agent carries no indicator. The dimmed card and the grey dot already say it.

**What the card no longer has.** The chip row (`awake`, the age, `server :8100`) and the `Open ›`
affordance are gone, and so is the Sleep, Hide, Forget row. The owner called them redundant: the
chips said again what the dot and the last line already say, Open is now the whole card, and the
life of an agent lives in one place, the kebab in the chat header. Waking an asleep agent
happens there too.

Every button inside the card stops the click from reaching the card, or reading the mail would
also open the chat behind the dialog.

AgentCard takes `agent`, `onOpen`, `count`, `onCount` and `onHeld`. Nothing else.

---

# The seven additions

What each one is, where it lives, and the class names it must keep.

## 1. Needs you, on Fleet

`src/screens/fleet/Needs.jsx`. Classes `.needs .needs-count .needs-list .needs-row .needs-who
.needs-what .needs-age .needs-acts .needs-empty`, plus `.done` and `.gone` on a row.

Approve folds the row to "Approved" with the command. Deny folds it to "Denied", red. Answer
folds it to "Opened in the chat" and calls `onScreen('chat')`, because the question card is
already waiting there.

**With nothing waiting the section is not drawn at all**: no heading, no count, no empty line.
The owner asked for that. Fleet still opens one socket per awake agent, so the section is back
the moment something is asked.

The rows come from App. See "The asks waiting on you" above.

## 2. The recap, at the top of the chat log

`src/screens/chat/Recap.jsx`. Classes `.recap .recap-kicker .recap-body .recap-next .recap-x`.
The x carries `aria-label="Dismiss the recap"`. Whether it is dismissed is Chat's own state.

## 3. Agent pages as sub tabs

one `.agent-panel` each. Only Personal Assistant has any. Picking another agent lands you on
its Chat, so a tab that agent does not have can never stay selected.

`src/screens/chat/PageFrame.jsx` is the frame both pages share: `.page-frame .pf-head .pf-name
.pf-tag .pf-open .pf-body`. Tasks adds `.tasks-filters .tasks-add .tasks-table .tasks-due`.
Week adds `.week .week-day .week-name .week-item .week-today`.

`useGlider` handles the longer strip with no change: `offsetLeft` is measured against
`.subtabs`, which is the offset parent, so the pill stays right while the strip scrolls
sideways on the phone.

## 4. The inline edit block

`.edit .edit-head .edit-file`, the chevron, `Badge ok` and `Badge fail`, and `DiffRows` inside.
Closed by default.

## 4a. The composer and its plus menu

`src/screens/chat/Composer.jsx`. The card stacks what you attached (`.ch-attach`), what you are
typing (`.ch-input`), the five phone keys when they are on (`.phonekeys.ch-keys`), and the
bottom row (`.ch-tools`).

**The bottom row is four things**: the round plus, the spacer `.ch-grow`, the round microphone
and the round Send. Nothing else. The permission mode select and the keyboard button are gone
from the row, and so is the disabled attach button.

**Send becomes Stop while the agent works.** When the socket's `state` is `thinking`, the same
`button.ch-round.ch-send` carries `aria-label="Stop"` and a small filled rounded square, and a
press calls `cc.stop(id)`. While the stop is in flight it is `aria-label="Stopping"`, disabled
and `aria-busy`, until the engine says idle or 4 s pass. No new classes.

**Escape on a desktop keyboard**, read in `index.jsx` on the chat tab only. It is ignored when the
focus is in a field that is not the composer, or a menu, a `<details>`, a `<dialog>` or an
`aria-modal` layer is open. A menu that takes an Escape calls `preventDefault()` so the chat does
not read it too.

| what is on screen | one Escape | two within 600 ms |
|---|---|---|
| the agent working, no card | stops the turn | stops the turn |
| a card waiting | nothing | skips the oldest card: a question is skipped, a permission is denied |

One Escape cannot stop while a card waits, because the stop ends the turn and takes the card with
it before the second press lands.

**Skip on the question card.** A quiet `btn quiet` at the left of each `.ask-foot`, where Deny
sits on the permission card. It posts `{id, allow: false}` for the whole ask and folds the card to
"Skipped", not red.

**The plus opens a menu.** `div.ch-plus-menu` with `role="menu"`, a popover above the button
inside `span.ch-plus-wrap`. It closes on a tap anywhere else and on Escape, and the focus goes
back to the plus.

| item | role | what it does |
|---|---|---|
| Attach image or file | `menuitem` | clicks a hidden `input[type=file]` with `accept="image/*,*/*"`. A chosen or pasted image draws the same thumbnail row at the top of the card. Nothing is uploaded: there is no route yet, and `PUT /api/agents/<id>/inbox/<name>` is the one it will use |
| Permission mode | three `menuitemradio` in a `group` | acceptEdits, plan, bypassPermissions. The current one carries `aria-checked`. Choosing one calls `api.setPermissionMode` |
| Model | six `menuitemradio` in a `group` | default, `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-6`, `sonnet`, `haiku`. Read from the agent record's `model`, saved with `cc.updateAgent(id, {model})`. "default" sends null |
| Keyboard keys | `menuitemradio` | turns the five phone keys row on and off |

A mode or a model the list has never heard of is added to it rather than hidden, so a switcher
never names the wrong one.

The kebab in the agent header holds Wake, Sleep, Stop task, Hide and Forget, and no longer holds
the permission mode. The mode lives in one place, the plus.

## 5. The files strip

`src/screens/chat/Files.jsx`. Classes `.files .files-label .file-chip .file-name .file-dl
.file-drop`. Driven by the agent's `files`, and it renders nothing for an agent with none.

The download button is a plain `<button className="file-dl">` and not `IconButton`, because
`.iconbtn` draws a 36px bordered box and `.file-dl` a 26px bare one. The two cannot share an
element. It still carries an `aria-label`.

## 6. Cords on Fleet

`src/screens/fleet/Cords.jsx`, an absolutely positioned `<svg className="cords">` over the
grid. Classes `.cord .cord-end .cord-plate .cord-label`, and `.corded` on both cards.

The geometry is measured, not written down. Fleet keeps a ref on `.fleet` and one ref per
card. A `useLayoutEffect` reads the three bounding boxes, turns them into grid space, and
builds the path:

- the cards overlap vertically, so they share a row: a cubic arc over their top edges, from
  72% across the left card to 28% across the right one, lifted 30px.
- they do not: one column, so a straight vertical line down the middle of the gap.

It measures again on resize, once the fonts land, and whenever the needs list changes, which
is what `redraw` is for. The list sits above the grid, so folding a row moves every card.

## 7. The orb and the voice overlay

`button.orb.orb-top` in TopBar, `button.orb.orb-mic` in the composer, and `canvas.orb.orb-big`
inside the overlay. The CSS decides which of the first two is on: the top one above 720px, the
composer one below, where it takes the microphone's seat. The Dictate button carries `.mic` so
the CSS can hide it there.

Both buttons call `onVoice()`, which opens one call however you reached for it. A second tap while
a call is running focuses Hang up rather than starting a second one. The overlay is
`src/shell/Voice.jsx` and App renders it.

---

# What the UI deliberately does not show

Two things the old console offers and this one does not. The owner asked for both to go from the
screen. **Nothing was deleted.** The daemon keeps every route and the old `web/` console keeps
every control, so the way to do either is still there.

| hidden | where it still lives | why it is not here |
|---|---|---|
| cords, the line between two agent cards | `GET/POST /api/cords`, `POST /api/cords/<id>/hangup`, and the drawing in `web/app.js` | the owner's instruction. `src/screens/fleet/Cords.jsx` stays in the tree and Fleet does not render it |

The model picker came back. The owner asked for it once it had a home that is not the bar: it is
one row inside the composer's plus menu, and that is its only home. `cc.updateAgent(id, {model})`
is the call, which is `PATCH /api/agents/<id>`. The model takes effect at the next wake, and the
chat says so in one quiet line after it is set.

---

# What each screen reads, and what is written down

A number on screen is either measured or written down, and a screen must never let one look like
the other.

## Integrations

| card | live | written down |
|---|---|---|
| Tailscale | the scope line, `window.location.host` | the badge, the use line |
| every other card | nothing | the badge, the scope, the use line |

Every written down scope line ends with `(static)`, and a note under the grid says the daemon has
no route that reports whether a service is connected, Telegram included. The Telegram bridge is
`commandcenter/telegram.py` and it publishes nothing in `/api/state` or `/api/health`, so its
card is written down like the rest. When a state route lands, give that card `live: true` in
`data.js` and feed it the real value. The identity sentence stays under the grid.

## Insights

Everything but the sparkline comes from `useFleet()`.

| thing | source |
|---|---|
| the three cards | `agents` and `jobs`: how many are awake and working, the tokens across the fleet, the job count and the last outcome |
| the usage table | one row per agent: `name`, `turns`, the four `tokens` counters added, `cost_usd`, `last_message_at` as a relative time |
| the sparklines | a flat placeholder, with the note "(history not yet recorded)" under each one |
| Widgets | a heading and the line "Agents will post here" |

Two honest labels. The table says lifetime, because the daemon keeps no monthly count. The money
column renames itself "Tokens out" when `cost_usd` is null, which is the case with no price list,
rather than showing an empty cell that would read as free.

The "Last change" diff is gone. The daemon records no last change, so there was no source for it.
`src/screens/insights/Diff.jsx` and the `DIFF` rows stay in the folder for the chat's use.

## Memory

There is no search route on the daemon. The screen keeps its search field, its filter chips and
its layout, says "Search over recaps is not wired yet", and keeps the five design cards behind a
"Show sample" button that labels them as samples. The Search button is disabled until there is
something to search. The future source is the away_summary rows, the recap an agent writes after
a turn you were not watching.
