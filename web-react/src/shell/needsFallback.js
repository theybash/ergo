/* A stand in for src/screens/fleet/needsStore.js, which the Fleet agent owns and which was not
   on disk when the shell was wired. Same two exports, so the swap is one import line in
   App.jsx and one in TabBar.jsx:

     import { subscribe, getPending } from '../screens/fleet/needsStore'

   It reports nothing waiting, which is the honest answer from a store that holds nothing. */

export function subscribe() {
  // Nothing ever changes here, so the unsubscribe has nothing to undo.
  return function () {}
}

export function getPending() {
  return 0
}

export default { subscribe, getPending }
