/* WhatsApp replies an agent drafted because the owner asked it to, on Insights beside the tasks, where
   he looks for what waits on him. Each one can be edited, sent with a tap, or discarded. Sending
   also needs the switch on Config; while it is off, the note says so and Send stays grey. */

import { useEffect, useState } from 'react'
import { whatsappDrafts } from '../../data/api'
import { onWhatsapp } from '../../data/fleetStore'
import { Draft } from '../config/WhatsApp'
import '../config/config.css'

export default function Drafts() {
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

  if (!drafts.length) { return null }
  return (
    <section className="section" aria-labelledby="wa-drafts-h">
      <div className="section-head">
        <h3 id="wa-drafts-h">WhatsApp drafts</h3>
      </div>
      {!sending ? <p className="note">Sending is off. Turn on "Allow sending from Ergo" in Config, WhatsApp, to send these.</p> : null}
      <ul className="cf-wa-drafts">
        {drafts.map(function (d) {
          return <Draft key={d.id} d={d} sending={sending} onDone={load} />
        })}
      </ul>
    </section>
  )
}
