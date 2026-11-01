/* Hidden agents: put away with Hide, never deleted. Show brings one back to Fleet. Moved here from
   the foot of Fleet (from user feedback: "Move hidden agents to config"). */

import { useState } from 'react'
import { show } from '../../data/api'

export default function HiddenAgents({ fleet }) {
  const { hidden, refresh } = fleet
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  if (!hidden.length) { return <p className="cf-note">No hidden agents.</p> }

  async function bring(id) {
    setBusy(id); setError('')
    try { await show(id); refresh() } catch (err) { setError(err.message || 'That did not work.') }
    setBusy('')
  }

  return (
    <>
      <ul className="cf-rows agentcard">
        {hidden.map(function (id) {
          return (
            <li className="cf-row cf-hidden-row" key={id}>
              <span className="cf-hidden-id">{id}</span>
              <button type="button" className="btn sm" disabled={busy === id}
                onClick={function () { bring(id) }} aria-label={'Show ' + id + ' in Fleet again'}>
                Show
              </button>
            </li>
          )
        })}
      </ul>
      {error ? <p className="cf-note" role="alert">{error}</p> : null}
    </>
  )
}
