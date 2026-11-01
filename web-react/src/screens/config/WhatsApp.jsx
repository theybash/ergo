/* WhatsApp. The bridge is a linked device on your WhatsApp number. First whether it is linked, or
   the QR to scan while it pairs. Then the switch that allows sending, off unless you turn it on.
   Then the replies the agents drafted, each with Send and Discard: agents never send, you do.
   Then every chat it has seen, each with a picker: Not followed, or the agent that reads it. A
   chat that is not followed is never stored. Nothing here shows a received message.

   No timer. The bridge tells the daemon when the QR or the chats change, the daemon says when a
   draft comes or goes, and both arrive on the page's one event stream. */

import { useEffect, useState } from 'react'
import {
  fileUrl,
  whatsappStatus, whatsappChats, routeWhatsapp, saveWhatsappSending, whatsappDrafts,
  sendWhatsappDraft, discardWhatsappDraft,
} from '../../data/api'
import { onWhatsapp } from '../../data/fleetStore'

/* WhatsApp hides some numbers behind dots, the owner's own among them, which made those chats
   impossible to find. The number is in the chat's id, so it is shown and searched from there. */
function chatName(c) {
  const name = c.name || ''
  const digits = (c.id || '').split('@')[0].split('-')[0]
  if (!name || name.includes('\u2219')) { return /^\d{8,}$/.test(digits) ? '+' + digits : (name || digits) }
  return name
}

function since(iso) {
  if (!iso) { return '' }
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) + ', ' +
    d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function Qr({ text }) {
  const [src, setSrc] = useState('')
  useEffect(function () {
    let live = true
    // The QR library is its own file, fetched only when a pairing code is shown, because the
    // chat pulls this file in for its drafts and would otherwise carry the library too.
    import('qrcode')
      .then(function (m) {
        return m.default.toDataURL(text, { margin: 2, width: 232, errorCorrectionLevel: 'L' })
      })
      .then(function (url) { if (live) { setSrc(url) } }, function () {})
    return function () { live = false }
  }, [text])
  return src ? <img className="cf-wa-qr" src={src} width="232" height="232" alt="WhatsApp pairing code" /> : null
}

/* The photo or file an agent put with its draft: the picture itself, or the file's name, which opens
   it. Both come from the agent's outbox, the same place its other files are. */
export function DraftFile({ d }) {
  if (!d.file) { return null }
  const url = fileUrl(d.agent, d.file)
  return d.photo
    ? <a className="wa-file wa-photo" href={url} target="_blank" rel="noreferrer"><img src={url} alt={d.file} /></a>
    : <a className="wa-file" href={url} target="_blank" rel="noreferrer">{d.file}</a>
}

export function Draft({ d, sending, onDone }) {
  const [text, setText] = useState(d.text)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

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
    <li className="cf-wa-draft">
      <span className="cf-wa-to">{d.chat_name} <span>{d.agent_name}</span></span>
      <DraftFile d={d} />
      <textarea className="cf-wa-text" value={text} rows={2} placeholder={d.file ? 'Caption, if any' : undefined}
        onChange={function (e) { setText(e.target.value) }} aria-label={'Reply to ' + d.chat_name} />
      <span className="cf-login-acts">
        <button type="button" className="chip" disabled={busy} onClick={function () { act(false) }}>Discard</button>
        <button type="button" className="chip" disabled={busy || !sending || !(text.trim() || d.file)}
          onClick={function () { act(true) }}>{busy ? 'Sending...' : 'Send'}</button>
      </span>
      {error ? <p className="fl-err">{error}</p> : null}
    </li>
  )
}

export default function WhatsApp() {
  const [status, setStatus] = useState(null)
  const [data, setData] = useState(null)
  const [drafts, setDrafts] = useState([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [find, setFind] = useState('')

  function loadChats() {
    whatsappChats().then(setData, function () { setError('Could not read the chats.') })
  }
  function loadDrafts() {
    whatsappDrafts().then(function (r) {
      setDrafts(r.drafts || [])
      setSending(!!r.sending)
    }, function () {})
  }

  useEffect(function () {
    let live = true
    whatsappStatus().then(function (s) { if (live) { setStatus(s); setSending(!!s.sending) } },
      function () { if (live) { setError('Could not reach the daemon.') } })
    loadChats()
    loadDrafts()
    const off = onWhatsapp(function (d) {
      if (!live) { return }
      if (d.status) { setStatus(d.status) }
      if (d.what === 'chats') { loadChats() }
      if (d.what === 'drafts') { loadDrafts() }
    })
    return function () { live = false; off() }
  }, [])

  async function pick(chat, agent) {
    setError('')
    try {
      setData(await routeWhatsapp(chat.id, agent || null))
    } catch (e) {
      setError(e.message || 'That did not save.')
    }
  }

  async function allow(next) {
    const was = sending
    setSending(next); setError('')
    try {
      const r = await saveWhatsappSending(next)
      setSending(!!r.sending)
    } catch {
      setSending(was)
      setError('That did not save.')
    }
  }

  const agents = (data && data.agents) || []
  const chats = (data && data.chats) || []

  let line = 'Not linked'
  if (status && status.linked) { line = 'Linked since ' + since(status.since) }

  return (
    <div className="cf-wa">
      <p className="cf-meter-line">
        <span className="cf-meter-label">{status ? line : '...'}</span>
        {status && !status.linked && status.error ? <span className="cf-meter-rest">{status.error}</span> : null}
      </p>
      {status && !status.linked && status.qr ? (
        <div className="cf-wa-pair">
          <Qr text={status.qr} />
          <p className="cf-note">On your phone: WhatsApp, Settings, Linked devices, Link a device.</p>
        </div>
      ) : null}

      <div>
        <label className="cf-check">
          <input type="checkbox" checked={sending} disabled={!status}
            onChange={function (e) { allow(e.target.checked) }} />
          Allow sending from Ergo
        </label>
        <p className="cf-note">WhatsApp can ban numbers that send like a bot. Ergo only sends what you tap Send on.</p>
      </div>

      {drafts.length ? (
        <p className="cf-note">{drafts.length === 1 ? 'One draft waits' : drafts.length + ' drafts wait'} on Insights.</p>
      ) : null}

      {chats.length ? (
        <input className="cf-input wa-find" type="search" value={find} placeholder={'Find a chat among ' + chats.length}
          aria-label="Find a chat" onChange={function (e) { setFind(e.target.value) }} />
      ) : null}
      {chats.length ? (
        /* A long contact list scrolls inside its own box, followed chats first, so the page stays
           short and the four that matter are on top. */
        <ul className="cf-rows agentcard wa-list">
          {chats.filter(function (c) {
            const q = find.trim().toLowerCase()
            return !q || (chatName(c) + ' ' + c.id).toLowerCase().includes(q.replace(/[\s+]/g, ''))
              || chatName(c).toLowerCase().includes(q)
          }).sort(function (a, b) { return (b.agent ? 1 : 0) - (a.agent ? 1 : 0) }).map(function (c) {
            return (
              <li className="cf-row" key={c.id}>
                <span className="cf-name">{chatName(c)}</span>
                <span className="cf-controls">
                  <select className="cf-select" aria-label={'Who reads ' + (c.name || 'this chat')}
                    value={c.agent || ''} onChange={function (e) { pick(c, e.target.value) }}>
                    <option value="">Not followed</option>
                    <optgroup label="One agent">
                      {agents.map(function (a) { return <option key={a.id} value={a.id}>{a.name}</option> })}
                    </optgroup>
                  </select>
                </span>
              </li>
            )
          })}
        </ul>
      ) : status && status.linked ? <p className="cf-note">No chats seen yet. They appear as messages arrive.</p> : null}
      {error ? <p className="fl-err">{error}</p> : null}
    </div>
  )
}
