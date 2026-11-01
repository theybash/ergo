/* The bubble wheel's look, HUD, Glass, Glass 2, Bubbles or Dial. The choice lives in the Android
   app, so this row is drawn only when the console runs inside it; in a browser there is nothing
   to set and it draws nothing.

   It saves the way every other control on Config saves: the moment it is picked. The wheel reads
   it the next time it opens. */

import { useEffect, useState } from 'react'
import { askApp, inApp } from '../../shell/native'
import ReadNotifications from './ReadNotifications'

const THEMES = [
  { id: 'hud', label: 'HUD' },
  { id: 'glass', label: 'Glass' },
  { id: 'glass2', label: 'Glass 2' },
  { id: 'bubbles', label: 'Bubbles' },
  { id: 'dial', label: 'Dial' },
]

const WORDS = [
  { id: 'one', label: 'One at a time' },
  { id: 'none', label: 'None' },
]

export default function BubbleTheme() {
  const [theme, setTheme] = useState(null)
  const [blur, setBlur] = useState(false)
  const [words, setWords] = useState('one')
  const [follow, setFollow] = useState(false)
  const [needsFollow, setNeedsFollow] = useState(false)
  const [error, setError] = useState('')

  useEffect(function () {
    let live = true
    if (!inApp()) { return function () { live = false } }
    askApp('getWheelTheme').then(function (r) {
      if (live && r && r.theme) { setTheme(r.theme) }
    })
    askApp('getMenuWords').then(function (r) {
      if (live && r && (r.words === 'one' || r.words === 'none')) { setWords(r.words) }
    })
    askApp('getRecentFollowsView').then(function (r) {
      if (live && r && typeof r.on === 'boolean') { setFollow(r.on) }
    })
    askApp('getNeedsFollowsView').then(function (r) {
      if (live && r && typeof r.on === 'boolean') { setNeedsFollow(r.on) }
    })
    // An app too old to know the blur answers nothing, and the switch stays off.
    askApp('getDialBlur').then(function (r) {
      if (live && r && typeof r.on === 'boolean') { setBlur(r.on) }
    })
    return function () { live = false }
  }, [])

  if (!theme) { return null }

  async function pick(next) {
    if (next === theme) { return }
    setError('')
    const was = theme
    setTheme(next)
    const r = await askApp('setWheelTheme', { theme: next })
    if (!r || r.theme !== next) {
      setTheme(was)
      setError('That did not save.')
    }
  }

  async function toggleBlur(on) {
    setError('')
    setBlur(on)
    const r = await askApp('setDialBlur', { on: on })
    if (!r || r.on !== on) {
      setBlur(!on)
      setError('That did not save.')
    }
  }

  async function pickWords(next) {
    if (next === words) { return }
    setError('')
    const was = words
    setWords(next)
    const r = await askApp('setMenuWords', { words: next })
    if (!r || r.words !== next) {
      setWords(was)
      setError('That did not save.')
    }
  }

  async function toggleFollow(on) {
    setError('')
    setFollow(on)
    const r = await askApp('setRecentFollowsView', { on: on })
    if (!r || r.on !== on) {
      setFollow(!on)
      setError('That did not save.')
    }
  }

  async function toggleNeedsFollow(on) {
    setError('')
    setNeedsFollow(on)
    const r = await askApp('setNeedsFollowsView', { on: on })
    if (!r || r.on !== on) {
      setNeedsFollow(!on)
      setError('That did not save.')
    }
  }

  return (
    <div className="cf-meter">
      <div className="cf-meter-line cf-vm-line" role="radiogroup" aria-labelledby="cf-bubble-h">
        <span className="cf-meter-label" id="cf-bubble-h">Bubble</span>
        <span className="cf-bubble-opts">
          {THEMES.map(function (t) {
            return (
              <label key={t.id} className="cf-check">
                <input
                  type="radio"
                  name="cf-bubble-theme"
                  value={t.id}
                  checked={theme === t.id}
                  onChange={function () { pick(t.id) }}
                />
                {t.label}
              </label>
            )
          })}
        </span>
      </div>
      {/* The rows below are for the menu themes, Dial and Glass 2, read at each open; the other
          themes ignore them. */}
      <div className="cf-meter-line cf-vm-line" role="radiogroup" aria-labelledby="cf-words-h">
        <span className="cf-meter-label" id="cf-words-h">Words</span>
        <span className="cf-bubble-opts">
          {WORDS.map(function (w) {
            return (
              <label key={w.id} className="cf-check">
                <input
                  type="radio"
                  name="cf-menu-words"
                  value={w.id}
                  checked={words === w.id}
                  onChange={function () { pickWords(w.id) }}
                />
                {w.label}
              </label>
            )
          })}
        </span>
      </div>
      <div className="cf-meter-line cf-vm-line">
        <span className="cf-meter-label" aria-hidden="true"></span>
        <span className="cf-bubble-opts">
          <label className="cf-check">
            <input
              type="checkbox"
              checked={blur}
              onChange={function (e) { toggleBlur(e.target.checked) }}
            />
            Blur behind the menu
          </label>
        </span>
      </div>
      <div className="cf-meter-line cf-vm-line">
        <span className="cf-meter-label" aria-hidden="true"></span>
        <span className="cf-bubble-opts">
          <label className="cf-check">
            <input
              type="checkbox"
              checked={follow}
              onChange={function (e) { toggleFollow(e.target.checked) }}
            />
            Recent follows the view
          </label>
          <label className="cf-check">
            <input
              type="checkbox"
              checked={needsFollow}
              onChange={function (e) { toggleNeedsFollow(e.target.checked) }}
            />
            Needs you follows the view
          </label>
        </span>
      </div>
      {error ? <p className="cf-err">{error}</p> : null}
      <ReadNotifications />
    </div>
  )
}
