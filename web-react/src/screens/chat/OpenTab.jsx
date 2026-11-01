/* The Open tab: what this agent and the owner talked about and never did. The agent keeps the list
   in OPEN.md in its own folder; this tab only reads it, each time the tab opens.

   A tap on a line quotes it above the message box in the chat, the way Chat on a feed post does,
   so the next message is about that line (from user feedback: "I need it in this tab as well
   so I can pick it up"). */

import { useEffect, useState } from 'react'
import cc from '../../data/api'
import { pinOpen } from '../../data/taskPin'
import useScrollMemory from '../../shell/scrollMemory'

// The list line as he reads it: markdown marks off, the line itself kept.
function plain(text) {
  return text.replace(/\*\*([^*]+)\*\*/g, '$1').replace(/\*([^*]+)\*/g, '$1').replace(/`([^`]+)`/g, '$1')
}

export default function OpenTab({ id, name, active, onPick }) {
  // Its own place on the page, not the one the last screen left (shell/scrollMemory.js).
  useScrollMemory('open:' + id, active)
  const [got, setGot] = useState(null)
  const [error, setError] = useState('')

  useEffect(function () {
    if (!active || !id) { return undefined }
    let live = true
    cc.agentOpen(id).then(
      function (d) { if (live) { setGot(d); setError('') } },
      function (e) { if (live) { setError(e.message) } },
    )
    return function () { live = false }
  }, [active, id])

  if (error) { return <div className="ft"><p className="ft-empty">{error}</p></div> }
  if (!got) { return <div className="ft"><p className="ft-empty">Loading...</p></div> }
  if (!got.items.length) {
    return (
      <div className="ft">
        <p className="ft-empty">
          {got.exists ? 'Nothing open. ' : 'No open list yet. '}
          {name} keeps it in OPEN.md in its folder.
        </p>
      </div>
    )
  }

  let section = null
  const rows = []
  got.items.forEach(function (it) {
    if (it.section !== section) {
      section = it.section
      if (section) { rows.push(<li className="ot-head" key={'h' + it.n}>{section}</li>) }
    }
    rows.push(
      <li className="ft-row" key={it.n}>
        <button type="button" className="ft-open ot-line"
          onClick={function () { pinOpen(id, name, it); onPick() }}>
          {plain(it.text)}
        </button>
      </li>,
    )
  })
  return (
    <div className="ft">
      <ul className="ft-list">{rows}</ul>
    </div>
  )
}
