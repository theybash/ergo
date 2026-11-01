/* The notes at the foot of the log.

   A note is a one-off sentence for the person: the daemon saying the chat engine took over from
   a terminal, or a refused answer saying why. It is drawn at the bottom of the log rather than
   above the composer, because the composer is for typing and a sentence over it was read as a
   warning about the composer itself.

   The socket hook keeps every note it has ever heard, and never drops one. This keeps the last
   three and forgets each after nine seconds, because a note is worth one read. It also gives
   the screen a way to say something of its own, which the hook has no room for: a send that
   failed, an answer the daemon refused, a file that landed. */

import { useCallback, useEffect, useRef, useState } from 'react'

// Long enough to read twice, short enough that the log is not carrying yesterday's news.
const LIFE = 9000
const KEEP = 3

export function useNotes(id, fromSocket) {
  const [notes, setNotes] = useState([])
  // How many of the socket's notes have been taken already, and a counter for stable keys.
  const taken = useRef(0)
  const seq = useRef(0)

  const say = useCallback(function (text) {
    if (!text) { return }
    seq.current += 1
    const row = { id: seq.current, text: String(text), at: Date.now() }
    setNotes(function (rows) { return rows.concat([row]).slice(-KEEP) })
  }, [])

  // Another agent, another conversation. Its notes are not yours.
  useEffect(function () {
    taken.current = 0
    /* oxlint-disable-next-line react/set-state-in-effect -- the agent on screen changed, and a
     * note about the last one must not survive the change. */
    setNotes([])
  }, [id])

  useEffect(function () {
    const all = fromSocket || []
    if (all.length <= taken.current) {
      // Shorter than last time means a new socket, so start counting again.
      taken.current = all.length
      return
    }
    const fresh = all.slice(taken.current).map(function (n) {
      seq.current += 1
      return { id: seq.current, text: n.text, at: n.at || Date.now() }
    })
    taken.current = all.length
    /* oxlint-disable-next-line react/set-state-in-effect -- the socket is an external system
     * and a frame from it is the only thing that can start a note. */
    setNotes(function (rows) { return rows.concat(fresh).slice(-KEEP) })
  }, [fromSocket])

  useEffect(function () {
    if (!notes.length) { return undefined }
    const t = setInterval(function () {
      setNotes(function (rows) {
        const live = rows.filter(function (r) { return Date.now() - r.at < LIFE })
        return live.length === rows.length ? rows : live
      })
    }, 1000)
    return function () { clearInterval(t) }
  }, [notes.length])

  return { notes, say }
}

export default useNotes
