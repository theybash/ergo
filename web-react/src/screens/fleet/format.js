/* Turning daemon numbers into words. Local to this screen, because src/data holds the calls and
   the sockets and nothing about how a thing reads. Ported from web/app.js, which has been
   printing these lines on 8080 for months. */

/* Lower case, never uppercased by CSS. "2M" in a label reads as two million, which is a real
   misreading beside a token count. The daemon sends ISO strings for message times and Unix
   floats for everything else, so this takes both. */
export function ago(ts) {
  if (!ts) { return '' }
  const t = typeof ts === 'string' ? Date.parse(ts) / 1000 : ts
  if (!t || Number.isNaN(t)) { return '' }
  const d = Math.max(0, Date.now() / 1000 - t)
  if (d < 60) { return Math.round(d) + 's ago' }
  if (d < 3600) { return Math.round(d / 60) + 'm ago' }
  if (d < 86400) { return Math.round(d / 3600) + 'h ago' }
  return Math.round(d / 86400) + 'd ago'
}

/* The same distance, without the word "ago", for an ask that is still waiting. */
export function since(at) {
  if (!at) { return '' }
  const d = Math.max(0, (Date.now() - at) / 1000)
  if (d < 60) { return Math.round(d) + 's' }
  if (d < 3600) { return Math.round(d / 60) + 'm' }
  return Math.round(d / 3600) + 'h'
}

/* How often a job runs. 3600 is an hour, not 3600 seconds. */
export function everyShort(seconds) {
  const n = Number(seconds || 0)
  if (n % 3600 === 0) { return n / 3600 + 'h' }
  if (n % 60 === 0) { return n / 60 + 'm' }
  return n + 's'
}

/* When a job runs next. Today gives the clock alone, another day gives the weekday first. */
export function nextAt(ts) {
  if (!ts) { return 'never' }
  const d = new Date(ts * 1000)
  const today = new Date().toDateString() === d.toDateString()
  const day = today ? '' : d.toLocaleDateString(undefined, { weekday: 'short' }) + ' '
  return day + d.toTimeString().slice(0, 5)
}

/* A job's task is a whole briefing, sometimes forty lines of it. The strip shows the first line
   and stops, because a title that wraps four times is not a title. */
export function firstLine(text, max = 60) {
  const line = String(text || '').split('\n')[0].trim()
  if (line.length <= max) { return line }
  return line.slice(0, max - 1).trimEnd() + '…'
}

/* A latency that was never measured is not zero. */
export function ms(v) {
  return v == null ? 'not measured' : v + ' ms'
}

/* One decimal, and the word "none" when the daemon had nothing to give. */
export function num(v, digits = 1, unit = '') {
  if (v == null || Number.isNaN(v)) { return 'not measured' }
  return Number(v).toFixed(digits) + unit
}

/* How long until a moment in the future. The schedule strip says "next in 3h" rather than a
   clock time, because the question a person asks a cron row is how long they have, not what
   the clock will read when it fires. */
export function until(ts) {
  if (!ts) { return 'never' }
  const d = (ts * 1000 - Date.now()) / 1000
  if (d <= 0) { return 'due now' }
  if (d < 60) { return 'in ' + Math.round(d) + 's' }
  if (d < 3600) { return 'in ' + Math.round(d / 60) + 'm' }
  if (d < 86400) { return 'in ' + Math.round(d / 3600) + 'h' }
  return 'in ' + Math.round(d / 86400) + 'd'
}
