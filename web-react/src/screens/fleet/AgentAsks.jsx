/* One awake agent's pending asks, reported upward. It draws nothing.

   An ask arrives on the agent's own WebSocket, not in /api/state, so there is one socket per
   awake agent and one of these components to hold it. A sleeping agent has no engine and can
   have no ask, so Fleet mounts none for it and the socket is never opened.

   The socket is asked for with wake false, which is the same key the chat screen uses, so the
   two share one socket rather than opening two on the same agent. */

import { useEffect } from 'react'
import useAgentSocket from '../../data/useAgentSocket'

export default function AgentAsks({ id, onAsks }) {
  const sock = useAgentSocket(id, { wake: false })
  const asks = sock.asks

  useEffect(function () {
    onAsks(id, asks)
  }, [id, asks, onAsks])

  /* When the agent falls asleep this unmounts, and its asks go with it. The daemon has already
     lost them: the engine that was waiting is gone. */
  useEffect(function () {
    return function () { onAsks(id, null) }
  }, [id, onAsks])

  return null
}
