/* The plan prompt: what an agent reads when he presses Plan on a task card.

   The task's title, source and what he gets are added under this text, so they are not in the
   box. Save writes it for the next Plan. Put back the default restores the words it shipped
   with.

   Under it, the hours that make a task hot. A change to a task due within
   them, or overdue, wakes the agent that owns it; any other change waits for its next message. */

import { useEffect, useState } from 'react'
import cc from '../../data/api'

export default function TasksPrompt() {
  const [held, setHeld] = useState(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState('')
  const [hours, setHours] = useState('')
  const [hoursSaid, setHoursSaid] = useState('')

  useEffect(function () {
    let live = true
    cc.taskSettings().then(
      function (d) { if (live) { setHeld(d); setDraft(d.plan_prompt); setHours(String(d.hot_hours)) } },
      function () {},
    )
    return function () { live = false }
  }, [])

  if (!held) { return null }

  const changed = draft !== held.plan_prompt
  const isDefault = held.plan_prompt === held.default_plan_prompt

  async function saveHours() {
    const n = Number(hours)
    if (!hours.trim() || Number.isNaN(n) || n === held.hot_hours) { setHours(String(held.hot_hours)); return }
    try {
      const d = await cc.saveTaskSettings({ hot_hours: n })
      setHeld(d)
      setHours(String(d.hot_hours))
      setHoursSaid('Saved.')
    } catch (e) {
      setHoursSaid(e.message || 'That did not save.')
      setHours(String(held.hot_hours))
    }
  }

  async function save(text, done) {
    setBusy(true)
    setSaid('')
    try {
      const d = await cc.saveTaskSettings({ plan_prompt: text })
      setHeld(d)
      setDraft(d.plan_prompt)
      setSaid(done)
    } catch (e) {
      setSaid(e.message || 'That did not save.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="hp">
      <label className="cf-hide" htmlFor="cf-plan-prompt">The plan prompt</label>
      <textarea
        id="cf-plan-prompt"
        className="hp-box tk-prompt"
        spellCheck="true"
        value={draft}
        maxLength={held.limit || 4000}
        onChange={function (e) { setDraft(e.target.value); setSaid('') }}
        onKeyDown={function (e) {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && changed && !busy) {
            e.preventDefault()
            save(draft, 'Saved. The next Plan uses it.')
          }
        }}
      />
      <div className="hp-acts">
        <span className="hp-said" role="status">{said || 'What an agent reads when you press Plan.'}</span>
        {!isDefault && !changed ? (
          <button type="button" className="btn sm quiet" disabled={busy}
            onClick={function () { save('', 'Put back the default.') }}>
            Put back the default
          </button>
        ) : null}
        {changed ? (
          <button type="button" className="btn sm quiet" disabled={busy}
            onClick={function () { setDraft(held.plan_prompt); setSaid('') }}>
            Cancel
          </button>
        ) : null}
        <button type="button" className="btn sm" disabled={busy || !changed || !draft.trim()}
          onClick={function () { save(draft, 'Saved. The next Plan uses it.') }}>
          Save
        </button>
      </div>
      <div className="hp-acts">
        <label className="hp-said" htmlFor="cf-hot-hours">Wake a task's agent when the task is due within</label>
        <input id="cf-hot-hours" className="hp-box tk-hours" type="number" min="0" max="336" step="1"
          inputMode="numeric" value={hours}
          onChange={function (e) { setHours(e.target.value); setHoursSaid('') }}
          onBlur={saveHours}
          onKeyDown={function (e) { if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur() } }} />
        <span className="hp-said" role="status">{hoursSaid || 'hours, or overdue'}</span>
      </div>
    </div>
  )
}
