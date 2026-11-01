/* One post, drawn the same in the feed and in the agent's own chat (from user feedback:
   "the cards should also be able to be displayed in chat, it's elegant really").

   A post is something an agent shows him: a suggestion, a find, a draft. It is not a task, so
   there is nothing to finish. The x hides it from the feed, and so does a right swipe; that is all.

     link    a quoted card for the source, the whole card a link. With a file, a markdown file
             the agent wrote in its outbox, the card opens it whole over the screen to read,
             drawn by the Files tab's own viewer
     draft   words for him to send himself, inside the quoted card, with Copy
     card    one big headline, for a pick or a result
     media   up to four images or videos in a grid, like X. A video plays muted inline until he
             taps its sound on. A file comes from the agent's outbox through the daemon

   Under each post: Copy (with a draft), Read (with a file), Open (with a link) and, in the feed, Chat (the agent's
   own chat, with the post quoted above the message box). In the chat the card is framed, has no
   face, and no Chat or x: he is already there. */

import { useState } from 'react'
import { createPortal } from 'react-dom'
import cc from '../../data/api'
import { setMark } from '../../data/postsStore'
import { pinPost } from '../../data/taskPin'
import { FileView } from '../chat/FilesTab'
import ImageView from '../chat/ImageView'
import Swipe from '../../primitives/Swipe'
import '../chat/chat.css'
import './feed.css'
import Face, { faceColour } from '../../primitives/Face'

export { faceColour }

export function ago(at) {
  const s = Math.max(0, Date.now() / 1000 - (at || 0))
  if (s < 60) { return 'now' }
  if (s < 3600) { return Math.floor(s / 60) + 'm' }
  const d = new Date(at * 1000)
  const today = new Date()
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  if (d.toDateString() === today.toDateString()) { return hm }
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' + hm
}

function mediaSrc(post, m) {
  return m.url || cc.fileUrl(post.agent, m.file)
}

/* A tap opens the picture in the app's own view, which zooms and closes on back. Followed as a
   link, it downloaded in the phone app. */
function stop(e) { e.stopPropagation() }

function Media({ post }) {
  const [open, setOpen] = useState(null)
  const items = post.media || []
  if (!items.length) { return null }
  return (
    <div className={'fd-media n' + items.length}>
      {open ? createPortal(
        // On the body, so no swiped or blurred card around it pins it inside the card. A portal
        // still bubbles to the card in React, so the view's touches stop here.
        <div onPointerDown={stop} onPointerMove={stop} onPointerUp={stop} onClick={stop}>
          <ImageView src={open.src} alt={open.alt} onClose={function () { setOpen(null) }} />
        </div>, document.body) : null}
      {items.map(function (m, i) {
        const src = mediaSrc(post, m)
        return m.kind === 'video' ? (
          <video key={i} src={src} controls muted playsInline preload="metadata" aria-label={m.alt || 'video'}></video>
        ) : (
          <a key={i} href={src} target="_blank" rel="noreferrer"
            onClick={function (e) { e.preventDefault(); e.stopPropagation(); setOpen({ src: src, alt: m.alt || '' }) }}>
            <img src={src} alt={m.alt || ''} loading="lazy" />
          </a>
        )
      })}
    </div>
  )
}

function host(url) {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return '' }
}

const ICONS = {
  copy: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"></rect><path d="M4 16V6a2 2 0 0 1 2-2h10"></path></svg>,
  open: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"></path></svg>,
  chat: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-8 8H4l2-3a8 8 0 1 1 15-5z"></path></svg>,
  read: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"></path><path d="M14 3v5h5M9 13h6M9 17h6"></path></svg>,
  hide: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>,
}

/* What a post is about, as a small glyph by the agent's name, so the eye sorts the feed before
   it reads. From the post's kind first, then its link's site. */
const SOURCES = {
  hn: { label: 'Hacker News', glyph: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3.5" y="3.5" width="17" height="17" rx="3"></rect><path d="M8 7.5l4 5.5 4-5.5M12 13v4"></path></svg> },
  linkedin: { label: 'LinkedIn', glyph: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3.5" y="3.5" width="17" height="17" rx="3"></rect><path d="M8 10.5V17M8 7.5v.01M11.5 17v-6.5M11.5 13.5a2.5 2.5 0 0 1 5 0V17"></path></svg> },
  news: { label: 'News', glyph: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 5h11v13a2 2 0 0 0 2 2H6a1 1 0 0 1-1-1V5z"></path><path d="M16 9h3v9a2 2 0 0 1-2 2M8 9h5M8 12.5h5M8 16h3"></path></svg> },
  work: { label: 'Work', glyph: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3.5" y="7.5" width="17" height="12" rx="2"></rect><path d="M9 7.5V6a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 6v1.5M3.5 12.5h17"></path></svg> },
}

export function sourceOf(post, _agent) {
  const kind = String(post.kind || '').toLowerCase()
  const site = host((post.link && post.link.url) || (post.card && post.card.url) || '')
  if (/\bhn\b|hacker ?news/.test(kind) || site === 'news.ycombinator.com') { return 'hn' }
  if (/linkedin/.test(kind) || site.endsWith('linkedin.com')) { return 'linkedin' }
  if (/\bnews\b/.test(kind)) { return 'news' }
  if (/\bwork\b/.test(kind)) { return 'work' }
  return ''
}

export default function Post({ post, agent, onScreen, onToast, inChat }) {
  const name = (agent && agent.name) || post.agent
  const source = sourceOf(post, agent)
  const link = post.link
  const draft = post.draft
  const card = post.card
  const file = (link && link.file) || ''
  const [reading, setReading] = useState(false)
  const open = (link && link.url) || (card && card.url) || (draft && draft.url) || ''

  function copy() {
    const text = draft.text
    const done = function () { onToast('Draft copied') }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { onToast('Could not copy') })
    } else {
      onToast('Could not copy')
    }
  }

  function hide() {
    setMark(post, 'hidden')
    cc.markPost(post.id, 'hidden').then(
      function () { onToast('Hidden', function () { unhide() }) },
      function () { setMark(post, post.state || ''); onToast('Not hidden') })
  }

  function unhide() {
    setMark(post, '')
    cc.markPost(post.id, '').catch(function () { setMark(post, 'hidden') })
  }

  const shown = (
    <article className={'fd-post' + (inChat ? ' in-chat' : '')}>
      {inChat ? null : (
        <Face
          as="button"
          type="button"
          className="fd-face"
          id={post.agent}
          name={name}
          onClick={function () { onScreen('chat', '#/a/' + post.agent) }}
          aria-label={'Open ' + name + "'s chat"}
        />
      )}
      <div className="fd-body">
        <p className="fd-who">
          <b>{name}</b>
          {source ? <i className="fd-kind" title={SOURCES[source].label} aria-label={SOURCES[source].label}>{SOURCES[source].glyph}</i> : null}
          {post.kind ? <span>· {post.kind}</span> : null}
          {post.at ? <span>· {ago(post.at)}</span> : null}
          {inChat || !post.id ? null : (
            <button type="button" className="fd-x" onClick={hide} aria-label="Hide this post">{ICONS.hide}</button>
          )}
        </p>
        <p className="fd-text">{post.text}</p>

        <Media post={post} />

        {link || draft ? (
          <div className="fd-quote">
            {link && file ? (
              <button type="button" className="fd-src" onClick={function () { setReading(true) }}>
                <span className="fd-host">{name + ' wrote'}</span>
                {link.title ? <span className="fd-title">{link.title}</span> : null}
                {link.meta ? <span className="fd-meta">{link.meta}</span> : null}
              </button>
            ) : link ? (
              <a className="fd-src" href={link.url || undefined} target="_blank" rel="noreferrer">
                {link.image ? <img className="fd-pic" src={link.image} alt="" loading="lazy" /> : null}
                {link.url ? <span className="fd-host">{host(link.url)}</span> : null}
                {link.title ? <span className="fd-title">{link.title}</span> : null}
                {link.meta ? <span className="fd-meta">{link.meta}</span> : null}
              </a>
            ) : null}
            {draft ? (
              <div className={'fd-draft' + (link ? '' : ' alone')}>
                <p className="fd-lbl">{draft.to ? <>Your reply to <b>{draft.to}</b></> : 'Your draft'}</p>
                <p className="fd-words">{draft.text}</p>
              </div>
            ) : null}
          </div>
        ) : null}

        {card ? (
          <div className="fd-card">
            <p className="fd-headline">{card.headline}</p>
            {card.sub ? <p className="fd-sub">{card.sub}</p> : null}
            {card.url ? (
              <a className="fd-go" href={card.url} target="_blank" rel="noreferrer">{(card.label || 'Open') + ' →'}</a>
            ) : null}
          </div>
        ) : null}

        {draft || file || open || !inChat ? (
          <div className="fd-acts">
          {draft ? <button type="button" onClick={copy}>{ICONS.copy}<span>Copy</span></button> : null}
          {file ? <button type="button" onClick={function () { setReading(true) }}>{ICONS.read}<span>Read</span></button> : null}
          {open ? <a href={open} target="_blank" rel="noreferrer">{ICONS.open}<span>Open</span></a> : null}
          {inChat ? null : (
            <button type="button" onClick={function () { pinPost(post, name); onScreen('chat', '#/a/' + post.agent) }}>{ICONS.chat}<span>Chat</span></button>
          )}
          </div>
        ) : null}
      </div>
    </article>
  )
  // A right swipe hides it, the same as the x, with Undo.
  // The file reader sits outside the swipe, so a drag inside it never moves the post.
  const reader = file ? <FileView agent={post.agent} name={reading ? file : ''} onClose={function () { setReading(false) }} /> : null
  if (inChat || !post.id) { return <>{shown}{reader}</> }
  return <><Swipe label="Hide" onDone={hide}>{shown}</Swipe>{reader}</>
}

