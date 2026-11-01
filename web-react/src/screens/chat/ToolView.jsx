/* One tool call whole: the command it ran and everything it answered, full screen and
   scrollable. A tap on a command or a result row in the log opens it; back or the cross closes it.
   The rows carry only the first few hundred characters, so the whole call is fetched on open. */

import { useEffect, useState } from 'react'
import useBackClose from '../../shell/useBackClose'

const BASE = (import.meta.env && import.meta.env.VITE_CC_API) || ''

export default function ToolView({ agent, id, onClose }) {
  const [call, setCall] = useState(null)
  const [error, setError] = useState('')
  useBackClose(!!id, onClose)

  useEffect(function () {
    if (!id) { return undefined }
    let live = true
    setCall(null)
    setError('')
    fetch(`${BASE}/api/agents/${agent}/tool?id=${encodeURIComponent(id)}`)
      .then(function (r) {
        if (r.ok) { return r.json() }
        // The daemon says "no such tool call" when the record has lost it; anything else means the
        // daemon does not know this request yet, which is an old daemon, not a lost call.
        return r.json().catch(function () { return {} }).then(function (body) {
          throw new Error(/no such tool call/.test(body.error || '')
            ? 'That call is no longer in the record.'
            : 'The laptop needs a restart of Ergo before calls open here.')
        })
      })
      .then(function (got) { if (live) { setCall(got) } }, function (e) { if (live) { setError(e.message) } })
    return function () { live = false }
  }, [agent, id])

  if (!id) { return null }
  return (
    <div className="toolview" role="dialog" aria-modal="true" aria-label="Tool call">
      <div className="toolview-bar">
        <span className="toolview-name">{call ? call.tool : ''}</span>
        <button type="button" className="toolview-x" aria-label="Close" onClick={onClose}>×</button>
      </div>
      <div className="toolview-body">
        {error ? <p className="toolview-note">{error}</p> : null}
        {!call && !error ? <p className="toolview-note">Loading...</p> : null}
        {call ? (
          <>
            <p className="toolview-head">Command</p>
            <pre className="toolview-pre">{call.input || ''}</pre>
            <p className={'toolview-head' + (call.error ? ' toolview-failed' : '')}>
              {call.result === undefined ? 'No result yet' : (call.error ? 'Failed' : 'Result')}
            </p>
            {call.result !== undefined ? <pre className={'toolview-pre' + (call.error ? ' toolview-failed' : '')}>{call.result || '(empty)'}</pre> : null}
            {call.cut ? <p className="toolview-note">The result is longer than this; the rest is cut.</p> : null}
          </>
        ) : null}
      </div>
    </div>
  )
}
