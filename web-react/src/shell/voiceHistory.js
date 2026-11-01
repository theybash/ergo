/* The one history entry the voice overlay pushes, so the phone's back button closes the overlay
 * instead of leaving the app.
 *
 * Opening the overlay pushes an entry with a marker in its state and the same address, so
 * ?screen= and the #/a/<id> hash stay exactly as they were. Back pops that entry, and the overlay
 * hears it through onVoiceBack and minimises: the call keeps running under a bar, and the bar
 * pushes the entry again when it brings the overlay back. Any other close takes the entry off again, but only
 * when the entry in front is still ours, so a close never walks the owner back past his own page.
 *
 * The entry is a layer in shell/nav.js, so it waits in line behind a back that has not landed
 * yet, and the screens and agents the owner walked through stay behind it.
 */

import { openLayer, closeLayer, isLayer, VOICE_MARK } from './nav'

const hearers = new Set()

/* True when this history state is the one the overlay pushed. */
export function isVoiceEntry(state) {
  return isLayer(state, VOICE_MARK)
}

/* Push the entry. */
export function pushVoiceEntry() {
  openLayer(VOICE_MARK, function () {
    hearers.forEach(function (fn) { fn() })
  })
}

/* Take the entry off again after a close that was not the back button. */
export function dropVoiceEntry() {
  closeLayer(VOICE_MARK)
}

/* Hear back take the entry off. Returns the way to stop hearing. */
export function onVoiceBack(fn) {
  hearers.add(fn)
  return function () { hearers.delete(fn) }
}
