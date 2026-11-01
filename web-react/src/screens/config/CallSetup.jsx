/* What a full the front desk call needs from the phone, read from the phone itself, with a button to each
   switch that is off. Drawn only inside the app; a browser has no phone settings to read. It reads
   again whenever the page comes back into view, so turning a switch on shows here at once. */

import { useEffect, useState } from 'react'
import { askApp, inApp } from '../../shell/native'

/* The switches no app can flip for itself on Oppo, OnePlus and Realme phones. Signal and Telegram
   have no code for these either; they walk people through them, and so does this. */
const COLOROS = [
  'Auto start-up: on for Ergo',
  'Battery usage: allow background activity, or Unrestricted',
  'Lock Ergo in recent apps (pull it down in the recents view)',
  'Floating windows / display over other apps: on',
  'Notifications, the front desk calls: Banner and Lock screen on',
  'Notifications, Alarms: Banner and Lock screen on',
]

const ROWS = [
  { id: 'notifications', label: 'Notifications for Ergo', fix: 'Turn on' },
  { id: 'fullScreen', label: 'Full-screen calls over the lock screen', fix: 'Allow' },
  { id: 'channelOn', label: 'The front desk calls: banner and sound', fix: 'Open' },
  { id: 'lockScreen', label: 'The front desk calls: shown on the lock screen', fix: 'Open' },
  { id: 'alarmBanner', label: 'Alarms: banner over what you are doing', fix: 'Open' },
  { id: 'alarmLock', label: 'Alarms: shown on the lock screen', fix: 'Open' },
  { id: 'battery', label: 'Battery: unrestricted, so calls reach a sleeping phone', fix: 'Allow' },
]

export default function CallSetup() {
  const [state, setState] = useState(null)

  useEffect(function () {
    if (!inApp()) { return undefined }
    let live = true
    function read() { askApp('getCallSetup').then(function (r) { if (live && r) { setState(r) } }) }
    read()
    function back() { if (document.visibilityState === 'visible') { read() } }
    document.addEventListener('visibilitychange', back)
    return function () { live = false; document.removeEventListener('visibilitychange', back) }
  }, [])

  if (!state) { return null }

  function telecom(on) {
    askApp('setTelecom', { on: on }).then(function (r) { if (r) { setState(function (st) { return { ...st, telecom: r.on } }) } })
  }
  const oppo = /oppo|oneplus|realme/.test(state.maker || '')
  const off = ROWS.filter(function (r) { return state[r.id] === false })
  return (
    <div className="cf-logins">
      <p className="cf-note">{off.length ? 'The front desk and alarms can reach you, but these keep them from ringing like a real call:' : 'Everything is set. The front desk and alarms ring like a real call.'}</p>
      {ROWS.map(function (r) {
        const ok = state[r.id] !== false
        return (
          <div className="cf-login" key={r.id}>
            <span className="cf-login-what">{r.label}</span>
            <span className="cf-login-site">{ok ? 'on' : 'off'}</span>
            {ok ? null : <button type="button" className="chip" onClick={function () { askApp('openCallSetting', { which: r.id }) }}>{r.fix}</button>}
          </div>
        )
      })}
      <label className="cf-check" style={{ marginTop: 8 }}>
        <input type="checkbox" checked={state.telecom !== false} onChange={function (e) { telecom(e.target.checked) }} />
        Use Android's phone system: answer from earphones, hold for other calls. Turn off if calls misbehave.
      </label>
      {oppo ? (
        <div style={{ marginTop: 10 }}>
          <p className="cf-note">On this phone, ColorOS also needs these, from Ergo's app settings:</p>
          <ul className="cf-note" style={{ margin: '6px 0 8px 18px', padding: 0 }}>
            {COLOROS.map(function (t) { return <li key={t}>{t}</li> })}
          </ul>
          <button type="button" className="chip" onClick={function () { askApp('openCallSetting', { which: 'app' }) }}>Open Ergo's app settings</button>
        </div>
      ) : null}
    </div>
  )
}
