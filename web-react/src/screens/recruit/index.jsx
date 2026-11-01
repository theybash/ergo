/* Recruit. Two ways to put a card back on the Fleet screen.

   An agent is a conversation id and a folder, and nothing else. Delete on the Fleet screen
   only hides the record, and both engines have held conversations on this laptop that the
   console never knew about. So both of those are recruitable, and they sit here in two lists:

     Hidden     the console made the agent and a delete hid it. Bring back is one flag.
     Sessions   Claude Code or OpenCode made the conversation. Adopt writes a record for it.

   Neither starts anything. A recruited agent arrives asleep, like every other card, and the
   owner wakes it from the chat. The row says which engine will resume it.

   The rows are the fleet card face: a name, the one line that says what it is, and the quiet
   facts under it. No chips, no tags, no colour. */

import { useCallback, useEffect, useMemo, useState } from 'react'
import cc from '../../data/api'
/* The same words for the same distance in time as the Fleet screen uses. One spelling of
   "3d ago" in the whole console. */
import { ago } from '../fleet/format'
import { plain } from '../../data/plain'
import './recruit.css'

/* Bytes as a person reads them. A session file runs from a few kilobytes to eighty megabytes,
   and the size is the honest signal of how much conversation is in there. */
function weight(bytes) {
  const n = Number(bytes || 0)
  if (n >= 1024 * 1024) { return Math.round(n / (1024 * 1024)) + ' MB' }
  return Math.max(1, Math.round(n / 1024)) + ' kB'
}

/* The last part of a path, which is what a folder is called. */
function folderName(path) {
  const parts = String(path || '').split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : ''
}

function matches(text, needle) {
  return String(text || '').toLowerCase().includes(needle)
}

export default function Recruit({ onScreen }) {
  const [hidden, setHidden] = useState([])
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [find, setFind] = useState('')
  const [busy, setBusy] = useState({})
  /* The session whose name is being typed, and what has been typed so far. A session that
     already carries a name is adopted without asking. */
  const [naming, setNaming] = useState('')
  const [typed, setTyped] = useState('')

  const load = useCallback(async function () {
    try {
      const got = await cc.recruits()
      setHidden(got.hidden || [])
      setSessions(got.sessions || [])
      setError('')
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(function () {
    /* oxlint-disable-next-line react/set-state-in-effect -- the daemon is an external system,
     * and the list has to come from somewhere. */
    load()
  }, [load])

  /* A recruited agent lands on the Fleet screen as a card and opens in the chat, which is what
     a new agent does today. */
  const open = useCallback(function (id) {
    onScreen('chat', '#/a/' + id)
  }, [onScreen])

  const needle = find.trim().toLowerCase()

  const theHidden = useMemo(function () {
    if (!needle) { return hidden }
    return hidden.filter(function (a) {
      return matches(a.name, needle) || matches(a.id, needle)
        || matches(a.cwd, needle) || matches(a.last_message, needle)
    })
  }, [hidden, needle])

  const theSessions = useMemo(function () {
    if (!needle) { return sessions }
    return sessions.filter(function (s) {
      return matches(s.name, needle) || matches(s.cwd, needle)
        || matches(s.first, needle) || matches(s.session_id, needle)
        || matches(s.engine, needle)
    })
  }, [sessions, needle])

  function mark(key, on) {
    setBusy(function (all) {
      const next = { ...all }
      if (on) { next[key] = true } else { delete next[key] }
      return next
    })
  }

  async function bringBack(id) {
    mark(id, true)
    setError('')
    try {
      const rec = await cc.bringBack(id)
      open(rec.id)
    } catch (e) {
      setError(e.message)
      mark(id, false)
    }
  }

  async function adopt(row, name) {
    const key = row.session_id
    mark(key, true)
    setError('')
    const body = { session_id: key, engine: row.engine || 'claude' }
    if (name) { body.name = name }
    try {
      let made = await cc.adoptSession(body)
      /* Not a failure. A question: the folder that conversation ran in has gone, and here is
         where it would go back. The same question the new agent dialog asks. */
      if (made && made.needs_dir) {
        const ok = window.confirm(made.path + ' is not there any more.\n\nCreate it and adopt the session there?')
        if (!ok) { mark(key, false); return }
        made = await cc.adoptSession({ ...body, create_dir: true })
      }
      setNaming('')
      setTyped('')
      open(made.id)
    } catch (e) {
      setError(e.message)
      mark(key, false)
    }
  }

  function startAdopt(row) {
    if (row.name) { adopt(row, ''); return }
    /* The name box is open: Adopt is the confirm, since a phone keyboard's Enter is easy to
       miss and a second tap that only reopened the box read as the button doing nothing. */
    if (naming === row.session_id) {
      if (typed.trim()) { adopt(row, typed.trim()) }
      return
    }
    /* No name anywhere in the file, so one is asked for on the row itself. */
    setNaming(row.session_id)
    setTyped(folderName(row.cwd))
  }

  function nameKeys(ev, row) {
    if (ev.key === 'Enter') {
      ev.preventDefault()
      if (typed.trim()) { adopt(row, typed.trim()) }
    }
    if (ev.key === 'Escape') {
      ev.preventDefault()
      setNaming('')
      setTyped('')
    }
  }

  return (
    <section className="screen" id="screen-recruit" aria-labelledby="recruit-h">
      <div className="screen-head">
        <h1 id="recruit-h">Recruit</h1>
        <button
          type="button"
          className="rc-back"
          onClick={function () { onScreen('fleet') }}
        >
          Fleet
        </button>
      </div>

      {error ? <p className="fl-err">{error}</p> : null}

      <input
        className="field rc-find"
        type="search"
        autoComplete="off"
        aria-label="Filter by name, folder or first message"
        placeholder="Filter by name, folder or first message"
        value={find}
        onChange={function (ev) { setFind(ev.target.value) }}
      />

      <section className="section rc-first-section" aria-labelledby="rc-hidden-h">
        <div className="section-head">
          <h3 id="rc-hidden-h">Hidden</h3>
          <span className="hint">Delete only takes the card off the Fleet screen.</span>
        </div>

        {theHidden.map(function (a) {
          return (
            <div className="rc-row" key={a.id}>
              <div className="rc-body">
                <p className="rc-name">{a.name}</p>
                <p className="rc-said">{plain(a.last_message) || 'Has not said anything yet.'}</p>
                <p className="rc-facts">
                  <span className="rc-where">{a.cwd}</span>
                  <span>{ago(a.last_message_at) || 'never'}</span>
                  <span>{a.turns + (a.turns === 1 ? ' turn' : ' turns')}</span>
                </p>
              </div>
              <button
                type="button"
                className="btn sm"
                disabled={!!busy[a.id]}
                onClick={function () { bringBack(a.id) }}
              >
                Bring back
              </button>
            </div>
          )
        })}

        {!theHidden.length ? (
          <p className="needs-empty">
            {hidden.length ? 'Nothing hidden matches that.' : 'Nothing is hidden.'}
          </p>
        ) : null}
      </section>

      <section className="section" aria-labelledby="rc-sessions-h">
        <div className="section-head">
          <h3 id="rc-sessions-h">Sessions</h3>
          <span className="hint">Every Claude Code and OpenCode conversation on this laptop.</span>
        </div>

        {theSessions.map(function (s) {
          const title = s.name || folderName(s.cwd) || s.session_id.slice(0, 8)
          const asking = naming === s.session_id
          return (
            <div className="rc-row" key={s.session_id}>
              <div className="rc-body">
                <p className="rc-name">{title}</p>
                <p className="rc-said">{s.first || 'Nothing was typed in it.'}</p>
                <p className="rc-facts">
                  <span className="rc-where">{s.cwd || s.slug}</span>
                  <span>{s.engine === 'opencode' ? 'OpenCode' : 'Claude Code'}</span>
                  <span>{ago(s.last)}</span>
                  <span>{s.turns + (s.turns === 1 ? ' turn' : ' turns')}</span>
                  {s.size ? <span>{weight(s.size)}</span> : null}
                </p>
                {asking ? (
                  <input
                    className="field rc-name-field"
                    autoFocus
                    autoComplete="off"
                    aria-label={'A name for ' + title}
                    placeholder="A name for it, then Enter"
                    value={typed}
                    onChange={function (ev) { setTyped(ev.target.value) }}
                    enterKeyHint="done"
                    onKeyDown={function (ev) { nameKeys(ev, s) }}
                  />
                ) : null}
              </div>
              {s.registered ? (
                <span className="rc-known">registered</span>
              ) : (
                <button
                  type="button"
                  className="btn sm"
                  disabled={!!busy[s.session_id]}
                  onClick={function () { startAdopt(s) }}
                >
                  Adopt
                </button>
              )}
            </div>
          )
        })}

        {!theSessions.length ? (
          <p className="needs-empty">
            {loading
              ? 'Reading the conversations on this laptop.'
              : sessions.length ? 'No conversation matches that.' : 'No conversations to adopt.'}
          </p>
        ) : null}
      </section>
    </section>
  )
}
