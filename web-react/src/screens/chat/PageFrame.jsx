/* A page the agent wrote, in a frame of its own.

   The frame is sandboxed WITHOUT allow-same-origin, which gives the page an origin that matches
   nothing. It cannot read this console, and one agent's page cannot read another's. Adding
   allow-same-origin would let the page remove its own sandbox, so the two are never combined.
   That pair is the whole security of this feature and it is not negotiable.

   One frame per page, mounted when its tab is shown and dropped when you leave it. A frame left
   in the document keeps running, keeps its timers, and keeps asking for data about an agent
   nobody is looking at.

   THE BRIDGE
   A page has no network of its own, so everything it wants comes through postMessage. Four
   answers are given here from state the console already holds, so a page showing which agents
   exist costs no request. Three are routes. Anything else is refused by name.

   What a page may never do is reach the network itself, because then a stranger's code would be
   talking to the stranger's server from inside this browser. */

import { useEffect, useRef } from 'react'
import cc from '../../data/api'
import { pageUrl } from './urls'

/* The colours the wrapper page paints itself with, read off this document so a page changes
   with the theme toggle instead of being a white rectangle in a dark console. */
const TOKENS = ['--bg', '--surface', '--border', '--text', '--text-dim', '--text-faint',
  '--accent', '--accent-bg', '--up', '--down', '--waiting', '--low']

function tokens() {
  const style = getComputedStyle(document.documentElement)
  const out = {}
  for (const name of TOKENS) { out[name] = style.getPropertyValue(name).trim() }
  return out
}

export default function PageFrame({ id, page, fleet }) {
  const frame = useRef(null)
  const src = pageUrl(id, page.slug)
  // Held in a ref so the listener always reads the newest fleet without being rebuilt, which
  // would drop a request that was in flight.
  const state = useRef(fleet)
  useEffect(function () { state.current = fleet }, [fleet])

  useEffect(function () {
    const el = frame.current
    if (!el) { return undefined }

    /* The four answered here, from state the shell already has. */
    const local = {
      'platform.state': function () {
        return {
          agents: (state.current.agents || []).map(function (a) {
            return { id: a.id, name: a.name, state: a.state, working: a.working }
          }),
        }
      },
      'platform.agents': function () {
        return (state.current.agents || []).map(function (a) {
          return { id: a.id, name: a.name, role: a.role }
        })
      },
      'platform.health': function () { return state.current.health },
      'platform.limits': function () { return state.current.limits },
    }

    async function answer(name, args) {
      if (local[name]) { return local[name]() }
      /* Data by name. The daemon works out whether that name is a file the agent wrote or a
         route on its own server, and the page never learns which, so an agent can move from one
         to the other without touching the page it already wrote. */
      if (name === 'platform.data') { return cc.dataByName(id, (args && args.name) || '') }
      if (name === 'platform.available') { return cc.agentData(id) }
      /* The agent id travels so a fetch can be traced back to the page that wanted it in the
         log. It is not a permission. Every page may ask for every address. */
      if (name === 'platform.fetch') {
        return cc.api('/api/fetch', 'POST', { ...(args || {}), agent: id })
      }
      throw new Error(name + ' is not something a page can ask the platform for')
    }

    async function heard(ev) {
      if (ev.source !== el.contentWindow) { return }
      const ask = ev.data || {}
      if (!ask.id) { return }
      let reply
      try {
        reply = { id: ask.id, ok: true, value: await answer(ask.name, ask.args) }
      } catch (e) {
        reply = { id: ask.id, ok: false, error: e.message }
      }
      el.contentWindow.postMessage(reply, '*')
    }

    /* The wrapper page waits for this before it loads the agent's own module. Without it the
       frame sits blank, which reads as a broken tab rather than as a page still loading. */
    function boot() {
      el.contentWindow.postMessage(
        { boot: true, module: '/view/' + id + '/view.js', tokens: tokens() }, '*',
      )
    }

    window.addEventListener('message', heard)
    el.addEventListener('load', boot)
    return function () {
      window.removeEventListener('message', heard)
      el.removeEventListener('load', boot)
    }
  }, [id, page.slug])

  return (
    <div className="page-frame">
      <div className="pf-head">
        <span className="pf-name">{page.title || page.slug}</span>
        <span className="pf-tag">built by the agent</span>
        <a className="btn sm pf-open" href={src} target="_blank" rel="noopener noreferrer">
          Open full
        </a>
      </div>

      <div className="pf-body">
        <iframe
          className="ch-frame"
          ref={frame}
          title={page.title || page.slug}
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          src={src}
        ></iframe>
      </div>
    </div>
  )
}
