// The return-after-a-long-time reload, with a fake page: an Answer opens the call at once.
import assert from 'node:assert/strict'

let now = 1_000_000
const timers = new Map(); let nextTimer = 1
const listeners = {}
const body = { kids: [], appendChild(n) { this.kids.push(n) } }
globalThis.document = {
  visibilityState: 'visible', body,
  addEventListener(name, fn) { listeners[name] = fn },
  createElement() { return { style: {}, remove() { body.kids = body.kids.filter((k) => k !== this) } } },
}
let reloads = 0
globalThis.window = {
  setTimeout(fn, ms) { const id = nextTimer++; timers.set(id, { fn, at: now + ms }); return id },
  clearTimeout(id) { timers.delete(id) },
  location: { reload() { reloads++ } },
}
Date.now = () => now
function pass(ms) {
  now += ms
  for (const [id, t] of [...timers]) { if (t.at <= now) { timers.delete(id); t.fn() } }
}
function show(state) { document.visibilityState = state; listeners.visibilitychange() }

const away = await import('../web-react/src/shell/awayReload.js')
away.installAwayReload()

// A plain return after eleven minutes: the cover, then the reload.
show('hidden'); pass(11 * 60 * 1000); show('visible')
assert.equal(body.kids.length, 1, 'the Reloading cover shows')
pass(800)
assert.equal(reloads, 1, 'a plain return reloads')

// An Answer after eleven minutes: the call opens on this page, with no cover and no reload.
body.kids = []
show('hidden'); pass(11 * 60 * 1000); show('visible')
pass(100)
away.callAsked()
window.ccCallLive = true
assert.equal(body.kids.length, 0, 'the cover goes as the call opens')
pass(5 * 60 * 1000)
assert.equal(reloads, 1, 'no reload during the call')

// The call ends: the stale page starts over after a moment.
window.ccCallLive = false
away.callOver()
assert.equal(body.kids.length, 1, 'the cover shows after the call')
pass(1500)
assert.equal(reloads, 2, 'the page starts over after the call')
console.log('ok, answer')

// A long call with the app in the background kept the page alive: no reload after it.
window.ccCallLive = true
show('hidden'); pass(20 * 60 * 1000); show('visible')
window.ccCallLive = false
away.callOver()
pass(2000)
assert.equal(reloads, 2, 'a page a call kept alive does not start over')
console.log('ok, kept alive')
