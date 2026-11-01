/* The face switcher, made to behave like a phone browser's toolbar.

   On the phone the window is the scroller and the switcher sits at the top of a log that can be
   thousands of pixels long, so reaching Terminal in a long conversation meant scrolling the
   whole thing back up. Now the strip leaves its slot the moment the slot goes off the top of
   the screen, floats at the top of the window, slides away when the reader moves down the log,
   and comes back on any move up. Near the top of the page it is always there.

   The slot keeps its height whatever the strip does, so nothing in the log moves when the strip
   floats. The log's own scroll rule is untouched: this reads the same window scroll and writes
   to it in one case only, below.

   ROOM ON THE WAY BACK
   A floating strip covers the top of the window, and when it came back on a move up it sat on
   the first line of the bubble under it. So when it comes back, the page steps up by the
   strip's height. The reader is moving up already, so the step goes the way the hand goes, and
   the line that was at the top edge lands just under the strip. Going away needs nothing: the
   reader is moving down, and what the strip covered is behind him.

   ONLY THE READER MOVES IT
   The log scrolls the window itself in three cases: arriving, coming back to the Chat tab, and
   a message the reader sent. The terminal shortens the page and the browser moves the reader
   with it. None of that is somebody scrolling, and a switcher that vanished because the page
   changed height would read as a fault, so the direction is only judged while the reader's own
   hand is on the page: a wheel, a finger, a pointer or a key in the last GRIP milliseconds.

   Above 720px this does nothing at all. There the log is its own scroller, the window does not
   move, and app.css already pins the strip above the log. */

import { useEffect, useRef, useState } from 'react'

const PHONE = '(max-width: 719px)'

/* A thumb never holds still. A few pixels up must still bring the strip back, and a few pixels
   down must not take it away, so the two thresholds are far apart. */
const UP = 8
const DOWN = 24

// This near the top of the page the strip is shown whatever the last direction was.
const TOP = 40

// How long a hand on the page counts for, in milliseconds. A scroll with no hand behind it is
// the page moving itself, and the strip stays as it is.
const GRIP = 300

// What says the reader is moving the page. Every one of them is passive and writes one number.
const HANDS = ['wheel', 'touchmove', 'pointermove', 'keydown']

/* `dock` is the slot the strip sits in. `face` is the tab on screen: changing it changes the
   height of the page, so the answer is worked out again.

   It returns two flags, which the screen turns into two class names: float, the strip is off
   the flow and over the log, and gone, the strip is slid up out of sight. */
export default function useFaces(dock, face) {
  const [shape, setShape] = useState({ float: false, gone: false })
  // What was last handed out, so a scroll that changes nothing costs no render.
  const shown = useRef(shape)

  useEffect(function () {
    const phone = window.matchMedia(PHONE)
    let last = window.scrollY
    /* The run in the present direction. A turn resets it, so the two thresholds measure one
       movement of the thumb rather than the whole journey down the log. */
    let moved = 0
    let frame = 0
    let grip = 0

    function hand() { grip = Date.now() }

    function put(float, gone) {
      if (shown.current.float === float && shown.current.gone === gone) { return }
      shown.current = { float, gone }
      setShape(shown.current)
    }

    function read() {
      frame = 0
      const el = dock.current
      if (!el || !phone.matches) { put(false, false); return }

      const y = window.scrollY
      const step = y - last
      last = y

      let gone = shown.current.gone
      if (Date.now() - grip < GRIP) {
        moved = (step > 0) === (moved > 0) ? moved + step : step
        if (moved <= -UP) { gone = false }
        if (moved >= DOWN) { gone = true }
      } else {
        // The page moved itself. The run means nothing now, and the strip stays as it is.
        moved = 0
      }

      /* The slot is still on screen, or the page is near its top. Either way the strip belongs
         where it was drawn, and a strip sitting in its own slot is never hidden. */
      const float = el.getBoundingClientRect().top < 0 && y > TOP
      if (float && shown.current.gone && !gone) {
        // The strip's height plus the gap it floats at. The step is read as a move up, so the
        // strip stays.
        window.scrollBy({ top: -(el.offsetHeight + 8), behavior: 'instant' })
      }
      put(float, float && gone)
    }

    function again() {
      if (frame) { return }
      frame = window.requestAnimationFrame(read)
    }

    read()
    window.addEventListener('scroll', again, { passive: true })
    window.addEventListener('resize', again, { passive: true })
    HANDS.forEach(function (name) { window.addEventListener(name, hand, { passive: true }) })
    return function () {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('scroll', again)
      window.removeEventListener('resize', again)
      HANDS.forEach(function (name) { window.removeEventListener(name, hand) })
    }
  }, [dock, face])

  return shape
}
