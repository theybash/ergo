/* When an agent last spoke, said the short way, and whether he has read it.

     today          23:14
     this week      Mon
     older          26 Sep

   The daemon sends last_message_at as ISO with a Z, and read_at as seconds. An agent he has
   never opened has no read_at and counts as read, so nothing lights up on the first day. */

export function lastSaid(at) {
  if (!at) { return '' }
  const d = new Date(at)
  if (isNaN(d.getTime())) { return '' }
  const now = new Date()
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  if (now - d < 6 * 86400000) { return d.toLocaleDateString([], { weekday: 'short' }) }
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' })
}

export function unread(agent) {
  if (!agent || !agent.last_message_at || agent.read_at == null) { return false }
  const said = new Date(agent.last_message_at).getTime()
  return !isNaN(said) && said / 1000 > Number(agent.read_at) + 1
}

/* The master first, then whoever spoke last. An agent that never spoke goes to the end. */
export function byLastSaid(agents) {
  return agents.slice().sort(function (a, b) {
    const am = a.role === 'master' ? 1 : 0
    const bm = b.role === 'master' ? 1 : 0
    if (am !== bm) { return bm - am }
    const at = a.last_message_at ? new Date(a.last_message_at).getTime() : 0
    const bt = b.last_message_at ? new Date(b.last_message_at).getTime() : 0
    return (bt || 0) - (at || 0)
  })
}
