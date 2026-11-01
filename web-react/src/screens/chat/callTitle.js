/* How a call reads in one line: "Missed call · 14:32" or "Call · 3 min · 14:32". */

function clock(at) {
  const d = new Date(Number(at) * 1000)
  if (isNaN(d.getTime())) { return '' }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function length(seconds) {
  const s = Number(seconds) || 0
  if (s < 60) { return 'under a minute' }
  return Math.max(1, Math.round(s / 60)) + ' min'
}

export function callTitle(call) {
  const when = clock(call.answered_at || call.at)
  if (call.outcome === 'answered' || call.outcome === 'outgoing') { return ['Call', length(call.seconds), when].join(' · ') }
  if (call.outcome === 'ringing') { return ['Calling', when].join(' · ') }
  return ['Missed call', when].join(' · ')
}
