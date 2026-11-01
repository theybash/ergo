import { useCallback, useEffect, useRef, useState } from 'react'
import { messages as fetchMessages } from './api'

/* The settled conversation, read from the agent's session file.
 *
 * Fetched when a turn settles, and when the page comes back into view, and merged into
 * what is on screen rather than put in its place. A row keeps its key and its object for as long
 * as it is on screen, so React moves nothing that did not change, and a row the reader is on is
 * never taken away by a fetch. See load().
 */

const FIRST = 30
/* A working agent writes a row per tool call and per result, so one answer can be sixty rows.
 * Scrolling up asks for this many more at a time, up to MOST. */
const MORE = 500
const MOST = 10000
/* A refresh reads only the newest rows. The rows above them are already on screen and kept, so
 * reading all of them again every few seconds would be the same bytes over and over. */
const TAIL = 200

/* Enough of a row to tell it from a changed version of itself, without keeping a copy of it. The
 * daemon sends `pos`, where the row sits in the file, and that never changes however much of the
 * file a read covers. An older daemon sends no pos, and then the time stands in for it. A choice
 * card changes when his pick comes in, and a card changes when the tool refuses it, so both are
 * in it too. */
function rowKey(row) {
  return [row.pos || '', row.kind, row.who, row.tool || '', row.id || '', row.at || '',
    (row.text || row.input || '').length, row.answer || '', row.refused ? 1 : 0].join('\u0000')
}

/* One key per row. Two rows alike in every part of rowKey get a count after the second. With pos
 * in the key that happens only with an older daemon, and then the count depends on the window. */
function keyed(got) {
  const seen = new Map()
  return got.map((r) => {
    const k = rowKey(r)
    const n = seen.get(k) || 0
    seen.set(k, n + 1)
    return n ? `${k}#${n}` : k
  })
}

export function useHistory(id, settledTick = 0) {
  const [rows, setRows] = useState([])
  /* True when the daemon says the file has rows above the ones it sent and there is room to ask
   * for more. Held as state because only a read of everything asked for may change it. */
  const [hasMore, setHasMore] = useState(false)
  /* The keys of what is on screen and how many were asked for. Refs, because neither should
   * cause a render of its own. */
  const shown = useRef([])
  const want = useRef(FIRST)
  const gone = useRef(false)
  /* The agent whose rows are on screen. An answer for another agent, late from before a switch,
   * is thrown away: `gone` alone was set back to false by the new agent before it landed. */
  const mine = useRef(id)
  /* Which fetch was asked last and which answer was drawn last. An answer that comes back after
   * a newer one has been drawn is older news, and drawing it would take the newest rows away. */
  const asked = useRef(0)
  const drawn = useRef(0)

  /* THE TOP STAYS WHERE IT IS
   * The daemon answers with the last n rows of the file. A working agent adds a row for every
   * tool call and every result, so each answer during a turn starts further down the file than
   * the last one did. Drawing it as it came took the oldest rows off the top of the log: the
   * lines the reader was on went, the page got shorter under them, the browser pulled them to
   * the bottom, and the log then followed the stream from there. So the rows already on screen
   * that an answer no longer reaches back to are kept, and the next fetch asks for as many rows
   * as are on screen, so it still overlaps. Rows leave the log only when the agent changes. */
  const load = useCallback(async function run(whole = false) {
    if (!id) return
    const ticket = ++asked.current
    const count = whole ? want.current : Math.min(want.current, TAIL)
    let got, more
    try {
      const page = await fetchMessages(id, count)
      got = page.messages || []
      /* The daemon knows whether its read reached the start of the file. Counting rows cannot
       * tell: a raw line is not always a row, so an answer can be short with more above it. An
       * older daemon sends no flag, and then a full answer is the best guess. */
      more = typeof page.more === 'boolean' ? page.more : got.length >= count
    } catch {
      /* A refused read is a file that is not there yet, or an agent that has gone. Keeping what
       * is on screen is better than blanking a conversation somebody is reading. */
      return
    }
    if (gone.current || mine.current !== id || ticket < drawn.current) return

    const keys = keyed(got)
    const fresh = new Set(keys)
    /* Where this answer starts in what is on screen. Everything above that point is a row the
     * answer no longer reaches, and it stays. -1 means the answer reaches further back than the
     * screen does, which is Load older, or that nothing is on screen yet. */
    const at = keys.length ? shown.current.indexOf(keys[0]) : -1
    /* A busy turn wrote more rows than a refresh reads, so the answer does not reach the screen at
     * all. Drawing it would take every row above it away. Read again, enough to overlap. */
    if (!whole && at < 0 && shown.current.length && keys.length >= count) {
      want.current = Math.min(MOST, shown.current.length + keys.length)
      return run(true)
    }
    drawn.current = ticket
    const head = at > 0 ? shown.current.slice(0, at).filter((k) => !fresh.has(k)) : []

    shown.current = head.concat(keys)
    want.current = Math.min(MOST, Math.max(want.current, shown.current.length))
    setRows((prev) => {
      /* The same row keeps the same object, so React sees only what is new or changed. */
      const had = new Map(prev.map((r) => [r.key, r]))
      const top = head.map((k) => had.get(k)).filter(Boolean)
      return top.concat(got.map((r, i) => had.get(keys[i]) || { ...r, key: keys[i] }))
    })
    /* Only a read of everything asked for can say whether there is more above it. */
    if (whole || count === want.current) setHasMore(more && count < MOST)
  }, [id])

  /* A new agent starts from the bottom of its own file. */
  useEffect(() => {
    gone.current = false
    mine.current = id
    shown.current = []
    want.current = FIRST
    /* oxlint-disable-next-line react/set-state-in-effect -- a new agent must not show the last
     * one's conversation while its own is fetched. */
    setRows([])
    load(true)
    return () => { gone.current = true }
  }, [id, load])

  /* A turn landed in the file, so the last live reply is now a settled row. The 8 s fallback in
   * useAgentSocket bumps this tick too, so a turn whose last frame was lost still settles. */
  useEffect(() => {
    if (settledTick) load()
  }, [settledTick, load])

  /* No timer. The live socket says when a turn has settled, and that is the only time the file
   * grows with something worth reading. A page that was hidden may have missed a settle while the
   * phone slept, so coming back to it reads the newest rows once. */
  useEffect(() => {
    if (!id) return undefined
    const back = () => { if (document.visibilityState === 'visible') load() }
    document.addEventListener('visibilitychange', back)
    return () => document.removeEventListener('visibilitychange', back)
  }, [id, load])

  /* Older messages are fetched only when you go looking for them. The caller keeps the line it
   * was reading where it was, because the list grows at the top. */
  const loadOlder = useCallback(() => {
    if (want.current >= MOST) return
    want.current = Math.min(want.current + MORE, MOST)
    load(true)
  }, [load])

  const refresh = useCallback(() => load(), [load])

  return { rows, loadOlder, hasMore, refresh }
}

export default useHistory
