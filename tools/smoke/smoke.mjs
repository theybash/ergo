// Smoke test of the console. It opens each screen as a phone and reports what went wrong.
// It only navigates and takes screenshots. It never types, clicks or sends anything, and it
// answers any request that is not a read itself, so no write reaches the server.
// Usage: node tools/smoke/smoke.mjs [--base URL] [--out DIR] [--port N] [--wait MS] [--screens a,b,c] [--expect REGEX] [--strict]
import { spawn } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const SCREENS = ['fleet', 'chat', 'insights', 'memory', 'config', 'recruit', 'integrations', 'health']

// Faults that are normal and not worth a FAIL. Each is a regex on the reported line.
// The greeting answers 404 until the owner records one, and the shell asks for it on every screen.
const EXPECTED = [/^404 GET .*\/api\/voice\/greeting$/]

function args() {
  const a = { base: 'http://127.0.0.1:8085', out: '/tmp/smoke-' + Date.now(), port: 9471, wait: 6000, screens: SCREENS, expected: [...EXPECTED] }
  const v = process.argv.slice(2)
  for (let i = 0; i < v.length; i++) {
    const k = v[i], x = v[i + 1]
    if (k === '--base') { a.base = x.replace(/\/$/, ''); i++ }
    else if (k === '--out') { a.out = x; i++ }
    else if (k === '--port') { a.port = Number(x); i++ }
    else if (k === '--wait') { a.wait = Number(x); i++ }
    else if (k === '--screens') { a.screens = x.split(',').filter(Boolean); i++ }
    else if (k === '--expect') { a.expected.push(new RegExp(x)); i++ }
    else if (k === '--strict') { a.expected = [] }
    else if (k === '-h' || k === '--help') { console.log('node tools/smoke/smoke.mjs [--base URL] [--out DIR] [--port N] [--wait MS] [--screens a,b] [--expect REGEX] [--strict]'); process.exit(0) }
    else { console.error('unknown argument ' + k); process.exit(2) }
  }
  return a
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

async function startChrome(port) {
  const dir = `/tmp/cc-smoke-${port}`
  rmSync(dir, { recursive: true, force: true })
  // No back-forward cache: a cached page keeps its streams open, and after six screens Chrome
  // has no connection left to the host, so the seventh screen hung until the retry.
  const ch = spawn('google-chrome', ['--headless=new', `--user-data-dir=${dir}`, '--password-store=basic',
    '--no-first-run', '--no-default-browser-check', '--disable-features=BackForwardCache', `--remote-debugging-port=${port}`,
    '--user-agent=Mozilla/5.0 (Linux; Android 14) Mobile', 'about:blank'], { stdio: 'ignore' })
  // Wait for the debug port, up to 15 seconds.
  for (let i = 0; i < 60; i++) {
    try {
      const tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json()
      const page = tabs.find(t => t.type === 'page')
      if (page) { return { ch, dir, url: page.webSocketDebuggerUrl } }
    } catch {}
    await sleep(250)
  }
  ch.kill()
  throw new Error('Chrome did not open its debug port ' + port)
}

async function connect(url, sink) {
  const ws = new WebSocket(url)
  await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = bad })
  let id = 0
  const wait = {}
  const reqs = {}
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data)
    if (m.id && wait[m.id]) { wait[m.id](m); delete wait[m.id]; return }
    const p = m.params || {}
    if (m.method === 'Fetch.requestPaused') {
      // A screen can write on open: the chat marks an agent read and sets its mode. Only reads
      // reach the server. Any other method gets a fake empty 200 here and goes in blocked.
      const rq = p.request
      if (['GET', 'HEAD', 'OPTIONS'].includes(rq.method)) {
        ws.send(JSON.stringify({ id: ++id, method: 'Fetch.continueRequest', params: { requestId: p.requestId } }))
      } else {
        ws.send(JSON.stringify({ id: ++id, method: 'Fetch.fulfillRequest', params: { requestId: p.requestId, responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }], body: Buffer.from('{}').toString('base64') } }))
        if (sink.now) { sink.now.blocked.push(rq.method + ' ' + rq.url) }
      }
      return
    }
    const s = sink.now
    if (!s) { return }
    if (m.method === 'Runtime.consoleAPICalled' && ['error', 'warning', 'assert'].includes(p.type)) {
      s.console.push(p.type + ': ' + p.args.map(a => a.value ?? a.description ?? '').join(' ').slice(0, 400))
    } else if (m.method === 'Runtime.exceptionThrown') {
      const d = p.exceptionDetails
      s.exceptions.push((d.exception?.description || d.text || '').slice(0, 600))
    } else if (m.method === 'Log.entryAdded' && p.entry.level === 'error' && !p.entry.text.startsWith('Failed to load resource')) {
      // A failed resource shows in the network list already, so only other log errors land here.
      s.console.push('log: ' + p.entry.text.slice(0, 300) + (p.entry.url ? ' ' + p.entry.url : ''))
    } else if (m.method === 'Network.requestWillBeSent') {
      reqs[p.requestId] = { url: p.request.url, method: p.request.method }
    } else if (m.method === 'Network.responseReceived') {
      if (p.response.status >= 400) {
        s.network.push(`${p.response.status} ${reqs[p.requestId]?.method || 'GET'} ${p.response.url}`)
      }
    } else if (m.method === 'Network.loadingFailed') {
      const r = reqs[p.requestId] || { url: '?', method: '?' }
      // A stream or fetch cut off when the page moves on is not a fault. List it apart.
      const line = `${p.errorText} ${r.method} ${r.url}`
      if (p.canceled || p.errorText === 'net::ERR_ABORTED') { s.canceled.push(line) } else { s.network.push(line) }
    }
  }
  const cmd = (method, params = {}, t = 10000) => new Promise((ok) => {
    const i = ++id
    wait[i] = (m) => ok(m.result ?? null)
    ws.send(JSON.stringify({ id: i, method, params }))
    setTimeout(() => { if (wait[i]) { delete wait[i]; ok(null) } }, t)
  })
  return { ws, cmd }
}

async function main() {
  const a = args()
  mkdirSync(a.out, { recursive: true })
  const chrome = await startChrome(a.port)
  const sink = { now: null }
  const results = []
  try {
    const { ws, cmd } = await connect(chrome.url, sink)
    await cmd('Runtime.enable'); await cmd('Log.enable'); await cmd('Page.enable'); await cmd('Network.enable')
    await cmd('Fetch.enable', { patterns: [{ urlPattern: '*' }] })
    await cmd('Network.setBypassServiceWorker', { bypass: true })
    await cmd('Emulation.setDeviceMetricsOverride', { width: 400, height: 850, deviceScaleFactor: 2, mobile: true })
    await cmd('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
    for (const screen of a.screens) {
      const url = `${a.base}/?screen=${encodeURIComponent(screen)}`
      const r = { screen, url, console: [], exceptions: [], network: [], canceled: [], blocked: [], shot: null, landed: null, empty: false }
      // Leave the last page first, so its streams close before this screen starts counting.
      await cmd('Page.navigate', { url: 'about:blank' }); await sleep(300)
      sink.now = r
      let nav = await cmd('Page.navigate', { url }, 20000)
      if (!nav || nav.errorText) {
        r.retried = (nav?.errorText || 'timeout') + ' on the first try'
        nav = await cmd('Page.navigate', { url }, 20000)
      }
      if (!nav || nav.errorText) { r.network.push('navigate failed: ' + (nav?.errorText || 'timeout') + ' ' + url) }
      await sleep(a.wait)
      const ev = await cmd('Runtime.evaluate', { expression: `({ href: location.href, text: (document.getElementById('root') || document.body).innerText.trim().length })`, returnByValue: true })
      const v = ev?.result?.value
      r.landed = v?.href || null
      r.empty = !v || v.text === 0 || !String(v.href).startsWith(a.base)
      r.expected = r.network.filter(x => a.expected.some(re => re.test(x)))
      r.network = r.network.filter(x => !r.expected.includes(x))
      const pic = await cmd('Page.captureScreenshot', { format: 'png' }, 20000)
      if (pic?.data) { r.shot = join(a.out, screen + '.png'); writeFileSync(r.shot, Buffer.from(pic.data, 'base64')) }
      sink.now = null
      results.push(r)
      const bad = r.console.length + r.exceptions.length + r.network.length + (r.empty ? 1 : 0)
      console.log(`${bad ? 'FAIL' : 'ok  '} ${screen}: ${r.exceptions.length} exceptions, ${r.console.length} console, ${r.network.length} network${r.empty ? ', blank page' : ''}${r.expected.length ? `, ${r.expected.length} expected` : ''}${r.blocked.length ? `, ${r.blocked.length} writes blocked` : ''}${r.retried ? ', retried: ' + r.retried : ''}`)
      for (const x of r.exceptions) { console.log('    exception: ' + x.split('\n')[0]) }
      for (const x of r.console) { console.log('    console ' + x) }
      for (const x of r.network) { console.log('    network: ' + x) }
    }
    ws.close()
  } finally {
    chrome.ch.kill()
    await sleep(500)
    rmSync(chrome.dir, { recursive: true, force: true })
  }
  writeFileSync(join(a.out, 'report.json'), JSON.stringify({ base: a.base, at: new Date().toISOString(), results }, null, 2))
  const failed = results.filter(r => r.console.length || r.exceptions.length || r.network.length || r.empty)
  console.log(`\n${results.length - failed.length} of ${results.length} screens clean. Report and screenshots in ${a.out}`)
  process.exit(failed.length ? 1 : 0)
}

main().catch((e) => { console.error(e.message || e); process.exit(2) })
