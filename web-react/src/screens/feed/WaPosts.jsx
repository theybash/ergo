/* WhatsApp drafts in the feed (from user feedback: "build the whatsapp draft in feed").
   Drawn as feed posts, like the email cards: the agent's face and name, the reply quoted under
   the chat it is for, and Send. The same routes as the chat's and Config's cards, so a draft
   sent here is gone there too. Only his tap sends, and only while sending is on in Config. */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { whatsappDrafts, sendWhatsappDraft, discardWhatsappDraft } from '../../data/api'
import { onWhatsapp } from '../../data/fleetStore'
import { DraftFile } from '../config/WhatsApp'
import { faceColour, ago } from './Post'
import './feed.css'
import '../mail/mail.css'
import '../config/config.css'

// Taller than this, the reply is folded until he opens it, as an email's body is.
const TALL = 220

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

function WaCard({ d, sending, onDone }) {
  const [text, setText] = useState(d.text)
  const [whole, setWhole] = useState(false)
  const [tall, setTall] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const box = useRef(null)
  const name = d.agent_name || d.agent

  /* The reply grows with its words, so there is never a box to scroll. */
  useLayoutEffect(function () {
    const el = box.current
    if (!el) { return }
    el.style.height = 'auto'
    el.style.height = el.scrollHeight + 'px'
    setTall(el.scrollHeight > TALL)
  }, [text])

  async function act(send) {
    setBusy(true); setError('')
    try {
      if (send) { await sendWhatsappDraft(d.id, text) } else { await discardWhatsappDraft(d.id) }
      onDone()
    } catch (e) {
      setError(e.message || 'That did not work.')
    }
    setBusy(false)
  }

  return (
    <article className="fd-post ml-post">
      <span className="fd-face" style={{ background: faceColour(d.agent) }} aria-hidden="true">
        {name.slice(0, 1).toUpperCase()}
      </span>
      <div className="fd-body">
        <p className="fd-who">
          <b>{name}</b>
          <span>· WhatsApp</span>
          {d.at ? <span>· {ago(d.at)}</span> : null}
          <button type="button" className="fd-x" disabled={busy} aria-label="Discard this reply"
            onClick={function () { act(false) }}>{CLOSE}</button>
        </p>
        <div className="fd-quote">
          <div className="fd-draft alone ml-mail">
            <div className="ml-row"><span>To</span><b>{d.chat_name}</b></div>
            <DraftFile d={d} />
            <div className={'ml-text' + (tall && !whole ? ' folded' : '')}>
              <textarea ref={box} className="fd-words ml-body" value={text} rows={1}
                placeholder={d.file ? 'Caption, if any' : undefined}
                aria-label={'Reply to ' + d.chat_name}
                onFocus={function () { setWhole(true) }}
                onChange={function (e) { setText(e.target.value) }} />
            </div>
            {tall && !whole ? (
              <button type="button" className="ml-more" onClick={function () { setWhole(true) }}>Show all</button>
            ) : null}
          </div>
        </div>
        {!sending ? <p className="ml-err">Sending is off. Turn on "Allow sending from Ergo" in Config, WhatsApp.</p> : null}
        <div className="fd-acts ml-acts">
          <button type="button" disabled={busy || !sending || !(text.trim() || d.file)}
            onClick={function () { act(true) }}>{SEND}<span>{busy ? 'Sending...' : 'Send'}</span></button>
        </div>
        {error ? <p className="ml-err">{error}</p> : null}
      </div>
    </article>
  )
}

/* keep: which drafts this view shows. */
export default function WaPosts({ keep }) {
  const [drafts, setDrafts] = useState([])
  const [sending, setSending] = useState(false)

  function load() {
    whatsappDrafts().then(function (r) {
      setDrafts(r.drafts || [])
      setSending(!!r.sending)
    }, function () {})
  }

  useEffect(function () {
    load()
    return onWhatsapp(function (d) { if (d.what === 'drafts' || d.status) { load() } })
  }, [])

  const shown = drafts.filter(function (d) { return !keep || keep(d) })
  if (!shown.length) { return null }
  return (
    <div className="ml-drafts" role="group" aria-label="WhatsApp drafts">
      {shown.map(function (d) {
        return <WaCard key={d.id} d={d} sending={sending} onDone={load} />
      })}
    </div>
  )
}
