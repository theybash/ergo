import { useCallback, useEffect, useState } from 'react'
import { agentCalls } from './api'

/* An agent's calls with the owner, answered and missed, for its chat log.
 *
 * No timer. Read when the agent changes, when a turn settles (an answered call ends with the
 * transcript arriving in the chat, which is a turn), and when the page comes back into view, which
 * is when a call that rang while the phone was in a pocket can first be seen. A daemon without
 * the route answers 404, and then there are simply no call rows.
 */
export default function useAgentCalls(id, settledTick = 0) {
  const [calls, setCalls] = useState([])

  const load = useCallback(function () {
    if (!id) { return }
    agentCalls(id).then(
      function (got) { setCalls((got && got.calls) || []) },
      function () { /* keep what is on screen */ },
    )
  }, [id])

  useEffect(function () {
    /* oxlint-disable-next-line react/set-state-in-effect -- a new agent must not show the last
     * one's calls while its own are fetched. */
    setCalls([])
    load()
  }, [id, load])

  useEffect(function () {
    if (settledTick) { load() }
  }, [settledTick, load])

  useEffect(function () {
    function back() { if (document.visibilityState === 'visible') { load() } }
    document.addEventListener('visibilitychange', back)
    return function () { document.removeEventListener('visibilitychange', back) }
  }, [load])

  return calls
}
