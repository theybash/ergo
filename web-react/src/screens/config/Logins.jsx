/* The saved logins, and a form to add one. A login belongs to one agent and one site: the agent can
   sign in with it, and never sees what is in it. Nothing here shows a user name or a password once
   saved; the list is agent, title and site, and a login is changed by removing it and adding it again.

   The password and the authenticator secret go to the laptop over the tailnet and straight into the
   vault, whose key is sealed in the laptop's TPM chip. */

import { useEffect, useState } from 'react'

const BASE = (import.meta.env && import.meta.env.VITE_CC_API) || ''

async function api(path, method = 'GET', body) {
  const r = await fetch(BASE + path, {
    method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const d = await r.json().catch(function () { return {} })
  if (!r.ok) { throw new Error(d.error || 'That did not work.') }
  return d
}

const EMPTY = { agent: '', title: '', url: '', username: '', password: '', totp: '' }

export default function Logins({ fleet }) {
  const [rows, setRows] = useState(null)
  const [form, setForm] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  function load() {
    api('/api/vault').then(function (d) {
      setRows([].concat((d.personal || []).map(function (r) { return { ...r, side: 'personal' } }),
        (d.work || []).map(function (r) { return { ...r, side: 'work' } })))
    }, function (e) { setError(e.message) })
  }
  useEffect(load, [])

  function set(k) { return function (e) { const v = e.target.value; setForm(function (f) { return { ...f, [k]: v } }) } }

  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      await api('/api/vault', 'POST', form)
      setForm(null)
      load()
    } catch (err) { setError(err.message) }
    setBusy(false)
  }

  async function remove(r) {
    setError('')
    try { await api('/api/vault/remove', 'POST', { agent: r.agent, title: r.title }); load() } catch (err) { setError(err.message) }
  }

  const agents = (fleet && fleet.agents) || []
  return (
    <div className="cf-logins">
      {rows && rows.length === 0 ? <p className="cf-note">No logins yet. An agent asks you to take over when it meets a login page; save one here and it can sign in by itself.</p> : null}
      {(rows || []).map(function (r) {
        return (
          <div className="cf-login" key={r.side + r.agent + r.title}>
            <span className="cf-login-what"><b>{r.title}</b> <span>{r.agent}</span></span>
            <span className="cf-login-site">{(function () { try { return new URL(r.url).hostname } catch { return r.url } })()}</span>
            <button type="button" className="chip" onClick={function () { remove(r) }}>Remove</button>
          </div>
        )
      })}
      {form ? (
        <form className="cf-login-form" onSubmit={save} autoComplete="off">
          <select value={form.agent} onChange={set('agent')} required>
            <option value="">Which agent</option>
            {agents.map(function (a) { return <option key={a.id} value={a.id}>{a.name}</option> })}
          </select>
          <input placeholder="Title, like LinkedIn" value={form.title} onChange={set('title')} required />
          <input placeholder="Login page, like https://www.linkedin.com/login" value={form.url} onChange={set('url')} inputMode="url" required />
          <input placeholder="User name or email" value={form.username} onChange={set('username')} autoCapitalize="off" />
          <input placeholder="Password" type="password" value={form.password} onChange={set('password')} required autoComplete="new-password" />
          <input placeholder="Authenticator secret (optional)" value={form.totp} onChange={set('totp')} autoCapitalize="characters" />
          <div className="cf-login-acts">
            <button type="button" className="chip" onClick={function () { setForm(null) }}>Cancel</button>
            <button type="submit" className="chip" disabled={busy}>{busy ? 'Saving...' : 'Save login'}</button>
          </div>
        </form>
      ) : <button type="button" className="chip" onClick={function () { setForm(EMPTY) }}>Add a login</button>}
      {error ? <p className="fl-err">{error}</p> : null}
    </div>
  )
}
