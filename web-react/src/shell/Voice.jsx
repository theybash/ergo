/* The voice overlay. One big orb, a state line, the real transcript of the call, a proposal card
   when the voice has asked for a yes, a clock, a typed box the keyboard button shows and hides,
   and the call controls: Ask or Auto, Mute, the keyboard, Pause, Hang up.
   App renders it once, above every screen.

   The orb here is a picture, not a control. The ways out are Hang up, Escape, the session
   ceiling, a long silence, and the page going away. Every one of them ends the call; the first
   four by the same path as Hang up, the last with a beacon, because a page that is leaving cannot
   wait for the goodbye.

   Minimise is not a way out. The chevron at the top left, or the phone's back button, takes the
   overlay off the screen and leaves the call running: this component stays mounted, so the
   session, her audio and the microphone are untouched. A bar under the top bar holds the call
   meanwhile, with the small orb, the clock, a state word, Mute and Hang up. Tapping the bar
   brings the overlay back.

   Everything that touches OpenAI is in voiceSession.js. This file draws what that reports and
   sends back five things: a tap on Confirm or Cancel, a line he typed instead of saying, an
   image with any words beside it, the delegation mode, and a word to the voice when an agent it
   delegated to finishes or stops to ask something. That word also wakes a call that paused
   itself while it waited for exactly that agent. */

import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import useAgentSocket from '../data/useAgentSocket'
import { createVoiceSession, readMode } from './voiceSession'
import { takeCallTarget, setCallOpen } from './callTarget'
import { onVoiceBack } from './voiceHistory'
import { inApp, tellApp, onApp, askApp } from './native'
import { callOver } from './awayReload'
import './voice.css'

/* What the state line says. The session reports the left, the reader sees the right. */
const WORDS = {
  connecting: 'Connecting',
  listening: 'Listening',
  thinking: 'Thinking',
  looking: 'Looking',
  speaking: 'Speaking',
  paused: 'Paused',
  waiting: 'Paused, waiting',
  ended: 'Ended',
  error: 'Not connected',
  idle: 'Starting',
}

/* The bar's one word. Three states he needs at a glance; the overlay says the rest. */
function barWord(state, error) {
  if (error) { return 'Not connected' }
  if (state === 'speaking') { return 'Speaking' }
  if (state === 'paused' || state === 'waiting') { return 'Paused' }
  if (state === 'connecting' || state === 'idle') { return 'Connecting' }
  if (state === 'ended') { return 'Ended' }
  return 'Listening'
}

/* The minimise glyph: a chevron pointing down, the way the overlay goes. */
function Down() {
  return (
    <svg className="vc-mic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M6 9.5l6 6 6-6" />
    </svg>
  )
}

/* How many turns stay on screen. A call is a conversation, not a log; the whole of it is in
   var/voice/calls.jsonl and the last few are what a person glances at. */
const KEEP = 6

/* The warning before the session ceiling, in seconds. Spoken, not drawn, because you are
   listening rather than looking. */
const WARN_BEFORE = 120

/* A hidden tab ends the call only after this long. A phone hides the page for a moment to show a
   notification, and that must not hang up. */
const HIDDEN_WAIT = 20000

function clock(seconds) {
  const whole = Math.max(0, Math.round(seconds))
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

/* The microphone, crossed through when it is off. Drawn rather than lettered, because the button
   is round, and the button itself carries the name for anything that reads it out. */
function Mic({ off }) {
  return (
    <svg className="vc-mic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" />
      <line x1="12" y1="18" x2="12" y2="21" />
      {off ? <line className="vc-slash" x1="4" y1="20" x2="20" y2="4" /> : null}
    </svg>
  )
}

/* The keyboard, for the button that shows and hides the typed box. Same weight as the mic. */
function Keys() {
  return (
    <svg className="vc-mic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="2.5" y="6" width="19" height="12" rx="2" />
      <line x1="6" y1="9.5" x2="6.01" y2="9.5" />
      <line x1="9.3" y1="9.5" x2="9.31" y2="9.5" />
      <line x1="12.6" y1="9.5" x2="12.61" y2="9.5" />
      <line x1="15.9" y1="9.5" x2="15.91" y2="9.5" />
      <line x1="18" y1="9.5" x2="18.01" y2="9.5" />
      <line x1="6" y1="12.5" x2="6.01" y2="12.5" />
      <line x1="18" y1="12.5" x2="18.01" y2="12.5" />
      <line x1="8" y1="15" x2="16" y2="15" />
    </svg>
  )
}

/* The loudspeaker, with its sound waves while it is on. */
function Speaker({ on }) {
  return (
    <svg className="vc-mic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z" strokeLinejoin="round" />
      {on ? <path d="M15.5 9a4.5 4.5 0 0 1 0 6M18 6.5a8 8 0 0 1 0 11" /> : null}
    </svg>
  )
}

/* Pause, Resume and Hang up, drawn at the weight of the mic. */
function Hold() {
  return (
    <svg className="vc-mic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <line x1="9" y1="6" x2="9" y2="18" />
      <line x1="15" y1="6" x2="15" y2="18" />
    </svg>
  )
}

function Play() {
  return (
    <svg className="vc-mic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M8 5.5v13l10.5-6.5z" strokeLinejoin="round" />
    </svg>
  )
}

function HangUp() {
  return (
    <svg className="vc-end" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 9c-2.6 0-5.1.6-7.4 1.8-.6.3-.9.9-.8 1.5l.4 2.4c.1.6.7 1 1.3.9l2.9-.6c.5-.1.9-.6.9-1.1v-1.9c1.8-.6 3.6-.6 5.4 0v1.9c0 .5.4 1 .9 1.1l2.9.6c.6.1 1.2-.3 1.3-.9l.4-2.4c.1-.6-.2-1.2-.8-1.5C17.1 9.6 14.6 9 12 9z" />
    </svg>
  )
}

/* A picture, for the button inside the box that picks an image. */
function Picture() {
  return (
    <svg className="vc-mic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <circle cx="9" cy="10" r="1.6" />
      <path d="M4 17.5l5-4.5 4 3.5 2.5-2 4.5 3.5" />
    </svg>
  )
}

/* The largest image the daemon takes, decoded. Checked here too, so a huge photo is refused
   before it is uploaded rather than after. */
const IMAGE_MAX = 8 * 1024 * 1024

/* One agent the voice gave work to on this call. It holds that agent's socket and says when the
   agent settles a turn or stops to ask something. A component rather than a loop, because a
   socket is a hook and a hook cannot be called once per item in a list. */
function Delegated({ agent, onSettled, onAsk }) {
  const sock = useAgentSocket(agent, { wake: false })
  /* Null until the socket has been looked at once. It cannot start at 0, because 0 is also what
     a fresh socket counts, and then the first settled turn reads as the attach and is thrown
     away. That is the one turn this whole thing exists for. */
  const seenTick = useRef(null)
  const seenAsks = useRef(new Set())

  useEffect(function () {
    /* The first tick is whatever the socket had on attach, not news. */
    if (seenTick.current === null) { seenTick.current = sock.settledTick; return }
    if (sock.settledTick === seenTick.current) { return }
    seenTick.current = sock.settledTick
    onSettled(agent)
  }, [agent, sock.settledTick, onSettled])

  useEffect(function () {
    for (const ask of sock.asks) {
      if (!ask.id || seenAsks.current.has(ask.id)) { continue }
      seenAsks.current.add(ask.id)
      onAsk(agent, ask)
    }
  }, [agent, sock.asks, onAsk])

  return null
}

/* The transcript keeps the last KEEP spoken rows. An inbox row stays for the whole call, because
   it is the thing he may tap, and there are only ever a few. */
function keepRows(rows) {
  let spoken = rows.filter(function (r) { return r.who !== 'brief' }).length
  return rows.filter(function (r) {
    if (r.who === 'brief' || spoken <= KEEP) { return true }
    spoken -= 1
    return false
  })
}

export default function Voice({ onClose, keys, minimised, onMinimise, onRestore, onOpen, inboxChange }) {
  /* The transcript follows the talking. A call is watched, not read, so it stays at the newest
     line unless he has scrolled up on purpose, and then it leaves him where he is. */
  const logRef = useRef(null)
  const stick = useRef(true)
  useEffect(function () {
    const box = logRef.current
    if (!box) { return }
    function onScroll() {
      stick.current = box.scrollHeight - box.scrollTop - box.clientHeight < 40
    }
    box.addEventListener('scroll', onScroll, { passive: true })
    return function () { box.removeEventListener('scroll', onScroll) }
  }, [])
  const [state, setState] = useState('connecting')
  const [turns, setTurns] = useState([])
  const [proposal, setProposal] = useState(null)
  const [error, setError] = useState(null)
  const [budget, setBudget] = useState(null)
  const [spent, setSpent] = useState(0)
  const [busy, setBusy] = useState(false)
  const [watched, setWatched] = useState([])
  const [muted, setMuted] = useState(false)
  /* Loudspeaker or earpiece. Only the Android app can choose, so only there is the button. */
  const [speaker, setSpeaker] = useState(false)
  const app = inApp()
  const [paused, setPaused] = useState(false)
  const [holding, setHolding] = useState(false)
  /* Who the call is waiting on, as one line, because a list would be a new array every second
     and the overlay would redraw for nothing. */
  const [waitingFor, setWaitingFor] = useState('')
  /* Ask or Auto. The switch is remembered in browser storage and read straight into the first
     render, so the button never draws the wrong one for a frame. */
  const [mode, setMode] = useState(readMode)
  /* What he has keyed in so far, and whether the box has the focus. The second one is the
     phone's problem: the keyboard takes half the screen, so the orb gives way while he types. */
  const [typing, setTyping] = useState('')
  const [onKeys, setOnKeys] = useState(false)
  /* Whether the typed box is showing. Hidden on every open unless the Android app's Text slice
     opened the call, and nothing remembers it. */
  const [boxOpen, setBoxOpen] = useState(keys === true)
  /* The image waiting to go with the next send, as {url, name}, or null. A line of its own when
     it could not be taken, because a file too large says so in the box rather than vanishing. */
  const [pic, setPic] = useState(null)
  const [picSaid, setPicSaid] = useState('')
  /* Who is on the line. The front desk, unless an agent rang and this call is the answer: then it is that
     agent, by the name the owner gave it. Ask and Auto hold for an agent's call too: what he says
     goes into its chat after his yes, or at once. */
  /* The agent the call button in a chat aimed this call at, taken once as the overlay mounts. */
  const [target] = useState(takeCallTarget)
  const [who, setWho] = useState(null)
  const name = who || 'The front desk'

  const call = useRef(null)
  const onOpenRef = useRef(onOpen)
  useEffect(function () { onOpenRef.current = onOpen })
  const hangup = useRef(null)
  const box = useRef(null)
  const picker = useRef(null)
  const warned = useRef(false)
  /* How the call ended, for the daemon's log, and whether it has. The first way out wins, so the
     back that a hang up causes cannot rename it. */
  const ending = useRef('hangup')
  const ended = useRef(false)

  /* Every way out comes here, and then to the shell, which unmounts this and so hangs up. */
  const end = useCallback(function (why) {
    if (ended.current) { return }
    ended.current = true
    ending.current = why || 'hangup'
    onClose(ending.current)
  }, [onClose])

  /* The box takes the focus when it is shown, so the keyboard button is one tap to type. */
  useEffect(function () {
    if (boxOpen && box.current) { box.current.focus() }
  }, [boxOpen])

  /* One call for the life of the overlay. Opened on mount, hung up on unmount, so closing the
     overlay and ending the call are the same act and neither can happen without the other. A
     pause does not close the overlay: it closes the session and opens another on Resume. */
  useEffect(function () {
    const session = createVoiceSession(target ? { agent: target } : undefined)
    call.current = session
    setCallOpen(true)
    let alive = true

    const offState = session.onState(function (next) {
      if (alive) { setState(next) }
    })

    const offTurn = session.onTranscript(function (row) {
      if (!alive) { return }
      setTurns(function (held) {
        /* Every line has a row id, so a fragment that lands after she has started answering
           grows the line it belongs to rather than starting another one. An empty text is a line
           that turned out to be nothing but punctuation, and it comes off the screen. */
        const at = held.findIndex(function (r) { return r.id === row.id })
        if (at < 0) { return row.text ? keepRows(held.concat([row])) : held }
        if (!row.text) { return held.filter(function (r) { return r.id !== row.id }) }
        const next = held.slice()
        next[at] = row
        return next
      })
    })

    const offProposal = session.onProposal(function (next) {
      if (alive) { setProposal(next) }
    })

    const offDelegated = session.onDelegated(function (agent) {
      if (!alive) { return }
      setWatched(function (held) { return held.includes(agent) ? held : held.concat([agent]) })
    })

    /* An inbox item he wants to go into, by a tap or by saying so. The shell minimises the call
       and opens that agent's chat; the call keeps running. */
    const offOpen = session.onOpen(function (agent) {
      if (alive && onOpenRef.current) { onOpenRef.current(agent) }
    })

    /* The session asks to be hung up when nobody has spoken for the silence setting. */
    const offEnd = session.onEnd(function (why) {
      if (alive) { end(why) }
    })

    const offCaller = session.onCaller(function (c) {
      if (alive && c && c.name) { setWho(c.name) }
    })

    session.start().then(function (info) {
      if (alive) { setBudget(info.budget || null) }
    }).catch(function (err) {
      if (alive) { setError(err.message) }
    })

    return function () {
      alive = false
      offState(); offTurn(); offProposal(); offDelegated(); offEnd(); offOpen(); offCaller()
      setCallOpen(false)
      session.stop(ending.current)
    }
  }, [end, target])

  /* The ways out that are not a button. Back pops the entry the shell pushed, and the call hangs
     up like the button. A page that goes away, or stays hidden for HIDDEN_WAIT, ends the call at
     once with a beacon, and then closes the overlay, which then has nothing left to post. */
  useEffect(function () {
    let hideTimer = null
    function gone(why) {
      if (call.current) { call.current.endNow(why) }
      end(why)
    }
    // App's one popstate handler hands the back to shell/nav.js, which says when it was ours.
    // Back minimises. Hang up is on its button, and a back gesture is too easy to make by
    // mistake to be the thing that ends a call.
    const offBack = onVoiceBack(function () { if (onMinimise) { onMinimise('back') } })
    function onPageHide() { gone('pagehide') }
    function onVisible() {
      if (document.visibilityState === 'hidden') {
        // Inside the Android app a hidden page is the app off screen, and the app keeps the
        // call alive on purpose. Hang up, the notification and the bubble still end it.
        if (inApp()) { return }
        if (!hideTimer) {
          hideTimer = window.setTimeout(function () { hideTimer = null; gone('hidden') }, HIDDEN_WAIT)
        }
        return
      }
      if (hideTimer) { window.clearTimeout(hideTimer); hideTimer = null }
    }
    window.addEventListener('pagehide', onPageHide)
    document.addEventListener('visibilitychange', onVisible)
    return function () {
      if (hideTimer) { window.clearTimeout(hideTimer) }
      offBack()
      window.removeEventListener('pagehide', onPageHide)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [end, onMinimise])

  /* The clock, who the call is waiting on, and the session ceiling. The daemon refuses a new call
     past the daily one; this is the other, and the warning two minutes before it is spoken rather
     than drawn. While paused the elapsed number stops on its own, because no session is running,
     and the ceiling is left alone until he comes back.

     The pause flag is read here rather than only written by the button, because the call also
     pauses itself while it waits on an agent, and then nobody tapped anything. */
  useEffect(function () {
    if (error) { return undefined }
    const id = window.setInterval(function () {
      const session = call.current
      if (!session) { return }
      const seconds = session.elapsed()
      setSpent(seconds)
      setPaused(session.isPaused())
      const names = session.waitingOn().join(', ')
      setWaitingFor(function (held) { return held === names ? held : names })
      if (session.isPaused()) { return }
      const cap = (budget && budget.session_minutes ? budget.session_minutes : 30) * 60
      if (!warned.current && seconds > cap - WARN_BEFORE) {
        warned.current = true
        session.tell('This call reaches its time limit in two minutes.')
      }
      if (seconds >= cap) { end('cap') }
    }, 1000)
    return function () { window.clearInterval(id) }
  }, [budget, error, end])

  /* A newer item from the same agent replaced one this call holds. The daemon says so on the
     event stream, and the row closes with a note. */
  useEffect(function () {
    if (inboxChange && inboxChange.event === 'replaced' && call.current) {
      call.current.replacedItem(inboxChange.id)
    }
  }, [inboxChange])

  /* The Android app holds the microphone open off screen only while it knows a call is up, and
     its bubble and notification send Mute, Pause, the typed box and Hang up back here. In a
     browser all of this does nothing. See native.js. */
  /* The phone's call notification names who is on the line, as the call screen does. */
  useEffect(function () { tellApp('callName', { name: name }) }, [name])

  useEffect(function () {
    tellApp('callStarted')
    // awayReload.js reads it: a page that comes back after a long time away reloads, but never in
    // a call. A reload put off for the call happens when it ends.
    window.ccCallLive = true
    return function () { window.ccCallLive = false; tellApp('callEnded'); callOver() }
  }, [])

  useEffect(function () {
    tellApp('callState', { muted: muted, paused: paused })
  }, [muted, paused])

  const fromApp = useRef(null)
  useEffect(function () {
    fromApp.current = {
      toggleMute: toggleMute, togglePause: togglePause, minimised: minimised, onRestore: onRestore,
    }
  })
  useEffect(function () {
    return onApp(function (msg) {
      if (msg.do === 'hangup') { end('hangup') }
      if (msg.do === 'mute') { fromApp.current.toggleMute() }
      if (msg.do === 'pause') { fromApp.current.togglePause() }
      if (msg.do === 'keys') {
        setBoxOpen(true)
        if (fromApp.current.minimised && fromApp.current.onRestore) { fromApp.current.onRestore() }
      }
    })
  }, [end])

  /* Hang up takes the focus on the way in, so the keyboard lands on the way out. The same when
     the overlay comes back from the bar, and the bar takes it on the way down. */
  const barOpen = useRef(null)
  const firstShow = useRef(true)
  useEffect(function () {
    if (firstShow.current) {
      firstShow.current = false
      if (hangup.current) { hangup.current.focus() }
      return
    }
    const target = minimised ? barOpen.current : hangup.current
    if (target) { target.focus() }
  }, [minimised])

  /* Escape hangs up, except from inside the typed box with something in it. A half typed
     correction is worth more than one keystroke's convenience, and the box empty is still a
     way out without reaching for the mouse. */
  useEffect(function () {
    function onKey(e) {
      // A dialog over the call took this Escape to close itself (shell/useBackClose.js).
      if (e.key !== 'Escape' || minimised || e.defaultPrevented) { return }
      if (e.target === box.current && box.current && box.current.value) { return }
      e.preventDefault()
      end('hangup')
    }
    document.addEventListener('keydown', onKey)
    return function () { document.removeEventListener('keydown', onKey) }
  }, [end, minimised])

  /* An agent the voice delegated to has finished, or has stopped to ask something. The facts go
     to the call: an open call hears one line in the next gap, and a call that paused itself
     waiting for this agent opens again and says it first. */
  const settled = useCallback(function (agent) {
    if (!call.current) { return }
    call.current.wake({ agent, kind: 'settled' }).catch(function (err) { setError(err.message) })
  }, [])

  const asked = useCallback(function (agent, ask) {
    const what = ask.kind === 'question'
      ? 'a question of its own'
      : `permission to run ${ask.tool || 'a tool'}`
    if (!call.current) { return }
    call.current.wake({ agent, kind: 'ask', asking: what })
      .catch(function (err) { setError(err.message) })
  }, [])

  async function decide(name) {
    if (!proposal || !call.current) { return }
    setBusy(true)
    try {
      await call.current.runTool(name, { id: proposal.id })
    } catch {
      /* The card goes either way. The voice reads the outcome out, and a card that will not
         close is worse than one that closed on a failure. */
    }
    setProposal(null)
    setBusy(false)
  }

  /* Ask or Auto. Ask reads a proposal back and waits for a yes. Auto confirms it the moment it
     is proposed, through the same route, so the daemon still sends it once and still decides. */
  function toggleMode() {
    if (!call.current) { return }
    setMode(call.current.setMode(mode === 'auto' ? 'ask' : 'auto'))
  }

  /* He typed a line. A spelling, a name, a correction: the things speech recognition gets wrong
     and he should not have to repeat. The box empties at once, because the send is the answer. */
  function sendTyped(e) {
    e.preventDefault()
    const said = typing.trim()
    if ((!said && !pic) || !call.current) { return }
    setTyping('')
    if (pic) {
      const url = pic.url
      setPic(null)
      setPicSaid('')
      /* An image never takes the call down. Every failure is a sentence in the transcript. */
      call.current.image(said, url).catch(function () {})
      return
    }
    call.current.typed(said).catch(function (err) { setError(err.message) })
  }

  /* The keyboard button. Shows the box and gives it the focus; pressed again, hides it. */
  function toggleBox() {
    if (boxOpen) { setOnKeys(false) }
    setBoxOpen(!boxOpen)
  }

  /* One image file, from the picker or from a paste, read into a data URL for the send. */
  function takeImage(file) {
    if (!file || !/^image\//.test(file.type || '')) { return }
    if (file.size > IMAGE_MAX) {
      setPic(null)
      setPicSaid('That image is too large. The limit is 8 MB.')
      return
    }
    const reader = new FileReader()
    reader.onload = function () {
      setPicSaid('')
      setPic({ url: String(reader.result || ''), name: file.name || 'Pasted image' })
    }
    reader.onerror = function () { setPicSaid('That image could not be read.') }
    reader.readAsDataURL(file)
  }

  function picked(e) {
    const file = e.target.files && e.target.files[0]
    e.target.value = ''
    takeImage(file)
  }

  /* A pasted image is the same as a picked one. Pasted text is left to the input. */
  function pasted(e) {
    const items = (e.clipboardData && e.clipboardData.items) || []
    for (const item of items) {
      if (item.kind === 'file' && /^image\//.test(item.type)) {
        e.preventDefault()
        takeImage(item.getAsFile())
        return
      }
    }
  }

  /* Mute. The microphone track goes off and nothing else changes: the session stays open, she
     keeps talking, and the meter keeps running. */
  function toggleMute() {
    if (!call.current) { return }
    setMuted(call.current.setMuted(!muted))
  }

  /* Pause and Resume. Pause closes the session and charges it; Resume opens another one carrying
     this call's turns, so she picks the conversation up rather than starting it again. The same
     button handles a call that paused itself on a wait, because a pause is a pause either way. */
  async function togglePause() {
    const session = call.current
    if (!session || holding) { return }
    setHolding(true)
    try {
      if (session.isPaused()) { await session.resume() } else { await session.pause() }
    } catch (err) {
      setError(err.message)
    }
    setPaused(session.isPaused())
    setHolding(false)
  }

  const left = budget ? Math.max(0, budget.left_minutes - spent / 60) : null
  /* The state line. Waiting says who it waits on, because "Paused" alone would read as something
     he did. */
  const label = error ? WORDS.error : (WORDS[state] || state)
  const line = state === 'waiting' && waitingFor && !error ? `${label} on ${waitingFor}` : label
  useEffect(function () {
    const box = logRef.current
    if (box && stick.current) { box.scrollTop = box.scrollHeight }
  })

  /* The bar, while the overlay is down. It lands in the slot App keeps under the top bar. The
     wide button is the bar itself, so a tap anywhere but Mute and Hang up brings the call back. */
  const slot = minimised && typeof document !== 'undefined'
    ? document.getElementById('vc-bar-slot')
    : null
  const bar = minimised ? (
    <div className={'vc-bar' + (proposal ? ' vc-bar-asking' : '')} id="voice-bar" role="region" aria-label={'Call with ' + name}>
      <button
        type="button"
        className="vc-bar-open"
        id="voice-bar-open"
        ref={barOpen}
        aria-label={proposal ? 'Open the call, a proposal is waiting on you' : 'Open the call'}
        onClick={function () { if (onRestore) { onRestore() } }}
      >
        <span className="vc-bar-name">{name}</span>
        <span className="vc-bar-time">{clock(spent)}</span>
        <span className={'vc-bar-state' + (proposal ? ' vc-bar-wait' : '')} id="voice-bar-state">
          {proposal ? 'Waiting on you' : barWord(state, error)}
        </span>
      </button>
      <button
        type="button"
        className={'btn vc-round vc-bar-btn' + (muted ? ' vc-on' : '')}
        id="voice-bar-mute"
        aria-pressed={muted}
        aria-label="Mute the microphone"
        title={muted ? 'Microphone off' : 'Microphone on'}
        disabled={!!error}
        onClick={toggleMute}
      >
        <Mic off={muted} />
      </button>
      <button
        type="button"
        className="btn sm vc-bar-stop"
        id="voice-bar-stop"
        onClick={function () { end('hangup') }}
      >
        Hang up
      </button>
    </div>
  ) : null

  return (
    <>
    {bar ? (slot ? createPortal(bar, slot) : bar) : null}
    <div
      className={'voice' + (onKeys ? ' vc-typing' : '') + (proposal ? ' vc-asking' : '')}
      id="voice"
      role="dialog"
      aria-modal={minimised ? undefined : 'true'}
      aria-labelledby="voice-state"
      hidden={!!minimised}
    >
      <button
        type="button"
        className="btn vc-round vc-min"
        id="voice-min"
        aria-label="Minimise the call"
        title="Minimise, the call keeps running"
        onClick={function () { if (onMinimise) { onMinimise('button') } }}
      >
        <Down />
      </button>
      <button
        type="button"
        className={'vc-mode' + (mode === 'auto' ? ' vc-on' : '')}
        id="voice-mode"
        aria-pressed={mode === 'auto'}
        aria-label="Delegation mode"
        title={mode === 'auto'
          ? 'Auto: a proposal is sent at once'
          : 'Ask: a proposal waits for your yes'}
        disabled={!!error}
        onClick={toggleMode}
      >
        {mode === 'auto' ? 'Auto' : 'Ask'}
      </button>
      <div className="voice-inner">
        <h2 className="vc-name">{name}</h2>
        <p className="vc-time">{clock(spent)}</p>

        <p className="voice-state" id="voice-state">{line}</p>

        {error ? (
          <p className="vc-error" role="alert">{error}</p>
        ) : (
          <div className="vc-log" id="voice-transcript" ref={logRef} aria-live="polite" aria-label="Transcript">
            {turns.map(function (row) {
              /* An inbox item: the agent's name and its line. A tap opens that agent's chat and
                 minimises the call. Told is a faint mark; a closed item sits back. */
              if (row.who === 'brief') {
                return (
                  <p
                    key={row.id}
                    className={'vc-turn vc-brief' + (row.status === 'closed' ? ' vc-brief-done' : '')}
                  >
                    <button
                      type="button"
                      className="vc-brief-open"
                      data-item={row.item}
                      onClick={function () { if (call.current) { call.current.openItem(row.item) } }}
                    >
                      <span className="vc-brief-agent">{row.agent}</span>
                      <span className="vc-brief-text">{row.text}</span>
                    </button>
                    {row.status === 'told' ? <span className="vc-keyed">told</span> : null}
                  </p>
                )
              }
              /* Three kinds of row. His, hers, and the console's own: what it did, which is
                 neither of them speaking. */
              const kind = row.who === 'you' ? 'vc-you' : (row.who === 'note' ? 'vc-note' : 'vc-them')
              return (
                <p
                  key={row.id}
                  className={'vc-turn ' + kind + (row.partial ? ' vc-part' : '')}
                >
                  {row.text}
                  {row.image
                    ? <span className="vc-keyed"> image</span>
                    : (row.typed ? <span className="vc-keyed"> typed</span> : null)}
                </p>
              )
            })}
          </div>
        )}

        {proposal ? (
          <div className="vc-card" role="group" aria-label="Proposed delegation">
            <p className="vc-card-label">Waiting on you</p>
            <p className="vc-card-say">{proposal.readback}</p>
            <div className="vc-card-acts">
              <button
                type="button"
                className="btn primary"
                disabled={busy}
                onClick={function () { decide('confirm') }}
              >
                Confirm
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={function () { decide('cancel') }}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : null}

        {/* The call paused itself and is waiting on an agent. Said in words, because a paused
            call that nobody paused is a thing he has to be told about. */}
        {state === 'waiting' && !error ? (
          <p className="vc-hint" id="voice-waiting" role="status">
            Talking wakes it, or the agent coming back.
          </p>
        ) : null}

        {muted && !error ? <p className="vc-flag" id="voice-muted">Microphone off</p> : null}

        {budget && !error && left < 10 ? (
          <p className="vc-clock">
            {clock(spent)} on this call, {Math.floor(left)} min left today
          </p>
        ) : null}

        {/* Typed to her. Hidden until the keyboard button shows it. One line, Enter sends. It is
            for the things speech gets wrong: a spelling, a name, a correction. The picture button
            on its left, or a paste, adds an image, and the image goes with the words or alone. A
            paused call comes back and then sends it. */}
        {!error && boxOpen ? (
          <form className="vc-type" id="voice-box" onSubmit={sendTyped}>
            <div className="vc-field">
              <button
                type="button"
                className="vc-pick"
                id="voice-pick"
                aria-label="Add an image"
                title="Add an image"
                onClick={function () { if (picker.current) { picker.current.click() } }}
              >
                <Picture />
              </button>
              <input
                ref={picker}
                type="file"
                accept="image/*"
                hidden
                onChange={picked}
              />
              {pic ? (
                <span className="vc-chip" id="voice-chip">
                  <img className="vc-chip-img" src={pic.url} alt={pic.name} />
                  <button
                    type="button"
                    className="vc-chip-x"
                    aria-label="Remove the image"
                    title="Remove the image"
                    onClick={function () { setPic(null) }}
                  >
                    <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                      <line x1="3" y1="3" x2="9" y2="9" />
                      <line x1="9" y1="3" x2="3" y2="9" />
                    </svg>
                  </button>
                </span>
              ) : null}
              <input
                ref={box}
                id="voice-type"
                className="vc-type-in"
                type="text"
                value={typing}
                autoComplete="off"
                placeholder={'Type to ' + name}
                aria-label={'Type to ' + name}
                onFocus={function () { setOnKeys(true) }}
                onBlur={function () { setOnKeys(false) }}
                onChange={function (e) { setTyping(e.target.value) }}
                onPaste={pasted}
              />
            </div>
            <button type="submit" className="btn vc-type-go" disabled={!typing.trim() && !pic}>
              Send
            </button>
          </form>
        ) : null}

        {!error && boxOpen && picSaid ? <p className="vc-hint" role="status">{picSaid}</p> : null}

        <div className="vc-controls" role="group" aria-label="Call controls">
          <span className="vc-k">
          <button
            type="button"
            className={'btn vc-round' + (muted ? ' vc-on' : '')}
            id="voice-mute"
            aria-pressed={muted}
            aria-label="Mute the microphone"
            title={muted ? 'Microphone off' : 'Microphone on'}
            disabled={!!error}
            onClick={toggleMute}
          >
            <Mic off={muted} />
          </button>
          {muted ? 'Muted' : 'Mute'}
          </span>

          {app ? (
            <span className="vc-k">
            <button
              type="button"
              className={'btn vc-round' + (speaker ? ' vc-shown' : '')}
              id="voice-speaker"
              aria-pressed={speaker}
              aria-label="Speaker"
              disabled={!!error}
              onClick={function () {
                const next = !speaker
                setSpeaker(next)
                askApp('setSpeaker', { on: next }).then(function (r) { if (r && typeof r.on === 'boolean') { setSpeaker(r.on) } })
              }}
            >
              <Speaker on={speaker} />
            </button>
            Speaker
            </span>
          ) : null}

          <span className="vc-k">
          <button
            type="button"
            className={'btn vc-round' + (boxOpen ? ' vc-shown' : '')}
            id="voice-keys"
            aria-pressed={boxOpen}
            aria-label={'Type to ' + name}
            title={boxOpen ? 'Hide the typed box' : 'Type to ' + name}
            disabled={!!error}
            onClick={toggleBox}
          >
            <Keys />
          </button>
          Type
          </span>

          <span className="vc-k">
          <button
            type="button"
            className={'btn vc-round' + (paused ? ' vc-shown' : '')}
            id="voice-pause"
            aria-label={paused ? 'Resume' : 'Pause'}
            disabled={!!error || holding}
            onClick={togglePause}
          >
            {paused ? <Play /> : <Hold />}
          </button>
          {paused ? 'Resume' : 'Pause'}
          </span>

          <span className="vc-k">
          <button type="button" className="btn vc-round voice-stop" id="voice-stop" aria-label="Hang up" ref={hangup} onClick={function () { end('hangup') }}>
            <HangUp />
          </button>
          Hang up
          </span>
        </div>
      </div>

      {/* Nothing is drawn for these. Each one holds one delegated agent's socket and tells the
          voice when that agent finishes or stops to ask. */}
      {watched.map(function (agent) {
        return <Delegated key={agent} agent={agent} onSettled={settled} onAsk={asked} />
      })}
    </div>
    </>
  )
}
