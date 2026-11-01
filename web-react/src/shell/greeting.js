/* The front desk's greeting with no wait, and nothing he says lost while the call connects.

   The model needs a second or two before it can say anything, so the first words are a
   recording: The front desk's own "Welcome back, sir", taken once from a real call and kept by the
   daemon. It plays the moment a call is tapped. From the end of it until the call is up, the
   microphone is recorded here, and anything said in that gap is written down and handed to
   him once he can hear.

     loadGreeting     the clip as a playable address, fetched once and kept for the page
     playGreeting     play it; resolves when it has finished, or at once if it cannot play
     captureGreeting  record a real greeting from the call's audio, once, and keep it: The front desk's
                      by default, or an agent's "Hi, it's the assistant." when given `save`
     gapRecorder      record the microphone until the call is up, with the loudest moment seen */

import { voiceHeard, saveVoiceGreeting } from '../data/api'

let clip = null
let asked = null
let player = null

export function loadGreeting() {
  if (!asked) {
    asked = fetch('/api/voice/greeting', { cache: 'no-cache' })
      .then(function (r) { return r.ok ? r.blob() : null })
      .then(function (b) {
        clip = b && b.size > 1000 ? URL.createObjectURL(b) : null
        /* Loaded in full now, so the tap plays it from memory and never from a half loaded file. */
        if (clip) {
          player = new Audio(clip)
          player.preload = 'auto'
          player.load()
        }
        return clip
      })
      .catch(function () { clip = null; return null })
  }
  return asked
}

/* What loadGreeting found, now. */
export function greetingNow() { return clip }

/* The clip, waiting at most `ms` for it: a call opened as the page loads would otherwise start
   before the fetch had finished and miss its own greeting. */
export function greetingSoon(ms) {
  if (clip) { return Promise.resolve(clip) }
  return Promise.race([loadGreeting(), new Promise(function (done) { window.setTimeout(done, ms) })])
    .then(function () { return clip })
}

// Fetched as soon as the page loads, not only when a call is made.
loadGreeting()

export function playGreeting(url) {
  return new Promise(function (done) {
    let over = false
    function finish() { if (!over) { over = true; done() } }
    try {
      const a = player && url === clip ? player : new Audio(url)
      a.currentTime = 0
      a.addEventListener('ended', finish)
      a.addEventListener('error', finish)
      a.play().catch(finish)
      // A clip that stalls must not hold the call: it is two seconds long at most.
      window.setTimeout(finish, 4000)
    } catch { finish() }
  })
}

function blobToBase64(blob) {
  return new Promise(function (done, fail) {
    const r = new FileReader()
    r.onload = function () { done(String(r.result).split(',', 2)[1] || '') }
    r.onerror = fail
    r.readAsDataURL(blob)
  })
}

function recorderFor(stream) {
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
  const type = types.find(function (t) { return window.MediaRecorder && MediaRecorder.isTypeSupported(t) })
  return new MediaRecorder(stream, type ? { mimeType: type } : undefined)
}

/* Silence at both ends cut off, so the clip starts the instant it is played. */
function trimmedWav(buffer) {
  const data = buffer.getChannelData(0)
  const floor = 0.02
  let start = 0
  while (start < data.length && Math.abs(data[start]) < floor) { start++ }
  let end = data.length - 1
  while (end > start && Math.abs(data[end]) < floor) { end-- }
  const pad = Math.floor(buffer.sampleRate * 0.04)
  start = Math.max(0, start - pad)
  end = Math.min(data.length - 1, end + pad)
  const n = Math.max(0, end - start)
  const bytes = new DataView(new ArrayBuffer(44 + n * 2))
  const rate = buffer.sampleRate
  const put = function (at, s) { for (let i = 0; i < s.length; i++) { bytes.setUint8(at + i, s.charCodeAt(i)) } }
  put(0, 'RIFF'); bytes.setUint32(4, 36 + n * 2, true); put(8, 'WAVE'); put(12, 'fmt ')
  bytes.setUint32(16, 16, true); bytes.setUint16(20, 1, true); bytes.setUint16(22, 1, true)
  bytes.setUint32(24, rate, true); bytes.setUint32(28, rate * 2, true)
  bytes.setUint16(32, 2, true); bytes.setUint16(34, 16, true)
  put(36, 'data'); bytes.setUint32(40, n * 2, true)
  for (let i = 0; i < n; i++) {
    const v = Math.max(-1, Math.min(1, data[start + i]))
    bytes.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true)
  }
  return new Blob([bytes.buffer], { type: 'audio/wav' })
}

/* Record his first greeting from the call's own audio. The transcript runs ahead of the voice,
   so "sir" in the text only arms the stop: the recording ends when his voice has then been quiet
   for QUIET_MS, or ARMED_MAX after arming, whichever comes first. Trimmed, then sent, once. */
const QUIET_MS = 180
const ARMED_MAX = 1300
const captured = new Set()
export function captureGreeting(stream, key = 'desk', save = null) {
  if (captured.has(key) || !window.MediaRecorder) { return null }
  captured.add(key)
  const chunks = []
  let rec
  let ctx = null
  let node = null
  let watch = 0
  try {
    rec = recorderFor(stream)
    rec.ondataavailable = function (e) { if (e.data && e.data.size) { chunks.push(e.data) } }
    rec.start()
    ctx = new (window.AudioContext || window.webkitAudioContext)()
    node = ctx.createAnalyser()
    ctx.createMediaStreamSource(stream).connect(node)
  } catch { return null }
  function level() {
    const buf = new Uint8Array(node.fftSize)
    node.getByteTimeDomainData(buf)
    let peak = 0
    for (let i = 0; i < buf.length; i++) { peak = Math.max(peak, Math.abs(buf[i] - 128) / 128) }
    return peak
  }
  const api = {
    /* Called when the transcript has reached the end of the greeting: "sir", or the name. */
    arm: function () {
      if (watch) { return }
      const armed = Date.now()
      let quietSince = 0
      watch = window.setInterval(function () {
        const now = Date.now()
        if (level() > 0.04) { quietSince = 0 } else if (!quietSince) { quietSince = now }
        if ((quietSince && now - quietSince >= QUIET_MS) || now - armed >= ARMED_MAX) {
          window.clearInterval(watch)
          api.stop()
        }
      }, 50)
    },
    stop: function () {
      if (ctx) { ctx.close().catch(() => {}); ctx = null }
      if (rec.state === 'inactive') { return }
      rec.onstop = async function () {
        try {
          const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' })
          const ctx = new (window.AudioContext || window.webkitAudioContext)()
          const decoded = await ctx.decodeAudioData(await blob.arrayBuffer())
          ctx.close()
          const wav = trimmedWav(decoded)
          if (wav.size < 4000 || decoded.duration > 6) { return }
          const b64 = await blobToBase64(wav)
          if (save) { await save(b64) } else { await saveVoiceGreeting(b64); loadGreeting() }
        } catch { /* the next call tries again */ captured.delete(key) }
      }
      rec.stop()
    },
    cancel: function () {
      if (watch) { window.clearInterval(watch) }
      if (ctx) { ctx.close().catch(() => {}); ctx = null }
      try { rec.stop() } catch { /* gone */ }
    },
  }
  return api
}

/* The microphone from the end of the greeting until the call is up. The loudest moment is kept,
   so silence is thrown away without a request. */
const SPEECH = 0.06

export function gapRecorder(stream) {
  if (!window.MediaRecorder) { return null }
  const chunks = []
  let peak = 0
  let rec
  let ctx = null
  let timer = 0
  try {
    rec = recorderFor(stream)
    rec.ondataavailable = function (e) { if (e.data && e.data.size) { chunks.push(e.data) } }
    rec.start()
    ctx = new (window.AudioContext || window.webkitAudioContext)()
    const node = ctx.createAnalyser()
    ctx.createMediaStreamSource(stream).connect(node)
    const buf = new Uint8Array(node.fftSize)
    timer = window.setInterval(function () {
      node.getByteTimeDomainData(buf)
      for (let i = 0; i < buf.length; i++) { peak = Math.max(peak, Math.abs(buf[i] - 128) / 128) }
    }, 80)
  } catch { return null }
  function end() {
    window.clearInterval(timer)
    if (ctx) { ctx.close().catch(() => {}); ctx = null }
  }
  return {
    /* What he said in the gap, as text, or '' when he said nothing. */
    take: function () {
      return new Promise(function (done) {
        end()
        if (rec.state === 'inactive') { done(''); return }
        rec.onstop = async function () {
          if (peak < SPEECH || !chunks.length) { done(''); return }
          try {
            const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' })
            const got = await voiceHeard(await blobToBase64(blob), blob.type)
            done((got && got.text) || '')
          } catch { done('') }
        }
        rec.stop()
      })
    },
    cancel: function () { end(); try { rec.stop() } catch { /* gone */ } },
  }
}
