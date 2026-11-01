/* The composer, one rounded card the way the Claude app draws it.

   The card stacks three things: what you attached, what you are typing, and the controls. It is
   one card on the phone and the same card on the desktop, because a composer that rearranges
   itself between the two is two things to learn.

   On a keyboard, Enter sends and Shift Enter makes a new line, which is the rule everywhere else
   a person types a message. On a phone, Enter makes a new line and Send sends: the owner asked
   for that, because a phone keyboard has no Shift Enter and a long message needs its lines. The chat composer always sends wake:true, because the person is sitting
   there: a message held in the mailbox is the right default for a note left from the dashboard
   and the wrong one for a conversation.

   THE BOTTOM ROW IS FIVE THINGS
   The plus, a spacer, the call button, the microphone and Send. The call button opens a voice
   call with this agent, off while any call is open. While the agent works, Stop sits beside Send:
   a plain round button with a small square in it (from user feedback: "separate send and stop
   button when agent is working"), so he can add a message without stopping the turn. Stop halts
   the turn and keeps the agent awake. Everything else that used to sit in the row is
   inside the plus: attach, the permission mode, the model and the five phone keys. One row of
   round buttons is a row a thumb can read; six controls of three shapes was not.

   The wireframe orb is not here. It belongs to the top bar and to the voice overlay, which the
   shell owns, and that is where the front desk is called now.

   THE MICROPHONE DICTATES
   A tap starts dictation with the engine chosen in Config (shell/dictation.js). The words appear
   in the box as he speaks, the ones still being guessed shown dim, and the bottom row becomes
   cancel, a line that moves with his voice and a timer, and Send. Send stops the microphone,
   puts the text where the cursor was, with a space either side if it needs one, and sends it at
   once. It does not open the keyboard first (from user feedback: "it closes the voice and
   opens the keyboard first. And then lets me send. That causes some problem when I am on the
   float mode"). He reads the words in the box while he speaks. To change them first, he taps the
   words: the microphone stops and the box opens with the keyboard. The five minute stop does
   the same. His typing waits while it writes, so the two cannot fight over the box. */

import { useEffect, useRef, useState } from 'react'
import { loadFiles, loadText, saveFiles, saveText } from '../../data/drafts'
import { start as startDictation } from '../../shell/dictation'
import { MODELS, OPENCODE_MODELS, PERMISSION_MODES, codexModels, modelLabel, opencodeModels, permLabel } from '../../data/api'
import { PHONE_KEYS, SEQ } from './keys'
import useBackClose from '../../shell/useBackClose'
import { useCallOpen } from '../../shell/callTarget'


/* The three tiles of the plus menu. capture asks a phone to open the camera straight away; a
   desktop browser ignores it and shows its file picker. */
// The most files one message carries. Each is a separate upload, and an agent reads each one.
const MAX_FILES = 10

const PICKS = [
  { id: 'camera', label: 'Camera', accept: 'image/*', capture: 'environment',
    icon: <><path d="M4 8h3l2-2.5h6L17 8h3v11H4z"></path><circle cx="12" cy="13" r="3.5"></circle></> },
  { id: 'photos', label: 'Photos', accept: 'image/*', capture: undefined,
    icon: <><rect x="4" y="4" width="16" height="16" rx="2"></rect><circle cx="9" cy="9.5" r="1.5"></circle><path d="M20 15l-5-5-9 9"></path></> },
  { id: 'files', label: 'Files', accept: '*/*', capture: undefined,
    icon: <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"></path><path d="M14 3v5h5M12 17v-6M9.5 13.5L12 11l2.5 2.5"></path></> },
]

/* A phone: the Ergo app, any Android or iPhone, or a coarse pointer. Some Android web views
   report hover, so the media query alone missed the owner's phone. Checked once. */
const TOUCH = typeof window !== 'undefined' && (
  !!window.Capacitor ||
  /Android|iPhone|iPad/i.test(navigator.userAgent || '') ||
  (!!window.matchMedia && window.matchMedia('(pointer: coarse)').matches))

export default function Composer({
  value, onValue, onSend, sending, permMode, onPermMode, model, onModel, engine, onEngine, onKey,
  working, stopping, onStop, agentName, onCall, above, draftId,
}) {
  /* One call at a time: the call button is off while any call is open. */
  const calling = useCallOpen()
  const [attached, setAttached] = useState([])
  /* The held files are kept per agent with the draft text, so leaving for a screenshot loses
     nothing. `owner` is the agent the list on screen belongs to; nothing is saved until it is
     loaded, or switching agents would write one agent's files over another's. */
  const owner = useRef(null)
  useEffect(function () {
    let live = true
    owner.current = null
    /* oxlint-disable-next-line react/set-state-in-effect -- the agent changed, so its own held
     * files replace the last agent's. */
    setAttached([])
    if (!draftId) { return undefined }
    loadFiles(draftId).then(function (files) {
      if (!live) { return }
      owner.current = draftId
      setAttached(files.map(function (f) {
        const image = (f.type || '').startsWith('image/')
        return { key: Math.random().toString(36).slice(2), src: image ? URL.createObjectURL(f) : '',
          name: f.name || 'pasted image', file: f }
      }))
    })
    return function () { live = false }
  }, [draftId])
  useEffect(function () {
    if (draftId && owner.current === draftId) {
      saveFiles(draftId, attached.map(function (a) { return a.file }))
    }
  }, [attached, draftId])
  const [keysOpen, setKeysOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const input = useRef(null)
  const plus = useRef(null)
  const menu = useRef(null)
  const inputs = useRef({})
  const wrap = useRef(null)
  /* Dictation: what is on screen, the engine's handle, and where in the text it goes. */
  const [dict, setDict] = useState(null)
  const [dictNote, setDictNote] = useState('')
  const live = useRef(null)
  const at = useRef([0, 0])

  /* On the phone the composer floats over the page, and the page keeps room for it at the bottom.
     That room was a fixed 96px from the old design, and the composer is taller now, and taller
     still while a long message is typed, so the end of the conversation hid behind it. The real
     height is measured and handed to the page as --composer-h. */
  useEffect(function () {
    const el = wrap.current
    if (!el || typeof ResizeObserver === 'undefined') { return undefined }
    const root = document.documentElement
    const set = function () { root.style.setProperty('--composer-h', Math.ceil(el.getBoundingClientRect().height) + 'px') }
    set()
    const watch = new ResizeObserver(set)
    watch.observe(el)
    return function () { watch.disconnect(); root.style.removeProperty('--composer-h') }
  }, [])
  // Back closes the plus menu before it leaves the chat.
  useBackClose(menuOpen, function () { setMenuOpen(false) })

  // The box follows the text, then stops. Past 160px the text scrolls instead.
  useEffect(function () {
    const el = input.current
    if (!el) { return }
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 160) + 'px'
  }, [value])

  // A thumbnail's object URL is revoked when it goes, or the page holds the image for as long
  // as the tab is open.
  const shown = useRef([])
  useEffect(function () {
    const now = attached.map(function (a) { return a.src }).filter(Boolean)
    shown.current.forEach(function (u) { if (!now.includes(u)) { URL.revokeObjectURL(u) } })
    shown.current = now
  }, [attached])
  useEffect(function () { return function () { shown.current.forEach(function (u) { URL.revokeObjectURL(u) }) } }, [])

  /* The menu closes on a tap anywhere else and on Escape, and the keyboard goes back to the
     button that opened it, because a menu that leaves the focus nowhere loses the reader. */
  useEffect(function () {
    if (!menuOpen) { return undefined }
    function away(e) {
      if (menu.current && menu.current.contains(e.target)) { return }
      if (plus.current && plus.current.contains(e.target)) { return }
      setMenuOpen(false)
    }
    function key(e) {
      if (e.key !== 'Escape') { return }
      // Taken, so the chat does not also read this Escape as stop.
      e.preventDefault()
      setMenuOpen(false)
      if (plus.current) { plus.current.focus() }
    }
    document.addEventListener('pointerdown', away)
    document.addEventListener('keydown', key)
    return function () {
      document.removeEventListener('pointerdown', away)
      document.removeEventListener('keydown', key)
    }
  }, [menuOpen])

  function shut() {
    setMenuOpen(false)
    if (plus.current) { plus.current.focus() }
  }

  function buzz() { try { if (navigator.vibrate) { navigator.vibrate(12) } } catch { /* no motor */ } }

  /* The tap starts the microphone at once (see shell/dictation.js): "Listening" shows the moment
     it is open, before the key and the connection arrive. A done tapped that early is held and
     carried out when the engine is ready, so nothing said is lost. */
  const wantDone = useRef(false)
  // A cancel tapped before the engine is ready: the handle that arrives later is closed at once.
  const dropped = useRef(false)

  function dictate() {
    if (dict) { return }
    const el = input.current
    at.current = el && el.selectionStart != null ? [el.selectionStart, el.selectionEnd] : [value.length, value.length]
    wantDone.current = false
    dropped.current = false
    let begun
    try {
      begun = startDictation({
        ready: function () { setDict(function (d) { return d && { ...d, phase: 'on', started: Date.now() } }) },
        text: function (final, draft) { setDict(function (d) { return d && { ...d, final: final, draft: draft } }) },
        level: function (lv) { setDict(function (d) { return d && { ...d, levels: d.levels.slice(-27).concat(lv) } }) },
        note: function (text) { if (!dropped.current) { setDictNote(text) } },
      })
    } catch (e) {
      setDictNote((e && e.message) || 'Dictation did not start.')
      return
    }
    setDictNote('')
    setDict({ phase: 'starting', final: '', draft: '', levels: [], started: Date.now() })
    buzz()
    begun.then(function (h) {
      if (dropped.current) { h.cancel(); return }
      live.current = h
      if (wantDone.current) { finishDictation(true) }
    }, function (e) {
      live.current = null
      if (dropped.current) { return }
      setDict(null)
      setDictNote((e && e.message) || 'Dictation did not start.')
    })
  }

  /* send: the Send tap. The text goes out at once and the keyboard stays shut. Without it (the
     five minute stop) the text stays in the box with the cursor after it. */
  async function finishDictation(send) {
    const h = live.current
    if (!h) { wantDone.current = true; setDict(function (d) { return d && { ...d, phase: 'finishing' } }); return }
    live.current = null
    wantDone.current = false
    setDict(function (d) { return d && { ...d, phase: 'finishing' } })
    const text = (await h.done()).trim()
    buzz()
    setDict(null)
    if (!text) {
      if (send) { if (value.trim() || attached.length) { submit() } }
      else { window.setTimeout(function () { const el = input.current; if (el) { el.focus() } }, 0) }
      return
    }
    const [a, b] = at.current
    const before = value.slice(0, a), after = value.slice(b)
    const lead = before && !/\s$/.test(before) ? ' ' : ''
    const tail = after && !/^\s/.test(after) ? ' ' : ''
    const next = before + lead + text + tail + after
    onValue(next)
    if (send) { submit(next); return }
    const caret = (before + lead + text).length
    window.setTimeout(function () {
      const el = input.current
      if (el) { el.focus(); el.setSelectionRange(caret, caret) }
    }, 0)
  }

  function cancelDictation() {
    wantDone.current = false
    dropped.current = true
    if (live.current) { live.current.cancel() }
    live.current = null
    setDict(null)
    buzz()
  }

  // The clock, the five minute stop, and the screen kept awake while he talks.
  const dictOn = !!dict && dict.phase === 'on'
  useEffect(function () {
    if (!dictOn) { return undefined }
    let lock = null
    try { if (navigator.wakeLock) { navigator.wakeLock.request('screen').then(function (l) { lock = l }, function () {}) } } catch { /* no lock */ }
    const t = window.setInterval(function () {
      setDict(function (d) { return d && { ...d, now: Date.now() } })
      const h = live.current
      if (h && dict && Date.now() - dict.started > (h.maxSeconds || 300) * 1000) { finishDictation() }
    }, 250)
    return function () { window.clearInterval(t); if (lock) { lock.release().catch(function () {}) } }
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- only the on and off of dictation matters here
  }, [dictOn])

  // A note about the microphone goes after a few seconds.
  useEffect(function () {
    if (!dictNote) { return undefined }
    const t = window.setTimeout(function () { setDictNote('') }, 5000)
    return function () { window.clearTimeout(t) }
  }, [dictNote])

  /* Leaving the chat while dictating keeps what he said (from user feedback: "If I go back and
     come back to the chat, whatever I recorded is gone"). What was heard goes into the saved draft
     at once, then the whole text replaces it when the engine finishes, unless he changed the draft
     in between. The microphone stops either way. */
  const latest = useRef({})
  latest.current = { value: value, dict: dict, draftId: draftId }
  useEffect(function () {
    return function () {
      const h = live.current
      const { value: v, dict: d, draftId: id } = latest.current
      if (!h || !id) { return }
      live.current = null
      const [a, b] = at.current
      const put = function (text) {
        const before = v.slice(0, a), after = v.slice(b)
        const lead = before && !/\s$/.test(before) ? ' ' : ''
        const tail = after && !/^\s/.test(after) ? ' ' : ''
        return before + lead + text + tail + after
      }
      const heard = ((d && d.final) || '') + ((d && d.draft) ? ' ' + d.draft : '')
      const first = heard.trim() ? put(heard.trim()) : v
      saveText(id, first)
      h.done().then(function (text) {
        text = (text || '').trim()
        if (text && loadText(id) === first) { saveText(id, put(text)) }
      }, function () { /* the heard text is already kept */ })
    }
  }, [])

  function keyDown(e) {
    if (e.key !== 'Enter' || e.shiftKey || TOUCH) { return }
    e.preventDefault()
    submit()
  }

  /* Files, from the clipboard or from the picker. They are held here, up to MAX_FILES, and go
     with the next send: the chat uploads each to the agent's inbox and names its path in the
     message. */
  function hold(files) {
    const add = files.filter(Boolean).map(function (f) {
      const image = (f.type || '').startsWith('image/')
      return { key: Math.random().toString(36).slice(2), src: image ? URL.createObjectURL(f) : '',
        name: f.name || 'pasted image', file: f }
    })
    if (!add.length) { return }
    setAttached(function (was) {
      const all = was.concat(add)
      all.slice(MAX_FILES).forEach(function (a) { if (a.src) { URL.revokeObjectURL(a.src) } })
      return all.slice(0, MAX_FILES)
    })
  }

  function keepFocus(e) { if (document.activeElement === input.current) { e.preventDefault() } }

  /* Send, with the held files. They clear only when the send succeeds, so a failed upload leaves
     them where you can try again. */
  async function submit(said) {
    const ok = await onSend(attached.map(function (a) { return a.file }), said)
    if (ok) { setAttached([]) }
  }

  function paste(e) {
    const items = [...(e.clipboardData ? e.clipboardData.items : [])]
      .filter(function (i) { return i.kind === 'file' && i.type.startsWith('image/') })
    if (!items.length) { return }
    hold(items.map(function (i) { return i.getAsFile() }))
  }

  function chose(e) {
    hold([...(e.target.files || [])])
    // The same file twice running still fires a change, which it would not otherwise.
    e.target.value = ''
  }

  /* An agent may be in a mode these three do not hold, "default" from before they were the set.
     It is named as it stands rather than reported as one of the three: a switcher naming the
     wrong mode is worse than one naming a mode it cannot set. */
  const modes = PERMISSION_MODES.includes(permMode) || !permMode
    ? PERMISSION_MODES
    : PERMISSION_MODES.concat([permMode])

  // The same rule for the model. A record on something this list has never heard of keeps it.
  // OpenCode and Claude name models differently, so show only the list for the engine in use.
  // An older record may not carry an engine yet; its model id, or the lack of one, is the clue.
  const isOpenCode = engine === 'opencode' || (!engine && (!model || model.includes('/')))
  /* OpenCode's whole list, free ones first, each marked. Until it comes, or if it never does,
     the short list above, which is all free. */
  const [ocList, setOcList] = useState(null)
  useEffect(function () {
    if (!isOpenCode || ocList) return
    opencodeModels().then(function (r) {
      const got = (r.models || []).slice().sort(function (a, b) {
        return (b.free - a.free) || a.name.localeCompare(b.name)
      })
      if (got.length) setOcList(got)
    }).catch(function () {})
  }, [isOpenCode, ocList])
  const ocInfo = new Map((ocList || OPENCODE_MODELS.map(function (id) {
    return { id: id, name: modelLabel(id), free: true }
  })).map(function (m) { return [m.id, m] }))
  /* A Codex agent picks from the models its ChatGPT account may use, which Codex keeps on disk.
     A Claude name on a Codex agent is skipped by the daemon, so it shows here as "Codex default". */
  const isCodex = engine === 'codex'
  const [cxList, setCxList] = useState(null)
  useEffect(function () {
    if (!isCodex || cxList) return
    codexModels().then(function (r) { setCxList(r.models || []) }).catch(function () { setCxList([]) })
  }, [isCodex, cxList])
  const cxInfo = new Map((cxList || []).map(function (m) { return [m.id, m] }))
  const claudeOnCodex = isCodex && !!model && /^(opus|sonnet|haiku|fable|claude)/i.test(model)
  const choices = isCodex ? Array.from(cxInfo.keys())
    : isOpenCode ? Array.from(ocInfo.keys()) : MODELS
  const models = !model || choices.includes(model) || claudeOnCodex ? choices : choices.concat([model])
  /* "Free" is the marker's job, so it leaves the name. */
  const label = function (m) {
    if (isCodex) return (cxInfo.get(m) || { name: m }).name
    const info = isOpenCode && ocInfo.get(m)
    if (!info) return modelLabel(m)
    return <>{info.name.replace(/\s*free$/i, '')}{info.free ? <span className="ch-free">free</span> : null}</>
  }

  return (
    <div className="composer-wrap" ref={wrap}>
      {/* What sits just above the box, inside the sticky part: a quoted task. */}
      {above}
      <form className="ch-card" onSubmit={function (e) { e.preventDefault(); submit() }}>

        {attached.length ? (
          <div className="ch-attach">
            {attached.map(function (a) {
              return (
                <span className="ch-thumb" key={a.key}>
                  {a.src ? <img src={a.src} alt={a.name} /> : null}
                  {attached.length === 1 || !a.src ? <span className="ch-name">{a.name}</span> : null}
                  <button
                    type="button"
                    className="ch-rm"
                    aria-label={'Remove ' + a.name}
                    onClick={function () { setAttached(function (was) { return was.filter(function (x) { return x !== a }) }) }}
                  >
                    ×
                  </button>
                </span>
              )
            })}
          </div>
        ) : null}

        {dict ? (
          /* A tap on the words stops the microphone and opens them in the box to edit, with the
             keyboard. Send is the other way out, and sends them as they are. */
          <div className="ch-input ch-dict-text" aria-live="polite" role="button" aria-label="Stop and edit" onClick={function () { if (dict.phase === 'on') { finishDictation() } }}>
            {value.slice(0, at.current[0])}
            {dict.final ? (value.slice(0, at.current[0]) && !/\s$/.test(value.slice(0, at.current[0])) ? ' ' : '') + dict.final : ''}
            {dict.draft ? <span className="ch-dict-draft">{(dict.final || value.slice(0, at.current[0])) ? ' ' : ''}{dict.draft}</span> : null}
            {!dict.final && !dict.draft && !value ? <span className="ch-dict-draft">{dict.phase === 'starting' ? 'Opening the microphone...' : 'Listening...'}</span> : null}
            {value.slice(at.current[1])}
          </div>
        ) : null}
        {dictNote ? <p className="ch-dict-note" role="status">{dictNote}</p> : null}

        <label className="sr-only" htmlFor="msg">Message</label>
        <textarea
          enterKeyHint={TOUCH ? 'enter' : 'send'}
          className="ch-input"
          id="msg"
          rows="1"
          aria-label="Message"
          ref={input}
          value={value}
          disabled={sending}
          onChange={function (e) { onValue(e.target.value) }}
          onKeyDown={keyDown}
          onPaste={paste}
          hidden={!!dict}
        ></textarea>

        {/* The five keys a phone keyboard does not have. They live in the chat composer as well
            as on the terminal tab, because a permission dialog drawn inside the TERMINAL can
            only be answered with a keypress. The plus menu turns them on. */}
        {keysOpen ? (
          <div className="phonekeys ch-keys">
            {PHONE_KEYS.map(function (key) {
              return (
                <button
                  key={key.id}
                  type="button"
                  aria-label={key.label}
                  onClick={function () { onKey(SEQ[key.id]) }}
                >
                  {key.text}
                </button>
              )
            })}
          </div>
        ) : null}

        {dict ? (
          <div className="ch-tools ch-dict-row">
            <button type="button" className="ch-round" aria-label="Cancel dictation" onClick={cancelDictation}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>
            </button>
            <span className="ch-dict-meter" aria-hidden="true">
              {dict.levels.map(function (lv, i) { return <i key={i} style={{ height: Math.max(3, Math.round(lv * 26)) + 'px' }}></i> })}
            </span>
            <span className="ch-dict-time">{(function () { const s = Math.floor(((dict.now || dict.started) - dict.started) / 1000); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') })()}</span>
            <button type="button" className="ch-round ch-send" aria-label="Send" disabled={dict.phase !== 'on' || sending} onPointerDown={function (e) { e.preventDefault() }} onClick={function () { finishDictation(true) }}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"></path></svg>
            </button>
          </div>
        ) : null}

        <div className="ch-tools" hidden={!!dict}>
          <span className="ch-plus-wrap">
            <button
              type="button"
              className="ch-round"
              ref={plus}
              aria-label="More"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={function () { setMenuOpen(!menuOpen) }}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
                <path d="M12 5v14M5 12h14"></path>
              </svg>
            </button>

            {menuOpen ? (
              <div className="ch-plus-menu" role="menu" aria-label="More" ref={menu}>
                {/* Three ways in, like the Claude app: the camera, the photo library, any file.
                    Each opens its own input, so the phone shows the right picker for each. */}
                <div className="ch-add" role="group" aria-label="Add">
                  {PICKS.map(function (pick) {
                    return (
                      <button
                        key={pick.id}
                        type="button"
                        role="menuitem"
                        className="ch-add-tile"
                        onClick={function () { shut(); const el = inputs.current[pick.id]; if (el) { el.click() } }}
                      >
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          {pick.icon}
                        </svg>
                        <span>{pick.label}</span>
                      </button>
                    )
                  })}
                </div>

                <p className="ch-plus-label" id="ch-perm-label">Permission mode</p>
                <div className="ch-plus-row" role="group" aria-labelledby="ch-perm-label">
                  {modes.map(function (m) {
                    return (
                      <button
                        key={m}
                        type="button"
                        role="menuitemradio"
                        className="ch-plus-pick"
                        aria-checked={permMode === m}
                        onClick={function () { shut(); onPermMode(m) }}
                      >
                        {permLabel(m)}
                      </button>
                    )
                  })}
                </div>

                {/* The engine. A switch asks first, in the chat screen's setEngine. OpenCode shows
                    only on an agent already on it: a switch to it needs a session of its own. */}
                {onEngine ? (
                  <>
                    <p className="ch-plus-label" id="ch-engine-label">Engine</p>
                    <div className="ch-plus-row" role="group" aria-labelledby="ch-engine-label">
                      {(isOpenCode ? ['claude', 'codex', 'opencode'] : ['claude', 'codex']).map(function (e) {
                        return (
                          <button
                            key={e}
                            type="button"
                            role="menuitemradio"
                            className="ch-plus-pick"
                            aria-checked={(isCodex ? 'codex' : isOpenCode ? 'opencode' : 'claude') === e}
                            onClick={function () { shut(); onEngine(e) }}
                          >
                            {{ claude: 'Claude', codex: 'Codex', opencode: 'OpenCode' }[e]}
                          </button>
                        )
                      })}
                    </div>
                  </>
                ) : null}

                <p className="ch-plus-label" id="ch-model-label">{isCodex ? 'Codex models' : isOpenCode ? 'OpenCode models' : 'Claude models'}</p>
                <div className={isOpenCode ? 'ch-plus-row ch-plus-many' : 'ch-plus-row'} role="group" aria-labelledby="ch-model-label">
                  <button
                    type="button"
                    role="menuitemradio"
                    className="ch-plus-pick"
                    aria-checked={!model || claudeOnCodex}
                    onClick={function () { shut(); onModel('') }}
                  >
                    default
                  </button>
                  {models.map(function (m) {
                    return (
                      <button
                        key={m}
                        type="button"
                        role="menuitemradio"
                        className="ch-plus-pick"
                        aria-checked={model === m}
                        title={m}
                        onClick={function () { shut(); onModel(m) }}
                      >
                        {label(m)}
                      </button>
                    )
                  })}
                </div>

                <button
                  type="button"
                  role="menuitemradio"
                  className="ch-plus-item"
                  aria-checked={keysOpen}
                  onClick={function () { shut(); setKeysOpen(!keysOpen) }}
                >
                  {keysOpen ? 'Hide the keyboard keys' : 'Keyboard keys'}
                </button>
              </div>
            ) : null}
          </span>

          {/* Out of the tab order and out of the reading order. The menu item above is its only
              control, and a bare file input in the row would be a second one. */}
          {PICKS.map(function (pick) {
            return (
              <input
                key={pick.id}
                type="file"
                accept={pick.accept}
                capture={pick.capture}
                multiple={!pick.capture}
                ref={function (el) { inputs.current[pick.id] = el }}
                hidden
                onChange={chose}
              />
            )
          })}

          <span className="ch-grow"></span>

          {/* Talk to this agent by voice: the call overlay opens with the agent itself on the
              line, in its own voice, knowing the conversation. */}
          {onCall ? (
            <button
              type="button"
              className="ch-round ch-voice"
              aria-label={'Call ' + agentName}
              title={calling ? 'A call is already open' : 'Call ' + agentName}
              onClick={onCall}
              disabled={calling}
            >
              {/* Sound bars on a filled disc, the voice button people know from the chat apps. */}
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
                <path d="M6 10.5v3M9 8v8M12 5.5v13M15 8v8M18 10.5v3"></path>
              </svg>
            </button>
          ) : null}

          <button type="button" className="ch-round" aria-label="Dictate" onClick={dictate} disabled={sending}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
              <rect x="9" y="3" width="6" height="11" rx="3"></rect>
              <path d="M5 11a7 7 0 0 0 14 0M12 18v3"></path>
            </svg>
          </button>

          {working && !value.trim() ? (
            /* While the agent works the round button is Stop, so a runaway turn can always be
               halted with one tap — but only while the composer is empty. The owner asked for
               the send icon whenever words are typed, so a follow-up can go out mid-turn
               without first stopping the agent. Empty the box to get Stop back. */
            <button
              type="button"
              className="ch-round ch-stop"
              aria-label={stopping ? 'Stopping' : 'Stop'}
              aria-busy={stopping}
              disabled={stopping}
              onClick={onStop}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <rect x="6.5" y="6.5" width="11" height="11" rx="2.5" fill="currentColor"></rect>
              </svg>
            </button>
          ) : null}
          <button
              type="submit"
              className="ch-round ch-send"
              aria-label="Send"
              /* The press keeps the focus in the text box. Taking it closed the keyboard in the
                 middle of the tap, the composer moved under the finger, and the tap missed. */
              onPointerDown={keepFocus}
              onMouseDown={keepFocus}
              disabled={sending || (!value.trim() && !attached.length)}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 19V5M6 11l6-6 6 6"></path>
              </svg>
            </button>
        </div>
      </form>
    </div>
  )
}
