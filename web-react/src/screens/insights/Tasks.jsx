/* Tasks: what the agents offer to take off the owner's plate, one card each.

   An offer is a title, where it came from, and what he gets. He answers on the card: Yes sends
   it to the agent, No tells the agent without waking it, Plan asks for a few steps first, and
   Prompt opens the agent's chat with the card pinned on top. The small x takes a card off the
   list and tells the agent nothing.

   Three groups: what waits on him, what an agent is working on, then what is answered, fading.
   The cards come from the task store, which the stream keeps current, so nothing here fetches
   on a timer. The view hides the cards of agents it hides, the same rule as the fleet. */

import { entered } from './Board'
import { useState } from 'react'
import cc from '../../data/api'
import useFleet from '../../data/useFleet'
import useTasks from '../../data/tasksStore'
import { pinTask } from '../../data/taskPin'
import { showsIn } from '../../shell/views'
import './tasks.css'
import OpenFile from './OpenFile'

const REASONS = ['Not mine', 'Already done', 'Too risky', 'Not now']

/* 14:05 today, "yesterday 18:10", or "Thu 18:10" within the week, else "2 Oct". */
function when(seconds) {
  if (!seconds) { return '' }
  const at = new Date(seconds * 1000)
  const now = new Date()
  const hm = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const then = new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime()
  const days = Math.round((day - then) / 86400000)
  if (days <= 0) { return hm }
  if (days === 1) { return 'yesterday ' + hm }
  if (days < 7) { return at.toLocaleDateString([], { weekday: 'short' }) + ' ' + hm }
  return at.toLocaleDateString([], { day: 'numeric', month: 'short' })
}

function Spin() {
  return <span className="tk-spin" aria-hidden="true"></span>
}

function Plan({ steps }) {
  const [open, setOpen] = useState(true)
  if (!steps || !steps.length) { return null }
  return (
    <div className="tk-plan">
      <button
        type="button"
        className="tk-plan-toggle"
        aria-expanded={open}
        onClick={function () { setOpen(!open) }}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M9 6l6 6-6 6"></path>
        </svg>
        Plan, {steps.length} steps
      </button>
      {open ? (
        <ol className="tk-steps">
          {steps.map(function (s, i) { return <li key={i}>{s}</li> })}
        </ol>
      ) : null}
    </div>
  )
}

/* The story of a card so far, oldest first: when it was offered, its plan, and every word he
   wrote through Prompt and the agent's answer (from user feedback: "I have no idea what this
   is. ... it records our end-to-end summary"). Folded to one line; a tap opens it. */
function Story({ task, name }) {
  const [open, setOpen] = useState(false)
  const thread = task.thread || []
  const rows = [{ at: task.created, who: 'note', text: name + ' offered this' }]
  if (task.plan_state === 'ready') { rows.push({ who: 'note', text: 'The plan came back, ' + task.plan.length + ' steps' }) }
  for (const t of thread) { rows.push(t) }
  const last = thread[thread.length - 1]
  const now = task.state === 'talking'
    ? (last && last.who === 'agent' ? name + ' answered. Yes starts it, or write back' : 'Waiting for ' + name + ' to answer')
    : null
  return (
    <div className="tk-plan tk-story">
      <button type="button" className="tk-plan-toggle" aria-expanded={open}
        onClick={function () { setOpen(!open) }}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M9 6l6 6-6 6"></path>
        </svg>
        {now || 'So far'}
      </button>
      {open ? (
        <ol className="tk-story-rows">
          {rows.map(function (r, i) {
            return (
              <li key={i} className={'tk-said ' + r.who}>
                <span className="tk-said-who">
                  {r.who === 'owner' ? 'You' : r.who === 'agent' ? name : ''}
                  {r.at ? <span className="tk-said-at">{when(r.at)}</span> : null}
                </span>
                <span className="tk-said-text">{r.text}</span>
              </li>
            )
          })}
          {task.state === 'talking' && !thread.length ? (
            <li className="tk-said note"><span className="tk-said-text">What you and {name} said before 1 November was not kept on the card. It is in the chat</span></li>
          ) : null}
        </ol>
      ) : null}
    </div>
  )
}

/* Write to the agent from the card itself. The words go with the card quoted, the same as
   Prompt in the chat, so the card keeps them and moves on when the agent answers. */
function Reply({ task, name, onDone }) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  async function go(e) {
    e.preventDefault()
    const words = text.trim()
    if (!words) { return }
    setSending(true); setError('')
    try {
      await cc.send(task.agent, '[About task ' + task.id + ': ' + task.title + ']\n\n' + words, true)
      setText('')
      onDone()
    } catch (err) { setError(err.message || 'That did not go through.') }
    setSending(false)
  }
  return (
    <form className="tk-reply" onSubmit={go}>
      <textarea className="tk-reply-box" rows={2} value={text} placeholder={'Write to ' + name}
        aria-label={'Write to ' + name + ' about this card'}
        onChange={function (e) { setText(e.target.value) }} />
      <div className="tk-acts">
        <button type="submit" className="tk-b tk-yes" disabled={sending || !text.trim()}>Send</button>
        <button type="button" className="tk-b" onClick={onDone}>Cancel</button>
      </div>
      {error ? <p className="tk-line">{error}</p> : null}
    </form>
  )
}

function Card({ task, agent, onScreen }) {
  const [writing, setWriting] = useState(false)
  const [busy, setBusy] = useState(null)        // 'yes' or 'plan' while the agent is working
  const [pending, setPending] = useState(false)
  const [said, setSaid] = useState('')
  const name = task.agent_name || task.agent
  const offered = task.state === 'offered' || task.state === 'waiting_remind' || task.state === 'talking'
  const reminding = task.state === 'waiting_remind' || task.remind === 'plan'

  async function run(fn) {
    setPending(true)
    setSaid('')
    try {
      const r = await fn()
      return r
    } catch (e) {
      setSaid(e.message || 'That did not go through.')
      return null
    } finally {
      setPending(false)
    }
  }

  async function answer(which, mode) {
    const r = await run(function () {
      return which === 'yes' ? cc.taskYes(task.id, mode) : cc.taskPlan(task.id, mode)
    })
    if (r && r.busy) { setBusy(which); return }
    if (r) { setBusy(null) }
  }

  // Prompt writes from the card. "Open chat" in the box still goes to the whole conversation.
  function prompt() {
    setWriting(true)
  }

  function openChat() {
    pinTask(task)
    onScreen('chat', '#/a/' + task.agent)
  }

  const yesBtn = (
    <button type="button" className="tk-b tk-yes" disabled={pending}
      onClick={function () { answer('yes') }}>Yes</button>
  )
  const noBtn = (
    <button type="button" className="tk-b tk-no" disabled={pending}
      onClick={function () { run(function () { return cc.taskNo(task.id) }) }}>No</button>
  )
  const planBtn = (
    <button type="button" className="tk-b" disabled={pending}
      onClick={function () { answer('plan') }}>Plan</button>
  )
  const promptBtn = (
    <button type="button" className="tk-b" onClick={prompt}>Prompt</button>
  )

  let body = null
  if (offered && busy) {
    body = (
      <div className="tk-busy">
        <p className="tk-line">{name} is working on something right now.</p>
        <div className="tk-acts">
          <button type="button" className="tk-b" disabled={pending}
            onClick={function () { answer(busy, 'now') }}>Steer it now</button>
          <button type="button" className="tk-b" disabled={pending}
            onClick={function () { answer(busy, 'remind') }}>Remind me</button>
          <button type="button" className="tk-x tk-x-inline" aria-label="Back"
            onClick={function () { setBusy(null) }}>×</button>
        </div>
      </div>
    )
  } else if (offered) {
    const planning = task.plan_state === 'asked'
    const ready = task.plan_state === 'ready'
    body = (
      <>
        {planning ? <p className="tk-line"><Spin />Planning...</p> : null}
        {ready ? <Plan steps={task.plan} /> : null}
        {reminding ? <p className="tk-line">You'll get a reminder when {name} is free</p> : null}
        {task.state === 'talking' || (task.thread && task.thread.length) ? <Story task={task} name={name} /> : null}
        {writing ? <Reply task={task} name={name} onDone={function () { setWriting(false) }} /> : (
          <div className="tk-acts">
            {yesBtn}
            {noBtn}
            {planning || ready || task.remind === 'plan' ? null : planBtn}
            {promptBtn}
          </div>
        )}
        {writing ? <button type="button" className="tk-open-chat" onClick={openChat}>Open the chat with {name}</button> : null}
      </>
    )
  } else if (task.state === 'yes') {
    body = (
      <>
        <p className="tk-line"><Spin /><span className="tk-ok">Yes</span> · {name} is on it</p>
        {task.plan_state === 'ready' ? <Plan steps={task.plan} /> : null}
      </>
    )
  } else if (task.state === 'no') {
    body = task.why ? (
      <p className="tk-line"><span className="tk-bad">No</span> · {task.why}</p>
    ) : (
      <>
        <p className="tk-line"><span className="tk-bad">No</span></p>
        <p className="tk-ask">“Okay. Why not, so I offer better next time?”</p>
        <div className="tk-quick">
          {REASONS.map(function (r) {
            return (
              <button type="button" key={r} className="tk-q" disabled={pending}
                onClick={function () { run(function () { return cc.taskWhy(task.id, r) }) }}>
                {r}
              </button>
            )
          })}
        </div>
      </>
    )
  } else if (task.state === 'done') {
    body = (
      <>
        <p className="tk-line"><span className="tk-ok">Done</span> · {when(task.updated)}</p>
        {task.result ? <p className="tk-result">{task.result}</p> : null}
        {task.result_file ? <OpenFile agent={task.agent} name={task.result_file} /> : null}
      </>
    )
  }

  const faded = task.state === 'done' || (task.state === 'no' && task.why)

  return (
    <article className={'tk-card' + (faded ? ' tk-faded' : '')}>
      {offered ? (
        <button type="button" className="tk-x" aria-label="Take this card off the list"
          disabled={pending}
          onClick={function () { run(function () { return cc.taskDismiss(task.id) }) }}>×</button>
      ) : null}
      <div className="tk-who">
        <span className={'dot ' + (agent && agent.state === 'green' ? 'up' : 'sleep')}></span>{name}
      </div>
      <h4 className="tk-what">{task.title}</h4>
      {offered ? (
        <>
          <p className="tk-from">From {task.source}{task.created ? ', offered ' + entered(task.created) : ''}</p>
          <p className="tk-get">You get: {task.deliverable}</p>
        </>
      ) : null}
      {body}
      {said ? <p className="tk-said" role="status">{said}</p> : null}
    </article>
  )
}

function Group({ label, tasks, byId, onScreen }) {
  if (!tasks.length) { return null }
  return (
    <div className="tk-group">
      <h4 className="tk-label">{label}</h4>
      {tasks.map(function (t) {
        return <Card key={t.id} task={t} agent={byId.get(t.agent)} onScreen={onScreen} />
      })}
    </div>
  )
}

export default function Tasks({ view, onScreen }) {
  const store = useTasks()
  const fleet = useFleet()
  const byId = new Map(fleet.agents.map(function (a) { return [a.id, a] }))
  // An agent the console does not know yet shows everywhere, as an untagged agent does.
  const mine = store.list.filter(function (t) {
    if (t.agent === 'owner') { return false }
    const a = byId.get(t.agent)
    return !a || showsIn(a, view)
  })
  const waiting = mine.filter(function (t) { return t.state === 'offered' || t.state === 'waiting_remind' })
  // Answered in words through Prompt. Out of Waiting; Jev moves it on once the agent replies.
  const talking = mine.filter(function (t) { return t.state === 'talking' })
  // What an agent is doing or finished lives on the board above, "Who is doing what".
  const no = mine.filter(function (t) { return t.state === 'no' })

  // No live offer, no section: an empty one eats the first fold. The
  // ones he said no to alone do not bring it back.
  if (!store.error && !waiting.length && !talking.length) return null

  return (
    <section className="section tk" aria-labelledby="tasks-h">
      <div className="section-head">
        <h3 id="tasks-h">Agents offered</h3>
      </div>
      {store.error ? <p className="note">The tasks did not load: {store.error}</p> : null}
      <Group label="Waiting on you" tasks={waiting} byId={byId} onScreen={onScreen} />
      <Group label="In discussion" tasks={talking} byId={byId} onScreen={onScreen} />
      {/* Offers he turned down pile up, so they fold into one closed section that scrolls
          inside itself. */}
      {no.length ? (
        <details className="tk-answered">
          <summary className="tk-label">Said no <span className="tk-count">{no.length}</span></summary>
          <div className="tk-answered-box">
            <Group label="Said no" tasks={no} byId={byId} onScreen={onScreen} />
          </div>
        </details>
      ) : null}
    </section>
  )
}
