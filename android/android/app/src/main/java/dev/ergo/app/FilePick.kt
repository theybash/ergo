package dev.ergo.app

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.webkit.ValueCallback
import android.webkit.WebChromeClient

/* The phone's file picker, for the floating chat. A window over a game cannot start a picker and
   get its answer back: only an activity can. So this one opens it, sees nothing itself, hands the
   files to the chat, and goes (The owner, 31 October 2026: "photos don't work in floating window"). */
class FilePick : Activity() {

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        if (saved != null) return      // brought back after Android let it go: the picker is still open
        val ask = asked
        if (ask == null || callback == null) { give(null); return }
        try {
            @Suppress("DEPRECATION")
            startActivityForResult(ask, PICK)
        } catch (e: Throwable) {
            Log.w("CCFloat", "no picker", e)
            give(null)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != PICK) return
        // Several picked: each is in the clip. One: the plain answer.
        val many = data?.clipData?.let { c -> (0 until c.itemCount).mapNotNull { c.getItemAt(it).uri } }
        give(if (!many.isNullOrEmpty()) many.toTypedArray() else WebChromeClient.FileChooserParams.parseResult(resultCode, data))
    }

    private fun give(files: Array<Uri>?) {
        val cb = callback
        callback = null
        asked = null
        try { cb?.onReceiveValue(files) } catch (_: Throwable) { }
        done?.invoke()
        done = null
        finish()
        overridePendingTransition(0, 0)
    }

    companion object {
        private const val PICK = 7
        var callback: ValueCallback<Array<Uri>>? = null
        var asked: Intent? = null
        var done: (() -> Unit)? = null
    }
}
