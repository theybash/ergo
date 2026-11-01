/* The agent rail inside the agent page. Desktop only: the CSS hides it under 720px.
   The pill glides down the list instead of across it.

   Delegates fold under their parent, the same fold as the fleet: the
   parent's row ends in a face stack, and a tap on it unfolds them. The open agent's own parent is
   always unfolded, so the picked row is never hidden. Search looks through every agent, delegates
   too, and shows the matches as a flat list. */

import { useRef, useState } from 'react'
import useGlider from './useGlider'
import useFolds from '../data/folds'
import Dot from '../primitives/Dot'
import FaceStack from '../primitives/FaceStack'
import { unread } from '../data/when'

export default function Rail({ agents, active, onPick }) {
  const wrap = useRef(null)
  const [query, setQuery] = useState('')
  const [folds, fold] = useFolds()
  useGlider(wrap, active, { axis: 'v', selector: '[aria-current="true"]', parent: true })

  const ids = new Set(agents.map(function (a) { return a.id }))
  const subsOf = function (id) { return agents.filter(function (a) { return a.parent === id }) }
  const picked = agents.find(function (a) { return a.id === active })
  const q = query.trim().toLowerCase()

  let rows
  if (q) {
    rows = agents.filter(function (a) { return a.name.toLowerCase().includes(q) || a.id.includes(q) })
      .map(function (a) { return { agent: a, sub: false } })
  } else {
    rows = []
    for (const a of agents) {
      if (a.parent && ids.has(a.parent)) { continue }
      const subs = subsOf(a.id)
      const open = !!folds[a.id] || (picked && picked.parent === a.id)
      rows.push({ agent: a, subs, open })
      if (open) { for (const d of subs) { rows.push({ agent: d, sub: true }) } }
    }
  }

  return (
    <nav className="rail" aria-label="Agents">
      <p className="rail-head">Agents</p>
      <input className="rail-search" type="search" placeholder="Search" value={query}
        aria-label="Search agents" onChange={function (e) { setQuery(e.target.value) }}
        onKeyDown={function (e) {
          if (e.key === 'Escape') { setQuery('') }
          if (e.key === 'Enter' && rows.length) { onPick(rows[0].agent.id); setQuery('') }
        }} />
      <div className="rail-wrap" ref={wrap}>
        <span className="glider" aria-hidden="true"></span>
        <ul className="rail-list">
          {rows.map(function ({ agent, sub, subs, open }) {
            const news = subs ? subs.filter(unread).length : 0
            return (
              <li key={agent.id} className={sub ? 'rail-sub' : undefined}>
                <button
                  type="button"
                  className="rail-item"
                  aria-current={agent.id === active ? 'true' : undefined}
                  onClick={function () { onPick(agent.id) }}
                >
                  <Dot state={agent.state} />
                  <span className="rail-name">{agent.name}</span>
                  {unread(agent) ? <span className="ac-unread" role="img" aria-label="Unread"></span> : null}
                  {agent.needs > 0 ? <span className="count">{agent.needs}</span> : null}
                  {subs && subs.length ? (
                    <span className="rail-stack" role="button" tabIndex={0} aria-expanded={!!open}
                      aria-label={(open ? 'Fold ' : 'Unfold ') + subs.length + ' delegates'}
                      onClick={function (e) { e.stopPropagation(); fold(agent.id) }}
                      onKeyDown={function (e) {
                        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); fold(agent.id) }
                      }}>
                      <FaceStack agents={subs} size={16} />
                      {news ? <span className="count">{news}</span> : null}
                    </span>
                  ) : null}
                </button>
              </li>
            )
          })}
          {q && !rows.length ? <li className="rail-none">No agent called that.</li> : null}
        </ul>
      </div>
    </nav>
  )
}
