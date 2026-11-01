import { inApp, askApp } from './native'

/* The small chime when an agent finishes its turn (The owner, 26 Oct: "a cute message tone ... on
   phone and webapp"; he picked the kalimba from four). Config picks it. Inside the Android app
   the phone's own "Agent answered" notification plays the phone's choice, so the page stays quiet
   there and it never rings twice; in a browser the page plays this browser's choice. A browser
   plays sound only after the page has been touched once; before that it stays silent. */

export const CHIMES = [
  { id: 'kalimba', label: 'Kalimba' },
  { id: 'marimba', label: 'Marimba' },
  { id: 'drop', label: 'Water drop' },
  { id: 'bell', label: 'Glass bell' },
  { id: 'off', label: 'Off' },
]
const KEY = 'cc.chime'
const sounds = {}

function play(id) {
  if (id === 'off') return
  try {
    if (!sounds[id]) sounds[id] = new Audio('/sounds/chime-' + id + '.wav')
    sounds[id].currentTime = 0
    sounds[id].play().catch(function () {})
  } catch {
    // No audio here. The chat still shows the answer.
  }
}

function saved() {
  try { return localStorage.getItem(KEY) || 'kalimba' } catch { return 'kalimba' }
}

export function chime() {
  if (inApp()) return
  play(saved())
}

/* This device's choice: the phone's, inside the app; this browser's, outside it. */
export async function getChime() {
  if (inApp()) {
    const r = await askApp('getChime')
    return (r && r.chime) || null
  }
  return saved()
}

/* Pick one and hear it. */
export async function setChime(id) {
  play(id)
  if (inApp()) {
    const r = await askApp('setChime', { chime: id })
    return !!(r && r.chime === id)
  }
  try { localStorage.setItem(KEY, id) } catch { return false }
  return true
}
