/* Status: one block for him, then one for each agent holding a task.

   His block holds his own tasks: added here, by an agent, or by an alarm with a label. The circle
   ticks one off. A tap on the words opens the task's page, whose Prompt opens the assistant with the task
   quoted above the message box. A long press picks another agent instead.

   An agent's block holds what it is doing and what it finished. A tap on any task opens its page:
   the whole text, and Yes, No and Prompt. A finished task waits for his Yes, which closes it, or
   his No, which opens the agent's chat with the task quoted so he can say what is missing.

   Done and dropped tasks are not drawn at all (from user feedback: they "cause a bunch of
   clutter").

   No count anywhere: nothing here adds to the red numeral on the Insights tab. */

import { useEffect, useRef, useState } from 'react'
import cc from '../../data/api'
import useFleet from '../../data/useFleet'
import useTasks from '../../data/tasksStore'
import { pinTask } from '../../data/taskPin'
import { showsIn } from '../../shell/views'
import { faceColour } from '../../data/faceColour'
import useBackClose from '../../shell/useBackClose'
import '../fleet/fleet.css'
import './board.css'
import '../feed/feed.css'
import Swipe from '../../primitives/Swipe'
import OpenFile from './OpenFile'

const ME = 'owner'
const HOME = 'personal-assistant'

const tick = (
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
  return 'by ' + at.toLocaleDateString([], { day: 'numeric', month: 'short' })
}

function minutes(since) {
  if (!since) { return '' }
  const m = Math.max(1, Math.round((Date.now() / 1000 - since) / 60))
  return m < 60 ? m + ' min' : Math.round(m / 60) + ' h'
}

/* A tap, or a long press that hands the row to `onHold`. A finger that moves is a scroll. */
function useHold(onTap, onHold) {
  const hold = useRef(null)
  const held = useRef(false)
  function end() {
    if (hold.current) { window.clearTimeout(hold.current.t); hold.current = null }
  }
  return {
    onPointerDown: function (e) {
      held.current = false
      if (!onHold) { return }
      const x = e.clientX, y = e.clientY
      hold.current = { x, y, t: window.setTimeout(function () { held.current = true; onHold() }, 500) }
    },
    onPointerMove: function (e) {
      const h = hold.current
      if (h && Math.hypot(e.clientX - h.x, e.clientY - h.y) > 10) { end() }
    },
    onPointerUp: end,
    onPointerCancel: end,
    onContextMenu: function (e) { if (onHold) { e.preventDefault(); end(); held.current = true; onHold() } },
    onClick: function () {
      if (held.current) { held.current = false; return }
      onTap()
    },
  }
}

/* The day a task was put on the list, so an old one looks old: "today", "yesterday", or "13 Oct". */
export function entered(t) {
  if (!t) { return '' }
  const at = new Date(t * 1000)
  const now = new Date()
  if (at.toDateString() === now.toDateString()) { return 'today' }
  if (at.toDateString() === new Date(now.getTime() - 86400000).toDateString()) { return 'yesterday' }
  return at.toLocaleDateString([], { day: 'numeric', month: 'short' })
}

/* A row's words: the title, cut at two lines, and under it one quiet line with when it is due
   and who it is from. The day it went on the list is on the task's own page, not here: three
   grey lines at the right of every row made the list hard to read. */
function Words({ task, onTap, onHold, extra, who }) {
  const press = useHold(onTap, onHold)
  const line = [extra, who].filter(Boolean).join(' \u00b7 ')
  return (
    <button type="button" className="bd-words" {...press}>
      <span className="bd-title">{task.title}</span>
      {line ? <span className="bd-meta">{line}</span> : null}
    </button>
  )
}

/* Which agent gets one of his own tasks, after a long press. */
function Pick({ task, agents, onPick, onClose }) {
  useBackClose(true, onClose)
  return (
    <div className="as-scrim" onClick={onClose}>
      <div className="as-sheet" role="menu" aria-label="Talk about it with"
        onClick={function (e) { e.stopPropagation() }}>
        <p className="as-name">Talk about it with</p>
        <p className="bd-pick-task">{task.title}</p>
        {agents.map(function (a) {
          return (
            <button key={a.id} type="button" role="menuitem" className="as-item"
              onClick={function () { onPick(a.id) }}>{a.name || a.id}</button>
          )
        })}
      </div>
    </div>
  )
}

/* Dropping one of his tasks asks first, in a sheet like the others (from user feedback: the
   toast that asked was "extremely ugly"). Keep is the easy thumb; Drop is red. */
function DropSheet({ task, owner, onKeep, onDrop }) {
  useBackClose(true, onKeep)
  return (
    <div className="as-scrim" onClick={onKeep}>
      <div className="as-sheet yl-sheet" role="alertdialog" aria-label="Drop this task"
        onClick={function (e) { e.stopPropagation() }}>
        <p className="as-name">Drop this task?</p>
        <p className="yl-sheet-task">{task.title}</p>
        <p className="yl-sheet-note">It is not done. It leaves your list{owner ? ', and ' + owner + ' hears of it' : ''}.</p>
        <div className="yl-sheet-btns">
          <button type="button" className="yl-btn" onClick={onKeep}>Keep</button>
          <button type="button" className="yl-btn yl-btn-red" onClick={onDrop}>Drop</button>
        </div>
      </div>
    </div>
  )
}

/* A task he types asks when, and who looks after it. The time is
   needed; the agent is not, and the assistant takes it when he picks none. */
function hhmm(d) { return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) }
function whenChoices() {
  const now = new Date()
  const at = function (days, h) { const d = new Date(now); d.setDate(d.getDate() + days); d.setHours(h, 0, 0, 0); return d }
  const out = [{ key: 'hour', label: 'In an hour', at: new Date(Math.ceil((now.getTime() + 3600e3) / 300e3) * 300e3) }]
  if (now.getHours() < 17) { out.push({ key: 'eve', label: 'This evening', at: at(0, 18) }) }
  else if (now.getHours() < 20) { out.push({ key: 'eve', label: 'Tonight', at: at(0, 21) }) }
  out.push({ key: 'morn', label: 'Tomorrow morning', at: at(1, 9) })
  if (now.getHours() >= 20) { out.push({ key: 'eve', label: 'Tomorrow evening', at: at(1, 18) }) }
  return out
}
function whenText(d) {
  const today = new Date().toDateString() === d.toDateString()
  return (today ? '' : d.toLocaleDateString([], { weekday: 'short' }) + ' ') + hhmm(d)
}
function localInput(d) {
  const p = function (n) { return String(n).padStart(2, '0') }
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes())
}

function AddSheet({ title, agents, onAdd, onClose }) {
  useBackClose(true, onClose)
  const choices = useRef(whenChoices()).current
  const [when, setWhen] = useState(null)
  const [own, setOwn] = useState(HOME)
  const [picked, setPicked] = useState('')
  const people = agents.filter(function (a) { return !a.parent && !a.hidden && !a.archived })
    .sort(function (a, b) { return (b.id === HOME) - (a.id === HOME) })
  const at = when === 'pick' ? (picked ? new Date(picked) : null)
    : (choices.find(function (c) { return c.key === when }) || {}).at
  function add() {
    if (!at || isNaN(at.getTime())) { return }
    onAdd(Math.round(at.getTime() / 1000), own === HOME ? null : own)
  }
  return (
    <div className="as-scrim" onClick={onClose}>
      <div className="as-sheet yl-sheet" role="dialog" aria-label="Add a task"
        onClick={function (e) { e.stopPropagation() }}>
        <p className="as-name">{title}</p>

        <p className="yl-sheet-label">When</p>
        <div className="yl-when">
          {choices.map(function (c) {
            return (
              <button key={c.key} type="button" className={'yl-tile' + (when === c.key ? ' on' : '')}
                onClick={function () { setWhen(c.key) }}>
                <span>{c.label}</span>
                <b>{whenText(c.at)}</b>
              </button>
            )
          })}
          <label className={'yl-tile' + (when === 'pick' ? ' on' : '')}>
            <span>Pick a time</span>
            <b>{when === 'pick' && at ? at.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' + hhmm(at) : 'Any day'}</b>
            <input type="datetime-local" className="yl-pick" value={picked}
              min={localInput(new Date())}
              onChange={function (e) { setPicked(e.target.value); setWhen('pick') }}
              onClick={function () { setWhen('pick') }} />
          </label>
        </div>

        <p className="yl-sheet-label">Who looks after it</p>
        <div className="yl-who-row">
          {people.map(function (a) {
            const on = own === a.id
            return (
              <button key={a.id} type="button" className={'yl-pal' + (on ? ' on' : '')} aria-pressed={on}
                onClick={function () { setOwn(a.id) }}>
                <span className="yl-pal-face" style={{ background: faceColour(a.id) }}>
                  {(a.name || a.id).charAt(0).toUpperCase()}
                </span>
                <span className="yl-pal-name">{a.name || a.id}</span>
              </button>
            )
          })}
        </div>

        <button type="button" className="yl-btn yl-btn-go" disabled={!at} onClick={add}>Add to your list</button>
      </div>
    </div>
  )
}

/* One task whole, over the screen: the full text and the buttons. For a task an agent finished,
   Yes closes it, No opens its chat with the task quoted to say what is missing, and Prompt opens
   the chat to talk about it. For his own, Done ticks it off. A task from the Done list can only
   be taken up again. */
function Page({ task, onClose, onYes, onNo, onPrompt, onTick, onBack }) {
  useBackClose(!!task, onClose)
  if (!task) { return null }
  const mine = task.agent === ME
  const finished = task.state === 'closed' || task.state === 'ticked'
  const plan = task.plan_state === 'ready' ? task.plan || [] : []
  return (
    <div className="toolview" role="dialog" aria-label="Task">
      <div className="toolview-bar">
        <span className="toolview-name">{task.state === 'owed' || task.kind === 'owed'
          ? 'Owed to you by ' + (task.who || 'someone')
          : mine ? 'Your task' : task.agent_name}</span>
        <button type="button" className="toolview-x" aria-label="Close" onClick={onClose}>×</button>
      </div>
      <div className="toolview-body bd-page">
        <h2 className="bd-page-title">{task.title}</h2>
        {task.due ? <p className="bd-page-line">Due {due(task.due)}</p> : null}
        {task.created ? <p className="bd-page-line">Added {entered(task.created)}</p> : null}
        {mine && task.by && task.by !== ME ? <p className="bd-page-line">Found by {task.by_name}</p> : null}
        {task.source ? <p className="bd-page-line">From {task.source}</p> : null}
        {task.deliverable ? <p className="bd-page-line">You get: {task.deliverable}</p> : null}
        {plan.length ? (
          <ol className="tk-steps">{plan.map(function (p, i) { return <li key={i}>{p}</li> })}</ol>
        ) : null}
        {task.result ? (
          <>
            <p className="toolview-head">Result</p>
            <p className="bd-page-result">{task.result}</p>
          </>
        ) : null}
        {task.result_file ? (
          <OpenFile agent={task.agent} name={task.result_file} />
        ) : null}
        <div className="bd-page-acts">
          {task.state === 'done' ? (
            <>
              <button type="button" className="tk-b tk-yes" onClick={onYes}>Yes</button>
              <button type="button" className="tk-b tk-no" onClick={onNo}>No</button>
            </>
          ) : null}
          {mine && task.state === 'mine'
            ? <button type="button" className="tk-b tk-yes" onClick={onTick}>Done</button>
            : null}
          {task.state === 'owed'
            ? <button type="button" className="tk-b tk-yes" onClick={onTick}>Got it</button>
            : null}
          {/* A tick or a Yes pressed by mistake comes straight back, with no message to anyone. */}
          {finished ? <button type="button" className="tk-b tk-yes" onClick={onBack}>Bring it back</button> : null}
          <button type="button" className="tk-b" onClick={onPrompt}>{finished ? 'Take it up again' : 'Prompt'}</button>
        </div>
      </div>
    </div>
  )
}

function Avatar({ name, me, working }) {
  return (
    <span className={'bd-av' + (me ? ' bd-me' : '')}>
      {working ? <span className="bd-turn" aria-hidden="true"></span> : null}
      {(name || '?').charAt(0).toUpperCase()}
    </span>
  )
}

/* -- his list: Today and Not today --------------------------------
   His tasks and what people owe him, in one list split by the clock. Today is everything late
   and everything due before midnight; Not today groups the rest by day, then Later and No date, all open.
   Two tabs, so opening the app shows only today. Done and dropped tasks are never drawn: a ticked
   row slides away. An owed row wears the face of the person who owes it. */

const DAY = 86400

function startOfDay(at) {
  const d = new Date(at * 1000)
  d.setHours(0, 0, 0, 0)
  return d.getTime() / 1000
}

function clock(t) {
  return new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

function lateBy(t, now) {
  const s = now - t
  if (s < 3600) { return Math.max(1, Math.round(s / 60)) + 'm' }
  if (s < DAY) { return Math.round(s / 3600) + 'h' }
  return Math.floor(s / DAY) + 'd'
}

function Row({ t, now, onTick, onDrop, onOpen, onHold, leaving }) {
  const press = useHold(function () { onOpen(t) }, onHold ? function () { onHold(t) } : null)
  const owed = t.state === 'owed'
  const late = t.due && t.due < now
  const who = owed ? (t.who || 'Someone') + ' owes you' : (t.by && t.by !== ME ? t.by_name : '')
  return (
    <Swipe onDone={function () { onTick(t) }} onDrop={function () { onDrop(t) }}>
      <div className={'yl-row' + (leaving ? ' yl-leaving' : '')}>
        {owed ? (
          <button type="button" className="yl-face" aria-label={'Got ' + t.title}
            style={{ background: faceColour(t.who || '?') }}
            onClick={function () { onTick(t) }}>{(t.who || '?').trim().slice(0, 1).toUpperCase()}</button>
        ) : (
          <button type="button" className="yl-check" aria-label={'Tick off ' + t.title}
            onClick={function () { onTick(t) }}></button>
        )}
        <button type="button" className="yl-words" {...press}>
          <span className="yl-title">{t.title}</span>
          {who ? <span className="yl-who">{who}</span> : null}
        </button>
        {t.due ? <span className={'yl-time' + (late ? ' late' : '')}>{late ? lateBy(t.due, now)
          : t.due >= startOfDay(now) + 7 * DAY ? new Date(t.due * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' })
          : clock(t.due)}</span> : null}
      </div>
    </Swipe>
  )
}

function YourList({ items, onTick, onDrop, onOpen, onHold, onAdd }) {
  const [tab, setTab] = useState('today')
  const [adding, setAdding] = useState('')
  const [gone, setGone] = useState({})
  const now = Date.now() / 1000
  const today0 = startOfDay(now)
  const tomorrow0 = today0 + DAY
  const live = items.filter(function (t) { return !gone[t.id] || gone[t.id] === 'leaving' })
  const late = live.filter(function (t) { return t.due && t.due < now })
  const todays = live.filter(function (t) { return t.due && t.due >= now && t.due < tomorrow0 })
  const later = live.filter(function (t) { return !t.due || t.due >= tomorrow0 })
  const byTime = function (a, b) { return (a.due || 9e12) - (b.due || 9e12) || a.created - b.created }

  // A ticked row fades and folds where it is, then goes; nothing under it jumps.
  function tick(t) {
    setGone(function (g) { return { ...g, [t.id]: 'leaving' } })
    window.setTimeout(function () { setGone(function (g) { return { ...g, [t.id]: 'gone' } }) }, 260)
    onTick(t, function () { setGone(function (g) { const n = { ...g }; delete n[t.id]; return n }) })
  }
  function row(t) {
    return <Row key={t.id} t={t} now={now} onTick={tick} onDrop={onDrop} onOpen={onOpen}
      onHold={t.state === 'owed' ? null : onHold} leaving={gone[t.id] === 'leaving'} />
  }

  // Not today: the next six days by name, then Later and No date, which fold.
  const days = []
  for (let i = 1; i < 7; i++) {
    const from = today0 + i * DAY
    const rows = later.filter(function (t) { return t.due && t.due >= from && t.due < from + DAY }).sort(byTime)
    if (rows.length) {
      const d = new Date(from * 1000)
      days.push({ key: 'd' + i, rows,
        name: i === 1 ? 'Tomorrow' : d.toLocaleDateString([], { weekday: 'long' }),
        sub: i === 1 ? d.toLocaleDateString([], { weekday: 'short', day: 'numeric' }) : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) })
    }
  }
  const far = later.filter(function (t) { return t.due && t.due >= today0 + 7 * DAY }).sort(byTime)
  const undated = later.filter(function (t) { return !t.due }).sort(function (a, b) { return b.created - a.created })

  function add(e) {
    e.preventDefault()
    const title = adding.trim()
    if (!title) { return }
    onAdd(title, function () { setAdding('') })  // the sheet asks when and who
  }

  const todayCount = late.length + todays.length
  return (
    <div className="yl">
      <div className="yl-head">
        <h3 className="yl-h">Your list</h3>
        <p className="yl-date">{new Date().toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' })}</p>
      </div>
      <div className="yl-seg" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'today'} className={tab === 'today' ? 'on' : ''}
          onClick={function () { setTab('today') }}>
          Today{todayCount ? <i className={late.length ? 'red' : ''}>{todayCount}</i> : null}
        </button>
        <button type="button" role="tab" aria-selected={tab === 'later'} className={tab === 'later' ? 'on' : ''}
          onClick={function () { setTab('later') }}>
          Not today{later.length ? <i>{later.length}</i> : null}
        </button>
      </div>

      {tab === 'today' ? (
        <>
          {late.length ? (
            <div className="yl-card">
              <div className="yl-card-h late"><span>Late</span><span>{late.length}</span></div>
              {late.sort(byTime).map(row)}
            </div>
          ) : null}
          <div className="yl-card">
            <div className="yl-card-h"><span>Today</span>{todays.length ? <span>{todays.length}</span> : null}</div>
            {todays.sort(byTime).map(row)}
            {!todays.length && !late.length ? <p className="yl-none">Nothing for today.</p> : null}
            <form className="yl-row yl-add" onSubmit={add}>
              <span className="yl-check yl-dash"></span>
              <input className="yl-input" value={adding} placeholder="Add a task" aria-label="Add a task of yours"
                onChange={function (e) { setAdding(e.target.value) }} />
            </form>
          </div>
        </>
      ) : (
        <>
          {days.map(function (d) {
            return (
              <div key={d.key}>
                <div className="yl-day"><b>{d.name}</b><span>{d.sub}</span></div>
                <div className="yl-card">{d.rows.map(row)}</div>
              </div>
            )
          })}
          {/* Later and No date stay open, like the days (from user feedback: "keep it expanded"). */}
          {[['far', 'Later', far], ['undated', 'No date', undated]].map(function (f) {
            if (!f[2].length) { return null }
            return (
              <div key={f[0]}>
                <div className="yl-day"><b>{f[1]}</b></div>
                <div className="yl-card">{f[2].map(row)}</div>
              </div>
            )
          })}
          {!later.length ? <p className="yl-none yl-none-out">Nothing after today.</p> : null}
        </>
      )}
    </div>
  )
}

export default function Board({ view, onScreen }) {
  const store = useTasks()
  const fleet = useFleet()
  const [picking, setPicking] = useState(null)
  const [said, setSaid] = useState('')
  const [page, setPage] = useState(null)
  const [toast, setToast] = useState(null)
  const [dropping, setDropping] = useState(null)
  const [naming, setNaming] = useState(null)

  const agents = fleet.agents.filter(function (a) { return showsIn(a, view) })
  const byId = new Map(fleet.agents.map(function (a) { return [a.id, a] }))
  const shown = store.list.filter(function (t) {
    if (t.agent === ME) { return true }
    const a = byId.get(t.agent)
    return !a || showsIn(a, view)
  })
  // His own tasks and what people owe him: one list, split by the clock in YourList.
  const mine = shown.filter(function (t) {
    return t.agent === ME && (t.state === 'mine' || t.state === 'owed')
  })
  const theirs = shown.filter(function (t) { return t.agent !== ME && (t.state === 'yes' || t.state === 'done') })
  const holders = []
  for (const t of theirs) { if (!holders.includes(t.agent)) { holders.push(t.agent) } }

  function open(task, to, reopen) {
    pinTask(task, to, reopen)
    onScreen('chat', '#/a/' + to)
  }

  async function run(fn) {
    setSaid('')
    try { await fn() } catch (e) { setSaid(e.message) }
  }

  /* A tick or a right swipe, with Undo for a few seconds. `back` puts the row back on screen. */
  function swipeTick(t, back) {
    run(async function () {
      try { await cc.taskTick(t.id) } catch (e) { if (back) { back() } throw e }
      setToast({ text: 'Done', undo: function () {
        run(async function () { await cc.taskReopen(t.id); if (back) { back() } })
      } })
    })
  }
  /* A left swipe drops it: not done, things changed. It asks first, and stays until he answers.
     Its owner agent hears of the drop (owners.py). */
  function swipeDrop(t) { setDropping(t) }
  useEffect(function () {
    if (!toast) { return undefined }
    const t = window.setTimeout(function () { setToast(null) }, 4000)
    return function () { window.clearTimeout(t) }
  }, [toast])

  function add(title, clear) { setNaming({ title, clear }) }

  return (
    <section className="section bd" aria-label="Your list">
      {said ? <p className="note" role="status">{said}</p> : null}

      <YourList items={mine} onTick={swipeTick} onDrop={swipeDrop} onOpen={setPage}
        onHold={setPicking} onAdd={add} />

      {holders.length ? (
        <div className="section-head yl-agents-h">
          <h3 id="bd-h">Agents at work</h3>
        </div>
      ) : null}

      {holders.map(function (agent) {
        const a = byId.get(agent) || { id: agent, name: agent }
        const doing = theirs.filter(function (t) { return t.agent === agent && t.state === 'yes' })
        const done = theirs.filter(function (t) { return t.agent === agent && t.state === 'done' })
        const state = a.working
          ? 'working' + (doing.length ? ', ' + minutes(doing[0].updated) : '')
          : [doing.length ? doing.length + ' doing' : '', done.length ? done.length + ' done, waiting on your tick' : '']
            .filter(Boolean).join(', ')
        return (
          <div className="bd-block" key={agent}>
            <div className="bd-head">
              <Avatar name={a.name || agent} working={a.working} />
              <div>
                <div className="bd-name">{a.name || agent}</div>
                <div className="bd-state">{state}</div>
              </div>
            </div>
            {doing.map(function (t) {
              return (
                <div className="bd-row" key={t.id}>
                  <span className="bd-c"></span>
                  <Words task={t} onTap={function () { setPage(t) }} />
                </div>
              )
            })}
            {done.map(function (t) {
              return (
                <div className="bd-row" key={t.id}>
                  <span className="bd-c bd-ok">{tick}</span>
                  <Words task={t} onTap={function () { setPage(t) }} />
                </div>
              )
            })}
          </div>
        )
      })}

      <Page task={page}
        onClose={function () { setPage(null) }}
        onYes={function () { const t = page; setPage(null); run(function () { return cc.taskClose(t.id) }) }}
        onNo={function () { const t = page; setPage(null); open(t, t.agent, true) }}
        onTick={function () {
          const t = page
          setPage(null)
          run(async function () { await cc.taskTick(t.id) })
        }}
        onBack={function () {
          const t = page
          setPage(null)
          run(async function () { await cc.taskReopen(t.id) })
        }}
        onPrompt={function () {
          const t = page
          setPage(null)
          const finished = t.state === 'closed' || t.state === 'ticked'
          const to = t.agent !== ME ? t.agent : (t.owner || HOME)
          open(t, to, finished)
        }} />

      <div className={'fd-toast' + (toast ? ' on' : '')} role="status">
        {toast ? toast.text : ''}
        {toast && toast.undo ? (
          <button type="button" onClick={function () { toast.undo(); setToast(null) }}>Undo</button>
        ) : null}
      </div>

      {dropping ? (
        <DropSheet task={dropping}
          owner={dropping.state === 'owed' ? null : (byId.get(dropping.owner || HOME) || {}).name}
          onKeep={function () { setDropping(null) }}
          onDrop={function () {
            const t = dropping
            setDropping(null)
            run(async function () { await cc.taskDismiss(t.id) })
          }} />
      ) : null}

      {naming ? (
        <AddSheet title={naming.title} agents={agents}
          onClose={function () { setNaming(null) }}
          onAdd={function (when, owner) {
            const n = naming
            setNaming(null)
            run(async function () { await cc.addMyTask(n.title, when, owner); n.clear() })
          }} />
      ) : null}

      {picking ? (
        <Pick task={picking} agents={agents}
          onPick={function (to) { const t = picking; setPicking(null); open(t, to) }}
          onClose={function () { setPicking(null) }} />
      ) : null}
    </section>
  )
}
