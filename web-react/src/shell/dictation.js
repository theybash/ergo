/* Dictation: the microphone, written down live into the message box.

   start(hooks) opens the microphone and the engine the daemon names, and calls back:
     hooks.ready()              the microphone is open and listening, before the network is
     hooks.text(final, draft)   what is settled so far, and the words still being guessed
     hooks.level(0..1)          how loud he is, for the line that moves with his voice
     hooks.note(text)           something went wrong that he should see: a dropped connection,
                                or a microphone gone silent
   It returns { done, cancel }. done() stops listening and resolves with the whole text once the
   engine has made it final. cancel() drops everything.

   The live engines take 16-bit PCM, 24 kHz for OpenAI and 16 kHz for Gemini. The browser
   records at whatever rate its hardware runs, so frames are resampled here, in 100 ms chunks.
   The short lived key from the daemon opens one session; the audio goes straight to the
   provider, never through the laptop. */

import { dictateSession, openaiUsed, voiceHeard } from '../data/api'

const WORKLET = `
class Tap extends AudioWorkletProcessor {
  process(inputs) { const ch = inputs[0] && inputs[0][0]; if (ch) this.port.postMessage(ch.slice(0)); return true }
}
registerProcessor('ergo-tap', Tap)
`

function b64(bytes) {
  let s = ''
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
  return btoa(s)
}

function blobToBase64(blob) {
  return new Promise(function (done, fail) {
    const r = new FileReader()
    r.onload = function () { done(String(r.result).split(',', 2)[1] || '') }
    r.onerror = fail
    r.readAsDataURL(blob)
  })
}

/* Audio frames at the hardware rate in, 16-bit chunks of 100 ms at `rate` out. */
function resampler(from, rate, out) {
  const ratio = from / rate, chunk = rate / 10
  let pos = 0, buf = new Int16Array(chunk), n = 0, carry = new Float32Array(0)
  return function (frame) {
    const all = new Float32Array(carry.length + frame.length)
    all.set(carry); all.set(frame, carry.length)
    while (pos + 1 < all.length) {
      const i = Math.floor(pos), f = pos - i
      const v = all[i] * (1 - f) + all[i + 1] * f
      buf[n++] = Math.max(-1, Math.min(1, v)) * 0x7fff
      if (n === chunk) { out(new Uint8Array(buf.buffer.slice(0))); n = 0 }
      pos += ratio
    }
    const keep = Math.floor(pos)
    carry = all.slice(keep); pos -= keep
  }
}

/* Everything that does not need the network starts in the tap itself. Android lets audio run only
   when it is started by a tap, and an audio context made after a network wait could stay paused
   and pass on silence: that was a dictation with a flat line and no words. So the context, the
   microphone and the audio tap start at once, the key and the connection come in the background,
   and what he says before the connection is up is held and sent the moment it opens. */
export function start(hooks) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)()
  ctx.resume().catch(function () {})
  const sessionP = dictateSession()
  const micP = navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
  const tapP = ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' })))
  // The answers may come after a cancel; nobody is waiting on these, so they are not reported.
  sessionP.catch(function () {}); tapP.catch(function () {})
  return open(ctx, sessionP, micP, tapP, hooks)
}

async function open(ctx, sessionP, micP, tapP, hooks) {
  let stream
  try {
    stream = await micP
  } catch (e) {
    ctx.close().catch(function () {})
    throw new Error(e && e.name === 'NotAllowedError'
      ? 'Ergo may not use the microphone. Allow it in the app settings.'
      : 'The microphone did not open.')
  }
  if (ctx.state !== 'running') { await ctx.resume().catch(function () {}) }
  const src = ctx.createMediaStreamSource(stream)
  const meter = ctx.createAnalyser(); meter.fftSize = 512
  src.connect(meter)
  const wave = new Uint8Array(meter.fftSize)
  const note = hooks.note || function () {}
  /* The line moves with the microphone, not with the connection, so it cannot tell him the words
     are getting through. On 16 October a dictation showed "Listening..." and a moving line while
     the connection was gone. Two checks watch for that. A microphone that another app takes
     gives exact zeros, never room noise: three seconds of them gets a note. And four seconds of
     speech with no word back means the connection is lost. */
  let quiet = 0, spoken = 0, onLive = false, words = false, broken = ''
  const tick = window.setInterval(function () {
    meter.getByteTimeDomainData(wave)
    let peak = 0
    for (let i = 0; i < wave.length; i++) peak = Math.max(peak, Math.abs(wave[i] - 128) / 128)
    hooks.level(Math.min(1, peak * 2.2))
    if (peak === 0) { if (++quiet === 50) note('The microphone went quiet. Another app may have it.') } else { quiet = 0 }
    if (onLive && !words && peak > 0.1 && ++spoken === 67) lose('No words came back.')
  }, 60)
  let lose = function (why) { broken = broken || why }

  // A recorder too, from the first moment, for the whole dictation. The not-live engine needs it,
  // and a live engine that loses its connection falls back to it, so nothing said is lost.
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
  const type = types.find(function (t) { return window.MediaRecorder && MediaRecorder.isTypeSupported(t) })
  let rec = null, chunks = []
  try {
    rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined)
    rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data) }
    rec.start()
  } catch { rec = null }

  // The tap, and the audio it gives held until there is somewhere to send it.
  let held = [], feed = null
  try {
    await tapP
    const tap = new AudioWorkletNode(ctx, 'ergo-tap')
    src.connect(tap)
    tap.port.onmessage = function (e) { if (feed) { feed(e.data) } else { held.push(e.data) } }
  } catch { /* no worklet: the live engines cannot work, the recorder still can */ }

  let closed = false
  function closeMic() {
    if (closed) return
    closed = true
    window.clearInterval(tick)
    feed = null; held = []
    stream.getTracks().forEach(function (t) { t.stop() })
    ctx.close().catch(function () {})
  }
  hooks.ready()

  let s
  try {
    s = await sessionP
  } catch (e) {
    closeMic()
    throw e
  }
  if (s.engine === 'record') { return recordOnly(rec, chunks, hooks, closeMic) }
  const dropRec = function () { if (rec) { rec.ondataavailable = null; try { rec.stop() } catch { /* gone */ } rec = null } }

  const finals = []
  let draft = ''
  let finished = null, ending = false, up = false
  const said = function () { hooks.text(finals.join(' ').trim(), draft) }
  const ws = s.protocols ? new WebSocket(s.url, s.protocols) : new WebSocket(s.url)
  // The minutes this stream was open go to the OpenAI usage log. Only the browser knows them.
  let openedAt = 0
  ws.addEventListener('open', function () { openedAt = Date.now() })
  ws.addEventListener('close', function () {
    if (openedAt && s.engine === 'openai') { openaiUsed((Date.now() - openedAt) / 1000).catch(function () {}) }
    openedAt = 0
  })
  const opened = new Promise(function (ok) {
    ws.onopen = function () { up = true; if (s.setup) { ws.send(JSON.stringify({ setup: s.setup })) } ok(true) }
    const gone = function () { ok(false); if (!ending) lose(up ? 'The live connection dropped.' : 'The live connection did not open.') }
    ws.onerror = gone
    ws.onclose = gone
  })
  // From here a lost connection stops the live audio and tells him. What he says still goes on
  // the recording, and that is written down when he stops.
  lose = function (why) {
    if (broken || closed) return
    broken = why
    feed = null
    try { ws.close() } catch { /* gone */ }
    note(why)
    // The box says it too, and keeps saying it after the note goes.
    if (rec) hooks.text('', 'Recording. Ergo writes it down when you stop.')
  }
  if (broken) { const why = broken; broken = ''; lose(why) }
  ws.onmessage = async function (ev) {
    const text = typeof ev.data === 'string' ? ev.data : await ev.data.text()
    let m
    try { m = JSON.parse(text) } catch { return }
    if (s.engine === 'openai') {
      if (m.type === 'error' && !ending) { lose('Dictation failed: ' + ((m.error && m.error.message) || 'an error from OpenAI') + '.'); return }
      if (m.type === 'conversation.item.input_audio_transcription.delta') { words = true; draft += m.delta || ''; said() }
      if (m.type === 'conversation.item.input_audio_transcription.completed') {
        finals.push((m.transcript || '').trim()); draft = ''; said()
        if (finished) finished()
      }
    } else {
      const sc = m.serverContent || {}
      if (sc.interimInputTranscription || sc.inputTranscription) { words = true }
      if (sc.interimInputTranscription) { draft = sc.interimInputTranscription.text || ''; said() }
      if (sc.inputTranscription) { finals.push((sc.inputTranscription.text || '').trim()); draft = ''; said() }
      if (sc.speechState === 'NON_SPEECH' && finished) finished()
    }
  }
  const send = function (bytes) {
    if (ws.readyState !== 1) return
    const data = b64(bytes)
    ws.send(JSON.stringify(s.engine === 'openai'
      ? { type: 'input_audio_buffer.append', audio: data }
      : { realtimeInput: { audio: { data: data, mimeType: 'audio/pcm;rate=' + s.rate } } }))
  }
  // Once the connection is up, everything held goes first, then the live audio as it comes.
  const live = opened.then(function (ok) {
    if (!ok) { lose('The live connection did not open.'); return false }
    if (closed || broken) return false
    onLive = true
    const f = resampler(ctx.sampleRate, s.rate, send)
    held.forEach(f); held = []
    feed = f
    return true
  })

  return {
    maxSeconds: s.max_seconds,
    done: function () {
      return live.then(function (ok) {
        // Whatever arrived up to this tap is in; the microphone stops now.
        feed = null
        ending = true
        const shown = (finals.join(' ') + ' ' + draft).trim()
        if (!ok || broken) {
          try { ws.close() } catch { /* gone */ }
          return writeDown(rec, chunks, hooks, closeMic).then(function (got) { return got || shown })
        }
        dropRec()
        closeMic()
        return new Promise(function (resolve) {
          const end = function () { ws.close(); resolve((finals.join(' ') + ' ' + draft).trim()) }
          // The last words are made final once the stream ends. Four seconds at most, then the
          // draft counts as said, so a slow answer never loses what is on screen.
          const late = window.setTimeout(end, draft ? 4000 : 1500)
          finished = function () { window.clearTimeout(late); end() }
          ws.send(JSON.stringify(s.engine === 'openai' ? { type: 'input_audio_buffer.commit' } : { realtimeInput: { audioStreamEnd: true } }))
        })
      })
    },
    cancel: function () { ending = true; dropRec(); closeMic(); try { ws.close() } catch { /* gone */ } },
  }
}

/* The whole clip on the recorder, written down by the daemon. '' when there is none. */
function writeDown(rec, chunks, hooks, closeMic) {
  return new Promise(function (resolve) {
    if (!rec) { closeMic(); resolve(''); return }
    rec.onstop = async function () {
      closeMic()
      hooks.text('', 'Writing it down...')
      try {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' })
        const got = await voiceHeard(await blobToBase64(blob), blob.type, true)
        resolve((got && got.text) || '')
      } catch { resolve('') }
    }
    rec.stop()
  })
}

/* Not live: the recorder has run since the tap; the whole clip is written down when he stops. */
function recordOnly(rec, chunks, hooks, closeMic) {
  hooks.text('', '')
  return {
    maxSeconds: 300,
    done: function () { return writeDown(rec, chunks, hooks, closeMic) },
    cancel: function () { try { if (rec) { rec.stop() } } catch { /* gone */ } closeMic() },
  }
}
