package dev.ergo.app

import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import org.json.JSONObject
import java.lang.ref.WeakReference

/* The one bridge between the console and the app. The page calls in when a voice call starts,
   ends or changes; the app talks back with a `ccnative` window event (see web-react
   src/shell/native.js). */
@CapacitorPlugin(name = "CCNative")
class CCNativePlugin : Plugin() {

    /* The local pages' buttons: ergo://setup opens the address screen ("Change address"),
       ergo://retry loads the console again from the saved address ("Try again"). */
    override fun shouldOverrideLoad(url: android.net.Uri): Boolean? {
        if (url.scheme != "ergo") return null
        when (url.host) {
            "setup" -> context.startActivity(android.content.Intent(context, SetupActivity::class.java))
            "retry" -> if (Daemon.isSet) bridge.webView.post { bridge.webView.loadUrl(Daemon.base + "/") }
        }
        return true
    }

    override fun load() {
        current = WeakReference(this)
    }

    /* Float this agent's face over every app: "Float" in the chat menu. */
    @PluginMethod
    fun float(call: PluginCall) {
        val id = call.getString("agent") ?: ""
        if (!android.provider.Settings.canDrawOverlays(context)) {
            call.resolve(JSObject().put("ok", false).put("why", "overlay"))
            return
        }
        CommandService.float(context, id, call.getString("name"))
        call.resolve(JSObject().put("ok", id.isNotBlank()))
    }

    /* The chime when an agent finishes its turn, for Config. */
    @PluginMethod
    fun getChime(call: PluginCall) {
        call.resolve(JSObject().put("chime", Notices.chime(context)))
    }

    @PluginMethod
    fun setChime(call: PluginCall) {
        Notices.setChime(context, call.getString("chime") ?: "")
        call.resolve(JSObject().put("chime", Notices.chime(context)))
    }

    /* Who is on the line, for the call notification: the agent that rang, or the front desk. */
    @PluginMethod
    fun callName(call: PluginCall) {
        CommandService.callName(call.getString("name") ?: "")
        call.resolve()
    }

    @PluginMethod
    fun callStarted(call: PluginCall) {
        CommandService.callStarted(context)
        call.resolve()
    }

    @PluginMethod
    fun callEnded(call: PluginCall) {
        CommandService.callEnded(context)
        call.resolve()
    }

    /* The call on the loudspeaker or the earpiece. Answers which one it is now. */
    @PluginMethod
    fun setSpeaker(call: PluginCall) {
        CallAudio.setSpeaker(context, call.getBoolean("on", false) == true)
        call.resolve(com.getcapacitor.JSObject().put("on", CallAudio.speaker))
    }

    @PluginMethod
    fun speakerInfo(call: PluginCall) {
        call.resolve(com.getcapacitor.JSObject().put("info", CallAudio.info(context).toString()))
    }

    @PluginMethod
    fun getSpeaker(call: PluginCall) {
        call.resolve(com.getcapacitor.JSObject().put("on", CallAudio.speaker))
    }

    @PluginMethod
    fun callState(call: PluginCall) {
        CommandService.callState(call.getBoolean("muted", false) == true, call.getBoolean("paused", false) == true)
        call.resolve()
    }

    /* The wheel's theme, from the Config page: "hud", "glass", "bubbles", "dial" or "glass2". */
    @PluginMethod
    fun setWheelTheme(call: PluginCall) {
        val theme = CommandService.setWheelTheme(context, call.getString("theme") ?: CommandService.THEME_HUD)
        call.resolve(JSObject().put("theme", theme))
    }

    @PluginMethod
    fun getWheelTheme(call: PluginCall) {
        call.resolve(JSObject().put("theme", CommandService.wheelTheme(context)))
    }

    /* Blur behind the menu themes, from the Config page: on or off. */
    @PluginMethod
    fun setDialBlur(call: PluginCall) {
        val on = CommandService.setDialBlur(context, call.getBoolean("on", false) == true)
        call.resolve(JSObject().put("on", on))
    }

    @PluginMethod
    fun getDialBlur(call: PluginCall) {
        call.resolve(JSObject().put("on", CommandService.dialBlur(context)))
    }

    /* The menu themes' words, from the Config page: "one" at a time, or "none". */
    @PluginMethod
    fun setMenuWords(call: PluginCall) {
        val w = CommandService.setMenuWords(context, call.getString("words") ?: CommandService.WORDS_ONE)
        call.resolve(JSObject().put("words", w))
    }

    @PluginMethod
    fun getMenuWords(call: PluginCall) {
        call.resolve(JSObject().put("words", CommandService.menuWordsName(context)))
    }

    /* Whether Recent follows the console's view, from the Config page. */
    @PluginMethod
    fun setRecentFollowsView(call: PluginCall) {
        val on = CommandService.setRecentFollowsView(context, call.getBoolean("on", false) == true)
        call.resolve(JSObject().put("on", on))
    }

    @PluginMethod
    fun getRecentFollowsView(call: PluginCall) {
        call.resolve(JSObject().put("on", CommandService.recentFollowsView(context)))
    }

    /* Whether Needs you follows the console's view, from the Config page. */
    @PluginMethod
    fun setNeedsFollowsView(call: PluginCall) {
        val on = CommandService.setNeedsFollowsView(context, call.getBoolean("on", false) == true)
        call.resolve(JSObject().put("on", on))
    }

    @PluginMethod
    fun getNeedsFollowsView(call: PluginCall) {
        call.resolve(JSObject().put("on", CommandService.needsFollowsView(context)))
    }

    /* The console's current view, told on start and at every change. */
    @PluginMethod
    fun setMenuView(call: PluginCall) {
        val v = CommandService.setMenuView(context, call.getString("view") ?: "all")
        call.resolve(JSObject().put("view", v))
    }

    /* What a full the front desk call screen needs, for the Config page, and a way to each switch. */
    @PluginMethod
    fun getCallSetup(call: PluginCall) {
        call.resolve(JSObject.fromJSONObject(DeskCall.setup(context)))
    }

    @PluginMethod
    fun setTelecom(call: PluginCall) {
        DeskTelecom.setEnabled(context, call.getBoolean("on", true) == true)
        call.resolve(JSObject().put("on", DeskTelecom.enabled(context)))
    }

    @PluginMethod
    fun openCallSetting(call: PluginCall) {
        DeskCall.openSetting(context, call.getString("which") ?: "")
        call.resolve()
    }

    /* Whether Ergo may read notifications, for the Config page's Read notifications row. */
    @PluginMethod
    fun getNotificationAccess(call: PluginCall) {
        call.resolve(JSObject().put("on", NoticeListener.enabled(context)).put("pending", Phone.pending(context)))
    }

    /* Opens the system's notification access settings, on Ergo's own page where Android allows. */
    @PluginMethod
    fun openNotificationAccess(call: PluginCall) {
        try {
            val detail = if (android.os.Build.VERSION.SDK_INT >= 30) {
                android.content.Intent(android.provider.Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS)
                    .putExtra(
                        android.provider.Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME,
                        android.content.ComponentName(context, NoticeListener::class.java).flattenToString(),
                    )
            } else null
            val list = android.content.Intent(android.provider.Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
            val go = (detail ?: list).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            try {
                context.startActivity(go)
            } catch (_: Throwable) {
                context.startActivity(list.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK))
            }
            call.resolve(JSObject().put("ok", true))
        } catch (e: Throwable) {
            call.resolve(JSObject().put("ok", false))
        }
    }

    companion object {
        private var current = WeakReference<CCNativePlugin>(null)

        /* One message to the page. `done` hears false when no console is loaded to take it,
           so the caller can load a deep link instead. */
        fun send(message: JSONObject, done: ((Boolean) -> Unit)? = null) {
            val web = current.get()?.bridge?.webView
            if (web == null) {
                done?.invoke(false)
                return
            }
            val js = "(function(){if(!window.ccNativeReady){return false}" +
                "window.dispatchEvent(new CustomEvent('ccnative',{detail:$message}));return true})()"
            web.post { web.evaluateJavascript(js) { result -> done?.invoke(result == "true") } }
        }
    }
}
