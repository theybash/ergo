package dev.ergo.app

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.util.concurrent.TimeUnit
import kotlin.math.abs

/* One dictaphone recording, written down by OpenAI while he speaks (The owner, 26 Oct: "Make
   openai stream the text").

   The mic gives 16-bit sound at 24 kHz, the rate OpenAI's live transcription takes. Each tenth of
   a second goes to OpenAI over one live connection, opened with a short-lived key from the
   laptop, so the audio never passes through the laptop. The laptop's session asks for a low
   delay, so OpenAI sends the words as it hears them, about two seconds behind his voice, and
   makes them final when he stops. Until the connection is up, the sound waits here and goes
   first.

   The same sound is also kept in a WAV file on the phone. If the connection never opens or
   drops, the file is the fallback: the laptop writes it down when he stops, so nothing he said
   is lost. */
class LiveMemo(
    dir: File,
    private val main: Handler,
    private val session: () -> JSONObject,          // POST /api/dictate/session, off the main thread
    private val used: (Double) -> Unit,             // seconds the stream was open, for the usage log
    private val onText: (String) -> Unit,           // everything written down so far
    private val onLevel: (Float) -> Unit,
) {
    val file = File(dir, "memo-${System.currentTimeMillis()}.wav")
    var peak = 0f
        private set
    val startedAt = SystemClock.elapsedRealtime()

    private val pieces = LinkedHashMap<String, String>()   // OpenAI's item id -> its words
    private val waiting = HashSet<String>()                // pieces sent but not yet written down
    private val held = ArrayList<ByteArray>()
    private var ws: WebSocket? = null
    @Volatile private var up = false
    @Volatile private var broken = false
    @Volatile private var running = true
    private var openedAt = 0L
    private var audio: AudioRecord? = null
    private var out: RandomAccessFile? = null
    private var bytes = 0L

    /* Opens the mic. False when it is busy or not allowed; then nothing else happens. */
    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        val min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(min, CHUNK * 4))
        } catch (e: Throwable) {
            Log.w(TAG, "the mic would not open", e)
            return false
        }
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return false
        }
        try {
            out = RandomAccessFile(file, "rw").apply { setLength(0); write(ByteArray(44)) }
            rec.startRecording()
        } catch (e: Throwable) {
            Log.w(TAG, "the mic would not start", e)
            rec.release()
            file.delete()
            return false
        }
        audio = rec
        Thread({ read(rec) }, "memo-mic").start()
        Thread({ connect() }, "memo-ws").start()
        return true
    }

    private fun read(rec: AudioRecord) {
        val buf = ByteArray(CHUNK)
        while (running) {
            val n = rec.read(buf, 0, buf.size)
            if (n <= 0) continue
            val chunk = buf.copyOf(n)
            var top = 0
            var i = 0
            while (i + 1 < n) {
                val s = abs(((chunk[i + 1].toInt() shl 8) or (chunk[i].toInt() and 0xFF)).toShort().toInt())
                if (s > top) top = s
                i += 2
            }
            val level = (top / 12_000f).coerceIn(0f, 1f)
            if (level > peak) peak = level
            main.post { onLevel(level) }
            try { out?.write(chunk); bytes += n } catch (_: Throwable) {}
            synchronized(held) {
                if (up && !broken) send(chunk) else if (!broken) held += chunk
            }
        }
    }

    private fun connect() {
        val s = try {
            session()
        } catch (e: Throwable) {
            Log.i(TAG, "no live session: $e")
            broken = true
            return
        }
        val url = s.optString("url")
        val protocols = s.optJSONArray("protocols")
        if (url.isBlank() || protocols == null) { broken = true; return }
        val names = (0 until protocols.length()).joinToString(", ") { protocols.optString(it) }
        val req = Request.Builder().url(url).header("Sec-WebSocket-Protocol", names).build()
        client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                openedAt = SystemClock.elapsedRealtime()
                Log.i(TAG, "the live connection is open")
                synchronized(held) {
                    ws = webSocket
                    held.forEach { send(it) }
                    held.clear()
                    up = true
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val m = try { JSONObject(text) } catch (_: Throwable) { return }
                val id = m.optString("item_id")
                when (m.optString("type")) {
                    "input_audio_buffer.committed" -> main.post { if (id.isNotBlank()) { waiting += id; pieces.getOrPut(id) { "" } } }
                    "conversation.item.input_audio_transcription.delta" -> main.post {
                        pieces[id] = pieces[id].orEmpty() + m.optString("delta")
                        onText(words())
                    }
                    "conversation.item.input_audio_transcription.completed" -> main.post {
                        pieces[id] = m.optString("transcript").trim()
                        waiting -= id
                        onText(words())
                    }
                    "error" -> {
                        val why = m.optJSONObject("error")?.optString("message").orEmpty()
                        // An empty buffer at the end is normal: the last pause already sent it.
                        if (!why.contains("buffer", ignoreCase = true)) Log.w(TAG, "OpenAI: $why")
                    }
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.i(TAG, "the live connection dropped: $t")
                lost()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (running) lost()
            }
        })
    }

    private fun lost() {
        synchronized(held) { broken = true; up = false; held.clear() }
        report()
    }

    private fun send(chunk: ByteArray) {
        ws?.send(JSONObject().put("type", "input_audio_buffer.append")
            .put("audio", Base64.encodeToString(chunk, Base64.NO_WRAP)).toString())
    }

    fun words(): String = pieces.values.filter { it.isNotBlank() }.joinToString(" ").trim()

    /* True when the live words can stand for the recording: the connection stayed up. */
    val streamed: Boolean get() = openedAt > 0 && !broken

    /* Stops the mic. `done` gets the final words once OpenAI has written down the last piece, at
       most four seconds later; the WAV file is closed and ready either way. */
    fun stop(done: (String) -> Unit) {
        endMic()
        val w = ws
        if (w == null || !up || broken) { close(); main.post { done(words()) }; return }
        w.send(JSONObject().put("type", "input_audio_buffer.commit").toString())
        val from = SystemClock.elapsedRealtime()
        val check = object : Runnable {
            override fun run() {
                val waited = SystemClock.elapsedRealtime() - from
                if ((waiting.isEmpty() && waited > SETTLE_MS) || waited > LAST_WORDS_MS || broken) {
                    close()
                    done(words())
                } else main.postDelayed(this, 100)
            }
        }
        main.postDelayed(check, 100)
    }

    fun cancel() {
        endMic()
        close()
        file.delete()
    }

    private fun endMic() {
        running = false
        try { audio?.stop() } catch (_: Throwable) {}
        try { audio?.release() } catch (_: Throwable) {}
        audio = null
        try {
            out?.let { f ->
                f.seek(0)
                f.write(wavHeader(bytes))
                f.close()
            }
        } catch (_: Throwable) {}
        out = null
    }

    private fun close() {
        val w = ws
        ws = null
        up = false
        try { w?.close(1000, null) } catch (_: Throwable) {}
        report()
    }

    private fun report() {
        if (openedAt == 0L) return
        val s = (SystemClock.elapsedRealtime() - openedAt) / 1000.0
        openedAt = 0L
        Thread { try { used(s) } catch (_: Throwable) {} }.start()
    }

    private fun wavHeader(data: Long): ByteArray {
        val b = java.nio.ByteBuffer.allocate(44).order(java.nio.ByteOrder.LITTLE_ENDIAN)
        b.put("RIFF".toByteArray()).putInt((36 + data).toInt()).put("WAVE".toByteArray())
        b.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(RATE).putInt(RATE * 2)
            .putShort(2).putShort(16)
        b.put("data".toByteArray()).putInt(data.toInt())
        return b.array()
    }

    companion object {
        private const val TAG = "CCLiveMemo"
        const val RATE = 24_000
        private const val CHUNK = RATE / 10 * 2        // a tenth of a second, 16-bit
        private const val SETTLE_MS = 400L
        private const val LAST_WORDS_MS = 4_000L
        private val client = OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS).build()
    }
}
