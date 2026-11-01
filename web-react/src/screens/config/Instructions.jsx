/* The CLAUDE.md files: what every agent reads, in the order it reads them. One row each, with its
   size in words; a tap opens the file in the same box as the front desk's prompt, with Save, Cancel and
   Put back the previous. One file is open at a time.

   An agent reads its file when its session starts, so a save reaches it at its next start. Each
   save is kept in the daemon's edit log, which the master agent reads. */

import { useEffect, useMemo, useState } from 'react'
import cc from '../../data/api'
import DeskPrompt from './DeskPrompt'

function docFor(f) {
  return {
    id: 'cf-md-' + f.key, label: f.name,
    load: function () { return cc.instructionFile(f.key) },
    save: function (text) { return cc.saveInstructionFile(f.key, text) },
    restore: function () { return cc.restoreInstructionFile(f.key) },
    saved: 'Saved. The agent reads it at its next start.',
    putBack: 'Put back. The agent reads it at its next start.',
  }
}

export default function Instructions() {
  const [list, setList] = useState(null)
  const [open, setOpen] = useState('')

  useEffect(function () {
    let live = true
    cc.instructionFiles().then(function (d) { if (live) { setList(d.files || []) } }, function () {})
    return function () { live = false }
  }, [])

  const docs = useMemo(function () {
    const out = {}
    for (const f of list || []) { out[f.key] = docFor(f) }
    return out
  }, [list])

  if (!list) { return null }
  return (
    <div className="cf-logins">
      <p className="cf-note">What each agent reads: the global file, its house's file, then its own. A save reaches an agent at its next start.</p>
      {list.map(function (f) {
        const isOpen = open === f.key
        return (
          <div key={f.key}>
            <button
              type="button"
              className="cf-login cf-md-row"
              aria-expanded={isOpen}
              onClick={function () { setOpen(isOpen ? '' : f.key) }}
            >
              <span className="cf-login-what">{f.name}</span>
              <span className="cf-login-site">{f.words} words</span>
            </button>
            {isOpen ? <DeskPrompt doc={docs[f.key]} /> : null}
          </div>
        )
      })}
    </div>
  )
}
