/* Every call the console makes to the daemon, in one file.
 *
 * Ported from web/app.js, which is the reference implementation. Same paths, same bodies, same
 * error rule: the daemon answers a failure with {"error": "..."} and a non 2xx code, so the
 * string the operator should read is inside the body, not in the status line.
 *
 * No hook calls fetch. Everything goes through here, so a route that moves is one edit.
 */

/* Same origin in production, because the daemon serves the built app itself. VITE_CC_API lets
 * `npm run dev` on another port talk to the live daemon without a proxy. */
const BASE = (import.meta.env && import.meta.env.VITE_CC_API) || ''

export async function api(path, method = 'GET', body) {
  const r = await fetch(BASE + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) {
    /* The status goes with the sentence, so a caller can tell a route this daemon does not have
       yet from a route that refused. */
    const err = new Error(data.error || `${r.status}`)
    err.status = r.status
    throw err
  }
  return data
}

/* Agent ids are [a-z0-9-] at the daemon's own router, so they go in the path as they are. A file
 * name is not, so that one is escaped. */
const at = (id) => `/api/agents/${id}`

/* -- reading ---------------------------------------------------------------- */

/* Everything the dashboard draws, in one answer: agents, hidden ids, cords, jobs, health and the
 * thresholds health is judged against. */
export const state = () => api('/api/state')

export const agent = (id) => api(at(id))
/* He is reading this agent's chat now: everything it has said so far is read. */
export const markRead = (id) => api(at(id) + '/read', 'POST', {})

/* The settled conversation from the session file. The daemon clamps n to 200. */
export const messages = (id, n = 30) => api(`${at(id)}/messages?n=${n}`)

/* Messages typed while the agent slept, still undelivered. */
export const held = (id) => api(`${at(id)}/held`)

/* What this agent publishes for its own screens. */
export const agentData = (id) => api(`${at(id)}/data`)

export const dataByName = (id, name) =>
  api(`/api/data/${id}/${encodeURIComponent(name)}`)

/* Everything the Recruit screen draws: the agents a delete hid, and every Claude Code and
 * OpenCode conversation on this laptop. Read from disk on each ask, so it is asked for on open
 * and not kept. */
export const recruits = () => api('/api/recruits')

/* The Memory screen, read only. The list is every agent the view may see, each with its memory
 * index; memoryFile is one file of one agent, or one day of the front desk's calls. The daemon decides
 * which agents a view may see. */
const viewQuery = (view) => 'view=' + encodeURIComponent(view || 'all')
export const memoryList = (view) => api('/api/memory?' + viewQuery(view))
export const memoryFile = (view, agentId, file) =>
  api('/api/memory?' + viewQuery(view) + '&agent=' + encodeURIComponent(agentId)
    + '&file=' + encodeURIComponent(file))

export const jobs = () => api('/api/jobs')
export const cords = () => api('/api/cords')
export const network = () => api('/api/network')
export const health = () => api('/api/health')
/* What the Integrations screen may say about each service: only what the daemon measured. */
export const integrations = () => api('/api/integrations')
export const whoami = () => api('/api/whoami')

/* The plan, voice minutes and each agent's spend, for the Usage section of
 * Config. The daemon keeps the agent part for a minute. */
export const usage = () => api('/api/usage')

/* -- the agent's life ------------------------------------------------------- */

export const wake = (id) => api(`${at(id)}/wake`, 'POST', {})

/* force kills a busy agent rather than asking it to stop. */
export const sleep = (id, force = false) => api(`${at(id)}/sleep`, 'POST', { force })

/* Halts the task. The agent stays alive and ready for the next message. */
export const stop = (id) => api(`${at(id)}/stop`, 'POST', {})

export const hide = (id) => api(`${at(id)}/hide`, 'POST', {})
export const show = (id) => api(`${at(id)}/show`, 'POST', {})

/* Sleeps it, drops its held mail, forgets its transcript, its server, its port and its jobs. */
export const forget = (id) => api(`${at(id)}/forget`, 'POST', {})

/* -- talking to it ---------------------------------------------------------- */

/* Held by default, the same rule as the old console: a note left from the dashboard must not
 * wake an agent, because waking costs memory. wake=true is for when the message is the reason. */
/* Upload one file into an agent's inbox without the separate arrival note. The chat names the
   path inside the message it sends right after. Answers {ok, path, bytes}. */
export async function upload(id, file) {
  const name = encodeURIComponent((file.name || 'pasted-image.png').replace(/[/\\]/g, '_'))
  const r = await fetch(`${BASE}${at(id)}/inbox/${name}`, {
    method: 'PUT',
    headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Platform-Quiet': '1' },
    body: file,
  })
  let data = {}
  try { data = await r.json() } catch { data = {} }
  if (!r.ok || !data.ok) { throw new Error(data.error || 'The file did not upload.') }
  return data
}

/* One session of a call, recorded in the app. With `part`, one ten-second piece of it, sent
   during the call; piece 0 starts the file. */
export async function saveCallRecording(sessionId, blob, part) {
  const piece = part === undefined ? '' : `?part=${part}`
  const r = await fetch(`${BASE}/api/voice/recording/${encodeURIComponent(sessionId)}${piece}`, {
    method: 'PUT',
    headers: { 'Content-Type': blob.type || 'audio/webm' },
    body: blob,
  })
  if (!r.ok) {
    let data = {}
    try { data = await r.json() } catch { data = {} }
    throw new Error(data.error || 'The recording did not upload.')
  }
}

export const send = (id, text, wakeIt = false) =>
  api(`${at(id)}/message`, 'POST', { text, wake: !!wakeIt })

/* Which engine runs: "chat" or "terminal". Switching never wakes a sleeping agent. */
export const setMode = (id, mode) => api(`${at(id)}/mode`, 'POST', { mode })

/* What Claude may do without asking: acceptEdits, plan or bypassPermissions. */
export const setPermissionMode = (id, mode) =>
  api(`${at(id)}/permission_mode`, 'POST', { mode })

export const PERMISSION_MODES = ['acceptEdits', 'plan', 'bypassPermissions']

/* What a menu says for each mode. The daemon and Claude keep the raw names; a person reads these.
   A mode not here is shown as it stands. */
const PERMISSION_LABELS = {
  acceptEdits: 'Accept edits',
  plan: 'Plan only',
  bypassPermissions: 'Ask for nothing',
  default: 'Ask first',
}
export function permLabel(mode) { return PERMISSION_LABELS[mode] || mode }

/* The models the old console offered, and "default" for the record holding none. The daemon
   passes the string to `claude --model` untouched, so this list is a convenience and not a
   rule: an agent already on a model that is not here keeps it and it is shown as it stands. */
export const MODELS = ['claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-opus-4-6', 'sonnet', 'haiku']

/* The free provider's models, in the order OpenCode lists them. An OpenCode agent may be on any
   of these; a Claude agent keeps the list above. The picker shows whichever list matches the
   engine the record already names, so the two cannot be mistaken for each other. */
export const OPENCODE_MODELS = [
  'opencode/minimax-m3.1-flash-free',
  'opencode/nemotron-3.5-super-vl-free',
  'opencode/beam-free',
  'opencode/longcat-2.5-preview-free',
  'opencode/mimo-v2.6-flash-free',
  'opencode/deepseek-v4.1-flash-free',
  'opencode/glm-5.3-flash-free',
  'opencode/nemotron-3.5-lightning-free',
  'opencode/nemotron-3-ultra-free',
  'opencode/big-pickle'
]

const MODEL_LABELS = {
  'opencode/minimax-m3.1-flash-free': 'MiniMax 3.1',
  'opencode/nemotron-3.5-super-vl-free': 'Nemotron 3.5 VL',
  'opencode/beam-free': 'Beam',
  'opencode/longcat-2.5-preview-free': 'LongCat 2.5',
  'opencode/mimo-v2.6-flash-free': 'MiMo 2.6',
  'opencode/deepseek-v4.1-flash-free': 'DeepSeek 4.1',
  'opencode/glm-5.3-flash-free': 'GLM 5.3',
  'opencode/nemotron-3.5-lightning-free': 'Nemotron 3.5 Lightning',
  'opencode/nemotron-3-ultra-free': 'Nemotron 3 Ultra',
  'opencode/big-pickle': 'Big Pickle'
}

export const modelLabel = (m) => MODEL_LABELS[m] || m

/* OpenCode's own model list, {id, name, free}, read from its prices by the daemon. */
export const opencodeModels = () => api('/api/opencode/models')
/* Codex: the models its signed-in account may use, and its sign-in with a one-time code. */
export const codexModels = () => api('/api/codex/models')
export const codexLogin = () => api('/api/codex/login')
export const codexLoginStart = () => api('/api/codex/login', 'POST', {})

/* The yes, the no, or the chosen option. The daemon looks the ask up among that agent's pending
 * asks, so the browser never has to remember whether it was a permission or a question.
 *
 * A refusal here is not an error code. Two phones on one agent, the other one answered first,
 * and this returns {ok: false, error: "no such ask"} with a 200. Callers check `ok`. */
export const answer = (id, requestId, { allow = false, answers } = {}) =>
  api(`${at(id)}/answer`, 'POST', { id: requestId, allow: !!allow, answers })

/* One held message, sent now. */
export const sendHeld = (id, messageId) => api(`${at(id)}/sendheld`, 'POST', { id: messageId })

/* With a message id, one held message goes. Without one, all of them do. */
export const unhold = (id, messageId) =>
  api(`${at(id)}/unhold`, 'POST', messageId ? { id: messageId } : {})

/* -- making and changing ---------------------------------------------------- */

/* The daemon answers {needs_dir: true, path} rather than failing when the folder is missing, so
 * the caller can show the full path and send the same body back with create_dir set. That is the
 * guard against a mistyped path. */
export const createAgent = (fields) => api('/api/agents', 'POST', fields)

/* name, model, schedule, note, permission_mode, views and role. Anything else is ignored. */
export const updateAgent = (id, fields) => api(at(id), 'PATCH', fields)

/* Bring a hidden agent back. The record never left, so this is one flag on it. */
export const bringBack = (id) => api('/api/recruits/show', 'POST', { id })

/* Make an agent out of a conversation that already exists. session_id is the identity; name and
 * cwd are optional and only override what the transcript says.
 *
 * It answers {needs_dir, path} the same way createAgent does when the folder has gone, so the
 * caller shows the full path and sends the same body back with create_dir, or with another cwd.
 * Nothing is started: the owner wakes the new agent from its card. */
export const adoptSession = (fields) => api('/api/recruits/adopt', 'POST', fields)

export const createJob = (fields) => api('/api/jobs', 'POST', fields)

/* A missed job never fires late, so the offer to run it now has to land somewhere. This is it. */
export const runJob = (id) => api(`/api/jobs/${id}/run`, 'POST', {})

/* task, at, every, enabled, timeout, quiet. */
export const updateJob = (id, fields) => api(`/api/jobs/${id}`, 'PATCH', fields)
export const deleteJob = (id) => api(`/api/jobs/${id}`, 'DELETE')

export const createCord = (fields) => api('/api/cords', 'POST', fields)
export const hangup = (cordId, summary = '') =>
  api(`/api/cords/${cordId}/hangup`, 'POST', { summary })

/* Drop a file the agent offered, or one sent to it. which is "outbox" or "inbox". */
export const unfile = (id, name, which = 'outbox') =>
  api(`${at(id)}/unfile`, 'POST', { name, which })

/* The agent's own server. up:false stops it, anything else starts it. */
export const setServer = (id, up) => api(`${at(id)}/server`, 'POST', { up })

/* -- the voice front desk --------------------------------------------------- */

/* The door. It answers before the browser asks for the microphone and calls OpenAI not at all,
 * so a console with no key says so instead of prompting for a microphone it cannot use. 503 means
 * no key is configured and 402 means the day's minutes are spent; both carry the sentence to
 * show. */
export const voiceSession = (caller = null, resume = false) => {
  /* A resumed call is not a fresh one, so a ring that is up does not take it over. `caller` names
     an agent's call this browser already knows it is on. A plain the front desk call sends neither. */
  const body = {}
  if (caller) { body.caller = caller }
  if (resume) { body.resume = true }
  return api('/api/voice/session', 'POST', body)
}

/* The browser's connection offer, swapped for OpenAI's answer. GPT-Live has no ephemeral client
 * secret, so the daemon opens the session with the project key and passes back an address rather
 * than a credential. This is the moment the call starts and the clock begins.
 *
 * `resume` is the turns of a call he paused. A pause closes the session at OpenAI, so going again
 * opens a new one, and these are what make the two read as one conversation. `back` is the agent
 * that woke a paused call, as {agent, kind, asking}. The browser sends the turns and the facts;
 * the daemon writes the words, so the prompt is assembled in one place only. */
/* The view the console is in, read at the moment of each request, so a switch mid call takes
 * effect at the next tool call. The daemon builds the front desk from that view's agents only, and "all"
 * or nothing is the whole fleet. */
function callView() {
  try { return localStorage.getItem('cc-view') || 'all' } catch { return 'all' }
}

export const voiceOffer = (sdp, resume = null, back = null, sent = null, timing = null, caller = null) => {
  const body = { sdp, view: callView() }
  /* The agent's call this is, when one rang. The daemon opens the session as that agent. */
  if (caller) { body.caller = caller }
  /* How long each step before the offer took, for the daemon's log. Numbers only. */
  if (timing) { body.timing = timing }
  if (resume && resume.length) { body.resume = resume }
  /* The came-back facts not told yet, as a list, and every agent delegated on this call with
     when. The daemon writes the words and answers the sentence as `report`. */
  if (Array.isArray(back) ? back.length : (back && back.agent)) { body.back = back }
  if (sent && sent.length) { body.sent = sent }
  return api('/api/voice/offer', 'POST', body)
}

/* Every tool the voice has goes through this one route, so the browser forwards a tool call
 * without knowing what any of them mean. A tool added in the daemon needs no change here. */
export const voiceTool = (name, args = {}, sessionId = null) =>
  api('/api/voice/tool', 'POST', sessionId
    ? { name, args, view: callView(), session_id: sessionId }
    : { name, args, view: callView() })

/* The referee. {kind: "reply", proposal_id, line} answers {decision, confidence}, one of
 * confirmed, modified, cancelled, unrelated; {kind: "claim", line} answers {claimed, p}. The
 * daemon answers unrelated and not claimed on any fault, so a failure here never acts. */
export const voiceJudge = (body) => api('/api/voice/judge', 'POST', body)
/* What he said while a call was still connecting, written down and sent nowhere. */
export const voiceHeard = (audio, mime, english = false) => api('/api/voice/heard', 'POST', { audio, mime, english })
/* Dictation in the chat: the engine, and a session to start one with. */
/* WhatsApp on Config: linked or the pairing QR, and the chats seen with the agent each goes to.
 * agent null stops following a chat. */
export const whatsappStatus = () => api('/api/whatsapp/status')
export const whatsappChats = () => api('/api/whatsapp/chats')
export const routeWhatsapp = (id, agent) => api('/api/whatsapp/chats', 'POST', { id, agent })
/* Sending is off unless he turns it on. Agents only draft; he sends a draft, as edited, or drops it. */
export const saveWhatsappSending = (sending) => api('/api/whatsapp/settings', 'POST', { sending })
export const whatsappDrafts = () => api('/api/whatsapp/drafts')
export const sendWhatsappDraft = (id, text) => api(`/api/whatsapp/drafts/${id}/send`, 'POST', { text })
export const discardWhatsappDraft = (id) => api(`/api/whatsapp/drafts/${id}/discard`, 'POST', {})

/* Email drafts. Agents only draft; his tap sends one (after a minute's undo) or schedules it. */
export const mailDrafts = () => api('/api/mail/drafts')
export const sendMail = (id, edits) => api(`/api/mail/drafts/${id}/send`, 'POST', edits || {})
export const scheduleMail = (id, at, edits) => api(`/api/mail/drafts/${id}/schedule`, 'POST', { ...(edits || {}), at })
export const cancelMail = (id) => api(`/api/mail/drafts/${id}/cancel`, 'POST', {})
export const discardMail = (id) => api(`/api/mail/drafts/${id}/discard`, 'POST', {})

export const dictateSettings = () => api('/api/dictate')
export const saveDictate = (engine) => api('/api/dictate', 'POST', { engine })
export const openaiUsed = (seconds) => api('/api/openai/used', 'POST', { seconds })
export const openaiUsage = () => api('/api/openai/usage')
export const dictateSession = (engine) => api('/api/dictate/session', 'POST', engine ? { engine } : {})
/* The front desk's recorded greeting, kept by the daemon so the next call plays it at once. */
export const saveVoiceGreeting = (audio) => api('/api/voice/greeting', 'POST', { audio })
/* An agent's "Hi, it's the assistant.", taken from its own first call. */
export const saveAgentGreeting = (agent, audio) => api('/api/voice/greeting', 'POST', { agent, audio })
/* One small talk line while an agent works, written by the daemon. */
export const voiceSmalltalk = () => api('/api/voice/smalltalk', 'POST', {})

/* One finished turn of the conversation, written down as it arrives. who is "you" or "voice". */
export const voiceLog = (who, text, caller = null) =>
  api('/api/voice/log', 'POST', caller ? { who, text, caller } : { who, text })

/* An agent's calls with the owner, answered and missed, newest last, for its chat log. */
export const agentCalls = (id) => api(`${at(id)}/calls`)

/* Job results set to show in the chat, newest last. The agent never reads them. */
export const agentShown = (id) => api(`${at(id)}/shown`)

/* The agent's open list, from OPEN.md in its folder: {exists, path, items: [{n, section, text}]}. */
export const agentOpen = (id) => api(`${at(id)}/open`)

/* A few seconds of one voice, for the play button on Config. The daemon makes it once. */
export const voicePreviewUrl = (voice) => `${BASE}/api/voice/preview/${encodeURIComponent(voice)}`

export const voiceCalls = (n = 40) => api(`/api/voice/log?n=${n}`)

/* An image he pasted, as a data URL, with any words he typed beside it. The live voice cannot
 * see, so the daemon asks a small vision model and answers {said}, the words the voice is then
 * handed. The image is not kept anywhere. */
export const voiceImage = (image, text = '') =>
  api('/api/voice/image', 'POST', { image, text })

/* The front desk's two memory numbers: memory_turns, the turns of the call log in every call's briefing
 * (1 to 100), and recall_span, the turns before and after each match recall hands back (0 to 10).
 * GET answers {memory_turns, recall_span, limits: {name: {min, max, default}}}; the POST saves
 * whichever of the two it is sent and answers the same. The daemon keeps them in
 * var/voice/settings.json, so they survive a restart. */
export const voiceSettings = () => api('/api/voice/settings')
export const voicePersona = () => api('/api/voice/persona')
export const saveVoicePersona = (text) => api('/api/voice/persona', 'POST', { text })
export const restoreVoicePersona = () => api('/api/voice/persona/previous', 'POST', {})
export const agentVoicePrompt = () => api('/api/voice/agent-prompt')
export const saveAgentVoicePrompt = (text) => api('/api/voice/agent-prompt', 'POST', { text })
export const restoreAgentVoicePrompt = () => api('/api/voice/agent-prompt/previous', 'POST', {})
export const instructionFiles = () => api('/api/instructions')
export const instructionFile = (key) => api('/api/instructions/' + key)
export const saveInstructionFile = (key, text) => api('/api/instructions/' + key, 'POST', { text })
export const restoreInstructionFile = (key) => api('/api/instructions/' + key + '/previous', 'POST', {})
export const saveVoiceSettings = (fields) => api('/api/voice/settings', 'POST', fields)

/* The seconds the call lasted, charged to today's budget. `why` is how it ended, for the
 * daemon's log: hangup, back, pagehide, hidden, silence, cap, pause or wait. */
const endBody = (sessionId, seconds, why) =>
  (why ? { session_id: sessionId, seconds, why } : { session_id: sessionId, seconds })

export const voiceEnd = (sessionId, seconds, why) =>
  api('/api/voice/end', 'POST', endBody(sessionId, seconds, why))

/* The same post, for a page that is going away. A beacon is delivered after the page has gone,
 * which a fetch is not. It is a plain POST of the JSON, with no other headers, and the daemon
 * reads it like any other body. Falls back to a keepalive fetch when the browser has no beacon or
 * refuses it. */
export function voiceEndBeacon(sessionId, seconds, why) {
  const text = JSON.stringify(endBody(sessionId, seconds, why))
  try {
    const blob = new Blob([text], { type: 'application/json' })
    if (navigator.sendBeacon && navigator.sendBeacon(BASE + '/api/voice/end', blob)) {
      return true
    }
  } catch {
    // No beacon here. The fetch below is the fallback.
  }
  try {
    fetch(BASE + '/api/voice/end', {
      method: 'POST', keepalive: true, body: text,
      headers: { 'Content-Type': 'application/json' },
    }).catch(() => {})
  } catch {
    // Nothing more can be done for a page that is leaving.
  }
  return false
}

/* -- the inbox -------------------------------------------------------------- */

/* What the front desk has to tell. GET answers {waiting, told, summary}: every waiting item, and every
 * other item changed in the last day with its last decision. Each item is {id, agent, text,
 * urgency, expires, link, added, status, decision, by, p, changed}. */
export const inbox = () => api('/api/inbox')

/* One action on one item: told, handled, later, dismissed, opened, removed or restored. `by`
 * says who, for the record: the call's tap sends "tap", Config sends nothing. */
export const inboxAct = (id, action, by) =>
  api(`/api/inbox/${id}/${action}`, 'POST', by ? { by } : {})

/* Remove every waiting item. The daemon refuses it without the confirm. */
export const inboxClear = () => api('/api/inbox/clear', 'POST', { confirm: true })

/* Jev after a finished line. {moment: "told", line, ids} after hers, {moment: "reply", line,
 * ids, desk} after his. Answers {decisions: [{id, decision, p, agent, link?}]}, only the ones
 * at 0.8 or above, which the daemon has already written down. */
export const inboxJudge = (body) => api('/api/inbox/judge', 'POST', body)

/* -- tasks ------------------------------------------------------------------ */

/* The cards agents offer. GET answers {tasks, offered}: open cards first, then the answered ones
 * of the last three days. Each card is {id, agent, agent_name, side, title, source, deliverable,
 * plan, plan_state, state, created, updated, why, result, result_file, remind, reminded_at}. */
export const tasks = () => api('/api/tasks')

/* Yes and Plan answer {busy: true, agent_name} and change nothing while the agent works, unless
 * mode is "now" (send it anyway) or "remind" (tell him when the agent is free). */
export const taskYes = (id, mode) => api(`/api/tasks/${id}/yes`, 'POST', mode ? { mode } : {})
export const taskPlan = (id, mode) => api(`/api/tasks/${id}/plan`, 'POST', mode ? { mode } : {})
export const taskNo = (id, why) => api(`/api/tasks/${id}/no`, 'POST', why ? { why } : {})
export const taskWhy = (id, why) => api(`/api/tasks/${id}/why`, 'POST', { why })
export const taskDismiss = (id) => api(`/api/tasks/${id}/dismiss`, 'POST', {})
/* The task list: his own tasks, his Yes on a finished one, taking one up again, and the Done
   list ten at a time, older than `before`. */
export const addMyTask = (title, due, owner) => api('/api/tasks/mine', 'POST', { title, ...(due ? { due } : {}), ...(owner ? { owner } : {}) })
export const taskTick = (id) => api(`/api/tasks/${id}/tick`, 'POST', {})
/* A new due time for one of his own tasks, in seconds. */
export const taskMove = (id, due) => api(`/api/tasks/${id}/move`, 'POST', { due })
export const taskClose = (id) => api(`/api/tasks/${id}/close`, 'POST', {})
export const taskReopen = (id) => api(`/api/tasks/${id}/reopen`, 'POST', {})
export const tasksDone = (before) => api('/api/tasks/done' + (before ? '?before=' + encodeURIComponent(before) : ''))

/* The plan prompt on Config. An empty prompt puts the default back. */
export const taskSettings = () => api('/api/tasks/settings')
export const saveTaskSettings = (fields) => api('/api/tasks/settings', 'POST', fields)

/* -- the network panel ------------------------------------------------------ */

/* A button, never a timer. It spends real bandwidth on the link it measures. */
export const speedTest = () => api('/api/network/speed', 'POST', {})

/* -- addresses a screen needs but does not fetch ---------------------------- */

/* One socket carries both engines. Text frames are chat control, binary frames are terminal
 * bytes. 46x150 is what the chat asks for: a terminal is fitted to its smallest watcher, so
 * asking large never shrinks the real terminal on somebody else's phone. */
export function socketUrl(id, { rows = 46, cols = 150, wake: waking = false } = {}) {
  const origin = BASE || window.location.origin
  const u = new URL(`/ws/agent/${id}`, origin)
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'
  u.search = `rows=${rows}&cols=${cols}&wake=${waking ? 1 : 0}`
  return u.toString()
}

export const fileUrl = (id, name) =>
  `${BASE}/file/${id}/${encodeURIComponent(name)}`

export const eventsUrl = () => `${BASE}/api/events`

/* Grouped, so a screen can `import cc from '../../data/api'` and keep its own names for
 * "state" or "send". Named imports work too. */
/* The feed. Agents post; he marks a post done or skipped, or takes the mark back with ''. */
export const posts = (n = 100, days = 0, before = 0) =>
  api('/api/posts?n=' + n + (days ? '&days=' + days : '') + (before ? '&before=' + before : ''))
export const markPost = (id, state) => api(`/api/posts/${id}/mark`, 'POST', { state })

export default {
  upload,
  posts, markPost,
  api,
  state, agent, messages, held, agentData, dataByName, jobs, cords, network, health, whoami, usage,
  recruits, bringBack, adoptSession, memoryList, memoryFile,
  wake, sleep, stop, hide, show, forget,
  send, setMode, setPermissionMode, answer, unhold, sendHeld,
  createAgent, updateAgent, markRead, createJob, runJob, updateJob, deleteJob,
  createCord, hangup, unfile, setServer, speedTest,
  voiceSession, voiceOffer, voiceTool, voiceJudge, voiceSmalltalk, voiceLog, voiceCalls, voiceEnd, voiceEndBeacon,
  voiceImage, agentCalls, agentShown, agentOpen, voicePreviewUrl,
  voiceSettings, saveVoiceSettings, voicePersona, saveVoicePersona, restoreVoicePersona,
  agentVoicePrompt, saveAgentVoicePrompt, restoreAgentVoicePrompt,
  instructionFiles, instructionFile, saveInstructionFile, restoreInstructionFile,
  inbox, inboxAct, inboxClear, inboxJudge,
  tasks, taskYes, taskPlan, taskNo, taskWhy, taskDismiss, taskSettings, saveTaskSettings,
  addMyTask, taskTick, taskMove, taskClose, taskReopen, tasksDone,
  socketUrl, fileUrl, eventsUrl, PERMISSION_MODES,
}
