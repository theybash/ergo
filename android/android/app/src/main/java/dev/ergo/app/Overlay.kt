package dev.ergo.app

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.PixelFormat
import android.graphics.Point
import android.graphics.PointF
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowInsets
import android.view.WindowManager
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import kotlin.math.hypot
import kotlin.math.roundToInt

/* The bubble over other apps, the wheel it opens, and the trash target a drag shows. All three
   are plain windows the service adds to the window manager; nothing here knows about the console.

   The target's window is added before the bubble's, so the bubble always draws over it, and it
   stays GONE between drags. A window that is GONE is exempt from Android 12's rule against
   untrusted windows over other apps, so it never swallows a touch meant for the app below. */
class Overlay(private val service: CommandService) {

    private val ctx: Context = service
    private val wm = ctx.getSystemService(WindowManager::class.java)
    private val dp = ctx.resources.displayMetrics.density
    private val size = (56 * dp).roundToInt()
    private val margin = (6 * dp).roundToInt()
    private val slop = ViewConfiguration.get(ctx).scaledTouchSlop
    private val prefs = ctx.getSharedPreferences("bubble", Context.MODE_PRIVATE)

    private var bubble: BubbleView? = null
    private var wheel: WheelView? = null
    private var snapping: ValueAnimator? = null
    private var target: DismissView? = null
    private var magnet: ValueAnimator? = null
    private val magnetIn = 80 * dp
    private val magnetOut = 100 * dp

    private val targetParams = WindowManager.LayoutParams(
        WindowManager.LayoutParams.MATCH_PARENT, (200 * dp).roundToInt(),
        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
            WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
        PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.TOP or Gravity.START
        x = 0
        y = 0
        if (Build.VERSION.SDK_INT >= 28) {
            layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
        }
    }

    private val bubbleParams = WindowManager.LayoutParams(
        size, size,
        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
        PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.TOP or Gravity.START
        val screen = screen()
        x = prefs.getInt("x", screen.x - size - margin)
        y = prefs.getInt("y", screen.y / 3)
    }

    fun show(on: Boolean) {
        if (!on) {
            hide()
            return
        }
        if (bubble != null) return
        // The target first, so it is under the bubble.
        val trash = DismissView(ctx)
        wm.addView(trash, targetParams)
        target = trash
        val view = BubbleView(ctx)
        view.setOnTouchListener(Touch())
        wm.addView(view, bubbleParams)
        bubble = view
    }

    /* Takes down the bubble, any wheel and the target. Each step is on its own, so one that
       fails never leaves another window up. */
    fun hide() {
        main.removeCallbacks(idle)
        voice.cancel()
        voice.card.close()
        wheel?.let { drop(it) }
        leaving?.let { drop(it) }
        snapping?.cancel()
        magnet?.cancel()
        bubble?.let { safeRemove(it) }
        bubble = null
        target?.let { safeRemove(it) }
        target = null
    }

    private fun safeRemove(v: View) {
        try {
            if (v.parent != null) wm.removeView(v)
        } catch (e: Throwable) {
            Log.w(TAG, "a window would not go", e)
        }
    }

    /* The call started, ended, muted or paused: the bubble and an open wheel follow. */
    fun update() {
        bubble?.let {
            it.onCall = CommandService.callLive
            it.invalidate()
        }
        wheel?.let {
            try {
                it.choices = choices()
            } catch (e: Throwable) {
                fault(it, e)
            }
        }
    }

    /* The choices, in their order around the half ring, top to bottom. The thumb reaches the
       wheel from the bottom corner on either side, so the bottom of the ring is the easiest
       place to hit and the top the hardest: the choice used most sits at the bottom (Call, or
       Mute during a call), and the one to keep away from a stray thumb at the top (Fleet, or
       Hang up during a call). */
    private fun choices(): List<WheelView.Choice> = if (CommandService.callLive) listOf(
        WheelView.Choice("hangup", "Hang up", "Hang up", PHONE_OFF),
        WheelView.Choice("text", "Text chief of staff", "Text", MESSAGE),
        if (CommandService.paused) WheelView.Choice("pause", "Resume", "Resume", PLAY)
        else WheelView.Choice("pause", "Pause", "Pause", PAUSE),
        if (CommandService.muted) WheelView.Choice("mute", "Unmute", "Unmute", MIC)
        else WheelView.Choice("mute", "Mute", "Mute", MIC_OFF),
    ) else listOf(
        WheelView.Choice("fleet", "Fleet", "Fleet", GRID),
        WheelView.Choice("pa", "Personal assistant", "Assistant", PERSON),
        WheelView.Choice("text", "Text chief of staff", "Text", MESSAGE),
        WheelView.Choice("call", "Call chief of staff", "Call", PHONE),
    )

    /* The wheel on its way out, if one is. It is removed when its close has played, or by the
       watchdog a second after the close began, whichever comes first. */
    private var leaving: WheelView? = null
    private var blurListener: java.util.function.Consumer<Boolean>? = null
    private val main = Handler(Looper.getMainLooper())
    private val io = java.util.concurrent.Executors.newSingleThreadExecutor()

    /* The dial's voice note recorder. */
    private val voice = VoiceNote(service)

    /* No touch on the wheel for IDLE_MS closes it. A voice note being held down is not idle: the
       finger rests still while it records, and the recording ends itself at two minutes. */
    private val idle = object : Runnable {
        override fun run() {
            if (voice.recording && wheel != null) {
                main.postDelayed(this, IDLE_MS)
                return
            }
            closeWheel()
        }
    }

    /* Opens the wheel in a window over the bubble.

       Safety first, because this window takes every touch while it is up. The window stays
       inside the status bar, the navigation bar and the cutout, so the system keeps home, back
       and its gestures. A touch away from every button closes it at once, and so does back. No
       touch for eight seconds closes it. A close that has not removed the window after a second
       is cut short by the watchdog, and any error in the wheel's drawing, touch or animation
       removes the window at once. There is only ever one wheel window.

       On Android 12 and later the window blurs what is behind it, and dims it a little. When
       the system turns cross window blur off (battery saver, a weak GPU, the developer switch),
       the same window dims more and the wheel draws its glass denser. The wheel draws the
       bubble again in its place, so the real bubble is hidden under it while the wheel is up;
       every way the wheel goes brings the real bubble back. */
    private fun openWheel(): WheelView? {
        val from = bubble ?: return null
        // One window at most: while one is up or on its way out, opening does nothing.
        wheel?.let { return it }
        if (leaving != null) return null
        return try {
            addWheel(from)
        } catch (e: Throwable) {
            Log.e(TAG, "the wheel failed to open", e)
            (wheel ?: leaving)?.let { drop(it) }
            null
        }
    }

    private fun addWheel(from: View): WheelView {
        val at = IntArray(2)
        from.getLocationOnScreen(at)
        val anchor = PointF(at[0] + size / 2f, at[1] + size / 2f)
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            // Focusable, so back reaches the wheel. It holds the focus only while it is up, and
            // it never talks to the keyboard.
            WindowManager.LayoutParams.FLAG_ALT_FOCUSABLE_IM or
                WindowManager.LayoutParams.FLAG_DIM_BEHIND,
            PixelFormat.TRANSLUCENT,
        ).apply {
            // Inside the system bars and the cutout. Never over them.
            if (Build.VERSION.SDK_INT >= 30) {
                fitInsetsTypes = WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout()
            }
            if (Build.VERSION.SDK_INT >= 28) {
                layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_NEVER
            }
            if (Build.VERSION.SDK_INT >= 31) {
                flags = flags or WindowManager.LayoutParams.FLAG_BLUR_BEHIND
                blurBehindRadius = 0
            }
            dimAmount = 0f
            title = "ergo wheel"
        }
        // The theme is read at each open, so a change on the Config page shows on the next one.
        val theme = CommandService.wheelTheme(ctx)
        // Glass 2 is the Glass look with the Dial's menu and no dim; it draws as Glass.
        val glass2 = theme == CommandService.THEME_GLASS2
        val glass = theme == CommandService.THEME_GLASS || glass2
        val bubbles = theme == CommandService.THEME_BUBBLES
        val dial = theme == CommandService.THEME_DIAL
        // The menu themes, Dial and Glass 2, share their choices, their data and their voice note.
        val menu = dial || glass2
        // Their blur behind is the owner's choice in Config, off by default; they never dim.
        val dialBlur = menu && CommandService.dialBlur(ctx)
        var view: WheelView? = null
        var strength = 0f
        var sentStep = -1
        var sentBlur = false
        // The window's dim and blur follow the wheel's backdrop in eight steps, not every frame,
        // so the window manager is asked for a new layout a few times per open and close.
        fun backdrop(t: Float, force: Boolean = false) {
            val v = view ?: return
            strength = t
            val blur = v.blurOn
            val step = (t.coerceIn(0f, 1f) * BACKDROP_STEPS).roundToInt()
            if (!force && step == sentStep && blur == sentBlur) return
            sentStep = step
            sentBlur = blur
            val s = step / BACKDROP_STEPS.toFloat()
            // The dial has no dim and, by default, no blur: the screen behind stays as it is.
            // With its blur on, the Dial keeps a dim too faint to see: the blur rides on the dim
            // layer, which a dim of exactly 0 drops.
            params.dimAmount = if (menu) (if (dialBlur && blur) s * DIAL_DIM else 0f)
            else s * (if (blur) DIM_BLUR else DIM_FLAT) * (if (glass) GLASS_DIM else 1f)
            if (Build.VERSION.SDK_INT >= 31) {
                params.blurBehindRadius = if (blur && (!menu || dialBlur)) (s * BLUR_DP * dp).roundToInt() else 0
            }
            try {
                if (v.parent != null) wm.updateViewLayout(v, params)
            } catch (e: Throwable) {
                fault(v, e)
            }
        }
        val made = WheelView(
            ctx, if (menu) dialChoices().filter { !glass2 || it.id != "type" } else choices(), anchor, CommandService.callLive, CommandService.callSince,
            glass, bubbles, dial, if (menu) voice else null, glass2, CommandService.menuWords(ctx),
            onDone = { pick -> pickChoice(pick) },
            onAway = { closeWheel(instant = true) },
            onBackdrop = { backdrop(it) },
            onFirstFrame = { if (wheel === view || leaving === view) bubble?.alpha = 0f },
            onTouched = { restartIdle() },
            onFault = { e -> view?.let { fault(it, e) } },
        )
        view = made
        if (Build.VERSION.SDK_INT >= 31) {
            try {
                made.blurOn = wm.isCrossWindowBlurEnabled
                // Blur off is the same window with a heavier dim, never a different window.
                val listener = java.util.function.Consumer<Boolean> { on ->
                    try {
                        if (wheel === made || leaving === made) {
                            made.blurOn = on
                            backdrop(strength, force = true)
                        }
                    } catch (e: Throwable) {
                        fault(made, e)
                    }
                }
                blurListener?.let { wm.removeCrossWindowBlurEnabledListener(it) }
                blurListener = null
                wm.addCrossWindowBlurEnabledListener(ctx.mainExecutor, listener)
                blurListener = listener
            } catch (e: Throwable) {
                Log.w(TAG, "no blur listener, the wheel dims without blur", e)
                made.blurOn = false
            }
        }
        wheel = made
        wm.addView(made, params)
        restartIdle()
        fetchAsks(made)
        if (menu) fetchState(made)
        return made
    }

    /* Who is waiting on the owner, read off the main thread. The wheel never waits for it: the
       bars come in when the answer does, and only if this wheel is still the open one. */
    private fun fetchAsks(view: WheelView) {
        try {
            io.execute {
                val asks = try {
                    Asks.fetch()
                } catch (e: Throwable) {
                    Log.i(TAG, "no pending asks: $e")
                    null
                }
                if (asks != null) main.post {
                    if (wheel === view) {
                        try {
                            view.asks = asks
                        } catch (e: Throwable) {
                            fault(view, e)
                        }
                    }
                }
            }
        } catch (e: Throwable) {
            Log.w(TAG, "could not ask who is waiting", e)
        }
    }

    /* The fleet's state for the dial, read off the main thread like the asks: the untold count on
       the front desk, and the agents by their last activity for Recent. */
    private fun fetchState(view: WheelView) {
        try {
            io.execute {
                val state = try {
                    val conn = java.net.URL(Daemon.state).openConnection() as java.net.HttpURLConnection
                    try {
                        conn.connectTimeout = 1500
                        conn.readTimeout = 4000
                        org.json.JSONObject(conn.inputStream.use { it.readBytes() }.decodeToString())
                    } finally {
                        conn.disconnect()
                    }
                } catch (e: Throwable) {
                    Log.i(TAG, "no state for the dial: $e")
                    null
                } ?: return@execute
                val untold = state.optJSONObject("inbox")?.optInt("untold", 0) ?: 0
                // With Recent following the view, only the agents that show in the console's view
                // count, by the console's own rule: all shows everyone, and an agent with no tags
                // shows in every view. Needs you follows the view only with its own switch on.
                val follow = CommandService.recentFollowsView(ctx)
                val needsFollow = CommandService.needsFollowsView(ctx)
                val inView = CommandService.menuView(ctx)
                val notInView = HashSet<String>()
                val agents = state.optJSONArray("agents")
                val byLast = ArrayList<Pair<String, Double>>()
                if (agents != null) for (i in 0 until agents.length()) {
                    val a = agents.optJSONObject(i) ?: continue
                    if (a.optBoolean("hidden")) continue
                    val id = a.optString("id").takeIf { it.isNotBlank() } ?: continue
                    var shows = true
                    if (inView != "all") {
                        val tags = a.optJSONArray("views")
                        if (tags != null && tags.length() > 0) {
                            shows = false
                            for (k in 0 until tags.length()) if (tags.optString(k) == inView) shows = true
                        }
                    }
                    if (!shows && needsFollow) notInView.add(id)
                    if (!shows && follow) continue
                    byLast.add(id to a.optDouble("last_activity", 0.0).let { if (it.isNaN()) 0.0 else it })
                }
                val recent = byLast.sortedByDescending { it.second }.map { it.first }
                main.post {
                    if (wheel === view) {
                        try {
                            view.dialState(untold, recent, notInView)
                        } catch (e: Throwable) {
                            fault(view, e)
                        }
                    }
                }
            }
        } catch (e: Throwable) {
            Log.w(TAG, "could not read the state", e)
        }
    }

    /* The dial's six choices, top to bottom, the front desk by the thumb. During a call the call's own
       choices, as in every theme. */
    private fun dialChoices(): List<WheelView.Choice> = if (CommandService.callLive) choices() else listOf(
        WheelView.Choice("fleet", "Fleet", "Fleet", GRID),
        WheelView.Choice("recent", "Recent agents", "Recent", HISTORY),
        WheelView.Choice("voicenote", "Voice note to personal assistant", "Voice note", MIC),
        WheelView.Choice("type", "Type to personal assistant", "Type", KEYBOARD),
        WheelView.Choice("needs", "Needs you", "Needs you", BELL),
        WheelView.Choice("call", "Call the front desk", "The front desk", PHONE),
    )

    /* What a pick does. A voice note sends what was recorded; Type opens the personal
       assistant's chat; Needs you and Recent, released on themselves, only close. */
    private fun pickChoice(pick: WheelView.Choice?) {
        when (pick?.id) {
            "voicenote" -> {
                voice.anchor = android.graphics.Point(bubbleParams.x + size / 2, bubbleParams.y + size / 2)
                voice.finish()
                closeWheel(instant = true)
            }
            // Released on Needs you or Recent: the second ring stays open until the next tap. The
            // idle close still runs, so a pinned ring cannot hang.
            "needs", "recent" -> if (wheel?.pin() == true) restartIdle() else closeWheel(instant = true)
            "type" -> {
                closeWheel(instant = true)
                service.act("chat:personal-assistant")
            }
            else -> {
                closeWheel(instant = opensApp(pick))
                if (pick != null) service.act(pick.id)
            }
        }
    }

    /* A pick that opens the app takes the wheel away at once, before the app starts: the app's
       start holds the main thread, where the close animation and the watchdog also run, and the
       app covers the screen anyway. Mute, pause and hang up keep the close animation. */
    private fun opensApp(pick: WheelView.Choice?): Boolean =
        pick != null && pick.id !in setOf("mute", "pause", "hangup")

    private fun restartIdle() {
        main.removeCallbacks(idle)
        if (wheel != null) main.postDelayed(idle, IDLE_MS)
    }

    /* Closes the wheel. Instant takes the window away now. Otherwise the close plays and removes
       it, and the watchdog removes it anyway after WATCHDOG_MS. */
    private fun closeWheel(instant: Boolean = false) {
        main.removeCallbacks(idle)
        if (instant) {
            (wheel ?: leaving)?.let { drop(it) }
            leaving?.let { drop(it) }
            return
        }
        val view = wheel ?: return
        wheel = null
        leaving?.let { drop(it) }
        leaving = view
        main.postDelayed({
            if (leaving === view || view.parent != null) {
                Log.w(TAG, "the wheel's close did not finish in time, removed by the watchdog")
                drop(view)
            }
        }, WATCHDOG_MS)
        try {
            view.close { drop(view) }
        } catch (e: Throwable) {
            fault(view, e)
        }
    }

    /* Something in the wheel threw. It goes at once, and the bubble comes back. */
    private fun fault(view: WheelView, e: Throwable) {
        Log.e(TAG, "the wheel failed, removing its window", e)
        if (wheel === view) wheel = null
        if (leaving === view) leaving = null
        // Posted, so the window never goes in the middle of its own draw or touch.
        main.post { drop(view) }
        showBubble()
    }

    /* Takes a wheel's window away at once, and shows the real bubble again. It is safe to call
       more than once, and it never throws. */
    private fun drop(view: WheelView) {
        // A voice note never keeps recording once its window goes.
        voice.cancel()
        if (leaving === view) leaving = null
        if (wheel === view) wheel = null
        if (wheel == null && leaving == null) {
            main.removeCallbacks(idle)
            showBubble()
            if (Build.VERSION.SDK_INT >= 31) {
                try {
                    blurListener?.let { wm.removeCrossWindowBlurEnabledListener(it) }
                } catch (e: Throwable) {
                    Log.w(TAG, "the blur listener would not go", e)
                }
            }
            blurListener = null
        }
        view.stop()
        // The parent is set as soon as addView returns, before the view is attached, so this
        // also catches a window that has not drawn yet.
        if (view.parent != null) {
            try {
                wm.removeView(view)
            } catch (e: Throwable) {
                Log.w(TAG, "the wheel window would not go", e)
                try {
                    wm.removeViewImmediate(view)
                } catch (_: Throwable) {
                }
            }
        }
    }

    private fun showBubble() {
        bubble?.let {
            it.animate().cancel()
            it.alpha = 1f
            it.visibility = View.VISIBLE
        }
    }

    /* Debug builds only: makes the open wheel throw, to prove it goes and the phone stays free. */
    fun testFault(where: String) {
        if (!BuildConfig.DEBUG) return
        WheelView.testFault = where
        (wheel ?: leaving)?.poke()
    }

    /* After a drag the bubble slides to the nearer side edge and stays there. */
    private fun snap() {
        val view = bubble ?: return
        val screen = screen()
        val target = if (bubbleParams.x + size / 2 < screen.x / 2) margin else screen.x - size - margin
        bubbleParams.y = bubbleParams.y.coerceIn(0, (screen.y - size).coerceAtLeast(0))
        snapping?.cancel()
        snapping = ValueAnimator.ofInt(bubbleParams.x, target).apply {
            duration = 180
            addUpdateListener {
                if (bubble === view) {
                    bubbleParams.x = it.animatedValue as Int
                    wm.updateViewLayout(view, bubbleParams)
                }
            }
            start()
        }
        prefs.edit {
            putInt("x", target)
            putInt("y", bubbleParams.y)
        }
    }

    /* Where the bubble sits, moved there over a short slide. The magnet uses it to pull the
       bubble onto the target. */
    private fun glide(toX: Int, toY: Int) {
        val view = bubble ?: return
        magnet?.cancel()
        val fromX = bubbleParams.x
        val fromY = bubbleParams.y
        magnet = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 110
            addUpdateListener {
                val t = it.animatedValue as Float
                if (bubble === view) {
                    bubbleParams.x = (fromX + (toX - fromX) * t).roundToInt()
                    bubbleParams.y = (fromY + (toY - fromY) * t).roundToInt()
                    wm.updateViewLayout(view, bubbleParams)
                }
            }
            start()
        }
    }

    /* Dropped on the target. The bubble shrinks into it and goes, the same as Hide bubble. Its
       place is put back to where the drag began, so it returns at its old edge. */
    private fun dismiss(homeX: Int, homeY: Int) {
        val view = bubble ?: return
        magnet?.cancel()
        view.animate().scaleX(0f).scaleY(0f).alpha(0f).setDuration(160).withEndAction {
            target?.disappear()
            bubbleParams.x = homeX
            bubbleParams.y = homeY
            view.postDelayed({ service.hideBubble() }, 170)
        }.start()
    }

    /* The top of the screen the status bar and any cutout take, on screen. */
    private fun topInset(v: View): Int {
        val insets = ViewCompat.getRootWindowInsets(v) ?: return (24 * dp).roundToInt()
        return insets.getInsets(WindowInsetsCompat.Type.statusBars() or WindowInsetsCompat.Type.displayCutout()).top
    }

    private fun screen(): Point {
        if (Build.VERSION.SDK_INT >= 30) {
            val b = wm.currentWindowMetrics.bounds
            return Point(b.width(), b.height())
        }
        val p = Point()
        @Suppress("DEPRECATION")
        wm.defaultDisplay.getRealSize(p)
        return p
    }

    /* One touch on the bubble becomes a tap, a drag, or a hold.
       A tap opens the wheel for a second tap. A move past the touch slop before the hold time is
       a drag, and only a drag shows the trash target. Staying still past the hold time opens the
       wheel under the finger; sliding onto a button aims it, releasing there picks it, releasing
       anywhere else closes the wheel, and the target never shows.

       During a drag the target works like a magnet. Within magnetIn of its centre the bubble
       jumps onto it, the target grows and the phone ticks once. It lets go only past magnetOut,
       a little further, so a finger resting on the edge does not flicker in and out. */
    private inner class Touch : View.OnTouchListener {
        private var mode = IDLE
        private var downX = 0f
        private var downY = 0f
        private var startX = 0
        private var startY = 0
        private var offX = 0
        private var offY = 0
        private var aimX = 0f
        private var aimY = 0f
        private var stuck = false
        private val hold = Runnable {
            if (mode == PENDING) {
                mode = HOLD
                // The finger starts on the bubble, which now closes the wheel.
                openWheel()?.aimAt(downX, downY)
            }
        }

        @SuppressLint("ClickableViewAccessibility")
        override fun onTouch(v: View, e: MotionEvent): Boolean {
            val dx = e.rawX - downX
            val dy = e.rawY - downY
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = e.rawX
                    downY = e.rawY
                    startX = bubbleParams.x
                    startY = bubbleParams.y
                    snapping?.cancel()
                    mode = PENDING
                    v.postDelayed(hold, HOLD_MS)
                }
                MotionEvent.ACTION_MOVE -> {
                    if (mode == PENDING && hypot(dx, dy) > slop) {
                        v.removeCallbacks(hold)
                        mode = DRAG
                        beginDrag(v)
                    }
                    if (mode == DRAG) drag(v, startX + dx.roundToInt(), startY + dy.roundToInt())
                    if (mode == HOLD) wheel?.let {
                        restartIdle()
                        it.aimAt(e.rawX, e.rawY)
                    }
                }
                MotionEvent.ACTION_UP -> {
                    v.removeCallbacks(hold)
                    when (mode) {
                        PENDING -> {
                            v.performClick()
                            openWheel()
                        }
                        DRAG -> if (stuck) {
                            stuck = false
                            dismiss(startX, startY)
                        } else {
                            target?.disappear()
                            snap()
                        }
                        HOLD -> {
                            // On a button picks it. On the bubble, or on nothing, closes.
                            pickChoice(wheel?.aimedChoice)
                        }
                    }
                    mode = IDLE
                }
                MotionEvent.ACTION_CANCEL -> {
                    v.removeCallbacks(hold)
                    if (mode == HOLD) closeWheel()
                    if (mode == DRAG) {
                        // A cancel is never a drop.
                        stuck = false
                        target?.stick(false)
                        target?.disappear()
                        snap()
                    }
                    mode = IDLE
                }
            }
            return true
        }

        /* The drag began: where the bubble window is on screen, and where the target goes. */
        private fun beginDrag(v: View) {
            val at = IntArray(2)
            v.getLocationOnScreen(at)
            offX = at[0] - bubbleParams.x
            offY = at[1] - bubbleParams.y
            aimX = screen().x / 2f
            aimY = topInset(v) + 76 * dp
            stuck = false
            target?.let {
                it.cy = aimY
                it.appear()
            }
        }

        /* The finger wants the bubble at x, y. The magnet decides whether it gets there. */
        private fun drag(v: View, x: Int, y: Int) {
            val cx = x + offX + size / 2f
            val cy = y + offY + size / 2f
            val d = hypot(cx - aimX, cy - aimY)
            if (!stuck && d < magnetIn) {
                stuck = true
                target?.stick(true)
                v.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                glide((aimX - offX - size / 2f).roundToInt(), (aimY - offY - size / 2f).roundToInt())
                return
            }
            if (stuck && d <= magnetOut) return
            if (stuck) {
                stuck = false
                target?.stick(false)
                magnet?.cancel()
            }
            bubbleParams.x = x
            bubbleParams.y = y
            wm.updateViewLayout(v, bubbleParams)
        }
    }

    /* The bubble itself: a stone disc with the house mark. During a call a thin off white ring
       says the call is live, quietly. */
    @SuppressLint("ViewConstructor")
    private class BubbleView(context: Context) : View(context) {
        var onCall = false
        private val dp = resources.displayMetrics.density
        private val house = ContextCompat.getDrawable(context, R.drawable.bubble_house)
        private val disc = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = WheelView.STONE_RAISED }
        private val edge = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = dp
            color = WheelView.LINE
        }
        private val live = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = 1.5f * dp
            color = WheelView.TEXT
            alpha = 150
        }

        init {
            contentDescription = "Ergo bubble"
        }

        // A tap opens the wheel; the touch listener calls this so accessibility sees the click.
        override fun performClick(): Boolean = super.performClick()

        override fun onDraw(canvas: Canvas) {
            val c = width / 2f
            canvas.drawCircle(c, c, c - dp, disc)
            canvas.drawCircle(c, c, c - dp, edge)
            if (onCall) canvas.drawCircle(c, c, c - 4 * dp, live)
            house?.let {
                val half = (15 * dp).roundToInt()
                it.setBounds(c.roundToInt() - half, c.roundToInt() - half, c.roundToInt() + half, c.roundToInt() + half)
                it.draw(canvas)
            }
        }
    }

    private companion object {
        const val IDLE = 0
        const val PENDING = 1
        const val DRAG = 2
        const val HOLD = 3
        const val HOLD_MS = 220L
        const val TAG = "CCOverlay"

        /* The wheel's safety times: no touch for IDLE_MS closes it, and a close still on screen
           after WATCHDOG_MS is removed outright. */
        const val IDLE_MS = 8000L
        const val WATCHDOG_MS = 1000L
        const val BACKDROP_STEPS = 8

        /* The backdrop: how far behind the wheel is blurred, and how much it is dimmed with the
           blur and without it. */
        const val BLUR_DP = 15f
        const val DIM_BLUR = 0.21f
        /* The Glass theme is lighter: the home screen shows through softly. */
        const val GLASS_DIM = 0.55f
        /* The Dial with its blur on: a dim too faint to see, only there to carry the blur. */
        const val DIAL_DIM = 0.01f
        const val DIM_FLAT = 0.4f

        /* The wheel's glyphs, drawn for it on a 24 unit square as thin lines. */
        const val PHONE = "M6.8 3.8h2.4l1.4 4.1-1.9 1.4a11.5 11.5 0 0 0 6 6l1.4-1.9 4.1 1.4v2.4a1.8 1.8 0 0 1-1.8 1.8" +
            "A15.8 15.8 0 0 1 5 5.6a1.8 1.8 0 0 1 1.8-1.8z"
        const val PHONE_OFF = "M3.2 13.6c4.9-4.3 12.7-4.3 17.6 0l-1.9 2.9-3.7-1.1v-2.5a10.5 10.5 0 0 0-6.4 0v2.5" +
            "l-3.7 1.1z"
        const val MESSAGE = "M4.5 5.5h15v10h-8l-4.5 3.5v-3.5h-2.5zM8.5 9.5h7M8.5 12h4.5"
        const val PERSON = "M12 3.5l1.9 6.6 6.6 1.9-6.6 1.9-1.9 6.6-1.9-6.6-6.6-1.9 6.6-1.9z"
        const val HISTORY = "M4.5 12a7.5 7.5 0 1 0 2.2-5.3M4.5 5v4h4M12 8.5V12l2.5 2"
        const val BELL = "M6.5 10a5.5 5.5 0 0 1 11 0v4l1.5 2.5h-14L6.5 14zM10 19.5a2 2 0 0 0 4 0"
        const val KEYBOARD = "M3.5 7h17v10h-17zM7 10.5h.01M10 10.5h.01M13 10.5h.01M16 10.5h.01M8 14h8"
        const val GRID = "M7.5 4.5a3 3 0 1 1 0 6a3 3 0 1 1 0-6zM16.5 4.5a3 3 0 1 1 0 6a3 3 0 1 1 0-6z" +
            "M7.5 13.5a3 3 0 1 1 0 6a3 3 0 1 1 0-6zM16.5 13.5a3 3 0 1 1 0 6a3 3 0 1 1 0-6z"
        const val MIC = "M9.5 5.5a2.5 2.5 0 0 1 5 0v5.5a2.5 2.5 0 0 1-5 0zM6.2 11a5.8 5.8 0 0 0 11.6 0M12 16.8v3.4M9.2 20.2h5.6"
        const val MIC_OFF = MIC + "M4.5 4.5l15 15"
        const val PAUSE = "M9 5.5v13M15 5.5v13"
        const val PLAY = "M8.5 5.8v12.4l9.6-6.2z"
    }
}
