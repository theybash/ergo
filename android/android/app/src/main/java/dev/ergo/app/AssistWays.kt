package dev.ergo.app

import android.app.Activity
import android.content.Intent
import android.graphics.drawable.Icon
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import android.service.quicksettings.TileService
import android.widget.Toast

/* The other ways into assistant mode. Each opens the same bar the power key opens.

   - "Ask Ergo" in the menu that appears when text is selected in any app: the bar opens with
     that text as a quote, ready to ask about.
   - The Quick Settings tile "Ask an agent": for a phone whose power key is kept for something
     else.
   - The share sheet: share a link, some text or a picture to Ergo, and the bar opens with it.

   When Ergo is not the digital assistant, the bar cannot open; the phone's assistant setting
   opens instead, with one line saying why. */
object AssistWays {
    fun open(a: Activity, args: Bundle) {
        if (!AssistService.open(args)) {
            Toast.makeText(a, "Choose Ergo as the digital assistant first", Toast.LENGTH_LONG).show()
            try {
                a.startActivity(Intent(Settings.ACTION_VOICE_INPUT_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            } catch (_: Throwable) {
                try { a.startActivity(Intent(Settings.ACTION_SETTINGS)) } catch (_: Throwable) {}
            }
        }
    }
}

/* The text selection menu's "Ask Ergo". */
class AskTextActivity : Activity() {
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        val text = intent.getCharSequenceExtra(Intent.EXTRA_PROCESS_TEXT)?.toString()?.trim()
        if (!text.isNullOrBlank()) {
            AssistWays.open(this, Bundle().apply { putString(AssistService.ARG_QUOTE, text.take(4000)) })
        }
        finish()
    }
}

/* Shared to Ergo from any app: text and links become the quote; a picture rides along as the
   screen does when he taps the image button. */
class AskShareActivity : Activity() {
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        val args = Bundle()
        val text = listOfNotNull(intent.getStringExtra(Intent.EXTRA_SUBJECT), intent.getStringExtra(Intent.EXTRA_TEXT))
            .joinToString("\n").trim()
        if (text.isNotBlank()) args.putString(AssistService.ARG_QUOTE, text.take(4000))
        @Suppress("DEPRECATION")
        val image = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
        if (image != null && intent.type?.startsWith("image/") == true) args.putString(AssistService.ARG_SHARED, image.toString())
        AssistWays.open(this, args)
        finish()
    }
}

/* Quick Settings: "Ask an agent". */
class AskTile : TileService() {
    override fun onStartListening() {
        super.onStartListening()
        qsTile?.let {
            it.label = "Ask an agent"
            it.icon = Icon.createWithResource(this, R.drawable.ic_assist_tile)
            it.state = android.service.quicksettings.Tile.STATE_INACTIVE
            it.updateTile()
        }
    }

    override fun onClick() {
        super.onClick()
        if (!AssistService.open(Bundle())) {
            val i = Intent(Settings.ACTION_VOICE_INPUT_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            if (android.os.Build.VERSION.SDK_INT >= 34) {
                startActivityAndCollapse(android.app.PendingIntent.getActivity(this, 0, i,
                    android.app.PendingIntent.FLAG_IMMUTABLE))
            } else {
                @Suppress("DEPRECATION", "StartActivityAndCollapseDeprecated") startActivityAndCollapse(i)
            }
        }
    }
}
