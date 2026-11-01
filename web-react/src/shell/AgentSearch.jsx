/* Find an agent by name, from the top bar. Each agent shows its face, its name and the last thing it said; a
   tap, or Enter on the first, opens its chat. Only the agents the current view shows are found,
   the same as the Fleet. Faces are the letter on a colour for now;
   pictures come with agent faces. */

import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useFleet } from '../data/useFleet'
import { showsIn } from './views'
import useBackClose from './useBackClose'
import { faceColour } from '../data/faceColour'

export default function AgentSearch({ view, onScreen, onClose }) {
  const fleet = useFleet()
  const [q, setQ] = useState('')
  const box = useRef(null)
  useBackClose(true, onClose)
  useEffect(function () { if (box.current) { box.current.focus() } }, [])

  const want = q.trim().toLowerCase()
  const found = fleet.agents.filter(function (a) {
    if (!showsIn(a, view)) { return false }
    if (!want) { return true }
    return (a.name || '').toLowerCase().includes(want) || a.id.includes(want)
  }).sort(function (a, b) {
    // A name that starts with what he typed comes first.
    const as = (a.name || a.id).toLowerCase().startsWith(want) ? 0 : 1
    const bs = (b.name || b.id).toLowerCase().startsWith(want) ? 0 : 1
    return as - bs
  })

  function open(a) {
    onClose()
    onScreen('chat', '#/a/' + a.id)
  }

  // Into the body: the top bar's backdrop blur would hold a fixed layer inside the bar.
  return createPortal(
    <div className="srch-scrim" onClick={onClose}>
      <div className="srch" role="dialog" aria-modal="true" aria-label="Find an agent"
        onClick={function (e) { e.stopPropagation() }}>
        <input ref={box} className="srch-box" type="search" value={q} placeholder="Find an agent"
          aria-label="Agent name" enterKeyHint="go"
          onChange={function (e) { setQ(e.target.value) }}
          onKeyDown={function (e) { if (e.key === 'Enter' && found[0]) { e.preventDefault(); open(found[0]) } }} />
        <ul className="srch-list">
          {found.map(function (a) {
            const name = a.name || a.id
            return (
              <li key={a.id}>
                <button type="button" className="srch-row" onClick={function () { open(a) }}>
                  <span className="srch-face" style={{ background: faceColour(a.id) }} aria-hidden="true">
                    {name.slice(0, 1).toUpperCase()}
                  </span>
                  <span className="srch-words">
                    <span className="srch-name">{name}</span>
                    <span className="srch-said">{(a.last_message || '').split('\n')[0]}</span>
                  </span>
                </button>
              </li>
            )
          })}
          {!found.length ? <li className="srch-none">No agent by that name.</li> : null}
        </ul>
      </div>
    </div>,
    document.body,
  )
}
