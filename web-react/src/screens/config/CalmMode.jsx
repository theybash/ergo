/* Calm mode, for anxious days. One switch. While it is on, agents hold offers, nudges and briefs
   that are not urgent, the front desk makes no reminder calls and does not raise the task list, and
   nothing says overdue. It stays on until he turns it off. It saves the moment it is changed. */

import { useEffect, useState } from 'react'
import { api } from '../../data/api'

export default function CalmMode() {
  const [on, setOn] = useState(null)
  const [error, setError] = useState('')

  useEffect(function () {
    let live = true
    api('/api/calm').then(
      function (r) { if (live && r) { setOn(Boolean(r.on)) } },
      function () { if (live) { setError('Could not read the setting.') } },
    )
    return function () { live = false }
  }, [])

  async function flip(next) {
    const was = on
    setOn(next)
    setError('')
    try {
      const r = await api('/api/calm', 'POST', { on: next })
      if (!r || r.on !== next) { throw new Error() }
    } catch {
      setOn(was)
      setError('That did not save.')
    }
  }

  return (
    <div className="cf-meter">
      <div className="cf-meter-line cf-vm-line">
        <span className="cf-meter-label" id="cf-calm-h">Calm mode</span>
        <label className="cf-check">
          <input type="checkbox" checked={Boolean(on)} disabled={on === null}
            aria-labelledby="cf-calm-h" onChange={function (e) { flip(e.target.checked) }} />
          On
        </label>
      </div>
      <p className="cf-note">Only urgent things reach you. No offers, no reminder calls, and only
        what is due today. It stays on until you turn it off.</p>
      {error ? <p className="fl-err">{error}</p> : null}
    </div>
  )
}
