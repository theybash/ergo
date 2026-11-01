/* The top bar. A name on the left, the view switcher, the desktop tab strip, and the search
   that finds an agent by name. The theme switch is in Config. The CSS hides the tab strip under 720px, where the bottom bar takes over, and hides
   the switcher with it, because the Fleet head carries one there instead. */

import { useEffect, useRef, useState } from 'react'
import useGlider from './useGlider'
import ViewMenu from './ViewMenu'
import AgentSearch from './AgentSearch'
import { tabOf } from './tabs'
import './shell.css'

const TABS = [
  { id: 'fleet', label: 'Fleet' },
  { id: 'chat', label: 'Chat' },
  { id: 'feed', label: 'Feed' },
  { id: 'insights', label: 'Insights' },
  { id: 'memory', label: 'Memory' },
  // Config holds Integrations and Health, so the strip has five pills, the same five tabs the
  // phone bar has.
  { id: 'config', label: 'Config' },
]

const glass = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="11" cy="11" r="6.5"></circle>
    <path d="M16 16l4.5 4.5"></path>
  </svg>
)

export default function TopBar({ screen, onScreen, onVoice, untold, view, onView }) {
  const tabs = useRef(null)
  const bar = useRef(null)
  const lit = tabOf(screen)
  useGlider(tabs, lit)
  const [finding, setFinding] = useState(false)

  // The agent page fills what the top bar leaves, so the page needs the real height.
  useEffect(function () {
    function measure() {
      const el = bar.current
      if (el) { document.documentElement.style.setProperty('--topbar-h', el.offsetHeight + 'px') }
    }
    measure()
    window.addEventListener('resize', measure)
    // The display font changes the bar height, so measure again once it lands.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(measure).catch(function () {})
    }
    return function () { window.removeEventListener('resize', measure) }
  }, [])

  return (
    <header className="topbar" ref={bar}>
      <button
        type="button"
        className="brand brand-home"
        aria-label="Ergo, go to Fleet"
        onClick={function () { onScreen('fleet') }}
      >
        {/* The house with the antenna. White marks on the dark theme, near black on the light
            one, the mint window on both. */}
        <img className="brand-mark brand-mark-dark" src="/house-mark-dark.png" alt="" width="32" height="32" />
        <img className="brand-mark brand-mark-light" src="/house-mark-light.png" alt="" width="32" height="32" />
        <span className="brand-name">Ergo</span>
      </button>
      <ViewMenu view={view} onView={onView} />

      <div className="topbar-spacer"></div>

      {/* The static PERSONAL tag was here. It said which view you were in and could not change
          it, so the switcher took its place. */}

      <nav className="navtabs" aria-label="Screens" ref={tabs}>
        <span className="glider" aria-hidden="true"></span>
        {TABS.map(function (tab) {
          return (
            <button
              key={tab.id}
              type="button"
              aria-current={lit === tab.id ? 'page' : undefined}
              onClick={function () { onScreen(tab.id) }}
            >
              {tab.label}
            </button>
          )
        })}
      </nav>

      {/* No orb here: you do not call the front desk from the top bar. */}
      {/* On Fleet the search is a round button beside the plus instead. */}
      {lit !== 'fleet' ? (
        <button
          type="button"
          className="iconbtn"
          aria-label="Find an agent"
          onClick={function () { setFinding(true) }}
        >
          {glass}
        </button>
      ) : null}
      {finding ? <AgentSearch view={view} onScreen={onScreen} onClose={function () { setFinding(false) }} /> : null}
    </header>
  )
}
