/* The conversation, in the order it happened.

   Top to bottom: the recap, the record, your message that the record has not caught up with,
   the turn still arriving with the calls it is making, the dots while the agent works and has
   written nothing, and the cards it is waiting on you to answer. That order is not a style. A
   card asking to run a command belongs where your thumb is, under everything else, and a
   message you just sent belongs above the reply to it.

   THE ONE SCROLL RULE
   The log never moves the reader. Any scroll up, however small, stops following, and nothing
   the hand does after it turns following back on. Only the reader reaching the very bottom
   does. While they are up the page nothing moves: a small "New below" pill appears instead, and
   it goes the moment they are back at the bottom. Three things may still jump: arriving on the screen, coming back
   to the Chat tab, and a message the reader sent themselves.

   HIDDEN, IT KEEPS STILL
   Under 720px every tab scrolls the one page. The log hidden behind Files or Open kept pinning
   that page to its own bottom, so Files opened at its bottom. While it is not the tab on screen,
   the log neither reads nor moves the page. Back on it, it returns to where he was: the bottom if
   he was following, his place if he was reading up the page.

   ARRIVING ON UNREAD MESSAGES
   A chat with messages he has not read opens on the first of them, under a "New" line, not at
   the bottom. The arrow at the foot carries a small red count of them until he reaches the end.

   Above 720px this box is the only scroller on the page. Under it the box has no height of its
   own and the page scrolls instead, so every measurement asks which of the two is moving. */

import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import ApprovalCard from './ApprovalCard'
import CallRow from './CallRow'
import JobCard from './JobCard'
import SysRun from './SysRun'
import { runsOf } from './system'
import CallView from './CallView'
import ImageView from './ImageView'
import WaDrafts from './WaDrafts'
import MailDrafts from '../mail/MailDrafts'
import ToolView from './ToolView'
import LiveReply from './LiveReply'
import Prose from './Prose'
import QuestionCard from './QuestionCard'
import { answered, read } from './kinds'
import Recap from './Recap'
import ToolChips from './ToolChips'
import Turn from './Turn'
import { isShowCard, isShowResult } from './show'

// The very bottom, the one place that turns following back on, and how near the top asks for more.
const AT_FOOT = 2
const HEAD = 40
const PRELOAD = 320
// How long after a hand moving up the log ignores being at the bottom: a swipe up spends its
// first frames still there.
const UP_HOLD = 600
// How long an ask for older messages may take before another may be asked.
const OLDER_WAIT = 15000

/* Which of the two is the scroller. The layout decides, not the content: above 720px app.css
   gives the box its own overflow and it scrolls, under 720px it has no height of its own and the
   page moves. Guessing from the content size went wrong for a moment whenever the content was
   shorter than the box, for example while a settled reply replaced the live one. */
function owns(el) {
  const y = getComputedStyle(el).overflowY
  return y === 'auto' || y === 'scroll'
}

// How far the reader is from the bottom, in pixels, on whichever of the two is moving.
function gap(el) {
  if (owns(el)) { return el.scrollHeight - el.scrollTop - el.clientHeight }
  const doc = document.documentElement
  return doc.scrollHeight - window.scrollY - doc.clientHeight
}

/* The first message the reader can see, and where it is on the screen. When older messages land
   above, putting this same element back at the same height is what keeps his place. It holds
   whatever the browser's own scroll anchoring did in between, because it measures the result. */
function firstSeen(el) {
  const log = el.querySelector('.log')
  if (!log) { return [] }
  const top = owns(el) ? el.getBoundingClientRect().top : 0
  const seen = []
  for (const node of log.children) {
    // Headings can stay at the top while messages move below them.
    if (node.matches('.ch-older-quiet, .ch-day, .ch-newline, .recap, .ch-empty')) { continue }
    const r = node.getBoundingClientRect()
    if (r.height && r.bottom > top + 1) { seen.push({ node: node, top: r.top }) }
    // Keep fallbacks if an incomplete turn becomes a fold when its older rows arrive.
    if (seen.length === 10) { break }
  }
  return seen
}

function toBottom(el) {
  if (!el || !el.isConnected) { return }
  if (owns(el)) {
    el.scrollTop = el.scrollHeight
  } else {
    window.scrollTo(0, document.documentElement.scrollHeight)
  }
}

/* The live reply is already in the saved history when the last agent text there is the same
   words. That happens between Claude Code writing a message to its session file and the daemon
   emptying the live reply, and drawing both showed the message twice. */
/* A finished turn folds its workings. Everything between your message and the agent's last words
   (the tool calls, their results, the notes on the way) goes into one row above the answer, and
   the answer stays in view. A turn still running is shown whole, as it happens.

   Returns what to draw, in order: {row} for a row drawn as it is, and {work, rows, key} for a
   fold. A turn is finished when your next message follows it, or, for the last one, when nothing
   is still arriving. */
function fold(rows, running) {
  const out = []
  let turn = []
  function close(done) {
    if (!turn.length) { return }
    let last = -1
    for (let i = turn.length - 1; i >= 0; i -= 1) {
      if (turn[i].kind === 'text' && turn[i].who === 'agent') { last = i; break }
    }
    if (done && last > 0) {
      /* A card the agent showed him stays in view, above the answer, not inside the fold. */
      const cards = turn.filter(function (r, i) { return i !== last && isShowCard(r) })
      const work = turn.filter(function (r, i) { return i !== last && !isShowCard(r) && !isShowResult(r) })
      /* Keyed by the answer, not by the first row: older rows join the top of a cut-off turn,
         and a key that changed with them remounted the fold and lost the reader's place. */
      if (work.length) { out.push({ work: true, rows: work, key: 'work-' + turn[last].key }) }
      cards.forEach(function (r) { out.push({ row: r }) })
      out.push({ row: turn[last] })
    } else {
      turn.forEach(function (r) { out.push({ row: r }) })
    }
    turn = []
  }
  rows.forEach(function (r) {
    /* A call or a job's card is a moment of its own, like your message: it ends the turn before
       it. So is a question the agent asked and the card he answered it with, so neither hides in
       the fold. */
    const asked = r.kind === 'tool' && r.tool === 'AskUserQuestion'
    const tapped = r.kind === 'result' && !r.error && answered(r.text) !== null
    if ((r.kind === 'text' && r.who === 'operator') || r.kind === 'call' || r.kind === 'shown' ||
        asked || tapped) {
      close(true)
      out.push({ row: r })
    } else {
      turn.push(r)
    }
  })
  close(!running)
  return out
}

/* The agent's calls with the owner, and the job results shown for him, put among the messages by
   time. One older than the first message on screen waits until older messages are loaded, so it
   never sits at the top out of order. A row with no readable time keeps its place, and the ones
   after the last message go last. */
function withCalls(rows, calls, hasMore, shown) {
  const moments = (calls || [])
    .map(function (c) { return { kind: 'call', key: 'call-' + c.id, call: c, ms: c.at * 1000 } })
    .concat((shown || []).map(function (j) {
      return { kind: 'shown', key: 'shown-' + j.id, shown: j, ms: j.at * 1000 }
    }))
  if (!moments.length) { return rows }
  const first = rows.length ? Date.parse(rows[0].at) : NaN
  const waiting = moments
    .filter(function (m) { return !(hasMore && Number.isFinite(first) && m.ms < first) })
    // In time order, or the walk below stops at the first late one and drops the rest at the foot.
    .sort(function (a, b) { return a.ms - b.ms })
  const out = []
  let next = 0
  rows.forEach(function (r) {
    const t = Date.parse(r.at)
    while (next < waiting.length && Number.isFinite(t) && waiting[next].ms <= t) {
      out.push(waiting[next])
      next += 1
    }
    out.push(r)
  })
  return out.concat(waiting.slice(next))
}

/* How many of the folded rows were steps: calls to tools, which is what the count means. */
function steps(rows) {
  return rows.filter(function (r) { return r.kind === 'tool' }).length
}

function alreadySaid(rows, live) {
  const want = String(live || '').trim()
  if (!want) { return false }
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const r = rows[i]
    if (r && r.kind === 'text' && r.who === 'agent') { return String(r.text || '').trim() === want }
  }
  return false
}

/* The turn ended on an API error, which Claude Code saves as the agent's last words. A content
   filter does this after a long reply has streamed, and the record keeps only the error. */
function apiError(row) {
  return !!row && row.kind === 'text' && row.who === 'agent' &&
    /^API Error:/.test(String(row.text || '').trim())
}

/* The record already holds this text in its newest turn: any agent text after his last message.
   A history read in the middle of a turn can bring a text that is still drawn from the stream. */
function inRecord(rows, text) {
  const want = String(text || '').trim()
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const r = rows[i]
    if (r.kind === 'text' && r.who === 'operator') { return false }
    if (r.kind === 'text' && r.who === 'agent' && String(r.text || '').trim() === want) { return true }
  }
  return false
}

/* A drawn item's key: a fold has its own, a row has the row's. */
function keyOf(item) { return item.work ? item.key : item.row.key }

/* When a drawn row was said, in seconds, or NaN. Calls and job cards carry ms of their own. */
function whenOf(row) {
  if (row.ms) { return row.ms / 1000 }
  return Date.parse(row.at) / 1000
}

/* The day a drawn item belongs to, and its name over the log: Today, Yesterday, a weekday this
   week, else the date (from user feedback: "if it's like three days ago, it will just show
   the timestamp again. It's useless to me if it doesn't show the day"). */
function dayOf(item) {
  const r = item.work ? item.rows[0] : item.row
  if (!r) { return null }
  const t = r.kind === 'shown' && r.shown ? r.shown.at : whenOf(r)
  if (!(t > 0)) { return null }
  return new Date(t * 1000)
}

function dayName(d) {
  const start = function (x) { return new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime() }
  const ago = Math.round((start(new Date()) - start(d)) / 86400000)
  if (ago === 0) { return 'Today' }
  if (ago === 1) { return 'Yesterday' }
  if (ago > 1 && ago < 7) { return d.toLocaleDateString([], { weekday: 'long' }) }
  const year = d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric'
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short', year: year })
}

/* The first drawn item he has not read, and how many there are from it on. His own words do
   not count, nor does an agent he never opened (no read_at), which counts as read. */
function firstUnread(drawn, after) {
  if (after === null || after === undefined) { return { key: null, count: 0 } }
  let key = null
  let count = 0
  drawn.forEach(function (item) {
    if (item.work) { return }
    const r = item.row
    if (r.kind === 'text' && r.who === 'operator' && read(r.text) === null) { return }
    if (r.kind !== 'text' && r.kind !== 'call' && r.kind !== 'shown') { return }
    const t = whenOf(r)
    if (!(t > Number(after) + 1)) { return }
    if (!key) { key = keyOf(item) }
    count += 1
  })
  return { key: key, count: count }
}

/* The "New" line at the top of the screen, under the bars, where his eye starts. */
function toNew(el) {
  if (!el || !el.isConnected) { return }
  const line = el.querySelector('.ch-newline')
  if (line) { line.scrollIntoView({ block: 'start' }) }
}

export default function Log({
  id, active, rows, hasMore, loadOlder,
  echo, live, said, held, tools, thinking, cards, busyAsk, notes, sendTick,
  onPerm, onQuestion, onQuestionDone, onSkip, recap, onRecapClose, calls, shownJobs, name,
  unreadAfter,
}) {
  const box = useRef(null)
  // Whether this is the tab on screen, for the listeners, which are set once.
  const shows = useRef(active)
  shows.current = active
  /* The first message he had not read when the chat opened, and how many there were. Fixed when
     the messages land, and kept until another agent opens: the line stays where it was found. */
  const [fresh, setFresh] = useState({ key: null, count: 0 })
  // The "New" line is still to be brought into view, while the chat settles.
  const aim = useRef(false)
  /* Whether to keep the newest line in view. It holds where the reader was at the last scroll,
     which is where they were before the content changed, because content changing does not
     scroll anything by itself. */
  const stick = useRef(true)
  /* A chat still loading: its messages have not arrived, or arrived under half a second ago and
     are still taking their height. Until then a swipe neither leaves the newest line nor asks for
     older messages, so one swipe on a chat that is still landing cannot throw him far up. */
  const settling = useRef(true)
  const settleTimer = useRef(0)
  // The messages arrived and the half second for them to take their height has started.
  const landed = useRef(false)
  // A WhatsApp draft card is on screen. The arrows step aside for it: he is at the foot already.
  const [onDraft, setOnDraft] = useState(false)
  // When the reader last moved up the page, by hand. See watch().
  const upAt = useRef(0)
  /* Older messages asked for and not yet landed: when, the first row's key then, and the message
     the reader could see, to put back where it was. Only a new first row counts as landing, so a
     streaming word drawn in between cannot use the move up. */
  const asked = useRef(null)
  // Where the scroll was at the last scroll event, so a move up from any source can be seen.
  const lastY = useRef(0)
  // The height at the last paint, so new content can be told from a re-render of the same words.
  const tall = useRef(0)
  // There is something below and the reader is not at the bottom. The pill says so.
  const [behind, setBehind] = useState(false)
  // The reader is far enough down that there is a message above to go back to.
  const [above, setAbove] = useState(false)
  // Which folded turns are open. Folds start closed.
  const [open, setOpen] = useState({})
  // An image in a reply opens here, full screen, rather than as a download the app ignores.
  const [image, setImage] = useState(null)
  // A command or a result opens whole, in a panel of its own.
  const [toolId, setToolId] = useState(null)
  // A call opens whole, in the same kind of panel.
  const [callOpen, setCallOpen] = useState(null)
  function tapImage(e) {
    const row = e.target.closest && e.target.closest('[data-tool]')
    if (row && !(window.getSelection && String(window.getSelection()))) {
      setToolId(row.getAttribute('data-tool'))
      return
    }
    const link = e.target.closest && e.target.closest('a.md-img')
    if (!link) { return }
    e.preventDefault()
    const img = link.querySelector('img')
    setImage({ src: link.getAttribute('href'), alt: img ? img.alt : '' })
  }

  /* The three jumps that are allowed: arriving, coming back to the tab, and a message the
     reader sent. The pill uses it too, because tapping the pill is asking to be moved. */
  const jump = useCallback(function () {
    const el = box.current
    stick.current = true
    aim.current = false
    setBehind(false)
    if (!el) { return }
    toBottom(el)
    tall.current = el.scrollHeight
  }, [])

  // A new agent opens at the bottom of its own conversation, never where the last one was.
  /* Copy on a code block: one listener for the whole log, because the blocks are drawn from
     markdown and carry no React handlers of their own. */
  useEffect(function () {
    const el = box.current
    if (!el) { return undefined }
    function click(e) {
      const btn = e.target.closest && e.target.closest('.code-copy')
      if (!btn || !el.contains(btn)) { return }
      const pre = btn.parentElement && btn.parentElement.querySelector('pre')
      const text = pre ? pre.textContent : ''
      const done = function () { btn.textContent = 'Copied'; window.setTimeout(function () { btn.textContent = 'Copy' }, 1500) }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallback(text); done() })
      } else { fallback(text); done() }
    }
    function fallback(text) {
      const t = document.createElement('textarea')
      t.value = text; t.style.position = 'fixed'; t.style.opacity = '0'
      document.body.appendChild(t); t.select()
      try { document.execCommand('copy') } catch { /* nothing more to try */ }
      t.remove()
    }
    el.addEventListener('click', click)
    return function () { el.removeEventListener('click', click) }
  }, [])

  /* Select all picks the one message he was in, not the whole conversation. A phone's Select all
     selects the page, so the message is remembered from where the selection last started, and a
     selection that runs past it is pulled back to that message alone. */
  useEffect(function () {
    const el = box.current
    if (!el) { return undefined }
    let inside = null
    function bodyOf(node) {
      const n = node && (node.nodeType === 1 ? node : node.parentElement)
      const turn = n && n.closest && n.closest('.turn')
      if (!turn || !el.contains(turn)) { return null }
      return turn.querySelector('.bubble') || turn
    }
    function changed() {
      const sel = window.getSelection()
      if (!sel || !sel.rangeCount) { return }
      const range = sel.getRangeAt(0)
      if (range.collapsed) { inside = bodyOf(sel.anchorNode) || inside; return }
      const start = bodyOf(range.startContainer), end = bodyOf(range.endContainer)
      if (start && start === end) { inside = start; return }
      const keep = inside && inside.isConnected ? inside : (start || end)
      if (!keep) { return }
      const whole = document.createRange()
      whole.selectNodeContents(keep)
      if (range.compareBoundaryPoints(Range.START_TO_START, whole) === 0 &&
          range.compareBoundaryPoints(Range.END_TO_END, whole) === 0) { return }
      sel.removeAllRanges()
      sel.addRange(whole)
    }
    document.addEventListener('selectionchange', changed)
    return function () { document.removeEventListener('selectionchange', changed) }
  }, [])

  useEffect(function () {
    stick.current = true
    settling.current = true
    landed.current = false
    window.clearTimeout(settleTimer.current)
    /* A chat with nothing in it never lands. It is his to scroll after a few seconds anyway. */
    settleTimer.current = window.setTimeout(function () { settling.current = false }, 4000)
    asked.current = null
    tall.current = 0
    aim.current = false
    /* oxlint-disable-next-line react/set-state-in-effect -- the agent on screen changed and the
     * last one's pill must not survive it. */
    setBehind(false)
    setFresh({ key: null, count: 0 })
  }, [id])

  // Coming back to the tab is the same as arriving: the panel had no height while it was
  // hidden, so whatever the box did with its scroll position then was meaningless.
  /* Back on the Chat tab: following, it goes to the bottom as before; reading up the page, it goes
     back to his place, which the hidden log did not touch. */
  useEffect(function () {
    if (!active) { return undefined }
    settling.current = true
    /* The messages are already here, so the half second starts now. Waiting for the next render
       to start it left the chat snapping back to the bottom until something re-rendered. */
    window.clearTimeout(settleTimer.current)
    settleTimer.current = window.setTimeout(function () { settling.current = false }, 500)
    const frame = window.requestAnimationFrame(function () {
      const el = box.current
      if (!el || !el.isConnected) { return }
      if (el && aim.current) { toNew(el); return }
      if (stick.current || !el) { jump(); return }
      if (owns(el)) { el.scrollTop = lastY.current } else { window.scrollTo(0, lastY.current) }
    })
    return function () { window.cancelAnimationFrame(frame) }
  }, [active, jump])

  // The reader pressed Send. Their own message may move them, nothing else may.
  /* oxlint-disable-next-line react/set-state-in-effect -- the send happened in another
   * component and this is where the log hears about it. The pill has to go with it. */
  useEffect(function () {
    if (!sendTick) { return }
    jump()
  }, [sendTick, jump])

  /* An ask that brings nothing new never lands, so it lapses after OLDER_WAIT and another may go. */
  const asking = useCallback(function () {
    return !!asked.current && Date.now() - asked.current.at < OLDER_WAIT
  }, [])
  const askOlder = useCallback(function () {
    const el = box.current
    if (!el) { return }
    asked.current = { at: Date.now(), first: rows.length ? rows[0].key : null, seen: firstSeen(el) }
    loadOlder()
  }, [rows, loadOlder])

  /* Asks for older messages when he is at the top. Read by the window listeners, which are set
     once, so it is a ref brought up to date on every render. */
  const topAsk = useRef(null)
  useEffect(function () {
    topAsk.current = function () {
      const el = box.current
      if (!el || !hasMore || asking()) { return }
      const top = owns(el) ? el.scrollTop : window.scrollY
      if (top <= PRELOAD) { askOlder() }
    }
  })

  /* Where the reader is now, recorded on every scroll of whichever thing is scrolling. This is
     the whole of the "was the reader at the bottom" test: the answer is read at the next paint,
     and nothing but a scroll changes it. */
  const watch = useCallback(function () {
    const el = box.current
    // Hidden behind another tab, the page's scroll is that tab's, not the log's.
    if (!el || !shows.current) { return }
    // Still loading: held at the newest line, or at the "New" line, and nothing older is asked for.
    if (settling.current) {
      if (aim.current) { toNew(el) } else if (stick.current && gap(el) > 2) { toBottom(el) }
      return
    }
    // He may go on scrolling while older messages are on their way: keep his place up to date.
    if (asked.current) { asked.current.seen = firstSeen(el) }
    const foot = gap(el) <= AT_FOOT
    const y = owns(el) ? el.scrollTop : window.scrollY
    const movedUp = y < lastY.current - 1
    const now = Date.now()
    /* OWNER_ID'S RULE: A SCROLL UP STOPS FOLLOWING, AND ONLY THE VERY BOTTOM STARTS IT AGAIN
       Any move up, by wheel, finger, key or scrollbar, stops it. Nothing the hand does after that
       counts, a finger slipping back down as it lifts included: that slip used to read as "going
       back down", the log snapped to the bottom on every streamed word, and the screen shook.
       A page that shrinks under him, when a reply ends, only clamps him to the bottom if he was
       already there. While the hand is still moving up, the bottom does not count. */
    /* Following starts again only when he moved DOWN to the bottom. A page that shrinks under
       him, when a reply ends, moves him up to the new bottom, never down, so it cannot count. */
    const movedDown = y > lastY.current + 1
    if (movedUp && !foot) {
      stick.current = false
    } else if (foot && movedDown && now - upAt.current > UP_HOLD) {
      stick.current = true
    }
    lastY.current = y
    // Away from the newest line, the arrow shows, new content or not. It is the way back down.
    setBehind(!foot)
    // At the end he has seen them all, and the count goes. The line stays where it is.
    if (foot) { setFresh(function (f) { return f.count ? { key: f.key, count: 0 } : f }) }
    setAbove(y > HEAD * 3)
    /* Older messages load themselves near the top, on whichever of the two is scrolling. Under
       720px the page scrolls, so this used to wait for a tap on a button that never came. */
    const top = owns(el) ? el.scrollTop : window.scrollY
    if (top < PRELOAD && hasMore && !asking()) { askOlder() }
  }, [hasMore, askOlder, asking])

  // Under 720px the page is the scroller, so the box's own onScroll never fires.
  useEffect(function () {
    window.addEventListener('scroll', watch, { passive: true })
    return function () { window.removeEventListener('scroll', watch) }
  }, [watch])

  /* Height that changes with no render of the log: a picture loading, the composer growing a
     line, the keyboard opening. A reader at the bottom stays at the bottom through all of them. */
  useEffect(function () {
    const el = box.current
    const log = el && el.querySelector('.log')
    if (!el || !log || typeof ResizeObserver !== 'function') { return undefined }
    function pin() {
      if (!shows.current || !el.isConnected || el !== box.current) { return }
      if (aim.current && settling.current) { toNew(el); return }
      if (!stick.current) { return }
      toBottom(el)
      tall.current = el.scrollHeight
      lastY.current = owns(el) ? el.scrollTop : window.scrollY
    }
    /* The keyboard is up when he is typing and the window lost a fifth of the tallest height it
       has had. The tab bar then hides (chat.css), so the chat keeps more than three lines. */
    let tallest = window.innerHeight
    function keyboard() {
      if (!shows.current || !el.isConnected || el !== box.current) { return }
      const h = vvHeight()
      tallest = Math.max(tallest, h)
      const a = document.activeElement
      const typing = !!a && (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT')
      document.documentElement.classList.toggle('kb-up', typing && h < tallest * 0.8)
    }
    function vvHeight() { return window.visualViewport ? window.visualViewport.height : window.innerHeight }
    function both() { keyboard(); pin() }
    /* Only the keyboard really closing (a resize) takes kb-up away, never the text box losing
       focus. A tap on Send takes the focus first: the tab bar came back at once, the composer
       jumped up under the finger, and the tap missed the arrow. */
    function left() { pin() }
    document.addEventListener('focusin', both)
    document.addEventListener('focusout', left)
    const seen = new ResizeObserver(pin)
    seen.observe(log)
    seen.observe(el)
    const composer = document.querySelector('.composer-wrap')
    if (composer) { seen.observe(composer) }
    const vv = window.visualViewport
    if (vv) { vv.addEventListener('resize', both) }
    window.addEventListener('resize', both)
    return function () {
      seen.disconnect()
      if (vv) { vv.removeEventListener('resize', both) }
      window.removeEventListener('resize', both)
      document.removeEventListener('focusin', both)
      document.removeEventListener('focusout', left)
      document.documentElement.classList.remove('kb-up')
    }
  }, [])

  /* The reader's hand going up is a decision, taken at once: a wheel turned up, a finger dragging
     down the glass, Page Up, the up arrow or Home. The log stops following the moment it happens,
     and the arrow at the foot takes them back. */
  useEffect(function () {
    let lastY = null
    /* Only a hand on the log counts. A drag in the draft box, a table scrolled sideways, a pinch
       on a picture or Home in the composer is not him leaving the bottom. */
    function onLog(e) {
      const el = box.current
      const t = e.target
      if (!el || !t || !t.closest) { return false }
      if (t.closest('textarea, input, select, [contenteditable="true"], pre, table, .md-img')) { return false }
      return el.contains(t) || t === document.body || t === document.documentElement
    }
    function up() {
      /* His hand on a chat whose messages are in ends the wait at once: it is his to scroll. */
      if (settling.current) {
        if (!landed.current) { return }
        settling.current = false
      }
      upAt.current = Date.now()
      if (stick.current) { stick.current = false }
      /* At the very top a swipe up moves nothing, so no scroll event comes to ask for more.
         The swipe itself asks. */
      if (topAsk.current) { topAsk.current() }
    }
    function wheel(e) { if (e.deltaY < 0 && onLog(e)) { up() } }
    function start(e) { lastY = e.touches && e.touches[0] && onLog(e) ? e.touches[0].clientY : null }
    function move(e) {
      const y = e.touches && e.touches[0] ? e.touches[0].clientY : null
      if (y !== null && lastY !== null) {
        if (y > lastY + 2) { up() }
        lastY = y
      }
    }
    function key(e) {
      if ((e.key === 'PageUp' || e.key === 'ArrowUp' || e.key === 'Home') && onLog(e)) { up() }
    }
    window.addEventListener('wheel', wheel, { passive: true })
    window.addEventListener('touchstart', start, { passive: true })
    window.addEventListener('touchmove', move, { passive: true })
    window.addEventListener('keydown', key)
    return function () {
      window.removeEventListener('wheel', wheel)
      window.removeEventListener('touchstart', start)
      window.removeEventListener('touchmove', move)
      window.removeEventListener('keydown', key)
    }
  }, [])

  /* Every render, because the live reply grows a word at a time and each word is a render.
     No dependency list on purpose: the question is what the DOM measures now, not what a prop
     says. The one setState in it settles after one pass, because a second pass finds the same
     answer and React stops there. */
  /* oxlint-disable-next-line react-hooks/exhaustive-deps */
  useLayoutEffect(function () {
    const el = box.current
    if (!el || !active) { return }
    const a = asked.current
    if (a && rows.length && rows[0].key !== a.first) {
      // The older messages are in. The message he could see goes back where it was.
      asked.current = null
      const seen = a.seen.find(function (item) { return item.node.isConnected })
      if (seen) {
        const shift = seen.node.getBoundingClientRect().top - seen.top
        if (shift) { if (owns(el)) { el.scrollTop += shift } else { window.scrollBy(0, shift) } }
        // Child effects can finish laying out the newly mounted history after this effect.
        // Measure once more before paint, unless the reader has already moved again.
        const y = owns(el) ? el.scrollTop : window.scrollY
        window.requestAnimationFrame(function () {
          if (!shows.current || !el.isConnected || el !== box.current || !seen.node.isConnected) { return }
          if ((owns(el) ? el.scrollTop : window.scrollY) !== y) { return }
          const rest = seen.node.getBoundingClientRect().top - seen.top
          if (Math.abs(rest) > 1) {
            if (owns(el)) { el.scrollTop += rest } else { window.scrollBy(0, rest) }
          }
        })
      }
      tall.current = el.scrollHeight
      lastY.current = owns(el) ? el.scrollTop : window.scrollY
      return
    }
    /* Too little to scroll: nothing would ever reach the top, so older messages load now. */
    const page = !owns(el)
    const room = page ? document.documentElement.scrollHeight <= window.innerHeight + 2
      : el.scrollHeight <= el.clientHeight + 2
    if (hasMore && room && !asking() && rows.length) { askOlder() }
    const before = tall.current
    tall.current = el.scrollHeight
    // The messages are here: half a second more for images and the settled reply to take their
    // height, then the chat is his to scroll.
    if (settling.current && rows.length && !landed.current) {
      landed.current = true
      window.clearTimeout(settleTimer.current)
      settleTimer.current = window.setTimeout(function () { settling.current = false; aim.current = false }, 500)
      const found = firstUnread(drawn, unreadAfter)
      if (found.key) {
        stick.current = false
        aim.current = true
        /* oxlint-disable-next-line react/set-state-in-effect -- unread is known only once the
         * messages are in. */
        setFresh(found)
        // Below the line is unread, so the arrow and its count show from the start.
        setBehind(true)
        return
      }
    }
    if (aim.current && settling.current) { toNew(el); return }
    if (stick.current) { toBottom(el); return }
    /* The reader is up the page. Nothing moves under them. The pill is the whole of what new
       content is allowed to do while they are reading. */
    if (el.scrollHeight > before) { setBehind(true) }
  })

  /* Back to your previous question, placed in the middle of the screen. Each tap goes one
     question further up: the last of yours whose middle is above the screen's middle. Stops
     following, like any move up. */
  function toPrevious() {
    const el = box.current
    if (!el || !el.isConnected) { return }
    stick.current = false
    upAt.current = Date.now()
    const page = !owns(el)
    const view = page ? { top: 0, height: window.innerHeight }
      : { top: el.getBoundingClientRect().top, height: el.clientHeight }
    const middle = view.top + view.height / 2
    const mine = Array.prototype.slice.call(el.querySelectorAll('.log > .turn.you'))
    let target = null
    for (let i = mine.length - 1; i >= 0; i -= 1) {
      const r = mine[i].getBoundingClientRect()
      if (r.top + r.height / 2 < middle - 24) { target = mine[i]; break }
    }
    if (!target) {
      /* None of his questions above is loaded yet. Go to the top, where older messages load. */
      if (page) { window.scrollTo(0, 0) } else { el.scrollTop = 0 }
      if (topAsk.current) { topAsk.current() }
      return
    }
    const r = target.getBoundingClientRect()
    const shift = (r.top + r.height / 2) - middle
    if (page) { window.scrollTo(0, Math.max(0, window.scrollY + shift)) } else { el.scrollTop += shift }
  }

  /* Your message not yet in the record means the last turn in it is the one before, finished.
     Counting the echo as running drew that turn open for the second the record took. */
  const running = !!(live || thinking || (tools && tools.length) || (said && said.length)) && !echo
  const drawn = fold(withCalls(rows, calls, hasMore, shownJobs), running)

  /* The finished turn stays drawn from `settled` until the rows that replace it are in: the
     history changes, or already ends with the same words. */
  const heldFor = useRef({ held: null, rows: null })
  if (heldFor.current.held !== held) { heldFor.current = { held: held, rows: rows, next: null } }
  // The first rows after the held turn. Only they can hold that turn's error. A turn he did not
  // send from here, such as a job, leaves the held turn in place, so a later error is not its.
  if (!heldFor.current.next && heldFor.current.rows !== rows) { heldFor.current.next = rows }
  const keep = !!held && heldFor.current.rows === rows &&
    !(held.texts.length && alreadySaid(rows, held.texts[held.texts.length - 1]))
  const before = (said || []).concat(keep ? held.texts : [])
    .filter(function (text) { return !inRecord(rows, text) })
  /* The record came in and ends on an API error, and the streamed words are not in it. They stay
     on screen above the error, marked as not saved, keyed by the error row so they stay put. */
  const lost = useRef(new Map())
  const last = rows[rows.length - 1]
  if (held && !keep && heldFor.current.next === rows && apiError(last) &&
    !lost.current.has(id + '\u0000' + last.key)) {
    const gone = held.texts.filter(function (text) { return !inRecord(rows, text) })
    if (gone.length) { lost.current.set(id + '\u0000' + last.key, gone) }
  }

  // One drawn item: a call, a job's card, a message, or a fold of the agent's work.
  function draw(item) {
    if (!item.work && item.row.kind === 'call') {
      return <CallRow key={item.row.key} call={item.row.call} onOpen={setCallOpen} />
    }
    if (!item.work && item.row.kind === 'shown') {
      return <JobCard key={item.row.key} item={item.row.shown} agent={id} />
    }
    if (!item.work) {
      const unsaved = lost.current.get(id + '\u0000' + item.row.key)
      if (!unsaved) { return <Turn key={item.row.key} row={item.row} agent={id} /> }
      return (
        <Fragment key={item.row.key}>
          {unsaved.map(function (text, i) {
            return <LiveReply key={'lost-' + i} text={text} agent={id} done />
          })}
          <p className="stamp">Not saved</p>
          <Turn row={item.row} agent={id} />
        </Fragment>
      )
    }
    const shown = !!open[item.key]
    const n = steps(item.rows)
    return (
      <div className="ch-work" key={item.key}>
        <button
          type="button"
          className="ch-work-toggle"
          aria-expanded={shown}
          onClick={function () {
            setOpen(function (was) { return { ...was, [item.key]: !was[item.key] } })
          }}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M9 6l6 6-6 6"></path>
          </svg>
          {(shown ? 'Hide workings' : 'Show workings') + (n ? ' · ' + n + (n === 1 ? ' step' : ' steps') : '')}
        </button>
        {shown ? item.rows.map(function (r) { return <Turn key={r.key} row={r} agent={id} /> }) : null}
      </div>
    )
  }

  return (
    <div className="chat-scroll" id="chat-scroll" ref={box} onScroll={watch} onClick={tapImage}>
      <div className="log">
        {hasMore ? <p className="ch-older-quiet">Older messages load as you scroll up.</p> : null}

        <Recap recap={recap} onClose={onRecapClose} />

        {drawn.length === 0 && !live && !echo
          ? <p className="ch-empty">Nothing said yet.</p>
          : null}

        {(function () {
          let lastDay = ''
          return runsOf(drawn).map(function (run) {
            const d = dayOf(run.items[0])
            const name = d ? d.toDateString() : lastDay
            const day = name !== lastDay
              ? <p className="ch-day" key={'day-' + name}>{dayName(d)}</p>
              : null
            lastDay = name
            const line = fresh.key && run.items.some(function (it) { return keyOf(it) === fresh.key })
              ? <p className="ch-newline" key={'new-' + fresh.key}>New</p>
              : null
            // The outer group needs a key too. An unkeyed array remounts its messages when
            // older groups are prepended, even when the messages inside it have keys.
            // Use the last item so older system messages can join the start of a run.
            return (
              <Fragment key={keyOf(run.items[run.items.length - 1])}>
                {day}{line}
                {run.items.length > 1
                  ? <SysRun items={run.items}>{run.items.map(draw)}</SysRun>
                  : draw(run.items[0])}
              </Fragment>
            )
          })
        })()}

        {echo
          ? (
            <article className="turn you">
              <Prose text={echo.text} className="bubble" />
              <p className="stamp">{echo.mark}</p>
            </article>
          )
          : null}

        {before.map(function (text, i) {
          return <LiveReply key={'said-' + i} text={text} agent={id} done />
        })}
        {live && !alreadySaid(rows, live) ? <LiveReply text={live} agent={id} /> : null}
        <ToolChips tools={keep && !(tools && tools.length) ? held.tools : tools} />

        {/* Three dots, while the agent is working and has written nothing yet. They go the
            moment the first words arrive, because the words say the same thing better. No dots
            while an ask waits either: the engine is not thinking then, it is waiting on us. */}
        {thinking
          ? (
            <p className="ch-dots" aria-label="The agent is working">
              <span></span><span></span><span></span>
            </p>
          )
          : null}

        {cards.map(function (card) {
          const ask = card.ask
          if (ask.kind === 'perm') {
            return (
              <ApprovalCard
                key={ask.id}
                ask={ask}
                receipt={card.receipt}
                busy={busyAsk === ask.id}
                onAnswer={function (allow) { onPerm(ask, allow) }}
              />
            )
          }
          return (
            <QuestionCard
              key={ask.id}
              ask={ask}
              receipt={card.receipt}
              busy={busyAsk === ask.id}
              onAnswer={function (question, text) { return onQuestion(ask, question, text) }}
              onFinished={function (text) { onQuestionDone(ask, text) }}
              onSkip={function () { onSkip(ask) }}
            />
          )
        })}

        {/* What the daemon said to you: the chat engine taking over from a terminal, an engine
            that exited, an answer it refused. Not in the record, so not a turn, and quiet: one
            line each at the foot of the log. No dot in front. */}
        {/* WhatsApp replies this agent drafted, until he sends or discards them. */}
        <WaDrafts agent={id} onSeen={setOnDraft} />
        <MailDrafts agent={id} framed />

        {(notes || []).map(function (note) {
          return <p className="ch-note" key={note.id}>{note.text}</p>
        })}

        {/* There is more under the fold and the reader is up the page. The only thing new
            content may do while somebody is reading. */}
        {above && !onDraft
          ? (
            <button type="button" className="ch-prev" onClick={toPrevious} aria-label="Go to your previous question">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 19V5M6 11l6-6 6 6"></path>
              </svg>
            </button>
          )
          : null}

        {behind && !onDraft
          ? (
            <button type="button" className="ch-new" onClick={jump} aria-label={fresh.count ? 'Jump to the newest message, ' + fresh.count + ' new' : 'Jump to the newest message'}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 5v14M6 13l6 6 6-6"></path>
              </svg>
              {fresh.count ? <b className="ch-new-count">{fresh.count}</b> : null}
            </button>
          )
          : null}
      </div>
      <ToolView agent={id} id={toolId} onClose={function () { setToolId(null) }} />
      <CallView call={callOpen} name={name || id} onClose={function () { setCallOpen(null) }} />
      <ImageView src={image && image.src} alt={image && image.alt} onClose={function () { setImage(null) }} />
    </div>
  )
}
