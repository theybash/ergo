/* The Codex card: whether Codex is signed in, and a sign-in that works from the phone.
   Sign in asks the daemon for a one-time code. You open the page anywhere, type the
   code, and Codex finishes on the laptop. The end comes back on the "codex" event topic, so this
   card never polls. Which account it is never shows here. */

import { useEffect, useState } from 'react'
import Badge from '../../primitives/Badge'
import { codexLogin, codexLoginStart, eventsUrl } from '../../data/api'

export default function CodexCard({ item }) {
  const [st, setSt] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(function () {
    let live = true
    codexLogin().then(function (s) { if (live) setSt(s) }, function (e) { if (live) setErr(e.message) })
    return function () { live = false }
  }, [])

  const pending = st && st.pending
  useEffect(function () {
    if (!pending) { return undefined }
    const es = new EventSource(eventsUrl() + '?topics=codex')
    es.addEventListener('codex', function (ev) {
      try { setSt(JSON.parse(ev.data)) } catch (e) { /* a broken frame changes nothing */ }
    })
    return function () { es.close() }
  }, [!!pending])

  function start() {
    setBusy(true)
    setErr('')
    codexLoginStart().then(setSt, function (e) { setErr(e.message) }).finally(function () { setBusy(false) })
  }

  const signed = st && st.signed_in
  const badge = !st ? 'quiet' : signed ? 'ok' : 'needs'
  const state = !st ? 'checking' : signed ? 'signed in' : 'signed out'

  return (
    <article className="int-card">
      <div className="int-top">
        <span className="int-mark" aria-hidden="true">{item.mark}</span>
        <h3 className="int-name">{item.name}</h3>
        <Badge kind={badge}>{state}</Badge>
      </div>
      <p className="int-use">{item.use}</p>
      <p className="int-scope">{signed ? 'With ' + st.how : ''}</p>

      {pending ? (
        <div className="cx-code">
          <p className="cx-step">Open the page, sign in, and type this code. It runs out in 15 minutes.</p>
          <p className="cx-big">{pending.code}</p>
          <a className="btn sm" href={pending.url} target="_blank" rel="noreferrer">Open the sign-in page</a>
        </div>
      ) : null}
      {err || (st && st.error && !pending) ? <p className="int-scope cx-err">{err || st.error}</p> : null}

      {pending ? null : (
        <div className="int-foot">
          <button type="button" className={'btn sm' + (signed ? ' quiet' : '')} disabled={busy || !st} onClick={start}>
            {busy ? 'Starting...' : signed ? 'Sign in again' : 'Sign in'}
          </button>
        </div>
      )}
    </article>
  )
}
