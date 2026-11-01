/* Email drafts, as cards.
   The same card in Insights, at the foot of the agent's chat and at the top of the feed, so a
   mail sent in one place is gone in the others.

   An agent only drafts. Your tap sends it, from your connected mail account:
     Send        "Are you sure you want to send?", then a minute he can Undo, then it goes
     the clock   a tap sends it at the time the agent suggested; a long press picks a time.
                 The same "Are you sure" first
   A scheduled mail shows its time and can be taken back until it goes. */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { mailDrafts, sendMail, scheduleMail, cancelMail, discardMail } from '../../data/api'
import { onMail } from '../../data/fleetStore'
import { faceColour, ago } from '../feed/Post'
import '../feed/feed.css'
import './mail.css'

const HOLD_MS = 500
// Taller than this, the body is folded until he opens it, so one email never fills the feed.
const TALL = 220

function stamp(at) {
  const d = new Date(at * 1000)
  const today = new Date()
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  if (d.toDateString() === today.toDateString()) { return 'today ' + hm }
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' }) + ', ' + hm
}

/* A datetime-local value for a time, in his local time. */
function local(at) {
  const d = new Date(at * 1000)
  const p = function (n) { return String(n).padStart(2, '0') }
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes())
}

const CLOCK = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="8.5"></circle><path d="M12 7.5V12l3 2"></path>
  </svg>
)

function useNow(on) {
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(function () {
    if (!on) { return undefined }
    const t = window.setInterval(function () { setNow(Date.now() / 1000) }, 1000)
    return function () { window.clearInterval(t) }
  }, [on])
  return now
}

const SEND = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21 3 10 14M21 3l-7 18-4-7-7-4z"></path>
  </svg>
)
const CLOSE = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M6 6l12 12M18 6L6 18"></path>
  </svg>
)

/* One email, drawn as a feed post: the agent's face and name, the mail quoted the way a post
   quotes a draft, and the post's quiet row of actions under it. framed is the chat's and
   Insights' version: no face, a frame around it, as Post draws itself in the chat. */
function MailCard({ d, onDone, framed }) {
  const [to, setTo] = useState(d.to.join(', '))
  const [cc, setCc] = useState((d.cc || []).join(', '))
  const [subject, setSubject] = useState(d.subject)
  const [whole, setWhole] = useState(false)
  const [tall, setTall] = useState(false)
  const [body, setBody] = useState(d.body)
  const [ask, setAsk] = useState(null)     // null, {kind: 'send'} or {kind: 'at', at}
  const [picking, setPicking] = useState(false)
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const hold = useRef(0)
  const held = useRef(false)
  const box = useRef(null)
  const subj = useRef(null)
  const queued = d.state === 'queued' || d.state === 'sending'
  const now = useNow(queued)
  const left = queued ? Math.max(0, Math.round((d.send_at || 0) - now)) : 0
  const edits = { to: to, cc: cc, subject: subject, body: body }
  const name = d.agent_name || d.agent
  const later = d.suggest_at && d.suggest_at > Date.now() / 1000 + 60

  /* The body grows with its words, like a post's text, so there is never a box to scroll. */
  useLayoutEffect(function () {
    const el = box.current
    if (!el) { return }
    el.style.height = 'auto'
    el.style.height = el.scrollHeight + 'px'
    setTall(el.scrollHeight > TALL)
  }, [body, queued])

  useLayoutEffect(function () {
    const el = subj.current
    if (!el) { return }
    el.style.height = 'auto'
    el.style.height = el.scrollHeight + 'px'
  }, [subject, queued])

  async function run(fn) {
    setBusy(true); setError('')
    try { await fn(); setAsk(null); setPicking(false); onDone() } catch (e) { setError(e.message || 'That did not work.') }
    setBusy(false)
  }

  function clockDown() {
    held.current = false
    hold.current = window.setTimeout(function () {
      held.current = true
      setAsk(null)
      setPick(local(later ? d.suggest_at : Date.now() / 1000 + 3600))
      setPicking(true)
    }, HOLD_MS)
  }
  function clockUp() {
    window.clearTimeout(hold.current)
    if (held.current) { return }
    if (later) {
      setPicking(false)
      setAsk({ kind: 'at', at: d.suggest_at })
    } else {
      setPick(local(Date.now() / 1000 + 3600))
      setPicking(true)
    }
  }

  let acts
  if (queued) {
    const soon = left <= 70
    acts = (
      <div className="fd-acts ml-acts">
        <span className="ml-state">
          {d.state === 'sending' ? 'Sending...' : soon ? 'Sending in ' + left + ' s' : 'Sends ' + stamp(d.send_at)}
        </span>
        {d.state === 'sending' ? null : (
          <button type="button" className="on" disabled={busy} onClick={function () { run(function () { return cancelMail(d.id) }) }}>
            {soon ? 'Undo' : 'Take back'}
          </button>
        )}
      </div>
    )
  } else if (ask) {
    acts = (
      <div className="ml-ask">
        <p>{ask.kind === 'send' ? 'Are you sure you want to send?' : 'Are you sure you want to send it ' + stamp(ask.at) + '?'}</p>
        <div className="fd-acts ml-acts">
          <button type="button" disabled={busy} onClick={function () { setAsk(null) }}>No</button>
          <button type="button" className="on" disabled={busy}
            onClick={function () {
              run(function () { return ask.kind === 'send' ? sendMail(d.id, edits) : scheduleMail(d.id, ask.at, edits) })
            }}>{SEND}<span>{ask.kind === 'send' ? 'Yes, send' : 'Yes, schedule'}</span></button>
        </div>
      </div>
    )
  } else if (picking) {
    acts = (
      <div className="ml-ask">
        <input type="datetime-local" className="ml-pick" value={pick} onChange={function (e) { setPick(e.target.value) }} aria-label="When to send" />
        <div className="fd-acts ml-acts">
          <button type="button" onClick={function () { setPicking(false) }}>No</button>
          <button type="button" className="on" disabled={!pick}
            onClick={function () {
              const at = Math.floor(new Date(pick).getTime() / 1000)
              if (!(at > Date.now() / 1000 + 60)) { setError('Pick a time at least a minute from now.'); return }
              setError(''); setPicking(false); setAsk({ kind: 'at', at: at })
            }}>{CLOCK}<span>Schedule</span></button>
        </div>
      </div>
    )
  } else {
    acts = (
      <div className="fd-acts ml-acts">
        <button type="button" className="ml-clock" disabled={busy || !body.trim()}
          onPointerDown={clockDown} onPointerUp={clockUp}
          onPointerLeave={function () { window.clearTimeout(hold.current) }}
          onContextMenu={function (e) { e.preventDefault() }}
          aria-label={later ? 'Send ' + stamp(d.suggest_at) + '. Long press to pick a time' : 'Pick a time to send'}>
          {CLOCK}<span>{later ? stamp(d.suggest_at) : 'Later'}</span>
        </button>
        <button type="button" disabled={busy || !body.trim() || !to.trim()}
          onClick={function () { setPicking(false); setAsk({ kind: 'send' }) }}>{SEND}<span>Send</span></button>
      </div>
    )
  }

  return (
    <article className={'fd-post ml-post' + (framed ? ' in-chat' : '')}>
      {framed ? null : (
        <span className="fd-face" style={{ background: faceColour(d.agent) }} aria-hidden="true">
          {name.slice(0, 1).toUpperCase()}
        </span>
      )}
      <div className="fd-body">
        <p className="fd-who">
          <b>{name}</b>
          <span>· email</span>
          {d.at ? <span>· {ago(d.at)}</span> : null}
          {queued ? null : (
            <button type="button" className="fd-x" disabled={busy} aria-label="Discard this email"
              onClick={function () { run(function () { return discardMail(d.id) }) }}>{CLOSE}</button>
          )}
        </p>
        <div className="fd-quote">
          <div className="fd-draft alone ml-mail">
            <label className="ml-row"><span>To</span>
              {queued ? <b>{d.to.join(', ')}</b> : <input value={to} onChange={function (e) { setTo(e.target.value) }} />}
            </label>
            <label className="ml-row"><span>Cc</span>
              {queued ? <b>{(d.cc || []).join(', ')}</b> : <input value={cc} onChange={function (e) { setCc(e.target.value) }} />}
            </label>
            <label className="ml-row"><span>Subject</span>
              {queued ? <b className="ml-subject">{d.subject}</b> : (
                <textarea ref={subj} className="ml-subject" rows={1} value={subject}
                  onChange={function (e) { setSubject(e.target.value.replace(/\n/g, ' ')) }} />
              )}
            </label>
            <div className={'ml-text' + (tall && !whole ? ' folded' : '')}>
              {queued ? <p className="fd-words">{d.body}</p> : (
                <textarea ref={box} className="fd-words ml-body" value={body} aria-label="The email" rows={1}
                  onFocus={function () { setWhole(true) }}
                  onChange={function (e) { setBody(e.target.value) }} />
              )}
            </div>
            {tall && !whole && !queued ? (
              <button type="button" className="ml-more" onClick={function () { setWhole(true) }}>Show all</button>
            ) : null}
            {d.attach && d.attach.length ? <p className="fd-meta ml-attach">Attached: {d.attach.join(', ')}</p> : null}
          </div>
        </div>
        {d.error ? <p className="ml-err">{d.error}</p> : null}
        {acts}
        {error ? <p className="ml-err">{error}</p> : null}
      </div>
    </article>
  )
}

/* agent: only that agent's drafts. keep: which drafts this view shows. label: a line above, or
   none. framed: the chat's and Insights' frame instead of the feed's face. */
export default function MailDrafts({ agent, keep, label, framed }) {
  const [drafts, setDrafts] = useState([])

  function load() {
    mailDrafts().then(function (r) { setDrafts(r.drafts || []) }, function () {})
  }

  useEffect(function () {
    load()
    return onMail(function () { load() })
  }, [])

  const shown = drafts.filter(function (d) { return (!agent || d.agent === agent) && (!keep || keep(d)) })
  if (!shown.length) { return null }
  return (
    <div className="ml-drafts" role="group" aria-label="Email drafts">
      {label ? <p className="ml-label">{label}</p> : null}
      {shown.map(function (d) {
        return <MailCard key={d.id + ':' + d.state} d={d} onDone={load} framed={framed} />
      })}
    </div>
  )
}
