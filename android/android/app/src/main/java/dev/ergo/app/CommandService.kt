package dev.ergo.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.util.Log
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import org.json.JSONObject
import java.lang.ref.WeakReference

/* The one service. It always runs, because it holds the stream to the laptop, and the stream
   carries the alarms and the front desk's rings. It used to stop when there was no call and no bubble, so
   Hide bubble, or no overlay permission, also stopped every alarm and ring until the app was
   opened again. On 18 October the stream was down for hours while the notifications still went,
   and the 10:00, 10:05 and 11:00 alarms reached the phone after their time. Without a bubble or a
   call it is the quiet "Ergo" notification and nothing else.

   A call makes it a foreground service of type microphone, which is what lets the WebView keep
   the microphone once the app leaves the screen. The bubble makes it type special use, so the
   bubble stays up over other apps. Its notification carries Hang up during a call and Hide
   bubble when the bubble is on. A bubble hidden during a call, by the button or by the trash
   target, gets Show bubble on the call's notification, because the call keeps the service up
   and the app may not be opened from the launcher until the call is over. */
class CommandService : Service() {

    private lateinit var overlay: Overlay
    private lateinit var notices: Notices
    lateinit var floatMode: FloatMode
        private set

    /* Debug builds only: adb makes the open wheel throw, to prove the phone stays free.
       adb shell am broadcast -a dev.ergo.app.TEST_FAULT --es where draw
       The release build never registers it. */
    private var testFaults: BroadcastReceiver? = null

    /* The unlock, when an alarm held while the phone was locked can be set. */
    private val unlocked = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            Phone.releaseSoon(c)
            // A stream that dropped while the phone slept opens again now, not after its pause.
            notices.wake()
        }
    }

    override fun onCreate() {
        super.onCreate()
        instance = WeakReference(this)
        overlay = Overlay(this)
        floatMode = FloatMode(this)
        channels()
        notices = Notices(this)
        notices.start()
        // Float mode's faces come back where they were.
        floatMode.restore()
        ContextCompat.registerReceiver(this, unlocked, IntentFilter(Intent.ACTION_USER_PRESENT),
            ContextCompat.RECEIVER_NOT_EXPORTED)
        if (BuildConfig.DEBUG) {
            val r = object : BroadcastReceiver() {
                override fun onReceive(c: Context, i: Intent) {
                    val where = i.getStringExtra("where") ?: "draw"
                    // "theme:glass" or "theme:hud" sets the wheel's theme, as the Config page does.
                    if (where.startsWith("theme:")) setWheelTheme(c, where.removePrefix("theme:"))
                    // "loud:on" lets a silent test mic through the voice note's silence guard.
                    else if (where.startsWith("loud:")) VoiceNote.testLoud = where == "loud:on"
                    // "ring" rings as an urgent item would, to test the call without the daemon.
                    else if (where == "ring") DeskCall.ring(c, JSONObject().put("id", "test-" + System.currentTimeMillis())
                        .put("agent", "personal-assistant").put("agent_id", "personal-assistant").put("text", "You asked me to call. Creator has your pick for the call screen, mock A with call me back in ten. Your IRCTC password is out of my hands until you add it to the vault, and the morning letters still use your work browser profile, which needs your decision before tomorrow at seven.").put("count", 1).put("at", System.currentTimeMillis()))
                    // "call:on" and "call:off" stand in for a page that started or ended a call.
                    // "alarm" books a Ergo alarm twenty seconds from now.
                    else if (where == "alarm") ErgoAlarm.book(c, "test-" + System.currentTimeMillis(), System.currentTimeMillis() + 20_000, "Gym", "Leg day at the society gym")
                    // "free" posts an "agent is free" reminder as the inbox would.
                    else if (where == "free") notices.free("test-" + System.currentTimeMillis(), JSONObject()
                        .put("agent", "personal-assistant").put("kind", "free")
                        .put("text", "The assistant is free. Start 'Book the train tickets'?"))
                    // "notice" shows the full-screen notice as the quota alert would.
                    else if (where == "notice") FullNotice.show(c, "Usage is past today's line", "The week's usage is 64%. Today's line is 59%. Nothing is stopped. You decide.")
                    // "float:<agent id>" floats that agent, as the bar or the chat menu would.
                    else if (where.startsWith("float:")) floatMode.add(where.removePrefix("float:"), null)
                    else if (where == "call:on") callStarted(c)
                    else if (where == "call:off") callEnded(c)
                    else overlay.testFault(where)
                }
            }
            ContextCompat.registerReceiver(this, r, IntentFilter(ACTION_TEST_FAULT), ContextCompat.RECEIVER_EXPORTED)
            testFaults = r
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_HANGUP -> act("hangup")
            ACTION_MUTE -> act("mute")
            ACTION_HIDE -> {
                // The last resort: the bubble and any wheel go now, before anything else runs.
                bubbleHidden = true
                overlay.hide()
            }
            ACTION_SHOW -> bubbleHidden = false
        }
        apply()
        // Killed by Android, it is started again, and the stream with it.
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    /* The phone turned: float mode takes the other shape. */
    override fun onConfigurationChanged(newConfig: android.content.res.Configuration) {
        super.onConfigurationChanged(newConfig)
        floatMode.relayout()
    }

    /* Swiping the app away kills the WebView, and the call with it. */
    override fun onTaskRemoved(rootIntent: Intent?) {
        callLive = false
        apply()
    }

    override fun onDestroy() {
        notices.stop()
        try { unregisterReceiver(unlocked) } catch (_: IllegalArgumentException) { }
        testFaults?.let { unregisterReceiver(it) }
        testFaults = null
        overlay.hide()
        floatMode.hideAll()
        if (instance.get() === this) instance = WeakReference(null)
        super.onDestroy()
    }

    /* Dropped on the trash target: the same as Hide bubble. */
    fun hideBubble() {
        bubbleHidden = true
        apply()
    }

    /* The types last given to startForeground, or -1 before the first time. */
    private var types = -1

    /* Brings the service in line with the state: the right notification and types, the bubble
       up or down, or the service gone. */
    fun apply() {
        val bubble = if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0
        val mic = if (Build.VERSION.SDK_INT >= 30) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
        // The bubble holds the microphone type too, when the mic is allowed, so a voice note from
        // the dial can record while the app is off screen. Android lets that type start only
        // while the app is in use; if it refuses, the bubble falls back to its own type alone.
        val micOk = ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        var want = (if (canBubble(this)) bubble else 0) or (if (callLive || (canBubble(this) && micOk)) mic else 0)
        // During a call the types only grow. Changing them while the app is off screen could
        // cost the call its microphone, so Hide bubble takes the bubble down at once but drops
        // its type only when the call ends.
        if (callLive && types != -1) want = want or types
        // While the front desk rings or talks, the service is a phone call service too, which is what the
        // phone system expects of a calling app. If Android refuses it, the rest stands.
        if ((ringingNow || callLive) && Build.VERSION.SDK_INT >= 30) want = want or ServiceInfo.FOREGROUND_SERVICE_TYPE_PHONE_CALL
        // No bubble and no call: the stream alone keeps it, as special use.
        if (want == 0) want = bubble
        if (want != types) {
            val phone = if (Build.VERSION.SDK_INT >= 30) ServiceInfo.FOREGROUND_SERVICE_TYPE_PHONE_CALL else 0
            if (!promote(want) && !promote(want and phone.inv()) && !promote(want and mic.inv() and phone.inv()) && !promote(bubble)) {
                stopSelf()
                return
            }
        } else {
            getSystemService(NotificationManager::class.java).notify(NOTE_ID, note())
        }
        // The bubble steps aside while the front desk rings, so it never sits on the call screen.
        overlay.show(canBubble(this) && !appVisible && !ringingNow)
        overlay.update()
    }

    /* startForeground with these types. The system can refuse the microphone type, most often
       because the app was not on screen when the call began. */
    private fun promote(want: Int): Boolean = try {
        ServiceCompat.startForeground(this, NOTE_ID, note(), want)
        types = want
        micReady = Build.VERSION.SDK_INT < 30 || (want and ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE) != 0
        true
    } catch (e: Exception) {
        false
    }

    /* What a slice of the wheel, or the notification, asked for. */
    fun act(what: String) {
        when (what) {
            "call" -> openApp("voice")
            "text" -> openApp("keys")
            "pa" -> openApp("pa")
            "fleet" -> openApp("fleet")
            "mute", "pause" -> CCNativePlugin.send(JSONObject().put("do", what))
            "hangup" -> CCNativePlugin.send(JSONObject().put("do", "hangup")) { heard ->
                // No page to hang up means no call. Drop the notification anyway.
                if (!heard) callEnded(this)
            }
            // A bar on the wheel: that agent's chat.
            else -> if (what.startsWith("chat:")) openApp(what)
        }
    }

    private fun openApp(go: String) {
        val open = Intent(this, MainActivity::class.java)
            .putExtra(MainActivity.EXTRA_GO, go)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        startActivity(open)
    }

    /* The call's channel, the bubble's quiet one, and the notices'. The bubble's first channel
       was made at a higher importance, which Android keeps fixed, so it is deleted and the
       bubble has a new one at the lowest: no status bar icon, no sound, folded at the bottom of
       the shade. */
    private fun channels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_CALL, "Voice call", NotificationManager.IMPORTANCE_LOW)
        )
        nm.deleteNotificationChannel(OLD_CHANNEL_BUBBLE)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_BUBBLE, "Bubble", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Keeps the bubble up and the alarms coming. Android needs it; it stays out of the way."
                setShowBadge(false)
            }
        )
        Notices.channels(applicationContext)
    }

    private fun note(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val b = NotificationCompat.Builder(this, if (callLive) CHANNEL_CALL else CHANNEL_BUBBLE)
            .setSmallIcon(R.drawable.ic_note)
            .setContentIntent(open)
            .setOngoing(true)
            .setSilent(true)
            .setShowWhen(false)
        if (callLive) {
            val said = when {
                paused -> "Paused"
                muted -> "Microphone off"
                else -> "In a call"
            }
            // A phone call's own notification: the running clock, Hang up in red, and on Android
            // 12 and later the green call chip in the status bar, which brings the call back.
            val caller = androidx.core.app.Person.Builder().setName(callWho).setImportant(true).build()
            val started = System.currentTimeMillis() - (android.os.SystemClock.elapsedRealtime() - callSince)
            b.setContentTitle(callWho)
                .setContentText(said)
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
                .setShowWhen(true)
                .setWhen(started)
                .setUsesChronometer(true)
                .setStyle(NotificationCompat.CallStyle.forOngoingCall(caller, self(ACTION_HANGUP, 1)))
                .addAction(0, if (muted) "Unmute" else "Mute", self(ACTION_MUTE, 4))
        } else {
            // As quiet as Android allows: a title, no text, only Hide bubble.
            b.setContentTitle("Ergo")
                .setPriority(NotificationCompat.PRIORITY_MIN)
        }
        if (canBubble(this)) b.addAction(0, "Hide bubble", self(ACTION_HIDE, 2))
        else if (BUBBLE && callLive && bubbleHidden && Settings.canDrawOverlays(this)) {
            b.addAction(0, "Show bubble", self(ACTION_SHOW, 3))
        }
        return b.build()
    }

    private fun self(action: String, code: Int): PendingIntent = PendingIntent.getService(
        this, code, Intent(this, CommandService::class.java).setAction(action), PendingIntent.FLAG_IMMUTABLE
    )

    companion object {
        private const val NOTE_ID = 1
        private const val CHANNEL_CALL = "call"
        private const val CHANNEL_BUBBLE = "bubble_quiet"
        private const val OLD_CHANNEL_BUBBLE = "bubble"
        private const val ACTION_HANGUP = "hangup"
        private const val ACTION_MUTE = "mute"
        private const val ACTION_HIDE = "hide"
        private const val ACTION_SHOW = "show"
        private const val ACTION_TEST_FAULT = "dev.ergo.app.TEST_FAULT"

        /* The state, read and written on the main thread only. */
        var appVisible = false
        /* Hide bubble lasts until the app is opened from the launcher again. */
        var bubbleHidden = false
        var callLive = false
            private set
        /* When the live call began, on the elapsed realtime clock. The wheel's hub counts from it. */
        var callSince = 0L
            private set
        var muted = false
            private set
        var paused = false
            private set

        private var instance = WeakReference<CommandService>(null)

        /* Float this agent's face over every app (the assistant bar and the chat menu ask). */
        fun float(ctx: Context, id: String, name: String?) {
            val running = instance.get()
            if (running != null) running.floatMode.add(id, name)
            else Log.i("CCFloat", "the service is not running, nothing floated")
        }
        private val main = Handler(Looper.getMainLooper())

        /* The wheel's theme, "hud", "glass", "bubbles", "dial" or "glass2", read by the wheel each time it opens. */
        const val THEME_HUD = "hud"
        const val THEME_GLASS = "glass"
        const val THEME_BUBBLES = "bubbles"
        const val THEME_DIAL = "dial"
        const val THEME_GLASS2 = "glass2"
        private val THEMES = setOf(THEME_HUD, THEME_GLASS, THEME_BUBBLES, THEME_DIAL, THEME_GLASS2)

        /* Whether the running service holds the microphone type, so a voice note can record. */
        var micReady = false
            private set

        fun wheelTheme(ctx: Context): String =
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).getString(KEY_THEME, THEME_HUD)
                ?.takeIf { it in THEMES } ?: THEME_HUD

        fun setWheelTheme(ctx: Context, theme: String): String {
            val t = if (theme in THEMES) theme else THEME_HUD
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).edit { putString(KEY_THEME, t) }
            return t
        }

        /* Blur behind the menu themes, Dial and Glass 2, from the Config page; off by default. */
        fun dialBlur(ctx: Context): Boolean =
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).getBoolean(KEY_DIAL_BLUR, false)

        fun setDialBlur(ctx: Context, on: Boolean): Boolean {
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).edit { putBoolean(KEY_DIAL_BLUR, on) }
            return on
        }

        /* The menu themes' words: one at a time, under the finger (the default), or none. */
        fun menuWords(ctx: Context): Boolean =
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).getString(KEY_WORDS, WORDS_ONE) != WORDS_NONE

        fun setMenuWords(ctx: Context, words: String): String {
            val w = if (words == WORDS_NONE) WORDS_NONE else WORDS_ONE
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).edit { putString(KEY_WORDS, w) }
            return w
        }

        fun menuWordsName(ctx: Context): String = if (menuWords(ctx)) WORDS_ONE else WORDS_NONE

        /* Whether Recent shows only the agents of the console's current view; off by default. */
        fun recentFollowsView(ctx: Context): Boolean =
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).getBoolean(KEY_FOLLOW, false)

        fun setRecentFollowsView(ctx: Context, on: Boolean): Boolean {
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).edit { putBoolean(KEY_FOLLOW, on) }
            return on
        }

        /* Whether Needs you shows only the waiting agents of the console's view; off by default. */
        fun needsFollowsView(ctx: Context): Boolean =
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).getBoolean(KEY_NEEDS_FOLLOW, false)

        fun setNeedsFollowsView(ctx: Context, on: Boolean): Boolean {
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).edit { putBoolean(KEY_NEEDS_FOLLOW, on) }
            return on
        }

        /* The console's current view, as the page last told the app: all, work, demo or personal. */
        fun menuView(ctx: Context): String =
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).getString(KEY_VIEW, "all") ?: "all"

        fun setMenuView(ctx: Context, view: String): String {
            val v = view.takeIf { it in setOf("all", "work", "demo", "personal") } ?: "all"
            ctx.getSharedPreferences(SETTINGS, Context.MODE_PRIVATE).edit { putString(KEY_VIEW, v) }
            return v
        }

        const val WORDS_ONE = "one"
        const val WORDS_NONE = "none"
        private const val KEY_WORDS = "menu_words"
        private const val KEY_FOLLOW = "recent_follows_view"
        private const val KEY_NEEDS_FOLLOW = "needs_follows_view"
        private const val KEY_VIEW = "menu_view"
        private const val KEY_DIAL_BLUR = "dial_blur"
        private const val SETTINGS = "settings"
        private const val KEY_THEME = "wheel_theme"

        /* The bubble is retired (The owner, 25 October 2026: "I barely even use it"). The power
           button's assistant panel takes its place. Its code stays until that panel ships. */
        const val BUBBLE = false

        fun canBubble(ctx: Context) = BUBBLE && !bubbleHidden && Settings.canDrawOverlays(ctx)

        /* A running service is told directly. A new one is started only from here, and every
           caller that can start it (the activity on screen, a call that just began on screen)
           is in the foreground, which Android 12 and later require. */
        fun refresh(ctx: Context) {
            main.post {
                val running = instance.get()
                if (running != null) {
                    running.apply()
                } else {
                    try {
                        ContextCompat.startForegroundService(ctx, Intent(ctx, CommandService::class.java))
                    } catch (e: Exception) {
                        // Refused from the background. The next time the app is on screen it runs.
                    }
                }
            }
        }

        /* the front desk is ringing: the service takes the phone call type for the ring. */
        var ringingNow = false
            private set

        fun ringing(on: Boolean) = main.post {
            if (ringingNow == on) return@post
            ringingNow = on
            instance.get()?.apply()
        }

        /* Who is on the line, named by the page. */
        var callWho = "The front desk"
            private set

        fun callName(name: String) = main.post {
            callWho = name.ifBlank { "The front desk" }
            instance.get()?.apply()
        }

        fun callStarted(ctx: Context) = main.post {
            DeskTelecom.started(ctx.applicationContext)
            CallAudio.started(ctx)
            callLive = true
            callSince = android.os.SystemClock.elapsedRealtime()
            muted = false
            paused = false
            refresh(ctx.applicationContext)
        }

        /* A hang up from outside the page: a headset, a watch, the phone system. */
        fun hangUp(ctx: Context) {
            instance.get()?.act("hangup") ?: callEnded(ctx)
        }

        fun callEnded(ctx: Context) = main.post {
            DeskTelecom.stopped()
            CallAudio.ended(ctx)
            callLive = false
            refresh(ctx.applicationContext)
        }

        fun callState(isMuted: Boolean, isPaused: Boolean) = main.post {
            muted = isMuted
            paused = isPaused
            instance.get()?.apply()
        }
    }
}
