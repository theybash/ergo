/* Helpers for the system cards: a card's title and body, and runs of cards in the log. */

import { plain } from '../../data/plain'
import { read } from './kinds'

/* The first line that has words is the title, as plain words. The rest is the body. */
export function split(text) {
  const lines = String(text || '').trim().split('\n')
  const first = lines.findIndex(function (line) { return line.trim() })
  if (first < 0) { return { title: '', body: '' } }
  return { title: plain(lines[first]), body: lines.slice(first + 1).join('\n').trim() }
}

/* A row of small things only, none of his own words: a job's result, a task report, a one line
   event. Two or more in a row fold into one line. What he reads or acts on never folds: another
   agent speaking, a call, a task, a feed post, his own words. */
export function isSystem(item) {
  if (item.work) { return false }
  const r = item.row
  if (r.kind === 'shown') { return true }
  if (r.kind !== 'text' || r.who !== 'operator') { return false }
  const parts = read(r.text)
  return !!parts && parts.every(function (p) { return p.fold === true })
}

/* The drawn items, with each run of system cards together. */
export function runsOf(drawn) {
  const out = []
  drawn.forEach(function (item) {
    const last = out[out.length - 1]
    if (isSystem(item) && last && last.system) {
      last.items.push(item)
    } else {
      out.push({ system: isSystem(item), items: [item] })
    }
  })
  return out
}
