/* The messages the owner did not type, each drawn as the thing it is about (from user feedback:
   "for each of these cards, it should become a different element of ui"). kinds.js reads a
   message into parts; a part with `el` is drawn here.

     feed      his reply to a feed post: the whole post, then his words
     task      the live task, with its buttons (TasksCard)
     quote     what his words answer, small above them: a call, an OPEN.md line, a choice
     answer    his Yes, Plan or No on an offer, one line
     report    a task report: what changed, on which task
     job       a job's result: its name, the outcome as a word, what it found
     askcheck  an ask check, one dim line
     agent     another agent speaking: its face and name, its words on the page
     cord      a call between agents
     call      his call with this agent
     wa        a WhatsApp message
     event     one small line: a failed alarm, an email sent, a background task

   Long ones fold, with Show more (The owner: "some elements need to be contractible if long"). The
   feed post never folds ("feed reply can be full"), nor do buttons he acts on. Quiet chrome, his
   taste rules: no pills, no halos, no gradients, agent words on the page, only his in bubbles. */

import { useLayoutEffect, useRef, useState } from 'react'
import Face from '../../primitives/Face'
import { snapshot } from '../../data/fleetStore'
import useTasks from '../../data/tasksStore'
import { go } from '../../shell/nav'
import Prose from './Prose'
import TasksCard from './TasksCard'
import './elements.css'

/* An agent by its id or its name, as the daemon wrote it. */
function agentOf(who) {
  const w = String(who || '').trim().toLowerCase()
  const list = (snapshot() || {}).agents || []
  return list.find(function (a) { return a.id === w || String(a.name || '').toLowerCase() === w }) || null
}

/* Text cut at some lines, with Show more when the cut hides anything. Measured once drawn. */
export function Fold({ lines, children, className }) {
  const box = useRef(null)
  const [open, setOpen] = useState(false)
  const [long, setLong] = useState(false)
  useLayoutEffect(function () {
    const el = box.current
    /* oxlint-disable-next-line react/set-state-in-effect -- the cut is known only once drawn. */
    if (el && !open) { setLong(el.scrollHeight > el.clientHeight + 1) }
  }, [open, children])
  return (
    <>
      <div ref={box} className={'el-fold' + (className ? ' ' + className : '')}
        style={open ? null : { '--fold-lines': lines }} data-open={open ? 'true' : 'false'}>
        {children}
      </div>
      {long || open
        ? <button type="button" className="el-more" onClick={function () { setOpen(!open) }}>{open ? 'Show less' : 'Show more'}</button>
        : null}
    </>
  )
}

const ICONS = {
  phone: <path d="M6.6 3.5h2.6l1.4 4-1.9 1.3a10.5 10.5 0 0 0 6.5 6.5l1.3-1.9 4 1.4v2.6a2 2 0 0 1-2.2 2A16.5 16.5 0 0 1 4.6 5.7a2 2 0 0 1 2-2.2z" />,
  alarm: <><circle cx="12" cy="13" r="7" /><path d="M12 10v3l2 2M5 4L3 6M19 4l2 2" /></>,
  mail: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 7l9 6 9-6" /></>,
  file: <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /></>,
  done: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  task: <circle cx="12" cy="12" r="7" />,
  command: <path d="M5 7l5 5-5 5M12 17h7" />,
  chat: <path d="M4 20l1.3-3.9A8 8 0 1 1 8 19z" />,
}

function Icon({ name }) {
  return (
    <svg className="el-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{ICONS[name] || ICONS.task}</svg>
  )
}

function Head({ face, name, rel, at, icon }) {
  return (
    <p className="el-head">
      {icon ? <Icon name={icon} /> : null}
      {face ? <Face id={face.id} name={face.name} size={20} /> : null}
      <span className="el-name">{name}</span>
      {rel ? <span className="el-rel">{rel}</span> : null}
      {at ? <span className="el-at">{at}</span> : null}
    </p>
  )
}

function FeedPost({ p, agent, at }) {
  const a = agentOf(agent)
  return (
    <div className="el-feed" role="button" tabIndex={0}
      onClick={function (e) { if (!(e.target.closest && e.target.closest('a'))) { go('feed') } }}
      onKeyDown={function (e) { if (e.key === 'Enter') { go('feed') } }}>
      <Head face={a || { id: agent }} name={a ? a.name : agent} rel={p.kind} at={at} />
      <Prose text={p.post} agent={agent} className="el-feed-text" />
    </div>
  )
}

/* The task as it is now. A task gone from his list, ticked by someone or dropped, is its title. */
function Task({ p }) {
  const store = useTasks()
  const known = store.list.some(function (t) { return t.id === p.id })
  if (known || !store.loaded) { return <div className="el-task"><TasksCard data={{ ids: [p.id] }} /></div> }
  return <Quote p={{ label: 'About a task', text: p.title }} />
}

function Quote({ p }) {
  return (
    <div className="el-quote">
      <span className="el-quote-label">{p.label}</span>
      <span className="el-quote-text">{p.text}</span>
    </div>
  )
}

function Answer({ p, at }) {
  return (
    <p className="el-line">
      <span>You said <b className={p.answer === 'Yes' ? 'el-yes' : undefined}>{p.answer}</b> to: {p.title}</span>
      {at ? <span className="el-at">{at}</span> : null}
      {p.why ? <span className="el-why">{p.why}</span> : null}
    </p>
  )
}

/* "- what: "title" (id, due when)" lines, the first block of a task report. */
function Report({ p, agent, at }) {
  const lines = String(p.text || '').split('\n')
  const changes = []
  for (const line of lines) {
    if (/^- /.test(line)) { changes.push(line.slice(2).replace(/ \(([0-9a-f]{6,16})(, due ([^)]*))?\)/, function (m, id, d, when) { return when ? ', due ' + when : '' })) }
    else if (changes.length) { break }
  }
  const rest = lines.slice(lines.findIndex(function (l) { return /^- /.test(l) }) + changes.length).join('\n').trim()
  return (
    <div className="el-job">
      <Head name="Task report" at={at} />
      {changes.map(function (c, i) { return <p className="el-job-text" key={i}>{c}</p> })}
      {rest ? <Fold lines={0} className="el-job-text"><Prose text={rest} agent={agent} className="el-prose" /></Fold> : null}
    </div>
  )
}

const OUTCOME = { succeeded: 'done', success: 'done', started: 'started', failed: 'failed', 'timed out': 'timed out' }

function Job({ p, agent, at }) {
  const word = OUTCOME[p.outcome] || p.outcome || ''
  const bad = /fail|time|error/.test(word)
  return (
    <div className="el-job">
      <p className="el-head">
        <span className="el-name">{p.name}</span>
        {word ? <span className={bad ? 'el-bad' : 'el-ok'}>{word}</span> : null}
        {at ? <span className="el-at">{at}</span> : null}
      </p>
      <Fold lines={2} className="el-job-text"><Prose text={p.text} agent={agent} className="el-prose" /></Fold>
    </div>
  )
}

function AskCheck({ p, at }) {
  return (
    <p className="el-line el-dim">
      <Icon name={/nothing/i.test(p.text) ? 'task' : 'done'} />
      <span className="el-line-text">Ask check: {p.text}</span>
      {at ? <span className="el-at">{at}</span> : null}
    </p>
  )
}

function Agent({ p, agent, at }) {
  const a = agentOf(p.who)
  function open() { if (a) { go('chat', '#/a/' + a.id) } }
  return (
    <div className="el-agent">
      <p className="el-head">
        <Face id={a ? a.id : ''} name={a ? a.name : p.who} size={22} />
        <button type="button" className="el-name el-link" onClick={open} disabled={!a}>{a ? a.name : p.who}</button>
        {p.rel ? <span className="el-rel">{p.rel}</span> : null}
        {at ? <span className="el-at">{at}</span> : null}
      </p>
      <Fold lines={6} className="el-agent-body"><Prose text={p.text} agent={agent} className="el-prose" /></Fold>
    </div>
  )
}

function Cord({ p, agent, at }) {
  const a = agentOf(p.who)
  return (
    <div className="el-box">
      <p className="el-head">
        <Icon name="phone" />
        {a ? <Face id={a.id} name={a.name} size={18} /> : null}
        <span className="el-name">{a ? p.title.replace(p.who, a.name) : p.title}</span>
        {at ? <span className="el-at">{at}</span> : null}
      </p>
      <Fold lines={4} className="el-box-text"><Prose text={p.text} agent={agent} className="el-prose" /></Fold>
    </div>
  )
}

function Call({ p, agent, at }) {
  return (
    <div className="el-box">
      <p className="el-head el-call"><Icon name="phone" /><span className="el-name">{p.title}</span>{at ? <span className="el-at">{at}</span> : null}</p>
      <Fold lines={4} className="el-box-text"><Prose text={p.text} agent={agent} className="el-prose" /></Fold>
    </div>
  )
}

function WhatsApp({ p, agent, at }) {
  return (
    <div className="el-wa">
      <p className="el-head"><Icon name="chat" /><span className="el-name">{p.chat || 'WhatsApp'}</span>{at ? <span className="el-at">{at}</span> : null}</p>
      <Fold lines={6} className="el-box-text"><Prose text={p.text} agent={agent} className="el-prose" /></Fold>
    </div>
  )
}

function Event({ p, at }) {
  return (
    <p className={'el-line' + (p.tone === 'bad' ? ' el-alarm' : ' el-dim')}>
      <Icon name={p.icon} />
      <span className="el-line-text">{p.text}</span>
      {at ? <span className="el-at">{at}</span> : null}
    </p>
  )
}

const DRAW = {
  feed: FeedPost, task: Task, quote: Quote, answer: Answer, report: Report, job: Job,
  askcheck: AskCheck, agent: Agent, cord: Cord, call: Call, wa: WhatsApp, event: Event,
}

export default function Element({ part, agent, at }) {
  const Draw = DRAW[part.el]
  return Draw ? <Draw p={part} agent={agent} at={at} /> : null
}
