// A real React log in Chrome, with local history fixtures and no daemon requests.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from '../web-react/node_modules/vite/dist/node/index.js'

const root = fileURLToPath(new URL('../web-react/', import.meta.url))
const fixture = await mkdtemp(join(root, '.scroll-check-'))
const profile = await mkdtemp(join(tmpdir(), 'ergo-scroll-'))
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
let server, chrome, ws
try {
  await writeFile(join(fixture, 'index.html'), `<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{margin:0}.turn{min-height:120px;margin:16px}.turn:nth-child(3n){min-height:190px}
.recap{height:70px}.chat-scroll{overflow:visible;height:auto}
@media(min-width:720px){.chat-scroll{height:600px;overflow-y:auto}}
</style><div id="root"></div><script type="module" src="./fixture.jsx"></script>`)
  await writeFile(join(fixture, 'fixture.jsx'), `
import React, { useEffect, useLayoutEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Log from '../src/screens/chat/Log.jsx'
import useScrollMemory from '../src/shell/scrollMemory'
import '../src/screens/chat/chat.css'
const row = n => ({ key: 'message-' + n, kind: 'text', who: n % 2 ? 'agent' : 'operator',
  text: 'Message ' + n, at: '2026-10-30T10:00:00Z' })
function Fixture() {
  const [rows, setRows] = useState(Array.from({length: 12}, (_, i) => row(100 + i)))
  // A later layout effect models new history content settling after Log measures it.
  useLayoutEffect(() => {
    document.querySelector('.log').style.paddingTop = (rows.length - 12) * 3.65 + 'px'
  }, [rows.length])
  window.requests = window.requests || 0
  return <Log id="fixture" active rows={rows} hasMore cards={[]} calls={[]} shownJobs={[]}
    recap={{kicker:'Recap',body:'A fixed introduction'}}
    loadOlder={() => {
      window.requests++
      window.releaseOlder = () => setRows(old => Array.from({length: 20}, (_, i) => row(Number(old[0].key.split('-')[1]) - 20 + i)).concat(old))
    }} />
}
// Deliver a queued detached observer callback after Fleet restores its position.
// This makes the real Back race deterministic instead of depending on frame timing.
const NativeObserver = window.ResizeObserver
window.ResizeObserver = class extends NativeObserver {
  constructor(callback) { super(callback); this.callback = callback }
  observe(node) {
    if (node.classList.contains('chat-scroll')) window.oldPin = () => this.callback([], this)
    super.observe(node)
  }
}
function Fleet() {
  useScrollMemory('screen:fleet', true)
  useLayoutEffect(() => { requestAnimationFrame(() => {
    if (window.oldPin) { window.oldPin(); window.pinDelivered = true }
  }) }, [])
  return <div id="fleet" style={{height:1520}}><button style={{marginTop:320}} onClick={() => {
    history.pushState({}, '', '#chat'); window.dispatchEvent(new PopStateEvent('popstate')); window.scrollTo(0,0)
  }}>Open chat</button></div>
}
function BackFixture() {
  const [chat, setChat] = useState(location.hash === '#chat')
  useEffect(() => {
    const route = () => setChat(location.hash === '#chat')
    window.addEventListener('popstate', route)
    return () => window.removeEventListener('popstate', route)
  }, [])
  return chat ? <Log id="back" active rows={Array.from({length:12}, (_, i) => row(i))}
    cards={[]} calls={[]} shownJobs={[]} /> : <Fleet />
}
createRoot(document.getElementById('root')).render(location.search ? <BackFixture /> : <Fixture />)
`)
  server = await createServer({ root, server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' })
  await server.listen()
  chrome = spawn(process.argv[2], ['--headless=new', '--no-sandbox', '--disable-gpu',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' })
  let port
  for (let i = 0; i < 100 && !port; i++) {
    try { port = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0] } catch { await sleep(50) }
  }
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json()
  ws = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl)
  await new Promise(resolve => ws.addEventListener('open', resolve))
  let sequence = 0
  const pending = new Map()
  ws.addEventListener('message', event => {
    const message = JSON.parse(event.data)
    if (pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id) }
  })
  const send = (method, params = {}) => new Promise(resolve => {
    const id = ++sequence; pending.set(id, resolve); ws.send(JSON.stringify({id, method, params}))
  })
  const evaluate = async expression => {
    const response = await send('Runtime.evaluate', {expression, awaitPromise:true, returnByValue:true})
    assert.ok(!response.error && !response.result.exceptionDetails, JSON.stringify(response))
    return response.result.result.value
  }
  const url = `http://127.0.0.1:${server.httpServer.address().port}/${fixture.split('/').pop()}/index.html`
  for (const width of [400, 1000]) {
    await send('Emulation.setDeviceMetricsOverride', {width, height:900, deviceScaleFactor:1, mobile:width===400})
    await send('Page.navigate', {url})
    for (let i = 0; i < 100; i++) {
      if (await evaluate('!!document.querySelector(".turn")')) break
      await sleep(50)
    }
    await sleep(700)
    for (const y of [0, 280]) {
      const result = await evaluate(`(async () => {
        const pause=ms=>new Promise(r=>setTimeout(r,ms));
        const el=document.querySelector('.chat-scroll');const own=getComputedStyle(el).overflowY==='auto';
        const move=y=>own ? el.scrollTop=y : window.scrollTo(0,y);
        const start=window.requests;
        move(${y});document.body.dispatchEvent(new WheelEvent('wheel',{deltaY:-100,bubbles:true}));await pause(60);
        const requested=window.requests-start;
        // The hand keeps moving while the request is outstanding.
        move(${y ? 180 : 0});await pause(60);
        const top=own?el.getBoundingClientRect().top:0;
        const node=Array.from(el.querySelectorAll('.turn')).find(n=>n.getBoundingClientRect().bottom>top+1);
        const before=node.getBoundingClientRect().top;
        window.releaseOlder();await pause(150);
        return {requested,connected:node.isConnected,shift:node.getBoundingClientRect().top-before,
          text:node.textContent};
      })()`)
      assert.equal(result.requested, 1, JSON.stringify({width,y,result}))
      assert.equal(result.connected, true, JSON.stringify({width,y,result}))
      assert.ok(Math.abs(result.shift) <= 1, JSON.stringify({width,y,result}))
    }
  }
  console.log('Phone and desktop: date/recap skipped, message survives prepend, drift <= 1 px, prefetch at 280 px.')
  await send('Emulation.setDeviceMetricsOverride', {width:400, height:900, deviceScaleFactor:1, mobile:true})
  await send('Page.navigate', {url: url + '?back'})
  for (let i = 0; i < 100; i++) {
    if (await evaluate('!!document.querySelector("#fleet button")')) break
    await sleep(50)
  }
  const back = await evaluate(`(async () => {
    const pause=ms=>new Promise(r=>setTimeout(r,ms));
    scrollTo(0,300);await pause(100);const before=scrollY;
    document.querySelector('#fleet button').click();await pause(800);
    scrollTo(0,scrollY-100);await pause(700);scrollTo(0,document.documentElement.scrollHeight);await pause(100);
    const chat=scrollY;history.back();await pause(200);
    return {before,chat,after:scrollY,fleet:!!document.querySelector('#fleet'),delivered:window.pinDelivered};
  })()`)
  assert.equal(back.before, 300, JSON.stringify(back))
  assert.ok(back.chat > 300 && back.fleet && back.delivered, JSON.stringify(back))
  assert.equal(back.after, back.before, JSON.stringify(back))
  console.log('Phone Back: Fleet stays at 300 px even when the detached chat observer fires.')
} finally {
  if (ws) ws.close()
  if (chrome) { chrome.kill(); await new Promise(resolve => chrome.once('exit', resolve)) }
  if (server) await server.close()
  await rm(fixture, {recursive:true,force:true})
  await rm(profile, {recursive:true,force:true})
}
