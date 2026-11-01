package dev.ergo.app

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.PowerManager

/* Where a the front desk call is heard, and the screen at his ear.

   A call starts on the earpiece, like a phone call, unless headphones or a Bluetooth set are on,
   and then it stays on those. The Speaker button moves it to the loudspeaker and back. While it is
   on the earpiece, the proximity sensor turns the screen off when the phone is at his ear, so a
   cheek cannot press Hang up. */
object CallAudio {
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private var ear: PowerManager.WakeLock? = null
    var speaker = false
        private set
    private var live = false
    /* The page's own audio (the WebView) picks its route too, the loudspeaker by default, and
       picks again when it likes. While a call is live this puts the chosen one back each time. */
    private var watcher: Any? = null
    private var fixes = 0
    private var last = ""

    private val HEADSETS = setOf(
        AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO, AudioDeviceInfo.TYPE_USB_HEADSET,
        AudioDeviceInfo.TYPE_BLE_HEADSET, AudioDeviceInfo.TYPE_HEARING_AID)

    private fun headset(am: AudioManager) =
        am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).any { it.type in HEADSETS }

    fun started(ctx: Context) {
        live = true
        speaker = false
        val app = ctx.applicationContext
        if (Build.VERSION.SDK_INT >= 31 && watcher == null) {
            val am = app.getSystemService(AudioManager::class.java)
            val l = AudioManager.OnCommunicationDeviceChangedListener { dev ->
                last = "changed to " + (dev?.type ?: -1); if (live && dev?.type != wanted(am)) { fixes++; main.postDelayed({ if (live) route(app) }, 150) }
            }
            am.addOnCommunicationDeviceChangedListener(app.mainExecutor, l)
            watcher = l
        }
        route(app)
        // The page's own audio sets its route as it opens, a moment after the call starts; this
        // puts ours back on top of it.
        main.postDelayed({ if (live) route(ctx.applicationContext) }, 2000)
    }

    fun ended(ctx: Context) {
        live = false
        main.removeCallbacksAndMessages(null)
        if (Build.VERSION.SDK_INT >= 31) (watcher as? AudioManager.OnCommunicationDeviceChangedListener)?.let {
            ctx.applicationContext.getSystemService(AudioManager::class.java).removeOnCommunicationDeviceChangedListener(it)
        }
        watcher = null
        earLock(ctx, false)
        val am = ctx.getSystemService(AudioManager::class.java)
        try {
            if (Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice()
            else @Suppress("DEPRECATION") { am.isSpeakerphoneOn = false }
        } catch (_: Throwable) {}
    }

    fun setSpeaker(ctx: Context, on: Boolean) {
        speaker = on
        if (live) route(ctx.applicationContext)
    }

    /* What the phone's audio is doing, for the report the Speaker button sends to Creator. */
    fun info(ctx: Context): org.json.JSONObject {
        val am = ctx.getSystemService(AudioManager::class.java)
        val o = org.json.JSONObject()
            .put("sdk", Build.VERSION.SDK_INT).put("maker", Build.MANUFACTURER).put("model", Build.MODEL)
            .put("live", live).put("speakerWanted", speaker).put("mode", am.mode)
            .put("speakerphoneOn", @Suppress("DEPRECATION") am.isSpeakerphoneOn)
            .put("headset", headset(am)).put("watcherFixes", fixes).put("lastChange", last)
            .put("earLockHeld", ear?.isHeld == true)
            .put("telecomEndpoint", DeskTelecom.endpoint?.let { it.name.toString() + ":" + it.type } ?: "none")
            .put("telecomEndpoints", org.json.JSONArray(DeskTelecom.endpointNames()))
        if (Build.VERSION.SDK_INT >= 31) {
            o.put("commDevice", am.communicationDevice?.type ?: -1)
            o.put("available", org.json.JSONArray(am.availableCommunicationDevices.map { it.type }))
        }
        o.put("playing", org.json.JSONArray(am.activePlaybackConfigurations.map {
            "usage " + it.audioAttributes.usage + " content " + it.audioAttributes.contentType
        }))
        return o
    }

    /* The device the call should be on, or null for "leave it to the headset". */
    private fun wanted(am: AudioManager): Int? = when {
        speaker -> AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
        headset(am) -> null
        else -> AudioDeviceInfo.TYPE_BUILTIN_EARPIECE
    }

    private fun route(ctx: Context) {
        val am = ctx.getSystemService(AudioManager::class.java)
        val external = headset(am)
        // A call registered with Telecom is routed by Telecom, and only a request to it holds.
        if (DeskTelecom.route(speaker)) {
            earLock(ctx, live && !speaker && !external)
            return
        }
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                val want = wanted(am)
                if (want == null) { if (!external) am.clearCommunicationDevice() }
                else if (am.communicationDevice?.type != want) {
                    am.availableCommunicationDevices.firstOrNull { it.type == want }?.let { am.setCommunicationDevice(it) }
                }
            } else {
                @Suppress("DEPRECATION")
                am.isSpeakerphoneOn = speaker
            }
        } catch (e: Throwable) {
            android.util.Log.i("CCCall", "the call audio route was not set: $e")
        }
        earLock(ctx, live && !speaker && !external)
    }

    private fun earLock(ctx: Context, on: Boolean) {
        if (on) {
            if (ear == null) {
                val pm = ctx.getSystemService(PowerManager::class.java)
                if (!pm.isWakeLockLevelSupported(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK)) return
                ear = pm.newWakeLock(PowerManager.PROXIMITY_SCREEN_OFF_WAKE_LOCK, "ergo:ear")
            }
            if (ear?.isHeld == false) ear?.acquire(2 * 60 * 60 * 1000L)
        } else {
            ear?.let { if (it.isHeld) it.release(PowerManager.RELEASE_FLAG_WAIT_FOR_NO_PROXIMITY) }
        }
    }
}
