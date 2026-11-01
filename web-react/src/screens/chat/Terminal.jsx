/* The terminal tab. The emergency hatch, and nothing here is recorded as a turn.

   It connects the first time it is looked at, not when the page opens. Attaching is not free:
   it replays the scrollback and, because a terminal is fitted to its smallest watcher, it
   resizes the real terminal for everybody else too. Doing that for a page you opened to read a
   chart would be rude to whoever is working in it.

   Its socket asks wake=1, which is the whole difference from the chat's. The hook keys its
   sharing on that flag, so the chat and the terminal hold two sockets on one agent, which is
   right: they are asking the daemon for two different things. */

import { useCallback, useEffect, useRef, useState } from 'react'
import cc from '../../data/api'
import useAgentSocket from '../../data/useAgentSocket'
import { PHONE_KEYS, SEQ } from './keys'
import { loadXterm, termTheme } from './vendor'

/* A known gap in the daemon. The receive loop holds the pty as a local, taken when the socket
   attached, so a terminal started AFTER that is invisible to this socket for as long as it
   lives: resize, keystrokes and the exit notice all go nowhere. Waking therefore has to be
   followed by a new socket. The hook has no reconnect() to call, so the pane is remounted by
   its key, which releases the old socket and opens a fresh one. */
export default function Terminal({ id, active, onNote, onKeySend }) {
  const [generation, setGeneration] = useState(0)

  return (
    <Pane
      key={id + '#' + generation}
      id={id}
      active={active}
      onNote={onNote}
      onKeySend={onKeySend}
      onWoke={function () { setGeneration(function (n) { return n + 1 }) }}
    />
  )
}

function Pane({ id, active, onNote, onKeySend, onWoke }) {
  const box = useRef(null)
  const term = useRef(null)
  const fit = useRef(null)
  // Bytes that arrived before the emulator was ready. The daemon sends the scrollback as one
  // binary frame the moment it attaches, and the emulator is 290 KB away over the network, so
  // without this the first screenful is lost every time.
  const early = useRef([])
  const [size, setSize] = useState({ rows: 32, cols: 100 })
  const [waking, setWaking] = useState(false)
  const [broken, setBroken] = useState('')

  const sock = useAgentSocket(id, { rows: size.rows, cols: size.cols, wake: true })
  const { send, resize, subscribeBinary } = sock
  const last = sock.terminalNotices[sock.terminalNotices.length - 1] || { type: 'connecting' }

  /* Measure, then tell the daemon. A terminal drawing at the wrong width is a column of
     fragments, and the daemon cannot know the width of a browser it never sees. */
  const refit = useCallback(function () {
    if (!fit.current || !term.current) { return }
    try { fit.current.fit() } catch { return }
    const rows = term.current.rows
    const cols = term.current.cols
    setSize({ rows, cols })
    resize(rows, cols)
  }, [resize])

  // The emulator, built once for this pane and disposed with it.
  useEffect(function () {
    let gone = false
    loadXterm().then(function (w) {
      if (gone || !box.current) { return }
      const t = new w.Terminal({
        fontFamily: '"JetBrains Mono", ui-monospace, monospace',
        fontSize: 12.5,
        lineHeight: 1.35,
        letterSpacing: 0,
        cursorBlink: true,
        scrollback: 8000,
        theme: termTheme(),
        allowProposedApi: true,
      })
      const f = new w.FitAddon.FitAddon()
      t.loadAddon(f)
      t.open(box.current)
      term.current = t
      fit.current = f
      try { f.fit() } catch { /* a hidden panel has no size to fit to */ }
      setSize({ rows: t.rows, cols: t.cols })
      for (const bytes of early.current) { t.write(bytes) }
      early.current = []
      /* Keystrokes go back as binary, so a paste with a newline in it is bytes rather than an
         accidental submit. */
      t.onData(function (d) { send(d) })
    }).catch(function (e) {
      if (!gone) { setBroken(e.message) }
    })

    return function () {
      gone = true
      try { term.current?.dispose() } catch { /* already gone */ }
      term.current = null
      fit.current = null
    }
  }, [send])

  // Terminal bytes, straight through, escape sequences and all. That is what makes this tab 1:1
  // with the real screen rather than a rendering of the conversation.
  useEffect(function () {
    return subscribeBinary(function (bytes) {
      if (term.current) { term.current.write(bytes) } else { early.current.push(bytes) }
    })
  }, [subscribeBinary])

  // The box changes size when the panel is shown, when the window moves, and when the phone
  // keys open under it. All three are the same answer.
  useEffect(function () {
    const el = box.current
    if (!el || typeof ResizeObserver === 'undefined') { return undefined }
    const ro = new ResizeObserver(function () { refit() })
    ro.observe(el)
    window.addEventListener('resize', refit)
    return function () {
      ro.disconnect()
      window.removeEventListener('resize', refit)
    }
  }, [refit])

  /* The five phone keys in the chat composer type into the pty, and the pty can only be reached
     by a socket that was attached when it started. This one was, so the composer borrows it.
     Without this it would use the chat's socket, which the daemon drops when no terminal was
     running at the moment that socket opened. */
  useEffect(function () {
    if (!onKeySend) { return undefined }
    onKeySend(send)
    return function () { onKeySend(null) }
  }, [onKeySend, send])

  // A hidden panel has no width, so the fit taken while it was hidden was wrong. Take it again.
  useEffect(function () {
    if (active) { refit() }
  }, [active, refit])

  // The daemon says attached once the pty is on the other end, and asks for a size next.
  useEffect(function () {
    if (last.type === 'attached') { refit() }
  }, [last.type, last.at, refit])

  async function wake() {
    setWaking(true)
    try {
      await cc.wake(id)
      onNote('Waking it. The terminal reattaches in a moment.')
      // The socket that is open now cannot see the pty that just started, so it is replaced.
      onWoke()
    } catch (e) {
      onNote(e.message)
      setWaking(false)
    }
  }

  async function stop() {
    try { await cc.stop(id) } catch (e) { onNote(e.message) }
  }

  const status = {
    connecting: 'connecting',
    attached: last.pid ? 'live · pid ' + last.pid : 'live',
    grey: 'asleep',
    exited: ('exited ' + (last.status == null ? '' : last.status)).trim(),
    error: 'error',
    detached: 'detached',
  }[last.type] || last.type

  return (
    <>
      <div className="termbar">
        <span className="chip">
          <span className={last.type === 'attached' ? 'dot up' : 'dot sleep'} aria-hidden="true"></span>
          {' ' + status}
        </span>
        <span className="chip"><span className="mono">{size.rows + 'x' + size.cols}</span></span>
        <span className="grow"></span>
        <button type="button" className="btn sm danger" onClick={stop}>Stop task</button>
        <button type="button" className="btn sm" onClick={refit}>Fit</button>
      </div>

      {last.type === 'grey' ? (
        <div className="ch-asleep">
          <span>This agent is asleep. Waking it resumes the same conversation.</span>
          <button type="button" className="btn sm primary" disabled={waking} onClick={wake}>
            Wake it
          </button>
        </div>
      ) : null}

      {last.type === 'exited' ? (
        <p className="ch-asleep">The terminal closed. The conversation is still on disk.</p>
      ) : null}

      {last.type === 'error' && last.message ? (
        <p className="ch-asleep">{last.message}</p>
      ) : null}

      {broken ? <p className="ch-asleep">{'The terminal could not load: ' + broken}</p> : null}

      <div className="term" ref={box} role="group" aria-label="Terminal"></div>

      <div className="phonekeys">
        {PHONE_KEYS.map(function (key) {
          return (
            <button
              key={key.id}
              type="button"
              aria-label={key.label}
              onClick={function () { send(SEQ[key.id]) }}
            >
              {key.text}
            </button>
          )
        })}
      </div>

      <p className="term-note">
        This is the emergency hatch. Use it when the chat cannot answer a dialog, or when you
        want to see the raw session. Everything here is manual. Nothing on this tab is recorded
        as a turn.
      </p>
    </>
  )
}
