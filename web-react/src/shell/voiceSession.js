/* One voice call, from the microphone to OpenAI and back.
 *
 * The audio does not go through the daemon. The browser holds a peer connection straight to
 * OpenAI, because a round trip through this laptop would add a hop to every syllable. What the
 * daemon keeps is everything that must not leave it: the key, the briefing, and the tools.
 *
 * GPT-Live has no ephemeral client secret, so this browser never holds a credential at all. It
 * hands its connection offer to the daemon, the daemon opens the session with the project key,
 * and what comes back is an SDP answer, which is an address rather than a permission.
 *
 * One call is four things in order:
 *
 *   1. ask whether a call may be made at all     POST /api/voice/session
 *   2. take the microphone and build the offer   getUserMedia, RTCPeerConnection, ICE
 *   3. swap the offer for an answer              POST /api/voice/offer
 *   4. talk over the data channel                events out, events in
 *
 * Step 1 is separate on purpose. It costs nothing and it answers before the microphone prompt,
 * so a console with no key says so rather than asking for a microphone it cannot use.
 *
 * GPT-Live is two models. The one you hear holds the conversation; a backend model does the
 * thinking and asks for the tools. So its events arrive on two levels: the session's own, and
 * the backend's wrapped in a response.event envelope. Both are read here.
 *
 * A call can be paused. Pausing closes the session at OpenAI and charges the seconds, exactly as
 * hanging up does, and leaves the overlay and the transcript where they are. Resuming opens a new
 * session carrying this call's own turns, so the two read as one conversation.
 *
 * A call also pauses itself while it waits on an agent. The long silences in a real call all came
 * straight after "I have sent that to personal-assistant": he is not away, he is waiting, and an
 * open session bills every second of the wait. So the session closes, and the call opens again
 * when the agent comes back or when he speaks. Nothing else ends a paused call.
 *
 * A call that is running and has nobody speaking for the silence setting asks to be hung up.
 * The overlay does the hanging up, by the same path as the button. Nothing working may be in
 * flight: no tool call, no image, no agent that owes an answer. A paused call is not running, so
 * the wait pause always wins over the silence.
 *
 * A call opens with what the inbox has not told yet. The daemon puts those items in the startup
 * instructions and sends them back with the answer; each gets a transcript row. After each of her
 * lines Jev says which untold items she conveyed, and after each of his what he wants done with
 * the told ones. The daemon writes every decision down; this only draws it.
 *
 * A page that goes away cannot wait for the goodbye. endNow() sends it, closes the connection at
 * once, and posts the seconds with a beacon, which the browser delivers after the page is gone.
 */

import {
  voiceSession, voiceOffer, voiceTool, saveAgentGreeting, voiceJudge, voiceLog, voiceEnd, voiceEndBeacon, voiceImage,
  voiceSmalltalk,
  inboxJudge, inboxAct,
} from '../data/api'
import { setVoiceLevel } from './level'
import { startRecording } from './callRecorder'
import { loadGreeting, greetingNow, greetingSoon, playGreeting, captureGreeting, gapRecorder } from './greeting'

/* The channel OpenAI listens on. The name is fixed by the API, and it must exist before the
 * offer is created or it is not in the offer at all. */
const CHANNEL = 'oai-events'

/* Where the call log cuts one line and starts the next: this much quiet from that speaker.
 *
 * It decides the shape of a written line and nothing else. It is NOT how the voice decides when
 * it may speak. That is turn detection, it happens in the audio stream at OpenAI, and the API
 * offers no setting for it at all. The two jobs used to share 1400 ms, which is a turn detection
 * number, and the log cut the owner mid sentence into three lines. The daemon sends the real
 * number on every call; this is the fallback when it does not.
 *
 * The docs are why this timer exists at all: "Transcript deltas have no item ID or event that
 * marks a completed conversational turn, so your application decides how to group them." */
const LOG_GAP = 2500

/* A line worth keeping has a letter or a digit in it. Speech recognition emits fragments that are
 * one full stop or one comma, carrying the punctuation of the sentence before them and nothing
 * else. Those are not turns and the log is better without them. */
const WORD = /[\p{L}\p{N}]/u

/* A call that is waiting on an agent pauses itself after this much quiet. The fallback, in
 * seconds; the daemon sends the real one, and zero turns it off. It only ever runs while at least
 * one agent owes an answer. */
const WAIT_PAUSE = 45

/* A running call hangs up after this much time with nobody speaking, in minutes. The fallback;
 * the daemon sends the real one from the Config page. */
const SILENCE_MINUTES = 8

/* Your voice waking a paused call. There is no session while paused, so this microphone is read
 * in this tab and nowhere else: nothing is sent and nothing is billed. The level has to stay up
 * for the hold, so a door closing does not open a call. */
const WAKE_LEVEL = 0.06
const WAKE_HOLD = 300

/* A question handed to the backend can be slow, and a slow answer is silence with work behind it.
 * Past this the delegation is treated as lost rather than as still working, so a backend that
 * never answers cannot hold a call open all night. */
const THINKING_MAX = 60000

/* How many of this call's turns a resumed session carries over. The daemon decides what to do
 * with them; this is only how many are worth sending. */
const RESUME_TURNS = 12

/* How long to wait for the connection candidates, and for the goodbye. Both are the doc's own
 * numbers. */
const ICE_WAIT = 10000
/* How long the offer waits for a public route once it has any route at all. */
const ICE_EARLY = 800
const CLOSE_WAIT = 15000

/* How long the typed box waits for a resumed session to say it has started. An instruction sent
 * before that is sent into nothing. */
const STARTED_WAIT = 4000

/* The referee. After each finished line of his while a card is showing, Jev says whether it was a
 * yes to the card as read, a yes with a change, a no, or none of those. The console acts only at
 * or above REPLY_AT. Five real judgements of his own phrasings came back at 0.99 to 1.0, so 0.8
 * leaves a wide margin and still drops the unsure ones.
 *
 * After each finished line of hers, Jev says whether she claimed an action was done. At or above
 * CLAIM_AT, with nothing sent, she is told so. A real send in the last CLAIM_GRACE is what she is
 * reporting, so her lines then are not judged at all.
 *
 * A confirm she calls herself while his answer is still being judged waits up to REFEREE_WAIT for
 * the judgement, so "yes but add initech" can never send the unchanged proposal. */
const REPLY_AT = 0.8
const CLAIM_AT = 0.8
const CLAIM_GRACE = 15000
const REFEREE_WAIT = 3000

/* The quiet line each inbox decision draws in the transcript. Jev decides these in the daemon, at
 * 0.8 or above; the words are only what the transcript shows. */
const INBOX_NOTES = {
  handled: 'Marked handled.',
  later: 'Kept for later.',
  dismissed: 'Dismissed.',
  opened: 'Opened the chat.',
  replaced: 'Replaced by the newer one.',
}

/* Which mode the delegation control is in, remembered between calls. Ask reads a proposal back
 * and waits; Auto confirms it here, at once. */
const MODE_KEY = 'cc.voice.mode'

/* Every read and write of browser storage is wrapped. A private window throws on the accessor
 * itself, and a call that cannot start because storage is blocked is a bad trade for a
 * remembered switch. */
export function readMode() {
  try {
    return window.localStorage.getItem(MODE_KEY) === 'auto' ? 'auto' : 'ask'
  } catch {
    return 'ask'
  }
}

function writeMode(next) {
  try { window.localStorage.setItem(MODE_KEY, next) } catch { /* storage is off */ }
}

/* What the live model is told about the mode, once per session. The session `instructions` are
 * immutable after the session starts, so anything that depends on a switch he can flip has to
 * arrive as an appended instruction instead. */
/* How long the call is quiet after a task goes out before she makes small talk. Under the wait
   pause, which closes the session after 45 seconds of quiet. */
const SMALLTALK_AFTER = 12000

function modeLine(mode, agentCall) {
  if (mode === 'auto') {
    return agentCall
      ? 'Mode is Auto: propose puts it in your chat at once. Never ask whether to. '
        + 'Say it is noted in one line.'
      : 'Delegation mode is Auto: propose sends at once. Never ask Shall I. '
        + 'Say Sent to X in one line.'
  }
  return agentCall
    ? 'Mode is Ask: propose, read the readback to him, wait for his yes.'
    : 'Delegation mode is Ask: propose, read it back, wait for yes.'
}

export const INSECURE = 'The microphone needs https. On the laptop use localhost.'

function secure() {
  return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
}

/* One analyser per audio source. The orb follows whichever of you is louder, so it moves when
 * she speaks as well as when you do. `mine` says whose it is, because a muted microphone must
 * leave the orb following her alone. */
function meter(ctx, stream, mine) {
  const node = ctx.createAnalyser()
  node.fftSize = 512
  ctx.createMediaStreamSource(stream).connect(node)
  const buf = new Uint8Array(node.fftSize)
  return {
    mine: !!mine,
    read: function read() {
      node.getByteTimeDomainData(buf)
      let sum = 0
      for (let i = 0; i < buf.length; i++) {
        const d = (buf[i] - 128) / 128
        sum += d * d
      }
      return Math.min(1, Math.sqrt(sum / buf.length) * 3.2)
    },
  }
}

/* The offer needs at least one route in it: sent with none, the answer connects to nothing. But
 * waiting for every route is slow on a phone, which checks wifi, mobile data and IPv6 one after
 * another. So it goes as soon as a public route is known, or after ICE_EARLY with any route,
 * and only a phone with no route at all waits the full ICE_WAIT. */
function gathered(pc) {
  if (pc.iceGatheringState === 'complete') { return Promise.resolve() }
  return new Promise(function (done, fail) {
    let routes = 0
    let over = false
    function finish() {
      if (over) { return }
      over = true
      clearTimeout(timer)
      clearTimeout(early)
      pc.removeEventListener('icegatheringstatechange', look)
      pc.removeEventListener('icecandidate', seen)
      done()
    }
    const timer = setTimeout(function () {
      if (routes) { finish(); return }
      over = true
      pc.removeEventListener('icegatheringstatechange', look)
      pc.removeEventListener('icecandidate', seen)
      fail(new Error('The connection could not be set up in time.'))
    }, ICE_WAIT)
    const early = setTimeout(function () { if (routes) { finish() } }, ICE_EARLY)
    function look() {
      if (pc.iceGatheringState === 'complete') { finish() }
    }
    function seen(ev) {
      if (!ev.candidate) { finish(); return }
      routes += 1
      if (/ typ srflx /.test(ev.candidate.candidate || '')) { finish() }
    }
    pc.addEventListener('icegatheringstatechange', look)
    pc.addEventListener('icecandidate', seen)
  })
}

/* Said to the model when the recorded greeting has already played, so he does not greet twice. */
/* How long a call waits for the greeting clip when the page has only just loaded. */
const GREETING_WAIT = 350
/* When the greeting starts after the tap. Long enough for the phone's switch to call audio. */
const GREETING_DELAY = 700
/* After end_call: the call ends once the goodbye has been quiet this long, or at the cap. The
   transcript runs ahead of the sound, so the quiet is long enough for the last word to play. */
const ENDING_QUIET_MS = 3000
const ENDING_CAP_MS = 10000

const GREETED = 'The greeting "Welcome back, sir." has already been played to him in your voice as '
  + 'the call opened. Do not greet him again and do not say welcome back. Go on from there.'

/* `opts.agent` makes this a call the owner is placing to that agent, from its chat. No ring and no
   recorded line: the door and the offer name it "agent:<id>", and the agent greets him live. */
export function createVoiceSession(opts) {
  const target = (opts && opts.agent) || null
  loadGreeting()
  const stateFns = new Set()
  const turnFns = new Set()
  const proposalFns = new Set()
  /* Which agents were given work on this call, so the overlay can watch their sockets and tell
     the voice when one of them finishes or stops to ask something. */
  const delegatedFns = new Set()
  /* An inbox item he wants to go into: the overlay minimises and opens that agent's chat. */
  const openFns = new Set()

  let pc = null
  let channel = null
  let mic = null
  let audio = null
  let ctx = null
  // This session's recording, when Config has it on, and the session it belongs to.
  let recorder = null
  let recordOn = false
  let recordSession = ''
  // Restarts her voice when the page is shown again. Set with the audio, cleared at tear down.
  let unhide = null
  let frame = null
  let smoothed = 0
  let meters = []
  let state = 'idle'
  let opened = 0
  let info = null
  let closing = null
  let gone = false
  /* Set once the call is hung up, and never cleared: one session object is one call. A Hang up
   * while the call is still connecting lands here, and start() stops at its next step instead of
   * opening a session that nobody holds. `quitWhy` goes to the daemon's log. */
  let quit = false
  let quitWhy = ''

  /* What OpenAI says the session has cost so far, in seconds. It is the billed number, so it
   * beats the wall clock, which counts the time the tab spent connecting. `banked` is every
   * session before this one on the same call: a pause charges what it closes, and the clock has
   * to keep reading what the whole call has cost. */
  let charged = 0
  let banked = 0

  /* The two call states. Muted disables the microphone track and nothing else, so the session
   * stays open and she keeps talking. Paused has closed the session and is waiting to open
   * another, whether he tapped Pause or the call paused itself on a wait. */
  let muted = false
  let paused = false
  let pausing = false

  /* The quiet clock. `heardAt` is when either of you last produced speech, `thinkingAt` is when
   * the backend was last handed a question, and `tools` is how many tool calls are in flight. A
   * silence while she thinks or a tool runs is work, not a wait. */
  let heardAt = 0
  let thinkingAt = 0
  let tools = 0
  /* How many images are with the vision model right now. Work, like a tool call. */
  let looking = 0

  /* Which agents owe an answer, by id, and when each was handed the work. While this holds
   * anything, a long quiet means he is waiting rather than gone, and the call pauses itself. */
  const waiting = new Map()
  /* One small talk line per task sent: chatted says it has been used, chatting that it is on
     its way. */
  let chatted = true
  let chatting = false
  let waitPauseSeconds = WAIT_PAUSE

  /* Every agent given work on this call, by id, and when the confirm went out, in seconds. Not
     cleared when an agent comes back: a resume sends the lot, and the daemon reads how each one
     stands at that moment, so she never has to answer "has he replied" from memory. */
  const delegatedAll = new Map()

  /* The came-back facts she has not said yet, oldest first. A pause can land between the news
     and her saying it, and the news must not go with the session. A fact is told only once her
     next line starts after the words were handed to her; `reporting` is what was handed over,
     and `midLine` says she was already speaking then, so the rest of that line is not it. Every
     resume carries whatever is still here. Only a hang up clears it. */
  let untold = []
  let reporting = null
  let midLine = false
  /* The daemon's sentence for what came back, from the offer answer, sent once the session has
     started. */
  let pendingReport = ''

  /* The inbox items this call was opened with, by id: {id, agent, text, urgency, row, status}.
     Status is untold, told or closed, as far as this call knows. The daemon holds the truth; this
     is what the transcript rows draw and which ids go to Jev after each line. Kept across a
     pause, because a resume is the same call. `briefSay` is the daemon's line for a resumed
     session, so she says the untold ones first. `lastHer` is her last finished line, for the
     judgement of his answer. `toldWait` is the told judgement in flight, which his answer waits
     for, so "done" straight after she reads an item is judged against it. */
  const brief = new Map()
  let briefSay = ''
  /* A fresh call. The persona says she opens it, and the startup prompt holds any untold items,
     but a prompt alone does not make her speak first, so she is asked for a turn. */
  let briefOpen = false
  /* The recorded greeting: whether it played on this call, when it finished, the recording of
     the microphone from then until the call is up, and the capture of his real greeting on a
     call that has no recording yet. */
  let greeted = false
  let greetOver = Promise.resolve()
  let gap = null
  let sessionUp = false
  let capturer = null
  let lastHer = ''
  let toldWait = Promise.resolve()
  /* An agent's call. When an agent rang and this is the answer, the door says who it is, and
     from then on the call is that agent's: its opening line plays instead of the front desk's greeting,
     every line is written to that call's record, and its tools carry the session id the daemon
     tied to it. Null on a front desk call, and then nothing below changes. The agent's session opens
     already knowing its recorded greeting played, so nothing is appended for it; `lastSession` is the session
     to name when a paused call is hung up and there is no session open to close. */
  let caller = null
  let lastSession = null
  let openingTurn = ''
  const callerFns = new Set()
  function callerId() { return caller ? caller.call : null }

  /* Where the greeting being taken ends: "sir" for the front desk, the agent's own name for an agent. */
  function greetingDone(text) {
    if (!caller) { return /\bsir\b/i.test(text) }
    const name = String(caller.name || caller.agent || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    return !!name && new RegExp('\\b' + name + '\\b', 'i').test(text)
  }

  /* A tool call to the daemon. An agent's call names its session, so its confirm and cancel
     reach that call's own proposals and never the front desk's. */
  function callTool(name, args) {
    return voiceTool(name, args, caller && info ? info.session_id : null)
  }
  let waitTimer = null

  /* The silence hang up. `busyAt` is the last second something was working or an agent owed an
   * answer, so the silence counts from the later of that and the last word. It is kept apart from
   * `heardAt` because the wait pause reads that one. `silenced` says the ask has gone out once. */
  let silenceMs = SILENCE_MINUTES * 60000
  let busyAt = 0
  let silenced = false
  const endFns = new Set()

  /* The local microphone that listens for him while the call is paused. It reaches no session,
   * so it costs nothing; it feeds the orb through the same meter and says when he starts
   * talking. */
  let wakeMic = null
  let wakeCtx = null
  let wakeTimer = null
  let wakeSince = 0
  let wakeWanted = false

  /* Where a log line is cut. The daemon's number, or the fallback above. */
  let gapMs = LOG_GAP

  /* Ask or Auto, and it outlives the call. */
  let mode = readMode()

  /* Resolved when the session says it has started. The typed box waits on it: a resume returns
   * as soon as the answer lands, and an instruction sent before the session is going is lost. */
  let started = null
  let startedDone = null

  /* The turn each of you is part way through, and the row it is drawn on. Fragments are appended
   * exactly as they arrive, spaces and repeats and all.
   *
   * Both of you can have a line open at once, which is why each carries its own row id. He pauses
   * mid sentence, she starts answering the fragment, he carries on: his next fragment belongs to
   * the line he already started, not to a new one. That is the whole of the turn splitter bug. */
  const turnOf = {
    you: { id: 0, text: '', timer: null, live: false, startedAt: 0 },
    voice: { id: 0, text: '', timer: null, live: false, startedAt: 0 },
  }
  let rowId = 0

  /* This call's finished turns, for a resume. Only what was written down, so a line dropped as
   * punctuation is not carried over either. */
  const callTurns = []

  /* The card on screen, as the daemon proposed it, with `shownAt`. Only a line of his that
   * started after the card appeared is judged against it: the request that led to the card is
   * not an answer to it. */
  let card = null
  /* Confirms that are on their way to the daemon, and the ones it said were sent, by proposal
   * id. A tap, his spoken yes through Jev, and her own confirm can all race for one card. The
   * daemon sends once whatever happens; these keep the losers quiet. */
  const inFlight = new Map()
  const sentIds = new Set()
  let lastSendAt = 0
  let lastProposalAt = 0
  /* One judgement per card and line, so the judgement her confirm waits on is the same request
   * the finished line started. */
  const judged = new Map()

  function say(next) {
    if (state === next) { return }
    state = next
    stateFns.forEach((fn) => fn(next))
  }

  /* One row of the transcript. `who` is "you", "voice", or "note" for what the console did.
   * `typed` marks the line he keyed in rather than said. */
  function turn(who, text, partial, id, typed, image) {
    turnFns.forEach((fn) => fn({
      who, text, partial: !!partial, id, at: Date.now(), typed: !!typed, image: !!image,
    }))
  }

  /* A row that is neither his nor hers: the console saying a task went out, or did not. It is
   * drawn quietly and written to the call log under its own name, so the next call's briefing
   * reads it as a fact rather than as something one of them said. */
  function note(text) {
    if (!text) { return }
    const id = ++rowId
    turn('note', text, false, id)
    /* An older daemon does not know the name and files it as hers. It is still written down,
     * which is the part that matters, so nothing here depends on the answer. */
    voiceLog('note', text, callerId()).catch(() => {})
  }

  /* Somebody said something. Any speech from either side starts the quiet clock again. */
  function heard() {
    heardAt = Date.now()
  }

  /* A fragment lands. The line is redrawn at once and written down once that speaker has been
   * quiet for the gap. The other one speaking does not close it: she answers the moment he pauses
   * for breath, and closing his line there is what cut one question into three. */
  function fragment(who, delta) {
    if (!delta) { return }
    heard()
    const row = turnOf[who]
    if (!row.live) {
      row.id = ++rowId
      row.text = ''
      row.live = true
      row.startedAt = Date.now()
    }
    row.text += delta
    turn(who, row.text, true, row.id)
    if (row.timer) { clearTimeout(row.timer) }
    row.timer = setTimeout(function () { flush(who) }, gapMs)
  }

  function flush(who) {
    const row = turnOf[who]
    if (row.timer) { clearTimeout(row.timer); row.timer = null }
    if (!row.live) { return }
    row.live = false
    const text = row.text.trim()
    row.text = ''
    /* Nothing at all, or nothing but punctuation. The row goes off the screen and no empty turn
       is ever posted to the log. */
    if (!text || !WORD.test(text)) {
      turn(who, '', false, row.id)
      return
    }
    turn(who, text, false, row.id)
    callTurns.push({ who, text })
    /* A failed write is not worth interrupting a conversation for. */
    voiceLog(who, text, callerId()).catch(() => {})
    /* The referee, off the audio path: a request that answers later, and nothing waits on it. */
    if (who === 'you') {
      judgeReply(text, row.startedAt)
      judgeInbox(text)
    } else {
      judgeClaim(text)
      lastHer = text
      judgeTold(text)
    }
  }

  /* One inbox item's transcript row, drawn again whenever its status changes. */
  function briefRow(item) {
    turnFns.forEach((fn) => fn({
      who: 'brief', text: item.text, agent: item.agent, item: item.id, status: item.status,
      urgency: item.urgency, partial: false, id: item.row, at: Date.now(),
    }))
  }

  /* The untold items the daemon opened this session with. A new one gets a row; one already
     held is untold again, because the daemon only sends what is waiting. */
  function takeBrief(list) {
    for (const got of Array.isArray(list) ? list : []) {
      if (!got || !got.id) { continue }
      let item = brief.get(got.id)
      if (!item) {
        item = { id: got.id, agent: got.agent || '', text: got.text || '', urgency: got.urgency, row: ++rowId }
        brief.set(got.id, item)
      }
      item.status = 'untold'
      briefRow(item)
    }
  }

  function briefIds(status) {
    return Array.from(brief.values()).filter((i) => i.status === status).map((i) => i.id)
  }

  /* After her line: did she convey any untold item? Jev answers in the daemon, which marks the
     yeses told. Off the audio path; nothing waits on it but his next answer. */
  function judgeTold(text) {
    const ids = briefIds('untold')
    if (!ids.length) { return }
    toldWait = inboxJudge({ moment: 'told', line: text, ids }).then(function (got) {
      for (const d of (got && got.decisions) || []) {
        const item = brief.get(d.id)
        if (item && item.status === 'untold') { item.status = 'told'; briefRow(item) }
      }
    }).catch(() => {})
  }

  /* After his line: what does it mean for each item told on this call? Handled, later,
     dismissed, open, or nothing. The daemon applies the event; this draws the note and opens
     the chat for an open. */
  function judgeInbox(text) {
    const desk = lastHer
    toldWait.then(function () {
      const ids = briefIds('told')
      if (!ids.length) { return null }
      return inboxJudge({ moment: 'reply', line: text, ids, desk })
    }).then(function (got) {
      for (const d of (got && got.decisions) || []) {
        const item = brief.get(d.id)
        if (!item || item.status === 'closed') { continue }
        item.status = 'closed'
        briefRow(item)
        note(INBOX_NOTES[d.decision] || '')
        if (d.decision === 'opened') { openFns.forEach((fn) => fn(item.agent)) }
      }
    }).catch(() => {})
  }

  /* A tap on an item's row. Marked opened, and the chat opens. */
  function openItem(id) {
    const item = brief.get(id)
    if (!item) { return false }
    if (item.status !== 'closed') {
      item.status = 'closed'
      briefRow(item)
      inboxAct(id, 'opened', 'tap').catch(() => {})
      note(INBOX_NOTES.opened)
    }
    openFns.forEach((fn) => fn(item.agent))
    return true
  }

  /* The daemon says a newer item from the same agent replaced one of this call's. */
  function replacedItem(id) {
    const item = brief.get(id)
    if (!item || item.status === 'closed') { return }
    item.status = 'closed'
    briefRow(item)
    note(INBOX_NOTES.replaced)
  }

  function flushAll() {
    flush('you')
    flush('voice')
  }

  function send(event) {
    if (channel && channel.readyState === 'open') {
      channel.send(JSON.stringify(event))
      return true
    }
    return false
  }

  /* An instruction, straight to the live model. The session's own `instructions` are immutable
   * once the session starts, so this is the only way words reach the live model afterwards: the
   * mode on every session, and the lines he types. The cap is the API's own, 500 tokens, which
   * is about 1500 characters. */
  function instruct(text) {
    if (!text) { return false }
    return send({
      type: 'session.instructions.append',
      delegation_id: null,
      content: String(text).slice(0, 1500),
    })
  }

  /* The orb follows the loudest of the two of you, smoothed so it does not flicker. Rising fast
   * and falling slow, because a voice that stops should leave the orb settling rather than
   * snapping shut. A muted microphone is skipped, so the orb follows her alone. */
  function watchLevels() {
    function tick() {
      let loudest = 0
      for (const m of meters) {
        if (muted && m.mine) { continue }
        loudest = Math.max(loudest, m.read())
      }
      smoothed += (loudest - smoothed) * (loudest > smoothed ? 0.45 : 0.12)
      setVoiceLevel(Math.max(0, Math.min(1, smoothed)))
      frame = window.requestAnimationFrame(tick)
    }
    frame = window.requestAnimationFrame(tick)
  }

  /* True while the backend is working. A question handed over and a tool still running are both
   * silence with something happening behind it, and neither is a wait on an agent. */
  function working() {
    if (tools > 0 || looking > 0) { return true }
    return !!thinkingAt && Date.now() - thinkingAt < THINKING_MAX
  }

  /* An agent has been given work on this call, so from now on a long quiet is him waiting. */
  function waitOn(agent) {
    if (agent) { waiting.set(agent, Date.now()) }
  }

  /* That agent has answered, or has stopped to ask, so it owes nothing any more. */
  function cameBack(agent) {
    if (agent) { waiting.delete(agent) }
  }

  /* One fact for her to say. The newest fact per agent wins, so a settle after an ask is said
     as the settle. */
  function keepUntold(back) {
    if (!back || !back.agent) { return }
    untold = untold.filter(function (f) { return f.agent !== back.agent }).concat([{ ...back }])
  }

  /* The words for these facts have just gone to her. They count as told when her next line
     starts. */
  function handedOver(facts) {
    if (!facts.length) { return }
    reporting = (reporting || []).concat(facts)
    midLine = turnOf.voice.live
  }

  /* She has started a line. If it started after the report went over, the report is said. */
  function spoke(newLine) {
    if (!reporting) { return }
    if (midLine && !newLine) { return }
    const said = reporting
    reporting = null
    midLine = false
    untold = untold.filter(function (f) { return !said.includes(f) })
  }

  /* Who the call is waiting on, for the overlay's state line. */
  function waitingOn() {
    return Array.from(waiting.keys())
  }

  /* One agent handed work: the wait starts here, and the overlay is told so it can watch that
     agent's socket. Both the spoken yes and the tapped Confirm come through this. */
  function delegated(agent) {
    if (!agent) { return }
    chatted = false
    waitOn(agent)
    delegatedAll.set(agent, Date.now() / 1000)
    delegatedFns.forEach((fn) => fn(agent))
  }

  /* The call pauses itself while it waits. Three things have to be true together: an agent owes
     an answer, nothing is running here, and neither of you has said anything for the wait. Then
     the session closes and its seconds are charged, exactly as the Pause button does. */
  function waitTick() {
    if (!pc || paused || pausing || !(waitPauseSeconds > 0)) { return }
    if (waiting.size === 0) { return }
    if (working()) { heard(); return }
    if ((Date.now() - heardAt) / 1000 < waitPauseSeconds) { return }
    pause('wait').then(function () { say('waiting') }).catch(() => {})
  }

  /* Nobody has spoken for the silence setting, and nothing is working. The call asks the overlay
     to hang up, once. A paused call has no session and is left alone. */
  function silenceTick() {
    if (!pc || paused || pausing || silenced || !(silenceMs > 0)) { return }
    if (waiting.size > 0 || working()) { busyAt = Date.now(); return }
    if (Date.now() - Math.max(heardAt, busyAt) < silenceMs) { return }
    silenced = true
    endFns.forEach((fn) => fn('silence'))
  }

  /* Small talk. Once per task sent, when an agent owes an answer and the call has been quiet
     for SMALLTALK_AFTER, she says one "by the way, did you hear" line. The daemon picks the item
     and writes the words; the browser only knows when. A reply or a pause ends the chance. */
  function smalltalkTick() {
    if (!pc || paused || pausing || chatted || chatting) { return }
    if (waiting.size === 0 || working()) { return }
    if (Date.now() - heardAt < SMALLTALK_AFTER) { return }
    chatting = true
    voiceSmalltalk().then(function (got) {
      chatted = true
      if (!pc || paused || waiting.size === 0 || !got || !got.say) { return }
      if (Date.now() - heardAt < SMALLTALK_AFTER) { return }
      if (instruct(got.say)) { send({ type: 'response.create' }) }
    }).catch(() => { chatted = true }).finally(() => { chatting = false })
  }

  function everySecond() {
    smalltalkTick()
    waitTick()
    silenceTick()
  }

  /* What one confirm did, drawn and written down. Twice in one night she said "sent" and nothing
   * had been, and there was no way to tell from the screen. Now the console says it itself, and
   * says it whichever way the confirm was reached. */
  function noteSend(result, fallback) {
    // A stop is not a send. The daemon writes its sentence, "Stopped X." or why it could not.
    if (result && result.stopped) { note(result.said || `Stopped ${result.stopped}.`); return }
    const who = (result && result.agent) || fallback || ''
    // An agent's call puts it in that agent's own chat; it is not sent anywhere else.
    if (result && result.ok && caller) { note('Put in the chat.'); return }
    if (result && result.ok) { note(`Sent to ${who || 'the agent'}.`); return }
    note(who ? `Nothing was sent to ${who}.` : 'Nothing was sent.')
  }

  /* The card, shown or taken away. Every path that changes it comes through here, so the referee
   * always knows which proposal is on screen. */
  function showCard(next) {
    card = next ? { ...next, shownAt: Date.now() } : null
    if (next) { lastProposalAt = Date.now() }
    proposalFns.forEach((fn) => fn(next ? { ...next } : null))
  }

  /* How many confirms for one card are on their way. Counted, because two can be. */
  function hold(id) { if (id) { inFlight.set(id, (inFlight.get(id) || 0) + 1) } }
  function release(id) {
    if (!id) { return }
    const left = (inFlight.get(id) || 0) - 1
    if (left > 0) { inFlight.set(id, left) } else { inFlight.delete(id) }
  }

  /* A confirm the daemon answered, after release(). `ok` marks the send. A failure while
   * another path has sent the same card, or is still sending it, is that path winning the race:
   * it says nothing, and the winner draws what happened. */
  function settledConfirm(id, result) {
    if (result && result.ok) {
      if (id) { sentIds.add(id) }
      lastSendAt = Date.now()
      return false
    }
    return !!(id && (sentIds.has(id) || inFlight.has(id)))
  }

  /* She is not mid line, so she can be asked to speak. A response asked for while she speaks is
   * a second answer on top of the first. */
  function speakUp() {
    if (!turnOf.voice.live) { send({ type: 'response.create' }) }
  }

  /* One judgement of his line against one card. The same request is reused for the same pair,
   * and a failure is unrelated, so nothing ever acts on one. */
  function judgeOf(id, text) {
    const key = id + '\n' + text
    if (!judged.has(key)) {
      judged.set(key, voiceJudge({ kind: 'reply', proposal_id: id, line: text })
        .catch(function () { return { decision: 'unrelated', confidence: 0 } }))
    }
    return judged.get(key)
  }

  /* His line that answers the card in `c`, if there is one yet: the line in flight, or the last
   * finished one, as long as it started after the card appeared. */
  let lastYou = null
  function answerTo(c) {
    const row = turnOf.you
    if (row.live && row.startedAt >= c.shownAt && WORD.test(row.text)) { return row.text.trim() }
    if (lastYou && lastYou.startedAt >= c.shownAt) { return lastYou.text }
    return ''
  }

  /* A finished line of his while a card is showing. Ask mode only: Auto has already sent it. */
  function judgeReply(text, startedAt) {
    lastYou = { text, startedAt: startedAt || Date.now() }
    if (mode === 'auto' || !card || !card.id || !pc) { return }
    if ((startedAt || Date.now()) < card.shownAt) { return }
    const c = card
    judgeOf(c.id, text).then(function (got) {
      /* The card may have gone, been answered by a tap, or been replaced while Jev read it. A
         judgement of a card that is no longer on screen is a judgement of nothing. */
      if (quit || mode === 'auto' || !card || card.id !== c.id) { return }
      if (inFlight.has(c.id) || sentIds.has(c.id)) { return }
      if (!got || !(Number(got.confidence) >= REPLY_AT)) { return }
      if (got.decision === 'confirmed') {
        runTool('confirm', { id: c.id, agent: c.agent }, 'yes').catch(() => {})
      } else if (got.decision === 'modified') {
        runTool('cancel', { id: c.id }, 'change').then(function () {
          instruct(`The owner changed the proposal: ${text}. It was not sent. `
            + 'Propose again with his change and read it back.')
          speakUp()
        }).catch(() => {})
      } else if (got.decision === 'cancelled') {
        runTool('cancel', { id: c.id }, 'no').catch(() => {})
      }
    })
  }

  /* A finished line of hers. If she says something was done and nothing was, she is told, and
   * the transcript says so. A real send a moment ago is what she is reporting, and is left be. */
  function judgeClaim(text) {
    /* An agent's call sends through its own tools and has no proposals to claim. */
    if (caller) { return }
    if (!pc || Date.now() - lastSendAt < CLAIM_GRACE) { return }
    const before = lastSendAt
    voiceJudge({ kind: 'claim', line: text }).then(function (got) {
      if (quit || !pc || lastSendAt !== before) { return }
      if (!got || !got.claimed || !(Number(got.p) >= CLAIM_AT)) { return }
      /* Nothing sent since the newest proposal, or nothing sent on this call at all. */
      if (!card && lastSendAt && lastSendAt >= lastProposalAt) { return }
      instruct(card
        ? 'You have not sent anything. The card is still waiting for the owner.'
        : 'You have not sent anything. Propose it and read it back first.')
      speakUp()
      note('Not sent yet.')
    }).catch(() => {})
  }

  /* Her own confirm, in Ask mode. If his answer to this card is in hand or in flight, it is
     judged first; a change or a no stops the confirm here, and the card is dropped. Anything
     else, a yes, a line Jev could not read, or no answer within REFEREE_WAIT, lets it through,
     because the daemon's rule still stands behind it: only a proposed id sends. */
  async function refereeFirst(id) {
    if (mode === 'auto' || !card || card.id !== id) { return null }
    const said = answerTo(card)
    if (!said) { return null }
    const got = await Promise.race([
      judgeOf(id, said),
      new Promise(function (done) { window.setTimeout(function () { done(null) }, REFEREE_WAIT) }),
    ])
    if (!got || !(Number(got.confidence) >= REPLY_AT)) { return null }
    if (got.decision !== 'modified' && got.decision !== 'cancelled') { return null }
    try { await callTool('cancel', { id }) } catch { /* the card goes either way */ }
    if (card && card.id === id) { showCard(null) }
    if (got.decision === 'modified') {
      note('Changed. Not sent; ' + ((caller && caller.name) || 'The front desk') + ' will propose again.')
      return { ok: false, said: `Not sent. The owner changed the proposal: ${said}. `
        + 'Propose again with his change and read it back.' }
    }
    note('Cancelled.')
    return { ok: false, said: 'Not sent. The owner said no, so it was dropped.' }
  }

  /* Auto mode. The proposal is confirmed here, in the same breath, through the same route the
   * tap uses, so the daemon still decides and still sends exactly once. She never sees the
   * "Shall I?" at the end of the readback, because the answer handed back to her is the one
   * from the confirm. */
  async function autoSend(made) {
    let result = null
    lastProposalAt = Date.now()
    try {
      const got = await callTool('confirm', { id: made.id })
      result = got.result
    } catch (err) {
      result = { ok: false, agent: made.agent, said: `that failed: ${err.message}` }
    }
    settledConfirm(made.id, result)
    if (result && result.ok && result.agent) { delegated(result.agent) }
    noteSend(result, made.agent)
    return result
  }

  /* A tool call, forwarded whole. The daemon answers with {result}, the result goes back as the
   * output of that call, and then the backend is asked to carry on. Without that second ask the
   * backend holds the answer and the voice says nothing. */
  async function runToolCall(callId, name, rawArgs) {
    let args = {}
    try { args = rawArgs ? JSON.parse(rawArgs) : {} } catch { args = {} }
    let result
    let blocked = null
    const confirmId = name === 'confirm' ? String(args.id || '') : ''
    tools += 1
    try {
      /* Her confirm waits for the referee when his answer to this card is still being read. */
      if (confirmId) { blocked = await refereeFirst(confirmId) }
      if (blocked) {
        result = blocked
      } else {
        hold(confirmId)
        try {
          const got = await callTool(name, args)
          result = got.result
        } finally {
          release(confirmId)
        }
      }
    } catch (err) {
      result = `that failed: ${err.message}`
    } finally {
      tools = Math.max(0, tools - 1)
      heard()
    }
    if (name === 'propose' && result && result.id) {
      /* Now, into an agent in the middle of its work, is always his to choose. */
      if (mode === 'auto' && !result.choose) {
        /* Confirmed before she is answered. What goes back is what happened, not what was
           proposed, so there is nothing left for her to ask about. */
        const sent = await autoSend(result)
        result = { ...result, sent: true, readback: undefined, ...(sent || {}) }
        /* On an agent's call it went now or waits for the end; the result says which. */
        instruct(caller && result.reply
          ? 'Done already, and the agent answered: the answer is the reply in the result. Read '
            + 'it to him in a few plain sentences, then go on. Do not repeat what he said, and do '
            + 'not ask.'
          : caller
          ? 'Done already. Say in a few words what happened to it, as the result says: sent now, '
            + 'or kept for the end of the call. Behind a small beat, then go on. Do not repeat '
            + 'what he said, and do not ask.'
          : 'That was sent already. Say what went out in one line. Do not ask whether to '
            + 'send it. Then wait.')
      } else {
        showCard(result)
      }
    }
    if ((name === 'confirm' || name === 'cancel') && !blocked) {
      if (!card || !args.id || card.id === args.id) { showCard(null) }
    }
    if (name === 'confirm' && !blocked) {
      /* Lost the race to a tap or his yes, which already drew what happened. */
      const lost = settledConfirm(confirmId, result)
      if (!lost) { noteSend(result, args.agent) }
      /* She is told it went, not that it expired: it did go, by his tap or his yes. */
      if (lost && sentIds.has(confirmId)) {
        result = { ok: true, said: 'That was already sent. Do not send it again.' }
      }
      if (result && result.ok && result.agent) { delegated(result.agent) }
    }
    if (name === 'end_call' && result && result.hangup) { endAfterGoodbye() }
    send({
      type: 'response.item.create',
      item: {
        type: 'function_call_output',
        call_id: callId,
        output: typeof result === 'string' ? result : JSON.stringify(result),
      },
    })
    send({ type: 'response.create' })
  }

  /* He asked to hang up, or said goodbye: the voice says its goodbye, and the call ends once it
   * has been quiet for a moment, so he never has to reach for the phone. The cap ends it even if
   * no goodbye comes. */
  let goodbye = null
  function endAfterGoodbye() {
    let quietTimer = null
    const done = function () { if (!quit) { stop('hangup') } }
    const cap = window.setTimeout(done, ENDING_CAP_MS)
    goodbye = function () {
      if (quietTimer) { window.clearTimeout(quietTimer) }
      quietTimer = window.setTimeout(function () { window.clearTimeout(cap); done() }, ENDING_QUIET_MS)
    }
  }

  /* The backend's own events, unwrapped. A finished function call is read from the output item
   * rather than from the arguments event, because the arguments event alone does not say which
   * call it belongs to. */
  function backendEvent(event) {
    if (!event || event.type !== 'response.output_item.done') { return }
    const item = event.item || {}
    if (item.type !== 'function_call') { return }
    runToolCall(item.call_id, item.name, item.arguments)
  }

  function onEvent(raw) {
    let msg
    try { msg = JSON.parse(raw) } catch { return }

    switch (msg.type) {
      case 'session.started':
        say('listening')
        heard()
        sessionUp = true
        /* The mode is told once per session, here rather than in the session config, because
           the config is immutable after the session starts and the switch is not. A resume is
           a new session, so it is said again. */
        instruct(modeLine(mode, !!caller))
        /* An agent came back and she has not said so. A line in the prompt did not make her
           speak first, so she is handed the fact and asked for a turn, the way a typed line is.
           The sentence is the daemon's, from the offer answer. */
        if (pendingReport || briefSay) {
          const facts = pendingReport ? untold.slice() : []
          if (pendingReport) {
            instruct(`While the call was paused: ${pendingReport} `
              + 'Say this to him now in one sentence, then wait.')
          }
          /* A resumed call with untold items. The daemon wrote the line; she says them first. */
          if (briefSay) { instruct(briefSay) }
          send({ type: 'response.create' })
          if (facts.length) { handedOver(facts) }
          pendingReport = ''
          briefSay = ''
        } else if (greeted) {
          /* The greeting was the recording. Once it has finished, anything he said in the gap
             goes to her as his words; otherwise she takes the turn the opening asks for. */
          if (!caller) { instruct(GREETED) }
          const openNow = briefOpen
          greetOver.then(async function () {
            const said = gap ? await gap.take() : ''
            gap = null
            if (said) {
              instruct(`While the call was connecting he said: ${said} Answer him now.`)
              send({ type: 'response.create' })
              const hid = ++rowId
              turn('you', said, false, hid)
              callTurns.push({ who: 'you', text: said })
              voiceLog('you', said, callerId()).catch(() => {})
            } else if (openNow) {
              send({ type: 'response.create' })
            }
          })
        } else if (briefOpen) {
          send({ type: 'response.create' })
        }
        briefOpen = false
        if (startedDone) { startedDone(); startedDone = null }
        return
      case 'session.usage.updated':
        charged = (msg.usage && msg.usage.seconds) || charged
        return
      case 'session.closed':
        charged = (msg.usage && msg.usage.seconds) || charged
        if (closing) { closing() }
        return
      case 'session.input_transcript.delta':
        say('listening')
        fragment('you', msg.delta)
        return
      case 'session.output_transcript.delta':
        /* She is answering, so whatever she was handed has come back. */
        thinkingAt = 0
        say('speaking')
        if (msg.delta) { spoke(!turnOf.voice.live) }
        if (goodbye && msg.delta) { goodbye() }
        fragment('voice', msg.delta)
        if (capturer && greetingDone(turnOf.voice.text || '')) {
          capturer.arm()
          capturer = null
        }
        return
      case 'session.delegation.created':
        thinkingAt = Date.now()
        say('thinking')
        return
      case 'response.event':
        backendEvent(msg.event)
        return
      case 'error':
        turn('voice', (msg.error && msg.error.message) || 'Something went wrong.', false, ++rowId)
        return
      default:
        return
    }
  }

  /* Tell the voice something that did not come from the microphone: an agent finished, a new ask
   * arrived, a button was tapped. Commentary is the event for something she should say aloud,
   * and she paraphrases it, which is why these are written as facts rather than as sentences. */
  function tell(text) {
    if (!text) { return false }
    return send({
      type: 'session.commentary.append',
      delegation_id: null,
      content: String(text).slice(0, 600),
    })
  }

  /* The Confirm and Cancel buttons, and his spoken answer as the referee read it. The same route
   * the model uses, so a tap and a spoken yes are the same thing to the daemon, and the voice is
   * told what happened either way. `how` is tap (the default), yes, no, or change.
   *
   * A confirm for a card that another path is already confirming, or has sent, goes nowhere: the
   * daemon would refuse it anyway, and the refusal would draw "Nothing was sent" under a send. */
  async function runTool(name, args, how) {
    const id = String((args && args.id) || '')
    if (name === 'confirm' && id && (inFlight.has(id) || sentIds.has(id))) { return null }
    if (name === 'confirm') { hold(id) }
    let got
    try {
      got = await callTool(name, args)
    } finally {
      if (name === 'confirm') { release(id) }
    }
    if (name === 'confirm' || name === 'cancel') {
      if (!card || !id || card.id === id) { showCard(null) }
      const result = got.result || {}
      const said = result.said || 'Done.'
      const lost = name === 'confirm' && settledConfirm(id, result)
      if (lost) { return got.result }
      if (how === 'yes') {
        tell(`The owner said yes, so the console confirmed it. ${said}`)
      } else if (how === 'no') {
        tell(`The owner said no, so the console dropped it. ${said}`)
      } else if (how !== 'change') {
        tell(`The owner tapped ${name}. ${said}`)
      }
      if (name === 'confirm') {
        if (how === 'yes' && result.ok) { note('Confirmed by your yes.') }
        noteSend(got.result, args && args.agent)
        if (result.ok && result.agent) { delegated(result.agent) }
      } else if (how === 'change') {
        note('Changed. Not sent; ' + ((caller && caller.name) || 'The front desk') + ' will propose again.')
      } else if (how === 'no') {
        note('Cancelled.')
      }
    }
    return got.result
  }

  /* Ask or Auto. Remembered between calls, and the live model is told the moment it changes,
   * because the session's own instructions cannot be edited after it starts. */
  function setMode(next) {
    mode = next === 'auto' ? 'auto' : 'ask'
    writeMode(mode)
    instruct(modeLine(mode, !!caller))
    return mode
  }

  /* He typed instead of saying it. A spelling, a name, a correction: the things speech
   * recognition gets wrong and he should not have to say three times.
   *
   * It reaches her as an instruction, because that is the only client event that carries his
   * words into a conversation already in progress. It is drawn and logged as his turn, because
   * that is what it is.
   *
   * A paused call comes back first. The instruction has to wait for the new session to say it
   * has started, or it is sent into a channel nobody is reading yet. */
  async function typedIn(text) {
    const said = String(text || '').trim()
    if (!said) { return false }
    if (paused) {
      try { await resume() } catch { return false }
    }
    if (started) {
      await Promise.race([
        started,
        new Promise(function (done) { window.setTimeout(done, STARTED_WAIT) }),
      ])
    }
    heard()
    /* A full stop of its own only when he did not end with one. Two in a row is a stumble read
       aloud, and the whole point of this box is the lines she keeps mishearing. */
    const stop = /[.!?]$/.test(said) ? '' : '.'
    const ok = instruct(`The owner typed this instead of saying it: ${said}${stop} `
      + 'Treat it as his words and answer aloud.')
    /* An instruction does not start a turn on its own. Without this she reads it, agrees with
       it silently, and says nothing, which is exactly what a typed line must not do. */
    send({ type: 'response.create' })
    const id = ++rowId
    turn('you', said, false, id, true)
    callTurns.push({ who: 'you', text: said })
    voiceLog('you', said, callerId()).catch(() => {})
    judgeReply(said, Date.now())
    judgeInbox(said)
    return ok
  }

  /* He sent an image, with or without words. The live voice cannot see, so the daemon asks a
   * small vision model what it shows and she is handed that, in the same shape a typed line takes:
   * one instruction, then a response.create so she answers aloud.
   *
   * His row is drawn at once with an image mark. The call log gets his words and " [image]",
   * never the image. While the vision model looks the state line says Looking, and the call counts
   * it as work, so it cannot pause itself in the middle.
   *
   * Every failure is one plain sentence in the transcript. An older daemon has no image route and
   * answers 404, which is the one failure worth its own words. */
  async function imageIn(text, dataUrl) {
    const words = String(text || '').trim()
    if (!dataUrl) { return typedIn(words) }
    const id = ++rowId
    turn('you', words, false, id, true, true)
    const logged = words ? `${words} [image]` : '[image]'
    callTurns.push({ who: 'you', text: logged })
    voiceLog('you', logged, callerId()).catch(() => {})

    looking += 1
    heard()
    const before = state
    say('looking')
    let said = ''
    let failed = ''
    try {
      const got = await voiceImage(dataUrl, words)
      said = String((got && got.said) || '').trim()
      if (!said) { failed = 'The image came back with nothing said about it.' }
    } catch (err) {
      failed = err && err.status === 404
        ? 'This daemon cannot read images yet. It needs a restart.'
        : `The image could not be read. ${(err && err.message) || ''}`.trim()
    } finally {
      looking = Math.max(0, looking - 1)
      heard()
    }
    /* Back to what it said before, unless a session is open and listening is the truth. A call
       still connecting, or paused, says so again. */
    if (state === 'looking') { say(pc || before === 'looking' ? 'listening' : before) }
    if (failed) { note(failed) }
    if (!said && !words) { return false }

    if (paused) {
      try { await resume() } catch { return false }
    }
    if (started) {
      await Promise.race([
        started,
        new Promise(function (done) { window.setTimeout(done, STARTED_WAIT) }),
      ])
    }
    heard()
    const stop = /[.!?]$/.test(words) ? '' : '.'
    const his = words ? `His words: ${words}${stop}` : 'He said nothing with it.'
    const shows = said
      ? `The owner sent an image. What it shows: ${said} `
      : 'The owner sent an image, and it could not be read, so say so. '
    const ok = instruct(`${shows}${his} Answer him aloud.`)
    send({ type: 'response.create' })
    return ok
  }

  /* Mute. The microphone track is disabled, so nothing leaves this laptop and the orb stops
   * following you. The session stays open, the meter keeps running, and she keeps talking.
   *
   * The session is told as well. `session.input_audio.mute` is the documented event and the docs
   * say plainly that "muting input does not stop the assistant's output", so it is exactly this
   * and nothing more. The track is what makes it true; the event is what stops GPT-Live reading a
   * silent stream as you having stopped mid sentence. */
  function setMuted(next) {
    muted = !!next
    if (mic) { mic.getAudioTracks().forEach(function (t) { t.enabled = !muted }) }
    send({ type: muted ? 'session.input_audio.mute' : 'session.input_audio.unmute' })
    /* A paused call listens for his voice with a local microphone, and mute has to mean the same
       thing there: muted is not listened to at all, and unmuting starts the listening. */
    if (paused) {
      if (muted) { stopWake() } else { listenForWake() }
    }
    return muted
  }

  async function start(resuming) {
    if (pc) { return info }
    if (!secure()) {
      say('error')
      throw new Error(INSECURE)
    }
    say('connecting')
    gone = false
    greeted = false
    greetOver = Promise.resolve()
    gap = null
    sessionUp = false
    /* One promise per session, so a typed line waits for this session rather than for the one
       that was paused. */
    started = new Promise(function (done) { startedDone = done })

    /* The door first. No key or no minutes left, and nothing else has happened yet: no
       microphone prompt, no peer connection, no charge. Asked again on a resume, because the day
       can run out while a call is paused. */
    /* The microphone is asked for at the same time as the door, not after it: on the phone the
       permission is already given, and the two waits were adding up. A closed door still stops
       everything before any connection or charge; the microphone is simply let go. */
    const t0 = Date.now()
    const timing = {}
    const micAsked = navigator.mediaDevices.getUserMedia({ audio: true }).then(
      function (got) { timing.mic_ms = Date.now() - t0; return got },
      function () { return null })
    /* The recorded greeting, started GREETING_DELAY after the tap and only once the microphone
       is open. Opening the microphone puts the phone into call audio, and a clip already playing
       when that switch lands was cut off mid word. */
    /* The door is asked at once, beside the microphone. On a fresh call its answer says whether an
       agent rang and this is that agent's call, which decides whose line plays first. */
    const doorAsked = voiceSession(callerId() || (target ? 'agent:' + target : null), !!resuming)
    doorAsked.catch(() => {})
    let door = null
    if (!resuming) {
      try { door = await doorAsked } catch { door = null }
      if (door && door.caller && (door.caller.call || door.caller.agent)) {
        caller = door.caller
        callerFns.forEach((fn) => fn({ ...caller }))
      }
    }
    // A call he places to an agent never plays the front desk's "Welcome back": the agent greets him
    // itself, and both together were two greetings in a row.
    const clipUrl = !resuming && (caller ? caller.clip : target ? null : await greetingSoon(GREETING_WAIT))
    if (clipUrl) {
      greeted = true
      greetOver = Promise.all([
        micAsked,
        new Promise(function (done) { window.setTimeout(done, Math.max(0, GREETING_DELAY - (Date.now() - t0))) }),
      ]).then(function () { return quit ? null : playGreeting(clipUrl) }).then(function () {
        if (mic && !muted) { mic.getAudioTracks().forEach(function (t) { t.enabled = true }) }
        if (!sessionUp && mic && !quit) { gap = gapRecorder(mic) }
      })
      const gid = ++rowId
      /* What the clip says, and only that: "Hi the owner, it's the master agent." The reason is the voice's
         own next turn. Writing the whole opening here showed the reason twice. */
      const opening = caller ? (caller.greeting || caller.opening) : 'Welcome back, sir.'
      turn('voice', opening, false, gid)
      callTurns.push({ who: 'voice', text: opening })
      /* An agent's call is not answered until the offer, and a line for it is refused before
         then, so its opening line is written down once the call is up. */
      if (caller) { openingTurn = opening } else { voiceLog('voice', opening).catch(() => {}) }
    }
    try {
      info = await doorAsked
    } catch (err) {
      micAsked.then(function (got) { if (got) { got.getTracks().forEach((t) => t.stop()) } })
      throw err
    }
    timing.door_ms = Date.now() - t0
    if (quit) {
      micAsked.then(function (got) { if (got) { got.getTracks().forEach((t) => t.stop()) } })
      say('ended')
      return info
    }
    if (Number(info.turn_gap_ms) > 0) { gapMs = Number(info.turn_gap_ms) }
    if (Number(info.wait_pause_seconds) >= 0) {
      waitPauseSeconds = Number(info.wait_pause_seconds)
    }
    /* An older daemon sends no silence number, and the fallback stands. */
    if (Number(info.silence_minutes) > 0) { silenceMs = Number(info.silence_minutes) * 60000 }

    mic = await micAsked
    if (!mic) {
      say('error')
      throw new Error('The microphone was refused. Allow it for this page and try again.')
    }
    if (quit) { mic.getTracks().forEach((t) => t.stop()); mic = null; say('ended'); return info }
    /* Muted before the pause is muted after it. While the recorded greeting plays, the
       microphone is off too, so the call does not hear the front desk's own clip as him speaking. */
    if (muted || greeted) { mic.getAudioTracks().forEach(function (t) { t.enabled = false }) }
    greetOver.then(function () {
      if (mic && !muted) { mic.getAudioTracks().forEach(function (t) { t.enabled = true }) }
    })

    pc = new RTCPeerConnection()

    /* Her voice. An audio element rather than the raw track, because a track alone plays
       nowhere. */
    audio = document.createElement('audio')
    audio.autoplay = true
    /* Back from another app, her voice plays again. The Android app keeps the call and the
       microphone alive off screen, but the web view pauses this element while it is hidden and
       nothing restarted it: on 9 October he came back to a call that heard him and said nothing. */
    if (!unhide) {
      unhide = function () {
        if (document.visibilityState !== 'visible') { return }
        if (audio && audio.srcObject && audio.paused) { audio.play().catch(() => {}) }
        if (ctx && ctx.state === 'suspended') { ctx.resume().catch(() => {}) }
      }
      document.addEventListener('visibilitychange', unhide)
      window.addEventListener('focus', unhide)
      window.addEventListener('pageshow', unhide)
    }
    pc.addEventListener('track', function (ev) {
      const stream = new MediaStream([ev.track])
      audio.srcObject = stream
      audio.play().catch(() => {})
      /* No recorded greeting yet: this call's own first words are taken, once, for the next. */
      if (!resuming && !greeted && !caller && !greetingNow() && !capturer) { capturer = captureGreeting(stream) }
      /* An agent ringing with no greeting yet: its own first words are taken, once, for its next call. */
      if (!resuming && !greeted && caller && caller.capture && caller.agent && !capturer) {
        const who = caller.agent
        capturer = captureGreeting(stream, 'agent:' + who, function (b64) { return saveAgentGreeting(who, b64) })
      }
      if (ctx) { meters.push(meter(ctx, stream, false)) }
      if (recordOn && !recorder) { recorder = startRecording(mic, stream, function () { return recordSession }) }
    })

    for (const track of mic.getAudioTracks()) { pc.addTrack(track, mic) }

    /* Before the offer, or the channel is not in it. */
    channel = pc.createDataChannel(CHANNEL)
    channel.addEventListener('message', function (ev) { onEvent(ev.data) })
    channel.addEventListener('open', function () {
      if (muted) { send({ type: 'session.input_audio.mute' }) }
    })
    channel.addEventListener('close', function () {
      if (!gone) { say('ended') }
    })

    await pc.setLocalDescription(await pc.createOffer())
    const g0 = Date.now()
    await gathered(pc)
    timing.gather_ms = Date.now() - g0
    if (quit) { tearDown(); say('ended'); return info }
    const offer = pc.localDescription && pc.localDescription.sdp
    if (!offer) {
      tearDown()
      say('error')
      throw new Error('The browser produced no connection offer.')
    }

    let answer
    try {
      /* On a resume the turns of this call go with the offer, and `back` says which agent woke
         the call, when one did. The daemon writes the briefing and the sentence about what
         arrived; the browser only says what was said and what happened. */
      timing.before_offer_ms = Date.now() - t0
      answer = await voiceOffer(offer, resuming ? callTurns.slice(-RESUME_TURNS) : null,
        resuming && untold.length ? untold.slice() : null,
        resuming && delegatedAll.size
          ? Array.from(delegatedAll, function ([agent, at]) { return { agent, at } })
          : null, timing, caller ? (caller.call || 'agent:' + caller.agent) : 'desk')
    } catch (err) {
      tearDown()
      say('error')
      throw err
    }
    /* Hung up while OpenAI was opening it. The connection is never made, and the daemon is told
       it ended, so the log has an end for every open. Nothing was spoken, so nothing is owed. */
    if (quit) {
      tearDown()
      voiceEnd(answer.session_id, 0, quitWhy).catch(() => {})
      say('ended')
      return info
    }
    /* An agent's call: the daemon's sentence for an opening line already played, and the line
       itself, written down now that the call is answered. */
    if (caller) {
      /* A call he placed has its id only now, from the offer. */
      if (answer.caller && answer.caller.call) { caller = { ...caller, ...answer.caller } }
      lastSession = answer.session_id || lastSession
      if (openingTurn) { voiceLog('voice', openingTurn, caller.call).catch(() => {}); openingTurn = '' }
    }
    /* Held before the connection is made, because session.started can follow at once. An older
       daemon sends no report, and then she has only the prompt to go on. */
    pendingReport = resuming && untold.length ? String(answer.report || '') : ''
    /* The untold inbox items, as rows, and on a resume the line that makes her say them. An
       older daemon sends neither. */
    takeBrief(answer.brief)
    briefSay = resuming && Array.isArray(answer.brief) && answer.brief.length
      ? String(answer.brief_say || '') : ''
    briefOpen = !resuming
    /* Recorded unless Config says no. An older daemon says nothing, and then nothing is kept. */
    recordOn = answer.record === true
    recordSession = String(answer.session_id || '')
    /* The HTTP request started the session. Nothing is sent on the channel to start it. */
    await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp })

    ctx = new (window.AudioContext || window.webkitAudioContext)()
    meters = [meter(ctx, mic, true)]
    watchLevels()

    opened = Date.now()
    heard()
    thinkingAt = 0
    tools = 0
    busyAt = 0
    silenced = false
    waitTimer = window.setInterval(everySecond, 1000)
    info = { ...info, ...answer }
    return info
  }

  /* Everything torn down in the order it was built. Split from stop() because the goodbye has to
     wait for a final event and a failure must not leave the microphone on. */
  function tearDown() {
    gone = true
    /* Before the microphone stops, so the last words are in the file. */
    if (recorder) { recorder.stop(recordSession); recorder = null }
    recordOn = false
    if (unhide) {
      document.removeEventListener('visibilitychange', unhide)
      window.removeEventListener('focus', unhide)
      window.removeEventListener('pageshow', unhide)
      unhide = null
    }
    if (gap) { gap.cancel(); gap = null }
    if (capturer) { capturer.cancel(); capturer = null }
    if (frame) { window.cancelAnimationFrame(frame); frame = null }
    if (waitTimer) { window.clearInterval(waitTimer); waitTimer = null }
    /* Nobody is left waiting on a session that has gone. */
    if (startedDone) { startedDone(); startedDone = null }
    setVoiceLevel(0)
    meters = []
    for (const who of ['you', 'voice']) {
      const row = turnOf[who]
      if (row.timer) { clearTimeout(row.timer); row.timer = null }
      row.text = ''
      row.live = false
    }
    try { if (channel) { channel.close() } } catch { /* already gone */ }
    try { if (pc) { pc.close() } } catch { /* already gone */ }
    if (mic) { mic.getTracks().forEach((t) => t.stop()) }
    if (audio) { audio.srcObject = null }
    if (ctx) { ctx.close().catch(() => {}) }
    channel = null
    pc = null
    mic = null
    audio = null
    ctx = null
  }

  /* One session closed and charged. Hang up and Pause are the same act down to here, and the only
     difference above it is whether another session opens afterwards.

     The goodbye is sent and the final usage waited for, because that event carries the seconds
     OpenAI billed. If it never arrives the wall clock stands in, which over counts slightly, and
     over counting a budget is the safe direction. */
  async function close(why) {
    if (!pc) { return 0 }
    const wall = opened ? (Date.now() - opened) / 1000 : 0

    if (channel && channel.readyState === 'open') {
      /* The overlay is gone by now, so this is what catches a tab closed during the goodbye. */
      const leave = function () { endNow(why) }
      window.addEventListener('pagehide', leave)
      await new Promise(function (done) {
        const timer = setTimeout(done, CLOSE_WAIT)
        closing = function () { clearTimeout(timer); closing = null; done() }
        send({ type: 'session.close' })
      })
      window.removeEventListener('pagehide', leave)
    }
    closing = null
    /* The page went away during the goodbye, and endNow() has already charged this session. */
    if (!pc) { return 0 }

    const seconds = charged || wall
    /* The lines in flight are written down first. tearDown clears them, so a flush after it is a
       flush of nothing, and the last thing either of you said would be lost. */
    flushAll()
    const sessionId = info && info.session_id
    tearDown()
    opened = 0
    charged = 0
    if (seconds) { voiceEnd(sessionId, seconds, why).catch(() => {}) }
    return seconds
  }

  /* The page is going away. No waiting for the goodbye, because there is no later: the goodbye
     is sent, the connection is closed, and the seconds go out as a beacon, which the browser
     still delivers after the page has gone. After this the session is torn down, so a Hang up or
     an unmount that follows posts nothing. A paused call has no session and owes nothing. */
  function endNow(why) {
    if (!quit) { quit = true; quitWhy = why || 'pagehide' }
    stopWake()
    waiting.clear()
    forget()
    if (!pc) {
      if (caller && lastSession) { voiceEndBeacon(lastSession, 0, why || 'pagehide') }
      return 0
    }
    const seconds = charged || (opened ? (Date.now() - opened) / 1000 : 0)
    send({ type: 'session.close' })
    const sessionId = info && info.session_id
    tearDown()
    opened = 0
    charged = 0
    banked += seconds
    /* A Hang up part way through its goodbye is let go; the guard in close() sees it is done. */
    if (closing) { closing() }
    if (seconds) { voiceEndBeacon(sessionId, seconds, why) }
    return seconds
  }

  /* A hang up. Nothing is owed to a call that has ended. */
  function forget() {
    untold = []
    reporting = null
    pendingReport = ''
    delegatedAll.clear()
    card = null
    judged.clear()
    briefSay = ''
    briefOpen = false
  }

  /* The local microphone for a paused call. It is attached to nothing but the meter, so the orb
     still breathes with him and nothing leaves the laptop. A muted microphone is not opened at
     all: mute means he is not talking to her, and a paused call must not read him anyway. */
  async function listenForWake() {
    if (wakeWanted || wakeMic || muted || !secure()) { return }
    wakeWanted = true
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      wakeWanted = false
      return
    }
    /* The call came back while the browser was still handing the microphone over. */
    if (!wakeWanted || !paused) {
      stream.getTracks().forEach((t) => t.stop())
      wakeWanted = false
      return
    }
    wakeMic = stream
    wakeCtx = new (window.AudioContext || window.webkitAudioContext)()
    meters = [meter(wakeCtx, wakeMic, true)]
    watchLevels()
    wakeSince = 0
    wakeTimer = window.setInterval(wakeTick, 100)
  }

  /* He is talking. The level has to stay up for the hold before the call opens again, so one
     thump on the desk is not a resume. */
  function wakeTick() {
    if (!paused || muted || !meters.length) { wakeSince = 0; return }
    if (meters[0].read() < WAKE_LEVEL) { wakeSince = 0; return }
    if (!wakeSince) { wakeSince = Date.now(); return }
    if (Date.now() - wakeSince < WAKE_HOLD) { return }
    wakeSince = 0
    resume().catch(() => {})
  }

  function stopWake() {
    wakeWanted = false
    wakeSince = 0
    if (wakeTimer) { window.clearInterval(wakeTimer); wakeTimer = null }
    if (!wakeMic && !wakeCtx) { return }
    if (frame) { window.cancelAnimationFrame(frame); frame = null }
    meters = []
    setVoiceLevel(0)
    if (wakeMic) { wakeMic.getTracks().forEach((t) => t.stop()); wakeMic = null }
    if (wakeCtx) { wakeCtx.close().catch(() => {}); wakeCtx = null }
  }

  /* Hang up. The only thing that ends a call, paused or not. `why` is how it ended, for the
     daemon's log: hangup, back, silence, cap. */
  async function stop(why) {
    if (!quit) { quit = true; quitWhy = why || 'hangup' }
    stopWake()
    waiting.clear()
    forget()
    if (!pc) {
      /* A paused agent's call has no session to close, but the call itself has to end, so the
         agent gets what was said. The seconds were charged at the pause. */
      if (caller && lastSession) { voiceEnd(lastSession, 0, why || 'hangup').catch(() => {}) }
      paused = false
      say('ended')
      return banked
    }
    banked += await close(why || 'hangup')
    paused = false
    say('ended')
    return banked
  }

  /* Pause. The session at OpenAI is closed and its seconds are charged, exactly as hanging up
     does, so the budget is right whichever way the call ends. The overlay stays, the transcript
     stays, and the clock stops because there is no open session for it to count. The local
     microphone takes over, so talking brings the call back. */
  async function pause(why) {
    if (!pc || paused || pausing) { return banked }
    pausing = true
    try {
      banked += await close(why === 'wait' ? 'wait' : 'pause')
    } finally {
      pausing = false
    }
    paused = true
    say('paused')
    listenForWake()
    return banked
  }

  /* Resume. A new session, carrying this call's turns, so it reads as one conversation. Any
     resume, the button, his voice or an agent, carries every fact she has not said yet, and the
     daemon turns them into the sentence she opens with. */
  async function resume() {
    if (!paused) { return info }
    stopWake()
    paused = false
    return start(true)
  }

  /* One sentence for an agent that came back, for a call that is awake. A paused call sends the
     facts to the daemon instead and the daemon writes the words, which is the rule everywhere
     else; commentary takes words, so this is the one place the browser writes one. */
  function backLine(back) {
    if (!back || !back.agent) { return '' }
    if (back.kind === 'ask') {
      return back.asking
        ? `${back.agent} has stopped and is waiting on you: ${back.asking}.`
        : `${back.agent} has stopped and is waiting on an answer from you.`
    }
    return `${back.agent} has finished the turn you gave it.`
  }

  /* An agent came back. It owes nothing now, so the wait ends. A paused call opens again and she
     says what arrived as her first line; an open call is told in one line of commentary. */
  function wake(back) {
    cameBack(back && back.agent)
    keepUntold(back)
    if (!paused) {
      const facts = untold.filter(function (f) { return f.agent === (back && back.agent) })
      const ok = tell(backLine(back))
      if (ok) { handedOver(facts) }
      return Promise.resolve(ok)
    }
    return resume()
  }

  /* Seconds so far, for the overlay's clock. OpenAI's number for the session in hand, plus every
     session already closed on this call. While paused there is no session in hand, so it stops. */
  function elapsed() {
    if (!pc) { return banked }
    return banked + (charged || (opened ? (Date.now() - opened) / 1000 : 0))
  }

  function listen(set, fn) {
    set.add(fn)
    return function () { set.delete(fn) }
  }

  return {
    start,
    stop,
    endNow,
    pause,
    resume,
    setMuted,
    isMuted: () => muted,
    isPaused: () => paused,
    state: () => state,
    waitOn,
    cameBack,
    waitingOn,
    wake,
    tell,
    runTool,
    typed: typedIn,
    image: imageIn,
    mode: () => mode,
    setMode,
    elapsed,
    onState: (fn) => listen(stateFns, fn),
    onTranscript: (fn) => listen(turnFns, fn),
    onProposal: (fn) => listen(proposalFns, fn),
    onDelegated: (fn) => listen(delegatedFns, fn),
    onOpen: (fn) => listen(openFns, fn),
    openItem,
    replacedItem,
    onEnd: (fn) => listen(endFns, fn),
    /* Who is calling, when an agent is: {call, agent, name, opening}. Null on a front desk call. */
    caller: () => (caller ? { ...caller } : null),
    onCaller: (fn) => listen(callerFns, fn),
  }
}

export default createVoiceSession
