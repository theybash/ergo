package dev.ergo.app

import android.app.Activity
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/* The last thing he asked from the bar, and its answer. He does not wait for it (The owner, 26 Oct:
   "Do I have to wait for the agent to come back"): he sends and leaves, and the next time the bar
   opens it shows the thread. When the answer comes while the bar is closed, a quiet notification
   says so, and a tap opens the bar on it.

   It lives in the app's process, not in the bar: the bar is gone a second after he leaves, while
   the assistant service keeps the process alive. One thread at a time. A new question replaces
   it, the x on the card clears it, and it clears itself an hour after he has read the answer. */
object AssistThread {
    class State(
        val agent: String,
        val name: String,
        val question: String,
        val turns: Int,
        val at: Long,
        val answer: String?,
        val readAt: Long,
    )

    private const val TAG = "CCAssistThread"
    private const val KEY = "thread"
    private const val NOTE_ID = 7_401
    private const val KEEP_READ_MS = 60 * 60_000L     // an hour after he read it
    private const val GIVE_UP_MS = 30 * 60_000L       // an agent quiet for half an hour: the chat has it

    private val main = Handler(Looper.getMainLooper())
    private val waiter = Executors.newSingleThreadExecutor()
    private var watching = 0

    /* The open bar, if any. It hears the answer the moment it comes. */
    @Volatile var listener: ((State) -> Unit)? = null

    fun get(ctx: Context): State? {
        val s = ctx.getSharedPreferences("assist", Context.MODE_PRIVATE).getString(KEY, null) ?: return null
        val t = try {
            val o = JSONObject(s)
            State(o.getString("agent"), o.optString("name", o.getString("agent")), o.optString("question"),
                o.optInt("turns"), o.optLong("at"), o.optString("answer").takeIf { o.has("answer") && it.isNotBlank() },
                o.optLong("readAt"))
        } catch (_: Throwable) {
            return null
        }
        val now = System.currentTimeMillis()
        val stale = (t.readAt > 0 && now - t.readAt > KEEP_READ_MS) ||
            (t.answer == null && now - t.at > GIVE_UP_MS)
        if (stale) {
            clear(ctx)
            return null
        }
        return t
    }

    private fun put(ctx: Context, t: State) {
        val o = JSONObject().put("agent", t.agent).put("name", t.name).put("question", t.question)
            .put("turns", t.turns).put("at", t.at).put("readAt", t.readAt)
        t.answer?.let { o.put("answer", it) }
        ctx.getSharedPreferences("assist", Context.MODE_PRIVATE).edit().putString(KEY, o.toString()).apply()
    }

    fun clear(ctx: Context) {
        watching++
        ctx.getSharedPreferences("assist", Context.MODE_PRIVATE).edit().remove(KEY).apply()
        cancelNote(ctx)
    }

    /* He sent a question: keep it, and wait for the answer in the background. */
    fun start(ctx: Context, agent: String, name: String, question: String, turns: Int) {
        val t = State(agent, name, question, turns, System.currentTimeMillis(), null, 0L)
        put(ctx, t)
        cancelNote(ctx)
        watch(ctx.applicationContext, t)
    }

    /* The bar opened on a thread with no answer yet: make sure somebody is waiting for it. */
    fun resume(ctx: Context, t: State) {
        if (t.answer == null) watch(ctx.applicationContext, t)
    }

    fun markRead(ctx: Context) {
        val t = get(ctx) ?: return
        if (t.answer != null && t.readAt == 0L) put(ctx, State(t.agent, t.name, t.question, t.turns, t.at, t.answer, System.currentTimeMillis()))
        cancelNote(ctx)
    }

    private fun watch(app: Context, t: State) {
        val seq = ++watching
        waiter.execute {
            // Each ask waits up to 45 seconds on the laptop; keep asking until it answers, the
            // thread changes, or half an hour has passed.
            while (seq == watching && System.currentTimeMillis() - t.at < GIVE_UP_MS) {
                val text = try {
                    val conn = URL(Daemon.base + "/api/assist/reply?agent=" +
                        android.net.Uri.encode(t.agent) + "&turns=" + t.turns).openConnection() as HttpURLConnection
                    conn.connectTimeout = 4000
                    conn.readTimeout = 55_000
                    val body = conn.inputStream.use { it.readBytes().toString(Charsets.UTF_8) }
                    conn.disconnect()
                    JSONObject(body).optString("text").takeIf { it.isNotBlank() && it != "null" }
                } catch (e: Throwable) {
                    Log.i(TAG, "no answer yet: $e")
                    try { Thread.sleep(15_000) } catch (_: InterruptedException) {}
                    null
                }
                if (text != null) {
                    main.post {
                        if (seq != watching) return@post
                        val done = State(t.agent, t.name, t.question, t.turns, t.at, text.trim(), 0L)
                        put(app, done)
                        val bar = listener
                        if (bar != null) bar(done) else note(app, done)
                    }
                    return@execute
                }
            }
        }
    }

    // -- the notification -----------------------------------------------------------------------

    private fun note(ctx: Context, t: State) {
        try {
            val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
            Notices.channels(ctx)
            val open = PendingIntent.getActivity(ctx, NOTE_ID,
                Intent(ctx, AskOpenActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val n = Notification.Builder(ctx, Notices.turnChannel(ctx))
                .setSmallIcon(R.drawable.ic_assist_tile)
                .setContentTitle("${t.name} answered")
                .setContentText(t.answer?.lineSequence()?.firstOrNull { it.isNotBlank() }.orEmpty())
                .setContentIntent(open)
                .setAutoCancel(true)
                .build()
            nm.notify(NOTE_ID, n)
        } catch (e: Throwable) {
            Log.w(TAG, "the notification did not post", e)
        }
    }

    private fun cancelNote(ctx: Context) {
        try { ctx.getSystemService(NotificationManager::class.java)?.cancel(NOTE_ID) } catch (_: Throwable) {}
    }
}

/* The notification's tap: open the bar, which shows the thread. */
class AskOpenActivity : Activity() {
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        AssistWays.open(this, Bundle())
        finish()
    }
}
