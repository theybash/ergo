/* The front desk's prompt: his persona file, voice_persona.md, in a box he can edit.

   Save writes it for the next call; a call already open keeps what it started with. The daemon
   keeps the version before each save, so Put back the previous swaps the two, and a second press
   swaps them again. Cancel drops the edit. The fleet, the time, the view and the briefs are
   added under this text on every call, so they are not in the box.

   The same box edits the agents' voice prompt (agent_voice.md): how every agent speaks when it
   calls him or he calls it. Who the agent is, why it called and the time are added around it. */

import { useEffect, useState } from 'react'
import cc from '../../data/api'

const DESK = {
  id: 'cf-desk-prompt', label: "The front desk's prompt",
  load: cc.voicePersona, save: cc.saveVoicePersona, restore: cc.restoreVoicePersona,
}

export default function DeskPrompt({ doc = DESK }) {
  const [held, setHeld] = useState(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState('')

  useEffect(function () {
    let live = true
    doc.load().then(
      function (d) { if (live) { setHeld(d); setDraft(d.text) } },
      function () {},
    )
    return function () { live = false }
  }, [doc])

  if (!held) { return null }

  const changed = draft !== held.text

  async function run(fn, done) {
    setBusy(true)
    setSaid('')
    try {
      const d = await fn()
      setHeld(d)
      setDraft(d.text)
      setSaid(done)
    } catch (e) {
      setSaid(e.message || 'That did not save.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="hp">
      <textarea
        id={doc.id}
        className="hp-box"
        aria-label={doc.label}
        spellCheck="true"
        value={draft}
        maxLength={held.limit || 20000}
        onChange={function (e) { setDraft(e.target.value); setSaid('') }}
        onKeyDown={function (e) {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && changed && !busy) {
            e.preventDefault()
            run(function () { return doc.save(draft) }, doc.saved || 'Saved. The next call uses it.')
          }
        }}
      />
      <div className="hp-acts">
        <span className="hp-said" role="status">{said}</span>
        {held.previous && !changed ? (
          <button
            type="button"
            className="btn sm quiet"
            disabled={busy}
            onClick={function () { run(doc.restore, doc.putBack || 'Put back. The next call uses it.') }}
          >
            Put back the previous
          </button>
        ) : null}
        {changed ? (
          <button type="button" className="btn sm quiet" disabled={busy}
            onClick={function () { setDraft(held.text); setSaid('') }}>
            Cancel
          </button>
        ) : null}
        <button
          type="button"
          className="btn sm"
          disabled={busy || !changed || !draft.trim()}
          onClick={function () {
            run(function () { return doc.save(draft) }, doc.saved || 'Saved. The next call uses it.')
          }}
        >
          Save
        </button>
      </div>
    </div>
  )
}
