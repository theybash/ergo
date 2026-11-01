package dev.ergo.app

import android.app.Activity
import android.app.KeyguardManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.RingtoneManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.app.NotificationCompat
import androidx.core.app.Person
import org.json.JSONObject

/* the front desk calls. An urgent item from an agent rings the phone like a call: the ringtone, over the
   lock screen, "<agent> is calling" with Answer and Decline. Answer opens Ergo straight into a
   the front desk call, and he says what is urgent first. Decline, or 45 seconds unanswered, leaves it as a
   missed call; the item stays in his inbox and the front desk brings it up on the next call.

   The ring comes on the notices' stream digest. The full screen needs "full-screen notifications"
   allowed for Ergo on Android 14; without it the call still rings as a heads-up notification. */
object DeskCall {

    const val CHANNEL = "desk_calls"
    private const val NOTE = 11
    private const val TAG_RING = "desk-ring"
    const val EXTRA_TEXT = "ring_text"
    const val EXTRA_AGENT = "ring_agent"
    const val EXTRA_MORE = "ring_more"
    const val EXTRA_AGENT_ID = "ring_agent_id"
    const val EXTRA_ID = "ring_id"
    private var rang: String? = null
    /* The ring notification as last posted, while it rings, so the call screen can take its banner
       down while it shows and put it back if he leaves without answering. */
    private var ringing: android.app.Notification? = null
    private var player: android.media.MediaPlayer? = null
    private var buzz: Vibrator? = null
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private val quiet = Runnable { ringer(null, false) }

    /* The ringtone and the buzz, played by Ergo itself and looped until the call is answered,
       declined or missed, the way WhatsApp rings. A notification's own sound was cut short by
       ColorOS after about twenty seconds. Silent and vibrate modes are obeyed. */
    private fun ringer(ctx: Context?, on: Boolean) {
        main.removeCallbacks(quiet)
        try { player?.stop() } catch (_: Throwable) {}
        try { player?.release() } catch (_: Throwable) {}
        try { buzz?.cancel() } catch (_: Throwable) {}
        player = null
        buzz = null
        CommandService.ringing(false)
        if (!on || ctx == null) return
        CommandService.ringing(true)
        val audio = ctx.getSystemService(AudioManager::class.java)
        val mode = audio.ringerMode
        // A looping media player on the ring volume, the way Signal and Telegram ring. Held here,
        // so nothing frees it mid ring.
        if (mode == AudioManager.RINGER_MODE_NORMAL) {
            try {
                player = android.media.MediaPlayer().apply {
                    setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                    setDataSource(ctx, RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE))
                    isLooping = true
                    prepare()
                    start()
                }
            } catch (e: Throwable) {
                android.util.Log.i("CCCall", "no ringtone: $e")
            }
        }
        if (mode != AudioManager.RINGER_MODE_SILENT) {
            try {
                val v = if (Build.VERSION.SDK_INT >= 31) ctx.getSystemService(VibratorManager::class.java).defaultVibrator
                        else @Suppress("DEPRECATION") ctx.getSystemService(Vibrator::class.java)
                val wave = VibrationEffect.createWaveform(longArrayOf(0, 1000, 1000), 0)
                if (Build.VERSION.SDK_INT >= 33) {
                    // Marked as a ringtone, which is what lets a background app keep vibrating.
                    v.vibrate(wave, android.os.VibrationAttributes.createForUsage(android.os.VibrationAttributes.USAGE_RINGTONE))
                } else {
                    v.vibrate(wave, AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE).build())
                }
                buzz = v
            } catch (e: Throwable) {
                android.util.Log.i("CCCall", "no vibration: $e")
            }
        }
        main.postDelayed(quiet, 45_000)
    }

    /* The call's notification category. It has no sound of its own: Ergo rings itself. If it
       was turned down, by ColorOS or by hand, below what a call needs, it is replaced by a fresh
       one at full importance, the trick Telegram uses. Its id carries a number for that. */
    private const val PREFS = "desk_calls"

    fun channelId(ctx: Context): String {
        val n = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getInt("channel", 2)
        return "desk_calls_$n"
    }

    fun channel(ctx: Context): String {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.deleteNotificationChannel(CHANNEL)  // the first one, which had a sound of its own
        var id = channelId(ctx)
        val have = nm.getNotificationChannel(id)
        if (have != null && have.importance < NotificationManager.IMPORTANCE_HIGH) {
            nm.deleteNotificationChannel(id)
            val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            prefs.edit().putInt("channel", prefs.getInt("channel", 2) + 1).apply()
            id = channelId(ctx)
        }
        if (nm.getNotificationChannel(id) == null) {
            nm.createNotificationChannel(NotificationChannel(id, "The front desk calls", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "The front desk rings when an agent marks something urgent."
                setSound(null, null)
                enableVibration(false)
                setBypassDnd(true)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            })
        }
        return id
    }

    /* A ring from the digest. Each one rings once. */
    fun ring(ctx: Context, r: JSONObject) {
        val id = r.optString("id")
        val key = id + ":" + r.optString("at")
        if (id.isBlank() || key == rang) return
        rang = key
        val nm = ctx.getSystemService(NotificationManager::class.java)
        val channelId = channel(ctx)
        // The daemon sends the agent's own name. A ring with none is Ergo's.
        val agent = r.optString("agent").ifBlank { "Ergo" }.replace('-', ' ').replaceFirstChar { it.uppercase() }
        val count = r.optInt("count", 1)
        val text = r.optString("text")
        val more = if (count > 1) "and ${count - 1} more · " else ""

        val answer = PendingIntent.getActivity(ctx, 41,
            Intent(ctx, MainActivity::class.java).putExtra(MainActivity.EXTRA_GO, "voice")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val decline = PendingIntent.getActivity(ctx, 42,
            Intent(ctx, IncomingCall::class.java).putExtra("decline", true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val screen = PendingIntent.getActivity(ctx, 43,
            Intent(ctx, IncomingCall::class.java).putExtra(EXTRA_TEXT, text).putExtra(EXTRA_AGENT, agent).putExtra(EXTRA_MORE, more).putExtra(EXTRA_ID, id)
                .putExtra(EXTRA_AGENT_ID, r.optString("agent_id"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_USER_ACTION),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val caller = Person.Builder().setName(agent).setImportant(true).build()
        val n = NotificationCompat.Builder(ctx, channelId)
            .setSmallIcon(R.drawable.ic_note)
            .setContentTitle("$agent is calling")
            .setContentText("${more}urgent")
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setFullScreenIntent(screen, true)
            .setContentIntent(screen)
            .setOngoing(true)
            .setAutoCancel(true)
            .setTimeoutAfter(45_000)
            .setStyle(NotificationCompat.CallStyle.forIncomingCall(caller, decline, answer))
            .addAction(0, "Call back in 10", PendingIntent.getActivity(ctx, 44,
                Intent(ctx, IncomingCall::class.java).putExtra("callback", true).putExtra(EXTRA_ID, id)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
            .build()
        // The sound and the buzz are Ergo's own, looped until answered; the notification only shows.
        // And a call the phone system knows about, so a headset or watch can answer it too.
        DeskTelecom.incoming(ctx, agent)
        ringing = n
        try {
            nm.notify(TAG_RING, NOTE, n)
        } catch (e: SecurityException) {
            android.util.Log.i("CCCall", "notifications are not allowed: $e")
        }
        ringer(ctx, true)
        // And the call screen opened directly as well. Android allows it while the bubble is on
        // screen; when it is not allowed, the full-screen notification above is the way in.
        try {
            ctx.startActivity(Intent(ctx, IncomingCall::class.java).putExtra(EXTRA_TEXT, text)
                .putExtra(EXTRA_AGENT, agent).putExtra(EXTRA_MORE, more).putExtra(EXTRA_ID, id).putExtra(EXTRA_AGENT_ID, r.optString("agent_id"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_USER_ACTION))
        } catch (e: Throwable) {
            android.util.Log.i("CCCall", "the call screen did not open by itself: $e")
        }
    }

    /* The call screen is showing: the banner on top of it says the same thing again and covered
       the front desk's name, so it goes. The ring itself keeps playing. */
    fun hideBanner(ctx: Context) {
        if (ringing != null) ctx.getSystemService(NotificationManager::class.java).cancel(TAG_RING, NOTE)
    }

    /* He left the call screen while it still rings: the banner comes back, the way a phone
       call's does. */
    fun showBanner(ctx: Context) {
        val n = ringing ?: return
        try { ctx.getSystemService(NotificationManager::class.java).notify(TAG_RING, NOTE, n) } catch (_: SecurityException) {}
    }

    fun stop(ctx: Context) {
        ringing = null
        ringer(null, false)
        ctx.getSystemService(NotificationManager::class.java).cancel(TAG_RING, NOTE)
    }

    /* What stands between the front desk and a full call screen, for the Config page. */
    fun setup(ctx: Context): org.json.JSONObject {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        val ch = nm.getNotificationChannel(channel(ctx))
        val power = ctx.getSystemService(android.os.PowerManager::class.java)
        return org.json.JSONObject()
            .put("notifications", nm.areNotificationsEnabled())
            .put("fullScreen", if (Build.VERSION.SDK_INT >= 34) nm.canUseFullScreenIntent() else true)
            .put("channelOn", ch != null && ch.importance >= NotificationManager.IMPORTANCE_HIGH)
            .put("lockScreen", ch == null || ch.lockscreenVisibility != Notification.VISIBILITY_SECRET)
            .put("battery", power.isIgnoringBatteryOptimizations(ctx.packageName))
            .put("maker", Build.MANUFACTURER.lowercase())
            .put("telecom", DeskTelecom.enabled(ctx))
            // Alarms ring through a channel of their own. OnePlus turns banners off for a new
            // channel, and then an alarm with the phone in hand only lands in the shade.
            .put("alarmBanner", alarm(nm).importance >= NotificationManager.IMPORTANCE_HIGH)
            .put("alarmLock", alarm(nm).lockscreenVisibility != Notification.VISIBILITY_SECRET)
    }

    private fun alarm(nm: NotificationManager): android.app.NotificationChannel {
        ErgoAlarm.channel(nm)
        return nm.getNotificationChannel(ErgoAlarm.CHANNEL)
    }

    /* Opens the switch for one of the setup items. */
    fun openSetting(ctx: Context, which: String) {
        val pkg = ctx.packageName
        val intent = when (which) {
            "fullScreen" -> if (Build.VERSION.SDK_INT >= 34)
                Intent(android.provider.Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, Uri.parse("package:$pkg")) else null
            "channelOn", "lockScreen" -> Intent(android.provider.Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
                .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, pkg)
                .putExtra(android.provider.Settings.EXTRA_CHANNEL_ID, channelId(ctx))
            "alarmBanner", "alarmLock" -> Intent(android.provider.Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
                .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, pkg)
                .putExtra(android.provider.Settings.EXTRA_CHANNEL_ID, ErgoAlarm.CHANNEL)
            "notifications" -> Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, pkg)
            "app" -> Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$pkg"))
            "battery" -> Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$pkg"))
            else -> null
        } ?: Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$pkg"))
        try {
            ctx.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Throwable) {
            ctx.startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$pkg"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }
}

/* The screen over the lock screen while an agent rings: mock A, chosen by the owner on 6 October 2026,
   with the caller's own name large in the serif and no orb (25 October 2026). How urgent under it,
   the agent's words quoted, round Decline and Answer mirrored at the bottom, and "Call me back in 10
   minutes" under them. Drawn natively, so it never waits on a page to load behind the lock screen. */
class IncomingCall : Activity() {

    private val bg = Color.parseColor("#0F0E0D")
    private val ink = Color.parseColor("#EBE6DF")
    private val dim = Color.parseColor("#A8A29E")
    private val faint = Color.parseColor("#6F6964")
    private val mint = Color.parseColor("#66DBAF")
    private val rose = Color.parseColor("#E25958")

    private fun dp(v: Float) = v * resources.displayMetrics.density

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val id = intent?.getStringExtra(DeskCall.EXTRA_ID) ?: ""
        if (intent?.getBooleanExtra("decline", false) == true) {
            DeskCall.stop(this); DeskTelecom.declined(); finish(); return
        }
        if (intent?.getBooleanExtra("callback", false) == true) {
            callBack(id); finish(); return
        }
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        window.statusBarColor = bg
        window.navigationBarColor = bg
        val serif = androidx.core.content.res.ResourcesCompat.getFont(this, R.font.instrument_serif_regular)
        val italic = androidx.core.content.res.ResourcesCompat.getFont(this, R.font.instrument_serif_italic)
        val sans = androidx.core.content.res.ResourcesCompat.getFont(this, R.font.inter_medium)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            // One flat colour. The owner, 25 October 2026: gradients look sloppy.
            setBackgroundColor(bg)
            setPadding(0, dp(88f).toInt(), 0, dp(40f).toInt())
        }
        fun text(t: CharSequence, size: Float, color: Int, face: android.graphics.Typeface?) = TextView(this).apply {
            text = t; textSize = size; setTextColor(color); typeface = face; gravity = Gravity.CENTER
        }
        // The agent that is calling, large. No orb: the words and the buttons are the screen.
        val agent = (intent?.getStringExtra(DeskCall.EXTRA_AGENT) ?: "Ergo")
            .replace('-', ' ').replaceFirstChar { it.uppercase() }
        root.addView(text(agent, 58f, ink, serif))
        val more = intent?.getStringExtra(DeskCall.EXTRA_MORE) ?: ""
        val who = android.text.SpannableString("${more}urgent").apply {
            setSpan(android.text.style.ForegroundColorSpan(rose), length - 6, length, 0)
        }
        root.addView(text(who, 15f, dim, sans).apply { setPadding(0, dp(6f).toInt(), 0, 0) })
        root.addView(android.view.View(this), LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        val words = intent?.getStringExtra(DeskCall.EXTRA_TEXT) ?: ""
        if (words.isNotBlank()) {
            root.addView(text("\u201C$words\u201D", 22f, Color.parseColor("#D6D0C8"), italic).apply {
                setPadding(dp(36f).toInt(), 0, dp(36f).toInt(), dp(32f).toInt())
                // A long message gets smaller type before it gets cut, and a cut ends with an
                // ellipsis; the front desk reads the whole thing on the call.
                textSize = when {
                    words.length > 220 -> 16f
                    words.length > 140 -> 18f
                    words.length > 90 -> 20f
                    else -> 22f
                }
                maxLines = 5
                ellipsize = android.text.TextUtils.TruncateAt.END
                setLineSpacing(0f, 1.12f)
            })
        }
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER }
        fun round(label: String, color: Int, turn: Float, icon: Int = R.drawable.ic_call, act: () -> Unit) = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            val disc = android.widget.ImageView(context).apply {
                setImageResource(icon)
                rotation = turn
                scaleType = android.widget.ImageView.ScaleType.CENTER
                background = android.graphics.drawable.GradientDrawable().apply { shape = android.graphics.drawable.GradientDrawable.OVAL; setColor(color) }
                contentDescription = label
                setOnClickListener { act() }
            }
            addView(disc, LinearLayout.LayoutParams(dp(76f).toInt(), dp(76f).toInt()))
            addView(text(label, 13f, dim, sans).apply { setPadding(0, dp(12f).toInt(), 0, 0); maxLines = 1 })
        }
        // Equal thirds of the width, each button centred in its third: mirrored round Prompt, and
        // never wider than the screen whatever the phone's display size. Prompt shows only when
        // the ring names its agent.
        fun third() = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        val agentId = intent?.getStringExtra(DeskCall.EXTRA_AGENT_ID).orEmpty()
        row.addView(round("Decline", rose, 135f) { DeskCall.stop(this); DeskTelecom.declined(); finish() }, third())
        if (agentId.isNotBlank()) row.addView(round("Prompt", dim, 0f, R.drawable.ic_prompt) { prompt(agentId, words) }, third())
        row.addView(round("Answer", mint, 0f) { answer() }, third())
        root.addView(row, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
            setMargins(dp(24f).toInt(), 0, dp(24f).toInt(), 0)
        })
        root.addView(text("Call me back in 10 minutes", 14f, faint, sans).apply {
            setPadding(dp(24f).toInt(), dp(28f).toInt(), dp(24f).toInt(), dp(12f).toInt())
            setOnClickListener { callBack(id); finish() }
        })
        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        DeskCall.hideBanner(this)
    }

    override fun onStop() {
        super.onStop()
        if (!isFinishing) DeskCall.showBanner(this)
    }

    private fun answer() {
        DeskCall.stop(this)
        val km = getSystemService(KeyguardManager::class.java)
        val open = {
            startActivity(Intent(this, MainActivity::class.java).putExtra(MainActivity.EXTRA_GO, "voice")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
            finish()
        }
        // A locked phone asks to be unlocked first; the call opens once it is.
        if (Build.VERSION.SDK_INT >= 26 && km.isKeyguardLocked) {
            km.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() = open()
            })
        } else open()
    }

    /* Prompt: the call is declined, and the agent's own chat opens with the call's sentence
       quoted above the message box, as Prompt on an offer card does (The owner, 22 October 2026). */
    private fun prompt(agent: String, words: String) {
        DeskCall.stop(this)
        DeskTelecom.declined()
        val km = getSystemService(KeyguardManager::class.java)
        val open = {
            startActivity(Intent(this, MainActivity::class.java).putExtra(MainActivity.EXTRA_GO, "chat:$agent")
                .putExtra(MainActivity.EXTRA_PIN, words)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
            finish()
        }
        if (Build.VERSION.SDK_INT >= 26 && km.isKeyguardLocked) {
            km.requestDismissKeyguard(this, object : KeyguardManager.KeyguardDismissCallback() {
                override fun onDismissSucceeded() = open()
            })
        } else open()
    }

    /* "Call me back in 10 minutes": the ring stops now and the daemon rings the same item again
       then. Sent off the main thread; if the laptop cannot be reached, the item still waits in
       the inbox for the front desk's next call. */
    private fun callBack(id: String) {
        DeskCall.stop(this)
        DeskTelecom.declined()
        if (id.isBlank()) return
        Thread {
            try {
                val conn = java.net.URL(Daemon.phone + "/callback").openConnection() as java.net.HttpURLConnection
                conn.connectTimeout = 5000; conn.readTimeout = 8000
                conn.requestMethod = "POST"; conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(org.json.JSONObject().put("id", id).toString().toByteArray()) }
                conn.responseCode
                conn.disconnect()
            } catch (e: Throwable) {
                android.util.Log.i("CCCall", "call back did not reach the laptop: $e")
            }
        }.start()
    }
}
