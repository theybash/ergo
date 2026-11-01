/* A few of his tasks, picked by the agent, drawn from the live task list. The card holds only
   ids, so a task ticked on Insights shows ticked here too.

   Tick and move call the task routes directly. The agent gets no message and takes no turn. A
   tick pressed by mistake comes back with Undo, the way it does on Insights. Move offers three
   times: in three hours, tomorrow at nine, next Monday at nine. Only his own open tasks move;
   a task someone owes him can be ticked, and an agent's task is shown with no buttons. */

import { useState } from 'react'
import cc from '../../data/api'
import useTasks, { merge } from '../../data/tasksStore'

const tickMark = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M5 12l5 5 9-10"></path>
  </svg>
)

function due(t) {
  if (!t) { return '' }
  const at = new Date(t * 1000)
  const now = new Date()
  const hm = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  if (at.toDateString() === now.toDateString()) { return hm }
  const days = (at - now) / 86400000
  if (days > 0 && days < 6) { return at.toLocaleDateString([], { weekday: 'short' }) + ' ' + hm }
  return at.toLocaleDateString([], { day: 'numeric', month: 'short' })
}

/* The three times Move offers, in seconds. */
function times() {
  const now = new Date()
  const later = new Date(now.getTime() + 3 * 3600000)
  later.setMinutes(0, 0, 0)
  const tomorrow = new Date(now)
  tomorrow.setDate(now.getDate() + 1)
  tomorrow.setHours(9, 0, 0, 0)
  const monday = new Date(now)
  monday.setDate(now.getDate() + (((8 - now.getDay()) % 7) || 7))
  monday.setHours(9, 0, 0, 0)
  return [['Later', later], ['Tomorrow', tomorrow], ['Next week', monday]]
    .map(function ([word, d]) { return [word, d.getTime() / 1000] })
}

function Row({ task }) {
  const [moving, setMoving] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const ticked = task.state === 'ticked'
  const canTick = task.state === 'mine' || task.state === 'owed'

  async function act(fn) {
    if (busy) { return }
    setBusy(true)
    setError('')
    try {
      merge(await fn())
      setMoving(false)
    } catch (e) {
      setError(e.message || 'That did not work.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="sw-task">
      <div className="sw-task-line">
        {canTick || ticked
          ? (
            <button type="button" className={ticked ? 'sw-tick sw-ticked' : 'sw-tick'} disabled={busy || ticked}
              aria-label={ticked ? 'Done' : 'Tick off ' + task.title}
              onClick={function () { act(function () { return cc.taskTick(task.id) }) }}>
              {ticked ? tickMark : null}
            </button>
          )
          : <span className="sw-tick sw-none" aria-hidden="true"></span>}
        <span className={ticked ? 'sw-task-title sw-dim' : 'sw-task-title'}>{task.title}</span>
        {ticked
          ? (
            <button type="button" className="sw-word" disabled={busy}
              onClick={function () { act(function () { return cc.taskReopen(task.id) }) }}>Undo</button>
          )
          : null}
        {!ticked && task.due ? <span className="sw-due">{due(task.due)}</span> : null}
        {!ticked && task.state === 'mine'
          ? (
            <button type="button" className="sw-word" aria-expanded={moving} disabled={busy}
              onClick={function () { setMoving(!moving) }}>Move</button>
          )
          : null}
      </div>
      {moving
        ? (
          <div className="sw-moves">
            {times().map(function ([word, at]) {
              return (
                <button type="button" className="btn sm quiet" key={word} disabled={busy}
                  onClick={function () { act(function () { return cc.taskMove(task.id, at) }) }}>{word}</button>
              )
            })}
          </div>
        )
        : null}
      {error ? <p className="sw-error">{error}</p> : null}
    </li>
  )
}

export default function TasksCard({ data }) {
  const store = useTasks()
  // The last copy seen of each task. The list drops a ticked task, and this card should still
  // show it ticked, with its Undo.
  const [seen, setSeen] = useState({})
  const ids = (Array.isArray(data.ids) ? data.ids : []).map(String)
  const rows = ids.map(function (id) {
    return store.list.find(function (t) { return t.id === id }) || null
  })

  const known = {}
  ids.forEach(function (id, i) {
    known[id] = rows[i] || (seen[id] ? { ...seen[id], state: 'ticked' } : null)
  })
  const fresh = ids.filter(function (id, i) { return rows[i] && seen[id] !== rows[i] })
  if (fresh.length) {
    setSeen(function (was) {
      const next = { ...was }
      ids.forEach(function (id, i) { if (rows[i]) { next[id] = rows[i] } })
      return next
    })
  }

  const shown = ids.map(function (id) { return known[id] }).filter(Boolean)
  return (
    <section className="sw-card" aria-label={String(data.title || 'Tasks')}>
      {data.title ? <p className="sw-q">{String(data.title)}</p> : null}
      {shown.length
        ? <ul className="sw-tasks">{shown.map(function (t) { return <Row key={t.id} task={t} /> })}</ul>
        : <p className="sw-none-left">{store.loaded ? 'None of these is open now.' : ''}</p>}
    </section>
  )
}
