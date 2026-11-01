import { state as fetchState, eventsUrl } from './api'
import { chime } from '../shell/chime'

/* The fleet, kept current by one event stream for the whole page.
 *
 * The daemon sends the whole state once, when the stream opens, and after that only what
 * changed: the fields of the one agent that changed, not the fleet. Nothing is fetched while the
 * stream is up. A stream that drops reconnects by itself, and the daemon sends the whole state
 * again on the new one, so a phone back from a tunnel is current without a request of its own.
 *
 * Every screen shares this one stream. Health, the machine snapshot every 2 s, comes only while
 * a screen that draws it is mounted, because on mobile data it was most of the traffic.
 */

const EMPTY = { agents: [], hidden: [], cords: [], jobs: [], health: null, limits: {}, inbox: null }

let snap = EMPTY
let version = -1
let connected = false
let failed = null
let es = null
let healthWanted = 0
let inboxChange = null
const listeners = new Set()
/* The task cards ride on the same stream. Each change is the card itself; null means the stream
 * opened again, so whatever was missed while it was down is read once. */
const taskListeners = new Set()

function tellTasks(change) {
  for (const fn of taskListeners) fn(change)
}

export function onTasks(fn) {
  taskListeners.add(fn)
  return () => { taskListeners.delete(fn) }
}

/* The WhatsApp bridge's status rides on the same stream, pushed when the bridge changes it: a new
 * QR, linked, or new chats seen. Config listens while it is open. */
const whatsappListeners = new Set()

export function onWhatsapp(fn) {
  whatsappListeners.add(fn)
  return () => { whatsappListeners.delete(fn) }
}

/* An email draft added, queued, sent or dropped. The cards reload when it comes. */
const mailListeners = new Set()

export function onMail(fn) {
  mailListeners.add(fn)
  return () => { mailListeners.delete(fn) }
}

/* A feed post, new or newly marked, pushed whole on the same stream. The feed's store listens. */
const postListeners = new Set()

export function onPosts(fn) {
  postListeners.add(fn)
  return () => { postListeners.delete(fn) }
}

function tell() {
  for (const fn of listeners) fn()
}

function set(next) {
  snap = next
  tell()
}

/* A list of records, merged by id: null removes one, fields replace only themselves, and a new
 * id arrives whole. The order comes with the change when it changed. */
function merge(list, changes, order) {
  const byId = new Map(list.map((r) => [r.id, r]))
  for (const [id, fields] of Object.entries(changes || {})) {
    if (fields === null) byId.delete(id)
    else byId.set(id, { ...(byId.get(id) || {}), ...fields })
  }
  const ids = order || [...byId.keys()]
  return ids.map((id) => byId.get(id)).filter(Boolean)
}

function applyDiff(d) {
  if (typeof d.v === 'number' && d.v <= version) return
  if (typeof d.v === 'number') version = d.v
  const next = { ...snap }
  // An agent that was working and now is not has finished its turn: one chime, however many did.
  // A delegate makes no sound: it reports to its parent, and the parent's turn chimes.
  const done = Object.entries(d.agents || {}).some(([id, f]) => {
    const was = snap.agents.find((a) => a.id === id) || {}
    return f && f.working === false && was.working === true && !was.parent
  })
  if (done) chime()
  if (d.agents || d.agents_order) next.agents = merge(snap.agents, d.agents, d.agents_order)
  if (d.jobs || d.jobs_order) next.jobs = merge(snap.jobs, d.jobs, d.jobs_order)
  for (const k of ['hidden', 'cords', 'limits', 'inbox']) if (k in d) next[k] = d[k]
  set(next)
}

function parse(ev) {
  try { return JSON.parse(ev.data) } catch { return null }
}

function open() {
  if (es) es.close()
  const topics = ['state', 'inbox', 'tasks', 'whatsapp', 'posts', 'mail'].concat(healthWanted > 0 ? ['health'] : [])
  es = new EventSource(eventsUrl() + '?topics=' + topics.join(','))
  es.addEventListener('full', (ev) => {
    const s = parse(ev)
    if (!s) return
    version = typeof s.v === 'number' ? s.v : -1
    connected = true
    failed = null
    set({ ...EMPTY, ...s, health: s.health || snap.health })
    tellTasks(null)
  })
  es.addEventListener('state', (ev) => {
    const d = parse(ev)
    if (d) applyDiff(d)
  })
  /* The inbox summary rides on its own event, and the change itself goes to the call, which
   * closes a row a newer item replaced. */
  es.addEventListener('inbox', (ev) => {
    const d = parse(ev)
    if (!d) return
    inboxChange = { event: d.event, id: d.id, by: d.by }
    set(d.summary ? { ...snap, inbox: d.summary } : { ...snap })
  })
  es.addEventListener('tasks', (ev) => {
    const d = parse(ev)
    if (d && d.task) tellTasks(d.task)
  })
  es.addEventListener('whatsapp', (ev) => {
    const d = parse(ev)
    if (d) for (const fn of whatsappListeners) fn(d)
  })
  es.addEventListener('mail', (ev) => {
    const d = parse(ev)
    if (d) for (const fn of mailListeners) fn(d)
  })
  es.addEventListener('posts', (ev) => {
    const d = parse(ev)
    if (d && d.post) for (const fn of postListeners) fn(d.post)
  })
  es.addEventListener('health', (ev) => {
    const h = parse(ev)
    if (h) set({ ...snap, health: h })
  })
  /* A daemon from before the feed ignores the topics, sends no whole state, and pings instead.
   * Then the page works the old way: one fetch now and one per ping. */
  let old = null
  const legacy = () => {
    if (version >= 0) return
    clearTimeout(old)
    old = setTimeout(() => fetchState().then((s) => { if (version < 0) set({ ...EMPTY, ...s }) }, () => {}), 150)
  }
  es.addEventListener('open', () => { if (version < 0) setTimeout(legacy, 1500) })
  for (const k of ['agents', 'cords', 'jobs']) es.addEventListener(k, legacy)
  /* EventSource reconnects on its own, and the daemon sends the whole state again when it does.
   * A page that never got a first state says why, from one plain request. */
  es.onerror = () => {
    connected = false
    if (version >= 0) return
    fetchState().then(
      (s) => { if (version < 0) { failed = null; set({ ...EMPTY, ...s }) } },
      (e) => { failed = e.message; tell() },
    )
  }
}

export function subscribe(fn, wantsHealth = false) {
  listeners.add(fn)
  if (wantsHealth) healthWanted += 1
  if (!es || (wantsHealth && healthWanted === 1)) open()
  return () => {
    listeners.delete(fn)
    if (wantsHealth) {
      healthWanted -= 1
      // Health stops as soon as the last screen that draws it goes.
      if (healthWanted === 0 && es) open()
    }
  }
}

export function snapshot() {
  return snap
}

export function status() {
  return { loading: version < 0 && !failed && snap === EMPTY, error: failed }
}

export function lastInboxChange() {
  return inboxChange
}

/* Asked for after an action, out of habit. While the stream is up it already carries the change,
 * so this fetches only when it is down. */
export async function refresh() {
  if (connected) return
  const s = await fetchState()
  set({ ...EMPTY, ...s })
}
