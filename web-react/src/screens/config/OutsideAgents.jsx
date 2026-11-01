/* Outside agents: your agents on your other machines, which hand work to Ergo's agents and
   take it back over the tailnet, through one MCP server at /mcp. Each is added here by name.
   Its token is shown once, with the lines that connect Claude Code and Codex, and never again:
   the daemon keeps only a hash. */

import { useEffect, useState } from 'react'
import { api } from '../../data/api'

function seen(t) {
  if (!t) { return 'never connected' }
  const at = new Date(t * 1000)
  if (at.toDateString() === new Date().toDateString()) {
    return 'seen ' + at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  }
  return 'seen ' + at.toLocaleDateString([], { day: 'numeric', month: 'short' })
}

export default function OutsideAgents() {
  const [rows, setRows] = useState(null)
  const [form, setForm] = useState(null)
  const [made, setMade] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  function load() {
    api('/api/outside').then(function (d) { setRows(d.outside || []) }, function () { setRows([]) })
  }
  useEffect(load, [])

  async function add(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      setMade(await api('/api/outside', 'POST', form))
      setForm(null)
      load()
    } catch (err) { setError(err.message) }
    setBusy(false)
  }

  async function remove(r) {
    setError('')
    try { await api('/api/outside/' + r.id, 'DELETE'); load() } catch (err) { setError(err.message) }
  }

  const url = window.location.origin + '/mcp'
  const command = made
    ? `claude mcp add --transport http --scope user ergo ${url} --header "Authorization: Bearer ${made.token}"`
    : ''
  const codex = made
    ? `# ~/.codex/config.toml\n[mcp_servers.ergo]\nurl = "${url}"\nbearer_token_env_var = "ERGO_TOKEN"\n\n# ~/.zshrc\nexport ERGO_TOKEN=${made.token}`
    : ''

  function copy(text, which) {
    try { navigator.clipboard.writeText(text).then(function () { setCopied(which) }) } catch { /* select it by hand */ }
  }

  return (
    <div className="cf-logins">
      {rows && rows.length === 0 && !made ? (
        <p className="cf-note">None yet. An agent on your MacBook or a server can hand work to these agents and take it back: list them, message them, send and read files.</p>
      ) : null}
      {(rows || []).map(function (r) {
        return (
          <div className="cf-login" key={r.id}>
            <span className="cf-login-what"><b>{r.name}</b></span>
            <span className="cf-login-site">{seen(r.seen)}</span>
            <button type="button" className="chip" onClick={function () { remove(r) }}>Remove</button>
          </div>
        )
      })}
      {made ? (
        <div className="cf-outside-made">
          <p className="cf-note">On {made.name}, once. The token is shown only now.</p>
          <p className="cf-note">Claude Code</p>
          <pre className="cf-outside-cmd">{command}</pre>
          <p className="cf-note">Codex</p>
          <pre className="cf-outside-cmd">{codex}</pre>
          <div className="cf-login-acts">
            <button type="button" className="chip" onClick={function () { copy(command, 'claude') }}>{copied === 'claude' ? 'Copied' : 'Copy Claude'}</button>
            <button type="button" className="chip" onClick={function () { copy(codex, 'codex') }}>{copied === 'codex' ? 'Copied' : 'Copy Codex'}</button>
            <button type="button" className="chip" onClick={function () { setMade(null); setCopied(false) }}>Done</button>
          </div>
        </div>
      ) : null}
      {form ? (
        <form className="cf-login-form" onSubmit={add} autoComplete="off">
          <input placeholder="Name, like MacBook Claude" value={form.name}
            onChange={function (e) { setForm({ ...form, name: e.target.value }) }} required />
          <div className="cf-login-acts">
            <button type="button" className="chip" onClick={function () { setForm(null) }}>Cancel</button>
            <button type="submit" className="chip" disabled={busy}>{busy ? 'Adding...' : 'Add'}</button>
          </div>
        </form>
      ) : (made ? null : <button type="button" className="chip" onClick={function () { setForm({ name: '', side: 'personal' }) }}>Add an outside agent</button>)}
      {error ? <p className="fl-err">{error}</p> : null}
    </div>
  )
}
