/* The Android app, when the console runs inside it. In a browser every function here does
   nothing, so the console behaves exactly as before.

   The page tells the app three things through the app's one plugin, CCNative: a call started,
   a call ended, and the call is muted or paused. The Config page also reads and sets the
   bubble wheel's theme there. The app tells the page what its bubble or its
   notification asked for, as a `ccnative` window event: open a call, show the typed box, go to
   a screen, mute, pause, hang up. */

function bridge() {
  const cap = window.Capacitor
  if (!cap || !cap.isNativePlatform || !cap.isNativePlatform()) { return null }
  const headers = cap.PluginHeaders || []
  return headers.some(function (p) { return p.name === 'CCNative' }) ? cap : null
}

/* True inside the Android app. */
export function inApp() {
  return !!bridge()
}

/* One word to the app. It never throws and never waits: the call must not depend on it. */
export function tellApp(method, data) {
  const cap = bridge()
  if (!cap) { return }
  try {
    cap.nativePromise('CCNative', method, data || {}).catch(function () {})
  } catch {
    // An app too old to know the method. The console carries on.
  }
}

/* One question to the app, and its answer. Null in a browser, from an app too old to know the
   method, or when the app does not answer. It never throws. */
export async function askApp(method, data) {
  const cap = bridge()
  if (!cap) { return null }
  try {
    return await cap.nativePromise('CCNative', method, data || {})
  } catch {
    return null
  }
}

/* Hear what the app asks for. The flag tells the app that a console is up to hear it, so it
   loads a deep link only when nothing is. Returns the way to stop hearing. */
export function onApp(fn) {
  function heard(e) { fn(e.detail || {}) }
  window.addEventListener('ccnative', heard)
  window.ccNativeReady = true
  return function () { window.removeEventListener('ccnative', heard) }
}
