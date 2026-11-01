import { useSyncExternalStore } from 'react'
import { tasks as fetchTasks } from './api'
import { onTasks, subscribe as fleetSubscribe } from './fleetStore'

/* The task cards agents offer, kept current by the one fleet stream.
 *
 * Read once when the first screen asks, and again only when the stream opens again, because
 * something may have changed while it was down. After that every change arrives as the card
 * itself and is merged here by id. Nothing polls.
 *
 * The tab bar asks for the count, so this is loaded as soon as the page is. */

// Answered cards leave the list after three days, the same rule the daemon applies.
const SHOW_ANSWERED = 3 * 86400
const OPEN = ['offered', 'waiting_remind', 'yes', 'talking']

let snap = { list: [], loaded: false, error: null }
let started = 0
let users = 0
let offStream = null
let offFleet = null
const listeners = new Set()

function set(next) {
  snap = { ...snap, ...next }
  for (const fn of listeners) fn()
}

/* Open cards first, newest first, then the answered ones, most recent first. */
function order(list) {
  const now = Date.now() / 1000
  const keep = list.filter(function (t) {
    if (t.state === 'dismissed' || t.state === 'closed' || t.state === 'ticked') { return false }
    // A finished task waits for his tick and his own task waits for him, however old.
    if (t.state === 'done' || t.state === 'mine' || t.state === 'owed') { return true }
    return OPEN.includes(t.state) || now - (t.updated || 0) <= SHOW_ANSWERED
  })
  const open = keep.filter(function (t) { return OPEN.includes(t.state) })
    .sort(function (a, b) { return (b.created || 0) - (a.created || 0) })
  const rest = keep.filter(function (t) { return !OPEN.includes(t.state) })
    .sort(function (a, b) { return (b.updated || 0) - (a.updated || 0) })
  return open.concat(rest)
}

function load() {
  started = Date.now()
  fetchTasks().then(
    function (d) { set({ list: order(d.tasks || []), loaded: true, error: null }) },
    function (e) { set({ loaded: true, error: e.message }) },
  )
}

/* One card, as the daemon sent it, in place of the one held. */
export function merge(task) {
  if (!task || !task.id) { return }
  const rest = snap.list.filter(function (t) { return t.id !== task.id })
  set({ list: order(rest.concat([task])) })
}

export function subscribe(fn) {
  listeners.add(fn)
  users += 1
  if (users === 1) {
    offFleet = fleetSubscribe(function () {})
    offStream = onTasks(function (change) {
      if (change) { merge(change); return }
      // The stream opened. A read that started a moment ago already covers it.
      if (Date.now() - started > 2000) { load() }
    })
    load()
  }
  return function () {
    listeners.delete(fn)
    users -= 1
    if (users === 0) {
      if (offStream) { offStream() }
      if (offFleet) { offFleet() }
      offStream = offFleet = null
    }
  }
}

export function snapshot() {
  return snap
}

export default function useTasks() {
  return useSyncExternalStore(subscribe, snapshot)
}
