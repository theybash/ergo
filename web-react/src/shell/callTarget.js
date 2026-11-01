/* Who the next call is with, and whether a call is open.

   The call button in an agent's chat opens the same voice overlay as the orb, through the same
   openVoice the shell already hands the chat. So the button says which agent first, here, and the
   overlay takes it as it mounts. A target that nobody takes within a few seconds is dropped, so
   a later tap on the orb can never turn into a call with an agent he reached for a while ago.

   The overlay also says here when it is up and when it has gone, so the button can be off while
   a call is open. */

import { useSyncExternalStore } from 'react'

const FRESH_MS = 5000

let target = null
let open = false
const fns = new Set()

/* The next call is with this agent. */
export function aimCall(agent) {
  target = agent ? { agent, at: Date.now() } : null
}

/* The agent the next call is with, once. Null for the front desk. */
export function takeCallTarget() {
  const got = target
  target = null
  return got && Date.now() - got.at < FRESH_MS ? got.agent : null
}

export function setCallOpen(next) {
  if (open === !!next) { return }
  open = !!next
  fns.forEach(function (fn) { fn() })
}

function subscribe(fn) {
  fns.add(fn)
  return function () { fns.delete(fn) }
}

function snapshot() { return open }

/* True while a call is open, the front desk's or an agent's, minimised or not. */
export function useCallOpen() {
  return useSyncExternalStore(subscribe, snapshot)
}
