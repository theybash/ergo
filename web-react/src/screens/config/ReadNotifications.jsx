/* Read notifications: whether Ergo may pass the phone's notifications to the laptop, and the
   way to the system setting that turns it on. Drawn only inside the app; a browser has nothing
   to read. The state is asked again whenever the page comes back, since the setting lives in
   Android's own screen. */

import { useEffect, useState } from 'react'
import { askApp, inApp } from '../../shell/native'

export default function ReadNotifications() {
  const [on, setOn] = useState(null)

  useEffect(function () {
    let live = true
    if (!inApp()) { return function () { live = false } }
    function ask() {
      askApp('getNotificationAccess').then(function (r) {
        if (live && r && typeof r.on === 'boolean') { setOn(r.on) }
      })
    }
    ask()
    function back() { if (document.visibilityState === 'visible') { ask() } }
    document.addEventListener('visibilitychange', back)
    return function () {
      live = false
      document.removeEventListener('visibilitychange', back)
    }
  }, [])

  if (on === null) { return null }

  return (
    <div className="cf-meter">
      <p className="cf-meter-line cf-vm-line">
        <span className="cf-meter-label">Read notifications</span>
        <span className="cf-meter-rest">{on ? 'On' : 'Off'}</span>
        <span>
          <button
            type="button"
            className="btn sm"
            onClick={function () { askApp('openNotificationAccess') }}
          >
            {on ? 'Settings' : 'Turn on'}
          </button>
        </span>
      </p>
    </div>
  )
}
