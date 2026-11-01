package dev.ergo.app

import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.telecom.DisconnectCause
import android.util.Log
import androidx.core.telecom.CallAttributesCompat
import androidx.core.telecom.CallControlScope
import androidx.core.telecom.CallEndpointCompat
import androidx.core.telecom.CallsManager
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/* the front desk as a real Android call, the way WhatsApp and Telegram do it: Ergo registers with the
   phone system as a self-managed calling app (Core-Telecom), and every the front desk call, ringing in for
   an urgent item or started from the orb, is a call the system knows. So Bluetooth and the watch
   can answer and hang up, another incoming call puts the front desk on hold instead of talking over him,
   and audio routing follows the call.

   The audio itself is still the WebRTC call in the page. This only tells Android a call is there
   and carries its decisions back: hold pauses the front desk, resume resumes him, a hang up from anywhere
   ends him. If registering fails on a phone, all of this stays off and calls work as before. */
object DeskTelecom {

    private const val TAG = "CCTelecom"
    private val io = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val main = Handler(Looper.getMainLooper())
    private var manager: CallsManager? = null
    private var control: CallControlScope? = null
    /* Where the call can be heard, and where it is, as Telecom reports them. While a call is
       registered here, Telecom owns the audio route: the Speaker button asks it, because a route
       set straight on the AudioManager is put back by Telecom at once. */
    @Volatile private var endpoints: List<CallEndpointCompat> = emptyList()
    @Volatile var endpoint: CallEndpointCompat? = null
        private set
    private var ended: CompletableDeferred<Unit>? = null
    private var ringing = false
    private var answered = false

    /* A switch on the Config page. Telegram keeps the phone system off on phones that are not
       Google's, because some implement it badly; Signal decides per model. Here the owner decides. */
    fun enabled(ctx: Context) = ctx.getSharedPreferences("desk_calls", Context.MODE_PRIVATE).getBoolean("telecom", true)

    fun setEnabled(ctx: Context, on: Boolean) {
        ctx.getSharedPreferences("desk_calls", Context.MODE_PRIVATE).edit().putBoolean("telecom", on).apply()
    }

    private fun ready(ctx: Context): CallsManager? {
        if (!enabled(ctx)) return null
        manager?.let { return it }
        return try {
            CallsManager(ctx.applicationContext).also {
                it.registerAppWithTelecom(CallsManager.CAPABILITY_BASELINE)
                manager = it
            }
        } catch (e: Throwable) {
            Log.i(TAG, "the phone system would not take Ergo as a calling app: $e")
            null
        }
    }

    // The caller the phone shows in its own call screen and call log: the agent that rang, not
    // "The front desk" (The owner, 25 October 2026: "it's not the front desk that's calling. It's the agent").
    private var caller = "Ergo"

    private fun attrs(direction: Int) = CallAttributesCompat(
        displayName = caller,
        address = Uri.parse("ergo:" + caller.lowercase().replace(' ', '-')),
        direction = direction,
        callType = CallAttributesCompat.CALL_TYPE_AUDIO_CALL,
    )

    private fun begin(ctx: Context, direction: Int, then: suspend CallControlScope.() -> Unit) {
        val m = ready(ctx) ?: return
        if (control != null) return
        val done = CompletableDeferred<Unit>()
        ended = done
        io.launch {
            try {
                m.addCall(attrs(direction),
                    onAnswer = { _ -> main.post { answerFromSystem(ctx) } },
                    onDisconnect = { _ -> main.post { hungUpBySystem(ctx); forget() } },
                    onSetActive = { main.post { resume() } },
                    onSetInactive = { main.post { hold() } },
                ) {
                    // The scope is the call's own; it lives until the call is disconnected.
                    control = this
                    launch { availableEndpoints.collect { endpoints = it } }
                    launch { currentCallEndpoint.collect { endpoint = it } }
                    launch { then() }
                }
            } catch (e: Throwable) {
                Log.i(TAG, "the call did not register: $e")
                forget()
            }
        }
    }

    /* An urgent ring: the system rings with Ergo's call screen and notification on top. */
    fun incoming(ctx: Context, name: String) {
        caller = name.ifBlank { "Ergo" }
        ringing = true
        answered = false
        begin(ctx, CallAttributesCompat.DIRECTION_INCOMING) {}
        // Unanswered after 45 seconds, it is a missed call.
        main.postDelayed({ if (ringing && !answered) { DeskCall.stop(ctx); end(DisconnectCause.MISSED) } }, 45_000)
    }

    /* The page started a the front desk call: the answer to a ring, or a call from the orb. */
    fun started(ctx: Context) {
        DeskCall.stop(ctx)
        val c = control
        if (c != null && ringing) {
            answered = true
            ringing = false
            io.launch { try { c.answer(CallAttributesCompat.CALL_TYPE_AUDIO_CALL) } catch (_: Throwable) {} }
            return
        }
        if (c != null) return
        caller = "Ergo"
        begin(ctx, CallAttributesCompat.DIRECTION_OUTGOING) {
            try { setActive() } catch (_: Throwable) {}
        }
    }

    /* The loudspeaker, or back to the earpiece (or a headset when one is on). False when no call
       is registered with Telecom, and then the caller sets the route itself. */
    fun route(speaker: Boolean): Boolean {
        val c = control ?: return false
        val order = if (speaker) listOf(CallEndpointCompat.TYPE_SPEAKER)
            else listOf(CallEndpointCompat.TYPE_BLUETOOTH, CallEndpointCompat.TYPE_WIRED_HEADSET, CallEndpointCompat.TYPE_EARPIECE)
        val ep = order.firstNotNullOfOrNull { t -> endpoints.firstOrNull { it.type == t } } ?: return false
        if (endpoint?.type == ep.type) return true
        io.launch {
            try { Log.i(TAG, "route to ${ep.name}: " + c.requestEndpointChange(ep)) } catch (e: Throwable) { Log.i(TAG, "route refused: $e") }
        }
        return true
    }

    fun endpointNames(): List<String> = endpoints.map { it.name.toString() + ":" + it.type }

    /* The page ended the call. */
    fun stopped() = end(DisconnectCause.LOCAL)

    /* Declined from the call screen or the notification. */
    fun declined() = end(DisconnectCause.REJECTED)

    private fun end(cause: Int) {
        val c = control ?: return
        ringing = false
        io.launch {
            try { c.disconnect(DisconnectCause(cause)) } catch (_: Throwable) {}
            ended?.complete(Unit)
            forget()
        }
    }

    private fun forget() {
        control = null
        ringing = false
        answered = false
    }

    // -- what the system decides ------------------------------------------------------

    private fun answerFromSystem(ctx: Context) {
        // Answered from a headset or watch: open the front desk as the call screen's Answer does.
        if (answered) return
        answered = true
        ringing = false
        DeskCall.stop(ctx)
        ctx.startActivity(android.content.Intent(ctx, MainActivity::class.java)
            .putExtra(MainActivity.EXTRA_GO, "voice")
            .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP))
    }

    private fun hungUpBySystem(ctx: Context) {
        DeskCall.stop(ctx)
        ended?.complete(Unit)
        if (CommandService.callLive) CommandService.hangUp(ctx)
    }

    private fun hold() {
        if (CommandService.callLive && !CommandService.paused) CCNativePlugin.send(org.json.JSONObject().put("do", "pause"))
    }

    private fun resume() {
        if (CommandService.callLive && CommandService.paused) CCNativePlugin.send(org.json.JSONObject().put("do", "pause"))
    }
}
