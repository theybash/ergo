/* A call, recorded in the app: his microphone and the voice that answers, mixed into one file.

   Android's own screen recorder leaves call audio out on purpose, so a recording of a call has
   to be made here, the one place that hears both sides. The audio goes phone to OpenAI and never
   passes through the laptop, so each ten-second piece is sent there as it is made, in order, and
   a call that drops keeps what it had. Config turns it off; the daemon
   says which, in the answer to each offer, the front desk's and an agent's alike.

   Its own AudioContext, so the call's meters and the recording never share one that either side
   might close. A failure anywhere records nothing and leaves the call alone. */

import { saveCallRecording } from '../data/api'

const TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus']

export function startRecording(mic, remote, sessionOf) {
  try {
    if (!window.MediaRecorder || !window.AudioContext || !mic || !remote) { return null }
    const ctx = new AudioContext()
    const out = ctx.createMediaStreamDestination()
    ctx.createMediaStreamSource(mic).connect(out)
    ctx.createMediaStreamSource(remote).connect(out)
    const type = TYPES.find(function (t) { return MediaRecorder.isTypeSupported(t) })
    const rec = new MediaRecorder(out.stream, type ? { mimeType: type } : undefined)
    // Pieces wait here until the session has its id, then go up one after another. Only the
    // first piece carries the file's header, so the order matters.
    const waiting = []
    let sent = 0
    let line = Promise.resolve()
    let fixed = ''
    function flush(id) {
      if (!id) { return }
      while (waiting.length) {
        const blob = waiting.shift()
        const n = sent++
        line = line.then(function () {
          return saveCallRecording(id, blob, n).catch(function () { /* a lost piece is a gap */ })
        })
      }
    }
    rec.addEventListener('dataavailable', function (ev) {
      if (!ev.data || !ev.data.size) { return }
      waiting.push(new Blob([ev.data], { type: rec.mimeType || 'audio/webm' }))
      flush(fixed || (sessionOf ? sessionOf() : ''))
    })
    rec.start(10000)
    // The app keeps the call alive off screen, but the web view may suspend this context there.
    function wake() { if (ctx.state === 'suspended') { ctx.resume().catch(function () {}) } }
    document.addEventListener('visibilitychange', wake)
    return {
      stop(sessionId) {
        document.removeEventListener('visibilitychange', wake)
        fixed = sessionId || ''
        rec.addEventListener('stop', function () { ctx.close().catch(function () {}) }, { once: true })
        // The last piece arrives as a dataavailable just before stop, and goes up the same way.
        try { rec.stop() } catch { ctx.close().catch(function () {}) }
      },
    }
  } catch {
    return null
  }
}
