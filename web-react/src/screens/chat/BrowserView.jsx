/* The agent's own browser, live, and the way to take it over.

   The daemon streams the page as pictures while this view is open, and only then: nothing is sent
   while nobody looks. Watching keeps the browser from idling shut. "Take over" gives the owner the
   page: his taps, typing and keys go to it, and the agent's own actions wait until he hands it
   back. That is how a login, a captcha or a two-factor code gets done.

   Opening the tab does not start a closed browser by itself; the agent starts it by using it, or
   "Open it" does.

   ZOOM, AND A PHONE LAYOUT ON ASK
   The page stays the desktop page the agent works on. Two fingers zoom the picture in, up to four
   times, and one finger moves around it while zoomed; a tap lands on the right spot at any zoom,
   because it is measured on the picture as drawn. "Phone layout" asks the page itself to take this
   panel's size in mobile mode, the way OpenClaw fits its panel, and it goes back to desktop when
   he turns it off or leaves. */

import { useEffect, useRef, useState } from 'react'

const BASE = (import.meta.env && import.meta.env.VITE_CC_API) || ''
const PHONE = typeof window !== 'undefined' && (!!window.Capacitor || /Android|iPhone|iPad/i.test(navigator.userAgent || ''))

function wsUrl(id) {
  const u = new URL(`/ws/browser/${id}`, BASE || window.location.origin)
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'
  return u.toString()
}

export default function BrowserView({ id, active }) {
  const [status, setStatus] = useState(null)
  const [want, setWant] = useState(false)
  const [frame, setFrame] = useState(null)
  const [control, setControl] = useState(false)
  const [error, setError] = useState('')
  const [typing, setTyping] = useState('')
  const sock = useRef(null)
  const size = useRef({ w: 1366, h: 900 })
  const img = useRef(null)
  const drag = useRef(null)
  const screen = useRef(null)
  const touches = useRef(new Map())
  const pinch = useRef(null)
  const [zoom, setZoom] = useState({ k: 1, x: 0, y: 0 })
  const [phone, setPhone] = useState(false)
  /* A login he typed while in control, offered for the vault: {site, user}, then what happened. */
  const [offer, setOffer] = useState(null)
  const [saved, setSaved] = useState('')

  function togglePhone() {
    const next = !phone
    setPhone(next)
    setZoom({ k: 1, x: 0, y: 0 })
    if (next) {
      const el = screen.current
      const w = el ? Math.round(el.getBoundingClientRect().width) : 390
      send({ t: 'fit', w: w, h: Math.round(Math.min(window.innerHeight * 0.72, w * 1.9)), mobile: PHONE })
    } else {
      send({ t: 'unfit' })
    }
  }

  // Is it open? Asked when the tab is shown, never on a timer.
  useEffect(function () {
    if (!active) { return undefined }
    let live = true
    fetch(BASE + '/api/browser').then(function (r) { return r.json() }).then(function (s) {
      if (live) { setStatus(s[id] || { open: false }) }
    }, function () { if (live) { setStatus({ open: false }) } })
    return function () { live = false }
  }, [active, id])

  const watching = active && (want || (status && status.open))

  useEffect(function () {
    if (!watching) { return undefined }
    const ws = new WebSocket(wsUrl(id))
    sock.current = ws
    ws.onopen = function () { setError('') }
    ws.onmessage = function (ev) {
      let m
      try { m = JSON.parse(ev.data) } catch { return }
      if (m.t === 'frame') {
        if (m.w && m.h) { size.current = { w: m.w, h: m.h } }
        setFrame('data:image/jpeg;base64,' + m.data)
      } else if (m.t === 'state') {
        setControl(!!m.control)
      } else if (m.t === 'save_login') {
        setOffer({ site: m.site, user: m.user || '', busy: false })
        setSaved('')
      } else if (m.t === 'saved') {
        setOffer(null)
        setSaved(m.saved ? 'Saved to the vault as ' + m.title + '. The agent can now sign in there by itself.' : '')
      } else if (m.t === 'error') {
        setOffer(null)
        setError(m.message || 'The browser said no.')
      }
    }
    ws.onclose = function () { if (sock.current === ws) { sock.current = null } }
    return function () { ws.close(); sock.current = null; setControl(false); setPhone(false) }
  }, [watching, id])

  function send(msg) {
    const ws = sock.current
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify(msg)) }
  }

  // A point on the picture, as a point on the page.
  function at(e) {
    const el = img.current
    if (!el) { return null }
    const r = el.getBoundingClientRect()
    return { x: (e.clientX - r.left) / r.width * size.current.w, y: (e.clientY - r.top) / r.height * size.current.h }
  }

  function down(e) {
    touches.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (touches.current.size === 2) {
      const [p, q] = [...touches.current.values()]
      pinch.current = { d: Math.hypot(p.x - q.x, p.y - q.y), k: zoom.k, x: zoom.x, y: zoom.y, cx: (p.x + q.x) / 2, cy: (p.y + q.y) / 2 }
      drag.current = null
      return
    }
    drag.current = { x: e.clientX, y: e.clientY, at: at(e), moved: false, zx: zoom.x, zy: zoom.y, sx: e.clientX, sy: e.clientY }
  }
  function move(e) {
    if (touches.current.has(e.pointerId)) { touches.current.set(e.pointerId, { x: e.clientX, y: e.clientY }) }
    if (pinch.current && touches.current.size === 2) {
      const [p, q] = [...touches.current.values()]
      const pc = pinch.current
      const k = Math.max(1, Math.min(4, pc.k * Math.hypot(p.x - q.x, p.y - q.y) / pc.d))
      const r = screen.current.getBoundingClientRect()
      const ox = pc.cx - r.left, oy = pc.cy - r.top
      setZoom({ k: k, x: k === 1 ? 0 : ox - (ox - pc.x) * k / pc.k, y: k === 1 ? 0 : oy - (oy - pc.y) * k / pc.k })
      return
    }
    const d = drag.current
    if (!d) { return }
    if (zoom.k > 1) {
      // Zoomed: one finger moves the view, not the page.
      if (Math.hypot(e.clientX - d.sx, e.clientY - d.sy) > 8) { d.moved = true }
      setZoom(function (z) { return { ...z, x: d.zx + (e.clientX - d.sx), y: d.zy + (e.clientY - d.sy) } })
      return
    }
    if (!control) { return }
    const dy = d.y - e.clientY
    if (Math.abs(dy) > 12) {
      d.moved = true
      d.y = e.clientY
      send({ t: 'scroll', dy: dy * (size.current.h / (img.current ? img.current.getBoundingClientRect().height : 600)), x: d.at && d.at.x, y: d.at && d.at.y })
    }
  }
  function up(e) {
    touches.current.delete(e.pointerId)
    if (touches.current.size < 2) { pinch.current = null }
    const d = drag.current
    drag.current = null
    if (!control || !d || d.moved) { return }
    const p = at(e)
    if (p) { send({ t: 'tap', x: p.x, y: p.y }) }
  }

  function sendTyping(e) {
    e.preventDefault()
    if (typing) { send({ t: 'type', text: typing }) }
    setTyping('')
  }

  if (!active) { return null }

  if (!watching) {
    return (
      <div className="bv-empty">
        <p>{status ? 'The browser is closed. It opens when the agent uses it, and closes after ten idle minutes.' : 'Checking the browser...'}</p>
        {status ? <button type="button" className="chip" onClick={function () { setWant(true) }}>Open it</button> : null}
      </div>
    )
  }

  return (
    <div className="bv">
      <div className="bv-bar">
        <span className="bv-state">{control ? 'You have control' : 'Watching'}</span>
        {zoom.k > 1 ? <button type="button" className="chip" onClick={function () { setZoom({ k: 1, x: 0, y: 0 }) }}>Fit</button> : null}
        <button type="button" className="chip" aria-pressed={phone} onClick={togglePhone}>{phone ? 'Desktop layout' : 'Phone layout'}</button>
        <button type="button" className={'chip' + (control ? '' : ' bv-take')}
          onClick={function () { send({ t: 'control', on: !control }) }}>
          {control ? 'Hand back' : 'Take over'}
        </button>
      </div>
      {error ? <p className="bv-err" role="status">{error}</p> : null}
      {offer ? (
        <div className="bv-offer" role="group" aria-label="Save this login">
          <p>Save the login for <b>{offer.site}</b>{offer.user ? <> as {offer.user}</> : null} to the vault?</p>
          <button type="button" className="chip" onClick={function () { send({ t: 'save_login', yes: false }); setOffer(null) }}>Not now</button>
          <button type="button" className="chip bv-take" disabled={offer.busy}
            onClick={function () { setOffer(Object.assign({}, offer, { busy: true })); send({ t: 'save_login', yes: true }) }}>
            {offer.busy ? 'Saving... the first save sets up the vault and can take half a minute' : 'Save'}
          </button>
        </div>
      ) : null}
      {saved ? <p className="bv-saved" role="status">{saved}</p> : null}
      <div ref={screen} className={'bv-screen' + (control ? ' bv-live' : '')}
        onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}
        style={{ touchAction: control || zoom.k > 1 ? 'none' : 'pan-y' }}>
        {frame
          ? <img ref={img} src={frame} alt="The agent's browser" draggable="false"
              style={{ transform: `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.k})`, transformOrigin: '0 0' }} />
          : <p className="bv-wait">Opening the browser...</p>}
      </div>
      {control ? (
        <form className="bv-keys" onSubmit={sendTyping}>
          <input value={typing} onChange={function (e) { setTyping(e.target.value) }}
            placeholder="Type into the page" autoCapitalize="off" autoCorrect="off" spellCheck="false" />
          <button type="submit" className="chip">Type</button>
          {['enter', 'tab', 'backspace', 'escape'].map(function (k) {
            return <button key={k} type="button" className="chip" onClick={function () { send({ t: 'key', key: k }) }}>{k === 'backspace' ? '⌫' : k}</button>
          })}
          <button type="button" className="chip" onClick={function () { send({ t: 'back' }) }}>back</button>
        </form>
      ) : null}
    </div>
  )
}
