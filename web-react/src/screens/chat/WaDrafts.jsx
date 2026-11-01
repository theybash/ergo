/* WhatsApp replies this agent drafted, as cards at the foot of its chat: the chat it is for, the
   text, which he can edit, and Send or Discard. The same card and the same routes as Config and
   Insights, so a draft sent here is gone there too. Only the owner's tap sends, and only while
   sending is on in Config; the agent never sends.

   While a card is on screen it tells the log, through onSeen, so the log can hide its arrows: he
   is at the foot already, and the arrow sat on Send. */

import { useEffect, useRef, useState } from 'react'
import { whatsappDrafts } from '../../data/api'
import { onWhatsapp } from '../../data/fleetStore'
import { Draft } from '../config/WhatsApp'
import '../config/config.css'

export default function WaDrafts({ agent, onSeen }) {
  const [drafts, setDrafts] = useState([])
  const [sending, setSending] = useState(false)
  const box = useRef(null)

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

  const mine = drafts.filter(function (d) { return d.agent === agent })
  const shown = mine.length > 0

  useEffect(function () {
    const el = box.current
    if (!shown || !el || !onSeen || typeof IntersectionObserver === 'undefined') { return undefined }
    const watch = new IntersectionObserver(function (seen) { onSeen(seen[0].isIntersecting) })
    watch.observe(el)
    return function () { watch.disconnect(); onSeen(false) }
  }, [shown, onSeen])

  if (!shown) { return null }
  return (
    <div className="ch-wa" ref={box} role="group" aria-label="WhatsApp drafts">
      <p className="ch-wa-label">WhatsApp {mine.length === 1 ? 'draft' : 'drafts'}, waiting on you</p>
      {!sending ? <p className="ch-wa-off">Sending is off. Turn on "Allow sending from Ergo" in Config, WhatsApp.</p> : null}
      <ul className="cf-wa-drafts">
        {mine.map(function (d) {
          return <Draft key={d.id} d={d} sending={sending} onDone={load} />
        })}
      </ul>
    </div>
  )
}
