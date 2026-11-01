/* The Cron filter. A quiet word in the Cron heading opens a short list of the agents that own a
   job, each with its count. Several can be picked; none picked means all. An agent with no job
   is not in the list, because picking it would only empty the section. The shape is Linear's:
   a trigger that names the filter, a list with checks, no chips left on the page. */

import { useEffect, useRef, useState } from 'react'

export default function CronFilter({ owners, picked, onPick }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)

  useEffect(function () {
    if (!open) { return undefined }
    function away(e) { if (box.current && !box.current.contains(e.target)) { setOpen(false) } }
    function key(e) { if (e.key === 'Escape') { setOpen(false) } }
    document.addEventListener('pointerdown', away)
    document.addEventListener('keydown', key)
    return function () {
      document.removeEventListener('pointerdown', away)
      document.removeEventListener('keydown', key)
    }
  }, [open])

  if (owners.length < 2) { return null }

  const shown = owners.filter(function (o) { return picked.includes(o.id) })
  const label = !shown.length ? 'All agents'
    : shown.length === 1 ? shown[0].name
      : shown.length + ' agents'

  function toggle(id) {
    onPick(picked.includes(id)
      ? picked.filter(function (p) { return p !== id })
      : picked.concat([id]))
  }

  return (
    <div className="crf" ref={box}>
      <button
        type="button"
        className={'crf-trigger' + (shown.length ? ' crf-on' : '')}
        aria-haspopup="true"
        aria-expanded={open}
        onClick={function () { setOpen(!open) }}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M4 6h16M7 12h10M10 18h4"></path>
        </svg>
        {label}
      </button>

      {open ? (
        <div className="crf-menu" role="menu">
          <button
            type="button"
            role="menuitemcheckbox"
            aria-checked={!shown.length}
            className="crf-row"
            onClick={function () { onPick([]) }}
          >
            <span className="crf-check" aria-hidden="true">{!shown.length ? '✓' : ''}</span>
            <span className="crf-name">All agents</span>
            <span className="crf-n">{owners.reduce(function (n, o) { return n + o.count }, 0)}</span>
          </button>
          <div className="crf-rule"></div>
          {owners.map(function (o) {
            const on = picked.includes(o.id)
            return (
              <button
                key={o.id}
                type="button"
                role="menuitemcheckbox"
                aria-checked={on}
                className="crf-row"
                onClick={function () { toggle(o.id) }}
              >
                <span className="crf-check" aria-hidden="true">{on ? '✓' : ''}</span>
                <span className="crf-name">{o.name}</span>
                <span className="crf-n">{o.count}</span>
              </button>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
