/* A card an agent put in the chat with its show tool. The agent sends a kind and typed data, and
   this draws it. No HTML comes from the agent and nothing is framed: every word is drawn as text.

   The call sits in the session file with its whole input, so history draws the card again the
   same way, the way a question is drawn. The tool checked the data against its kind already.
   This reads it loosely all the same, because a card that cannot be read is better as nothing
   than as a broken screen.

     choice  a question and 2 to 5 options. A tap sends one machine line to the agent.
     tasks   a few of his tasks, with tick and move. Those go to the task routes, not the agent.
     report  a title, short sections and an optional link.

   A feed_post call is drawn here too, as the same card the feed shows. The feed's own copy of the
   post wins when it is loaded, because it has the link's picture and the time. */

import { useEffect, useState } from 'react'
import ChoiceCard from './ChoiceCard'
import Post from '../feed/Post'
import usePosts from '../../data/postsStore'
import useFleet from '../../data/useFleet'
import { POST_TOOL } from './show'
import TasksCard from './TasksCard'
import './show.css'

function read(row) {
  let input = null
  try { input = JSON.parse(row.inputRaw || 'null') } catch { input = null }
  if (!input || typeof input !== 'object') { return null }
  let data = input.data
  if (typeof data === 'string') {
    try { data = JSON.parse(data) } catch { data = null }
  }
  if (!data || typeof data !== 'object') { return null }
  return { kind: input.kind, data }
}

/* A link may only go to the web or to a page of Ergo's own. */
function safeUrl(url) {
  const s = String(url || '')
  return /^(https?:\/\/\S+|\/\S*)$/.test(s) ? s : ''
}

function Report({ data }) {
  const sections = Array.isArray(data.sections) ? data.sections : []
  const link = data.link && typeof data.link === 'object' ? data.link : null
  const url = link ? safeUrl(link.url) : ''
  return (
    <section className="sw-card" aria-label={String(data.title || 'Report')}>
      <h3 className="sw-title">{String(data.title || '')}</h3>
      {sections.map(function (s, i) {
        if (!s || typeof s !== 'object') { return null }
        return (
          <div className="sw-section" key={i}>
            <p className="sw-head">{String(s.head || '')}</p>
            <p className="sw-body">{String(s.body || '')}</p>
          </div>
        )
      })}
      {url
        ? <a className="sw-link" href={url} target="_blank" rel="noopener noreferrer">{String(link.label || url)}</a>
        : null}
    </section>
  )
}

function part(value) {
  if (typeof value === 'string') {
    try { value = JSON.parse(value) } catch { return null }
  }
  return value && typeof value === 'object' ? value : null
}

function FeedCard({ row, agent }) {
  const posts = usePosts()
  const fleet = useFleet()
  const [toast, setToast] = useState('')
  useEffect(function () {
    if (!toast) { return undefined }
    const t = window.setTimeout(function () { setToast('') }, 1600)
    return function () { window.clearTimeout(t) }
  }, [toast])
  let input = null
  try { input = JSON.parse(row.inputRaw || 'null') } catch { input = null }
  if (!input || typeof input !== 'object' || !input.text) { return null }
  const id = agent
  const rec = (fleet.agents || []).find(function (a) { return a.id === id })
  const text = String(input.text).trim()
  const held = posts.list.find(function (p) { return p.agent === id && p.text === text })
  const media = part(input.media)
  const post = held || {
    agent: id, text: text, kind: input.kind || '', link: part(input.link), draft: part(input.draft),
    card: part(input.card), media: Array.isArray(media) ? media : media ? [media] : [],
  }
  return (
    <>
      <Post post={post} agent={rec} inChat onToast={setToast} />
      <div className={'fd-toast' + (toast ? ' on' : '')} role="status">{toast}</div>
    </>
  )
}

export default function ShowCard({ row, agent }) {
  if (row.tool === POST_TOOL) { return <FeedCard row={row} agent={agent} /> }
  const card = read(row)
  if (!card) { return null }
  if (card.kind === 'choice') { return <ChoiceCard data={card.data} row={row} agent={agent} /> }
  if (card.kind === 'tasks') { return <TasksCard data={card.data} /> }
  if (card.kind === 'report') { return <Report data={card.data} /> }
  return null
}
