/* One scheduled job. The pencil opens the whole task in a text box, saved by Save or Ctrl+Enter
   and dropped by Cancel or Escape. The next run reads the new text. Delete sits past a rule and asks once more, because a job is deleted once
   and run every day, and a thumb must not mistake the two.

   A missed run never fires late. The daemon will not decide for you whether a 07:00 letter
   still makes sense at 14:00, so the row says it was missed and offers Run now. */

import { useRef, useState } from 'react'
import cc from '../../data/api'
import { everyShort, firstLine, until } from './format'

/* agent name, then when, then what happened. The agent comes first so a job is never an orphan. */
function line(job) {
  // A watch job has no next time. It runs when files change.
  const when = job.watch ? 'when files change in ' + job.watch
    : job.at ? 'daily ' + job.at : 'every ' + everyShort(job.every)
  const parts = [job.enabled ? when : when + ', off']
  if (!job.watch) { parts.push('next ' + (job.enabled ? until(job.next) : 'never')) }
  if (job.last_outcome) {
    parts.push('last run ' + job.last_outcome + (job.missed > 1 ? ' missed ×' + job.missed : ''))
  }
  return parts.join(' · ')
}

export default function CronStrip({ job, agentName, onChanged }) {
  const [asking, setAsking] = useState(false)
  const [gone, setGone] = useState(false)
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState('')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const delBtn = useRef(null)
  const yesBtn = useRef(null)

  // The row leaves the page, so the eye is not left to check a label.
  if (gone) { return null }

  function ask() {
    setAsking(true)
    // Move the keyboard to the answer, the way the mock does.
    window.requestAnimationFrame(function () {
      if (yesBtn.current) { yesBtn.current.focus() }
    })
  }

  function keep() {
    setAsking(false)
    window.requestAnimationFrame(function () {
      if (delBtn.current) { delBtn.current.focus() }
    })
  }

  /* Every call says what went wrong in the row itself. A job strip with a silent button is a
     strip you press twice. */
  async function run(fn, thenGone) {
    setBusy(true)
    try {
      const r = await fn()
      setSaid(r && r.said ? r.said : '')
      if (thenGone) { setGone(true) }
      if (onChanged) { onChanged() }
    } catch (e) {
      setSaid(e.message)
    } finally {
      setBusy(false)
    }
  }

  function edit() {
    setDraft(job.task || '')
    setEditing(true)
  }

  async function save() {
    const task = draft.trim()
    if (!task || busy) { return }
    if (task === (job.task || '').trim()) { setEditing(false); return }
    await run(function () { return cc.updateJob(job.id, { task: task }) })
    setEditing(false)
  }

  if (editing) {
    return (
      <div className="cron cron-editing">
        <p className="cron-sub">{(agentName || job.agent) + ' · '}{line(job)}</p>
        <textarea
          className="cron-edit"
          value={draft}
          autoFocus
          rows={Math.min(14, Math.max(4, draft.split('\n').length + 1))}
          aria-label="The task this job gives the agent"
          onChange={function (e) { setDraft(e.target.value) }}
          onKeyDown={function (e) {
            if (e.key === 'Escape') { e.preventDefault(); setEditing(false) }
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save() }
          }}
        />
        <div className="cron-edit-acts">
          {said ? <span className="chip">{said}</span> : null}
          <button type="button" className="btn sm quiet" onClick={function () { setEditing(false) }}>Cancel</button>
          <button type="button" className="btn sm" disabled={busy || !draft.trim()} onClick={save}>Save</button>
        </div>
      </div>
    )
  }

  return (
    <div className="cron">
      <div className="cron-main">
        <p className="cron-title">
          {firstLine(job.task)}
          <button type="button" className="cron-pencil" aria-label="Edit this job's task" title="Edit the task" onClick={edit}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 20h9"></path>
              <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"></path>
            </svg>
          </button>
          {said ? <span className="chip">{said}</span> : null}
        </p>
        <p className="cron-sub">{(agentName || job.agent) + ' · '}{line(job)}</p>
      </div>

      <div className="cron-acts">
        <button
          type="button"
          className="btn sm"
          disabled={busy}
          onClick={function () { run(function () { return cc.runJob(job.id) }) }}
        >
          Run now
        </button>
        <button
          type="button"
          className="switch"
          aria-pressed={job.enabled ? 'true' : 'false'}
          aria-label={job.enabled ? 'Turn this job off' : 'Turn this job on'}
          disabled={busy}
          onClick={function () {
            run(function () { return cc.updateJob(job.id, { enabled: !job.enabled }) })
          }}
        >
          <span className="track" aria-hidden="true"><span className="knob"></span></span>
          <span>{job.enabled ? 'On' : 'Off'}</span>
        </button>

        {/* Where a fresh run's result goes: on, a card in the chat for the owner; off, the agent's
            context. A run in the main conversation is in the context already, so it has no choice. */}
        {job.kind === 'fresh'
          ? (
            <button
              type="button"
              className="switch"
              aria-pressed={job.show === 'chat' ? 'true' : 'false'}
              aria-label={job.show === 'chat'
                ? 'Send results to the agent instead of the chat'
                : 'Show results in the chat instead of the agent'}
              title="On: the result shows in the chat and stays out of the agent's context"
              disabled={busy}
              onClick={function () {
                run(function () { return cc.updateJob(job.id, { show: job.show === 'chat' ? 'context' : 'chat' }) })
              }}
            >
              <span className="track" aria-hidden="true"><span className="knob"></span></span>
              <span>In chat</span>
            </button>
          )
          : null}

        <span className="cron-danger">
          <button type="button" className="btn sm danger" onClick={ask} hidden={asking} ref={delBtn}>Delete</button>
          <span className="cron-confirm" hidden={!asking}>
            <span>Delete this job for good?</span>
            <button
              type="button"
              className="btn sm danger"
              ref={yesBtn}
              disabled={busy}
              onClick={function () { run(function () { return cc.deleteJob(job.id) }, true) }}
            >
              Yes, delete
            </button>
            <button type="button" className="btn sm quiet" onClick={keep}>Keep</button>
          </span>
        </span>
      </div>
    </div>
  )
}
