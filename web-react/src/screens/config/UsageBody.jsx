/* The Usage section, heading and all. What the plan, the voice and each agent have
   used, from one call to GET /api/usage. Asked for when Config opens, when the app comes back to
   the front, and when you press Reload. No timer.

   A daemon without the route, or one that is down, gets one faint line and nothing else. This
   section must never take the Config screen with it. */

import { useCallback, useEffect, useRef, useState } from 'react'
import cc from '../../data/api'
import VoiceMemory from './VoiceMemory'
import BubbleTheme from './BubbleTheme'

// Above this share of a limit the bar takes the waiting tone. Below it the bar stays grey.
const HIGH = 0.8

/* A reset time in the owner's own clock. Today it is the time alone, another day adds the day. */
function when(seconds) {
  if (typeof seconds !== 'number') { return '' }
  const d = new Date(seconds * 1000)
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (d.toDateString() === new Date().toDateString()) { return time }
  return d.toLocaleDateString('en-GB', { weekday: 'short' }) + ' ' + time
}

function share(used, cap) {
  if (typeof used !== 'number' || typeof cap !== 'number' || cap <= 0) { return 0 }
  return Math.max(0, Math.min(1, used / cap))
}

function minutes(n) {
  return n >= 10 || Number.isInteger(n) ? String(Math.round(n)) : n.toFixed(1)
}

function tokens(n) {
  if (typeof n !== 'number') { return '' }
  if (n < 1000) { return String(n) }
  if (n < 1e6) { return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + 'k' }
  if (n < 1e9) { return (n / 1e6).toFixed(1) + 'M' }
  return (n / 1e9).toFixed(2) + 'B'
}

function dollars(n) {
  return '$' + n.toFixed(2)
}

/* One line of words and a thin bar under it. */
function Meter({ label, figure, rest, fraction, stale }) {
  return (
    <div className="cf-meter">
      <p className="cf-meter-line">
        <span className="cf-meter-label">{label}</span>
        <span className="cf-num">{figure}</span>
        {rest ? <span className="cf-meter-rest">{rest}</span> : null}
        {stale ? <span className="cf-faint">stale</span> : null}
      </p>
      <div className={'cf-bar' + (fraction > HIGH ? ' cf-bar-high' : '')} aria-hidden="true">
        <span style={{ width: (fraction * 100).toFixed(1) + '%' }} />
      </div>
    </div>
  )
}

/* What the OpenAI key cost: today and this week, then each use. Prices are the published ones;
   the voice's backend model on a call is billed by OpenAI inside the session and is not here. */
const USE_NAMES = { calls: 'Calls', dictation: 'Dictation', 'voice note': 'Voice notes',
  speech: 'Speech', vision: 'Pictures', memory: 'Call memory' }

function OpenAI({ o }) {
  if (!o) { return null }
  const uses = Object.entries(o.by_use || {}).sort(function (a, b) { return b[1].week - a[1].week })
  return (
    <div className="cf-meter">
      <p className="cf-meter-line">
        <span className="cf-meter-label">OpenAI</span>
        <span className="cf-num">{dollars(o.today || 0)}</span>
        <span className="cf-meter-rest">today, {dollars(o.week || 0)} this week</span>
      </p>
      {uses.map(function ([use, u]) {
        return (
          <p className="cf-meter-line" key={use}>
            <span className="cf-meter-rest">{USE_NAMES[use] || use}</span>
            <span className="cf-num">{dollars(u.today)}</span>
            <span className="cf-meter-rest">today, {dollars(u.week)} week{u.minutes ? ', ' + minutes(u.minutes) + ' min' : ''}</span>
          </p>
        )
      })}
    </div>
  )
}

/* An engine's plan limits: Claude, then Codex. A window the plan does
   not have, like Codex's 5 hours on a business plan, has no row. */
function Plan({ plan, name }) {
  if (!plan) {
    return name === 'Claude'
      ? <p className="cf-faint cf-u-line">Claude usage appears after the next reply in any session.</p>
      : null
  }
  const rows = [[name + ', 5 hours', plan.five_hour], [name + ', week', plan.seven_day]]
    .filter(function ([, w]) { return !!w })
  return rows.map(function ([label, w]) {
    const pct = w && typeof w.used_percentage === 'number' ? w.used_percentage : null
    const reset = w ? when(w.resets_at) : ''
    return (
      <Meter
        key={label}
        label={label}
        figure={pct === null ? 'no reading' : Math.round(pct) + '%'}
        rest={reset ? 'resets ' + reset : ''}
        fraction={pct === null ? 0 : share(pct, 100)}
        stale={plan.stale}
      />
    )
  })
}

function Agents({ agents }) {
  if (!agents.length) { return null }
  return (
    <>
      <table className="cf-u-table">
        <thead>
          <tr>
            <th scope="col">Agent</th>
            <th scope="col" className="cf-u-model">Model</th>
            <th scope="col">API equivalent</th>
            <th scope="col">Input tokens</th>
            <th scope="col">Output tokens</th>
          </tr>
        </thead>
        <tbody>
          {agents.map(function (a) {
            return (
              <tr key={a.id}>
                <td className="cf-u-name">{a.name}</td>
                <td className="cf-u-model cf-num">{a.model || ''}</td>
                <td className="cf-num">
                  {typeof a.cost_usd === 'number' ? dollars(a.cost_usd) : <span className="cf-faint">no record</span>}
                </td>
                <td className="cf-num">{tokens(a.tokens_in)}</td>
                <td className="cf-num">{tokens(a.tokens_out)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="cf-faint cf-u-line">Since each agent's session began. Input counts the conversation re-read from cache on every turn, which is most of it. API equivalent; on the Max plan this is not billed. Insights shows input and output added.</p>
    </>
  )
}

export default function UsageBody() {
  const [data, setData] = useState(null)
  const [failed, setFailed] = useState(false)

  const [busy, setBusy] = useState(false)
  const live = useRef(true)

  const load = useCallback(function () {
    setBusy(true)
    cc.usage().then(
      function (d) { if (live.current) { setData(d); setFailed(false) } },
      function () { if (live.current) { setFailed(true) } },
    ).finally(function () { if (live.current) { setBusy(false) } })
  }, [])

  useEffect(function () {
    live.current = true
    // The app stays open in the background for hours, so a read made on opening goes stale.
    // Coming back to the front reads again. A timer would keep asking over mobile data.
    function back() { if (document.visibilityState === 'visible') { load() } }
    load()
    document.addEventListener('visibilitychange', back)
    return function () { live.current = false; document.removeEventListener('visibilitychange', back) }
  }, [load])

  const head = (
    <div className="cf-u-head">
      <h2 className="cf-head" id="cf-usage-h">Usage</h2>
      <button type="button" className="btn sm quiet" disabled={busy} onClick={load}>
        {busy ? 'Reading' : 'Reload'}
      </button>
    </div>
  )

  if (!data) {
    return <>{head}<p className="cf-faint cf-u-line">{failed ? 'Usage is not available yet.' : 'Reading usage.'}</p></>
  }

  const voice = data.voice || {}

  return (
    <>
    {head}
    <div className="cf-usage">
      <Plan plan={data.plan} name="Claude" />
      <Plan plan={data.codex} name="Codex" />
      {typeof voice.cap_minutes === 'number' ? (
        <Meter
          label="Voice"
          figure={minutes(voice.used_minutes || 0) + ' of ' + minutes(voice.cap_minutes)}
          rest="min today"
          fraction={share(voice.used_minutes || 0, voice.cap_minutes)}
        />
      ) : null}
      <VoiceMemory />
      <BubbleTheme />
      <OpenAI o={data.openai} />
      <Agents agents={Array.isArray(data.agents) ? data.agents : []} />
    </div>
    </>
  )
}
