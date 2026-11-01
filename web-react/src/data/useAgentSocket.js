import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { socketUrl } from './api'

/* One WebSocket per agent, shared by everybody who asks for it.
 *
 * `/ws/agent/<id>` carries both engines. Text frames are the chat engine talking, and they carry
 * a `t`. The terminal's own notices travel on the same socket and carry a `type`, so each side
 * reads only its own. Binary frames are raw terminal bytes and go untouched to whoever asked for
 * them, escape sequences and all.
 *
 * The old console opened two sockets, one for the chat tab and one for the terminal tab, because
 * each tab built and dropped its own whenever you looked at it. A hook removes that reason: the
 * socket belongs to the hook, not to a tab, so the chat and the terminal share one.
 *
 * Sharing is keyed by agent AND by wake, and that is not fussiness. wake=1 is the terminal tab
 * asking the daemon to start a terminal. The chat asks wake=0 so that reading yesterday's
 * conversation never wakes a sleeping agent. Those two cannot be the same socket, so a chat and
 * a terminal on one agent still make two, while two chats or two terminals make one.
 */

const BLANK = {
  /* Which engine is running: chat, terminal, or off. off is the resting state of a sleeping
   * agent, not a fault. */
  mode: 'off',
  /* idle or thinking, asked of Claude Code rather than guessed from output. */
  state: 'idle',
  permMode: '',
  /* The whole reply so far. Every delta carries the whole text, never a piece to append. */
  live: '',
  /* The texts this turn wrote before its latest tool call. The daemon empties the live reply when
   * a tool starts, because the record will have that text. The record is read only when the turn
   * settles, so until then these stay on screen above the live reply. */
  said: [],
  /* The finished turn: its texts and tool chips, kept from `settled` until the history fetch
   * brings the saved rows. Dropping them first made the page thousands of pixels shorter for a
   * moment, and the browser moved the reader to the new bottom. Log.jsx decides when they go. */
  held: null,
  tools: [],
  asks: [],
  echo: null,
  notes: [],
  /* Goes up once per settled turn. useHistory watches it and refetches. */
  settledTick: 0,
  terminalNotices: [],
}

const OPEN = new Map()

const raf = (fn) =>
  typeof requestAnimationFrame === 'function' ? requestAnimationFrame(fn) : setTimeout(fn, 16)

/* An ask with no question in it is a card with nothing on it. Say so instead of drawing one. */
const EMPTY_ASK = 'An ask arrived with no question in it.'

function makeEntry(key, id, opts) {
  return {
    key, id, opts: { ...opts },
    sock: null,
    retry: null,
    settleTimer: null,
    refs: 0,
    dead: false,
    listeners: new Set(),
    binary: new Set(),
    /* The mutable truth. `snap` is the copy React is allowed to see. */
    s: { ...BLANK },
    snap: BLANK,
    pending: false,
  }
}

/* One React update per animation frame, however many frames arrive. A long reply sends a delta
 * per token, and a setState per token is a render per token. */
function touch(entry) {
  if (entry.pending || entry.dead) return
  entry.pending = true
  raf(() => {
    entry.pending = false
    if (entry.dead) return
    entry.snap = { ...entry.s }
    for (const fn of entry.listeners) fn(entry.snap)
  })
}

function note(entry, text) {
  entry.s.notes = [...entry.s.notes, { text, at: Date.now() }]
}

function notice(entry, msg) {
  entry.s.terminalNotices = [...entry.s.terminalNotices.slice(-9), { ...msg, at: Date.now() }]
}

/* `end` says the writing stopped. `settled` says the record has it. Between the two the reply
 * stays on screen: dropping it at `end` blanked a finished answer for the second or two the
 * session file took to catch up, and that came back looking like a bug.
 *
 * If `settled` never arrives, this settles the turn anyway after 8 s. Without it a reply that
 * lost its last frame would sit above its own settled twin for as long as the tab stayed open,
 * the same words twice. */
function settleSoon(entry, arm) {
  if (entry.settleTimer) {
    clearTimeout(entry.settleTimer)
    entry.settleTimer = null
  }
  if (!arm) return
  entry.settleTimer = setTimeout(() => {
    entry.settleTimer = null
    settle(entry)
    touch(entry)
  }, 8000)
}

function settle(entry) {
  const texts = [...entry.s.said, entry.s.live].filter((t) => String(t || '').trim())
  if (texts.length || entry.s.tools.length) entry.s.held = { texts, tools: entry.s.tools }
  entry.s.said = []
  entry.s.live = ''
  entry.s.tools = []
  entry.s.settledTick += 1
}

/* Every frame is a statement about now, never a patch on something said earlier, so one arriving
 * late or not at all leaves the state correct rather than half built. */
function chatFrame(entry, m) {
  switch (m.t) {
    case 'mode':
      entry.s.mode = m.mode || 'off'
      break
    case 'state':
      entry.s.state = m.state || 'idle'
      break
    case 'live':
      if (m.phase === 'start') {
        /* A new text in the same turn. What was written before stays, and so do the chips: taking
         * either away shrank the page under the reader. */
        if (entry.s.live.trim()) entry.s.said = [...entry.s.said, entry.s.live]
        entry.s.live = ''
        settleSoon(entry, false)
      } else if (m.phase === 'delta') {
        if (!m.text && entry.s.live.trim()) entry.s.said = [...entry.s.said, entry.s.live]
        entry.s.live = m.text || ''
        settleSoon(entry, false)
      } else if (m.phase === 'end') {
        settleSoon(entry, true)
      }
      break
    case 'tool':
      entry.s.tools = [...entry.s.tools, { name: m.name || 'tool', summary: m.summary || '' }]
      break
    case 'settled':
      settleSoon(entry, false)
      settle(entry)
      break
    case 'echo':
      /* Your own message, back at once, before the record has it. */
      if (m.text) {
        entry.s.echo = { text: m.text, at: Date.now() }
        entry.s.held = null
      }
      break
    case 'note':
      note(entry, m.text || '')
      break
    /* A repeated id is a card already on screen. The daemon re-sends every waiting ask on each
     * attach, so a reconnect would otherwise stack the same question twice. */
    case 'ask':
      if (!m.id || entry.s.asks.some((a) => a.id === m.id)) break
      if (m.kind === 'question' && !(m.questions || []).length) {
        note(entry, EMPTY_ASK)
        break
      }
      entry.s.asks = [...entry.s.asks, m]
      break
    /* Answered elsewhere, or withdrawn. The card goes without a word: whoever answered it in
     * another window knows they did. */
    case 'ask_done':
      entry.s.asks = entry.s.asks.filter((a) => a.id !== m.id)
      break
    case 'perm_mode':
      entry.s.permMode = m.mode || ''
      break
    default:
      break
  }
}

function connect(entry) {
  if (entry.dead) return
  const sock = new WebSocket(socketUrl(entry.id, entry.opts))
  sock.binaryType = 'arraybuffer'
  entry.sock = sock

  sock.onopen = () => {
    /* The daemon sends mode, state, perm_mode, the turn in flight and every pending ask on every
     * attach, so the state is rebuilt from frames alone. Clearing first is what makes a
     * reconnect correct: a turn that finished while the socket was down would otherwise sit
     * there forever, and an ask answered on a phone would come back as a card. */
    entry.s.live = ''
    entry.s.said = []
    entry.s.held = null
    entry.s.tools = []
    entry.s.asks = []
    notice(entry, { type: 'connecting' })
    touch(entry)
  }

  sock.onmessage = (ev) => {
    if (typeof ev.data !== 'string') {
      /* Terminal bytes. Nobody registered means nobody is drawing a terminal, so they go. */
      if (entry.binary.size) {
        const bytes = new Uint8Array(ev.data)
        for (const fn of entry.binary) fn(bytes)
      }
      return
    }
    let msg
    try { msg = JSON.parse(ev.data) } catch { return }
    if (msg.t) {
      chatFrame(entry, msg)
      touch(entry)
    } else if (msg.type) {
      /* attached, grey, exited, error. The terminal reads the last one for its status line. */
      notice(entry, msg)
      touch(entry)
    }
  }

  /* A phone that sleeps loses its socket without telling anybody, and wakes to a chat that never
   * updates again. Reattaching costs nothing and is the whole catch up. */
  sock.onclose = () => {
    if (entry.dead) return
    notice(entry, { type: 'detached' })
    touch(entry)
    entry.retry = setTimeout(() => {
      entry.retry = null
      if (!entry.dead) connect(entry)
    }, 1500)
  }

  sock.onerror = () => {
    /* onclose follows and does the reconnecting. Nothing to say here. */
  }
}

function acquire(id, opts) {
  const key = `${id}|${opts.wake ? 1 : 0}`
  let entry = OPEN.get(key)
  if (!entry) {
    entry = makeEntry(key, id, opts)
    OPEN.set(key, entry)
    connect(entry)
  }
  entry.refs += 1
  return entry
}

function release(entry) {
  entry.refs -= 1
  if (entry.refs > 0) return
  entry.dead = true
  OPEN.delete(entry.key)
  if (entry.retry) clearTimeout(entry.retry)
  if (entry.settleTimer) clearTimeout(entry.settleTimer)
  entry.binary.clear()
  entry.listeners.clear()
  try { entry.sock?.close() } catch { /* already gone */ }
  entry.sock = null
}

/* The chat asks 46x150 on purpose. A terminal is fitted to its smallest watcher, so asking for
 * 150 columns never shrinks anybody: if a phone is attached at 40, it is already 40. */
export function useAgentSocket(id, { rows = 46, cols = 150, wake = false } = {}) {
  const entryRef = useRef(null)
  /* The size the socket should ask for when it next opens. Kept in a ref and updated in an
   * effect, so a resize does not tear the socket down and rebuild it. */
  const sizeRef = useRef({ rows, cols })
  /* Who wants terminal bytes, held here rather than on the socket. A child effect runs before
   * its parent's, so a terminal can register before the socket exists. */
  const wantBytes = useRef(new Set())

  useEffect(() => { sizeRef.current = { rows, cols } }, [rows, cols])

  /* useSyncExternalStore rather than an effect with setState. The socket IS an external store:
   * React subscribes, the store hands back the snapshot it already built, and nothing renders
   * twice on mount. Subscribing is also where the socket is acquired and released, so the
   * refcount follows the mounted consumers exactly. */
  const key = `${id}|${wake ? 1 : 0}`

  const subscribe = useCallback((cb) => {
    if (!id) return () => {}
    const entry = acquire(id, { ...sizeRef.current, wake })
    entryRef.current = entry
    entry.listeners.add(cb)
    const mine = wantBytes.current
    for (const fn of mine) entry.binary.add(fn)
    return () => {
      entry.listeners.delete(cb)
      for (const fn of mine) entry.binary.delete(fn)
      release(entry)
      if (entryRef.current === entry) entryRef.current = null
    }
  }, [id, wake])

  /* Cached by the store, so the same state gives the same object and React stops there. */
  const getSnapshot = useCallback(() => (id && OPEN.get(key)?.snap) || BLANK, [id, key])

  const snap = useSyncExternalStore(subscribe, getSnapshot, () => BLANK)

  /* Keystrokes go back as binary, so a paste with a newline in it is bytes rather than an
   * accidental submit. */
  const send = useCallback((bytes) => {
    const sock = entryRef.current?.sock
    if (!sock || sock.readyState !== 1) return false
    sock.send(typeof bytes === 'string' ? new TextEncoder().encode(bytes) : bytes)
    return true
  }, [])

  const resize = useCallback((r, c) => {
    const entry = entryRef.current
    if (!entry) return false
    /* Remembered as well as sent, so a reconnect asks for the size the terminal is now. */
    entry.opts = { ...entry.opts, rows: r, cols: c }
    if (entry.sock?.readyState !== 1) return false
    entry.sock.send(JSON.stringify({ type: 'resize', rows: r, cols: c }))
    return true
  }, [])

  const subscribeBinary = useCallback((fn) => {
    wantBytes.current.add(fn)
    entryRef.current?.binary.add(fn)
    return () => {
      wantBytes.current.delete(fn)
      entryRef.current?.binary.delete(fn)
    }
  }, [])

  return useMemo(
    () => ({ ...snap, send, resize, subscribeBinary }),
    [snap, send, resize, subscribeBinary],
  )
}

export default useAgentSocket
