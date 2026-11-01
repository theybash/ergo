/* The chime when an agent finishes its turn. On the phone it is the phone's sound; in a browser
   it is that browser's. Picking one plays it and saves it at once, like every control here. */

import { useEffect, useState } from 'react'
import { CHIMES, getChime, setChime } from '../../shell/chime'

export default function ChimeRow() {
  const [chosen, setChosen] = useState(null)
  const [error, setError] = useState('')

  useEffect(function () {
    let live = true
    getChime().then(function (c) {
      if (!live) { return }
      if (c) { setChosen(c) } else { setError('Update Ergo to pick the phone\'s chime.') }
    })
    return function () { live = false }
  }, [])

  async function pick(next) {
    const was = chosen
    setChosen(next)
    setError('')
    if (!(await setChime(next))) {
      setChosen(was)
      setError('That did not save.')
    }
  }

  return (
    <div className="cf-meter">
      <div className="cf-meter-line cf-vm-line" role="radiogroup" aria-labelledby="cf-chime-h">
        <span className="cf-meter-label" id="cf-chime-h">When an agent answers</span>
        <span className="cf-bubble-opts">
          {CHIMES.map(function (c) {
            return (
              <label key={c.id} className="cf-check">
                <input type="radio" name="cf-chime" value={c.id} checked={chosen === c.id}
                  disabled={!chosen} onChange={function () { pick(c.id) }} />
                {c.label}
              </label>
            )
          })}
        </span>
      </div>
      {error ? <p className="fl-err">{error}</p> : null}
    </div>
  )
}
