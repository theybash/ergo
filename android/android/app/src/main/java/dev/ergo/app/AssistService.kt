package dev.ergo.app

import android.content.Intent
import android.os.Bundle
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession.SHOW_WITH_ASSIST
import android.service.voice.VoiceInteractionSession.SHOW_WITH_SCREENSHOT
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.speech.RecognitionService
import android.speech.SpeechRecognizer

/* Assistant mode. Ergo as the phone's digital assistant (Settings > Apps > Default apps >
   Digital assistant app): holding the power key, or the assistant gesture, opens Ergo's bar
   over whatever is on screen, the lock screen included. The bar itself is AssistBar.

   Android needs three pieces for an assistant: this service, which only has to exist; the session
   service, which makes one bar per press; and a speech recognizer, which Android insists every
   assistant declares. Ours is a stub that says no: the bar dictates through the phone's own
   recognizer, chosen by name, so nothing in Ergo depends on this one. */
class AssistService : VoiceInteractionService() {

    override fun onReady() {
        super.onReady()
        running = this
    }

    override fun onShutdown() {
        if (running === this) running = null
        super.onShutdown()
    }

    /* The lock screen's assistant shortcut, on phones that offer one. The same bar. */
    override fun onLaunchVoiceAssistFromKeyguard() {
        showSession(Bundle(), 0)
    }

    companion object {
        const val ARG_QUOTE = "quote"
        const val ARG_SHARED = "shared"
        @Volatile private var running: AssistService? = null

        /* The other ways in (the text selection menu, the Quick Settings tile, the share
           sheet) open the same bar, with what they carry. False when Ergo is not the phone's
           digital assistant, so the caller can say how to make it one. */
        fun open(args: Bundle): Boolean {
            val svc = running ?: return false
            return try {
                svc.showSession(args, SHOW_WITH_ASSIST or SHOW_WITH_SCREENSHOT)
                true
            } catch (_: Throwable) {
                false
            }
        }
    }
}

class AssistSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession = AssistBar(this)
}

class AssistRecognizer : RecognitionService() {
    override fun onStartListening(recognizerIntent: Intent?, listener: Callback?) {
        try {
            listener?.error(SpeechRecognizer.ERROR_CLIENT)
        } catch (_: Throwable) {
        }
    }

    override fun onCancel(listener: Callback?) {}

    override fun onStopListening(listener: Callback?) {}
}
