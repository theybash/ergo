/* How many asks are waiting on you, for anybody outside the Fleet screen.
 *
 * The shell puts this number on the Fleet tab as a badge. It cannot read it off the Fleet
 * screen, because the asks arrive on one WebSocket per awake agent and those sockets are opened
 * by Fleet. So Fleet writes the number here and the shell reads it.
 *
 * The shell uses two functions and nothing else:
 *
 *   subscribe(fn)   Call fn whenever the number changes. Returns a function that stops it.
 *   getPending()    The number of asks waiting right now. A plain integer, cached, so
 *                   useSyncExternalStore sees the same value until it really changed.
 *
 * In the shell:
 *
 *   const pending = useSyncExternalStore(subscribe, getPending, getPending)
 *
 * The third function, setPending, belongs to Fleet. Nothing else calls it.
 *
 * When Fleet unmounts it leaves the last number here rather than zeroing it. The sockets close
 * with the screen, so Fleet stops knowing, and a badge that drops to zero the moment you open
 * the chat would say the asks were answered when they were not.
 */

let pending = 0
const listeners = new Set()

export function subscribe(fn) {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

export function getPending() {
  return pending
}

/* Fleet only. A number in, every listener told, but only when it changed. */
export function setPending(n) {
  const next = Number(n) || 0
  if (next === pending) return
  pending = next
  for (const fn of listeners) fn()
}

export default { subscribe, getPending, setPending }
