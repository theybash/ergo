package dev.ergo.app

import android.app.Activity
import android.os.Bundle

/* The tap on a held alarm's notification. A tap is the owner's own act, so Android lets the app
   open the Clock app from here even when nothing else of Ergo is on screen. Shows nothing: it
   sets the one alarm and closes. */
class AlarmTap : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        intent?.getStringExtra(EXTRA_ID)?.let { Phone.release(this, it) }
        finish()
    }

    companion object {
        const val EXTRA_ID = "held_alarm_id"
    }
}
