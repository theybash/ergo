import { useSyncExternalStore } from 'react'
import { posts as fetchPosts } from './api'
import { onPosts } from './fleetStore'

/* The feed's posts, kept current by the one fleet stream.
 *
 * Read once when the first screen asks, and again when the app comes back to the front, because
 * the stream may have been down while it was away. After that every post arrives whole and is
 * merged here by id. Nothing polls.
 *
 * `seen` is when he last opened the feed, kept on this device, so the tab can count what is new
 * since. */

const SEEN_KEY = 'cc.feedSeen'

let snap = { list: [], loaded: false, error: null, seen: readSeen(), more: false, next: 0 }
let loadingOlder = false
let started = false
const listeners = new Set()

function readSeen() {
  try { return Number(window.localStorage.getItem(SEEN_KEY)) || 0 } catch { return 0 }
}

function set(next) {
  snap = { ...snap, ...next }
  for (const fn of listeners) fn()
}

function order(list) {
  return list.slice().sort(function (a, b) { return (b.at || 0) - (a.at || 0) })
}

/* Three days at a time (from user feedback: "don't load the entire feed at once. It will eat
   a lot of data and it doesn't matter"). Older days load as he scrolls to the end (30 October:
   "I can't scroll farther than 3 days?"). New posts still arrive by the stream. */
const DAYS = 3

function load() {
  fetchPosts(100, DAYS).then(
    function (d) {
      // Coming back to the front refreshes the newest days and keeps the older ones he scrolled to.
      const first = !snap.next
      const fresh = d.posts || []
      const ids = new Set(fresh.map(function (p) { return p.id }))
      const kept = first ? [] : snap.list.filter(function (p) { return !ids.has(p.id) })
      set({ list: order(fresh.concat(kept)), loaded: true, error: null,
        ...(first ? { more: !!d.more, next: d.next || 0 } : {}) })
    },
    function (e) { set({ loaded: true, error: e.message }) },
  )
}

/* The next older days, when the feed's end comes into view. */
export function loadOlder() {
  if (loadingOlder || !snap.more || !snap.next) { return }
  loadingOlder = true
  fetchPosts(100, DAYS, snap.next).then(
    function (d) {
      loadingOlder = false
      const ids = new Set(snap.list.map(function (p) { return p.id }))
      const older = (d.posts || []).filter(function (p) { return !ids.has(p.id) })
      set({ list: order(snap.list.concat(older)), more: !!d.more, next: d.next || 0 })
    },
    function () { loadingOlder = false },
  )
}

function merge(post) {
  if (!post || !post.id) { return }
  set({ list: order(snap.list.filter(function (p) { return p.id !== post.id }).concat([post])) })
}

function start() {
  if (started) { return }
  started = true
  load()
  onPosts(merge)
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { load() }
  })
}

export function subscribe(fn) {
  start()
  listeners.add(fn)
  return function () { listeners.delete(fn) }
}

export function snapshot() { return snap }

/* He opened the feed: everything up to now counts as seen. */
export function markSeen() {
  const now = Date.now() / 1000
  try { window.localStorage.setItem(SEEN_KEY, String(now)) } catch { /* private mode */ }
  set({ seen: now })
}

/* His mark, shown at once and then confirmed by the stream. */
export function setMark(post, state) {
  merge({ ...post, state: state })
}

export default function usePosts() {
  return useSyncExternalStore(subscribe, snapshot)
}
