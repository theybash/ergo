package dev.ergo.app

import android.app.KeyguardManager
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.AlarmClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.edit
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.Calendar
import java.util.Locale
import java.util.concurrent.Executors

/* What the daemon asks of the phone, and what the phone tells the daemon.

   Alarms: the daemon queues commands; the app gets them on the notices' event stream the moment
   they are queued, and reads them once at app start, sets each alarm in the Clock app, and says
   done. A done id is remembered until the
   daemon stops listing it, so no alarm is ever set twice.

   A locked phone cannot set one. Android drops a request to open the Clock app from an app that
   is not on screen, and gives no error, so a dark screen used to lose the alarm and still say
   done. Now an alarm that comes while the screen is off or locked is held: the daemon hears
   "held", a notification offers to set it at once, and the next unlock sets it, because the
   bubble is on screen again then. An alarm whose time passes while held is dropped and said to
   have failed, never set for the day after.

   Notifications: the listener hands every notification it keeps to the queue here, a small file
   of at most 500 items. The queue sends in batches, oldest first, and keeps what did not go for
   the next try. The daemon drops repeats, so a resend is harmless. */
object Phone {

    private const val TAG = "CCPhone"
    private const val PREFS = "phone"
    private const val KEY_DONE = "done_commands"
    private const val KEY_HELD = "held_alarms"
    private const val HELD_NOTE = 9
    private const val MAX_QUEUE = 500
    private const val BATCH = 50

    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    /* ---- alarms ---- */

    /* Reads the daemon's commands and carries out the new ones. Off the main thread; any error
       is logged and the next look tries again. */
    fun checkCommands(ctx: Context) {
        val app = ctx.applicationContext
        try {
            io.execute {
                try {
                    val list = JSONObject(get(Daemon.phone + "/commands")).optJSONArray("commands") ?: JSONArray()
                    main.post { run(app, list) }
                } catch (e: Throwable) {
                    Log.i(TAG, "no phone commands: $e")
                }
            }
        } catch (e: Throwable) {
            Log.w(TAG, "could not ask for phone commands", e)
        }
    }

    /* The commands the daemon's stream carried, on the main thread. */
    fun take(ctx: Context, list: JSONArray) = run(ctx.applicationContext, list)

    private fun run(ctx: Context, list: JSONArray) {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val done = prefs.getStringSet(KEY_DONE, emptySet())?.toMutableSet() ?: mutableSetOf()
        val listed = HashSet<String>()
        for (i in 0 until list.length()) {
            val c = list.optJSONObject(i) ?: continue
            val id = c.optString("id").takeIf { it.isNotBlank() } ?: continue
            listed.add(id)
            if (id in done) continue
            done.add(id)
            prefs.edit { putStringSet(KEY_DONE, HashSet(done)) }
            val result = try {
                when (c.optString("kind")) {
                    "alarm" -> setAlarm(ctx, c)
                    "quiet" -> Result(true, ErgoAlarm.quiet(ctx, c.optLong("until")))
                    "cancel_alarm" -> c.optString("alarm").let { a ->
                        if (a.isBlank()) Result(false, "no alarm to cancel") else Result(true, ErgoAlarm.cancel(ctx, a)) }
                    // An agent floated itself (float_me): the same as his Float button.
                    "float" -> if (!android.provider.Settings.canDrawOverlays(ctx)) Result(false, "Ergo may not draw over other apps")
                        else { CommandService.float(ctx, c.optString("agent"), c.optString("name").ifBlank { null }); Result(true, "floated") }
                    "notice" -> { FullNotice.show(ctx, c.optString("title").ifBlank { "Ergo" }, c.optString("text")); Result(true, "shown") }
                    else -> Result(false, "unknown command " + c.optString("kind"))
                }
            } catch (e: Throwable) {
                Log.w(TAG, "a phone command failed", e)
                Result(false, e.message ?: e.javaClass.simpleName)
            }
            report(id, result)
            // A notice is its own screen; a line on the Done channel would say it twice.
            // Quiet time is for a nap: no line on the Done channel either. A cancel follows a
            // tick or a drop he just made, so a line about it would be noise. A float is its own face.
            if (c.optString("kind") !in setOf("notice", "quiet", "cancel_alarm", "float")) note(ctx, id, result)
        }
        // A done id is kept only while the daemon still lists it.
        val kept = done.filter { it in listed }.toSet()
        if (kept != done) prefs.edit { putStringSet(KEY_DONE, HashSet(kept)) }
        // A look while the phone is open is also a chance to set what was held.
        release(ctx)
    }

    /* The screen is off, or the lock screen is up. Either way nothing of Ergo is on screen. */
    private fun locked(ctx: Context): Boolean {
        val power = ctx.getSystemService(PowerManager::class.java)
        val keys = ctx.getSystemService(KeyguardManager::class.java)
        return power?.isInteractive == false || keys?.isKeyguardLocked == true
    }

    private fun held(ctx: Context): MutableList<JSONObject> {
        val raw = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_HELD, "[]")
        val arr = try { JSONArray(raw) } catch (_: Throwable) { JSONArray() }
        return (0 until arr.length()).mapNotNull { arr.optJSONObject(it) }.toMutableList()
    }

    private fun saveHeld(ctx: Context, list: List<JSONObject>) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit {
            putString(KEY_HELD, JSONArray(list).toString())
        }
    }

    private fun hold(ctx: Context, c: JSONObject) {
        val id = c.optString("id")
        saveHeld(ctx, held(ctx).filter { it.optString("id") != id } + c)
        report(id, Result(true, "held"), held = true)
        offer(ctx, c)
        Log.i(TAG, "alarm $id held until the phone is unlocked")
    }

    /* The next time the alarm's hour and minute come round after the daemon queued it. */
    private fun dueAt(c: JSONObject): Long {
        val queued = (c.optDouble("at", 0.0) * 1000).toLong().takeIf { it > 0 } ?: System.currentTimeMillis()
        val cal = Calendar.getInstance().apply {
            timeInMillis = queued
            set(Calendar.HOUR_OF_DAY, c.optInt("hour"))
            set(Calendar.MINUTE, c.optInt("minute"))
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
        }
        if (cal.timeInMillis <= queued) cal.add(Calendar.DAY_OF_MONTH, 1)
        return cal.timeInMillis
    }

    private fun clockOf(c: JSONObject) =
        String.format(Locale.ROOT, "%02d:%02d", c.optInt("hour"), c.optInt("minute"))

    /* Sets every held alarm, or only `only`. Needs Ergo on screen: the unlock, with the bubble
       back, or the tap on the offer, which opens AlarmTap. Main thread. */
    fun release(ctx: Context, only: String? = null) {
        val list = held(ctx)
        // Held by an older version of the app. Ergo rings alarms itself now, so a locked phone
        // no longer stops one being set.
        if (list.isEmpty()) return
        val nm = ctx.getSystemService(NotificationManager::class.java)
        val left = ArrayList<JSONObject>()
        for (c in list) {
            val id = c.optString("id")
            if (only != null && id != only) { left.add(c); continue }
            nm?.cancel("held:$id", HELD_NOTE)
            val r = if (System.currentTimeMillis() >= dueAt(c)) {
                Result(false, "The phone stayed locked past ${clockOf(c)}, so the alarm was not set")
            } else try {
                setAlarm(ctx, c)
            } catch (e: Throwable) {
                Result(false, e.message ?: e.javaClass.simpleName)
            }
            report(id, r)
            note(ctx, id, r)
        }
        saveHeld(ctx, left)
    }

    /* Released from the unlock broadcast. The bubble comes back a moment after the unlock, and
       the Clock app is only reachable once it is on screen. */
    fun releaseSoon(ctx: Context) {
        val app = ctx.applicationContext
        main.postDelayed({ release(app) }, 800)
    }

    /* On the lock screen: the alarm is waiting, and a tap sets it now. */
    private fun offer(ctx: Context, c: JSONObject) {
        try {
            val id = c.optString("id")
            val label = c.optString("label").ifBlank { "Ergo" }
            val tap = PendingIntent.getActivity(
                ctx, id.hashCode(),
                Intent(ctx, AlarmTap::class.java).putExtra(AlarmTap.EXTRA_ID, id),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
            val n = NotificationCompat.Builder(ctx, Notices.CHANNEL_NEEDS)
                .setSmallIcon(R.drawable.ic_note)
                .setContentTitle("Tap to set alarm ${clockOf(c)}")
                .setContentText("$label. Or unlock the phone and Ergo sets it.")
                .setContentIntent(tap)
                .setAutoCancel(true)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .build()
            val nm = ctx.getSystemService(NotificationManager::class.java)
            Notices.channels(ctx)
            nm.notify("held:$id", HELD_NOTE, n)
        } catch (e: Throwable) {
            Log.i(TAG, "no offer for the held alarm: $e")
        }
    }

    private class Result(val ok: Boolean, val line: String)

    private fun setAlarm(ctx: Context, c: JSONObject): Result {
        val hour = c.optInt("hour", -1)
        val minute = c.optInt("minute", -1)
        if (hour !in 0..23 || minute !in 0..59) return Result(false, "no time for the alarm")
        val label = c.optString("label").ifBlank { "Ergo" }
        val at = String.format(Locale.ROOT, "%02d:%02d", hour, minute)
        // Ergo books and rings the alarm itself (ErgoAlarm). The Clock app was reached by
        // opening its screen, which Android quietly refuses to a background app.
        val refused = ErgoAlarm.book(ctx, c.optString("id"), dueAt(c), label, c.optString("title"))
        return if (refused == null) Result(true, "Alarm set for $at") else Result(false, refused)
    }

    /* The alarm screen to the daemon: "stopped" when he pressed Stop, "prompt" with his words for
       the agent that set the alarm. Off the main thread; a failure is logged and dropped. */
    fun alarmSaid(id: String, what: String, body: JSONObject, then: ((Boolean) -> Unit)? = null) {
        io.execute {
            val ok = try {
                post(Daemon.phone + "/alarm/" + id + "/" + what, body.toString())
                true
            } catch (e: Throwable) {
                Log.i(TAG, "the alarm's $what did not reach the laptop: $e")
                false
            }
            if (then != null) main.post { then(ok) }
        }
    }

    private fun report(id: String, r: Result, held: Boolean = false) {
        io.execute {
            try {
                val body = JSONObject().put("ok", r.ok)
                if (held) body.put("held", true)
                if (!r.ok) body.put("error", r.line)
                post(Daemon.phone + "/commands/" + id + "/done", body.toString())
            } catch (e: Throwable) {
                Log.i(TAG, "could not say a command was done: $e")
            }
        }
    }

    /* A quiet line on the Done channel, so he knows it landed. */
    private fun note(ctx: Context, id: String, r: Result) {
        try {
            val open = PendingIntent.getActivity(
                ctx, id.hashCode(), Intent(ctx, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
            )
            val n = NotificationCompat.Builder(ctx, Notices.CHANNEL_DONE)
                .setSmallIcon(R.drawable.ic_note)
                .setContentTitle(r.line)
                .setContentIntent(open)
                .setAutoCancel(true)
                .build()
            val nm = ctx.getSystemService(NotificationManager::class.java)
            // At app start the service may not have made its channels yet.
            Notices.channels(ctx)
            nm.notify("cmd:$id", 8, n)
        } catch (e: Throwable) {
            Log.i(TAG, "no note for the command: $e")
        }
    }

    /* ---- notifications ---- */

    /* Adds items to the file queue and tries to send. Never throws. */
    fun queue(ctx: Context, items: List<JSONObject>) {
        if (items.isEmpty()) return
        val app = ctx.applicationContext
        try {
            io.execute {
                try {
                    val f = queueFile(app)
                    val lines = (if (f.exists()) f.readLines() else emptyList()).toMutableList()
                    for (i in items) lines.add(i.toString())
                    // At most 500: the oldest go first.
                    val kept = if (lines.size > MAX_QUEUE) lines.takeLast(MAX_QUEUE) else lines
                    f.writeText(kept.joinToString("\n", postfix = if (kept.isEmpty()) "" else "\n"))
                    flushNow(app)
                } catch (e: Throwable) {
                    Log.w(TAG, "the notification queue failed", e)
                }
            }
        } catch (_: Throwable) {
        }
    }

    /* Sends what waits in the queue, oldest first. Never throws. */
    fun flush(ctx: Context) {
        val app = ctx.applicationContext
        try {
            io.execute {
                try {
                    flushNow(app)
                } catch (e: Throwable) {
                    Log.w(TAG, "the notification queue did not send", e)
                }
            }
        } catch (_: Throwable) {
        }
    }

    /* How many items wait, for the only thing about them the phone ever shows. */
    fun pending(ctx: Context): Int = try {
        val f = queueFile(ctx.applicationContext)
        if (f.exists()) f.readLines().count { it.isNotBlank() } else 0
    } catch (_: Throwable) {
        0
    }

    private fun flushNow(ctx: Context) {
        val f = queueFile(ctx)
        while (true) {
            if (!f.exists()) return
            val lines = f.readLines().filter { it.isNotBlank() }
            if (lines.isEmpty()) return
            val batch = lines.take(BATCH)
            val arr = JSONArray()
            for (l in batch) arr.put(JSONObject(l))
            try {
                post(Daemon.phone + "/notifications", JSONObject().put("items", arr).toString())
            } catch (e: Throwable) {
                Log.i(TAG, "the laptop did not take ${batch.size} notifications, kept for later: $e")
                return
            }
            val rest = lines.drop(batch.size)
            f.writeText(rest.joinToString("\n", postfix = if (rest.isEmpty()) "" else "\n"))
        }
    }

    private fun queueFile(ctx: Context) = File(ctx.filesDir, "notice_queue.jsonl")

    /* ---- the wire ---- */

    private fun get(url: String): String {
        val conn = URL(url).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 1500
            conn.readTimeout = 4000
            if (conn.responseCode !in 200..299) error("$url answered ${conn.responseCode}")
            return conn.inputStream.use { it.readBytes() }.decodeToString()
        } finally {
            conn.disconnect()
        }
    }

    private fun post(url: String, body: String) {
        val conn = URL(url).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 1500
            conn.readTimeout = 8000
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(body.toByteArray()) }
            if (conn.responseCode !in 200..299) error("$url answered ${conn.responseCode}")
        } finally {
            conn.disconnect()
        }
    }
}
