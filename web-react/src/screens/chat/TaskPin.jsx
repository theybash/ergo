/* The task quoted above the message box, the way WhatsApp quotes a message you reply to: a
   green bar, whose task it is, and its title. The message he sends next carries it, then it goes.

   A tap opens it in full: the whole title, where it came from, and the result if there is one.
   A long press, or a right click on the laptop, offers the same two things as a menu: read it
   in full, or remove it. The x removes it too.

   The scroll buttons are pinned above the message box on the phone, so the quote tells the page
   its height and they lift by that much rather than covering it. */

import { useEffect, useRef, useState } from 'react'
import useTasks from '../../data/tasksStore'
import useTaskPin, { unpin } from '../../data/taskPin'
import useBackClose from '../../shell/useBackClose'
import '../fleet/fleet.css'

function Menu({ onRead, onRemove, onClose }) {
  useBackClose(true, onClose)
  return (
    <div className="as-scrim" onClick={onClose}>
      <div className="as-sheet" role="menu" aria-label="Quoted task"
        onClick={function (e) { e.stopPropagation() }}>
        <button type="button" role="menuitem" className="as-item" onClick={onRead}>Read in full</button>
        <button type="button" role="menuitem" className="as-item as-danger" onClick={onRemove}>Remove</button>
      </div>
    </div>
  )
}

export default function TaskPin({ agent }) {
  const pin = useTaskPin()
  const store = useTasks()
  const [full, setFull] = useState(false)
  const [menu, setMenu] = useState(false)
  const box = useRef(null)
  const hold = useRef(null)
  const held = useRef(false)
  const shown = !!pin && pin.to === agent

  // A new quote opens short.
  const pinId = shown ? pin.task.id : ''
  useEffect(function () { setFull(false) }, [pinId])

  useEffect(function () {
    const root = document.documentElement
    if (!shown || !box.current) { root.style.removeProperty('--quote-h'); return undefined }
    const el = box.current
    const size = new ResizeObserver(function () {
      root.style.setProperty('--quote-h', (el.offsetHeight + 6) + 'px')
    })
    size.observe(el)
    return function () { size.disconnect(); root.style.removeProperty('--quote-h') }
  }, [shown, pinId])

  if (!shown) { return null }
  const task = (!pin.post && !pin.open && !pin.call && store.list.find(function (t) { return t.id === pin.task.id })) || pin.task
  const whose = pin.call ? 'The front desk\'s call'
    : pin.open ? (task.agent_name || task.agent) + ', open list'
    : pin.post ? (task.agent_name || task.agent) + ', feed post'
    : task.agent === 'owner' ? 'Your task' : (task.agent_name || 'Task')

  function end() {
    if (hold.current) { window.clearTimeout(hold.current.t); hold.current = null }
  }
  const press = {
    onPointerDown: function (e) {
      held.current = false
      const x = e.clientX, y = e.clientY
      hold.current = { x, y, t: window.setTimeout(function () { held.current = true; setMenu(true) }, 500) }
    },
    onPointerMove: function (e) {
      const h = hold.current
      if (h && Math.hypot(e.clientX - h.x, e.clientY - h.y) > 10) { end() }
    },
    onPointerUp: end,
    onPointerCancel: end,
    onContextMenu: function (e) { e.preventDefault(); end(); held.current = true; setMenu(true) },
    onClick: function () {
      if (held.current) { held.current = false; return }
      setFull(!full)
    },
  }

  return (
    <>
      <div className={'tq' + (full ? ' tq-full' : '')} ref={box} role="note" aria-label={pin.call ? 'Call you are replying about' : pin.open ? 'Open line you are replying about' : pin.post ? 'Post you are replying about' : 'Task you are replying about'}>
        <div className="tq-body" {...press}>
          <div className="tq-who">{whose}{pin.reopen ? ', taking it up again' : ''}</div>
          <div className="tq-what">{task.title}</div>
          {full && task.source && !pin.post && !pin.open && !pin.call ? <div className="tq-more">From {task.source}</div> : null}
          {full && task.result ? <div className="tq-more">Result: {task.result}</div> : null}
        </div>
        <button type="button" className="tq-x" aria-label="Remove the quoted task" onClick={unpin}>×</button>
      </div>
      {menu ? (
        <Menu
          onRead={function () { setMenu(false); setFull(true) }}
          onRemove={function () { setMenu(false); unpin() }}
          onClose={function () { setMenu(false) }} />
      ) : null}
    </>
  )
}
