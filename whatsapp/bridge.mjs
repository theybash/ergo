// The WhatsApp bridge: a linked device on the owner's WhatsApp number that reads, and sends only
// what the owner taps Send on.
//
// It reads the chats the owner chose on Config and writes them to var/whatsapp/messages, where the
// daemon hands each agent its own chats. Everything else it sees is dropped at once.
//
// QUIET BY CONSTRUCTION. It never calls readMessages or sendPresenceUpdate, it never marks the owner
// online, and it asks for no full history. A silent device is the kind of linked device where
// WhatsApp's ban risk is lowest, and nothing may speak for the owner: no blue tick, no "online".
//
// SENDING IS OFF BY DEFAULT. The owner wanted it there and off: better to have it and not need it.
// The one send path is POST /send on loopback, with the token from bridge.json, and the daemon
// calls it only when the owner taps Send on the console. Even then this file refuses unless, at that
// moment, settings.json says sending is on and the chat is in the allowlist. Text only, one
// message, after a short random pause. Agents never reach it; they write drafts.
//
// Files, all private to this user (the umask below makes every new file 0600 and folder 0700):
//   ~/.creds/whatsapp/               the linked device's keys (Baileys multi-file auth state)
//   var/whatsapp/status.json         {linked, since, qr_at, error}, for Config
//   var/whatsapp/qr.txt              the raw pairing QR string while pairing, gone once linked
//   var/whatsapp/chats-seen.json     chats seen: id, name, kind, the last message's time and
//                                    whether the owner wrote it (last_me). Never contents
//   var/whatsapp/chats.json          the allowlist, written by the daemon: id -> {name, agent}
//   var/whatsapp/messages/<id>.jsonl the followed chats, kept for 30 days
//   var/whatsapp/media/<chat>__<msg>.<ext>  the photos and files in followed chats, kept for 30 days; the
//                                    message's line names the file in `media`
//   var/whatsapp/settings.json       {"sending": false}, written by the daemon, read per send
//   var/whatsapp/bridge.json         the loopback port and token for /send, read by the daemon
//   var/logs/whatsapp.log            state changes only, never a name or a message. A new file
//                                    each day; the old ones are whatsapp.log.<date>, kept 7 days
//
// ONE LINK PER PROCESS. When a link that was up closes, this process exits and systemd starts a
// fresh one. Once, a reconnect inside the same process came back "linked" but deaf: the
// link was up and nothing arrived for 16 minutes, so an @ergo was missed. Whatever hung stays
// in the process that hung. A message the hung process never confirmed is sent again by WhatsApp
// to the new one, so the gap fills itself. If the link stays up but nothing arrives for
// DEAF_MINUTES in the daytime, the bridge treats itself as deaf, says so, and exits the same way.

import crypto from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import makeWASocket, {
  Browsers, DisconnectReason, downloadMediaMessage, fetchLatestBaileysVersion,
  normalizeMessageContent, useMultiFileAuthState,
} from '@whiskeysockets/baileys'

process.umask(0o077)

const HERE = path.dirname(fileURLToPath(import.meta.url))
const VAR = process.env.PLATFORM_VAR || path.join(HERE, '..', 'var')
const DIR = path.join(VAR, 'whatsapp')
const MESSAGES = path.join(DIR, 'messages')
const MEDIA = path.join(DIR, 'media')
const LOG = path.join(VAR, 'logs', 'whatsapp.log')
const CREDS = process.env.ERGO_WHATSAPP_CREDS ||
  path.join(os.homedir(), '.creds', 'whatsapp')
const API = process.env.PLATFORM_API || 'http://127.0.0.1:8080'
const SEND_PORT = Number(process.env.ERGO_WHATSAPP_PORT || 8097)
const KEEP_DAYS = 30
const DAY = 24 * 3600 * 1000

for (const d of [DIR, MESSAGES, MEDIA, path.dirname(LOG), CREDS]) {
  fs.mkdirSync(d, { recursive: true, mode: 0o700 })
}
fs.chmodSync(CREDS, 0o700)
fs.chmodSync(DIR, 0o700)

// -- small helpers ---------------------------------------------------------------

const LOG_DAYS = 7
let logDay = null

function rotate (today) {
  // The first line of a new day moves yesterday's file aside and drops files past LOG_DAYS.
  try {
    const was = fs.statSync(LOG).mtime.toISOString().slice(0, 10)
    if (was !== today) fs.renameSync(LOG, `${LOG}.${was}`)
  } catch {}
  const cutoff = new Date(Date.now() - LOG_DAYS * DAY).toISOString().slice(0, 10)
  for (const f of fs.readdirSync(path.dirname(LOG))) {
    const m = f.match(/^whatsapp\.log\.(\d{4}-\d{2}-\d{2})$/)
    if (m && m[1] < cutoff) fs.rmSync(path.join(path.dirname(LOG), f), { force: true })
  }
}

function log (line) {
  // State changes only. A chat name or a message never goes here.
  const now = new Date().toISOString()
  if (now.slice(0, 10) !== logDay) { logDay = now.slice(0, 10); rotate(logDay) }
  fs.appendFileSync(LOG, `${now} ${line}\n`, { mode: 0o600 })
}

function writeJson (file, value) {
  // Write then rename, so the daemon never reads half a file.
  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(value, null, 1), { mode: 0o600 })
  fs.renameSync(tmp, file)
}

function readJson (file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')) } catch { return fallback }
}

// The same rule as safe_id() in commandcenter/whatsapp.py. The two must agree.
export function safeId (chat) {
  return String(chat).replace(/[^A-Za-z0-9._-]/g, '_')
}

function kindOf (jid) {
  if (!jid) return null
  if (jid.endsWith('@g.us')) return 'group'
  if (jid.endsWith('@s.whatsapp.net') || jid.endsWith('@lid')) return 'dm'
  return null // status updates, broadcast lists and channels are not chats he follows
}

function number (jid) {
  return '+' + String(jid || '').split('@')[0].split(':')[0]
}

// -- status ----------------------------------------------------------------------

const status = readJson(path.join(DIR, 'status.json'), {})

function poke (what) {
  // Tell the daemon a file changed, so Config redraws at once instead of asking on a timer. The
  // daemon reads the file itself; nothing is in this request. A daemon that is down is fine.
  fetch(`${API}/api/whatsapp/changed`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ what }),
  }).catch(() => {})
}

function setStatus (change) {
  Object.assign(status, change)
  writeJson(path.join(DIR, 'status.json'), status)
  poke('status')
}

function clearQr () {
  fs.rmSync(path.join(DIR, 'qr.txt'), { force: true })
  setStatus({ qr_at: null })
}

// -- chats seen: names only ------------------------------------------------------

const seen = new Map((readJson(path.join(DIR, 'chats-seen.json'), []) || []).map(c => [c.id, c]))
const contactNames = new Map() // jid -> the name saved on the phone, or their own
let seenDirty = false

function noteChat (id, name, at, me) {
  // me: whether the owner wrote the newest message, so an agent can see who owes a reply in a chat
  // nobody follows yet. Unknown (null) when only the time is known.
  const kind = kindOf(id)
  if (!kind) return
  const had = seen.get(id) || { id, kind, name: null, last_at: null }
  const next = { ...had, kind }
  if (name && name !== had.name) next.name = name
  if (at && (!had.last_at || at >= had.last_at)) {
    next.last_at = at
    if (me !== undefined || at > (had.last_at || 0)) next.last_me = me ?? null
  }
  if (JSON.stringify(next) !== JSON.stringify(had)) {
    seen.set(id, next)
    seenDirty = true
  }
}

function flushSeen () {
  if (!seenDirty) return
  seenDirty = false
  writeJson(path.join(DIR, 'chats-seen.json'), [...seen.values()])
  poke('chats')
}
setInterval(flushSeen, 5000).unref()

function noteContact (c) {
  const name = c.name || c.notify || c.verifiedName
  if (!name) return
  for (const jid of [c.id, c.lid, c.phoneNumber]) {
    if (!jid) continue
    contactNames.set(jid, name)
    if (seen.has(jid) && !seen.get(jid).name) noteChat(jid, name)
  }
}

// -- the allowlist ---------------------------------------------------------------

let allow = {}
let allowAt = 0

function allowlist () {
  // Re-read when the daemon changes it, so a chat followed on Config counts at once.
  const file = path.join(DIR, 'chats.json')
  let at = 0
  try { at = fs.statSync(file).mtimeMs } catch { allow = {}; return allow }
  if (at !== allowAt) {
    allow = readJson(file, {}) || {}
    allowAt = at
  }
  return allow
}

// -- turning a message into one line ---------------------------------------------

export function describe (content) {
  // The text, or a short tag for media plus its caption. Never the media itself.
  const m = normalizeMessageContent(content)
  if (!m) return null
  if (m.protocolMessage?.editedMessage) {
    const was = describe(m.protocolMessage.editedMessage)
    return was ? `(edited) ${was}` : null
  }
  const withCaption = (tag, caption) => caption ? `${tag} ${caption}` : tag
  if (m.conversation) return m.conversation
  if (m.extendedTextMessage?.text) return m.extendedTextMessage.text
  if (m.imageMessage) return withCaption('[photo]', m.imageMessage.caption)
  if (m.videoMessage) return withCaption(m.videoMessage.gifPlayback ? '[gif]' : '[video]', m.videoMessage.caption)
  if (m.audioMessage) return m.audioMessage.ptt ? '[voice note]' : '[audio]'
  if (m.documentMessage) {
    return withCaption(`[document: ${m.documentMessage.fileName || 'file'}]`, m.documentMessage.caption)
  }
  if (m.stickerMessage) return '[sticker]'
  if (m.contactMessage) return `[contact: ${m.contactMessage.displayName || ''}]`
  if (m.contactsArrayMessage) return '[contacts]'
  if (m.locationMessage || m.liveLocationMessage) return '[location]'
  const poll = m.pollCreationMessage || m.pollCreationMessageV2 || m.pollCreationMessageV3
  if (poll) return `[poll: ${poll.name || ''}]`
  if (m.eventMessage) return `[event: ${m.eventMessage.name || ''}]`
  return null // reactions, receipts, key exchanges and the rest are not messages to read
}

// -- photos and files --------------------------------------------------------------
// A photo or a file (a PDF, a sheet, a document) in a followed chat is downloaded once and kept
// beside the messages, so an agent can open it (from user feedback: "you can also download
// the files, be it PDF or Excel"). Never video or audio, never a file over FILE_MB, and never from a
// chat he does not follow.

const EXT = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif' }
const FILE_MB = 50

// The extension the sender's file name has, letters and digits only, or one from its type.
function extOf (doc) {
  const named = /\.([A-Za-z0-9]{1,8})$/.exec(doc.fileName || '')
  if (named) return named[1].toLowerCase()
  const type = String(doc.mimetype || '').split(';')[0]
  return EXT[type] || { 'application/pdf': 'pdf' }[type] || 'bin'
}

async function savePhoto (sock, msg, chat) {
  const m = normalizeMessageContent(msg.message)
  const image = m?.imageMessage
  const doc = m?.documentMessage
  if (!(image || doc) || !msg.key?.id) return null
  if (doc && Number(doc.fileLength || 0) > FILE_MB * 1024 * 1024) return null
  const ext = image ? (EXT[image.mimetype] || 'jpg') : extOf(doc)
  const file = path.join(MEDIA, `${safeId(chat)}__${safeId(msg.key.id)}.${ext}`)
  if (fs.existsSync(file)) return file
  // An old photo has usually left WhatsApp's servers; reuploadRequest asks the phone to put it back.
  const data = await Promise.race([
    downloadMediaMessage(msg, 'buffer', {}, { logger: quiet, reuploadRequest: sock.updateMediaMessage }),
    new Promise((resolve, reject) => setTimeout(() => reject(new Error('timeout')), 60000)),
  ])
  fs.writeFileSync(file, data, { mode: 0o600 })
  return file
}

// Ids already written, so a message delivered twice is kept once.
const written = new Set()

function seedWritten () {
  for (const f of fs.readdirSync(MESSAGES)) {
    if (!f.endsWith('.jsonl')) continue
    for (const line of fs.readFileSync(path.join(MESSAGES, f), 'utf8').split('\n')) {
      try { const r = JSON.parse(line); if (r.id) written.add(r.id) } catch {}
    }
  }
}

async function chatIdOf (sock, key) {
  // A direct chat can arrive under a phone number or under WhatsApp's newer private id (LID). The
  // phone number form is preferred so one person is one chat on Config.
  let jid = key.remoteJid
  if (jid?.endsWith('@lid')) {
    if (key.remoteJidAlt?.endsWith('@s.whatsapp.net')) return key.remoteJidAlt
    try {
      const pn = await sock.signalRepository?.lidMapping?.getPNForLID?.(jid)
      if (pn) jid = pn.replace(/:\d+@/, '@')
    } catch {}
  }
  return jid
}

let step = null // what onMessage is doing now, for the stuck-work line

export async function onMessage (sock, msg) {
  const key = msg.key || {}
  step = 'finding the chat id'
  const chat = await chatIdOf(sock, key)
  const kind = kindOf(chat)
  if (!kind) return
  const at = Number(msg.messageTimestamp || 0) || Math.floor(Date.now() / 1000)

  // Every chat is noted by name so the owner can pick it on Config. Never what was said.
  if (kind === 'dm') noteChat(chat, contactNames.get(chat) || (!key.fromMe && msg.pushName) || null, at, !!key.fromMe)
  else noteChat(chat, null, at, !!key.fromMe)

  // Not followed: dropped here, written nowhere.
  const entry = allowlist()[chat]
  if (!entry || !entry.agent) return
  if (key.id && written.has(key.id)) return
  if (at * 1000 < Date.now() - KEEP_DAYS * DAY) return

  const text = describe(msg.message)
  if (!text) return

  let from
  if (key.fromMe) from = 'The owner'
  else {
    const who = kind === 'group' ? (key.participantAlt || key.participant) : chat
    from = contactNames.get(who) || msg.pushName || number(who)
  }
  // me: the owner's own line. The daemon reads it, so a contact with the same name is never taken for the owner.
  const line = { id: key.id || null, at, chat, from, me: !!key.fromMe, text }
  step = 'downloading a photo or file'
  try {
    const file = await savePhoto(sock, msg, chat)
    if (file) line.media = file
  } catch (e) { log(`a photo or file could not be downloaded: ${e?.message || 'error'}`) }
  step = 'writing the message'
  fs.appendFileSync(path.join(MESSAGES, `${safeId(chat)}.jsonl`), JSON.stringify(line) + '\n',
    { mode: 0o600 })
  if (key.id) written.add(key.id)
  // "@ergo" in a followed chat, from the owner or anyone in it, means: now. The daemon reads the
  // message from the file itself and wakes the chat's agent; this poke only says where to look.
  if (/@ergo\b/i.test(text)) poke('mention')
}

// -- keeping 30 days ---------------------------------------------------------------

function prune () {
  const cutoff = Date.now() / 1000 - KEEP_DAYS * 24 * 3600
  let dropped = 0
  for (const f of fs.readdirSync(MESSAGES)) {
    if (!f.endsWith('.jsonl')) continue
    const file = path.join(MESSAGES, f)
    const lines = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean)
    const kept = lines.filter(l => { try { return JSON.parse(l).at >= cutoff } catch { return false } })
    if (kept.length === lines.length) continue
    dropped += lines.length - kept.length
    if (!kept.length) { fs.rmSync(file, { force: true }); continue }
    fs.writeFileSync(`${file}.tmp`, kept.join('\n') + '\n', { mode: 0o600 })
    fs.renameSync(`${file}.tmp`, file)
  }
  if (dropped) log(`pruned ${dropped} lines older than ${KEEP_DAYS} days`)
  for (const f of fs.readdirSync(MEDIA)) {
    const file = path.join(MEDIA, f)
    try { if (fs.statSync(file).mtimeMs < cutoff * 1000) fs.rmSync(file, { force: true }) } catch {}
  }
}

// -- photos saved before photos were kept --------------------------------------------
// Lines written before this as "[photo]" have no file. Once per start, the bridge asks the phone
// for each followed chat's recent messages again (the same request WhatsApp Web makes when you
// scroll up), finds those photos by id, downloads them, and adds `media` to their lines. A photo his
// phone no longer has stays a "[photo]" line. Nothing is sent to anyone in the chat.

const wanted = new Map() // message id -> chat, still without a file
let backfilled = false

function missingPhotos () {
  const out = new Map() // chat -> [{id, at, fromMe}], oldest first, and the newest line as anchor
  for (const f of fs.readdirSync(MESSAGES)) {
    if (!f.endsWith('.jsonl')) continue
    const rows = []
    for (const l of fs.readFileSync(path.join(MESSAGES, f), 'utf8').split('\n')) {
      try { const r = JSON.parse(l); if (r.id) rows.push(r) } catch {}
    }
    const photos = rows.filter(r => /^\[(photo|document:)/.test(r.text || '') && !r.media)
    if (!photos.length || !allowlist()[photos[0].chat]?.agent) continue
    const newest = rows[rows.length - 1]
    const back = rows.length - rows.indexOf(photos[0]) + 5
    out.set(photos[0].chat, { photos, anchor: newest, count: Math.min(back, 50) })
  }
  return out
}

async function backfill (sock) {
  for (const [chat, { photos, anchor, count }] of missingPhotos()) {
    photos.forEach(p => wanted.set(p.id, chat))
    try {
      await sock.fetchMessageHistory(count,
        { remoteJid: chat, id: anchor.id, fromMe: anchor.me ?? anchor.from === 'The owner' }, anchor.at)
      log(`history asked for one chat, ${count} messages`)
    } catch (e) { log(`history request failed: ${e?.message || 'error'}`) }
  }
  if (wanted.size) log(`asked the phone for ${wanted.size} old photos`)
}

function setMedia (chat, id, file) {
  const f = path.join(MESSAGES, `${safeId(chat)}.jsonl`)
  let lines
  try { lines = fs.readFileSync(f, 'utf8').split('\n').filter(Boolean) } catch { return }
  const next = lines.map(l => {
    try { const r = JSON.parse(l); if (r.id === id && !r.media) { r.media = file; return JSON.stringify(r) } } catch {}
    return l
  })
  fs.writeFileSync(`${f}.tmp`, next.join('\n') + '\n', { mode: 0o600 })
  fs.renameSync(`${f}.tmp`, f)
}

async function fromHistory (sock, messages) {
  for (const msg of messages || []) {
    const id = msg.key?.id
    const chat = id && wanted.get(id)
    if (!chat) continue
    try {
      const file = await savePhoto(sock, msg, chat)
      if (file) { setMedia(chat, id, file); wanted.delete(id); log('an old photo or file was saved') }
    } catch (e) { log(`an old photo or file could not be downloaded: ${e?.message || 'error'}`) }
  }
}

// -- the connection ----------------------------------------------------------------

// Baileys logs through pino, and the objects on its lines can carry message contents and phone
// numbers. Warnings and errors are kept, as the short text alone: "Buffer timeout reached" or
// "failed to decrypt message" is the line that shows where a stall began, and it names no one.
function baileysLine (level) {
  return (obj, msg) => {
    const text = typeof obj === 'string' ? obj : (typeof msg === 'string' ? msg : null)
    const err = obj?.err?.message || obj?.error?.message
    if (text) log(`baileys ${level}: ${text.slice(0, 160)}${err ? ` (${String(err).slice(0, 80)})` : ''}`)
  }
}
const quiet = {
  level: 'warn',
  child () { return quiet },
  trace () {}, debug () {}, info () {},
  warn: baileysLine('warn'), error: baileysLine('error'), fatal: baileysLine('fatal'),
}

// -- is it hearing anything ----------------------------------------------------------

const DEAF_MINUTES = 20
const HEARTBEAT_MINUTES = 10
const MIN = 60 * 1000
let linkedAt = null  // when this process's link opened
let lastHeard = null // the last message of any kind, from any chat, followed or not
let heardSince = 0   // messages since the last heartbeat line

function ago (ms) {
  if (!ms) return 'never'
  const m = Math.round((Date.now() - ms) / MIN)
  return m < 1 ? 'just now' : `${m}m ago`
}

function heartbeat () {
  log(`heartbeat: ${linkedAt ? 'linked' : 'not linked'}, ${heardSince} messages in ` +
      `${HEARTBEAT_MINUTES}m, last ${ago(lastHeard)}`)
  heardSince = 0
}

function daytime () {
  // His waking hours, by the laptop's clock. At night silence is normal.
  const h = new Date().getHours()
  return h >= 8 && h < 23
}

async function restartFresh (why) {
  // Exit, and systemd starts a new process in RestartSec. Non-zero, because the unit restarts
  // on failure only.
  log(`exiting for a fresh process: ${why}`)
  setStatus({ linked: false })
  await new Promise(r => setTimeout(r, 300)) // let the status poke leave
  process.exit(75)
}

async function checkDeaf () {
  if (!linkedAt || !daytime()) return
  const quietFor = Date.now() - Math.max(linkedAt, lastHeard || 0)
  if (quietFor < DEAF_MINUTES * MIN) return
  log(`deaf: linked ${ago(linkedAt)}, nothing heard for ${Math.round(quietFor / MIN)}m`)
  // The daemon tells the owner on Telegram. Awaited, with a short cap, because the exit is next.
  await Promise.race([
    fetch(`${API}/api/whatsapp/changed`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ what: 'deaf' }),
    }).catch(() => {}),
    new Promise(r => setTimeout(r, 3000)),
  ])
  await restartFresh('deaf')
}

let backoff = 2000
let lastState = null
let current = null // the live socket, for the one send path
let pairing = false // a QR is up and nobody has scanned it yet

function state (s, detail) {
  if (s === lastState && !detail) return
  lastState = s
  log(detail ? `${s}: ${detail}` : s)
}

async function connect () {
  const { state: auth, saveCreds } = await useMultiFileAuthState(CREDS)
  let version
  try { ({ version } = await fetchLatestBaileysVersion()) } catch {}

  const sock = makeWASocket({
    auth,
    ...(version ? { version } : {}),
    logger: quiet,
    browser: Browsers.ubuntu('Ergo'),
    // Silent: never "online", so the phone keeps its notifications and nobody sees him there.
    markOnlineOnConnect: false,
    // No full history. Baileys still takes the small recent sync it needs to map contacts.
    syncFullHistory: false,
    // Nothing is ever sent, so there is nothing to resend.
    getMessage: async () => undefined,
  })

  current = sock
  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', async (u) => {
    if (u.qr) {
      pairing = true
      fs.writeFileSync(path.join(DIR, 'qr.txt'), u.qr, { mode: 0o600 })
      setStatus({ linked: false, qr_at: new Date().toISOString(), error: null })
      state('waiting for the QR to be scanned')
    }
    if (u.connection === 'open') {
      linkedAt = Date.now()
      backoff = 2000
      pairing = false
      clearQr()
      setStatus({ linked: true, since: status.since || new Date().toISOString(), error: null })
      state('linked')
      try {
        const groups = await sock.groupFetchAllParticipating()
        for (const g of Object.values(groups || {})) noteChat(g.id, g.subject || null)
        flushSeen()
      } catch (e) {
        log(`group list failed: ${e?.output?.statusCode || e?.name || 'error'}`)
      }
      // Once a day, a few seconds in, so the link settles first. Not once per start: the bridge
      // now starts a fresh process at every reconnect, about fifty a day, and a quiet device does
      // not ask the phone for history fifty times a day.
      const stamp = path.join(DIR, 'backfill.json')
      if (!backfilled && Date.now() - (readJson(stamp, {}).at || 0) > DAY) {
        backfilled = true
        writeJson(stamp, { at: Date.now() })
        setTimeout(() => backfill(sock).catch(() => {}), 5000)
      }
    }
    if (u.connection === 'close') {
      const code = u.lastDisconnect?.error?.output?.statusCode
      const reason = String(u.lastDisconnect?.error?.message || 'no reason').slice(0, 80)
      const lasted = linkedAt ? `${Math.round((Date.now() - linkedAt) / MIN)}m` : 'never linked'
      const wasLinked = !!linkedAt
      linkedAt = null
      log(`link closed: code ${code || 'none'}, ${reason}, link lasted ${lasted}`)
      setStatus({ linked: false })
      if (code === DisconnectReason.loggedOut) {
        // He removed the device on the phone. The old keys are useless; start a fresh pairing.
        state('logged out from the phone')
        // The LID to phone number maps are kept: they are not keys, and they keep chat ids stable.
        for (const f of fs.readdirSync(CREDS)) {
          if (!f.startsWith('lid-mapping-')) fs.rmSync(path.join(CREDS, f), { force: true })
        }
        setStatus({ since: null, error: 'Logged out from the phone. Scan a new QR to link again.' })
        return setTimeout(connect, 2000)
      }
      if (code === DisconnectReason.restartRequired) {
        state('restarting after pairing')
        return connect()
      }
      if (code === DisconnectReason.connectionReplaced) {
        // Another copy of this bridge took over. Fighting it would loop, so wait long.
        state('replaced by another connection')
        setStatus({ error: 'Another copy of the bridge is connected.' })
        return setTimeout(connect, 10 * 60 * 1000)
      }
      if (pairing) {
        // The QRs ran out unscanned. That is not an error: drop the stale code and show a new one.
        pairing = false
        clearQr()
        state('QR expired, showing a new one')
        return setTimeout(connect, 3000)
      }
      if (wasLinked) {
        // A link that was up closed: start clean, see ONE LINK PER PROCESS at the top. A link
        // that never opened is retried here with the backoff, so an outage is not a restart loop.
        return restartFresh(`link closed with code ${code || 'none'}`)
      }
      const wait = backoff
      backoff = Math.min(backoff * 2, 5 * 60 * 1000)
      setStatus({ error: `Disconnected (${code || 'no code'}). Retrying.` })
      state('disconnected', `code ${code || 'none'}, retry in ${Math.round(wait / 1000)}s`)
      setTimeout(connect, wait)
    }
  })

  sock.ev.on('contacts.upsert', cs => cs.forEach(noteContact))
  sock.ev.on('contacts.update', cs => cs.forEach(noteContact))
  sock.ev.on('groups.update', gs => gs.forEach(g => g.id && g.subject && noteChat(g.id, g.subject)))
  sock.ev.on('groups.upsert', gs => gs.forEach(g => g.id && noteChat(g.id, g.subject || null)))
  sock.ev.on('chats.upsert', cs => cs.forEach(c => c.name && noteChat(c.id, c.name)))
  // The small recent sync: chat and contact names only. Its messages are history and are not kept.
  sock.ev.on('messaging-history.set', ({ chats, contacts, messages, syncType }) => {
    if (wanted.size) log(`history arrived: ${(messages || []).length} messages, type ${syncType}`)
    ;(contacts || []).forEach(noteContact)
    // The chat's last time comes along; who wrote it does not.
    ;(chats || []).forEach(c => noteChat(c.id, c.name || contactNames.get(c.id) || null,
      Number(String(c.conversationTimestamp || 0)) || null))
    // Only the old photos it was asked for. Every other message in it is history and is dropped.
    if (wanted.size) fromHistory(sock, messages).catch(() => {})
  })

  sock.ev.on('messages.upsert', async ({ messages }) => {
    for (const msg of messages || []) {
      lastHeard = Date.now()
      heardSince++
      // One message should take a second. Past 30 the line names the step it is stuck in.
      const slow = setTimeout(() => log(`a message is taking over 30s, at: ${step || 'unknown'}`),
        30 * 1000)
      try { await onMessage(sock, msg) } catch (e) { log(`a message could not be read: ${e?.name || 'error'}`) }
      finally { clearTimeout(slow); step = null }
    }
  })
}

// -- the one send path, off unless the owner turned it on ---------------------------------

// A photo goes as a photo, with the text as its caption. Anything else goes as a document under
// its own name, so a PDF or a sheet arrives as the file it is.
const MIME = { pdf: 'application/pdf', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  xls: 'application/vnd.ms-excel', csv: 'text/csv', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  doc: 'application/msword', pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  txt: 'text/plain', md: 'text/markdown', zip: 'application/zip', json: 'application/json' }
const PHOTO = { jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp', gif: 'image/gif' }

export function outgoing (file, name, text) {
  const ext = path.extname(name || file).slice(1).toLowerCase()
  const caption = text || undefined
  if (PHOTO[ext]) return { image: { url: file }, mimetype: PHOTO[ext], caption }
  return { document: { url: file }, mimetype: MIME[ext] || 'application/octet-stream',
    fileName: name || path.basename(file), caption }
}

export function mayCurrentlySend (chat) {
  // Read both files now, every time. A switch turned off a second ago counts.
  const settings = readJson(path.join(DIR, 'settings.json'), {}) || {}
  if (settings.sending !== true) return 'sending is off'
  const route = (readJson(path.join(DIR, 'chats.json'), {}) || {})[chat]
  if (!route || !route.agent) return 'that chat is not followed'
  return null
}

function serveSends () {
  const token = crypto.randomBytes(24).toString('hex')
  const server = http.createServer((req, res) => {
    const answer = (code, body) => {
      res.writeHead(code, { 'content-type': 'application/json' })
      res.end(JSON.stringify(body))
    }
    // Loopback names only, against a page that points its own name at 127.0.0.1 (a lesson from
    // the old crodex bridge), and the token only the daemon reads.
    const host = String(req.headers.host || '').replace(/:\d+$/, '').replace(/^\[|\]$/g, '')
    if (!['127.0.0.1', 'localhost', '::1'].includes(host)) return answer(400, { error: 'bad host' })
    if (req.method !== 'POST' || req.url !== '/send') return answer(404, { error: 'no such route' })
    const given = Buffer.from(String(req.headers['x-ergo-token'] || ''))
    const want = Buffer.from(token)
    if (given.length !== want.length || !crypto.timingSafeEqual(given, want)) {
      return answer(403, { error: 'no' })
    }
    let raw = ''
    req.on('data', c => { raw += c; if (raw.length > 64 * 1024) req.destroy() })
    req.on('end', async () => {
      let body
      try { body = JSON.parse(raw) } catch { return answer(400, { error: 'not json' }) }
      const chat = String(body.chat || '')
      const text = String(body.text || '').trim()
      // A photo or file goes only from the daemon's outgoing folder, by the path it chose.
      const file = body.file ? path.resolve(String(body.file)) : null
      if (file && (path.dirname(file) !== path.join(DIR, 'outgoing') || !fs.existsSync(file))) {
        return answer(400, { error: 'that file is not in the outgoing folder' })
      }
      if (!kindOf(chat) || !(text || file) || text.length > 4000) return answer(400, { error: 'give a chat and a text or a file' })
      const refused = mayCurrentlySend(chat)
      if (refused) return answer(403, { error: refused })
      if (!current || !status.linked) return answer(503, { error: 'WhatsApp is not linked' })
      // A short random pause before each send, so taps never leave in a machine rhythm.
      await new Promise(r => setTimeout(r, 1500 + Math.floor(Math.random() * 2500)))
      if (mayCurrentlySend(chat)) return answer(403, { error: 'sending was turned off' })
      try {
        const sent = await current.sendMessage(chat, file ? outgoing(file, String(body.file_name || ''), text) : { text })
        log(file ? 'sent one message with a file' : 'sent one message')
        answer(200, { ok: true, id: sent?.key?.id || null })
      } catch (e) {
        log(`a send failed: ${e?.output?.statusCode || e?.name || 'error'}`)
        answer(502, { error: 'WhatsApp did not take the message' })
      }
    })
  })
  server.on('error', e => log(`send port failed: ${e?.code || 'error'}`))
  server.listen(SEND_PORT, '127.0.0.1', () => {
    writeJson(path.join(DIR, 'bridge.json'), { port: SEND_PORT, token })
  })
}

// Started as a program, not when imported for a quick check.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  setStatus({ linked: false })
  seedWritten()
  prune()
  setInterval(prune, DAY).unref()
  serveSends()
  setInterval(heartbeat, HEARTBEAT_MINUTES * MIN).unref()
  setInterval(() => { checkDeaf().catch(() => {}) }, MIN)
  process.on('unhandledRejection', e => {
    // Logged, then the same crash as before, so systemd still starts a fresh process.
    log(`unhandled rejection: ${String(e?.message || e?.name || 'error').slice(0, 120)}`)
    process.exit(1)
  })
  log('starting')
  connect().catch(e => {
    log(`could not start: ${e?.name || 'error'}`)
    setStatus({ linked: false, error: 'The bridge could not start.' })
    process.exit(1)
  })
}
