/* Two or more system cards in a row, as one line: how many, and the newest one's title. A tap
   opens the list, each card as it would be on its own. Nothing is hidden for good: the line says
   what is in it, and one tap shows it all. */

import { useState } from 'react'
import { Chevron } from './SystemCard'
import { read } from './kinds'
import { split } from './system'

function titleOf(item) {
  const r = item.row
  if (r.kind === 'shown') { return split(r.shown.text).title || r.shown.name }
  const parts = read(r.text) || []
  const p = parts[parts.length - 1] || {}
  return p.name || split(p.text).title || p.card || ''
}

function when(item) {
  const r = item.row
  const d = r.kind === 'shown' ? new Date(r.shown.at * 1000) : new Date(r.at)
  return isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function SysRun({ items, children }) {
  const [open, setOpen] = useState(false)
  const jobs = items.every(function (item) { return item.row.kind === 'shown' })
  const failed = items.filter(function (item) {
    return item.row.kind === 'shown' && item.row.shown.outcome !== undefined && item.row.shown.outcome !== 'succeeded'
  }).length
  const newest = items[items.length - 1]

  return (
    <div className="ch-sysrun">
      <div
        className="ch-sys opens"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={function () { setOpen(!open) }}
        onKeyDown={function (e) {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(!open) }
        }}
      >
        <p className="ch-sys-head">
          <span className="ch-sys-title">{items.length + (jobs ? ' job results' : ' messages')}</span>
          {open ? null : <span className="ch-sys-from">{titleOf(newest)}</span>}
          {failed ? <span className="ch-sys-bad">{failed}</span> : null}
          <span className="ch-sys-at">{when(newest)}</span>
          <Chevron open={open} />
        </p>
      </div>
      {open ? <div className="ch-sysrun-list">{children}</div> : null}
    </div>
  )
}
