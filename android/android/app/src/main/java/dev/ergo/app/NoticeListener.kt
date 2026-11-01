package dev.ergo.app

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import androidx.core.app.NotificationCompat
import org.json.JSONObject

/* Every notification the phone receives, sent to the laptop through the queue in Phone.

   Skipped: Ergo's own, ongoing ones, group summaries, and ones with no title and no text. It
   must never crash the app, so each item is caught on its own and dropped if it fails. Nothing
   it reads is shown or logged on the phone. */
class NoticeListener : NotificationListenerService() {

    private val main = Handler(Looper.getMainLooper())

    /* While connected, what did not send is tried again every few minutes. */
    private val retry = object : Runnable {
        override fun run() {
            Phone.flush(this@NoticeListener)
            main.postDelayed(this, RETRY_MS)
        }
    }

    override fun onListenerConnected() {
        try {
            Phone.flush(this)
            main.removeCallbacks(retry)
            main.postDelayed(retry, RETRY_MS)
        } catch (e: Throwable) {
            Log.w(TAG, "the listener did not start its retry")
        }
    }

    override fun onListenerDisconnected() {
        main.removeCallbacks(retry)
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        try {
            val item = sbn?.let { read(it) } ?: return
            Phone.queue(this, listOf(item))
        } catch (e: Throwable) {
            // Nothing of the notification in the log: only that one was dropped.
            Log.w(TAG, "a notification was dropped: ${e.javaClass.simpleName}")
        }
    }

    private fun read(sbn: StatusBarNotification): JSONObject? {
        if (sbn.packageName == packageName) return null
        val n = sbn.notification ?: return null
        if (n.flags and Notification.FLAG_ONGOING_EVENT != 0) return null
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return null
        val extras = n.extras
        var title = extras?.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        var text = (extras?.getCharSequence(Notification.EXTRA_BIG_TEXT)
            ?: extras?.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
        var conversation: String? = extras?.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)?.toString()
        // A messaging notification: the last message and who sent it.
        try {
            val style = NotificationCompat.MessagingStyle.extractMessagingStyleFromNotification(n)
            val last = style?.messages?.lastOrNull()
            if (last != null) {
                last.text?.toString()?.let { text = it }
                last.person?.name?.toString()?.takeIf { it.isNotBlank() }?.let { title = it }
                if (conversation.isNullOrBlank()) conversation = style.conversationTitle?.toString()
            }
        } catch (_: Throwable) {
        }
        if (title.isBlank() && text.isBlank()) return null
        val o = JSONObject()
            .put("key", sbn.key)
            .put("package", sbn.packageName)
            .put("app", label(sbn.packageName))
            .put("title", title)
            .put("text", text)
            .put("posted", sbn.postTime)
        if (!conversation.isNullOrBlank()) o.put("conversation", conversation)
        return o
    }

    private fun label(pkg: String): String = try {
        val pm = packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (_: PackageManager.NameNotFoundException) {
        pkg
    } catch (_: Throwable) {
        pkg
    }

    companion object {
        private const val TAG = "CCListener"
        private const val RETRY_MS = 3 * 60_000L

        /* Whether Ergo has notification access. */
        fun enabled(ctx: Context): Boolean = try {
            val flat = Settings.Secure.getString(ctx.contentResolver, "enabled_notification_listeners").orEmpty()
            val me = ComponentName(ctx, NoticeListener::class.java)
            flat.split(':').any { ComponentName.unflattenFromString(it) == me }
        } catch (_: Throwable) {
            false
        }
    }
}
