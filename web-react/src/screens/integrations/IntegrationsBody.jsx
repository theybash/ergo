/* The Integrations cards and the two notes under them, without the screen head, so the Config
   screen can hold the same thing under its own heading. ?screen=integrations still shows it too.

   Each card's state is what the daemon measured (GET /api/integrations), or "not measured". A
   daemon without the route, or one that does not answer, leaves every card not measured, so a
   failure never shows a service as connected. */

import { useEffect, useState } from 'react'
import { integrations } from '../../data/api'
import INTEGRATIONS, { NOTE, MEASURED_NOTE } from './data'
import IntegrationCard from './IntegrationCard'
import CodexCard from './CodexCard'

const UNMEASURED = { state: 'not measured', badge: 'quiet', scope: null }

export default function IntegrationsBody() {
  const [measured, setMeasured] = useState({})

  useEffect(function () {
    let live = true
    integrations().then(function (got) { if (live) setMeasured(got || {}) }).catch(function () {})
    return function () { live = false }
  }, [])

  // Served from the daemon it talks to, so this is the address Tailscale is actually carrying.
  const tailscale = { state: 'connected', badge: 'ok', scope: window.location.host }

  return (
    <>
      <div className="int-grid">
        {INTEGRATIONS.map(function (item) {
          if (item.id === 'codex') { return <CodexCard key={item.id} item={item} /> }
          const live = item.id === 'tailscale' ? tailscale : (measured[item.id] || UNMEASURED)
          return <IntegrationCard key={item.id} item={item} live={live} />
        })}
      </div>

      <p className="note int-note">{MEASURED_NOTE}</p>
      <p className="note int-note">{NOTE}</p>
    </>
  )
}
