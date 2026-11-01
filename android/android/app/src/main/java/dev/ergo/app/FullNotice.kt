package dev.ergo.app

import android.app.Activity
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.app.NotificationCompat

/* A full-screen notice from the daemon, such as the daily quota alert: quiet, in the alarm
   screen's look, with one button. It stops nothing; it only tells him.

   The screen opens straight away where Android allows it (Ergo may draw over other apps, which
   lets it start a screen from the background). A full-screen notification is the way in where it
   does not, and it stays in the shade until he opens it. */
object FullNotice {
    private const val CHANNEL = "ergo_notice_1"
    private const val ID = 14
    const val EXTRA_TITLE = "notice_title"
    const val EXTRA_TEXT = "notice_text"

    fun show(ctx: Context, title: String, text: String) {
        val app = ctx.applicationContext
        val screen = Intent(app, NoticeScreen::class.java).putExtra(EXTRA_TITLE, title).putExtra(EXTRA_TEXT, text)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val nm = app.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Notices", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Full-screen notices from Ergo, such as the daily usage line."
                setSound(null, null)
            })
        }
        val open = PendingIntent.getActivity(app, 61, screen, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(app, CHANNEL)
            .setSmallIcon(R.drawable.ic_note)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setFullScreenIntent(open, true)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        try { nm.notify(ID, n) } catch (_: SecurityException) {}
        try { app.startActivity(screen) } catch (_: Throwable) {}
    }

    fun clear(ctx: Context) = ctx.getSystemService(NotificationManager::class.java).cancel(ID)
}

class NoticeScreen : Activity() {
    private val bg = Color.parseColor("#0F0E0D")
    private val ink = Color.parseColor("#EBE6DF")
    private val mint = Color.parseColor("#66DBAF")
    private fun dp(v: Float) = v * resources.displayMetrics.density

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 27) { setShowWhenLocked(true); setTurnScreenOn(true) }
        window.statusBarColor = bg
        window.navigationBarColor = bg
        FullNotice.clear(this)
        val title = intent?.getStringExtra(FullNotice.EXTRA_TITLE) ?: "Ergo"
        val text = intent?.getStringExtra(FullNotice.EXTRA_TEXT) ?: ""
        val serif = androidx.core.content.res.ResourcesCompat.getFont(this, R.font.instrument_serif_regular)
        val sans = androidx.core.content.res.ResourcesCompat.getFont(this, R.font.inter_medium)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setBackgroundColor(bg)
            setPadding(dp(28f).toInt(), dp(140f).toInt(), dp(28f).toInt(), dp(56f).toInt())
        }
        root.addView(TextView(this).apply {
            this.text = "NOTICE"; textSize = 12f; setTextColor(Color.parseColor("#6F6964")); typeface = sans
            gravity = Gravity.CENTER; letterSpacing = 0.18f
        })
        root.addView(TextView(this).apply {
            this.text = title; textSize = 40f; setTextColor(ink); typeface = serif; gravity = Gravity.CENTER
            setPadding(0, dp(12f).toInt(), 0, 0)
        })
        root.addView(TextView(this).apply {
            this.text = text; textSize = 18f; setTextColor(Color.parseColor("#D6D0C8")); typeface = sans
            gravity = Gravity.CENTER; setLineSpacing(0f, 1.3f)
            setPadding(0, dp(24f).toInt(), 0, 0)
        })
        root.addView(android.view.View(this), LinearLayout.LayoutParams(1, 0, 1f))
        root.addView(TextView(this).apply {
            this.text = "OK"; textSize = 17f; setTextColor(bg); typeface = sans; gravity = Gravity.CENTER
            background = android.graphics.drawable.GradientDrawable().apply { cornerRadius = dp(32f); setColor(mint) }
            setOnClickListener { finish() }
        }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(64f).toInt()))
        setContentView(root)
    }

    /* A newer notice while this one shows: it takes its place. */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        recreate()
    }

    override fun onResume() {
        super.onResume()
        FullNotice.clear(this)
    }
}
