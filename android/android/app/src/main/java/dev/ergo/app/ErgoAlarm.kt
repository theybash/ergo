package dev.ergo.app

import android.app.Activity
import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.media.VolumeProvider
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.view.Gravity
import android.view.KeyEvent
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/* Alarms that Ergo rings itself.

   An alarm the daemon asks for used to go to the Clock app, and Android drops a background app's
   request to open the Clock app without a word, so alarms were lost and still reported set. Now
   Ergo books the time with the system's own alarm clock (the alarm icon shows in the status bar),
   and at that time it rings: the alarm sound, which plays even on silent the way any alarm clock
   does, the buzz, and a full-screen alarm over the lock screen with Snooze and Stop.

   Either volume key snoozes it, as on most alarm clocks: on the alarm screen, and with only the
   notification showing too.

   Booked alarms are kept in a small list, so a restart of the phone or an update of the app books
   them again. An alarm left ringing stops by itself after ten minutes. */
object ErgoAlarm {

    private const val PREFS = "ergo_alarms"
    private const val KEY = "booked"
    const val CHANNEL = "ergo_alarm_1"
    /* The ring's own notification, quiet, which keeps the service alive; the loud one with the
       full-screen alarm is separate, so it can be taken down while the alarm screen shows. */
    const val QUIET = "ergo_alarm_quiet"
    const val LOUD_ID = 13
    var loud: Notification? = null

    fun hideBanner(ctx: Context) { if (loud != null) ctx.getSystemService(NotificationManager::class.java).cancel(LOUD_ID) }
    fun showBanner(ctx: Context) {
        val n = loud ?: return
        try { ctx.getSystemService(NotificationManager::class.java).notify(LOUD_ID, n) } catch (_: SecurityException) {}
    }
    const val EXTRA_ID = "alarm_id"
    const val EXTRA_LABEL = "alarm_label"
    const val EXTRA_AT = "alarm_at"
    /* The name of the task on his list that the alarm is for. */
    const val EXTRA_TITLE = "alarm_title"
    const val SNOOZE_MS = 10 * 60 * 1000L
    const val RING_MS = 10 * 60 * 1000L

    fun clock(at: Long): String {
        val c = java.util.Calendar.getInstance().apply { timeInMillis = at }
        return String.format(Locale.ROOT, "%02d:%02d", c.get(java.util.Calendar.HOUR_OF_DAY), c.get(java.util.Calendar.MINUTE))
    }

    private fun booked(ctx: Context): MutableList<JSONObject> {
        val raw = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, "[]")
        val arr = try { JSONArray(raw) } catch (_: Throwable) { JSONArray() }
        return (0 until arr.length()).mapNotNull { arr.optJSONObject(it) }.toMutableList()
    }

    private fun save(ctx: Context, list: List<JSONObject>) =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit { putString(KEY, JSONArray(list).toString()) }

    /* Books one alarm. Null when it is booked, or the reason it could not be. */
    fun book(ctx: Context, id: String, at: Long, label: String, title: String = "", held: Boolean = false): String? {
        val app = ctx.applicationContext
        if (at <= System.currentTimeMillis()) return "The time ${clock(at)} has already passed"
        val am = app.getSystemService(AlarmManager::class.java)
        if (Build.VERSION.SDK_INT >= 31 && !am.canScheduleExactAlarms()) {
            return "Android has not allowed Ergo to set alarms. Allow \"Alarms and reminders\" for Ergo in its app settings"
        }
        val show = PendingIntent.getActivity(app, 0, Intent(app, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        am.setAlarmClock(AlarmManager.AlarmClockInfo(at, show), fireIntent(app, id, at, label, title))
        save(app, booked(app).filter { it.optString("id") != id } + JSONObject().put("id", id).put("at", at).put("label", label).put("title", title).put("held", held))
        return null
    }

    private const val KEY_QUIET = "quiet_until"
    private const val APART_MS = 60 * 1000L

    /* Quiet time from the daemon, for a nap: until that time nothing rings. Every alarm booked in
       the window moves to its end, one minute apart, so they do not all ring at once. When quiet
       ends early (until is 0 or past), the moved ones ring from a minute later, one minute apart. */
    fun quiet(ctx: Context, until: Long): String {
        val app = ctx.applicationContext
        app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit { putLong(KEY_QUIET, until) }
        val now = System.currentTimeMillis()
        val list = booked(app)
        val moving = if (until > now) list.filter { it.optLong("at") in now until until }
                     else list.filter { it.optBoolean("held") && it.optLong("at") > now }
        val start = if (until > now) until else now + APART_MS
        moving.sortedBy { it.optLong("at") }.forEachIndexed { i, a ->
            book(app, a.optString("id"), start + i * APART_MS, a.optString("label"), a.optString("title"), until > now)
        }
        return if (until > now) "Quiet until ${clock(until)}, ${moving.size} alarms moved" else "Quiet is over"
    }

    private fun quietUntil(ctx: Context) =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getLong(KEY_QUIET, 0L)

    private fun fireIntent(ctx: Context, id: String, at: Long, label: String, title: String): PendingIntent =
        PendingIntent.getBroadcast(ctx, id.hashCode(),
            Intent(ctx, AlarmFire::class.java).setAction(AlarmFire.FIRE)
                .putExtra(EXTRA_ID, id).putExtra(EXTRA_AT, at).putExtra(EXTRA_LABEL, label).putExtra(EXTRA_TITLE, title),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    /* After a phone restart or an app update: every alarm still ahead is booked again. */
    fun rebook(ctx: Context) {
        val now = System.currentTimeMillis()
        val ahead = booked(ctx).filter { it.optLong("at") > now }
        save(ctx, emptyList())
        for (a in ahead) book(ctx, a.optString("id"), a.optLong("at"), a.optString("label"), a.optString("title"))
    }

    /* The daemon took the alarm back: its task was ticked, dropped or moved, or an agent cancelled
       it. The booked time is unbooked with the same PendingIntent it was booked with, and so is
       any snooze of it. It leaves the booked list, so rebook() does not bring it back. */
    fun cancel(ctx: Context, id: String): String {
        val app = ctx.applicationContext
        val am = app.getSystemService(AlarmManager::class.java)
        val base = id.substringBefore("-snooze-")
        val list = booked(app)
        val gone = list.filter { it.optString("id").substringBefore("-snooze-") == base }
        // The first id is unbooked even when the list has lost it: a cancel of nothing is harmless.
        val ids = (gone.map { it.optString("id") } + base).toSet()
        for (one in ids) {
            val a = gone.firstOrNull { it.optString("id") == one }
            val pi = fireIntent(app, one, a?.optLong("at") ?: 0L, a?.optString("label") ?: "", a?.optString("title") ?: "")
            am.cancel(pi)
            pi.cancel()
        }
        save(app, list - gone.toSet())
        return if (gone.isEmpty()) "No alarm $base was booked" else "Alarm cancelled"
    }

    /* The time came. It is no longer booked; the ringing service takes it from here. */
    fun fire(ctx: Context, id: String, at: Long, label: String, title: String) {
        // In quiet time it does not ring: it waits for the end, behind the ones already waiting.
        val until = quietUntil(ctx)
        if (until > System.currentTimeMillis() + 1000) {
            val waiting = booked(ctx).count { it.optString("id") != id && it.optLong("at") >= until }
            book(ctx, id, until + waiting * APART_MS, label, title, true)
            return
        }
        save(ctx, booked(ctx).filter { it.optString("id") != id })
        ContextCompat.startForegroundService(ctx, Intent(ctx, AlarmService::class.java)
            .putExtra(EXTRA_ID, id).putExtra(EXTRA_AT, at).putExtra(EXTRA_LABEL, label).putExtra(EXTRA_TITLE, title))
    }

    /* Ends the ring. Without an id it is the ring giving way to a snooze, or to the ten minutes
       running out. With one, he pressed Stop, and the daemon is told: half an hour later it looks
       at the alarm's task, and if it is not ticked, the front desk calls. */
    fun stop(ctx: Context, id: String? = null) {
        ctx.stopService(Intent(ctx, AlarmService::class.java))
        if (id != null) Phone.alarmSaid(id, "stopped", JSONObject())
    }

    /* He pressed Done: the daemon ticks the alarm's task, and the ring ends. If the laptop does
       not take it, it counts as Stop, so the check half an hour later still asks. */
    fun done(ctx: Context, id: String, then: ((Boolean) -> Unit)? = null) {
        val app = ctx.applicationContext
        hush()
        Phone.alarmSaid(id, "done", JSONObject()) { ok ->
            if (ok) stop(app) else if (then == null) stop(app, id)
            then?.invoke(ok)
        }
    }

    fun snooze(ctx: Context, id: String, label: String, title: String = "") {
        stop(ctx)
        val at = System.currentTimeMillis() + SNOOZE_MS
        // Always from the first id, so a fifth snooze is as short as the first.
        book(ctx, "${id.substringBefore("-snooze-")}-snooze-$at", at, label, title)
    }

    /* The sound and the buzz stop, the screen stays: he is typing in the prompt box. */
    fun hush() { AlarmService.current?.hush() }

    fun channel(nm: NotificationManager) {
        if (nm.getNotificationChannel(QUIET) == null) {
            nm.createNotificationChannel(NotificationChannel(QUIET, "Alarm ringing", NotificationManager.IMPORTANCE_LOW))
        }
        if (nm.getNotificationChannel(CHANNEL) != null) return
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Alarms", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Alarms Ergo rings. Its own sound plays, so the channel is silent."
            setSound(null, null)
            enableVibration(false)
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        })
    }
}

/* The booked time arriving, the phone starting up, and the app being updated. */
class AlarmFire : BroadcastReceiver() {
    companion object { const val FIRE = "dev.ergo.app.ALARM_FIRE" }

    override fun onReceive(ctx: Context, intent: Intent) {
        when (intent.action) {
            FIRE -> ErgoAlarm.fire(ctx, intent.getStringExtra(ErgoAlarm.EXTRA_ID) ?: "alarm",
                intent.getLongExtra(ErgoAlarm.EXTRA_AT, System.currentTimeMillis()),
                intent.getStringExtra(ErgoAlarm.EXTRA_LABEL) ?: "Ergo",
                intent.getStringExtra(ErgoAlarm.EXTRA_TITLE) ?: "")
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> ErgoAlarm.rebook(ctx)
        }
    }
}

/* The ring itself, in a foreground service so Android cannot cut it short. */
class AlarmService : Service() {
    companion object { var current: AlarmService? = null }
    private var player: android.media.MediaPlayer? = null
    private var buzz: Vibrator? = null
    private var keys: MediaSession? = null
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private val giveUp = Runnable { stopSelf() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val id = intent?.getStringExtra(ErgoAlarm.EXTRA_ID) ?: "alarm"
        val at = intent?.getLongExtra(ErgoAlarm.EXTRA_AT, System.currentTimeMillis()) ?: System.currentTimeMillis()
        val label = intent?.getStringExtra(ErgoAlarm.EXTRA_LABEL) ?: "Ergo"
        val title = intent?.getStringExtra(ErgoAlarm.EXTRA_TITLE) ?: ""
        current = this
        val nm = getSystemService(NotificationManager::class.java)
        ErgoAlarm.channel(nm)
        val screen = Intent(this, AlarmScreen::class.java).putExtra(ErgoAlarm.EXTRA_ID, id)
            .putExtra(ErgoAlarm.EXTRA_AT, at).putExtra(ErgoAlarm.EXTRA_LABEL, label).putExtra(ErgoAlarm.EXTRA_TITLE, title)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_USER_ACTION)
        val full = PendingIntent.getActivity(this, 51, screen, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stopIt = PendingIntent.getBroadcast(this, 52, Intent(this, AlarmAct::class.java).setAction(AlarmAct.STOP)
            .putExtra(ErgoAlarm.EXTRA_ID, id), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val later = PendingIntent.getBroadcast(this, 53, Intent(this, AlarmAct::class.java).setAction(AlarmAct.SNOOZE)
            .putExtra(ErgoAlarm.EXTRA_ID, id).putExtra(ErgoAlarm.EXTRA_LABEL, label).putExtra(ErgoAlarm.EXTRA_TITLE, title), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val tick = PendingIntent.getBroadcast(this, 54, Intent(this, AlarmAct::class.java).setAction(AlarmAct.DONE)
            .putExtra(ErgoAlarm.EXTRA_ID, id), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        fun build(channel: String) = NotificationCompat.Builder(this, channel)
            .setSmallIcon(R.drawable.ic_note)
            .setContentTitle("Alarm ${ErgoAlarm.clock(at)}")
            .setContentText(label)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setFullScreenIntent(full, true)
            .setContentIntent(full)
            .setOngoing(true)
            .addAction(0, "Snooze 10 min", later)
            .addAction(0, "Stop", stopIt)
            .apply { if (title.isNotBlank()) addAction(0, "Done", tick) }
        val type = if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0
        ServiceCompat.startForeground(this, 12, build(ErgoAlarm.QUIET).setFullScreenIntent(null, false).setSilent(true).build(), type)
        val loud = build(ErgoAlarm.CHANNEL).build()
        ErgoAlarm.loud = loud
        try { nm.notify(ErgoAlarm.LOUD_ID, loud) } catch (_: SecurityException) {}
        ring()
        listenForVolumeKeys(id, label, title)
        // Like the phone's own alarm (The owner, 21 October 2026): full screen when the phone is
        // locked or dark, a heads-up notification while he is using it. The full-screen
        // notification does both by itself; the screen is also opened straight away only when
        // locked, where Android allows it. Opening it over his app put the big Stop button under
        // a finger meant for something else.
        val km = getSystemService(android.app.KeyguardManager::class.java)
        val pm = getSystemService(android.os.PowerManager::class.java)
        if (km.isKeyguardLocked || !pm.isInteractive) {
            try { startActivity(screen) } catch (_: Throwable) {}
        }
        if (Build.VERSION.SDK_INT >= 34 && !nm.canUseFullScreenIntent()) {
            android.util.Log.w("CCAlarm", "full-screen alarms are not allowed: Settings, Apps, Ergo, full screen notifications")
        }
        main.removeCallbacks(giveUp)
        main.postDelayed(giveUp, ErgoAlarm.RING_MS)
        return START_NOT_STICKY
    }

    private fun ring() {
        quiet()
        try {
            val uri = RingtoneManager.getActualDefaultRingtoneUri(this, RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            player = android.media.MediaPlayer().apply {
                setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                setDataSource(this@AlarmService, uri)
                isLooping = true
                prepare()
                start()
            }
        } catch (e: Throwable) {
            android.util.Log.i("CCAlarm", "no alarm sound: $e")
        }
        try {
            val v = if (Build.VERSION.SDK_INT >= 31) getSystemService(VibratorManager::class.java).defaultVibrator
                    else @Suppress("DEPRECATION") getSystemService(Vibrator::class.java)
            val wave = VibrationEffect.createWaveform(longArrayOf(0, 800, 800), 0)
            if (Build.VERSION.SDK_INT >= 33) {
                v.vibrate(wave, android.os.VibrationAttributes.createForUsage(android.os.VibrationAttributes.USAGE_ALARM))
            } else {
                v.vibrate(wave, AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).build())
            }
            buzz = v
        } catch (e: Throwable) {
            android.util.Log.i("CCAlarm", "no vibration: $e")
        }
    }

    /* With no screen of its own in front, a volume key goes to whichever app is playing. For these
       ten minutes that is Ergo: a session that says it is playing to a remote speaker gets the
       keys as "louder" or "softer" instead of the phone changing a volume. Either one snoozes. */
    private fun listenForVolumeKeys(id: String, label: String, title: String) {
        keys?.release()
        var done = false
        keys = try {
            MediaSession(this, "ErgoAlarm").apply {
                setPlaybackToRemote(object : VolumeProvider(VOLUME_CONTROL_RELATIVE, 10, 5) {
                    override fun onAdjustVolume(direction: Int) {
                        if (direction == 0 || done) return
                        done = true
                        main.post { ErgoAlarm.snooze(this@AlarmService, id, label, title) }
                    }
                })
                setPlaybackState(PlaybackState.Builder()
                    .setState(PlaybackState.STATE_PLAYING, 0, 1f).build())
                isActive = true
            }
        } catch (e: Throwable) {
            android.util.Log.i("CCAlarm", "no volume keys: $e")
            null
        }
    }

    fun hush() {
        quiet()
        // He is typing: the screen gets a fresh ten minutes before the ring ends itself.
        main.removeCallbacks(giveUp)
        main.postDelayed(giveUp, ErgoAlarm.RING_MS)
        try { keys?.release() } catch (_: Throwable) {}
        keys = null
    }

    private fun quiet() {
        try { player?.stop() } catch (_: Throwable) {}
        try { player?.release() } catch (_: Throwable) {}
        try { buzz?.cancel() } catch (_: Throwable) {}
        player = null
        buzz = null
    }

    override fun onDestroy() {
        if (current === this) current = null
        main.removeCallbacks(giveUp)
        quiet()
        try { keys?.release() } catch (_: Throwable) {}
        keys = null
        ErgoAlarm.loud = null
        getSystemService(NotificationManager::class.java).cancel(ErgoAlarm.LOUD_ID)
        AlarmScreen.close()
        super.onDestroy()
    }
}

/* Stop and Snooze from the notification. */
class AlarmAct : BroadcastReceiver() {
    companion object {
        const val STOP = "dev.ergo.app.ALARM_STOP"
        const val SNOOZE = "dev.ergo.app.ALARM_SNOOZE"
        const val DONE = "dev.ergo.app.ALARM_DONE"
    }

    override fun onReceive(ctx: Context, intent: Intent) {
        when (intent.action) {
            STOP -> ErgoAlarm.stop(ctx, intent.getStringExtra(ErgoAlarm.EXTRA_ID))
            DONE -> ErgoAlarm.done(ctx, intent.getStringExtra(ErgoAlarm.EXTRA_ID) ?: "alarm")
            SNOOZE -> ErgoAlarm.snooze(ctx, intent.getStringExtra(ErgoAlarm.EXTRA_ID) ?: "alarm",
                intent.getStringExtra(ErgoAlarm.EXTRA_LABEL) ?: "Ergo",
                intent.getStringExtra(ErgoAlarm.EXTRA_TITLE) ?: "")
        }
    }
}

/* The alarm over the lock screen, in the call screen's look: the time large, what it is for, the
   task on his list it is linked to, a box to answer the agent that set it, and Snooze and Stop.
   Tapping the box quiets the ring so he can type or dictate; Snooze or Stop still ends it. */
class AlarmScreen : Activity() {
    companion object {
        private const val TOUCH_GRACE_MS = 1000L
        private var open: java.lang.ref.WeakReference<AlarmScreen>? = null
        fun close() { open?.get()?.finish() }
    }

    private val bg = Color.parseColor("#0F0E0D")
    private val ink = Color.parseColor("#EBE6DF")
    private val dim = Color.parseColor("#A8A29E")
    private val mint = Color.parseColor("#66DBAF")
    private fun dp(v: Float) = v * resources.displayMetrics.density

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        open = java.lang.ref.WeakReference(this)
        if (Build.VERSION.SDK_INT >= 27) { setShowWhenLocked(true); setTurnScreenOn(true) }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.statusBarColor = bg
        window.navigationBarColor = bg
        val id = intent?.getStringExtra(ErgoAlarm.EXTRA_ID) ?: "alarm"
        val at = intent?.getLongExtra(ErgoAlarm.EXTRA_AT, System.currentTimeMillis()) ?: System.currentTimeMillis()
        val label = intent?.getStringExtra(ErgoAlarm.EXTRA_LABEL) ?: "Ergo"
        val title = intent?.getStringExtra(ErgoAlarm.EXTRA_TITLE) ?: ""
        val serif = androidx.core.content.res.ResourcesCompat.getFont(this, R.font.instrument_serif_regular)
        val italic = androidx.core.content.res.ResourcesCompat.getFont(this, R.font.instrument_serif_italic)
        val sans = androidx.core.content.res.ResourcesCompat.getFont(this, R.font.inter_medium)
        fun text(t: CharSequence, size: Float, color: Int, face: android.graphics.Typeface?) = TextView(this).apply {
            text = t; textSize = size; setTextColor(color); typeface = face; gravity = Gravity.CENTER
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setBackgroundColor(bg)
            setPadding(dp(24f).toInt(), dp(110f).toInt(), dp(24f).toInt(), dp(40f).toInt())
        }
        root.addView(text("ALARM", 12f, Color.parseColor("#6F6964"), sans).apply { letterSpacing = 0.18f })
        root.addView(text(ErgoAlarm.clock(at), 96f, ink, serif).apply { setPadding(0, dp(8f).toInt(), 0, 0) })
        root.addView(text(label, 24f, Color.parseColor("#D6D0C8"), italic).apply {
            setPadding(dp(12f).toInt(), dp(4f).toInt(), dp(12f).toInt(), 0); maxLines = 3
        })
        if (title.isNotBlank() && !title.equals(label, ignoreCase = true)) {
            root.addView(text("Task: $title", 15f, dim, sans).apply {
                setPadding(dp(12f).toInt(), dp(10f).toInt(), dp(12f).toInt(), 0); maxLines = 2
            })
        }
        root.addView(android.view.View(this), LinearLayout.LayoutParams(1, 0, 1f))
        val line = Color.parseColor("#2A2623")
        val box = android.widget.EditText(this).apply {
            hint = "Tell the agent…"
            setHintTextColor(Color.parseColor("#6F6964")); setTextColor(ink); typeface = sans; textSize = 16f
            maxLines = 3
            setPadding(dp(20f).toInt(), 0, dp(8f).toInt(), 0)
            background = null
            imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_SEND
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            setOnFocusChangeListener { _, has -> if (has) ErgoAlarm.hush() }
        }
        val send = TextView(this).apply {
            text = "Send"; textSize = 15f; setTextColor(mint); typeface = sans; gravity = Gravity.CENTER
            setPadding(dp(16f).toInt(), 0, dp(20f).toInt(), 0)
        }
        fun sendIt() {
            val words = box.text.toString().trim()
            if (words.isEmpty()) return
            box.setText("")
            box.hint = "Sending…"
            Phone.alarmSaid(id, "prompt", JSONObject().put("text", words)) { ok ->
                // Sent is Stop as well (The owner, 18 Oct 2026): his words are his answer to the alarm.
                if (ok) {
                    android.widget.Toast.makeText(this, "Sent to the agent", android.widget.Toast.LENGTH_SHORT).show()
                    ErgoAlarm.stop(this, id); finish()
                } else { box.setText(words); box.hint = "Tell the agent…"
                    android.widget.Toast.makeText(this, "Not sent. The laptop did not take it.", android.widget.Toast.LENGTH_LONG).show() }
            }
        }
        send.setOnClickListener { sendIt() }
        box.setOnEditorActionListener { _, _, _ -> sendIt(); true }
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            background = android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = dp(32f); setColor(Color.TRANSPARENT); setStroke(dp(1f).toInt(), line)
            }
            addView(box, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f))
            addView(send, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.MATCH_PARENT))
        }
        fun button(t: String, fill: Int?, color: Int, act: () -> Unit) = TextView(this).apply {
            text = t; textSize = 17f; setTextColor(color); typeface = sans; gravity = Gravity.CENTER
            background = android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = dp(32f)
                if (fill != null) setColor(fill) else { setColor(Color.TRANSPARENT); setStroke(dp(1f).toInt(), Color.parseColor("#2A2623")) }
            }
            setOnClickListener { act() }
        }
        val wide = { LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(64f).toInt()) }
        root.addView(row, wide().apply { bottomMargin = dp(14f).toInt() })
        // A tap in the first second is a finger that was already on the glass, not an answer.
        val shown = android.os.SystemClock.uptimeMillis()
        fun meant() = android.os.SystemClock.uptimeMillis() - shown > TOUCH_GRACE_MS
        val snoozeIt = { if (meant()) { ErgoAlarm.snooze(this, id, label, title); finish() } }
        val stopIt = { if (meant()) { ErgoAlarm.stop(this, id); finish() } }
        if (title.isBlank()) {
            root.addView(button("Snooze 10 minutes", null, ink, snoozeIt), wide())
            root.addView(button("Stop", mint, bg, stopIt), wide().apply { topMargin = dp(14f).toInt() })
        } else {
            // The alarm has a task (The owner, 28 Oct 2026): Done ticks it, so it is the big one.
            val pair = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            val half = { LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f) }
            pair.addView(button("Snooze", null, ink, snoozeIt), half().apply { marginEnd = dp(7f).toInt() })
            pair.addView(button("Stop", null, ink, stopIt), half().apply { marginStart = dp(7f).toInt() })
            root.addView(pair, wide())
            var ticking = false
            lateinit var doneBtn: TextView
            doneBtn = button("Done", mint, bg) {
                if (meant() && !ticking) {
                    ticking = true
                    doneBtn.text = "Ticking…"
                    ErgoAlarm.done(this, id) { ok ->
                        if (ok) {
                            android.widget.Toast.makeText(this, "Ticked", android.widget.Toast.LENGTH_SHORT).show()
                            finish()
                        } else {
                            ticking = false
                            doneBtn.text = "Done"
                            android.widget.Toast.makeText(this, "Not ticked. The laptop did not take it.", android.widget.Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
            root.addView(doneBtn, wide().apply { topMargin = dp(14f).toInt() })
        }
        snoozeNow = { ErgoAlarm.snooze(this, id, label, title); finish() }
        setContentView(root)
    }

    private var snoozeNow: (() -> Unit)? = null

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP) {
            snoozeNow?.invoke()
            snoozeNow = null
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onResume() {
        super.onResume()
        ErgoAlarm.hideBanner(this)
    }

    override fun onStop() {
        super.onStop()
        if (!isFinishing) ErgoAlarm.showBanner(this)
    }

    override fun onDestroy() {
        if (open?.get() === this) open = null
        super.onDestroy()
    }
}
