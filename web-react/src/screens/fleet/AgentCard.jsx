/* One agent card, drawn from the daemon's own record.

   THE WHOLE CARD IS THE LINK. Tap it anywhere, or press Enter or Space on it, and the agent's
   chat opens. The owner asked for that, and it took the chip row and the button row with it:
   awake, the age, the port and Open all said again what the card already shows, and Sleep,
   Hide and Forget live in the long press menu and the kebab in the chat header.

   So a card holds three things: who the agent is and what it wants, the one line it last said,
   and its undelivered mail if it has any. An asleep agent is the dimmed card and the grey dot.
   Waking happens in the chat.

   Model, tokens and cost are not on the card either. The record still carries them and the
   daemon still counts them. A card says who the agent is and what it last said, and a bill is
   not that. */

import { useRef, useState } from 'react'
import Face from '../../primitives/Face'
import FaceStack from '../../primitives/FaceStack'
import ImageView from '../chat/ImageView'
import AgentSheet from './AgentSheet'
import { lastSaid, unread } from '../../data/when'
import { plain } from '../../data/plain'

/* The daemon has two states and no third. Green means an engine is alive, whichever engine it
   is. Reading `pid` instead would call a working agent asleep, because `pid` belongs to the
   terminal and is null while the chat engine runs. */
/* delegates: the agents this one made, drawn as a face stack at the end of the second line. A tap on
   the stack unfolds them under this row (onFold); a tap anywhere else opens this agent's chat. */
export default function AgentCard({ agent, onOpen, count = 0, onCount, onHeld, onPower, onSlept, powering = false,
  delegates = [], folded = false, onFold }) {
  const green = agent.state === 'green'
  const cls = 'agentcard fl-open' + (green ? '' : ' asleep') + (agent.parent ? ' delegate' : '')

  /* The last words, as the daemon cleaned them, with the markdown marks taken off for the
     preview. No quote marks are added. */
  const said = plain(agent.last_message) || 'Has not said anything yet.'

  /* A long press, or a right click on the laptop, opens the agent's options. The tap that ends a
     long press must not also open the chat, and a finger that moves is a scroll, not a press. */
  const [sheet, setSheet] = useState(false)
  const [picture, setPicture] = useState(null)
  const hold = useRef(null)
  const held = useRef(false)
  function holdStart(e) {
    held.current = false
    const x = e.clientX, y = e.clientY
    hold.current = { x, y, t: window.setTimeout(function () { held.current = true; setSheet(true) }, 500) }
  }
  function holdMove(e) {
    const h = hold.current
    if (h && Math.hypot(e.clientX - h.x, e.clientY - h.y) > 10) { holdEnd() }
  }
  function holdEnd() {
    if (hold.current) { window.clearTimeout(hold.current.t); hold.current = null }
  }
  function open(e) {
    if (held.current) { held.current = false; e.preventDefault(); return }
    onOpen()
  }

  /* A button inside a link. The press belongs to the button alone, or reading the mail would
     also open the chat behind the dialog. */
  function only(fn) {
    return function (e) {
      e.stopPropagation()
      fn()
    }
  }

  function press(e) {
    if (e.key !== 'Enter' && e.key !== ' ') { return }
    e.preventDefault()
    onOpen()
  }

  return (
    <>
    <article className={cls + (unread(agent) ? ' unread' : '')} role="link" tabIndex={0} onClick={open} onKeyDown={press}
      onPointerDown={holdStart} onPointerMove={holdMove} onPointerUp={holdEnd} onPointerCancel={holdEnd}
      onPointerLeave={holdEnd}
      onContextMenu={function (e) { e.preventDefault(); holdEnd(); setSheet(true) }}>
      <Face id={agent.id} name={agent.name} size={agent.parent ? 32 : 46} className="ir-face" aria-hidden="true" onOpen={setPicture} />
      <div className="ir-body">
        <div className="ir-top">
          <h3 className="ir-name">{agent.name}</h3>
          {(agent.views || []).includes('work') ? <span className="ir-tag">work</span> : null}
          {agent.archived ? <span className="ir-tag">archived</span> : null}
          {agent.working ? <span className="ac-spin" role="img" aria-label="Working"></span> : null}
          {agent.last_message_at ? <span className="ir-when">{lastSaid(agent.last_message_at)}</span> : null}
        </div>
        <div className="ir-bottom">
          <p className="ir-last">{said}</p>
          {delegates.length ? (
            <button type="button" className="ir-stack" onClick={only(onFold)} aria-expanded={folded}
              aria-label={(folded ? 'Fold ' : 'Unfold ') + delegates.length + (delegates.length === 1 ? ' delegate' : ' delegates')}>
              <FaceStack agents={delegates} />
              {delegates.filter(unread).length ? <span className="ir-sub-count">{delegates.filter(unread).length}</span> : null}
            </button>
          ) : null}
          {count > 0 ? (
            <button type="button" className="ir-count" onClick={only(onCount)}
              aria-label={count + (count === 1 ? ' needs you' : ' need you')}>{count}</button>
          ) : null}
          {onPower && !green && !agent.archived ? (
            <button type="button" className="ac-power" disabled={powering} aria-label={'Wake ' + agent.name}
              onClick={only(onPower)}>{powering ? '...' : 'Wake'}</button>
          ) : null}
        </div>
        {agent.waiting > 0 ? (
          <button type="button" className="fl-waiting-btn ir-waiting" onClick={only(onHeld)}
            aria-label={'Read the ' + agent.waiting + ' undelivered messages for ' + agent.name}>
            {agent.waiting + ' undelivered'}
          </button>
        ) : null}
      </div>
    </article>
    <ImageView src={picture} alt={agent.name} onClose={function () { setPicture(null) }} />
    {sheet ? <AgentSheet agent={agent} onSlept={onSlept} onClose={function () { setSheet(false) }} /> : null}
    </>
  )
}
