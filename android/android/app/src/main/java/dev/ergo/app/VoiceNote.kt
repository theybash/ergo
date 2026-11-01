package dev.ergo.app

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import android.widget.Toast
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/* A voice note for the personal assistant, recorded from the dial: hold on Voice note to record,
   release to send, slide off to throw it away. At most two minutes.

   The mic is used through the running service, which holds the microphone type when Android
   lets it. When it cannot be used, a short toast says why and nothing else happens. The note
   goes to the daemon as JSON with the audio in base64, and the toast is the daemon's answer, or
   "not switched on yet" from a daemon without that route or one that cannot be reached. Every
   note's file is deleted once it is sent or thrown away. */
class VoiceNote(private val ctx: Context) {

    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private var recorder: MediaRecorder? = null
    private var file: File? = null

    /* The loudest the voice has been in this recording, and how loud it is now, both 0 to 1,
       sampled every 100 ms. A note that never rose above SILENT is not sent: silence would only
       become invented words downstream. */
    private var peak = 0f
    private var now = 0f
    private val sample = object : Runnable {
        override fun run() {
            val r = recorder ?: return
            now = try {
                (r.maxAmplitude / 12_000f).coerceIn(0f, 1f)
            } catch (_: Throwable) {
                0f
            }
            if (now > peak) peak = now
            main.postDelayed(this, 100)
        }
    }

    /* When the recording began, on the elapsed realtime clock, or 0 when none is running. */
    var startedAt = 0L
        private set

    val recording: Boolean
        get() = recorder != null

    /* Starts recording. False, with a toast saying why, when the mic cannot be used. */
    fun start(): Boolean {
        if (recorder != null) return true
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            toast("Voice note needs the microphone. Allow it in the app's settings.")
            return false
        }
        if (!CommandService.micReady) {
            toast("Open Ergo once, then the bubble can use the microphone.")
            return false
        }
        val out = File(ctx.cacheDir, "voice-note-${System.currentTimeMillis()}.m4a")
        val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(ctx) else @Suppress("DEPRECATION") MediaRecorder()
        return try {
            r.setAudioSource(MediaRecorder.AudioSource.MIC)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            r.setAudioEncodingBitRate(64_000)
            r.setAudioSamplingRate(44_100)
            r.setMaxDuration(MAX_MS)
            r.setOutputFile(out.absolutePath)
            r.setOnInfoListener { _, what, _ ->
                // Two minutes is the most: the note sends itself.
                if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_DURATION_REACHED) main.post { finish() }
            }
            r.prepare()
            r.start()
            recorder = r
            file = out
            startedAt = SystemClock.elapsedRealtime()
            peak = 0f
            now = 0f
            main.removeCallbacks(sample)
            main.postDelayed(sample, 100)
            true
        } catch (e: Throwable) {
            Log.w(TAG, "the mic would not start", e)
            try {
                r.release()
            } catch (_: Throwable) {
            }
            out.delete()
            toast("The microphone is busy or not allowed right now.")
            false
        }
    }

    /* How loud the voice is right now, 0 to 1. */
    fun level(): Float = if (recorder == null) 0f else now

    /* Stops and throws the note away. Safe to call at any time. */
    fun cancel() {
        val f = stopRecorder()
        f?.delete()
    }

    /* Stops and sends the note. A release that came before any recording began says how to use
       it instead. */
    fun finish() {
        if (recorder == null) {
            toast("Hold on Voice note to record.")
            return
        }
        val took = SystemClock.elapsedRealtime() - startedAt
        val loudest = peak
        val f = stopRecorder() ?: return
        if (loudest < SILENT && !(BuildConfig.DEBUG && testLoud)) {
            f.delete()
            toast("Nothing heard")
            return
        }
        if (took < MIN_MS || f.length() == 0L) {
            f.delete()
            toast("Too short, nothing sent.")
            return
        }
        // The check step: the card shows at once, the note is written down with nothing sent,
        // and the owner then sends, edits or cancels.
        val a = anchor
        card.writing(a?.x ?: 0, a?.y ?: 0, a?.let { it.x < ctx.resources.displayMetrics.widthPixels / 2 } ?: true)
        card.onSend = { words -> sendWords(words) }
        io.execute {
            val answer = try {
                writeDown(f)
            } catch (e: Throwable) {
                Log.i(TAG, "the note was not written down: $e")
                Answer(null, "Voice note is not switched on yet")
            } finally {
                f.delete()
            }
            main.post {
                if (answer.text != null) card.words(answer.text) else card.error(answer.error ?: "That did not work")
            }
        }
    }

    /* Where the bubble is, for the card, set by the overlay before a note finishes. */
    var anchor: android.graphics.Point? = null

    /* The check card; the overlay closes it on every way the bubble goes. */
    val card = NoteCard(ctx)

    private class Answer(val text: String?, val error: String?)

    private fun sendWords(words: String) {
        io.execute {
            val said = try {
                post(Daemon.note + "/send", JSONObject().put("agent", "personal-assistant").put("text", words))
            } catch (e: Throwable) {
                Log.i(TAG, "the note did not send: $e")
                Answer(null, "Voice note is not switched on yet")
            }
            main.post {
                if (said.text != null) {
                    toast(said.text)
                    card.close()
                } else card.error(said.error ?: "That did not send")
            }
        }
    }

    private fun stopRecorder(): File? {
        main.removeCallbacks(sample)
        val r = recorder ?: return null
        recorder = null
        startedAt = 0L
        val f = file
        file = null
        return try {
            r.stop()
            f
        } catch (e: Throwable) {
            // A recording stopped at once has nothing in it.
            f?.delete()
            null
        } finally {
            try {
                r.release()
            } catch (_: Throwable) {
            }
        }
    }

    /* The note written down, with nothing sent: its words, or the error to show. */
    private fun writeDown(f: File): Answer {
        val body = JSONObject()
            .put("agent", "personal-assistant")
            .put("mime", "audio/mp4")
            .put("audio", Base64.encodeToString(f.readBytes(), Base64.NO_WRAP))
            .put("send", false)
        val a = post(Daemon.note, body, key = "text")
        return a
    }

    /* One POST to the daemon: on ok, the answer's `key` (the words, or the line it said); on an
       error, its error text; a daemon without the route says so. */
    private fun post(url: String, body: JSONObject, key: String = "said"): Answer {
        val conn = URL(url).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 3000
            conn.readTimeout = 30_000
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(body.toString().toByteArray()) }
            val code = conn.responseCode
            if (code == 404) return Answer(null, "Voice note is not switched on yet")
            val text = (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.use { it.readBytes() }?.decodeToString().orEmpty()
            val o = try {
                JSONObject(text)
            } catch (_: Throwable) {
                null
            }
            return if (code in 200..299 && o?.optBoolean("ok") == true) {
                val v = o.optString(key)
                if (key == "text") {
                    if (v.isBlank()) Answer(null, "Nothing heard") else Answer(v, null)
                } else Answer(v.ifBlank { "Sent" }, null)
            } else {
                Answer(null, o?.optString("error")?.ifBlank { null } ?: "That did not work")
            }
        } finally {
            conn.disconnect()
        }
    }

    private fun toast(text: String) {
        main.post {
            try {
                Toast.makeText(ctx, text, Toast.LENGTH_SHORT).show()
            } catch (_: Throwable) {
            }
        }
    }

    companion object {
        private const val TAG = "CCVoiceNote"
        private const val MAX_MS = 120_000
        private const val MIN_MS = 600L
        /* The quiet line: a note whose loudest moment stays under this is not sent. */
        private const val SILENT = 0.06f

        /* Debug builds only: lets the emulator's silent mic through, to test a send against a
           stub. The release build never reads it. */
        @Volatile
        var testLoud = false
    }
}
