/* The agent's options, from a long press on its card: the same six the chat header's menu has,
   without opening the chat. Opening the chat to reach them meant scrolling to the very top of
   the conversation, and that loads older messages on the way. Back, a tap outside or a choice
   closes it. */

import { useState } from 'react'
import { createPortal } from 'react-dom'
import cc from '../../data/api'
import useBackClose from '../../shell/useBackClose'
import { askApp, inApp } from '../../shell/native'

export default function AgentSheet({ agent, onClose, onSlept }) {
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  useBackClose(true, onClose)

  async function run(what, go) {
    setBusy(true)
    setNote('')
    try {
      await go()
      onClose()
    } catch (e) {
      setNote(what + ' failed: ' + e.message)
      setBusy(false)
    }
  }

  function rename() {
    const name = (window.prompt('New name for ' + agent.name, agent.name) || '').trim()
    if (!name || name === agent.name) { return }
    run('Rename', function () { return cc.updateAgent(agent.id, { name: name }) })
  }

  function forget() {
    const sure = window.confirm(
      'Forget ' + agent.name + '? It is put to sleep, its held messages, its jobs and its record '
      + 'go, and there is no undo. The conversation file on disk is untouched.',
    )
    if (sure) { run('Forget', function () { return cc.forget(agent.id) }) }
  }

  const items = [
    ['Wake', function () { run('Wake', function () { return cc.wake(agent.id) }) }],
    /* The fleet offers an undo for a few seconds after, so a wrong press costs nothing. The
       daemon says ok false when the agent was asleep already, and then there is nothing to
       undo: an Undo there would wake an agent nobody put to sleep. */
    ['Sleep', function () {
      run('Sleep', function () {
        return cc.sleep(agent.id).then(function (r) { if (onSlept && r && r.ok) { onSlept(agent) } })
      })
    }],
    ['Stop task', function () { run('Stop', function () { return cc.stop(agent.id) }) }],
    ['Rename', rename],
    /* Float mode, as in the chat's menu: the face on the edge over every app (Android app only). */
    ...(inApp() ? [['Float', function () {
      run('Float', async function () {
        const got = await askApp('float', { agent: agent.id, name: agent.name })
        if (got && got.why === 'overlay') { throw new Error('turn on "Display over other apps" for Ergo in Android settings') }
      })
    }]] : []),
    ['Hide', function () { run('Hide', function () { return cc.hide(agent.id) }) }],
  ]

  return createPortal(
    <div className="as-scrim" onClick={onClose}>
      <div className="as-sheet" role="menu" aria-label={agent.name + ' options'}
        onClick={function (e) { e.stopPropagation() }}>
        <p className="as-name">{agent.name}</p>
        {items.map(function ([label, act]) {
          return (
            <button key={label} type="button" role="menuitem" className="as-item" disabled={busy} onClick={act}>
              {label}
            </button>
          )
        })}
        <button type="button" role="menuitem" className="as-item as-danger" disabled={busy} onClick={forget}>
          Forget
        </button>
        {note ? <p className="as-note" role="status">{note}</p> : null}
      </div>
    </div>,
    document.body,
  )
}
