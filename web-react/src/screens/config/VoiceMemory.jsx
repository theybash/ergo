/* Three numbers for the front desk, from one GET /api/voice/settings.

     Voice memory     turns of the call log read at the start of every call, 1 to 100
     Recall context   turns before and after each match recall hands back, 0 to 10
     Silence hang up  minutes with nobody speaking before a call hangs up, 1 to 30

   They save the way every other control on Config saves: the moment one is settled, on Enter or
   when the field loses focus. No Save button and no success message; the number simply stays.
   A failure puts one line under that field and it goes back to what the daemon holds. A daemon
   without the route draws nothing at all, and a daemon without the silence setting draws only
   the first two. */

import { useEffect, useState } from 'react'
import cc from '../../data/api'

function NumberRow({ name, label, rest, value, limits, onSave }) {
  const [draft, setDraft] = useState(String(value))
  const [error, setError] = useState('')

  async function save() {
    const n = Number(draft)
    if (draft.trim() === '' || !Number.isFinite(n)) {
      setDraft(String(value))
      return
    }
    const want = Math.max(limits.min, Math.min(limits.max, Math.round(n)))
    setDraft(String(want))
    if (want === value) { return }
    setError('')
    try {
      await onSave({ [name]: want })
    } catch (e) {
      setError(e.message || 'That did not save.')
      setDraft(String(value))
    }
  }

  const id = 'cf-vm-' + name
  return (
    <div className="cf-meter">
      <p className="cf-meter-line cf-vm-line">
        <label className="cf-meter-label" htmlFor={id}>{label}</label>
        <input
          id={id}
          className="cf-number"
          type="number"
          inputMode="numeric"
          min={limits.min}
          max={limits.max}
          step="1"
          value={draft}
          onChange={function (e) { setDraft(e.target.value) }}
          onBlur={save}
          onKeyDown={function (e) { if (e.key === 'Enter') { e.currentTarget.blur() } }}
        />
        <span className="cf-meter-rest">{rest}</span>
      </p>
      {error ? <p className="cf-err">{error}</p> : null}
    </div>
  )
}

export default function VoiceMemory() {
  const [held, setHeld] = useState(null)

  useEffect(function () {
    let live = true
    cc.voiceSettings().then(
      function (d) { if (live) { setHeld(d) } },
      function () {},
    )
    return function () { live = false }
  }, [])

  if (!held || !held.limits) { return null }

  async function save(fields) {
    setHeld(await cc.saveVoiceSettings(fields))
  }

  return (
    <>
      {/* Keyed by the saved value, so a new value from the daemon starts a fresh draft. */}
      <NumberRow
        key={'memory_turns' + held.memory_turns}
        name="memory_turns"
        label="Voice memory"
        rest="turns read at each call start"
        value={held.memory_turns}
        limits={held.limits.memory_turns}
        onSave={save}
      />
      <NumberRow
        key={'recall_span' + held.recall_span}
        name="recall_span"
        label="Recall context"
        rest="turns before and after each match"
        value={held.recall_span}
        limits={held.limits.recall_span}
        onSave={save}
      />
      {held.limits.silence_minutes ? (
        <NumberRow
          key={'silence_minutes' + held.silence_minutes}
          name="silence_minutes"
          label="Silence hang up"
          rest="minutes with nobody speaking"
          value={held.silence_minutes}
          limits={held.limits.silence_minutes}
          onSave={save}
        />
      ) : null}
    </>
  )
}
