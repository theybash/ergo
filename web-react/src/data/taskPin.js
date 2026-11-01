import { useSyncExternalStore } from 'react'

/* The task pinned on top of an agent's chat, after Prompt on its card.
 *
 * Browser memory only. A reload drops it, and that is fine: the task is still on Insights. It
 * sits above the message box like a WhatsApp reply. The message he sends with it carries the
 * task's id and title, so the agent knows which one he means, and then the pin goes.
 *
 *   pin          {task, to, used}   to is the agent's chat; used turns true once the message went
 *                post               set for a feed post: what the post said, for the prefix
 */

let pin = null
const listeners = new Set()

function tell() {
  for (const fn of listeners) fn()
}

/* `to` is the chat it opens in: the task's own agent, or for one of his own tasks the assistant or
   whoever he long-pressed. `reopen` takes a finished task up again when the message goes: his No
   on a finished task, or a tap in the Done list. A plain tap only asks about it. */
export function pinTask(task, to, reopen) {
  pin = task ? { task: task, to: to || task.agent, reopen: !!reopen, used: false } : null
  tell()
}

/* A feed post, quoted the same way in its agent's chat after Chat on the post. The agent has no
   way to read the feed, so the message carries what the post said. */
export function pinPost(post, name) {
  const parts = [post.text]
  if (post.card && post.card.headline) { parts.push('Card: ' + post.card.headline) }
  if (post.link) { parts.push('Link: ' + [post.link.title, post.link.url].filter(Boolean).join(' ')) }
  if (post.draft && post.draft.text) { parts.push('Draft: ' + post.draft.text) }
  // A post from a job's run names the job, whose result the agent already has, and where the
  // run's whole transcript is, for when the result is not enough.
  if (post.job) { parts.push('From your scheduled job ' + post.job) }
  if (post.transcript) { parts.push('Transcript: ' + post.transcript) }
  pin = {
    task: { id: post.id, title: post.text, agent: post.agent, agent_name: name, source: post.kind || '' },
    post: parts.join('\n'), to: post.agent, reopen: false, used: false,
  }
  tell()
}

/* A line from the agent's open list, quoted the same way after a tap in its Open tab. The
   line number lets the agent find it in its OPEN.md. */
export function pinOpen(agent, name, item) {
  pin = {
    task: { id: 'line ' + item.n, title: item.text, agent: agent, agent_name: name, source: '' },
    open: true, to: agent, reopen: false, used: false,
  }
  tell()
}

/* What the front desk's call said, quoted in the calling agent's chat after Prompt on the call screen.
   The call was declined, so the message carries the sentence it rang with. */
export function pinCall(agent, text) {
  pin = {
    task: { id: 'call', title: text, agent: agent, agent_name: '', source: '' },
    call: true, to: agent, reopen: false, used: false,
  }
  tell()
}

/* The task to take up again with the next message to this agent, or nothing. */
export function pinReopens(agent) {
  if (!pin || pin.to !== agent || !pin.reopen) { return null }
  return pin.task.id
}

export function unpin() {
  pin = null
  tell()
}

/* The prefix for the next message to this agent, once, or nothing. */
export function takePrefix(agent) {
  if (!pin || pin.used || pin.to !== agent) { return '' }
  pin = { ...pin, used: true }
  tell()
  const t = pin.task
  if (pin.call) { return `[About your call to him, which he declined to answer in the chat: ${t.title}]` }
  if (pin.open) { return `[About this line of your OPEN.md, ${t.id}: ${t.title}]` }
  if (pin.post) { return `[About your feed post ${t.id}${t.source ? ' (' + t.source + ')' : ''}:\n${pin.post}]` }
  if (t.agent === 'owner') { return `[About his own task ${t.id}: ${t.title}]` }
  if (pin.reopen) {
    return `[About task ${t.id}: ${t.title}. He has taken it up again; it is not done.]`
  }
  return `[About task ${t.id}: ${t.title}]`
}

/* A send that failed gives the prefix back, so the next try still carries it. */
export function giveBack(agent) {
  if (pin && pin.to === agent && pin.used) {
    pin = { ...pin, used: false }
    tell()
  }
}

function subscribe(fn) {
  listeners.add(fn)
  return function () { listeners.delete(fn) }
}

function current() {
  return pin
}

export default function useTaskPin() {
  return useSyncExternalStore(subscribe, current)
}
