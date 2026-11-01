/* The chat screen. One agent is open, the rail picks which, and the sub tabs choose between the
   conversation, the raw terminal, and the pages this agent built for you.

   WHICH AGENT IS OPEN
   The address bar decides. The fragment is `#/a/<id>`, the same shape the old console used, and
   the Fleet screen passes it with onScreen('chat', '#/a/<id>'). This screen reads it on mount
   and again on every change of address, so the back button and a pasted link both work. With no
   fragment, or one naming an agent that is gone, it opens the first awake agent, or the first
   agent there is. The rail writes the fragment rather than calling back into this screen, so one
   place decides and nothing can disagree with the address bar. A pick in the rail is one history
   entry, so back returns to the agent before it.

   WHAT TALKS TO THE DAEMON
   Nothing here fetches. useFleet holds the agent records, useAgentSocket holds the live turn,
   useHistory holds the settled conversation, and api.js posts. The one exception is the file
   drop, which PUTs raw bytes and so cannot go through the JSON helper.

   The shell owns the voice overlay. The front desk is called from the orb in the top bar; the composer microphone dictates. */

import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import Rail from '../../shell/Rail'
import useGlider from '../../shell/useGlider'
import cc from '../../data/api'
import useAgentSocket from '../../data/useAgentSocket'
import useFleet from '../../data/useFleet'
import useHistory from '../../data/useHistory'
import useAgentCalls from '../../data/useAgentCalls'
import useAgentShown from '../../data/useAgentShown'
import { aimCall } from '../../shell/callTarget'
import useEcho from '../../data/echo'
import AgentHead from './AgentHead'
import Composer from './Composer'
import Files from './Files'
import FilesTab, { unseen } from './FilesTab'
import Log from './Log'
import PageFrame from './PageFrame'
import BrowserView from './BrowserView'
import OpenTab from './OpenTab'
import TaskPin from './TaskPin'
import useTaskPin, { takePrefix, giveBack, pinReopens, unpin } from '../../data/taskPin'
import { loadText, saveText } from '../../data/drafts'
import useFaces from './faces'
import useNotes from './notes'
import { showsIn } from '../../shell/views'
import { go, onRoute } from '../../shell/nav'
import lazyFile from '../../shell/lazyFile'
import './chat.css'

/* The terminal is its own file, fetched the first time its tab opens, as xterm itself is. */
const Terminal = lazyFile(function () { return import('./Terminal') })

// The faces every agent has. Its own pages come after these.
// Terminal is not in the strip. It sits behind the three dots at the strip's end.
const FACES = [
  { id: 'chat', label: 'Chat' },
  { id: 'web', label: 'Browser' },
  { id: 'files', label: 'Files' },
  { id: 'open', label: 'Open' },
]

const FRAGMENT = /^#\/a\/([a-z0-9-]+)/

// How long an answered card holds its receipt before it goes.
const FOLD = 1800

// How long the stop button may stay busy after the daemon took the stop.
const STOP_WAIT = 4000

// Two presses of Escape closer than this are one double Escape, the way Claude Code reads them.
const TWICE = 600

// Something else on the page that Escape belongs to: a menu, a fold, a dialog.
const OWNS_ESCAPE = '[aria-haspopup][aria-expanded="true"], details[open], dialog[open], [aria-modal="true"]'

/* Escape is ours unless the focus is in a field that is not the composer. The question card's
   own text box is such a field: Escape there is the reader leaving the box. */
function typingElsewhere() {
  const el = document.activeElement
  if (!el || el.id === 'msg') { return false }
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable
}

function fromHash() {
  const m = window.location.hash.match(FRAGMENT)
  return m ? m[1] : ''
}

/* A page named after the agent, #/a/<id>/<slug>. Only Open full writes one, so that coming back
   from full screen lands on the page tab rather than on the conversation. */
const PAGE_IN_HASH = /^#\/a\/([a-z0-9-]+)\/([a-z0-9-]+)/

function pageFromHash(id) {
  const m = window.location.hash.match(PAGE_IN_HASH)
  return m && m[1] === id ? m[2] : ''
}

export default function Chat({ onScreen, view, onVoice }) {
  const fleet = useFleet()
  const [want, setWant] = useState(fromHash)
  const [sub, setSub] = useState('chat')
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  /* Goes up once per message this browser sent. The log may jump to the bottom for a message
     the reader typed themselves, and for nothing else the agent does. */
  const [sendTick, setSendTick] = useState(0)
  /* Asks that have been answered and are folding away. The daemon sends ask_done the moment it
     takes the answer, which drops the card from the socket's list, so without this the card
     would vanish under the thumb that pressed it. It stays for FOLD milliseconds as one quiet
     line saying what was decided, and then it goes. */
  const [folded, setFolded] = useState([])
  // Asks whose answer is on its way. See post().
  const [answering, setAnswering] = useState([])
  const [busyAsk, setBusyAsk] = useState(null)
  const [stopping, setStopping] = useState(false)
  const [override, setOverride] = useState(null)
  const [dragging, setDragging] = useState(false)
  // The terminal connects the first time it is looked at and then stays connected, because a
  // socket dropped and rebuilt on every tab switch replays the scrollback each time and resizes
  // the real terminal for everybody else.
  const [termSeen, setTermSeen] = useState(false)
  const [more, setMore] = useState(null)   // where the three dots' menu opens, or null

  useEffect(function () {
    function again() { setWant(fromHash()) }
    window.addEventListener('hashchange', again)
    const off = onRoute(again)
    return function () {
      window.removeEventListener('hashchange', again)
      off()
    }
  }, [])

  /* The view the fleet is switched to hides the same agents here. Otherwise the rail would list
     agents the view hides. */
  const agents = fleet.agents.filter(function (a) { return showsIn(a, view) })
  const named = agents.find(function (a) { return a.id === want })
  const awake = agents.find(function (a) { return a.state === 'green' })
  const agent = named || awake || agents[0] || null
  const id = agent ? agent.id : ''

  /* Reading this chat marks it read: on opening it, whenever it says something new while it is
     open, and when the phone comes back to it. The daemon keeps the mark, so the blue dot goes
     on every screen at once. */
  const saidAt = agent ? agent.last_message_at : null
  /* Where he had read to when this chat opened. Taken once per agent, before the mark below moves
     it, so the log can open on the first message he had not read (Log.jsx). */
  const opened = useRef({ id: null, at: null })
  if (opened.current.id !== id) { opened.current = { id: id, at: agent ? agent.read_at : null } }
  useEffect(function () {
    if (!id) { return undefined }
    function read() {
      if (document.visibilityState === 'visible') { cc.markRead(id).catch(function () {}) }
    }
    read()
    document.addEventListener('visibilitychange', read)
    return function () { document.removeEventListener('visibilitychange', read) }
  }, [id, saidAt])

  /* A task pinned from its card on Insights. The chat opens on the conversation with the
     composer ready, so he can say what he wants about it at once. */
  const pin = useTaskPin()
  const pinned = pin && pin.to === id ? pin.task.id : ''
  useEffect(function () {
    if (!pinned) { return }
    /* oxlint-disable-next-line react/set-state-in-effect -- a pin is an instruction to show the
     * conversation, whatever tab was open for this agent before. */
    setSub('chat')
    const t = window.setTimeout(function () {
      const box = document.getElementById('msg')
      if (box) { box.focus() }
    }, 60)
    return function () { window.clearTimeout(t) }
  }, [pinned])

  const sock = useAgentSocket(id, { rows: 46, cols: 150, wake: false })
  const history = useHistory(id, sock.settledTick)
  const calls = useAgentCalls(id, sock.settledTick)
  /* A job shown in the chat changes its last run in the jobs feed when it finishes, and the
     daemon writes the card before it says so. That moment is when to read the cards again. */
  const jobTick = fleet.jobs.reduce(function (most, j) {
    return j.agent === id && j.show === 'chat' ? Math.max(most, j.last_fire || 0) : most
  }, 0)
  const shownJobs = useAgentShown(id, jobTick)
  const echo = useEcho(sock.echo, history.rows)
  const { notes, say } = useNotes(id, sock.notes)

  const subtabs = useRef(null)
  useGlider(subtabs, sub, { selector: '[aria-selected="true"]' })
  /* On the phone the strip rides the scroll like a browser toolbar, so the reader never has to
     go back up a long log to reach the Terminal. It is the dock that holds its place. */
  const dock = useRef(null)
  const faces = useFaces(dock, sub)

  // The five phone keys type into the pty, and only a socket that was attached when the pty
  // started can reach it. The terminal pane lends this one its own send while it is mounted.
  const termSend = useRef(null)
  const lendKeys = useCallback(function (fn) { termSend.current = fn }, [])

  // A drag anywhere over the page reveals the files strip, so a file can be dropped on an agent
  // that has offered nothing. The timer is the whole of the "the drag ended" logic: dragleave
  // fires on every child the pointer crosses and cannot be trusted on its own.
  const ending = useRef(0)

  // Another agent has other pages, so picking one lands you on its chat, never on a tab that
  // agent does not have.
  useEffect(function () {
    /* oxlint-disable-next-line react/set-state-in-effect -- the agent on screen changed, and
     * the last one's tab, receipts and pending mode must not survive the change. */
    const slug = pageFromHash(id)
    const known = slug && agent && (agent.pages || []).some(function (p) { return p.slug === slug })
    setSub(known ? 'page:' + slug : 'chat')
    // Read once and taken off, so a later visit to this agent opens on the chat as it always did.
    if (slug) { go('chat', '#/a/' + id, true) }
    setFolded([])
    setAnswering([])
    setOverride(null)
    // What he was writing to this agent, as he left it.
    setMessage(loadText(id))
    /* oxlint-disable-next-line react-hooks/exhaustive-deps -- the pages are read at the moment
     * the agent changes and not again; a page list refreshing must not throw the reader off
     * the tab they chose. */
  }, [id])

  /* Which engine the agent should run. The tab you are looking at is the only thing that knows,
     so the browser says it rather than the daemon guessing. Quiet on failure: an agent that is
     asleep must not put a red line over a conversation you were reading. Page tabs say nothing,
     because a page the agent wrote does not care what is running behind it. */
  useEffect(function () {
    if (!id || (sub !== 'chat' && sub !== 'term')) { return }
    cc.setMode(id, sub === 'term' ? 'terminal' : 'chat').catch(function (e) {
      console.warn('mode refused:', e.message)
    })
  }, [id, sub])

  /* The permission mode the record holds is shown at once, because a switcher that stays blank
     until the socket says otherwise reads as broken. The socket's own answer wins as soon as it
     lands, and a mode this browser just set wins until then. */
  const heard = useRef(sock.permMode)
  useEffect(function () {
    if (sock.permMode !== heard.current) {
      heard.current = sock.permMode
      /* oxlint-disable-next-line react/set-state-in-effect -- the daemon is an external system
       * and its answer replaces the mode this browser asked for. */
      setOverride(null)
    }
  }, [sock.permMode])
  const permMode = override || sock.permMode || (agent && agent.permission_mode) || ''

  async function setPermMode(mode) {
    if (!mode || !id) { return }
    try {
      const r = await cc.setPermissionMode(id, mode)
      setOverride(r.mode || mode)
      if (r.mode && r.mode !== mode) { say('The agent is in ' + r.mode + '.') }
    } catch (e) {
      say(e.message)
    }
  }

  /* The model the agent wakes on. It is a field on the record and nothing else: PATCH writes
     it and the next wake reads it, so there is no live effect to wait for. api.js already has
     the call, updateAgent, which is PATCH /api/agents/<id>. */
  async function setModel(pick) {
    if (!id) { return }
    try {
      await cc.updateAgent(id, { model: pick || null })
      await fleet.refresh()
      // Codex starts a process for each message, so its new model counts from the next one.
      const when = agent && agent.engine === 'codex' ? ' from the next message.' : ' at the next wake.'
      say(pick ? 'On ' + pick + when : 'On the default model' + when)
    } catch (e) {
      say(e.message)
    }
  }

  /* The engine the agent wakes on (from user feedback: "an alert should come up that this is
     a big decision"). The daemon changes it only on a sleeping agent, so the switch puts it to
     sleep first. Nothing carries the chat across: the next message starts a new one on the new
     engine, and the old one stays on disk for a switch back. The model is cleared with it,
     because the two engines name their models differently. */
  async function setEngine(next) {
    const was = (agent && agent.engine) || 'claude'
    if (!id || !agent || next === was) { return }
    if (working) { say(agent.name + ' is working. Let it finish, then switch.'); return }
    const names = { claude: 'Claude', codex: 'Codex', opencode: 'OpenCode' }
    const lines = [
      'Switch ' + agent.name + ' from ' + names[was] + ' to ' + names[next] + '?',
      '',
      'This is a big decision.',
      '',
      '- ' + agent.name + ' goes to sleep now.',
      '- Its next message starts a new, empty chat on ' + names[next] + '. It does not see the '
        + names[was] + ' chat. That chat stays on disk and comes back if you switch back.',
      '- It keeps its folder, its CLAUDE.md, its STATE.md and its tools.',
    ]
    if (agent.parent) {
      lines.push('- It stays a delegate of ' + agent.parent + ', and still reports to it.')
    }
    if (!window.confirm(lines.join('\n'))) { return }
    try {
      await cc.sleep(id)
      await cc.updateAgent(id, { engine: next, model: null })
      await fleet.refresh()
      say(agent.name + ' is on ' + names[next] + ' now. Its next message starts a new chat.')
    } catch (e) {
      say(e.message)
    }
  }

  /* said: the words from a dictation sent in the same tap, which the message state does not
     hold yet. */
  async function send(files, said) {
    let text = (said != null ? said : message).trim()
    files = files || []
    if ((!text && !files.length) || !id || sending) { return false }
    setSending(true)
    // A message sent with a task quoted says which task, so the agent needs no guessing. A
    // finished one is taken up again first, so the list shows it open before the agent replies.
    const reopen = pinReopens(id)
    const about = takePrefix(id)
    if (about) { text = about + '\n\n' + text }
    try {
      // Held files go up first, one after another, into the agent's inbox, and their paths ride
      // in the message. Claude Code reads an image from a path, so the agent sees them on this
      // same turn.
      const paths = []
      for (const file of files) { paths.push((await cc.upload(id, file)).path) }
      if (paths.length) {
        text = (text ? text + '\n\n' : '') + paths.map(function (p) { return `[Attached: ${p}]` }).join('\n')
      }
      // The chat composer always wakes. A message held in the mailbox is the right default for
      // a note left from the dashboard and the wrong one for somebody sitting here typing.
      if (reopen) { await cc.taskReopen(reopen) }
      const r = await cc.send(id, text, true)
      // Gone once sent, the way a WhatsApp reply quote goes.
      if (about) { unpin() }
      setMessage('')
      saveText(id, '')
      setSendTick(function (n) { return n + 1 })
      if (r.outcome === 'held') { say('Held. The agent reads it when it wakes.') }
      if (r.outcome === 'failed') { say('The daemon could not deliver that.') }
      // The session file needs a moment. The echo frame draws the message meanwhile.
      history.refresh()
      window.setTimeout(history.refresh, 600)
      window.setTimeout(history.refresh, 1600)
      return true
    } catch (e) {
      if (about) { giveBack(id) }
      say(e.message)
      return false
    } finally {
      setSending(false)
    }
  }

  /* The one place that posts an answer. On ok the receipt shows at once rather than waiting for
     the daemon's ask_done, because a tap has to feel finished. A refusal leaves the card exactly
     where it was, so the ask is not lost, and puts the reason at the foot of the log: a refusal is a
     sentence you may need to read twice. */
  async function post(ask, body) {
    setBusyAsk(ask.id)
    /* ask_done can come before the answer's own reply. The card stays drawn until its receipt
       takes its place, or it went for a moment and the log jumped. */
    setAnswering(function (was) { return was.concat([ask]) })
    try {
      const r = await cc.answer(id, ask.id, body)
      if (r && r.ok === false) { throw new Error(r.error || 'the answer was refused') }
      return true
    } catch (e) {
      say(e.message)
      return false
    } finally {
      setAnswering(function (was) { return was.filter(function (a) { return a.id !== ask.id }) })
      setBusyAsk(null)
    }
  }

  function fold(ask, receipt) {
    setFolded(function (rows) { return rows.concat([{ ask, receipt }]) })
    window.setTimeout(function () {
      setFolded(function (rows) {
        return rows.filter(function (r) { return r.ask.id !== ask.id })
      })
    }, FOLD)
  }

  async function answerPerm(ask, allow) {
    const ok = await post(ask, { allow })
    if (!ok) { return }
    fold(ask, {
      verdict: allow ? 'Approved' : 'Denied',
      detail: allow ? (ask.input && ask.input.command) || ask.summary || '' : '',
      negative: !allow,
    })
  }

  function answerQuestion(ask, question, text) {
    return post(ask, { answers: { [question]: text } })
  }

  function questionDone(ask, text) {
    fold(ask, { verdict: 'Answered', detail: text })
  }

  /* Skip is a deny of the question. The daemon words it so the agent reads that nobody chose,
     not that an option was refused. Not red: skipping is not a failure. */
  async function skipQuestion(ask) {
    const ok = await post(ask, { allow: false })
    if (!ok) { return }
    fold(ask, { verdict: 'Skipped', detail: '' })
  }

  /* The stop button and a single Escape. The daemon interrupts the turn in either engine and the
     agent stays awake. The live reply ends when the engine says the turn settled. */
  async function stopTurn() {
    if (!id || stopping) { return }
    setStopping(true)
    let held = false
    try {
      const r = await cc.stop(id)
      if (r && r.ok === false) { say('The agent did not stop.') } else { held = true }
    } catch (e) {
      say('Stop failed: ' + e.message)
    }
    if (!held) { setStopping(false); return }
    // The daemon has the stop. The button stays busy until the engine says the turn is over,
    // with a ceiling, so a lost frame cannot leave it busy.
    window.setTimeout(function () { setStopping(false) }, STOP_WAIT)
  }

  function sendKey(seq) {
    const fn = termSend.current || sock.send
    if (!fn(seq)) { say('No terminal attached to send that to.') }
  }

  // The turn is over, so the stop is done.
  const working = sock.state === 'thinking'
  useEffect(function () {
    /* oxlint-disable-next-line react/set-state-in-effect -- the engine is an external system and
     * its idle frame is what ends the busy stop. */
    if (!working) { setStopping(false) }
  }, [working])

  /* Escape, the way Claude Code reads it. Once while the agent works stops it. With a card
     waiting, one press does nothing and the second within TWICE skips the oldest card: a
     question is skipped, a permission is denied. A single press cannot stop then, because the
     stop would end the turn and take the card with it before the second press lands. The
     handler reads the newest values through a ref, so it is added once. */
  const pressed = useRef(0)
  const latest = useRef(null)
  const waiting = sock.asks
    .concat(answering.filter(function (a) {
      return !sock.asks.some(function (b) { return b.id === a.id })
    }))
    .filter(function (a) {
      return !folded.some(function (f) { return f.ask.id === a.id })
    })
  useEffect(function () {
    latest.current = { sub, working, waiting, stopTurn, answerPerm,
      skipQuestion, busyAsk }
  })
  useEffect(function () {
    function key(e) {
      if (e.key !== 'Escape' || e.defaultPrevented || e.repeat) { return }
      const now = latest.current
      if (!now || now.sub !== 'chat' || typingElsewhere() || document.querySelector(OWNS_ESCAPE)) { return }
      const ask = now.waiting[0]
      if (ask) {
        const twice = Date.now() - pressed.current < TWICE
        pressed.current = twice ? 0 : Date.now()
        if (!twice || now.busyAsk) { return }
        e.preventDefault()
        if (ask.kind === 'perm') { now.answerPerm(ask, false) } else { now.skipQuestion(ask) }
        return
      }
      pressed.current = 0
      if (now.working) {
        e.preventDefault()
        now.stopTurn()
      }
    }
    window.addEventListener('keydown', key)
    return function () { window.removeEventListener('keydown', key) }
  }, [])

  // Hidden or forgotten. The fragment names an agent that is not there any more, so it goes and
  // the first agent takes its place. A correction, so it replaces the entry and pushes none.
  function gone() {
    go('chat', '', true)
    setWant('')
    fleet.refresh()
  }

  if (!agent) {
    return (
      <section className="screen" id="screen-chat" aria-labelledby="chat-h">
        <div className="screen-head">
          <h1 id="chat-h">Chat</h1>
          <p>{fleet.loading ? 'Reading the fleet.' : fleet.error || 'No agent to talk to yet.'}</p>
        </div>
        {/* A new install opens here with no agent, so the way to start one is one tap away. */}
        {fleet.loading || fleet.error ? null : (
          <button type="button" className="btn sm" onClick={function () { onScreen('fleet') }}>
            Start one on Fleet
          </button>
        )}
      </section>
    )
  }

  /* What the log draws under the conversation: every ask still waiting, then every ask just
     answered, folded to its receipt. An answered one is dropped from the first list so it
     cannot be drawn twice in the moment before ask_done arrives. */
  const cards = waiting
    .map(function (a) { return { ask: a, receipt: null } })
    .concat(folded)

  const pages = agent.pages || []
  const fresh = unseen(id, agent.files)
  function openFace(face) {
    setMore(null)
    // The terminal ends a running chat engine, and every background job the engine started
    // ends with it. A render died that way, so ask first.
    if (face === 'term' && sub !== 'term' && sock.mode === 'chat'
        && agent.state === 'green' && !window.confirm(
      'Opening the terminal ends ' + agent.name + '\'s chat engine. Any job it '
      + 'runs in the background stops. Open the terminal?',
    )) { return }
    // The terminal is built the first time it is asked for and kept after that.
    if (face === 'term') { setTermSeen(true) }
    setSub(face)
  }

  const tabs = FACES.concat(pages.map(function (p) {
    return { id: 'page:' + p.slug, label: p.title || p.slug }
  }))

  // The rail counts the asks waiting on the agent that is open. It cannot count another
  // agent's, because only the open agent has a socket.
  const railAgents = agents.map(function (a) {
    return { ...a, state: a.state === 'green' ? 'up' : 'sleep', needs: a.id === id ? sock.asks.length : 0 }
  })

  function over(e) {
    e.preventDefault()
    setDragging(true)
    window.clearTimeout(ending.current)
    ending.current = window.setTimeout(function () { setDragging(false) }, 300)
  }

  return (
    <section className="screen" id="screen-chat" aria-labelledby="chat-h">
      <div className="agent-page" onDragOver={over}>

        <Rail
          agents={railAgents}
          active={id}
          onPick={function (pick) { go('chat', '#/a/' + pick) }}
        />

        <div className="agent-main">
          <AgentHead
            agent={agent}
            onNote={say}
            onGone={gone}
            onWoke={function () {
              // A wake starts a terminal and the daemon calls the agent terminal mode. This tab
              // is the one that knows what should run, so it says so again.
              if (sub === 'chat' || sub === 'term') {
                return cc.setMode(id, sub === 'term' ? 'terminal' : 'chat').catch(function () {})
              }
            }}
          />

          {/* The dock is the strip's slot in the flow and holds its height on the phone, where
              the strip leaves the flow and floats at the top of the window. */}
          <div className="ch-facedock" ref={dock}>
            <div
              className={'subtabs ch-faces' + (faces.float ? ' ch-float' : '') + (faces.gone ? ' ch-gone' : '')}
              role="tablist"
              aria-label="Agent view"
              ref={subtabs}
            >
              <span className="glider" aria-hidden="true"></span>
              {tabs.map(function (tab) {
                return (
                  <button
                    key={tab.id}
                    type="button"
                    role="tab"
                    id={'tab-' + tab.id}
                    aria-selected={sub === tab.id}
                    aria-controls={'panel-' + tab.id}
                    onClick={function () { openFace(tab.id) }}
                  >
                    {tab.label}
                    {tab.id === 'files' && sub !== 'files' && fresh > 0
                      ? <span className="ac-count ft-count" aria-label={fresh + ' new'}>{fresh}</span>
                      : null}
                  </button>
                )
              })}
              {/* Sticky at the right edge, so the dots show however far the strip scrolls. While
                  the terminal is open they are the selected face, and the glider sits on them. */}
              <button
                type="button"
                role="tab"
                id="tab-more"
                className="ch-more"
                aria-label="More views"
                aria-haspopup="menu"
                aria-expanded={!!more}
                aria-selected={sub === 'term'}
                onClick={function (e) {
                  const r = e.currentTarget.getBoundingClientRect()
                  setMore(more ? null : { top: r.bottom + 6, right: window.innerWidth - r.right })
                }}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
                  <circle cx="5" cy="12" r="1.4"></circle>
                  <circle cx="12" cy="12" r="1.4"></circle>
                  <circle cx="19" cy="12" r="1.4"></circle>
                </svg>
              </button>
            </div>
            {more ? (
              <>
                <div className="ch-more-away" onClick={function () { setMore(null) }}></div>
                <div className="ch-menu-body ch-more-menu" role="menu" style={{ top: more.top, right: more.right }}>
                  <button type="button" role="menuitem" className="ch-menu-item"
                    aria-current={sub === 'term' ? 'true' : undefined}
                    onClick={function () { openFace('term') }}>Terminal</button>
                </div>
              </>
            ) : null}
          </div>

          <Files
            id={id}
            inbox={agent.inbox_count}
            dragging={dragging}
            onNote={say}
            onSent={fleet.refresh}
          />

          <div
            className="agent-panel"
            role="tabpanel"
            id="panel-chat"
            aria-labelledby="tab-chat"
            hidden={sub !== 'chat'}
          >
            <Log
              id={id}
              active={sub === 'chat'}
              rows={history.rows}
              hasMore={history.hasMore}
              loadOlder={history.loadOlder}
              echo={echo}
              live={sock.live}
              said={sock.said}
              held={sock.held}
              tools={sock.tools}
              thinking={sock.state === 'thinking' && !sock.live && !sock.asks.length && sock.mode !== 'terminal'}
              cards={cards}
              busyAsk={busyAsk}
              sendTick={sendTick}
              onPerm={answerPerm}
              onQuestion={answerQuestion}
              onQuestionDone={questionDone}
              onSkip={skipQuestion}
              /* No recap yet. The daemon has no route for one, and the source it will read is
                 the away_summary rows in the agent's session jsonl. */
              notes={notes}
              calls={calls}
              shownJobs={shownJobs}
              name={agent ? agent.name : ''}
              unreadAfter={opened.current.at}
              recap={null}
              onRecapClose={function () {}}
            />

            <Composer
              above={<TaskPin agent={id} />}
              value={message}
              onValue={function (v) { setMessage(v); saveText(id, v) }}
              draftId={id}
              onSend={send}
              sending={sending}
              permMode={permMode}
              onPermMode={setPermMode}
              model={agent.model || ''}
              engine={agent.engine || ''}
              onModel={setModel}
              onEngine={setEngine}
              onKey={sendKey}
              working={working}
              stopping={stopping}
              onStop={stopTurn}
              agentName={agent.name || agent.id}
              onCall={onVoice ? function () { aimCall(agent.id); onVoice() } : null}
            />
          </div>

          <div
            className="agent-panel"
            role="tabpanel"
            id="panel-term"
            aria-labelledby="tab-more"
            hidden={sub !== 'term'}
          >
            {termSeen
              ? (
                <Suspense fallback={null}>
                  <Terminal id={id} active={sub === 'term'} onNote={say} onKeySend={lendKeys} />
                </Suspense>
              )
              : null}
          </div>

          <div
            className="agent-panel"
            role="tabpanel"
            id="panel-web"
            aria-labelledby="tab-web"
            hidden={sub !== 'web'}
          >
            <BrowserView id={id} active={sub === 'web'} />
          </div>

          <div
            className="agent-panel"
            role="tabpanel"
            id="panel-files"
            aria-labelledby="tab-files"
            hidden={sub !== 'files'}
          >
            <FilesTab
              id={id}
              files={agent.files}
              inbox={agent.inbox_count}
              active={sub === 'files'}
              onNote={say}
              onSent={fleet.refresh}
            />
          </div>

          <div
            className="agent-panel"
            role="tabpanel"
            id="panel-open"
            aria-labelledby="tab-open"
            hidden={sub !== 'open'}
          >
            <OpenTab
              id={id}
              name={agent.name || agent.id}
              active={sub === 'open'}
              onPick={function () { setSub('chat') }}
            />
          </div>

          {pages.map(function (page) {
            const tab = 'page:' + page.slug
            return (
              <div
                className="agent-panel"
                role="tabpanel"
                id={'panel-' + tab}
                aria-labelledby={'tab-' + tab}
                hidden={sub !== tab}
                key={page.slug}
              >
                {sub === tab ? <PageFrame id={id} page={page} fleet={fleet} /> : null}
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}
