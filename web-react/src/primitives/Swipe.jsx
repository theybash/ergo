/* A right swipe means done (from user feedback: "Right swipe generally should mean done").
   The row follows the finger; past a third of its width, or 110 px, it slides off and `onDone`
   runs. Short of that it springs back. Only a finger swipes: a mouse at the desk keeps its clicks
   and text selection. The page still scrolls up and down, because only a mostly sideways drag is
   taken. A tap that ends a swipe does not also open the row.

   A left swipe means drop (from user feedback: "This is not done, this is circumstances
   changed"). With `onDrop`, the row follows the finger left too; past the same mark it springs
   back and `onDrop` runs, which asks first. The row stays until he says yes. */

import { useRef, useState } from 'react'

export default function Swipe({ onDone, onDrop, label = 'Done', dropLabel = 'Drop', className = '', children }) {
  const [dx, setDx] = useState(0)
  const [gone, setGone] = useState(false)
  const [held, setHeld] = useState(false)
  const at = useRef(null)
  const swiped = useRef(false)
  const box = useRef(null)
  // The offset as last set. A lift can come before React has drawn the last move, so the lift
  // reads this, not the state.
  const off = useRef(0)
  function slide(x) { off.current = x; setDx(x) }
  // One function for the window, the same on every render, so it can be taken off again.
  const latest = useRef(null)
  const lift = useRef(function () { latest.current() })

  /* The lift is heard on the window too. On 25 October a row stayed half swiped: the lift never
     reached the row, because the list redrew under the finger and the capture was lost. */
  function down(e) {
    if (e.pointerType === 'mouse' || (!onDone && !onDrop)) { return }
    at.current = { x: e.clientX, y: e.clientY, side: null, id: e.pointerId }
    setHeld(true)
    swiped.current = false
    window.addEventListener('pointerup', lift.current)
    window.addEventListener('pointercancel', lift.current)
  }
  function move(e) {
    const a = at.current
    if (!a || a.id !== e.pointerId) { return }
    const x = e.clientX - a.x
    const y = e.clientY - a.y
    if (!a.side && Math.hypot(x, y) > 10) {
      a.side = (x > 0 || onDrop) && Math.abs(x) > Math.abs(y) * 1.5 ? 'x' : 'y'
      if (a.side === 'x') { try { box.current.setPointerCapture(e.pointerId) } catch { /* moves still come */ } }
    }
    if (a.side === 'x') { swiped.current = true; slide(onDrop ? x : Math.max(0, x)) }
    if (a.side === 'y') { at.current = null }
  }
  function up() {
    window.removeEventListener('pointerup', lift.current)
    window.removeEventListener('pointercancel', lift.current)
    const a = at.current
    if (!a) { return }
    at.current = null
    setHeld(false)
    if (a.side !== 'x') { if (off.current) { slide(0) } return }
    const dx = off.current
    const width = box.current ? box.current.offsetWidth : 300
    if (onDrop && -dx > Math.min(110, width / 3)) {
      slide(0)
      onDrop()
    } else if (dx > Math.min(110, width / 3)) {
      setGone(true)
      slide(width)
      window.setTimeout(function () {
        try { onDone() } finally { setGone(false); slide(0) }
      }, 180)
    } else {
      slide(0)
    }
  }

  latest.current = up

  return (
    <div className={'sw ' + className} ref={box}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}
      onClickCapture={function (e) { if (swiped.current) { e.stopPropagation(); e.preventDefault(); swiped.current = false } }}>
      {dx > 0 ? <div className="sw-under" aria-hidden="true">{label}</div> : null}
      {dx < 0 ? <div className="sw-under sw-drop" aria-hidden="true">{dropLabel}</div> : null}
      <div className={'sw-top' + (held ? '' : ' sw-settle') + (gone ? ' sw-gone' : '')}
        style={dx ? { transform: 'translateX(' + dx + 'px)' } : undefined}>
        {children}
      </div>
    </div>
  )
}
