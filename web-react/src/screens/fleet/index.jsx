/* The fleet. Every agent the daemon knows, on one page, with the one line each of them last
   said, the asks waiting on you above the grid, and the schedule below it.

   The health strip is gone. The owner asked for it: health is a screen, and a card repeating
   two of its numbers above the fleet was a second place to read them. Health lives in the
   Config screen, which has a tab on the phone and a pill on the desktop, so this head no
   longer links to it.

   Cords are not drawn. Cords.jsx is still here and still correct, and the owner decided a line
   arcing between two cards is a picture of something that lasts twenty seconds. The data is in
   useFleet().cords for whoever wants it later. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import useFleet from '../../data/useFleet'
import useFolds from '../../data/folds'
import cc from '../../data/api'
import still from '../../shell/still'
import { showsIn } from '../../shell/views'
import AgentCard from './AgentCard'
import AgentSearch from '../../shell/AgentSearch'
import AgentAsks from './AgentAsks'
import CronFilter from './CronFilter'
import CronStrip from './CronStrip'
import HeldDialog from './HeldDialog'
import Needs from './Needs'
import NewAgentDialog from './NewAgentDialog'
import { setPending } from './needsStore'
import { since } from './format'
import './fleet.css'
import useScrollMemory from '../../shell/scrollMemory'

/* An answered row holds its receipt this long, then fades, then goes. The same two numbers the
   shell used for the fake list, because the fold is the same fold. */
const FOLD = 1500
const FADE = 220

/* Ages and "next in 3h" go stale sitting on the screen. One timer for the whole page. */
const TICK = 20000

// How long the undo for a Sleep stays on screen.
const UNDO = 8000

/* What one ask says on one line. A permission ask shows the command it wants to run, in full
   in the title, because a command you are asked to approve must not be cut. */
function describe(ask) {
  if (ask.kind === 'question') {
    const first = (ask.questions || [])[0] || {}
    return { what: first.question || 'asks you a question', mono: '', acts: ['answer'] }
  }
  const command = ask.input && ask.input.command
  if (command) {
    return { what: (ask.tool || 'A tool') + ' wants to run ', mono: command, acts: ['deny', 'approve'] }
  }
  return {
    what: (ask.tool || 'A tool') + ' wants permission',
    mono: ask.summary || '',
    acts: ['deny', 'approve'],
  }
}

export default function Fleet({ onScreen, view }) {
  // Its own place on the page, not the one the last screen left (shell/scrollMemory.js).
  useScrollMemory('screen:fleet', true)
  const [finding, setFinding] = useState(false)
  const fleet = useFleet()
  const { agents, jobs, refresh } = fleet

  const box = useRef(null)

  const [asksBy, setAsksBy] = useState({})
  const [receipts, setReceipts] = useState({})
  const [busyAsk, setBusyAsk] = useState({})
  const [busyAgent, setBusyAgent] = useState({})
  /* Cron folds away and stays folded across visits. Storage can be off, so a failed read means
     folded, which is the quiet default. */
  const [cronOpen, setCronOpen] = useState(function () {
    try { return localStorage.getItem('cc-cron-open') === '1' } catch { return false }
  })
  const [cronPick, setCronPick] = useState(function () {
    try { return JSON.parse(localStorage.getItem('cc-cron-pick') || '[]') } catch { return [] }
  })
  const [newAgent, setNewAgent] = useState(false)
  const [heldFor, setHeldFor] = useState(null)
  const [notice, setNotice] = useState('')
  // The agent just put to sleep from its menu, for the few seconds its undo is offered.
  const [slept, setSlept] = useState(null)
  useEffect(function () {
    if (!slept) { return }
    const t = window.setTimeout(function () { setSlept(null) }, UNDO)
    return function () { window.clearTimeout(t) }
  }, [slept])
  const [, setTick] = useState(0)

  useEffect(function () {
    const t = window.setInterval(function () { setTick(function (n) { return n + 1 }) }, TICK)
    return function () { window.clearInterval(t) }
  }, [])

  /* Stable, so AgentAsks reports only when its own asks changed.

     The arrival time is stamped here, where the ask arrives, and kept beside it. The daemon
     puts no time on an ask frame and resends every waiting one on each attach, so the first
     sighting in this browser is the only clock there is. An ask already held keeps its own
     stamp, which is what makes a reconnect leave the ages alone. */
  const onAsks = useCallback(function (id, list) {
    const now = Date.now()
    setAsksBy(function (prev) {
      if (list === null) {
        if (!(id in prev)) { return prev }
        const next = { ...prev }
        delete next[id]
        return next
      }
      const before = prev[id] || []
      const known = new Map(before.map(function (row) { return [row.ask.id, row] }))
      const rows = list.map(function (ask) {
        return known.get(ask.id) || { ask, at: now }
      })
      const same = rows.length === before.length
        && rows.every(function (row, i) { return row === before[i] })
      if (same && id in prev) { return prev }
      if (!(id in prev) && !rows.length) { return prev }
      return { ...prev, [id]: rows }
    })
  }, [])

  // Newest first, as an inbox is.
  const shown = agents.filter(function (a) { return showsIn(a, view) })
    .sort(function (a, b) { return (Date.parse(b.last_message_at) || 0) - (Date.parse(a.last_message_at) || 0) })
  /* Delegates fold under their parent: they work for it, not for him,
     so they stay out of his list until he opens the fold. Archived ones sink to the bottom. A
     delegate whose parent is not shown here still gets a row of its own, so none is lost. */
  const parents = new Set(shown.map(function (a) { return a.id }))
  const visible = shown.filter(function (a) { return !a.parent || !parents.has(a.parent) })
  function delegatesOf(id) {
    return shown.filter(function (a) { return a.parent === id })
      .sort(function (a, b) { return (a.archived ? 1 : 0) - (b.archived ? 1 : 0) })
  }
  const [folds, fold] = useFolds()

  /* Wake from the card, and Sleep where a caller still asks for it. Sleep asks, it never forces: a busy agent that will not stop
     says so in the notice, and the chat head is where a force lives. */
  async function power(agent) {
    setBusyAgent(function (held) { return { ...held, [agent.id]: true } })
    try {
      if (agent.state === 'green') { await cc.sleep(agent.id) } else { await cc.wake(agent.id) }
      await refresh()
    } catch (err) {
      setNotice(err.message || 'That did not work.')
    } finally {
      setBusyAgent(function (held) { const next = { ...held }; delete next[agent.id]; return next })
    }
  }

  /* Every ask from every awake agent, in one list, with its receipt if it has one. */
  const rows = useMemo(function () {
    const out = []
    const used = new Set()
    for (const a of agents) {
      for (const row of asksBy[a.id] || []) {
        const key = a.id + '|' + row.ask.id
        const held = receipts[key]
        used.add(key)
        out.push({
          key,
          agentId: a.id,
          askId: row.ask.id,
          who: a.name,
          age: since(row.at),
          busy: !!busyAsk[key],
          receipt: held ? held.receipt : null,
          gone: held ? held.gone : false,
          ...describe(row.ask),
        })
      }
    }
    /* A receipt outlives its ask: the daemon drops the ask the moment it is answered, and the
       row has to stay long enough to be read. */
    for (const key of Object.keys(receipts)) {
      if (used.has(key)) { continue }
      const held = receipts[key]
      out.push({
        key, who: held.who, what: '', mono: '', age: '', acts: [],
        receipt: held.receipt, gone: held.gone,
      })
    }
    return out
  }, [agents, asksBy, receipts, busyAsk])

  const pending = rows.filter(function (row) { return !row.receipt }).length

  /* The shell reads this for the Fleet tab badge. */
  useEffect(function () { setPending(pending) }, [pending])

  /* Each deadline is counted from the moment the row was answered, so a second answer never
     pushes the first row's clock back. */
  useEffect(function () {
    const fade = still() ? 0 : FADE
    const timers = Object.keys(receipts).map(function (key) {
      const held = receipts[key]
      const due = held.gone ? held.at + FOLD + fade : held.at + FOLD
      return window.setTimeout(function () {
        setReceipts(function (all) {
          const one = all[key]
          if (!one) { return all }
          const next = { ...all }
          if (one.gone) { delete next[key] } else { next[key] = { ...one, gone: true } }
          return next
        })
      }, Math.max(0, due - Date.now()))
    })
    return function () { timers.forEach(function (t) { window.clearTimeout(t) }) }
  }, [receipts])

  const toNeeds = useCallback(function () {
    const el = box.current
    if (el) { el.scrollIntoView({ behavior: still() ? 'auto' : 'smooth', block: 'start' }) }
  }, [])

  /* Open hands the agent to the chat through the address. The shell pushes one history entry
     for the screen and the hash together, so back from the chat returns here. */
  const open = useCallback(function (id) {
    onScreen('chat', '#/a/' + id)
  }, [onScreen])

  function keep(key, receipt, who) {
    setReceipts(function (all) {
      return { ...all, [key]: { receipt, at: Date.now(), gone: false, who } }
    })
  }

  async function answerRow(row, kind) {
    if (row.receipt) { return }
    if (kind === 'answer') {
      /* The question card is already waiting in the chat, with its options drawn. */
      keep(row.key, { verdict: 'Opened in the chat' }, row.who)
      open(row.agentId)
      return
    }
    setBusyAsk(function (all) { return { ...all, [row.key]: true } })
    setNotice('')
    try {
      const r = await cc.answer(row.agentId, row.askId, { allow: kind === 'approve' })
      /* A refusal here is a 200 with ok false. The usual cause is a second browser answering
         first, and the ask is not lost, so the row stays. */
      if (r && r.ok === false) { throw new Error(r.error || 'that ask has gone') }
      keep(
        row.key,
        kind === 'approve'
          ? { verdict: 'Approved', detail: row.mono }
          : { verdict: 'Denied', negative: true },
        row.who,
      )
    } catch (e) {
      setNotice(e.message)
    } finally {
      setBusyAsk(function (all) {
        const next = { ...all }
        delete next[row.key]
        return next
      })
    }
  }

  async function act(id, fn) {
    setBusyAgent(function (all) { return { ...all, [id]: true } })
    setNotice('')
    try {
      await fn()
      await refresh()
    } catch (e) {
      setNotice(e.message)
    } finally {
      setBusyAgent(function (all) {
        const next = { ...all }
        delete next[id]
        return next
      })
    }
  }

  /* The agents that own a job, in fleet order, each with its count. A picked agent that no longer
     owns a job drops out of the pick, so the filter never hides everything. */
  const cronOwners = []
  jobs.forEach(function (job) {
    const had = cronOwners.find(function (o) { return o.id === job.agent })
    if (had) { had.count += 1; return }
    const owner = agents.find(function (a) { return a.id === job.agent })
    cronOwners.push({ id: job.agent, name: owner ? owner.name : job.agent, count: 1 })
  })
  const cronPicked = cronPick.filter(function (id) {
    return cronOwners.some(function (o) { return o.id === id })
  })
  const cronJobs = cronPicked.length
    ? jobs.filter(function (job) { return cronPicked.includes(job.agent) })
    : jobs

  return (
    <section className="screen" id="screen-fleet" aria-labelledby="fleet-h">
      {/* One socket per awake agent, and none at all for a sleeping one: it has no engine, so
          it can have no ask. These draw nothing. */}
      {agents.filter(function (a) { return a.state === 'green' }).map(function (a) {
        return <AgentAsks key={a.id} id={a.id} onAsks={onAsks} />
      })}

      <div className="screen-head">
        <h1 id="fleet-h">Fleet</h1>
        <div className="fleet-tools">
          <button type="button" className="fl-recruit fl-find" aria-label="Find an agent"
            onClick={function () { setFinding(true) }}>
            <span className="fl-recruit-word">Find</span>
            <svg className="fl-recruit-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7"></circle>
              <path d="M20 20l-3.5-3.5"></path>
            </svg>
          </button>
          {finding ? <AgentSearch view={view} onScreen={onScreen} onClose={function () { setFinding(false) }} /> : null}
          {/* Above 720px it says what it does. Under it the word would take the width the
              fleet needs, so it is a round plus, named the same either way. */}
          <button
            type="button"
            className="btn sm fl-new"
            aria-label="New agent"
            onClick={function () { setNewAgent(true) }}
          >
            <span className="fl-new-word">New agent</span>
            <svg className="fl-new-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <path d="M12 5v14M5 12h14"></path>
            </svg>
          </button>
          {/* Recruit. A hidden agent and a Claude Code or OpenCode conversation started outside
              the console are both one press from a card, and that press is here, beside the one
              that makes a new agent. Above 720px it is a quiet word; under it, a round button
              beside the plus, because the word would take the width the fleet needs. */}
          <button
            type="button"
            className="fl-recruit"
            aria-label="Recruit"
            onClick={function () { onScreen('recruit') }}
          >
            <span className="fl-recruit-word">Recruit</span>
            <svg className="fl-recruit-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path>
              <circle cx="9" cy="7" r="4"></circle>
              <path d="M19 8v6M22 11h-6"></path>
            </svg>
          </button>
        </div>
      </div>

      {fleet.error ? <p className="fl-err">{fleet.error}</p> : null}
      {notice ? <p className="fl-err">{notice}</p> : null}
      {slept ? (
        <p className="fl-undo" role="status">
          {slept.name + ' is asleep.'}
          <button
            type="button"
            className="fl-undo-btn"
            onClick={function () {
              const id = slept.id
              setSlept(null)
              act(id, function () { return cc.wake(id) })
            }}
          >
            Undo
          </button>
        </p>
      ) : null}


      <Needs rows={rows} onAnswer={answerRow} boxRef={box} />

      <div className="fleet" id="fleet-grid">
        {visible.map(function (agent) {
          const subs = delegatesOf(agent.id)
          const card = function (a) {
            return (
              <AgentCard
                delegates={a.id === agent.id ? subs : []}
                folded={!!folds[agent.id]}
                onFold={function () { fold(agent.id) }}
                key={a.id}
                agent={a}
                count={rows.filter(function (r) { return r.agentId === a.id && !r.receipt }).length}
                onCount={toNeeds}
                onOpen={function () { open(a.id) }}
                onHeld={function () { setHeldFor(a) }}
                powering={!!busyAgent[a.id]}
                onPower={function () { power(a) }}
                onSlept={function (x) { refresh(); setSlept(x) }}
              />
            )
          }
          if (!subs.length || !folds[agent.id]) { return card(agent) }
          return (
            <div key={agent.id} className="fl-family">
              {card(agent)}
              {subs.map(card)}
            </div>
          )
        })}
      </div>

      {!fleet.loading && !visible.length ? (
        <p className="needs-empty">
          {agents.length
            ? 'Every agent is tagged for another view.'
            : 'No agents yet. New agent starts one.'}
        </p>
      ) : null}

      <section className="section" aria-labelledby="cron-h">
        <div className="section-head">
          <h3 id="cron-h">
            <button
              type="button"
              className="fl-fold"
              aria-expanded={cronOpen}
              onClick={function () {
                const next = !cronOpen
                setCronOpen(next)
                try { localStorage.setItem('cc-cron-open', next ? '1' : '0') } catch { /* fine */ }
              }}
            >
              <svg className={'chev' + (cronOpen ? ' fl-fold-open' : '')} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M9 6l6 6-6 6"></path>
              </svg>
              Cron
            </button>
          </h3>
          <span className="hint">{cronJobs.length + (cronJobs.length === 1 ? ' job' : ' jobs')}</span>
          <button
            type="button"
            className="fl-info"
            data-tip="A missed run never fires late."
            aria-label="A missed run never fires late."
            onClick={function (e) { e.currentTarget.focus() }}
          >
            i
          </button>
          {cronOpen ? (
            <CronFilter
              owners={cronOwners}
              picked={cronPicked}
              onPick={function (next) {
                setCronPick(next)
                try { localStorage.setItem('cc-cron-pick', JSON.stringify(next)) } catch { /* fine */ }
              }}
            />
          ) : null}
        </div>

        {!cronOpen ? null : cronJobs.length ? cronJobs.map(function (job) {
          const owner = agents.find(function (a) { return a.id === job.agent })
          return (
            <CronStrip
              key={job.id}
              job={job}
              agentName={owner ? owner.name : job.agent}
              onChanged={refresh}
            />
          )
        }) : <p className="needs-empty">Nothing is scheduled.</p>}
      </section>

      {newAgent ? (
        <NewAgentDialog
          onClose={function () { setNewAgent(false) }}
          onMade={function (made) { refresh(); if (made && made.id) { open(made.id) } }}
        />
      ) : null}

      {heldFor ? (
        <HeldDialog
          agent={heldFor}
          onClose={function () { setHeldFor(null) }}
          onChanged={refresh}
        />
      ) : null}
    </section>
  )
}
