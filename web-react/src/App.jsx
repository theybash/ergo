/* The shell. It owns which screen is on, which theme is on, which view of the fleet you are
   looking at, and whether the voice overlay is up. Every screen gets onScreen, so a card can
   send you to the chat.

   Back goes back one level. Every screen change and every agent opened is one history entry,
   the address (?screen= and #/a/<id>) says which, and the one popstate handler below hands the
   back to shell/nav.js and then reads the address again. With nothing behind the first screen,
   the Android app closes, as a browser tab would leave the page. */

import { Suspense, useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import TopBar from './shell/TopBar'
import TabBar from './shell/TabBar'
import Voice from './shell/Voice'
import { pushVoiceEntry, dropVoiceEntry } from './shell/voiceHistory'
import { HOME, go, onRoute, popped, screenInUrl } from './shell/nav'
import { onApp, tellApp } from './shell/native'
import useInbox from './data/useInbox'
import { pinCall } from './data/taskPin'
import { VIEWS } from './shell/views'
/* The Fleet screen owns the count. src/shell/needsFallback.js is the stand in, if this file
   ever goes missing again. */
import { subscribe, getPending } from './screens/fleet/needsStore'
import Chat from './screens/chat'
import lazyFile from './shell/lazyFile'
import { callAsked } from './shell/awayReload'

/* Chat and the shell come in the first file. Every other screen is its own file, fetched the
   first time it opens, so the phone downloads less over the tailnet before it draws. */
const Fleet = lazyFile(function () { return import('./screens/fleet') })
const Recruit = lazyFile(function () { return import('./screens/recruit') })
const Insights = lazyFile(function () { return import('./screens/insights') })
const Memory = lazyFile(function () { return import('./screens/memory') })
const Integrations = lazyFile(function () { return import('./screens/integrations') })
const Health = lazyFile(function () { return import('./screens/health') })
const Config = lazyFile(function () { return import('./screens/config') })
const Feed = lazyFile(function () { return import('./screens/feed') })

const SCREENS = {
  fleet: Fleet,
  /* No pill and no tab. Recruit is reached from the Fleet head, or by ?screen=recruit, because
     it is a screen the owner opens rarely and a seventh pill would cost every other one width. */
  recruit: Recruit,
  chat: Chat,
  insights: Insights,
  memory: Memory,
  /* Config holds the agent settings, Integrations and Health, and has the tab. The two older
     screens have no tab now and stay here so ?screen=integrations and ?screen=health still open. */
  config: Config,
  feed: Feed,
  integrations: Integrations,
  health: Health,
}

/* The screen the address names. None is HOME, the chat at a desk and the Fleet on a phone. An
   unknown name is the Fleet. */
function screenOfUrl() {
  const want = screenInUrl()
  if (SCREENS[want]) return want
  return want ? 'fleet' : HOME
}

function firstTheme() {
  try {
    const saved = localStorage.getItem('cc-theme')
    if (saved === 'light' || saved === 'dark') { return saved }
  } catch {
    // Storage can be off. Dark stands.
  }
  return 'dark'
}

/* Work, demo or personal. The old console kept this in the browser and so does this one: the
   operator said a reload or a reboot may lose it, the last setting is fine until he changes it.
   The key is cc-view and not the old console's cc-mode, because "mode" already means which
   engine an agent runs, chat or terminal, and one word may mean one thing. */
function firstView() {
  try {
    const saved = localStorage.getItem('cc-view')
    if (saved === 'all' || VIEWS.includes(saved)) { return saved }
  } catch {
    // Storage can be off. Personal stands.
  }
  return 'personal'
}

export default function App() {
  // The address decides the screen: ?screen=chat opens on the chat, and the hash is left alone,
  // so ?screen=chat#/a/job-search opens that agent's chat. A tab, a back and a link all move the
  // address first, and the screen follows it.
  const [screen, setScreen] = useState(screenOfUrl)
  const [theme, setTheme] = useState(firstTheme)
  const [view, setView] = useState(firstView)

  const [voice, setVoice] = useState(false)
  /* The call is running but the overlay is off the screen, and a bar under the top bar holds it.
     Same session, same audio, same microphone: Voice stays mounted and only its drawing changes. */
  const [voiceMin, setVoiceMin] = useState(false)
  /* Whether the overlay opens with its typed box shown. Only the Android app's Text slice asks. */
  const [voiceKeys, setVoiceKeys] = useState(false)
  const opener = useRef(null)
  /* Whether the overlay is up, readable at once. A close can be asked for twice in one breath,
     by a hang up and then the back it causes, and only the first one may do anything. */
  const voiceUp = useRef(false)

  // The asks waiting on you. The Fleet screen owns the list; the shell only reads the count, so
  // the bottom tab badge and the number on the Fleet list can never disagree.
  const pending = useSyncExternalStore(subscribe, getPending)

  // What the front desk has not told yet. The orb glows with the count, and the call hears when a newer
  // item replaced one of its own.
  const inbox = useInbox()

  // One attribute repaints the page, so the theme lives on <html>.
  useEffect(function () {
    document.documentElement.setAttribute('data-theme', theme)
    const meta = document.querySelector('meta[name="theme-color"]:not([media])')
    if (meta) { meta.setAttribute('content', theme === 'dark' ? '#0f0e0d' : '#ffffff') }
    try {
      localStorage.setItem('cc-theme', theme)
    } catch {
      // Storage can be off. The theme still holds for this visit.
    }
  }, [theme])

  useEffect(function () {
    // Inside the app, the bubble's Recent can follow this view; in a browser this does nothing.
    tellApp('setMenuView', { view: view })
    try {
      localStorage.setItem('cc-view', view)
    } catch {
      // Storage can be off. The view still holds for this visit.
    }
  }, [view])

  // The pills glide only after the first paint, so nothing slides in on load.
  useEffect(function () {
    const id = window.requestAnimationFrame(function () {
      document.documentElement.classList.add('motion')
    })
    return function () { window.cancelAnimationFrame(id) }
  }, [])

  /* The one popstate handler. nav.js first, because it knows whether the back was a layer
     closing, and then the screen follows the address. An address naming no screen is corrected
     in place, not pushed, because it is not a step the owner took. */
  useEffect(function () {
    if (screenInUrl() && !SCREENS[screenInUrl()]) { go('fleet', undefined, true) }
    function onPop() { popped() }
    window.addEventListener('popstate', onPop)
    const off = onRoute(function () { setScreen(screenOfUrl()) })
    return function () {
      window.removeEventListener('popstate', onPop)
      off()
    }
  }, [])

  /* One step. `hash` is the chat's agent, #/a/<id>; left out, the hash stays, so coming back to
     the chat lands on the same agent. A tap on the screen already open pushes nothing. */
  function goTo(name, hash) {
    if (!SCREENS[name]) { return }
    go(name, hash)
    window.scrollTo(0, 0)
  }


  /* Minimise. The overlay leaves and the call stays. Back has already taken the entry off; the
     button takes it off itself, so Back from the bar walks the screens and never hangs up. */
  const minimiseVoice = useCallback(function (why) {
    if (!voiceUp.current) { return }
    setVoiceMin(true)
    if (why !== 'back') { dropVoiceEntry() }
  }, [])

  /* The bar was tapped, or the orb. The overlay comes back with its own entry, so Back
     minimises it again. */
  const restoreVoice = useCallback(function () {
    if (!voiceUp.current) { return }
    pushVoiceEntry()
    setVoiceMin(false)
  }, [])

  function openVoice(keys) {
    // Both the orb in the top bar and the microphone in the composer come here, so one call is
    // opened however you reached for it. A second tap while a call is already running must not
    // start a second one: it puts the keyboard on Hang up instead, which is the control the
    // person reaching for the orb again is usually after.
    if (voice) {
      // A minimised call comes back rather than starting another.
      if (voiceMin) { restoreVoice(); return }
      const out = document.getElementById('voice-stop')
      if (out) { out.focus() }
      return
    }
    // Remember the orb that opened it, so Escape and Hang up put the keyboard back where it was.
    opener.current = document.activeElement
    // One history entry, so the phone's back button closes the overlay and not the app.
    pushVoiceEntry()
    voiceUp.current = true
    // An orb passes its click event here, so only a real true opens the box.
    setVoiceKeys(keys === true)
    setVoice(true)
  }

  /* The Android app asks from its bubble: ?voice=call or ?voice=keys when it loads the page,
     a ccnative event when the page is already up. The query is read once and taken off the
     address, so a reload does not start another call. See src/shell/native.js. */
  const fromApp = useRef(null)
  useEffect(function () {
    fromApp.current = { openVoice: openVoice, goTo: goTo }
  })
  useEffect(function () {
    const params = new URLSearchParams(window.location.search)
    // Prompt on the front desk's call screen, when the page was not up yet: ?pin= quotes the call.
    const pinned = params.get('pin')
    const agentInHash = window.location.hash.match(/^#\/a\/([a-z0-9-]+)/)
    if (pinned) {
      if (agentInHash) { pinCall(agentInHash[1], pinned) }
      params.delete('pin')
      window.history.replaceState(window.history.state, '',
        window.location.pathname + (params.toString() ? '?' + params.toString() : '') + window.location.hash)
    }
    const want = params.get('voice')
    if (want) {
      params.delete('voice')
      const rest = params.toString()
      window.history.replaceState(window.history.state, '',
        window.location.pathname + (rest ? '?' + rest : '') + window.location.hash)
      fromApp.current.openVoice(want === 'keys')
    }
    return onApp(function (msg) {
      if (msg.do === 'voice') {
        // A page about to start over opens the call now, and starts over after it (awayReload.js).
        callAsked()
        fromApp.current.openVoice(msg.keys === true)
      }
      if (msg.do === 'screen' && SCREENS[msg.screen]) {
        const hash = msg.hash ? (msg.hash.charAt(0) === '#' ? msg.hash : '#' + msg.hash) : undefined
        if (msg.pin && hash) { pinCall(hash.replace(/^#\/a\//, ''), msg.pin) }
        fromApp.current.goTo(msg.screen, hash)
      }
    })
  }, [])

  /* An inbox item he wants to go into, from the call. The call minimises and keeps running, and
     that agent's chat opens under it. */
  function openFromVoice(agent) {
    if (!agent) { return }
    minimiseVoice('button')
    goTo('chat', '#/a/' + agent)
  }

  // `why` is how it closed. Back has already taken the entry off, and a page that is going away
  // must not be sent back as it leaves; every other close takes the entry off itself.
  const closeVoice = useCallback(function (why) {
    if (!voiceUp.current) { return }
    voiceUp.current = false
    setVoice(false)
    setVoiceMin(false)
    // A minimised call has no entry left to take off, and dropVoiceEntry knows that.
    if (why !== 'back' && why !== 'pagehide') { dropVoiceEntry() }
    const back = opener.current
    opener.current = null
    if (back && back.focus) { back.focus() }
  }, [])

  const Screen = SCREENS[screen]

  // The one exception to "a screen gets no other prop": Fleet owns the view of the fleet and
  // renders the switcher on the phone, Chat reads the count of asks and hides what the view hides,
  // Insights hides the task cards of the agents the view hides, Memory asks the daemon for
  // the memory of the agents the view may see, and Config holds the theme switch.
  const extra = screen === 'fleet' || screen === 'insights' || screen === 'memory' || screen === 'feed'
    ? { view: view }
    : screen === 'chat'
      ? { needs: pending, onVoice: openVoice, view: view }
      : screen === 'config'
        ? { theme: theme, onTheme: setTheme }
        : null

  return (
    <>
      <TopBar
        screen={screen}
        onScreen={goTo}
        onVoice={openVoice}
        untold={inbox.summary ? inbox.summary.untold : 0}
        view={view}
        onView={setView}
      />

      {/* The minimised call's bar lands here, under the top bar and above the screen, so it
          pushes the page down rather than covering the composer or the tab bar. */}
      <div className="vc-bar-slot" id="vc-bar-slot"></div>

      <main>
        {/* While a screen's file comes, the main area stays empty, as it is for a moment
            anyway while a screen asks the daemon for its first data. */}
        <Suspense fallback={null}>
          <Screen onScreen={goTo} {...extra} />
        </Suspense>
      </main>

      <TabBar screen={screen} onScreen={goTo} view={view} />

      {voice ? (
        <Voice
          onClose={closeVoice}
          keys={voiceKeys}
          minimised={voiceMin}
          onMinimise={minimiseVoice}
          onRestore={restoreVoice}
          onOpen={openFromVoice}
          inboxChange={inbox.change}
        />
      ) : null}
    </>
  )
}
