/* The agent's name, how it is, and everything you can do to it that is not typing.

   The menu is a real <details>, so the keyboard and the screen reader get the fold for free and
   nothing has to listen for a click somewhere else on the page to close it.

   There is no model picker and no permission mode here. Both live in the composer's plus menu,
   so each of them has one home. This menu is the life of the agent and nothing else.

   Forget asks first. The daemon asks nothing: it sleeps the agent, drops its held mail, frees
   its server and its port, deletes every job for it and removes the record, with no undo. The
   confirm is the whole guard, so it lives here. */

import { useRef, useState } from 'react'
import useBackClose from '../../shell/useBackClose'
import Dot from '../../primitives/Dot'
import Face from '../../primitives/Face'
import cc from '../../data/api'
import { askApp, inApp } from '../../shell/native'

export default function AgentHead({ agent, onNote, onGone, onWoke }) {
  const menu = useRef(null)
  const [busy, setBusy] = useState(false)
  // Whether the fold is open, heard from the <details> itself, so back can close it.
  const [open, setOpen] = useState(false)
  useBackClose(open, shut)

  function shut() {
    if (menu.current) { menu.current.open = false }
  }

  async function run(what, go, gone) {
    shut()
    setBusy(true)
    try {
      await go()
      if (gone) { onGone() }
    } catch (e) {
      onNote(what + ' failed: ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  /* A new name for the card, the chat and the phone's ring. The id behind it never changes, so
     folders, jobs and messages to it keep working. */
  function rename() {
    shut()
    const name = (window.prompt('New name for ' + agent.name, agent.name) || '').trim()
    if (!name || name === agent.name) { return }
    run('Rename', function () { return cc.updateAgent(agent.id, { name: name }) })
  }

  /* Float mode: the agent's face on the edge over every app, its chat a tap away (Android app
     only). The app says when it may not draw over other apps. */
  function float() {
    run('Float', async function () {
      const got = await askApp('float', { agent: agent.id, name: agent.name })
      if (got && got.why === 'overlay') { onNote('Float needs "Display over other apps" for Ergo in Android settings.') }
    })
  }

  function forget() {
    shut()
    const sure = window.confirm(
      'Forget ' + agent.name + '? It is put to sleep, its held messages, its jobs and its record '
      + 'go, and there is no undo. The conversation file on disk is untouched.',
    )
    if (sure) { run('Forget', function () { return cc.forget(agent.id) }, true) }
  }

  /* The menu offers only the move that changes something: Wake to a sleeping agent, Sleep to an
     awake one. */
  const awake = agent.state === 'green'

  return (
    <div className="agent-head">
      <Dot state={awake ? 'up' : 'sleep'} />
      <Face id={agent.id} name={agent.name} size={28} aria-hidden="true" />
      <h1 id="chat-h">{agent.name}</h1>

      <details
        className="ch-menu"
        ref={menu}
        onToggle={function (e) { setOpen(e.currentTarget.open) }}
      >
        <summary className="iconbtn ch-kebab" aria-label="Agent settings" role="button">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
            <circle cx="12" cy="5" r="1.4"></circle>
            <circle cx="12" cy="12" r="1.4"></circle>
            <circle cx="12" cy="19" r="1.4"></circle>
          </svg>
        </summary>

        <div className="ch-menu-body">
          {awake ? null : <button
            type="button"
            className="ch-menu-item"
            disabled={busy}
            onClick={function () { run('Wake', async function () { await cc.wake(agent.id); if (onWoke) { await onWoke() } }) }}
          >
            Wake
          </button>}
          {awake ? <button
            type="button"
            className="ch-menu-item"
            disabled={busy}
            onClick={function () { run('Sleep', function () { return cc.sleep(agent.id) }) }}
          >
            Sleep
          </button> : null}
          <button
            type="button"
            className="ch-menu-item"
            disabled={busy}
            onClick={function () { run('Stop', function () { return cc.stop(agent.id) }) }}
          >
            Stop task
          </button>
          <button
            type="button"
            className="ch-menu-item"
            disabled={busy}
            onClick={rename}
          >
            Rename
          </button>
          {inApp() ? <button
            type="button"
            className="ch-menu-item"
            disabled={busy}
            onClick={float}
          >
            Float
          </button> : null}
          <button
            type="button"
            className="ch-menu-item"
            disabled={busy}
            onClick={function () { run('Hide', function () { return cc.hide(agent.id) }, true) }}
          >
            Hide
          </button>
          <button
            type="button"
            className="ch-menu-item ch-danger"
            disabled={busy}
            onClick={forget}
          >
            Forget
          </button>
        </div>
      </details>
    </div>
  )
}
