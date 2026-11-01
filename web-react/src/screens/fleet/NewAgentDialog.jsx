/* The new agent dialog. Name, folder, engine, permissions, and whether it is the master.

   No model field. The owner took it off: the model is a PATCH on the record that takes effect
   at the next wake, and choosing one at the moment of creation put a decision in front of a
   person who had not met the agent yet. The daemon's own default stands.

   The folder is the guard. If it does not exist the daemon answers {needs_dir, path} rather
   than failing, so the full resolved path is shown and asked about. That is the moment a
   mistyped tilde is caught. */

import { useEffect, useRef, useState } from 'react'
import useBackClose from '../../shell/useBackClose'
import cc from '../../data/api'

const MODES = [
  ['acceptEdits', 'accept edits, ask to run'],
  ['plan', 'plan only, change nothing'],
  ['bypassPermissions', 'ask for nothing'],
]

export default function NewAgentDialog({ onClose, onMade }) {
  const box = useRef(null)
  // Back closes the dialog and leaves you on the Fleet.
  useBackClose(true, onClose)
  const [name, setName] = useState('')
  const [cwd, setCwd] = useState('')
  const [perm, setPerm] = useState('bypassPermissions')
  const [master, setMaster] = useState(false)
  const [engine, setEngine] = useState('claude')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(function () {
    const el = box.current
    if (el && !el.open) { el.showModal() }
  }, [])

  async function submit(ev) {
    ev.preventDefault()
    if (busy) { return }
    setBusy(true)
    setError('')
    const body = { name: name.trim(), cwd: cwd.trim(), permission_mode: perm }
    if (master) { body.role = 'master' }
    if (engine !== 'claude') { body.engine = engine }
    try {
      let made = await cc.createAgent(body)
      /* Not a failure. A question: the folder is not there yet, and here is where it would go. */
      if (made && made.needs_dir) {
        const ok = window.confirm(made.path + ' does not exist yet.\n\nCreate it and start the agent there?')
        if (!ok) { setBusy(false); return }
        made = await cc.createAgent({ ...body, create_dir: true })
      }
      if (onMade) { onMade(made) }
      onClose()
    } catch (e) {
      setError(e.message)
      setBusy(false)
    }
  }

  return (
    <dialog className="fl-dlg" ref={box} onCancel={onClose} aria-labelledby="fl-new-h">
      <form onSubmit={submit}>
        <div className="fl-dlg-top">
          <h2 id="fl-new-h">New agent</h2>
          <button type="button" className="btn sm quiet" onClick={onClose} aria-label="Close this dialog">Close</button>
        </div>

        <label className="fl-lab" htmlFor="fl-new-name">Name</label>
        <input
          id="fl-new-name" className="field" required autoComplete="off" placeholder="job search"
          value={name} onChange={function (ev) { setName(ev.target.value) }}
        />

        <label className="fl-lab" htmlFor="fl-new-cwd">Folder</label>
        <input
          id="fl-new-cwd" className="field" required autoComplete="off"
          placeholder="~/work/thing or /full/path"
          value={cwd} onChange={function (ev) { setCwd(ev.target.value) }}
        />

        <label className="fl-lab" htmlFor="fl-new-perm">Permissions</label>
        <select
          id="fl-new-perm" className="field"
          value={perm} onChange={function (ev) { setPerm(ev.target.value) }}
        >
          {MODES.map(function (m) {
            return <option key={m[0]} value={m[0]}>{m[1]}</option>
          })}
        </select>

        {/* Codex and OpenCode run one process per message on their own CLIs. Neither can ask in
            the chat, so the permission mode sets what they may do: plan reads only, accept edits
            writes in the folder, ask for nothing is full access. Both start on their first chat
            message, not with a terminal. */}
        <label className="fl-lab" htmlFor="fl-new-engine">Engine</label>
        <select
          id="fl-new-engine" className="field"
          value={engine} onChange={function (ev) { setEngine(ev.target.value) }}
        >
          <option value="claude">Claude Code</option>
          <option value="codex">Codex</option>
          <option value="opencode">OpenCode</option>
        </select>

        <label className="fl-lab" htmlFor="fl-new-role">Role</label>
        <select
          id="fl-new-role" className="field"
          value={master ? 'master' : ''}
          onChange={function (ev) { setMaster(ev.target.value === 'master') }}
        >
          <option value="">no role</option>
          <option value="master">master</option>
        </select>

        <p className="fl-note">
          Two agents may share a folder. They are two conversations, not two copies of one.
          There is at most one master, so naming a second one is refused.
        </p>
        {error ? <p className="fl-err">{error}</p> : null}

        <div className="fl-act">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn primary" disabled={busy}>Start</button>
        </div>
      </form>
    </dialog>
  )
}
