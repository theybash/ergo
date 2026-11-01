/* Back after a long time away, the page starts over (from user feedback: the app froze
   after a long time closed). Android freezes a backgrounded app, and the stream and sockets it held
   are dead when it wakes, with nothing to say so: the page shows old content and a tap waits on a
   dead connection forever. A reload makes new ones. The Android app does the same from its side,
   which also covers a page too stuck to run this.

   A call comes first. A tapped Answer comes back as this same visibility change, with the app's
   ask to open the call a moment later. The reload waits a moment for that ask, and when it comes
   the reload waits until the call is over: the call needs none of the page's dead connections,
   only fresh requests and its own audio link. Opening the call on the reloaded page would hold
   the call screen back 2 to 3 seconds. */

export const AWAY_MS = 10 * 60 * 1000
const SETTLE_MS = 800
// Long enough for the hang up and the last transcript lines to reach the daemon.
const AFTER_CALL_MS = 1500

let hiddenAt = 0
let timer = 0
let cover = null
// The page came back after a long time away and has not started over yet.
let stale = false

function showCover() {
  if (cover) { return }
  // Until the fresh page draws, the old one would look ready and ignore taps, so a plain cover says what is going on and takes the taps.
  cover = document.createElement('div')
  cover.textContent = 'Reloading'
  cover.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;' +
    'justify-content:center;background:var(--bg,#0f0e0d);color:var(--text-faint,#78716c);font-size:14px'
  document.body.appendChild(cover)
}

function hold() {
  window.clearTimeout(timer)
  timer = 0
  if (cover) { cover.remove(); cover = null }
  window.ccReloadDue = false
}

function startOver(wait) {
  window.ccReloadDue = true
  showCover()
  window.clearTimeout(timer)
  timer = window.setTimeout(function () {
    timer = 0
    // A call that started in the meantime keeps the page; it starts over when the call ends.
    if (window.ccCallLive) { hold(); return }
    stale = false
    window.location.reload()
  }, wait)
}

export function installAwayReload() {
  document.addEventListener('visibilitychange', function () {
    // A call live as the page left keeps it running, so that time away does not count.
    if (document.visibilityState === 'hidden') { hiddenAt = window.ccCallLive ? 0 : Date.now(); return }
    if (hiddenAt && Date.now() - hiddenAt > AWAY_MS) { stale = true }
    hiddenAt = 0
    if (stale && !window.ccCallLive) { startOver(SETTLE_MS) }
  })
}

/* The app asked for a call. A reload that waits for it is put off until the call is over. */
export function callAsked() {
  if (timer) { hold() }
}

/* The call screen closed. A page that came back stale starts over now, or on its next return. */
export function callOver() {
  if (stale && document.visibilityState === 'visible') { startOver(AFTER_CALL_MS) }
}
