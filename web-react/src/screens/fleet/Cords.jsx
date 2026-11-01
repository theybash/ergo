/* The cord between two talking agents. The line is drawn from the cards themselves, so it is
   right in one column and right in three. Two cards in one row get an arc over their tops,
   two cards stacked get a straight drop down the gap. */

import { useLayoutEffect, useState } from 'react'

// How far the arc rises above the card tops.
const LIFT = 30

export default function Cords({ gridRef, cards, cord, redraw }) {
  const [shape, setShape] = useState(null)

  // Measured before paint, so the line is never seen in the wrong place. redraw changes when the
  // needs list above the grid grows or shrinks, which moves every card.
  useLayoutEffect(function () {
    function draw() {
      const grid = gridRef.current
      const a = cards.current[cord.from]
      const b = cards.current[cord.to]
      if (!grid || !a || !b) { return }

      const g = grid.getBoundingClientRect()
      if (!g.width || !g.height) { return }   // the fleet is hidden, there is nothing to measure

      function box(r) {
        return { l: r.left - g.left, t: r.top - g.top, r: r.right - g.left, b: r.bottom - g.top }
      }
      const A = box(a.getBoundingClientRect())
      const B = box(b.getBoundingClientRect())

      let x1, y1, x2, y2, d, mx, my
      const sideBySide = A.t < B.b && B.t < A.b

      if (sideBySide) {
        // Two cards in one row sit 12px apart, so the cord arcs over their tops where there is room.
        const left = A.l <= B.l ? A : B
        const right = A.l <= B.l ? B : A
        x1 = left.l + (left.r - left.l) * 0.72
        y1 = left.t
        x2 = right.l + (right.r - right.l) * 0.28
        y2 = right.t
        d = 'M' + x1 + ' ' + y1 + ' C' + x1 + ' ' + (y1 - LIFT) + ' ' + x2 + ' ' + (y2 - LIFT) + ' ' + x2 + ' ' + y2
        mx = (x1 + x2) / 2
        my = Math.min(y1, y2) - LIFT * 0.75
      } else {
        // One column, so the cord runs straight down the gap between them.
        const up = A.t <= B.t ? A : B
        const down = A.t <= B.t ? B : A
        y1 = up.b
        y2 = down.t
        x1 = (Math.max(A.l, B.l) + Math.min(A.r, B.r)) / 2
        x2 = x1
        d = 'M' + x1 + ' ' + y1 + ' L' + x2 + ' ' + y2
        mx = x1
        my = (y1 + y2) / 2
      }

      setShape({ d: d, x1: x1, y1: y1, x2: x2, y2: y2, mx: mx, my: my, w: cord.label.length * 5.6 + 14 })
    }

    draw()
    window.addEventListener('resize', draw)
    // The display font changes the card heights, so measure again once it lands.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(draw).catch(function () {})
    }
    return function () { window.removeEventListener('resize', draw) }
  }, [gridRef, cards, cord, redraw])

  return (
    <svg className="cords" id="cords" aria-hidden="true">
      {shape ? (
        <>
          <path className="cord" d={shape.d}></path>
          <circle className="cord-end" cx={shape.x1} cy={shape.y1} r="2.5"></circle>
          <circle className="cord-end" cx={shape.x2} cy={shape.y2} r="2.5"></circle>
          <rect
            className="cord-plate"
            x={shape.mx - shape.w / 2}
            y={shape.my - 9}
            width={shape.w}
            height="18"
            rx="4"
          ></rect>
          <text className="cord-label" x={shape.mx} y={shape.my + 0.5}>{cord.label}</text>
        </>
      ) : null}
    </svg>
  )
}
