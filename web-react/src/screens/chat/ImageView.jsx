/* An image from a reply, full screen. Two fingers zoom up to five times, one finger moves it
   while zoomed, a double tap zooms in or back out. Back, the cross or a tap outside a fitted
   image closes it. The file is fetched from the outbox the same way the reply showed it. */

import { useRef, useState } from 'react'
import useBackClose from '../../shell/useBackClose'
import './imageview.css'

const FIT = { k: 1, x: 0, y: 0 }

export default function ImageView({ src, alt, onClose }) {
  const [z, setZ] = useState(FIT)
  const pts = useRef(new Map())
  const pinch = useRef(null)
  const drag = useRef(null)
  const lastTap = useRef(0)
  useBackClose(!!src, onClose)
  if (!src) { return null }

  function down(e) {
    e.currentTarget.setPointerCapture(e.pointerId)
    pts.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pts.current.size === 2) {
      const [p, q] = [...pts.current.values()]
      pinch.current = { d: Math.hypot(p.x - q.x, p.y - q.y), cx: (p.x + q.x) / 2, cy: (p.y + q.y) / 2, ...z }
      drag.current = null
      return
    }
    drag.current = { sx: e.clientX, sy: e.clientY, x: z.x, y: z.y, moved: false }
  }
  function move(e) {
    if (!pts.current.has(e.pointerId)) { return }
    pts.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    const pc = pinch.current
    if (pc && pts.current.size === 2) {
      const [p, q] = [...pts.current.values()]
      const k = Math.max(1, Math.min(5, pc.k * Math.hypot(p.x - q.x, p.y - q.y) / pc.d))
      // The point between the fingers stays under the fingers.
      const ox = pc.cx - window.innerWidth / 2, oy = pc.cy - window.innerHeight / 2
      setZ(k === 1 ? FIT : { k, x: ox - (ox - pc.x) * k / pc.k, y: oy - (oy - pc.y) * k / pc.k })
      return
    }
    const d = drag.current
    if (!d) { return }
    if (Math.hypot(e.clientX - d.sx, e.clientY - d.sy) > 8) { d.moved = true }
    if (z.k > 1) { setZ({ k: z.k, x: d.x + e.clientX - d.sx, y: d.y + e.clientY - d.sy }) }
  }
  function up(e) {
    pts.current.delete(e.pointerId)
    if (pts.current.size < 2) { pinch.current = null }
    const d = drag.current
    drag.current = null
    if (!d || d.moved) { return }
    const now = Date.now()
    if (now - lastTap.current < 300) {
      lastTap.current = 0
      if (z.k > 1) { setZ(FIT); return }
      const ox = e.clientX - window.innerWidth / 2, oy = e.clientY - window.innerHeight / 2
      setZ({ k: 2.5, x: -ox * 1.5, y: -oy * 1.5 })
      return
    }
    lastTap.current = now
    if (z.k === 1 && e.target === e.currentTarget) { onClose() }
  }

  return (
    <div className="imgview" role="dialog" aria-modal="true" aria-label={alt || 'Image'}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}>
      <img src={src} alt={alt} draggable="false"
        style={{ transform: `translate(${z.x}px, ${z.y}px) scale(${z.k})` }} />
      <button type="button" className="imgview-x" aria-label="Close"
        onPointerDown={function (e) { e.stopPropagation() }}
        onClick={onClose}>×</button>
    </div>
  )
}
