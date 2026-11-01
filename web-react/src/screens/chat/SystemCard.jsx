/* A message the owner did not type: a scheduled job, a task report, a note from another agent, a
   card he tapped. It is always shown, never hidden, and it is quieter than the chat, not louder:
   no fill, a thin mint bar on the left, one small title line, and the text one step smaller and
   dimmer, cut on whole lines. The title leads with what happened, the text's own first line, and
   names where it came from after it. A tap anywhere on the card opens the rest in place.
   A message whose title is cut, like a question passed on from a call, opens too: the whole
   message shows under the title, its first line in full.
   A receipt is the title line alone, for a tap or the machine part of his own message. */

import { useLayoutEffect, useRef, useState } from 'react'
import Prose from './Prose'
import { plain } from '../../data/plain'
import { split } from './system'

export function Chevron({ open }) {
  return (
    <svg className={open ? 'ch-sys-chev open' : 'ch-sys-chev'} viewBox="0 0 16 16" aria-hidden="true">
      <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/* A tap on a link or a drag to select text is not a tap on the card. */
function ownTap(e) {
  if (e.target.closest && e.target.closest('a, button, pre')) { return false }
  const sel = window.getSelection && window.getSelection()
  return !(sel && String(sel).length)
}

export default function SystemCard({ label, text, agent, receipt, at, failed }) {
  const body = useRef(null)
  const head = useRef(null)
  const [open, setOpen] = useState(false)
  const [long, setLong] = useState(false)
  const [cut, setCut] = useState(false)
  const parts = receipt ? { title: label, body: '' } : split(text)
  const title = parts.title || label
  // A receipt keeps its few words on the title line, after the label.
  const from = receipt ? plain(text) : (parts.title ? label : '')

  // Whether the cut hides anything, measured once drawn. The chevron shows only when it does.
  useLayoutEffect(function () {
    const el = body.current
    /* oxlint-disable-next-line react/set-state-in-effect -- the cut is known only once drawn. */
    if (el && !open) { setLong(el.scrollHeight > el.clientHeight + 1) }
    const t = head.current
    /* oxlint-disable-next-line react/set-state-in-effect -- the cut is known only once drawn. */
    if (t && !open) { setCut(t.scrollWidth > t.clientWidth + 1) }
  }, [parts.body, parts.title, open])

  // A cut title opens too, to the whole message, its first line in full.
  const whole = !receipt && cut
  const shown = whole && open ? String(text || '').trim() : parts.body
  const opens = whole || (!!parts.body && (long || open))
  function toggle(e) { if (opens && ownTap(e)) { setOpen(!open) } }
  function key(e) {
    if (opens && (e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
      e.preventDefault()
      setOpen(!open)
    }
  }

  return (
    <div
      className={'ch-sys' + (receipt ? ' receipt' : '') + (opens ? ' opens' : '')}
      role={opens ? 'button' : undefined}
      tabIndex={opens ? 0 : undefined}
      aria-expanded={opens ? open : undefined}
      onClick={toggle}
      onKeyDown={key}
    >
      <p className="ch-sys-head">
        <span ref={head} className="ch-sys-title">{title}</span>
        {from ? <span className="ch-sys-from">{from}</span> : null}
        {failed ? <span className="ch-sys-bad">failed</span> : null}
        {at ? <span className="ch-sys-at">{at}</span> : null}
        {opens ? <Chevron open={open} /> : null}
      </p>
      {shown
        ? (
          <div ref={body} className={open ? 'ch-sys-body open' : 'ch-sys-body'}>
            <Prose text={shown} agent={agent} className="ch-sys-text" />
          </div>
        )
        : null}
    </div>
  )
}
