/* The feed (from user feedback: "X-style is good ... a standardized feed feature which all
   agents can use"). What the agents show him, newest first, drawn like a timeline. A post is not
   a task: the x on it hides it, with Undo for a moment, and nothing else marks it. Each post is
   drawn by Post.jsx, the same card the agent's chat shows.

   Opening the feed counts everything in it as seen, for the count on the tab. */

import { useEffect, useRef, useState } from 'react'
import useFleet from '../../data/useFleet'
import usePosts, { loadOlder, markSeen } from '../../data/postsStore'
import { showsIn } from '../../shell/views'
import Post from './Post'
import MailDrafts from '../mail/MailDrafts'
import WaPosts from './WaPosts'
import './feed.css'
import useScrollMemory from '../../shell/scrollMemory'

export default function Feed({ onScreen, view }) {
  // Its own place on the page, not the one the last screen left (shell/scrollMemory.js).
  useScrollMemory('screen:feed', true)
  const posts = usePosts()
  const fleet = useFleet()
  const [toast, setToast] = useState(null)
  const end = useRef(null)

  useEffect(function () { markSeen() }, [posts.list.length])

  useEffect(function () {
    if (!toast) { return undefined }
    const t = window.setTimeout(function () { setToast(null) }, toast.undo ? 4000 : 1600)
    return function () { window.clearTimeout(t) }
  }, [toast])

  // The end of the list coming near loads the next older days. A new observer after each page:
  // it reports at once, so a short page that leaves the end in view loads the next one too.
  useEffect(function () {
    if (!posts.more || !end.current) { return undefined }
    const io = new IntersectionObserver(function (es) {
      if (es.some(function (e) { return e.isIntersecting })) { loadOlder() }
    }, { rootMargin: '800px 0px' })
    io.observe(end.current)
    return function () { io.disconnect() }
  }, [posts.more, posts.next])

  function say(text, undo) { setToast({ text: text, undo: undo || null }) }

  const byId = new Map((fleet.agents || []).map(function (a) { return [a.id, a] }))
  // The view scopes the feed the way it scopes the fleet. In Work, Demo or Personal, a post from
  // an agent the fleet does not list (hidden, or gone) is left out: Demo must never show it.
  const shown = posts.list.filter(function (p) {
    if (p.state === 'hidden') { return false }
    const a = byId.get(p.agent)
    return !view || view === 'all' || (!!a && showsIn(a, view))
  })

  return (
    <section className="fd">
      <h1 className="fd-head">Feed</h1>
      <MailDrafts keep={function (d) {
        const a = byId.get(d.agent)
        return !view || view === 'all' || (!!a && showsIn(a, view))
      }} />
      <WaPosts keep={function (d) {
        const a = byId.get(d.agent)
        return !view || view === 'all' || (!!a && showsIn(a, view))
      }} />
      {posts.loaded && !shown.length ? (
        <p className="fd-empty">Nothing yet. When an agent finds something worth your eye, it posts it here.</p>
      ) : null}
      {posts.error && !shown.length ? <p className="fd-empty">The feed did not load: {posts.error}</p> : null}
      {shown.map(function (p) {
        return <Post key={p.id} post={p} agent={byId.get(p.agent)} onScreen={onScreen} onToast={say} />
      })}
      <div ref={end} aria-hidden="true" />
      <div className={'fd-toast' + (toast ? ' on' : '')} role="status">
        {toast ? toast.text : ''}
        {toast && toast.undo ? (
          <button type="button" onClick={function () { toast.undo(); setToast(null) }}>Undo</button>
        ) : null}
      </div>
    </section>
  )
}
