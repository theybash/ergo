/* One status dot. The stylesheet carries three looks: up, sleep and down.
   A working agent is a live agent, so working reuses up.
   "waiting" is deprecated. Nothing renders it any more, and the stylesheet no longer paints it,
   so it falls back to sleep. It stays in the map only so an old caller cannot throw. */

const LOOK = {
  up: 'up',
  sleep: 'sleep',
  waiting: 'sleep',
  working: 'up',
  down: 'down',
}

export default function Dot({ state = 'sleep' }) {
  return <span className={'dot ' + (LOOK[state] || 'sleep')} aria-hidden="true"></span>
}
