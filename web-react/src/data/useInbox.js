import { useSyncExternalStore } from 'react'
import { subscribe, snapshot, lastInboxChange } from './fleetStore'

/* What the front desk has not told yet, for the orb, and the last thing that changed, for the call.
 *
 * Read from the one fleet stream. An inbox event carries its own summary, {untold, urgent, at},
 * so nothing is fetched when an item is told or added. The change itself, {event, id, by}, goes
 * to the call, which closes a row a newer item replaced.
 */
export default function useInbox() {
  const snap = useSyncExternalStore(subscribe, snapshot)
  return { summary: snap.inbox || null, change: lastInboxChange() }
}
