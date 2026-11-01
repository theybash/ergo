/* Back goes back one level. The shell keeps real browser history, so the phone's back gesture
 * walks the owner back the way he came, and the Android app closes only when nothing is behind.
 *
 * TWO KINDS OF ENTRY
 * A step is a screen or an agent: the address changes (?screen= and #/a/<id>) and one entry is
 * pushed. A layer is a dialog, a menu or the voice overlay: the address stays, one entry with a
 * marker in its state is pushed, and back takes it off, which closes the layer.
 *
 * THE ADDRESS DECIDES
 * The screen and the agent are read from the address, never from the state. So a reload, a
 * pasted link and a back all land on what the address says.
 *
 * ONE BACK AT A TIME
 * A layer closed by a button takes its entry off with history.back(), and that finishes later.
 * A push asked for in that gap would land on top and then be the entry the back removes. So
 * every push, replace and drop waits in line until the back has landed. App's one popstate
 * handler calls popped(), which is how this file hears it land.
 */

const ROUTE = 'cc-route'
// Layer markers. The voice overlay's is fixed so voiceHistory.js keeps its name for it.
const VOICE = 'ccVoice'
const PREFIX = 'ccLayer:'

let backs = 0
let waitTimer = 0
const waiting = []
const wanted = new Set()
const stacked = new Set()
const goneFns = new Map()
let count = 0

function isMark(key) { return key === VOICE || key.indexOf(PREFIX) === 0 }

function state() {
  const s = window.history.state
  return s && typeof s === 'object' ? s : {}
}

/* The state with every layer marker taken out. A new entry never inherits a layer below it. */
function bare(s) {
  const out = {}
  Object.keys(s).forEach(function (k) { if (!isMark(k)) { out[k] = s[k] } })
  return out
}

function hasMark(s) { return Object.keys(s).some(isMark) }

function drain() {
  while (backs === 0 && waiting.length) { waiting.shift()() }
}

function inLine(fn) {
  waiting.push(fn)
  drain()
}

function stepBack() {
  backs += 1
  window.clearTimeout(waitTimer)
  // A back that never lands must not hold the line for ever.
  waitTimer = window.setTimeout(function () { backs = 0; drain() }, 800)
  try { window.history.back() } catch { backs = 0 }
}

function announce() { window.dispatchEvent(new Event(ROUTE)) }

/* Hear every change of address this file makes or lands on. Returns the way to stop. */
export function onRoute(fn) {
  window.addEventListener(ROUTE, fn)
  return function () { window.removeEventListener(ROUTE, fn) }
}

/* The screen the address names, or '' when it names none. */
export function screenInUrl() {
  return new URLSearchParams(window.location.search).get('screen') || ''
}

/* The screen a bare address opens. At a desk it is the chat, by his word of 16 October, and the
   Fleet is a tab away. On a phone, the app or a finger for a pointer, it stays the Fleet. */
const TOUCH = typeof window !== 'undefined' && (!!window.Capacitor
  || /Android|iPhone|iPad/i.test(navigator.userAgent || '')
  || (!!window.matchMedia && window.matchMedia('(pointer: coarse)').matches))
export const HOME = TOUCH ? 'fleet' : 'chat'

function urlFor(screen, hash) {
  const params = new URLSearchParams(window.location.search)
  if (screen === HOME) { params.delete('screen') } else { params.set('screen', screen) }
  const q = params.toString()
  const h = hash === undefined ? window.location.hash : hash
  return window.location.pathname + (q ? '?' + q : '') + (h && h !== '#' ? h : '')
}

/* One step. `hash` undefined keeps the hash, so coming back to Chat lands on the same agent.
 * `replace` is for a redirect or a correction, which is not a step the owner took. A step to
 * where he already is pushes nothing. */
export function go(screen, hash, replace) {
  inLine(function () {
    const url = urlFor(screen, hash)
    const here = window.location.pathname + window.location.search + window.location.hash
    try {
      if (replace) {
        window.history.replaceState(state(), '', url)
      } else if (url !== here) {
        window.history.pushState(bare(state()), '', url)
      }
    } catch {
      // A browser that refuses still has the screen switched by the announce below.
    }
    announce()
  })
}

/* Called by App's popstate handler, first thing. */
export function popped() {
  const s = state()
  // Layers whose entry is no longer the one in front were taken off by this back.
  stacked.forEach(function (mark) {
    if (!s[mark]) {
      stacked.delete(mark)
      if (wanted.has(mark)) {
        wanted.delete(mark)
        const fn = goneFns.get(mark)
        if (fn) { fn() }
      }
    }
  })
  if (backs > 0) {
    backs -= 1
    if (backs === 0) { window.clearTimeout(waitTimer) }
  }
  // An entry left by a layer that has closed since. Back over it at once, so one back is one
  // level; a forward onto it steps back too, which is the lesser surprise.
  if (hasMark(s) && !Object.keys(s).some(function (k) { return isMark(k) && stacked.has(k) })) {
    stepBack()
    return
  }
  announce()
  drain()
}

function pushMark(mark, onGone) {
  wanted.add(mark)
  if (onGone) { goneFns.set(mark, onGone) }
  inLine(function () {
    if (!wanted.has(mark)) { return }
    try {
      window.history.pushState({ ...bare(state()), [mark]: Date.now() }, '')
      stacked.add(mark)
    } catch {
      // No entry. The layer still has its own close.
    }
  })
}

function dropMark(mark) {
  wanted.delete(mark)
  goneFns.delete(mark)
  inLine(function () {
    if (!stacked.has(mark)) { return }
    // Only when it is the entry in front. Under another layer it stays, and back skips it later.
    if (state()[mark]) {
      stacked.delete(mark)
      stepBack()
    } else {
      stacked.delete(mark)
    }
  })
}

/* A new layer marker, unique to one dialog or menu. */
export function newLayer() {
  count += 1
  return PREFIX + count
}

/* Open a layer: one entry, and `onGone` is called when back takes it off. */
export function openLayer(mark, onGone) { pushMark(mark, onGone) }

/* Close a layer by any other way than back. Its entry goes, if it is still the one in front. */
export function closeLayer(mark) { dropMark(mark) }

/* True when this state is the entry the layer pushed. */
export function isLayer(s, mark) {
  return !!(s && typeof s === 'object' && s[mark])
}

export const VOICE_MARK = VOICE
