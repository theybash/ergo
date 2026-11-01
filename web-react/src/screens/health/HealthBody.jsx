/* Health. How the machine is, how the link is, and how many agents are awake on it.

   Everything the daemon measures about the link comes in pairs, so it is shown in pairs. The
   gateway figure is the one that goes bad on this laptop and the internet figure beside it is
   what makes the first one mean anything: 8 ms to the router is fine beside 12 ms out and a
   broken router beside 200 ms.

   The speed test is a button and never a timer. It moves about five megabytes over the link it
   is measuring, so a timer would spend the bandwidth it is reporting on. */

import { useCallback, useEffect, useState } from 'react'
import cc from '../../data/api'
import './health.css'

/* The daemon takes the measurement at most every twenty seconds and hands the same one to
   every watcher, so asking on this timer is three pings in total, not three per browser. */
const POLL = 20000

function ms(v) {
  return v == null ? 'not measured' : v + ' ms'
}

function pct(v) {
  return v == null ? 'not measured' : Math.round(v) + '%'
}

/* The Health readout without its screen head, so the Config screen can hold the same thing
   under its own heading. The caller owns useFleet() and passes it in, so a page that shows
   agents and health together opens one event stream, not two. */
export default function HealthBody({ fleet }) {
  const [net, setNet] = useState(null)
  const [snap, setSnap] = useState(null)
  const [testing, setTesting] = useState(false)
  const [error, setError] = useState('')

  const readNet = useCallback(async function () {
    try {
      setNet(await cc.network())
    } catch {
      /* The daemon will be back. The last reading stays on the screen rather than blanking. */
    }
  }, [])

  useEffect(function () {
    /* oxlint-disable-next-line react/set-state-in-effect -- the daemon is an external system,
     * and the first reading has to come from somewhere. */
    readNet()
    const t = window.setInterval(readNet, POLL)
    return function () { window.clearInterval(t) }
  }, [readNet])

  /* /api/state already carries the machine snapshot and the event stream keeps it current, so
     this one fetch is only for the first paint before state has landed. */
  useEffect(function () {
    let gone = false
    cc.health().then(function (h) { if (!gone) { setSnap(h) } }).catch(function () {})
    return function () { gone = true }
  }, [])

  async function runSpeedTest() {
    setTesting(true)
    setError('')
    try {
      const r = await cc.speedTest()
      /* The daemon writes the result into its own cached snapshot, so the next poll carries it
         too. This puts it on the screen without waiting twenty seconds for that. */
      setNet(function (prev) { return prev ? { ...prev, speed: r } : prev })
    } catch (e) {
      setError(e.message)
    } finally {
      setTesting(false)
    }
  }

  const h = fleet.health || snap
  const limits = fleet.limits || {}
  const agents = fleet.agents || []
  const lat = (net && net.latency) || {}
  const wifi = (net && net.wifi) || {}
  const drops = (net && net.drops) || {}
  const speed = net && net.speed
  const gate = lat.gateway || {}
  const out = lat.internet || {}

  const disks = (h && h.disks) || []
  const root = disks.find(function (d) { return d.mount === '/' }) || disks[0]
  const memory = (h && h.memory) || null
  const battery = (h && h.battery) || null
  const pressure = (h && h.pressure) || {}

  const memLow = memory && memory.available_gb < (limits.mem_avail_gb == null ? 1.5 : limits.mem_avail_gb)
  const diskLow = root && (100 - root.percent) < (limits.disk_free_pct == null ? 10 : limits.disk_free_pct)
  const battLow = battery && battery.percent != null && !battery.plugged
    && battery.percent < (limits.battery_pct == null ? 20 : limits.battery_pct)
  const pressHigh = pressure.memory != null && pressure.memory > (limits.pressure == null ? 1 : limits.pressure)

  const awake = agents.filter(function (a) { return a.state === 'green' }).length
  const working = agents.filter(function (a) { return a.working }).length
  const watchers = agents.reduce(function (n, a) { return n + (a.watchers || 0) }, 0)
  const procs = (h && h.agents) || []
  const rss = procs.reduce(function (n, p) { return n + (p.rss_mb || 0) }, 0)

  return (
    <>
      <p className="hl-verdict">
        {net ? (net.verdict || 'The daemon measured the link and wrote no verdict.')
          : 'Measuring the link.'}
      </p>

      {error ? <p className="hl-err">{error}</p> : null}
      {fleet.error ? <p className="hl-err">{fleet.error}</p> : null}

      <section className="section" aria-labelledby="hl-link-h">
        <div className="section-head">
          <h3 id="hl-link-h">The link</h3>
          <span className="hint">Measured every twenty seconds, once for everybody watching.</span>
        </div>

        <div className="fleet">
          <article className="agentcard">
            <div className="hl-pair">
              <div className="hl-half">
                <p className="hl-label">To the router</p>
                <p className="hl-big">{ms(gate.avg_ms)}</p>
                <p className="hl-sub">{'worst ' + ms(gate.max_ms) + ', lost ' + pct(gate.loss_pct)}</p>
              </div>
              <div className="hl-half">
                <p className="hl-label">Out to the internet</p>
                <p className="hl-big">{ms(out.avg_ms)}</p>
                <p className="hl-sub">{'worst ' + ms(out.max_ms) + ', lost ' + pct(out.loss_pct)}</p>
              </div>
            </div>
            <p className="hl-note">
              {'Router at ' + ((net && net.gateway && net.gateway.address) || 'an address the daemon could not read')}
            </p>
          </article>

          <article className="agentcard">
            <div className="hl-pair">
              <div className="hl-half">
                <p className="hl-label">Link rate</p>
                <p className="hl-big">{wifi.rate_mbit == null ? 'not measured' : wifi.rate_mbit + ' Mb/s'}</p>
                <p className="hl-sub">{'on ' + (wifi.ssid || 'an unnamed network')}</p>
              </div>
              <div className="hl-half">
                <p className="hl-label">Signal</p>
                <p className="hl-big">{pct(wifi.signal_pct)}</p>
                <p className="hl-sub">
                  {(wifi.level_dbm == null ? 'strength not measured' : wifi.level_dbm + ' dBm')
                    + ', ' + (wifi.retries == null ? 'retries not counted' : wifi.retries + ' retries')}
                </p>
              </div>
            </div>
            <p className="hl-note">
              {'Radio ' + (wifi.device || (net && net.gateway && net.gateway.device) || 'not named')}
            </p>
          </article>

          <article className="agentcard">
            <div className="hl-pair">
              <div className="hl-half">
                <p className="hl-label">Down</p>
                <p className="hl-big">
                  {speed && speed.down_mbit != null ? speed.down_mbit + ' Mb/s' : 'not tested'}
                </p>
              </div>
              <div className="hl-half">
                <p className="hl-label">Up</p>
                <p className="hl-big">
                  {speed && speed.up_mbit != null ? speed.up_mbit + ' Mb/s' : 'not tested'}
                </p>
              </div>
            </div>
            {speed && speed.error ? <p className="hl-err">{'The last test failed: ' + speed.error}</p> : null}
            <div className="hl-foot">
              <button type="button" className="btn sm" disabled={testing} onClick={runSpeedTest}>
                {testing ? 'Testing' : 'Run speed test'}
              </button>
              <p className="hl-note">
                It moves about five megabytes over the link it measures, so it runs when you
                press it and never on a timer.
              </p>
            </div>
          </article>
        </div>
      </section>

      <section className="section" aria-labelledby="hl-drops-h">
        <div className="section-head">
          <h3 id="hl-drops-h">Drops since boot</h3>
          <span className="hint">Counted from the machine starting, not from today.</span>
        </div>

        <article className="agentcard">
          <div className="hl-pair">
            <div className="hl-half">
              <p className="hl-label">Carrier changes</p>
              <p className="hl-big">{drops.carrier_changes == null ? 'not counted' : drops.carrier_changes}</p>
              <p className="hl-sub">The radio lost and found the access point.</p>
            </div>
            <div className="hl-half">
              <p className="hl-label">NetworkManager disconnects</p>
              <p className="hl-big">{drops.disconnects == null ? 'not counted' : drops.disconnects}</p>
              <p className="hl-sub">The whole connection went down and came back.</p>
            </div>
          </div>
        </article>
      </section>

      <section className="section" aria-labelledby="hl-machine-h">
        <div className="section-head">
          <h3 id="hl-machine-h">The machine</h3>
          <span className="hint">{h && h.host ? h.host : 'waiting for the first reading'}</span>
        </div>

        <div className="tablewrap">
          <table className="hl-table">
            <thead>
              <tr><th>What</th><th>Now</th><th>Turns bad at</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>Memory available</td>
                <td className={memLow ? 'hl-num hl-bad' : 'hl-num'}>
                  {memory ? memory.available_gb.toFixed(1) + ' GB' : 'not measured'}
                </td>
                <td className="hl-num">{(limits.mem_avail_gb == null ? 1.5 : limits.mem_avail_gb) + ' GB'}</td>
              </tr>
              <tr>
                <td>Battery</td>
                <td className={battLow ? 'hl-num hl-bad' : 'hl-num'}>
                  {battery && battery.percent != null
                    ? Math.round(battery.percent) + '%' + (battery.plugged ? ', on mains power' : ', on battery')
                    : 'no battery reported'}
                </td>
                <td className="hl-num">
                  {(limits.battery_pct == null ? 20 : limits.battery_pct) + '% off mains'}
                </td>
              </tr>
              <tr>
                <td>{root ? 'Disk free on ' + root.mount : 'Disk free'}</td>
                <td className={diskLow ? 'hl-num hl-bad' : 'hl-num'}>
                  {root ? root.free_gb.toFixed(0) + ' GB, ' + Math.round(100 - root.percent) + '% left' : 'not measured'}
                </td>
                <td className="hl-num">{(limits.disk_free_pct == null ? 10 : limits.disk_free_pct) + '% left'}</td>
              </tr>
              <tr>
                <td>Memory pressure</td>
                <td className={pressHigh ? 'hl-num hl-bad' : 'hl-num'}>
                  {pressure.memory == null ? 'not measured' : pressure.memory.toFixed(2)}
                </td>
                <td className="hl-num">{limits.pressure == null ? 1 : limits.pressure}</td>
              </tr>
              <tr>
                <td>Processor</td>
                <td className="hl-num">
                  {h && h.cpu ? Math.round(h.cpu.percent) + '% of ' + h.cpu.cores + ' cores' : 'not measured'}
                </td>
                <td>never</td>
              </tr>
              <tr>
                <td>Machine up</td>
                <td className="hl-num">
                  {h && h.uptime_s != null ? (h.uptime_s / 3600).toFixed(1) + ' hours' : 'not measured'}
                </td>
                <td>not a limit</td>
              </tr>
              <tr>
                <td>Agent processes</td>
                <td className="hl-num">
                  {procs.length + (procs.length === 1 ? ' process, ' : ' processes, ') + rss + ' MB'}
                </td>
                <td>not a limit</td>
              </tr>
            </tbody>
          </table>
        </div>

        <p className="hl-note">
          A busy processor is doing its job, so it never turns bad. Machine up is counted from
          the last boot. Agent processes are every claude the daemon started, with the memory
          they hold between them.
        </p>
        <p className="hl-note">
          The daemon reports the machine, not itself. Its own process id, its own uptime and the
          folder it serves the page from are not in this reading, so they are not shown.
        </p>
      </section>

      <section className="section" aria-labelledby="hl-agents-h">
        <div className="section-head">
          <h3 id="hl-agents-h">The agents</h3>
          <span className="hint">A watcher is a browser attached to an agent terminal.</span>
        </div>

        <article className="agentcard">
          <div className="hl-pair">
            <div className="hl-half">
              <p className="hl-label">Awake</p>
              <p className="hl-big">{awake + ' of ' + agents.length}</p>
              <p className="hl-sub">{working + (working === 1 ? ' is working' : ' are working')}</p>
            </div>
            <div className="hl-half">
              <p className="hl-label">Watchers</p>
              <p className="hl-big">{watchers}</p>
              <p className="hl-sub">Terminals only. A chat is not a watcher.</p>
            </div>
          </div>
        </article>
      </section>
    </>
  )
}
