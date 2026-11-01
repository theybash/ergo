/* The orb, wrapped for React. It mounts a canvas, hands it to the renderer in orb.js, and
   feeds it the voice level. The canvas is a picture, so it is hidden from a screen reader and
   the button around it carries the name. */

import { useEffect, useRef } from 'react'
import { Orb } from './orb'
import { onVoiceLevel } from './level'

export default function WireOrb({ className, level }) {
  const canvas = useRef(null)
  const fixed = typeof level === 'number'

  useEffect(function () {
    const el = canvas.current
    if (!el) { return }
    const view = Orb(el)
    /* A fixed level is set once. Otherwise the orb follows the voice for as long as it lives. */
    if (fixed) {
      view.setLevel(level)
      return function () { view.destroy() }
    }
    const off = onVoiceLevel(function (v) { view.setLevel(v) })
    return function () { off(); view.destroy() }
  }, [fixed, level])

  return <canvas className={className} ref={canvas} aria-hidden="true"></canvas>
}
