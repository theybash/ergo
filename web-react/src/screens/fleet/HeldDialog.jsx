/* The messages you left for an agent while it slept, still undelivered.

   They are not in the agent record: the card carries only the count. The text comes from
   GET /held when the dialog opens. Deleting one is undone by typing it again. Deleting the lot
   is not, so that one asks first. Send delivers one now, waking the agent if it sleeps: a
   message held because the terminal was busy otherwise waits for a start that may never come. */

import { useCallback, useEffect, useRef, useState } from 'react'
import useBackClose from '../../shell/useBackClose'
import cc from '../../data/api'
import { ago } from './format'

export default function HeldDialog({ agent, onClose, onChanged }) {
  const box = useRef(null)
  // Back closes the dialog and leaves you on the Fleet.
  useBackClose(true, onClose)
  const [rows, setRows] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(true)

  const load = useCallback(async function () {
    setBusy(true)
    try {
      const r = await cc.held(agent.id)
      setRows(r.held || [])
      setError('')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }, [agent.id])

  useEffect(function () {
    const el = box.current
    if (el && !el.open) { el.showModal() }
    /* oxlint-disable-next-line react/set-state-in-effect -- the mailbox is an external system
     * and this is the read that opens the dialog. */
    load()
  }, [load])

  async function drop(id) {
    try {
      await cc.unhold(agent.id, id)
      await load()
      if (onChanged) { onChanged() }
    } catch (e) { setError(e.message) }
  }

  async function send(id) {
    setError('')
    try {
      const r = await cc.sendHeld(agent.id, id)
      if (r.outcome === 'held') { setError('The terminal is still busy, so it is held again.') }
      await load()
      if (onChanged) { onChanged() }
    } catch (e) { setError(e.message) }
  }

  async function dropAll() {
    if (!window.confirm('Delete every undelivered message for this agent?')) { return }
    try {
      await cc.unhold(agent.id)
      await load()
      if (onChanged) { onChanged() }
    } catch (e) { setError(e.message) }
  }

  return (
    <dialog className="fl-dlg" ref={box} onCancel={onClose} aria-labelledby="fl-held-h">
      <div className="fl-dlg-top">
        <h2 id="fl-held-h">{'Undelivered to ' + agent.name}</h2>
        <button type="button" className="btn sm quiet" onClick={onClose} aria-label="Close this dialog">Close</button>
      </div>

      <p className="fl-note">
        These wait in the mailbox. The agent reads them the next time it wakes, or now if you
        tap Send.
      </p>
      {error ? <p className="fl-err">{error}</p> : null}
      {busy ? <p className="fl-note">Reading the mailbox.</p> : null}
      {!busy && !rows.length ? <p className="fl-note">Nothing is waiting.</p> : null}

      <ul className="fl-held">
        {rows.map(function (row) {
          return (
            <li key={row.id} className="fl-held-row">
              <span className="fl-held-text">{row.text}</span>
              <span className="needs-age">{ago(row.ts)}</span>
              <button
                type="button"
                className="btn sm"
                onClick={function () { send(row.id) }}
                aria-label="Send this message now"
              >
                Send
              </button>
              <button
                type="button"
                className="btn sm quiet"
                onClick={function () { drop(row.id) }}
                aria-label="Delete this undelivered message"
              >
                Delete
              </button>
            </li>
          )
        })}
      </ul>

      <div className="fl-act">
        {rows.length > 1 ? (
          <button type="button" className="btn danger" onClick={dropAll}>Delete them all</button>
        ) : null}
        <button type="button" className="btn" onClick={onClose}>Done</button>
      </div>
    </dialog>
  )
}
