/* Your own message, on screen before the record has it.
 *
 * The daemon answers a send with an `echo` frame at once, and the session file catches up a
 * second or two later. Without the echo the message you just typed went nowhere for that second
 * and looked lost. With it and nothing else it would appear twice, once as the echo and once
 * from the record. So the echo is drawn until the record holds the same words, then dropped.
 */

/* The line a tap on a choice card sends. */
const CHOICE = /^\[Choice [A-Za-z0-9_-]+\] /

/* The record keeps the text as the terminal took it, and a trailing newline is not a
 * difference. */
const flat = (t) => String(t || '').replace(/\s+/g, ' ').trim()

/* A message that came while others were held is sent as one turn: the held part, "[Now]",
 * then his words (broker.py). So his words are what follows the last "[Now]". */
const NOW = '\n\n[Now]\n\n'
const afterNow = (t) => {
  const s = String(t || '')
  const at = s.lastIndexOf(NOW)
  return at < 0 ? '' : s.slice(at + NOW.length)
}

/* True when a message of yours in the record carries the same words, alone or after "[Now]". */
export function echoLanded(rows, echo) {
  if (!echo) return false
  const want = flat(echo.text)
  if (!want) return false
  return (rows || []).some(
    (r) => r.kind === 'text' && r.who === 'operator'
      && (flat(r.text) === want || flat(afterNow(r.text)) === want),
  )
}

/* The echo to draw under the conversation, or null.
 *
 * Nothing is stored and nothing is cleared: the answer is computed from the socket's echo and
 * the history rows every render, so the moment the record has the words, the echo stops being
 * returned. The mark is what the old console wrote beside it.
 *
 * One known edge, the same one the old console had. Send the identical text twice and the
 * second echo matches the first copy in the record, so it never shows.
 */
export function useEcho(echo, rows) {
  if (!echo || echoLanded(rows, echo)) return null
  /* His tap on a choice card. The card already shows the pick, and the record folds the line
   * into the card, so an echo of it would wait forever. */
  if (CHOICE.test(String(echo.text || ''))) return null
  const waited = Date.now() - (echo.at || 0)
  return {
    text: echo.text,
    at: echo.at,
    mark: waited > 30000 ? 'not in the record yet' : 'sending',
  }
}

export default useEcho
