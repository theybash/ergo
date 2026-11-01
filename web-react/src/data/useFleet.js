import { useCallback, useSyncExternalStore } from 'react'
import { subscribe, snapshot, status, refresh as refreshStore } from './fleetStore'
import { byLastSaid } from './when'

/* The whole fleet, kept current.
 *
 * Every screen reads the one store in fleetStore.js, fed by one event stream that carries the
 * change itself. So a card going green costs the bytes of that one field, and nothing is fetched
 * while the stream is up. useFleet({ health: true }) also asks for the machine snapshot, which
 * only a screen that draws it should.
 */

/* Which views an agent is tagged for. The daemon stores the tags per agent and nothing else:
 * the view you are looking at right now is browser state, not daemon state, because the operator
 * asked for no server state there. So this reports the tags in use, not the current view. */
function viewsInUse(agents) {
  const seen = new Set()
  for (const a of agents) for (const v of a.views || []) seen.add(v)
  return [...seen].sort()
}

export function useFleet({ health = false } = {}) {
  const sub = useCallback((fn) => subscribe(fn, health), [health])
  const snap = useSyncExternalStore(sub, snapshot)
  const { loading, error } = status()
  const refresh = useCallback(() => refreshStore().catch(() => {}), [])

  return {
    /* The master first, then whoever spoke last, in every list that draws agents. */
    agents: byLastSaid(snap.agents || []),
    jobs: snap.jobs || [],
    cords: snap.cords || [],
    health: snap.health || null,
    /* What health is judged against: mem_avail_gb, battery_pct, disk_free_pct, pressure. The
     * readout needs them to know which number to paint red. */
    limits: snap.limits || {},
    hidden: snap.hidden || [],
    /* What the front desk has not told yet: {untold, urgent, at}, or null from an older daemon. */
    inbox: snap.inbox || null,
    views: viewsInUse(snap.agents || []),
    loading,
    error,
    refresh,
  }
}

export default useFleet
