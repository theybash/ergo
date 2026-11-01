/* The view, behind a small arrow beside the brand. All, Work, Demo, Personal. The owner asked for
   it out of sight: a filter for a passer by, not a control that sits on the page.

   Closes on a pick, on a tap outside, on Escape and on back. */
import { useEffect, useRef, useState } from 'react'
import { MENU_VIEWS, VIEW_LABELS } from './views'
import useBackClose from './useBackClose'

export default function ViewMenu({ view, onView }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)
  // Back closes the menu before it leaves the screen.
  useBackClose(open, function () { setOpen(false) })

  useEffect(function () {
    if (!open) { return undefined }
    function away(e) { if (box.current && !box.current.contains(e.target)) { setOpen(false) } }
    // preventDefault marks the Escape as taken, so the chat does not also read it as stop.
    function key(e) { if (e.key === 'Escape') { e.preventDefault(); setOpen(false) } }
    document.addEventListener('pointerdown', away)
    document.addEventListener('keydown', key)
    return function () {
      document.removeEventListener('pointerdown', away)
      document.removeEventListener('keydown', key)
    }
  }, [open])

  return (
    <div className="sh-viewmenu" ref={box}>
      <button
        type="button"
        className="sh-viewmenu-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={'View: ' + VIEW_LABELS[view]}
        onClick={function () { setOpen(!open) }}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6 9l6 6 6-6"></path>
        </svg>
      </button>
      {open ? (
        <ul className="sh-viewmenu-list" role="menu">
          {MENU_VIEWS.map(function (v) {
            return (
              <li key={v} role="none">
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={v === view}
                  className={'sh-viewmenu-item' + (v === view ? ' on' : '')}
                  onClick={function () { onView(v); setOpen(false) }}
                >
                  {VIEW_LABELS[v]}
                </button>
              </li>
            )
          })}
        </ul>
      ) : null}
    </div>
  )
}
