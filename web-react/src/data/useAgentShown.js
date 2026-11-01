import { useCallback, useEffect, useState } from 'react'
import { agentShown } from './api'
import { onTasks } from './fleetStore'

/* The job results shown in an agent's chat for the owner, for its chat log.
 *
 * No timer. Read when the agent changes, when one of its jobs finishes (jobTick, from the jobs
 * feed the fleet store already holds), when a nag run on one of its tasks writes its log line
 * (nags.py writes the card first), and when the page comes back into view. A daemon without
 * the route answers 404, and then there are simply no job cards.
 */
export default function useAgentShown(id, jobTick = 0) {
  const [shown, setShown] = useState([])

  const load = useCallback(function () {
    if (!id) { return }
    agentShown(id).then(
      function (got) { setShown((got && got.shown) || []) },
      function () { /* keep what is on screen */ },
    )
  }, [id])

  useEffect(function () {
    /* oxlint-disable-next-line react/set-state-in-effect -- a new agent must not show the last
     * one's cards while its own are fetched. */
    setShown([])
    load()
  }, [id, load])

  useEffect(function () {
    if (jobTick) { load() }
  }, [jobTick, load])

  useEffect(function () {
    return onTasks(function (change) {
      // The stream passes the task itself. One of this agent's with a nag log may have a new card.
      if (change && change.owner === id && (change.nag_log || []).length) { load() }
    })
  }, [id, load])

  useEffect(function () {
    function back() { if (document.visibilityState === 'visible') { load() } }
    document.addEventListener('visibilitychange', back)
    return function () { document.removeEventListener('visibilitychange', back) }
  }, [load])

  return shown
}
