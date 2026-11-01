/* The bottom tab bar. The CSS fixes it to the bottom edge under 720px and hides it above.
   Five tabs in thumb reach: Fleet, Chat, Feed, Insights and More, which opens Memory and Config.
   A count on Fleet when an agent wants you, on Feed for posts since he last looked, and on
   Insights when an agent offers a task. Each count is a small red numeral and nothing around it.

   Five and only five, because a sixth tab would make every tab too narrow to hit. Config holds
   Integrations and Health, so neither needs a tab of its own. Opened by ?screen=integrations
   or ?screen=health, More is the one lit.

   The count comes from the Fleet screen's own store, so the badge here and the number on the
   Fleet list can never disagree. */

import { useRef, useState, useSyncExternalStore } from 'react'
import useGlider from './useGlider'
import { tabOf } from './tabs'
/* The Fleet screen owns the count. src/shell/needsFallback.js is the stand in, if this file
   ever goes missing again. */
import { subscribe, getPending } from '../screens/fleet/needsStore'
import { subscribe as fleetSubscribe, snapshot as fleetSnapshot } from '../data/fleetStore'
import useTasks from '../data/tasksStore'
import { showsIn } from './views'
import usePosts from '../data/postsStore'

const TABS = [
  {
    id: 'fleet',
    label: 'Fleet',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="3" y="3" width="7" height="7" rx="2"></rect>
        <rect x="14" y="3" width="7" height="7" rx="2"></rect>
        <rect x="3" y="14" width="7" height="7" rx="2"></rect>
        <rect x="14" y="14" width="7" height="7" rx="2"></rect>
      </svg>
    ),
  },
  {
    id: 'chat',
    label: 'Chat',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M21 12a8 8 0 0 1-8 8H4l2-3a8 8 0 1 1 15-5z"></path>
      </svg>
    ),
  },
  {
    id: 'feed',
    label: 'Feed',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M4 5h16M4 12h16M4 19h10"></path>
      </svg>
    ),
  },
  {
    id: 'insights',
    label: 'Insights',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M4 19V5M4 16l5-5 4 3 7-7"></path>
      </svg>
    ),
  },
  {
    id: 'more',
    label: 'More',
    icon: (
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <circle cx="5" cy="12" r="1.8"></circle>
        <circle cx="12" cy="12" r="1.8"></circle>
        <circle cx="19" cy="12" r="1.8"></circle>
      </svg>
    ),
  },
]

/* Behind More: the two that gave their places to Feed (from user feedback: "put memory
   and config in three dots menu"). */
const MORE = [
  {
    id: 'memory',
    label: 'Memory',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="11" cy="11" r="7"></circle>
        <path d="M20 20l-3.5-3.5"></path>
      </svg>
    ),
  },
  {
    id: 'config',
    label: 'Config',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M4 7h9M17 7h3M4 17h3M11 17h9"></path>
        <circle cx="15" cy="7" r="2"></circle>
        <circle cx="9" cy="17" r="2"></circle>
      </svg>
    ),
  },
]
const IN_MORE = new Set(['memory', 'config'])

/* The offered cards, of the agents this view shows. Waiting for a reminder is not counted: he
   already answered those, and his phone tells him when the agent is free. */
function useOffered(view) {
  const tasks = useTasks()
  const fleet = useSyncExternalStore(fleetSubscribe, fleetSnapshot)
  const byId = new Map(fleet.agents.map(function (a) { return [a.id, a] }))
  return tasks.list.filter(function (t) {
    const a = byId.get(t.agent)
    return t.state === 'offered' && (!a || showsIn(a, view))
  }).length
}

/* Posts that came since he last opened the feed, of the agents this view shows. */
function useNewPosts(view) {
  const posts = usePosts()
  const fleet = useSyncExternalStore(fleetSubscribe, fleetSnapshot)
  const byId = new Map(fleet.agents.map(function (a) { return [a.id, a] }))
  return posts.list.filter(function (p) {
    const a = byId.get(p.agent)
    const inView = !view || view === 'all' || (!!a && showsIn(a, view))
    return p.at > posts.seen && !p.state && inView
  }).length
}

export default function TabBar({ screen, onScreen, view }) {
  const bar = useRef(null)
  const [more, setMore] = useState(false)
  const lit = IN_MORE.has(tabOf(screen)) ? 'more' : tabOf(screen)
  useGlider(bar, lit)
  const needs = useSyncExternalStore(subscribe, getPending)
  const offered = useOffered(view)
  const fresh = useNewPosts(view)

  function pick(id) {
    setMore(false)
    onScreen(id)
  }

  return (
    <nav className="tabbar" aria-label="Screens" ref={bar}>
      <span className="glider" aria-hidden="true"></span>
      {TABS.map(function (tab) {
        return (
          <button
            key={tab.id}
            type="button"
            aria-current={lit === tab.id ? 'page' : undefined}
            aria-expanded={tab.id === 'more' ? more : undefined}
            onClick={function () { if (tab.id === 'more') { setMore(!more) } else { pick(tab.id) } }}
          >
            {tab.icon}
            <span>{tab.label}</span>
            {tab.id === 'fleet' && needs > 0 ? <span className="tb-badge">{needs}</span> : null}
            {tab.id === 'insights' && offered > 0 ? <span className="tb-badge">{offered}</span> : null}
            {tab.id === 'feed' && fresh > 0 && lit !== 'feed' ? <span className="tb-badge">{fresh}</span> : null}
          </button>
        )
      })}
      {more ? (
        <div className="tb-more">
          <button type="button" className="tb-more-dim" aria-label="Close" onClick={function () { setMore(false) }}></button>
          <div className="tb-more-sheet" role="menu">
            {MORE.map(function (tab) {
              return (
                <button key={tab.id} type="button" role="menuitem" onClick={function () { pick(tab.id) }}>
                  {tab.icon}
                  <span>{tab.label}</span>
                </button>
              )
            })}
          </div>
        </div>
      ) : null}
    </nav>
  )
}
