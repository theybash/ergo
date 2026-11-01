/* One voice level for every orb on the page. The overlay writes it from the microphone many
   times a second, so it must not go through React state: a re-render per frame would cost more
   than the drawing does. Each orb subscribes once and reads the number straight from here. */

let value = 0
const listeners = new Set()

export function setVoiceLevel(v) {
  value = Math.max(0, Math.min(1, Number(v) || 0))
  listeners.forEach(function (fn) { fn(value) })
}

export function voiceLevel() {
  return value
}

/* Returns the unsubscribe, which the caller runs when its orb goes away. */
export function onVoiceLevel(fn) {
  listeners.add(fn)
  fn(value)
  return function () { listeners.delete(fn) }
}
