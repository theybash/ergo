/* The inbox: what the front desk has to tell the owner at the start of the next call.

     waiting   one row each: the agent, two lines of it until tapped, the urgency and how old
               it is, and a quiet x that removes it; the list scrolls in a box of its own
     Clear all removes every waiting item, behind one confirm in place
     the day   items told or closed in the last day, faint, with what became of them and who
               decided, and Restore, which puts one back to waiting
     times     the morning brief time and the quiet hours, saved the way Voice memory saves

   The list comes from GET /api/inbox and is read again whenever the fleet's inbox summary moves,
   which the event stream carries. Every button saves at once; a failure is one line under the
   list and the list is read again. */

import { useEffect, useState } from 'react'
import cc from '../../data/api'

const cross = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>
)

/* How old an item is, in the fewest words. */
function age(added) {
  const secs = Math.max(0, Date.now() / 1000 - (added || 0))
  if (secs < 60) { return 'just now' }
  if (secs < 3600) { return Math.round(secs / 60) + ' min' }
  if (secs < 86400) { return Math.round(secs / 3600) + ' h' }
  return Math.round(secs / 86400) + ' d'
}

/* What became of an item, and who said so. */
function fate(item) {
  const word = item.decision || item.status
  if (item.by === 'jev') { return word + ' by Jev' }
  if (item.by === 'ask') { return 'answered' }
  return word
}

/* One time of day, HH:MM, in the same row as the Voice memory numbers. It saves when it loses
   the focus or on Enter; something that is not a time goes back to what the daemon holds. */
function TimeRow({ name, label, rest, value, onSave }) {
  const [draft, setDraft] = useState(value)
  const [error, setError] = useState('')

  async function save() {
    const got = draft.trim().match(/^(\d{1,2}):(\d{2})$/)
    if (!got || Number(got[1]) > 23 || Number(got[2]) > 59) {
      setDraft(value)
      return
    }
    const want = got[1].padStart(2, '0') + ':' + got[2]
    setDraft(want)
    if (want === value) { return }
    setError('')
    try {
      await onSave({ [name]: want })
    } catch (e) {
      setError(e.message || 'That did not save.')
      setDraft(value)
    }
  }

  const id = 'cf-ib-' + name
  return (
    <div className="cf-meter">
      <p className="cf-meter-line cf-vm-line">
        <label className="cf-meter-label" htmlFor={id}>{label}</label>
        <input
          id={id}
          className="cf-number cf-time"
          type="text"
          inputMode="numeric"
          maxLength={5}
          value={draft}
          onChange={function (e) { setDraft(e.target.value) }}
          onBlur={save}
          onKeyDown={function (e) { if (e.key === 'Enter') { e.currentTarget.blur() } }}
        />
        <span className="cf-meter-rest">{rest}</span>
      </p>
      {error ? <p className="cf-err">{error}</p> : null}
    </div>
  )
}

function Times() {
  const [held, setHeld] = useState(null)

  useEffect(function () {
    let live = true
    cc.voiceSettings().then(
      function (d) { if (live) { setHeld(d) } },
      function () {},
    )
    return function () { live = false }
  }, [])

  /* An older daemon has no times, and then there is nothing to set. */
  if (!held || !held.morning_brief) { return null }

  async function save(fields) {
    setHeld(await cc.saveVoiceSettings(fields))
  }

  return (
    <div className="cf-usage cf-ib-times">
      <TimeRow
        key={'morning_brief' + held.morning_brief}
        name="morning_brief"
        label="Morning brief"
        rest="Telegram says how many things wait"
        value={held.morning_brief}
        onSave={save}
      />
      <TimeRow
        key={'quiet_start' + held.quiet_start}
        name="quiet_start"
        label="Quiet from"
        rest="no ping for a now item"
        value={held.quiet_start}
        onSave={save}
      />
      <TimeRow
        key={'quiet_end' + held.quiet_end}
        name="quiet_end"
        label="Quiet until"
        rest=""
        value={held.quiet_end}
        onSave={save}
      />
    </div>
  )
}

export default function InboxRows({ fleet }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  // Which briefs he tapped open. Each shows two lines until then.
  const [whole, setWhole] = useState({})
  /* The summary moves on every inbox event, so the list is read again when it does. */
  const moved = fleet && fleet.inbox ? fleet.inbox.at : 0

  useEffect(function () {
    let live = true
    cc.inbox().then(
      function (d) { if (live) { setData(d) } },
      function () {},
    )
    return function () { live = false }
  }, [moved])

  async function run(call) {
    setBusy(true)
    setError('')
    try {
      await call()
    } catch (e) {
      setError(e.message || 'That did not save.')
    }
    try { setData(await cc.inbox()) } catch { /* the next event reads it again */ }
    setBusy(false)
  }

  /* A daemon without the route draws nothing but the times. */
  if (!data) { return <Times /> }

  const waiting = data.waiting || []
  const told = data.told || []

  return (
    <>
      {waiting.length ? (
        <ul className="cf-rows agentcard cf-ib cf-ib-box" id="cf-ib-waiting">
          {waiting.map(function (item) {
            return (
              <li className="cf-row cf-ib-row" key={item.id} data-item={item.id}>
                <span className="cf-ib-agent">{item.agent}</span>
                <span className={'cf-ib-text' + (whole[item.id] ? '' : ' cf-ib-two')}
                  onClick={function () { setWhole({ ...whole, [item.id]: !whole[item.id] }) }}>{item.text}</span>
                <span className="cf-ib-meta">{item.urgency}, {age(item.added)}</span>
                <button
                  type="button"
                  className="cf-ib-x"
                  disabled={busy}
                  aria-label={'Remove the item from ' + item.agent}
                  onClick={function () { run(function () { return cc.inboxAct(item.id, 'removed') }) }}
                >
                  {cross}
                </button>
              </li>
            )
          })}
        </ul>
      ) : (
        <p className="cf-faint cf-u-line" id="cf-ib-empty">Nothing waiting for the front desk.</p>
      )}

      {waiting.length ? (
        <p className="cf-ib-clear">
          {asking ? (
            <>
              <span className="cf-meter-rest">
                Remove all {waiting.length} waiting?
              </span>
              <button
                type="button"
                className="btn sm danger"
                id="cf-ib-clear-yes"
                disabled={busy}
                onClick={function () {
                  setAsking(false)
                  run(function () { return cc.inboxClear() })
                }}
              >
                Clear all
              </button>
              <button type="button" className="btn sm" disabled={busy} onClick={function () { setAsking(false) }}>
                Keep
              </button>
            </>
          ) : (
            <button type="button" className="btn sm" id="cf-ib-clear" disabled={busy} onClick={function () { setAsking(true) }}>
              Clear all
            </button>
          )}
        </p>
      ) : null}

      {told.length ? (
        <ul className="cf-rows agentcard cf-ib cf-ib-day cf-ib-box" id="cf-ib-told">
          {told.map(function (item) {
            return (
              <li className="cf-row cf-ib-row cf-ib-told" key={item.id} data-item={item.id}>
                <span className="cf-ib-agent">{item.agent}</span>
                <span className="cf-ib-text">{item.text}</span>
                <span className="cf-ib-meta">{fate(item)}</span>
                <button
                  type="button"
                  className="btn sm cf-ib-act"
                  disabled={busy}
                  aria-label={'Restore the item from ' + item.agent}
                  onClick={function () { run(function () { return cc.inboxAct(item.id, 'restored') }) }}
                >
                  Restore
                </button>
              </li>
            )
          })}
        </ul>
      ) : null}

      {error ? <p className="cf-err">{error}</p> : null}

      <Times />
    </>
  )
}
