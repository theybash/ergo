/* One call with the owner, drawn in the log where it happened, the way a phone app draws one:
   "Missed call · 14:32" or "Call · 3 min · 14:32". A missed call keeps the agent's opening line as
   a voicemail, playable where it sits. A tap on the line opens the whole call. */

import { callTitle } from './callTitle'

function Phone() {
  return (
    <svg className="ch-call-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M6.6 3.5h2.6l1.4 4-1.9 1.3a10.5 10.5 0 0 0 6.5 6.5l1.3-1.9 4 1.4v2.6a2 2 0 0 1-2.2 2A16.5 16.5 0 0 1 4.6 5.7a2 2 0 0 1 2-2.2z" />
    </svg>
  )
}

export default function CallRow({ call, onOpen }) {
  const missed = call.outcome === 'missed'
  return (
    <div className={'ch-call' + (missed ? ' ch-call-missed' : '')} data-call={call.id}>
      <button type="button" className="ch-call-open" onClick={function () { onOpen(call) }}>
        <Phone />
        <span>{callTitle(call)}</span>
      </button>
      {missed && call.audio
        ? <audio className="ch-call-audio" controls preload="none" src={call.audio} aria-label="Voicemail" />
        : null}
    </div>
  )
}
