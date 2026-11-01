package dev.ergo.app

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL

/* The notices: what the fleet does while the owner is elsewhere, as real notifications, each kind
   on its own channel so he can turn each off on its own.

     Needs you   an agent asks a question or a permission. One notification per ask, gone when
                 the ask is.
     Done        an agent went from working to not working between two looks.
     Job failed  a cron job ran and did not succeed.
     The front desk      an agent left a brief for the next call. One notification per inbox item, gone
                 when the front desk has told it or it is removed. Items from an ask or a failed job are
                 left out: Needs you and Job failed already say those.

   NO POLLING. While the service runs it holds one connection open to the daemon's event stream,
   topic phone. The daemon sends a small digest when the stream opens and a new one only when
   something in it changed: who is working and what each agent last said, each job's last run,
   the open asks, the front desk's briefs, and the phone's own commands. One gzip stream, so a digest
   that differs in one line costs about that line. A quiet stream carries a comment every 45 s
   and nothing else.

   A dropped stream (the laptop asleep, a tunnel, wifi to mobile) is opened again after a pause
   that grows from 2 s to 1 min, and at once when Android says a network is back. The first
   digest after a start only learns what is already true; it posts nothing. After a reconnect the
   digest is compared with the last one seen, so what happened while away still notifies. */
class Notices(private val service: CommandService) {

    private val main = Handler(Looper.getMainLooper())
    private val nm = service.getSystemService(NotificationManager::class.java)
    @Volatile private var running = false
    @Volatile private var conn: HttpURLConnection? = null
    private var thread: Thread? = null
    private val nap = Object()
    private var netBack: ConnectivityManager.NetworkCallback? = null

    /* What the last digest said. Null until one came. */
    private var asks: Map<String, Asks.Ask>? = null
    private var working: Map<String, Boolean>? = null
    private var fired: Map<String, Double>? = null
    private var briefs: Set<String>? = null

    fun start() {
        if (running) return
        running = true
        thread = Thread({ listen() }, "cc-stream").apply { isDaemon = true; start() }
        try {
            val cm = service.getSystemService(ConnectivityManager::class.java)
            val cb = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) = wake()
            }
            cm.registerDefaultNetworkCallback(cb)
            netBack = cb
        } catch (e: Throwable) {
            Log.i(TAG, "no network callback: $e")
        }
    }

    fun stop() {
        running = false
        netBack?.let {
            try { service.getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) } catch (_: Throwable) { }
        }
        netBack = null
        try { conn?.disconnect() } catch (_: Throwable) { }
        wake()
    }

    /* Cuts a pause between reconnects short: a network came back, or the phone was unlocked. */
    fun wake() {
        synchronized(nap) { nap.notifyAll() }
    }

    private fun listen() {
        var pause = FIRST_PAUSE_MS
        while (running) {
            try {
                val c = URL(Daemon.events + "?topics=phone").openConnection() as HttpURLConnection
                conn = c
                c.connectTimeout = CONNECT_MS
                // Three missed comments and the stream is taken for dead.
                c.readTimeout = STALL_MS
                if (c.responseCode !in 200..299) error("the stream answered ${c.responseCode}")
                pause = FIRST_PAUSE_MS
                BufferedReader(InputStreamReader(c.inputStream)).use { read(it) }
            } catch (e: Throwable) {
                if (running) Log.i(TAG, "the stream closed: $e")
            } finally {
                try { conn?.disconnect() } catch (_: Throwable) { }
                conn = null
            }
            if (!running) break
            synchronized(nap) { nap.wait(pause) }
            pause = minOf(pause * 2, LAST_PAUSE_MS)
        }
    }

    /* Server-sent events: "event:" and "data:" lines, a blank line after each event. */
    private fun read(r: BufferedReader) {
        var event = ""
        val data = StringBuilder()
        while (running) {
            val line = r.readLine() ?: return
            when {
                line.isEmpty() -> {
                    if (event == "phone" && data.isNotEmpty()) {
                        val digest = try { JSONObject(data.toString()) } catch (_: Throwable) { null }
                        if (digest != null) main.post { if (running) saw(digest) }
                    }
                    event = ""
                    data.setLength(0)
                }
                line.startsWith("event:") -> event = line.substring(6).trim()
                line.startsWith("data:") -> data.append(line.substring(5).trimStart())
            }
        }
    }

    private fun saw(d: JSONObject) {
        try {
            sawAsks(asksOf(d.optJSONArray("asks")))
            sawState(d.optJSONObject("agents") ?: JSONObject(), d.optJSONObject("jobs") ?: JSONObject())
            sawInbox(d.optJSONArray("briefs") ?: JSONArray())
            Phone.take(service, d.optJSONArray("commands") ?: JSONArray())
            d.optJSONObject("ring")?.let { DeskCall.ring(service, it) }
        } catch (e: Throwable) {
            Log.w(TAG, "a digest went wrong", e)
        }
    }

    private fun asksOf(list: JSONArray?): List<Asks.Ask> {
        if (list == null) return emptyList()
        return (0 until list.length()).mapNotNull { list.optJSONObject(it) }.mapNotNull { o ->
            val agent = o.optString("agent").takeIf { it.isNotBlank() } ?: return@mapNotNull null
            Asks.Ask(agent, o.optString("kind"), o.optString("asking"),
                o.optString("ask_id").ifBlank { agent + ":" + o.optString("asking") })
        }
    }

    private fun sawAsks(now: List<Asks.Ask>) {
        val byId = now.associateBy { it.id }
        val before = asks
        asks = byId
        if (before == null) return
        for ((id, ask) in byId) {
            if (id in before) continue
            val what = when (ask.kind) {
                "permission" -> ask.asking.removePrefix("may it ").ifBlank { "a permission" }
                else -> ask.asking.ifBlank { "a question" }
            }
            post(CHANNEL_NEEDS, "ask:$id", ask.agent, what, "chat:" + ask.agent)
        }
        for (id in before.keys) if (id !in byId) nm.cancel("ask:$id", NOTE)
    }

    /* agents: {id: [working, last line]}, jobs: {id: [last fire, last outcome, agent]}. */
    private fun sawState(agents: JSONObject, jobs: JSONObject) {
        val nowWorking = HashMap<String, Boolean>()
        val said = HashMap<String, String>()
        for (id in agents.keys()) {
            val a = agents.optJSONArray(id) ?: continue
            nowWorking[id] = a.optBoolean(0, false)
            said[id] = a.optString(1)
        }
        val nowFired = HashMap<String, Double>()
        val outcome = HashMap<String, String>()
        val owner = HashMap<String, String>()
        for (id in jobs.keys()) {
            val j = jobs.optJSONArray(id) ?: continue
            nowFired[id] = j.optDouble(0, 0.0).takeIf { !it.isNaN() } ?: 0.0
            outcome[id] = j.optString(1)
            owner[id] = j.optString(2)
        }
        val wasWorking = working
        val wasFired = fired
        working = nowWorking
        service.floatMode.saw(nowWorking)
        fired = nowFired
        if (wasWorking != null) for ((id, busy) in nowWorking) {
            if (wasWorking[id] == true && !busy) {
                // A floated agent's face tells him, with the chime, and no banner over his game.
                if (service.floatMode.floated(id)) continue
                val line = said[id].orEmpty().replace(Regex("\\s+"), " ").trim().take(120)
                post(turnChannel(service), "done:$id", id, line.ifBlank { "finished its turn" }, "chat:$id", once = false)
            }
        }
        if (wasFired != null) for ((id, at) in nowFired) {
            val prev = wasFired[id] ?: 0.0
            val result = outcome[id].orEmpty()
            if (at > prev && at > 0 && result.isNotBlank() && result != "succeeded") {
                val who = owner[id].orEmpty().ifBlank { "a job" }
                post(CHANNEL_JOBS, "job:$id", "Job failed", "$who: $result", "fleet")
            }
        }
    }

    private fun sawInbox(waiting: JSONArray) {
        val now = LinkedHashMap<String, JSONObject>()
        for (i in 0 until waiting.length()) {
            val it = waiting.optJSONObject(i) ?: continue
            val id = it.optString("id").takeIf { id -> id.isNotBlank() } ?: continue
            // An urgent item is the call itself; a second notification for it is noise.
            if (it.optString("urgency") == "urgent") continue
            now[id] = it
        }
        val before = briefs
        briefs = now.keys
        if (before == null) {
            // The first digest after the service starts. An "agent is free" reminder from the last
            // few minutes is still news; it would otherwise be lost in the gap.
            val since = System.currentTimeMillis() / 1000.0 - FREE_LATE_S
            for ((id, item) in now) {
                if (item.optString("kind") == "free" && item.optDouble("added", 0.0) > since) free(id, item)
            }
            return
        }
        for ((id, item) in now) {
            if (id in before) continue
            if (item.optString("kind") == "free") { free(id, item); continue }
            val agent = item.optString("agent").ifBlank { "an agent" }
            val title = if (item.optString("urgency") == "now") "The front desk, now: $agent" else "The front desk: $agent"
            post(CHANNEL_DESK, "brief:$id", title, item.optString("text"), "chat:$agent", call = true)
        }
        for (id in before) if (id !in now) nm.cancel("brief:$id", NOTE)
    }

    /* "<agent> is free. Start '...'?" on its own channel, with sound: he asked for a reminder
       that reaches him, not one that waits for a the front desk call (The owner, 21 October 2026). */
    fun free(id: String, item: JSONObject) {
        val agent = item.optString("agent").ifBlank { "an agent" }
        val text = item.optString("text")
        val title = text.substringBefore(" is free.", "").ifBlank { agent } + " is free"
        post(CHANNEL_FREE, "brief:$id", title, text, "chat:$agent")
    }

    /* One notification, unless the owner has turned notifications off. A tap opens the app on
       `go`, the same routes the wheel uses. */
    private fun post(channel: String, tag: String, title: String, text: String, go: String, call: Boolean = false,
                     once: Boolean = true) {
        if (!allowed()) return
        val tap = opener(tag.hashCode(), go)
        val build = NotificationCompat.Builder(service, channel)
            .setSmallIcon(R.drawable.ic_note)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(tap)
            .setAutoCancel(true)
            .setOnlyAlertOnce(once)
        // A brief also offers the call, where the front desk says it.
        if (call) build.addAction(0, "Call the front desk", opener((tag + ":call").hashCode(), "voice"))
        val note = build.build()
        try {
            nm.notify(tag, NOTE, note)
        } catch (e: SecurityException) {
            Log.i(TAG, "notifications are not allowed: $e")
        }
    }

    private fun opener(code: Int, go: String): PendingIntent {
        val open = Intent(service, MainActivity::class.java)
            .putExtra(MainActivity.EXTRA_GO, go)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(
            service, code, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    private fun allowed(): Boolean {
        if (!NotificationManagerCompat.from(service).areNotificationsEnabled()) return false
        return Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(service, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
    }

    companion object {
        private const val TAG = "CCNotices"
        private const val CONNECT_MS = 5000
        // The laptop speaks every 45 s, so a minute of silence means the line died quietly, as it
        // does when the daemon restarts. At 150 s a 15:59 alarm reached the phone at 16:00.
        // Waiting less costs nothing: no extra traffic, only a sooner reconnect.
        private const val STALL_MS = 60_000
        private const val FIRST_PAUSE_MS = 2_000L
        private const val LAST_PAUSE_MS = 60_000L
        private const val NOTE = 7

        const val CHANNEL_NEEDS = "needs_you"
        const val CHANNEL_DONE = "done"
        /* The chime when an agent finishes its turn, picked on Config (The owner, 26 Oct: "do
           kalimba, put option in config"). Android fixes a channel's sound once it has made the
           channel, so each chime is a channel of its own, "turn_<chime>", and the others go. */
        val CHIMES = listOf("kalimba", "marimba", "drop", "bell", "off")
        private const val CHIME_PREFS = "notices"
        private const val OLD_TURN = "turn_done"

        fun chime(ctx: Context): String =
            ctx.getSharedPreferences(CHIME_PREFS, Context.MODE_PRIVATE).getString("chime", null)
                ?.takeIf { it in CHIMES } ?: "kalimba"

        fun setChime(ctx: Context, name: String): Boolean {
            if (name !in CHIMES) return false
            ctx.getSharedPreferences(CHIME_PREFS, Context.MODE_PRIVATE).edit().putString("chime", name).commit()
            channels(ctx)
            return true
        }

        fun turnChannel(ctx: Context) = "turn_" + chime(ctx)
        const val CHANNEL_JOBS = "job_failed"
        const val CHANNEL_DESK = "desk"
        const val CHANNEL_FREE = "agent_free"
        private const val FREE_LATE_S = 15 * 60

        /* The four quiet channels, silent unless the owner gives one a sound in Settings, "Agent
           answered" with Ergo's own chime, and "Agent is free", which rings with the default sound.
           "Done" stays quiet: the phone's own confirmations use it. */
        fun channels(ctx: Context) {
            val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
            val quiet = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION).build()
            fun make(id: String, name: String, importance: Int, about: String) =
                NotificationChannel(id, name, importance).apply {
                    description = about
                    setSound(null, quiet)
                }
            nm.createNotificationChannel(
                make(CHANNEL_NEEDS, "Needs you", NotificationManager.IMPORTANCE_DEFAULT,
                    "An agent asks a question or a permission."),
            )
            nm.createNotificationChannel(
                make(CHANNEL_DONE, "Done", NotificationManager.IMPORTANCE_LOW, "An agent finished its turn."),
            )
            // An agent finished its turn: the chime he picked, or quiet when he picked Off.
            val pick = chime(ctx)
            val sound = when (pick) {
                "marimba" -> R.raw.chime_marimba
                "drop" -> R.raw.chime_drop
                "bell" -> R.raw.chime_bell
                "off" -> 0
                else -> R.raw.chime_kalimba
            }
            nm.createNotificationChannel(
                NotificationChannel("turn_$pick", "Agent answered",
                    if (sound == 0) NotificationManager.IMPORTANCE_LOW else NotificationManager.IMPORTANCE_DEFAULT).apply {
                    description = "An agent finished its turn."
                    setSound(if (sound == 0) null else android.net.Uri.parse(
                        "android.resource://${BuildConfig.APPLICATION_ID}/$sound"), quiet)
                },
            )
            for (old in CHIMES + OLD_TURN.removePrefix("turn_")) {
                if (old != pick) nm.deleteNotificationChannel("turn_$old")
            }
            nm.createNotificationChannel(
                make(CHANNEL_JOBS, "Job failed", NotificationManager.IMPORTANCE_DEFAULT, "A scheduled job did not succeed."),
            )
            nm.createNotificationChannel(
                make(CHANNEL_DESK, "The front desk", NotificationManager.IMPORTANCE_DEFAULT,
                    "An agent left something for the front desk to tell you."),
            )
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_FREE, "Agent is free", NotificationManager.IMPORTANCE_HIGH).apply {
                    description = "An agent you were waiting for is free to start a task."
                },
            )
        }
    }
}
