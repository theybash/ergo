package dev.ergo.app

import android.Manifest
import android.app.DownloadManager
import android.content.Intent
import android.os.Environment
import android.webkit.CookieManager
import android.webkit.URLUtil
import android.widget.Toast
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebViewRenderProcess
import android.webkit.WebViewRenderProcessClient
import android.provider.Settings
import android.webkit.WebView
import android.graphics.Color
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.core.content.edit
import androidx.core.net.toUri
import com.getcapacitor.BridgeActivity
import com.getcapacitor.WebViewListener
import org.json.JSONObject

/* The console, full screen, loaded live from the daemon at the saved laptop address (Daemon.kt), so the app never needs a rebuild when the console changes.

   Two rules keep a call alive off screen: the WebView is never paused here (no onPause, no
   pauseTimers; Capacitor's KeepRunning default leaves timers alone too), and the call runs
   inside CommandService as a microphone foreground service.

   Back goes back one level. While the console has history behind it, back is the WebView's
   goBack, which the console turns into the screen, agent, dialog or call it came from. With
   nothing behind, back does what it always did and the app leaves the screen. Capacitor alone
   does not do this: without its App plugin, back skips the WebView and closes the app. */
class MainActivity : BridgeActivity() {

    private val prefs by lazy { getSharedPreferences("app", MODE_PRIVATE) }
    private val updater by lazy { Updater(this) }

    /* When the app last left the screen, on the clock that counts deep sleep too. */
    private var stoppedAt = 0L
    private var reloadDue = false
    private var wentSince = false
    private var cover: View? = null

    private val askFirst = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        offerBubble()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        registerPlugin(CCNativePlugin::class.java)
        // The console comes from the saved laptop address. With none saved yet, Capacitor shows
        // the bundled www/index.html and the address screen opens on top.
        if (Daemon.isSet) config = Daemon.capConfig(this)
        super.onCreate(savedInstanceState)
        if (!Daemon.isSet) {
            startActivity(Intent(this, SetupActivity::class.java))
            return
        }
        // The renderer keeps its priority when the app is off screen, so Android does not
        // reclaim it in the middle of a call.
        bridge.webView.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false)
        watchRenderer()
        saveDownloads()
        CommandService.bubbleHidden = false
        // Once at app start: any alarm the daemon queued, and any notifications still waiting.
        Phone.checkCommands(this)
        Phone.flush(this)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                val web = bridge.webView
                if (web.canGoBack()) {
                    web.goBack()
                    return
                }
                // Nothing behind: the system's own back, once.
                isEnabled = false
                onBackPressedDispatcher.onBackPressed()
                isEnabled = true
            }
        })
        go(intent)
        firstLaunch()
    }

    /* A download link in the console (the Files tab, a task's file, an attachment) reaches the
       WebView as a download, and a WebView saves nothing by itself: without this the tap did
       nothing. Android's own download manager takes it instead, saves it to Downloads, and shows
       the notification that opens it. Only links from the console itself; anything else is ignored. */
    private fun saveDownloads() {
        bridge.webView.setDownloadListener { url, agent, disposition, mime, _ ->
            if (!url.startsWith("http")) return@setDownloadListener
            try {
                val name = URLUtil.guessFileName(url, disposition, mime)
                val ask = DownloadManager.Request(url.toUri())
                    .setTitle(name)
                    .setMimeType(mime)
                    .addRequestHeader("User-Agent", agent)
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                CookieManager.getInstance().getCookie(url)?.let { ask.addRequestHeader("Cookie", it) }
                // Before Android 10 the shared Downloads folder needs a storage permission the app
                // does not ask for, so those phones keep the file in the app's own folder.
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    ask.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
                } else {
                    ask.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, name)
                }
                getSystemService(DownloadManager::class.java).enqueue(ask)
                Toast.makeText(this, "Downloading $name", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                Toast.makeText(this, "Could not download: ${e.message}", Toast.LENGTH_LONG).show()
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.action == Intent.ACTION_MAIN) CommandService.bubbleHidden = false
        go(intent)
    }

    override fun onStart() {
        super.onStart()
        // Back after a long time away: start the page over (The owner, 21 October 2026, the app froze
        // after a long time closed). Android froze the app, and the connections the page held are
        // dead with nothing to say so. The page does the same itself; this also covers a page too
        // stuck to run it. Never in a call, which lives off screen.
        val away = if (stoppedAt > 0) SystemClock.elapsedRealtime() - stoppedAt else 0L
        stoppedAt = 0L
        // Decided in onResume, after onNewIntent: an Answer arrives as a new intent, and a reload
        // here would drop the call it asks for.
        reloadDue = away > AWAY_MS
        wentSince = false
        CommandService.appVisible = true
        CommandService.refresh(this)
        updater.check()
    }

    /* Also after the overlay settings screen, which is where the bubble first becomes allowed. */
    override fun onResume() {
        super.onResume()
        // A wheel slice or an Answer that came in with this return goes to the page, which starts
        // itself over with the call in its address (main.jsx). Only a plain return reloads here.
        if (reloadDue && !wentSince && !CommandService.callLive) {
            showReloading()
            bridge.webView.reload()
        }
        reloadDue = false
        CommandService.refresh(this)
        // Also after the "install unknown apps" settings screen, for an update that waits on it.
        updater.resume()
    }

    override fun onStop() {
        stoppedAt = SystemClock.elapsedRealtime()
        CommandService.appVisible = false
        CommandService.refresh(this)
        super.onStop()
    }

    /* A dead renderer leaves a blank screen, and one that stops answering leaves taps that do
       nothing. Either way the activity starts again with a new web view. Android would otherwise
       end the whole app for a dead renderer. A stuck renderer is ended first, which brings the
       same restart, but not in a call: a call off screen may be slow and is still worth keeping. */
    /* Back after a long time away the page starts over, and until the fresh one draws, Android
       keeps showing the old picture, which looks ready but ignores every tap (The owner, 30 October
       2026: "don't show old picture. Show reloading or smth"). A plain cover says what is going
       on and takes the taps. It goes when the page has loaded, or after ten seconds at most. */
    private fun showReloading() {
        if (cover != null) return
        val parent = bridge.webView.parent as? ViewGroup ?: return
        val words = TextView(this).apply {
            text = "Reloading"
            setTextColor(Color.parseColor("#78716C"))
            textSize = 14f
        }
        val view = FrameLayout(this).apply {
            setBackgroundColor(Color.parseColor("#0F0E0D"))
            isClickable = true
            addView(words, FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER))
        }
        parent.addView(view, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        cover = view
        view.postDelayed({ if (cover === view) hideReloading() }, 10_000)
    }

    private fun hideReloading() {
        val view = cover ?: return
        cover = null
        (view.parent as? ViewGroup)?.removeView(view)
    }

    private fun watchRenderer() {
        bridge.addWebViewListener(object : WebViewListener() {
            override fun onPageLoaded(webView: WebView?) {
                // The fresh page is in; a moment more lets it draw before the cover goes.
                bridge.webView.postDelayed({ hideReloading() }, 300)
            }

            override fun onRenderProcessGone(webView: WebView?, detail: RenderProcessGoneDetail?): Boolean {
                recreate()
                return true
            }
        })
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            bridge.webView.setWebViewRenderProcessClient(mainExecutor, object : WebViewRenderProcessClient() {
                override fun onRenderProcessUnresponsive(view: WebView, renderer: WebViewRenderProcess?) {
                    if (CommandService.appVisible && !CommandService.callLive) renderer?.terminate()
                }
                override fun onRenderProcessResponsive(view: WebView, renderer: WebViewRenderProcess?) {}
            })
        }
    }

    override fun onDestroy() {
        // The WebView goes with the activity, and the call with it.
        if (isFinishing) CommandService.callEnded(this)
        super.onDestroy()
    }

    /* A wheel slice sent the app here. A console that is up handles it in place, without a
       reload; one that is not up yet loads the same thing as a deep link. */
    private fun go(intent: Intent) {
        val what = intent.getStringExtra(EXTRA_GO) ?: return
        intent.removeExtra(EXTRA_GO)
        // Prompt on the front desk's call screen: the call's sentence, quoted in the chat it opens.
        val pin = intent.getStringExtra(EXTRA_PIN).orEmpty()
        intent.removeExtra(EXTRA_PIN)
        wentSince = true
        val (message, path) = when (what) {
            "voice" -> JSONObject().put("do", "voice") to "/?voice=call"
            "keys" ->
                if (CommandService.callLive) JSONObject().put("do", "keys") to "/?voice=keys"
                else JSONObject().put("do", "voice").put("keys", true) to "/?voice=keys"
            "pa" -> JSONObject().put("do", "screen").put("screen", "chat").put("hash", "#/a/personal-assistant") to
                "/?screen=chat#/a/personal-assistant"
            "fleet" -> JSONObject().put("do", "screen").put("screen", "fleet") to "/?screen=fleet"
            else -> {
                // A bar on the wheel: that agent's chat, where its ask card waits.
                val agent = what.removePrefix("chat:").takeIf { what.startsWith("chat:") && it.isNotBlank() }
                    ?: return
                val hash = "#/a/" + android.net.Uri.encode(agent)
                if (pin.isNotBlank()) {
                    JSONObject().put("do", "screen").put("screen", "chat").put("hash", hash).put("pin", pin) to
                        "/?screen=chat&pin=" + android.net.Uri.encode(pin) + hash
                } else {
                    JSONObject().put("do", "screen").put("screen", "chat").put("hash", hash) to "/?screen=chat$hash"
                }
            }
        }
        CCNativePlugin.send(message) { heard ->
            if (!heard && !CommandService.callLive) {
                bridge.webView.loadUrl(bridge.serverUrl.trimEnd('/') + path)
            }
        }
    }

    /* The microphone and notifications are asked for once, at the first launch. */
    private fun firstLaunch() {
        if (prefs.getBoolean("asked", false)) {
            offerBubble()
            return
        }
        prefs.edit { putBoolean("asked", true) }
        val want = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) want.add(Manifest.permission.POST_NOTIFICATIONS)
        askFirst.launch(want.toTypedArray())
    }

    /* The bubble needs "display over other apps", which only the settings screen can grant. */
    private fun offerBubble() {
        if (!CommandService.BUBBLE || Settings.canDrawOverlays(this) || prefs.getBoolean("noBubble", false)) return
        AlertDialog.Builder(this)
            .setTitle("Show the bubble?")
            .setMessage("A small bubble over other apps lets you call the front desk or open a chat from anywhere.")
            .setPositiveButton("Open settings") { _, _ ->
                startActivity(
                    Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, "package:$packageName".toUri())
                )
            }
            .setNegativeButton("Not now", null)
            .setNeutralButton("Never") { _, _ -> prefs.edit { putBoolean("noBubble", true) } }
            .show()
    }

    companion object {
        const val EXTRA_GO = "go"
        const val EXTRA_PIN = "pin"
        const val AWAY_MS = 10 * 60 * 1000L
    }
}
