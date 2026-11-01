/* Which engine the chat's microphone dictates with. OpenAI live is the default; Gemini live
   needs its own key; Record writes the clip down when you stop, not live. It saves the moment it is picked, like every control here. */

import { useEffect, useState } from 'react'
import { dictateSettings, saveDictate } from '../../data/api'

const ENGINES = [
  { id: 'openai', label: 'OpenAI live' },
  { id: 'gemini', label: 'Gemini live' },
  { id: 'record', label: 'Record, then write' },
]

export default function DictationEngine() {
  const [engine, setEngine] = useState(null)
  const [error, setError] = useState('')

  useEffect(function () {
    let live = true
    dictateSettings().then(
      function (r) { if (live && r) { setEngine(r.engine) } },
      function () { if (live) { setError('Could not read the setting.') } },
    )
    return function () { live = false }
  }, [])

  async function pick(next) {
    if (next === engine) { return }
    const was = engine
    setEngine(next)
    setError('')
    try {
      const r = await saveDictate(next)
      if (!r || r.engine !== next) { throw new Error() }
    } catch {
      setEngine(was)
      setError('That did not save.')
    }
  }

  return (
    <div className="cf-meter">
      <div className="cf-meter-line cf-vm-line" role="radiogroup" aria-labelledby="cf-dict-h">
        <span className="cf-meter-label" id="cf-dict-h">Engine</span>
        <span className="cf-bubble-opts">
          {ENGINES.map(function (e) {
            return (
              <label key={e.id} className="cf-check">
                <input type="radio" name="cf-dict-engine" value={e.id} checked={engine === e.id}
                  disabled={!engine} onChange={function () { pick(e.id) }} />
                {e.label}
              </label>
            )
          })}
        </span>
      </div>
      {error ? <p className="fl-err">{error}</p> : null}
    </div>
  )
}
