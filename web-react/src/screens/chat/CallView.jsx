/* One call with the owner, whole: why the agent called, the voicemail if there is one, and every line
   said, full screen. A tap on a call row in the log opens it; back or the cross closes it. The same
   panel as a tool call's, because it is the same gesture: a short row that opens into the rest. */

import useBackClose from '../../shell/useBackClose'
import { callTitle } from './callTitle'

export default function CallView({ call, name, onClose }) {
  useBackClose(!!call, onClose)
  if (!call) { return null }
  const lines = (call.turns || []).filter(function (t) { return t.who === 'you' || t.who === 'voice' })
  return (
    <div className="toolview" role="dialog" aria-modal="true" aria-label="Call">
      <div className="toolview-bar">
        <span className="toolview-name">{callTitle(call)}</span>
        <button type="button" className="toolview-x" aria-label="Close" onClick={onClose}>×</button>
      </div>
      <div className="toolview-body">
        <p className="cv-why">{call.reason}</p>
        {call.audio ? <audio className="cv-audio" controls preload="none" src={call.audio} /> : null}
        {lines.length ? (
          <div className="cv-lines">
            {lines.map(function (t, i) {
              return (
                <p key={i} className={'cv-line' + (t.who === 'you' ? ' cv-you' : '')}>
                  <span className="cv-who">{t.who === 'you' ? 'You' : name}</span>
                  {t.text}
                </p>
              )
            })}
          </div>
        ) : (
          <p className="toolview-note">
            {call.outcome === 'answered' ? 'Nothing was written down on this call.' : call.opening}
          </p>
        )}
      </div>
    </div>
  )
}
