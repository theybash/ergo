/* Memory. What each agent remembers, read back. Read only: nothing here edits a memory.

   Two kinds, both from the daemon's GET /api/memory:

     an agent's    the Claude Code memory of its folder: the MEMORY.md index, one file per thing
     the front desk's      the call log, one entry per day of calls

   Three steps on one screen: the agents, then one agent's memory list (a title and its one line
   hook), then one memory. The daemon decides who a view may see, and the demo view gets nobody.

   This is a plain read-only viewer. */

import { useEffect, useRef, useState } from 'react'
import cc from '../../data/api'
import Prose from '../chat/Prose'
import './memory.css'
import useScrollMemory from '../../shell/scrollMemory'

/* A memory file opens with front matter: its name, its one line, its type. The list already
   shows the first two, so the reader starts at the words. */
function body(text) {
  let t = String(text || '')
  if (t.startsWith('---')) {
    const end = t.indexOf('\n---', 3)
    if (end !== -1) { t = t.slice(end + 4).replace(/^\s+/, '') }
  }
  /* A list item wrapped by hand goes on with an indented line. The chat's renderer would start
     a new paragraph there, so the indented line joins the item, unless it is a list item itself. */
  return t.replace(/\n {2,}(?![-*+] |\d+\. )(?=\S)/g, ' ')
}

function count(n) {
  if (!n) { return 'Nothing kept yet.' }
  return n + (n === 1 ? ' memory' : ' memories')
}

export default function Memory({ view }) {
  // Its own place on the page, not the one the last screen left (shell/scrollMemory.js).
  useScrollMemory('screen:memory', true)
  /* The list, tagged with the view it came in, so the last view's agents never show in a new
     view while its own list is on the way. */
  const [list, setList] = useState({ view: null, agents: [] })
  const agents = list.view === view ? list.agents : []
  const loading = list.view !== view
  const asked = useRef(0)
  const [error, setError] = useState('')
  /* The agent and the memory open now, each tagged with the view it was opened in, so a new
     view starts again at the list of agents without an effect to clear them. */
  const [picked, setPicked] = useState({ view: null, id: '' })
  const [reading, setReading] = useState({ view: null, row: null })
  const who = picked.view === view ? picked.id : ''
  const open = reading.view === view ? reading.row : null
  function setWho(id) { setPicked({ view: view, id: id }) }
  function setOpen(row) { setReading({ view: view, row: row }) }

  useEffect(function () {
    /* A list that comes after the view changed again is dropped, so a slow reply from the last
       view never takes the place of this one. */
    let live = true
    cc.memoryList(view).then(function (got) {
      if (!live) { return }
      setList({ view: view, agents: got.agents || [] })
      setError('')
    }, function (e) {
      if (live) { setError(e.message) }
    })
    return function () { live = false }
  }, [view])

  async function read(agentId, row) {
    /* A reply that comes after the owner went back or opened another memory changes nothing. */
    const ask = ++asked.current
    setOpen({ file: row.file, title: row.title, text: '' })
    setError('')
    try {
      const got = await cc.memoryFile(view, agentId, row.file)
      if (ask === asked.current) { setOpen({ file: row.file, title: row.title, text: got.text }) }
    } catch (e) {
      if (ask === asked.current) {
        setOpen(null)
        setError(e.message)
      }
    }
  }

  const agent = agents.find(function (a) { return a.id === who }) || null

  function back() {
    asked.current += 1
    if (open) { setOpen(null) } else { setWho('') }
  }

  return (
    <section className="screen" id="screen-memory" aria-labelledby="mem-h">
      <div className="screen-head">
        <h1 id="mem-h">{agent ? agent.name : 'Memory'}</h1>
        {agent ? (
          <button type="button" className="mm-back" onClick={back}>
            {open ? agent.name : 'All agents'}
          </button>
        ) : null}
      </div>

      {error ? <p className="fl-err">{error}</p> : null}

      {!agent ? (
        <div className="mm-list">
          {agents.map(function (a) {
            return (
              <button type="button" className="mm-row" key={a.id} onClick={function () { setWho(a.id) }}>
                <span className="mm-title">{a.name}</span>
                <span className="mm-hook">{a.voice ? (a.memory.length ? 'Calls, by day' : 'No calls yet.') : count(a.memory.length)}</span>
              </button>
            )
          })}
          {!agents.length ? (
            <p className="needs-empty">
              {loading ? 'Reading the memories.' : view === 'demo' ? 'Memory is not shown in the demo view.' : 'No agent here keeps a memory.'}
            </p>
          ) : null}
        </div>
      ) : open ? (
        <article className="mm-read">
          <h2 className="mm-read-title">{open.title}</h2>
          {open.text
            ? (agent.voice
                ? <div className="mm-calls">{open.text}</div>
                : <Prose text={body(open.text)} className="mm-prose" />)
            : <p className="needs-empty">Reading.</p>}
        </article>
      ) : (
        <div className="mm-list">
          {agent.memory.map(function (row) {
            return (
              <button
                type="button"
                className="mm-row"
                key={row.file}
                disabled={row.missing}
                onClick={function () { read(agent.id, row) }}
              >
                <span className="mm-title">{row.title}</span>
                <span className="mm-hook">{row.missing ? 'The index names this file, and it is not there.' : row.hook}</span>
              </button>
            )
          })}
          {!agent.memory.length ? <p className="needs-empty">{count(0)}</p> : null}
        </div>
      )}
    </section>
  )
}
