/* Each screen and each tab keeps its own place on the page (from user feedback: "files are
   started from the bottom when tapped. Even feed").

   Under 720px every screen and every tab of a chat scrolls the one page, window.scrollY. So a
   list opened after the chat started where the chat had left the page: at its bottom. Here each
   one remembers where it was, by a key, and goes back there when it shows again. One never seen
   starts at the top, where the newest items are.

   The place comes back after the content has drawn. A list whose rows arrive a moment later is
   too short at first, and the browser would cut the place to the top, so it tries again as the
   page grows, for two seconds, and stops at once when his hand moves the page. Above 720px the
   page does not scroll, and none of this does anything. */

import { useLayoutEffect } from 'react'

const places = new Map()
const TRY_FOR = 2000

export default function useScrollMemory(key, active) {
  useLayoutEffect(function () {
    if (!active || !key) { return undefined }
    const want = places.get(key) || 0
    let done = false
    let seen = null
    const stop = function () { done = true; if (seen) { seen.disconnect() } }
    function place() {
      if (done) { return }
      window.scrollTo(0, want)
      if (Math.abs(window.scrollY - want) <= 2) { stop() }
    }
    place()
    if (!done && typeof ResizeObserver === 'function') {
      seen = new ResizeObserver(place)
      seen.observe(document.body)
    }
    const timer = window.setTimeout(stop, TRY_FOR)
    // Only his own scrolling is kept: while the place is still being put back, a short page moves
    // the scroll too, and that is not where he was.
    function save() { if (done) { places.set(key, window.scrollY) } }
    function hand() { stop() }
    window.addEventListener('scroll', save, { passive: true })
    window.addEventListener('touchstart', hand, { passive: true })
    window.addEventListener('wheel', hand, { passive: true })
    return function () {
      stop()
      window.clearTimeout(timer)
      window.removeEventListener('scroll', save)
      window.removeEventListener('touchstart', hand)
      window.removeEventListener('wheel', hand)
    }
  }, [key, active])
}
