package dev.ergo.app

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PointF
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.os.Build
import android.provider.Settings
import android.text.TextPaint
import android.text.TextUtils
import android.util.Log
import android.util.TypedValue
import android.view.Choreographer
import android.view.HapticFeedbackConstants
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.window.OnBackInvokedCallback
import android.window.OnBackInvokedDispatcher
import androidx.core.content.res.ResourcesCompat
import androidx.core.graphics.PathParser
import androidx.core.graphics.withClip
import androidx.core.graphics.withRotation
import androidx.core.graphics.withScale
import androidx.core.graphics.withTranslation
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import kotlin.math.abs
import kotlin.math.asin
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/* The wheel: four large plain choices on a half ring of lit glass, docked on the screen edge the
   bubble sits on, and beyond it a fan of bars for the agents that are waiting on the owner.

   The first layer grows out of that edge around the hub. Outside, a thin dial with tick marks.
   In the middle, a band of frosted glass lit from within, carrying the choices: a glyph and its
   name under it. Inside, two thin broken arcs close around the hub. The hub is glass with a red
   X, and a tap on it closes the wheel.

   The second layer shows only when agents wait on the owner: one bar per agent, fanned out
   beyond the dial over the open side, each a glass pill with a red disc at its inner end and the
   agent's name and what it wants along it. Up to four bars, then a "+N more" bar. A bar opens
   that agent's chat; "+N more" opens the fleet. The bars never touch the first layer, each
   other, or the screen's edges.

   Near the top or the bottom the rings' centre slides along the edge so they stay whole; the
   hub slides with it.

   The active choice's glyph and name turn full white and a short green arc glides along the
   band on a spring to sit behind it, with a soft light around it. An active bar lights its
   edge green.

   Two ways to pick. Held from the bubble, the finger slides onto a choice or a bar and the
   release picks it (the bubble's touch drives aimAt()). Tapped open, a tap on one picks it
   (this view's own touch). A release on the hub or outside everything picks nothing and closes
   the wheel.

   Any touch that lands outside the rings, the bars and the hub closes the wheel at once, and so
   does back. Any error in drawing, touch or animation stops the view and reports to onFault,
   which removes the window.

   The choices sit on one Ring, so a ring of sub options could come later as one more Ring.

   On open the rings draw on as quick staggered sweeps, outer first, the ticks fade in and the
   glyphs settle; the bars slide out when the answer about who is waiting arrives. On close it
   all sweeps back into the edge. At rest nothing moves, except that during a call the inner
   arcs turn slowly as a live signal. The service's window holds the blur and the dim; this view
   asks for them through onBackdrop, with a strength from 0 to 1. */
@SuppressLint("ViewConstructor")
class WheelView(
    context: Context,
    choices: List<Choice>,
    private val anchor: PointF,
    private val onCall: Boolean,
    @Suppress("unused") private val callSince: Long,
    /* The Glass theme instead of the HUD: thick frosted segments around a glass hub. */
    private val glass: Boolean,
    /* The Bubbles theme: round frosted discs floating in an arc around a black hub. */
    private val bubbles: Boolean,
    /* The Dial theme: a slim ring of six wedges on the edge, a red X floating at its centre. */
    private val dialTheme: Boolean,
    /* The Dial's voice note recorder, or null in the other themes. */
    private val voice: VoiceNote?,
    /* Glass 2: the Glass look with the Dial's menu: its choices, the agents as pills only while
       Needs you or Recent is reached or pinned, and no ground behind. */
    private val glass2: Boolean,
    /* The menu themes' words: true, one at a time under the finger; false, none ever. */
    private val menuWords: Boolean,
    private val onDone: (Choice?) -> Unit,
    private val onAway: () -> Unit,
    private val onBackdrop: (Float) -> Unit,
    private val onFirstFrame: () -> Unit,
    private val onTouched: () -> Unit,
    private val onFault: (Throwable) -> Unit,
) : View(context) {

    /* One choice. The label is the full name, for accessibility; the short one is the name the
       wheel shows. The icon is SVG path data on a 24 unit square, drawn as a thin line. */
    class Choice(val id: String, val label: String, val short: String, icon: String) {
        val path: Path = PathParser.createPathFromPathData(icon)
    }

    var choices: List<Choice> = choices
        set(value) {
            val old = aimedChoice?.id
            field = value
            if (value.size != settle.size) {
                settle = Array(value.size) { Spring(1f).also { s -> s.jump(1f) } }
                lit = Array(value.size) { Spring(0f) }
            }
            aimed = when {
                aimed == BACK || aimed >= BAR0 -> aimed
                else -> value.indexOfFirst { it.id == old }.takeIf { it >= 0 } ?: NONE
            }
            place()
            invalidate()
        }

    /* Who is waiting on the owner, as the daemon said when the wheel opened. Set once the answer
       arrives; an empty list shows no second layer. */
    var asks: List<Asks.Ask> = emptyList()
        set(value) {
            if (closing || dead) return
            field = value
            bars = barsFor(value)
            barIn = Array(bars.size) { Spring(0f) }
            barLit = Array(bars.size) { Spring(0f) }
            barsAt = System.nanoTime()
            if (speed == 0f) for (s in barIn) s.jump(1f) else for (s in barIn) s.set(1f, SETTLE_K, SETTLE_Z)
            if (aimed >= BAR0) aimed = NONE
            place()
            kick()
            invalidate()
        }

    /* Whether the window behind is blurred. Without the blur the glass is darker. */
    var blurOn = false
        set(value) {
            field = value
            shade()
            invalidate()
        }

    /* What the finger is on: a choice's index, BAR0 plus a bar's index, BACK for the hub, or
       NONE. */
    private var aimed = NONE

    /* The choice or bar the finger is on, or null for the hub or nothing. */
    val aimedChoice: Choice?
        get() = when {
            aimed >= TILE0 -> outerAgents().getOrNull(aimed - TILE0)?.choice?.takeIf { it.id != "none" }
            aimed >= BAR0 -> bars.getOrNull(aimed - BAR0)?.choice
            else -> choices.getOrNull(aimed)
        }

    private val dp = resources.displayMetrics.density
    private val sp = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, 1f, resources.displayMetrics)

    /* The radii, from the hub out. */
    private val hubR = 34 * dp
    private val bubbleR = 27 * dp
    private val arcIn = 44 * dp
    private val arcOut = 50 * dp
    private val bandIn = 60 * dp
    private val bandOut = 140 * dp
    private val bandMid = (bandIn + bandOut) / 2
    private val dial = 152 * dp
    /* The Glass theme's radii: the hub, the segments' inner and outer edges, the gap between
       segments, and the corner radii at the segments' outer and inner edges. */
    private val gHub = 38 * dp
    private val gIn = 52 * dp
    /* Glass 2 carries six segments, so its ring is wider to keep each word whole. */
    private val gOut = if (glass2) 172 * dp else 160 * dp
    /* How far past the half turn the edge cuts the Glass segments. Glass 2's six segments cut
       less, so its top and bottom ones keep room on screen for their words. */
    private val gPast = if (glass2) 8f else G_PAST
    private val gGap = 8 * dp
    private val gCornerOut = 14 * dp
    private val gCornerIn = 10 * dp

    /* The Bubbles theme: the hub, its smoked ring, how far in from the edge its centre sits (the
       ring just clears the edge), how far the discs float from it, and their size. */
    private val bHub = 32 * dp
    private val bRing = 64 * dp
    private val bInset = 62 * dp
    private val bOrbit = 116 * dp
    private val bDisc = 41 * dp
    /* The fainter outer arc, like a dial's guide, beyond the discs. */
    private val bOuter = 164 * dp

    /* The Dial, at 1.5 times its first size: the main ring's inner and outer edge, the second
       ring's, how far a main wedge's hit area runs past its word, and the X's half width, which
       floats in from the edge line just enough to be whole. */
    private val dIn = 60 * dp
    private val dOut = 138 * dp
    private val oIn = 142 * dp
    private val oOut = 204 * dp
    private val dHit = 268 * dp
    private val dX = 6.5f * dp
    /* Which choice's second ring is open, Needs you or Recent, or -1; and whether it was
       released on, so it stays open until the next tap. */
    private var gridOf = -1
    private var pinned = false
    /* From the daemon: The front desk's untold briefs, and the agents by their last activity. */
    private var untold = 0
    private var recentIds: List<String> = emptyList()
    /* Waiting agents outside the console's view, left out of Needs you when it follows the view. */
    private var hiddenWaiting: Set<String> = emptySet()
    private val tileLit = Array(MAX_TILES) { Spring(0f) }
    /* When the finger came to rest on Voice note, to start recording after a moment. */
    private val voiceRest = Runnable { restOnVoice() }
    private val voiceTick = object : Runnable {
        override fun run() {
            if (dead || voice?.recording != true) return
            invalidate()
            postDelayed(this, 100)
        }
    }

    /* How far out the first layer reaches, for keeping it whole on screen. */
    private val reach = when {
        dialTheme -> oOut + 10 * dp
        bubbles -> bOuter + 6 * dp
        glass -> gOut + 8 * dp
        else -> 164 * dp
    }
    private val pad = 6 * dp

    /* The bars: where they start, how thick they are, and how long they want to be. */
    private val barFrom = when {
        bubbles -> bOuter + 16 * dp
        glass -> gOut + 18 * dp
        else -> dial + 22 * dp
    }
    private val barH = 40 * dp
    private val barGap = 10 * dp

    /* The bubble's centre, and the rings'. They differ only when the rings had to slide. */
    private var ax = 0f
    private var ay = 0f
    private var cx = 0f
    private var cy = 0f
    private var ring: Ring? = null
    /* +1 when the rings open to the right, -1 when they open to the left. */
    private var way = 1
    /* Where the rings start and how far they sweep, in degrees clockwise from the right. The
       sweep runs past a half turn by as much as the edge needs, so the rings meet the edge. */
    private var start = 0f
    private var sweep = 180f

    /* One bar on the second layer: what it opens, its two lines, its kind, its angle and how
       far out it ends. */
    private class Bar(val choice: Choice, val title: String, val line: String, val kind: Int) {
        /* The pill's left edge, its width and its middle's height, from the rings' centre. */
        var left = 0f
        var w = 0f
        var y = 0f
    }

    /* The Glass theme's segments, in the rings' centre's coordinates, top to bottom. */
    private class Segment(val shape: Path, val mid: Float)

    private var segments: List<Segment> = emptyList()

    private var bars: List<Bar> = emptyList()
    private var barIn = arrayOf<Spring>()
    private var barLit = arrayOf<Spring>()
    private var barsAt = 0L

    /* Each glyph settling into place, 0 to 1. */
    private var settle = Array(choices.size) { Spring(0f) }
    /* Each glyph's light, 0 to 1. */
    private var lit = Array(choices.size) { Spring(0f) }
    private val litBack = Spring(0f)
    /* The green arc: where it is along the band, in degrees, and how much it shows. */
    private val accentAt = Spring(0f)
    private val accentOn = Spring(0f)
    private var accentPlaced = false
    /* The bubble turning into the hub, 0 to 1, and the hub on its way to the rings' centre. */
    private val morph = Spring(0f)
    private val slide = Spring(0f)
    /* The backdrop's strength, 0 to 1, on a short timed ease out. */
    private var backdrop = 0f
    private var backdropFrom = 0f
    private var backdropTo = 1f
    private var backdropStart = 0L
    /* The inner arcs' turn during a call, in degrees. */
    private var spin = 0f

    private var opened = 0L
    private var closedAt = 0L
    private var closing = false
    private var gone: (() -> Unit)? = null
    private var firstFrame = true
    private var last = 0L
    private var running = false
    /* Set once the view has failed or its window is going. It then draws nothing, takes no
       touch and runs no frame. */
    private var dead = false
    private var backCallback: Any? = null
    /* Debug builds only: the frames were stopped by the "stall" test fault. */
    private var stalled = false

    /* The system animator duration scale. Zero means animations are off. */
    private val speed = Settings.Global.getFloat(
        context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f,
    ).let { if (!ValueAnimator.areAnimatorsEnabled()) 0f else it }

    private val scrim = Paint()
    private val frost = Paint()
    /* A faint fixed grain over the frost when there is no blur, which the eye reads as a frosted
       surface rather than a grey sheet. Made once for the life of the process. */
    private val grain = Paint().apply {
        shader = android.graphics.BitmapShader(grainTile(), Shader.TileMode.REPEAT, Shader.TileMode.REPEAT)
    }
    /* A little extra white in the band's glass when there is no blur behind it. */
    private var bandBoost = 0
    /* The light inside the glass: around the rings, and around the active choice. */
    private val bloom = Paint(Paint.ANTI_ALIAS_FLAG)
    private val glow = Paint(Paint.ANTI_ALIAS_FLAG)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.BUTT
    }
    private val band = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = bandOut - bandIn
    }
    private val smoke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = bandOut - bandIn
    }
    private val wedge = Path()
    private val accent = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        color = MINT
    }
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val icon = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1.5f
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }
    /* The choices' names, the bars' two lines, and the small LIVE readout. */
    private val labelText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 12.5f * sp
        letterSpacing = 0.01f
        textAlign = Paint.Align.CENTER
        color = WHITE
    }
    private val titleText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 12.5f * sp
        color = WHITE
    }
    private val lineText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 10.5f * sp
        color = WHITE
    }
    private val monoText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = mono(context)
        textSize = 6.5f * sp
        letterSpacing = 0.2f
        textAlign = Paint.Align.CENTER
        color = WHITE
    }
    /* The Glass theme's words: the choice's name, and the small number under it. */
    private val wordText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 13f * sp
        textAlign = Paint.Align.CENTER
        color = INK
        setShadowLayer(3 * resources.displayMetrics.density, 0f, 0f, 0x4D000000)
    }
    private val hintText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 10f * sp
        textAlign = Paint.Align.CENTER
        color = WHITE
    }
    /* The Bubbles theme's sheen inside each disc, and the hub's smoked ring. Built once at the
       origin, at full size. */
    private val sheenFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        shader = LinearGradient(0f, -bDisc, 0f, bDisc * 0.05f, 0x47FFFFFF, 0x00FFFFFF, Shader.TileMode.CLAMP)
    }
    /* The big smoked glass disc behind the hub: a little lighter at its top. */
    private val holes = Path()
    private val hubGlass = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        shader = LinearGradient(
            0f, -bRing, 0f, bRing, intArrayOf(0xD92A2826.toInt(), 0xD91A1917.toInt(), 0xD9121110.toInt()),
            floatArrayOf(0f, 0.5f, 1f), Shader.TileMode.CLAMP,
        )
    }

    /* The Glass theme's faint light from above, inside each piece of glass. */
    private val topLight = LinearGradient(
        0f, -gOut, 0f, gOut * 0.3f, 0x14FFFFFF, 0x00FFFFFF, Shader.TileMode.CLAMP,
    )
    /* The Dial's wedge outlines, its red numeral, and the recording's time. */
    private val sector = Path()
    private val badgeText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 11f * sp
        isFakeBoldText = true
        textAlign = Paint.Align.CENTER
        color = RED
    }
    /* The Dial: the X's soft red glow, the agents' monograms, and a dim empty line. */
    private val redGlow = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        shader = RadialGradient(0f, 0f, 58 * resources.displayMetrics.density,
            intArrayOf(0x66E25958, 0x40E25958, 0x1AE25958), floatArrayOf(0f, 0.7f, 1f), Shader.TileMode.CLAMP)
    }
    private val monogramText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 11f * sp
        isFakeBoldText = true
        textAlign = Paint.Align.CENTER
        color = 0xFF121112.toInt()
    }
    private val dimLabel = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 13f * sp
        color = INK_DIM
    }
    private val timerText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = mono(context)
        textSize = 12f * sp
        color = MINT
    }
    /* Glass 2's floating word, and its backing's bounds. */
    private val outText = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = inter(context)
        textSize = 14f * sp
        color = INK
        setShadowLayer(4 * resources.displayMetrics.density, 0f, 1 * resources.displayMetrics.density, 0x99000000.toInt())
    }
    private val pill2 = RectF()
    private val question: Path = PathParser.createPathFromPathData(QUESTION)
    private val prompt: Path = PathParser.createPathFromPathData(PROMPT)
    private val plus: Path = PathParser.createPathFromPathData(PLUS)
    private val house = androidx.core.content.ContextCompat.getDrawable(context, R.drawable.bubble_house)?.mutate()
    private val at = IntArray(2)
    private val oval = RectF()
    private val pill = RectF()

    init {
        contentDescription = "Command wheel"
        // Focusable only so back reaches it; the window is focusable for the same reason.
        isFocusable = true
        isFocusableInTouchMode = true
        // The frost: brightest at the band's inner edge, fading toward the dial. The bloom:
        // light inside the glass, brightest on the band, ending at the dial, whose line hides
        // its edge. Both are built once at the origin; the rings are drawn translated to their
        // centre.
        band.shader = RadialGradient(
            0f, 0f, bandOut,
            intArrayOf(0x00FFFFFF, 0x30FFFFFF, 0x12FFFFFF),
            floatArrayOf(bandIn / bandOut - 0.01f, bandIn / bandOut, 1f), Shader.TileMode.CLAMP,
        )
        bloom.shader = RadialGradient(
            0f, 0f, dial,
            intArrayOf(0x00FFFFFF, 0x10FFFFFF, 0x1CFFFFFF, 0x0CFFFFFF),
            floatArrayOf(0f, arcIn / dial, bandMid / dial, 1f), Shader.TileMode.CLAMP,
        )
        // A soft dark shadow under every word and glyph, so they read on a light ground too.
        for (t in arrayOf(labelText, titleText, lineText)) t.setShadowLayer(3 * dp, 0f, 0.5f * dp, 0x59000000)
        icon.setShadowLayer(0.35f, 0f, 0.05f, 0x59000000)
        glow.shader = RadialGradient(
            0f, 0f, GLOW_R * dp, intArrayOf(0x2EFFFFFF, 0x12FFFFFF, 0x00FFFFFF),
            floatArrayOf(0f, 0.5f, 1f), Shader.TileMode.CLAMP,
        )
        shade()
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        guard { attached() }
    }

    private fun attached() {
        requestFocus()
        // Android 13 and later may send back through the window's dispatcher, not as a key.
        if (Build.VERSION.SDK_INT >= 33) {
            val cb = OnBackInvokedCallback { away() }
            findOnBackInvokedDispatcher()?.registerOnBackInvokedCallback(
                OnBackInvokedDispatcher.PRIORITY_OVERLAY, cb,
            )
            backCallback = cb
        }
        opened = System.nanoTime()
        backdropStart = opened
        if (speed == 0f) {
            for (s in settle) s.jump(1f)
            morph.jump(1f)
            slide.jump(1f)
        } else {
            for (s in settle) s.set(1f, SETTLE_K, SETTLE_Z)
            morph.set(1f, MORPH_K, MORPH_Z)
            slide.set(1f, MORPH_K, MORPH_Z)
        }
        performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
        kick()
    }

    override fun onDetachedFromWindow() {
        Choreographer.getInstance().removeFrameCallback(frame)
        running = false
        if (Build.VERSION.SDK_INT >= 33) {
            try {
                (backCallback as? OnBackInvokedCallback)?.let {
                    findOnBackInvokedDispatcher()?.unregisterOnBackInvokedCallback(it)
                }
            } catch (_: Throwable) {
            }
        }
        backCallback = null
        super.onDetachedFromWindow()
    }

    /* The window is going: nothing more runs here. */
    fun stop() {
        dead = true
        removeCallbacks(voiceRest)
        removeCallbacks(voiceTick)
        running = false
        Choreographer.getInstance().removeFrameCallback(frame)
    }

    /* Runs a piece of the view's own work. An error stops the view and goes to onFault once. */
    private inline fun guard(work: () -> Unit) {
        try {
            work()
        } catch (e: Throwable) {
            fail(e)
        }
    }

    private fun fail(e: Throwable) {
        if (dead) return
        stop()
        Log.e(TAG, "the wheel failed", e)
        try {
            onFault(e)
        } catch (_: Throwable) {
        }
    }

    /* Back, or a touch away from everything: the wheel goes at once. */
    private fun away() {
        if (dead) return
        try {
            onAway()
        } catch (e: Throwable) {
            fail(e)
        }
    }

    /* Debug builds only: draws and runs one frame, so a test fault fires. */
    fun poke() {
        if (dead) return
        invalidate()
        guard { kick() }
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.keyCode == KeyEvent.KEYCODE_BACK) {
            if (event.action == KeyEvent.ACTION_UP) away()
            return true
        }
        return super.dispatchKeyEvent(event)
    }

    /* Close: everything sweeps back into the edge, fast, then done. */
    fun close(done: () -> Unit) {
        if (closing || dead) return
        closing = true
        gone = done
        aimed = NONE
        if (speed == 0f || !isAttachedToWindow) {
            backdrop = 0f
            onBackdrop(0f)
            done()
            return
        }
        val now = System.nanoTime()
        for (s in settle) s.set(0f, CLOSE_K, CLOSE_Z)
        for (s in lit) s.set(0f, CLOSE_K, CLOSE_Z)
        for (s in barIn) s.set(0f, CLOSE_K, CLOSE_Z)
        for (s in barLit) s.set(0f, CLOSE_K, CLOSE_Z)
        litBack.set(0f, CLOSE_K, CLOSE_Z)
        accentOn.set(0f, CLOSE_K, CLOSE_Z)
        morph.set(0f, CLOSE_K, CLOSE_Z)
        slide.set(0f, CLOSE_K, CLOSE_Z)
        closedAt = now
        backdropFrom = backdrop
        backdropTo = 0f
        backdropStart = now
        kick()
    }

    private fun kick() {
        if (running || dead || stalled) return
        running = true
        last = System.nanoTime()
        Choreographer.getInstance().postFrameCallback(frame)
    }

    /* How far one part has drawn on, 0 to 1: its open sweep, cut back by its close sweep. */
    private fun drawn(openDelay: Float, openMs: Float, closeDelay: Float, closeMs: Float): Float {
        if (speed == 0f) return if (closing) 0f else 1f
        val now = System.nanoTime()
        val slow = max(speed, 0.01f)
        val o = ease(((now - opened) / 1e6f / slow - openDelay) / openMs)
        if (!closing) return o
        val c = ((now - closedAt) / 1e6f / slow - closeDelay) / closeMs
        return min(o, 1 - ease(c))
    }

    private fun ease(t: Float): Float {
        val f = t.coerceIn(0f, 1f)
        return 1 - (1 - f) * (1 - f) * (1 - f)
    }

    /* Whether every timed sweep has ended, open or close. */
    private fun sweepsDone(): Boolean {
        if (speed == 0f) return true
        val slow = max(speed, 0.01f)
        val now = System.nanoTime()
        return if (closing) (now - closedAt) / 1e6f / slow >= CLOSE_END_MS
        else (now - opened) / 1e6f / slow >= OPEN_END_MS
    }

    /* One frame: every spring steps, the backdrop eases, and the view draws again. */
    private val frame = object : Choreographer.FrameCallback {
        override fun doFrame(frameTimeNanos: Long) {
            if (dead) return
            try {
                step()
            } catch (e: Throwable) {
                running = false
                fail(e)
            }
        }

        private fun step() {
            if (BuildConfig.DEBUG) when (testFault) {
                "frame" -> {
                    testFault = null
                    throw IllegalStateException("test fault in the wheel's frame")
                }
                // The frames stop, as if the system never sent another one. Only the watchdog
                // can take the window away now.
                "stall" -> {
                    testFault = null
                    stalled = true
                    running = false
                    return
                }
            }
            val now = System.nanoTime()
            val slow = max(speed, 0.01f)
            val dt = min((now - last) / 1e9f, 0.1f) / slow
            last = now
            var moving = !sweepsDone()
            val n = settle.size
            val since = (now - (if (closing) closedAt else opened)) / 1e6f / slow
            for (i in 0 until n) {
                // Glyphs settle in order after the rings begin, and leave in reverse order.
                val order = if (closing) n - 1 - i else i
                val delay = if (closing) order * CLOSE_STAGGER else GLYPH_DELAY + order * OPEN_STAGGER
                if (since >= delay || speed == 0f) settle[i].step(dt)
                if (!settle[i].still()) moving = true
                lit[i].step(dt)
                if (!lit[i].still()) moving = true
            }
            // The bars slide out in order from when the answer came, and all go at once.
            val barSince = (now - barsAt) / 1e6f / slow
            for (i in barIn.indices) {
                if (closing || barSince >= i * BAR_STAGGER || speed == 0f) barIn[i].step(dt)
                if (!barIn[i].still()) moving = true
            }
            for (s in barLit) {
                s.step(dt)
                if (!s.still()) moving = true
            }
            for (s in tileLit) {
                s.step(dt)
                if (!s.still()) moving = true
            }
            for (s in arrayOf(litBack, accentAt, accentOn, morph, slide)) {
                s.step(dt)
                if (!s.still()) moving = true
            }
            if (onCall && !closing && speed > 0f) {
                spin = (spin + dt * SPIN_DEG_PER_S) % 360f
                moving = true
            }

            val ms = (now - backdropStart) / 1e6f / slow
            val span = if (closing) BACKDROP_OUT_MS else BACKDROP_IN_MS
            val f = if (speed == 0f) 1f else min(1f, ms / span)
            val b = backdropFrom + (backdropTo - backdropFrom) * ease(f)
            if (b != backdrop) {
                backdrop = b
                onBackdrop(b)
            }
            if (f < 1f) moving = true

            invalidate()
            if (closing && sweepsDone() && settle.all { it.x < 0.02f } && barIn.all { it.x < 0.02f } &&
                morph.x < 0.03f && slide.x < 0.03f && f >= 1f
            ) {
                running = false
                gone?.invoke()
                gone = null
                return
            }
            if (moving) Choreographer.getInstance().postFrameCallback(this) else running = false
        }
    }

    override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
        super.onLayout(changed, left, top, right, bottom)
        if (dead) return
        guard { place() }
    }

    /* The ground under the glass. Over a real blur: a light smoke, a little darker at the
       rings. Without the blur (battery saver, some phones never blur), the window must be the
       frost itself: a warm stone tint over everything, a soft vertical gradient, denser than
       the smoke, so sharp icons behind it sink back. The blur path keeps a thin layer of the
       same tint, so a phone that says it blurs but does not still reads as frosted. */
    private fun shade() {
        if (width == 0) return
        val far = hypot(max(cx, width - cx), max(cy, height - cy))
        val colors = when {
            (glass || bubbles) && blurOn -> intArrayOf(0x4D0F0E0D, 0x330F0E0D, 0x1A0F0E0D)
            glass || bubbles -> intArrayOf(0x660F0E0D, 0x4D0F0E0D, 0x330F0E0D)
            blurOn -> intArrayOf(0x580B0A09, 0x3D0B0A09, 0x1A0B0A09)
            else -> intArrayOf(0x800B0A09.toInt(), 0x600B0A09, 0x400B0A09)
        }
        scrim.shader = RadialGradient(cx, cy, far, colors, floatArrayOf(0f, 0.5f, 1f), Shader.TileMode.CLAMP)
        frost.shader = LinearGradient(
            0f, 0f, 0f, height.toFloat(),
            when {
                glass || bubbles -> intArrayOf(0x8C171514.toInt(), 0x990F0E0D.toInt(), 0xA60F0E0D.toInt())
                blurOn -> intArrayOf(0x262A2520, 0x2E211D19, 0x38181512)
                else -> intArrayOf(0xD6403830.toInt(), 0xDB332D27.toInt(), 0xE326221E.toInt())
            },
            floatArrayOf(0f, 0.5f, 1f), Shader.TileMode.CLAMP,
        )
        smoke.color = if (blurOn) 0x140B0A09 else 0x380B0A09
        bandBoost = 0  // 1.9 had no extra white on the band; kept at 0 by the owner's choice
    }

    /* Where the rings and the bars go. The rings' centre is the bubble's, on the side edge it
       sits on. Near the top or the bottom the centre slides along the edge until the rings are
       whole. */
    private fun place() {
        if (width == 0 || height == 0) return
        getLocationOnScreen(at)
        ax = anchor.x - at[0]
        ay = anchor.y - at[1]
        val insets = ViewCompat.getRootWindowInsets(this)
            ?.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
        val top = (insets?.top ?: 0) + pad
        val bottom = height - (insets?.bottom ?: 0) - pad
        val minY = top + reach
        val maxY = bottom - reach
        way = if (ax < width / 2f) 1 else -1
        // The Glass theme sits flush on the edge: its centre is on the edge line itself.
        // The Bubbles theme sits its hub just in from the edge, so its smoked ring clears it.
        cx = when {
            glass || dialTheme -> if (way > 0) 0f else width.toFloat()
            bubbles -> if (way > 0) bInset else width - bInset
            else -> ax
        }
        cy = if (minY <= maxY) ay.coerceIn(minY, maxY) else height / 2f
        // The distance from the centre to the edge the rings grow out of.
        val toEdge = if (way > 0) cx else width - cx
        // Past a half turn by the angle at which the smallest ring meets the edge.
        val over = Math.toDegrees(asin((toEdge / arcIn).coerceIn(0f, 1f).toDouble())).toFloat()
        val mid = if (way > 0) 0f else 180f
        sweep = 180f + 2 * over
        // The sweep runs from the top end, so the pen always starts at the top.
        start = mid - way * (90f + over)
        ring = when {
            dialTheme -> dialRing(choices.size, mid)
            glass -> glassRing(choices.size, mid)
            bubbles -> bubbleRing(choices.size, mid)
            else -> ring(choices.size, bandMid, mid)
        }
        if (glass) segments = glassSegments(choices.size, mid)
        if (!accentPlaced) {
            accentAt.jump(ring?.angles?.firstOrNull() ?: mid)
        }
        stack(top, bottom)
        shade()
    }

    /* Lays the bars out as a level list on the open side: every pill starts at the same edge
       just beyond the dial, the list is centred on the hub's height, BAR_GAP apart, and slides
       up or down as a whole if it would leave the window. A pill is at most 60 percent of the
       screen wide, and never wider than the room there is. */
    private fun stack(top: Float, bottom: Float) {
        val n = bars.size
        if (n == 0) return
        val room = if (way > 0) width - pad - (cx + barFrom) else cx - barFrom - pad
        val w = min(width * BAR_WIDTH, room).coerceAtLeast(0f)
        val total = n * barH + (n - 1) * barGap
        // The list's top, from the rings' centre, moved to stay inside the window.
        val first = (-total / 2).coerceIn(top - cy, bottom - cy - total)
        for (i in 0 until n) {
            val b = bars[i]
            b.w = w
            b.left = if (way > 0) barFrom else -barFrom - w
            b.y = first + i * (barH + barGap) + barH / 2
        }
    }

    /* The bars for these asks: one per ask up to four, then "+N more" for the rest. */
    private fun barsFor(list: List<Asks.Ask>): List<Bar> {
        // The Dial has no pills: waiting agents live in its Needs you cards.
        if (dialTheme || glass2) return emptyList()
        val shown = if (list.size <= MAX_BARS) list else list.take(MAX_BARS)
        val out = shown.map { a ->
            val what = when (a.kind) {
                "permission" -> a.asking.removePrefix("may it ").ifBlank { "a permission" }
                else -> a.asking.ifBlank { "a question" }
            }
            Bar(
                Choice("chat:" + a.agent, a.agent, a.agent, PLUS),
                a.agent, what, if (a.kind == "permission") PERMISSION else QUESTION_KIND,
            )
        }.toMutableList()
        if (list.size > MAX_BARS) {
            val more = list.size - MAX_BARS
            out.add(Bar(Choice("fleet", "Fleet", "Fleet", PLUS), "+$more more", "open the fleet", MORE))
        }
        return out
    }

    /* One ring of choices around the centre: the radius the glyphs sit on and each one's angle,
       in degrees clockwise from the right, top to bottom. */
    private class Ring(val radius: Float, val angles: FloatArray)

    /* n choices on a ring of this radius, spread evenly across the half turn facing mid. */
    private fun ring(n: Int, radius: Float, mid: Float): Ring {
        val step = if (n > 1) CHOICE_SPREAD / (n - 1) else 0f
        return Ring(radius, FloatArray(n) { i -> mid + way * (-CHOICE_SPREAD / 2 + step * i) })
    }

    /* The Bubbles theme's ring: n discs spread evenly from near straight up to near straight
       down, top to bottom, so the thumb reaches the bottom one first. */
    private fun bubbleRing(n: Int, mid: Float): Ring {
        // Even and symmetric about the hub's horizontal line, as the owner prefers.
        val step = if (n > 1) B_SPREAD / (n - 1) else 0f
        return Ring(bOrbit, FloatArray(n) { i -> mid + way * (-B_SPREAD / 2 + step * i) })
    }

    /* The Dial: n equal wedges over a little more than a half turn, so the edge cuts the first
       and last flush, as Glass does; each icon in the middle of its wedge's part on screen. */
    private fun dialSpan(n: Int) = (180f + 2 * G_PAST) / n.coerceAtLeast(1)

    private fun dialRing(n: Int, mid: Float): Ring {
        val span = dialSpan(n)
        return Ring((dIn + dOut) / 2, FloatArray(n) { i ->
            val lo = (-90f - G_PAST + span * i).coerceIn(-90f, 90f)
            val hi = (-90f - G_PAST + span * (i + 1)).coerceIn(-90f, 90f)
            mid + way * (lo + hi) / 2
        })
    }

    /* The Dial: what the daemon said about the front desk's untold briefs and the recent agents. */
    fun dialState(untoldBriefs: Int, recent: List<String>, waitingOutOfView: Set<String> = emptySet()) {
        hiddenWaiting = waitingOutOfView
        if (dead) return
        untold = untoldBriefs.coerceAtLeast(0)
        recentIds = recent
        invalidate()
    }

    /* The Dial: one agent of the second ring, its choice, its ask and its kind. */
    private class Agent(val choice: Choice, val title: String, val line: String, val kind: Int)

    /* The Dial: the second ring's agents. Needs you's are those waiting on the owner; Recent's
       the three most recently active not already waiting. None, one quiet place saying so. */
    private fun outerAgents(): List<Agent> {
        val waiting = asks.map { it.agent }.toSet()
        return when (choices.getOrNull(gridOf)?.id) {
            "needs" -> asks.filter { it.agent !in hiddenWaiting }.take(MAX_TILES).map { a ->
                val what = when (a.kind) {
                    "permission" -> a.asking.removePrefix("may it ").ifBlank { "a permission" }
                    else -> a.asking.ifBlank { "a question" }
                }
                Agent(Choice("chat:" + a.agent, a.agent, a.agent, PLUS), a.agent, what,
                    if (a.kind == "permission") PERMISSION else QUESTION_KIND)
            }.ifEmpty { listOf(Agent(Choice("none", "", "", PLUS), "Nobody is waiting", "", EMPTY)) }
            "recent" -> recentIds.filter { it !in waiting }.take(3)
                .map { Agent(Choice("chat:$it", it, it, PLUS), it, "", NAME) }
                .ifEmpty { listOf(Agent(Choice("none", "", "", PLUS), "No recent agents", "", EMPTY)) }
            else -> emptyList()
        }
    }

    /* The Dial, released on Needs you or Recent: the second ring stays open until the next tap. */
    fun pin(): Boolean {
        if (dead || closing || gridOf < 0) return false
        // Glass 2 keeps its pills only after a plain tap, since a tap cannot slide; a release
        // after a slide just closes.
        if (glass2 && !releasedAsTap) return false
        pinned = true
        invalidate()
        return true
    }

    val isPinned: Boolean
        get() = pinned

    /* Whether the last release on this view was a plain tap: down and up without moving. A
       release that came from the bubble's press and slide never is. */
    private var releasedAsTap = false
    private var downX = 0f
    private var downY = 0f
    private var moved = false

    /* The Dial, when the finger moves: Needs you and Recent open the second ring and keep it
       while the finger crosses to it; another main wedge or the X closes it. Resting on Voice
       note starts a recording after a moment; leaving it throws the recording away. */
    private fun dialAimed(was: Int, next: Int) {
        val id = choices.getOrNull(next)?.id
        when {
            id == "needs" || id == "recent" -> gridOf = next
            next in choices.indices || next == BACK -> {
                gridOf = -1
                pinned = false
            }
        }
        for (k in tileLit.indices) tileLit[k].set(if (TILE0 + k == next) 1f else 0f, LIT_K, LIT_Z)
        if (speed == 0f) for (s in tileLit) s.jump(s.target)
        val onVoice = id == "voicenote"
        if (choices.getOrNull(was)?.id == "voicenote" && !onVoice) {
            removeCallbacks(voiceRest)
            removeCallbacks(voiceTick)
            voice?.cancel()
        }
        if (onVoice) {
            removeCallbacks(voiceRest)
            postDelayed(voiceRest, VOICE_REST_MS)
        }
    }

    private fun restOnVoice() {
        if (dead || closing || choices.getOrNull(aimed)?.id != "voicenote") return
        val v = voice ?: return
        if (v.start()) {
            performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
            post(voiceTick)
        }
    }

    /* The Dial: the slim ring of warm translucent charcoal, cut into wedges by faint darker
       partitions, a dim grey icon in each and nothing else at rest. The wedge under the finger
       is outlined and tinted mint, its icon white, and its word floats level just outside it;
       on Voice note while recording, a mint mic, a live level and the time. When Needs you or
       Recent is reached, the second ring of agents shows just outside. The canvas is at the
       hub, on the edge line. */
    private fun drawDial(canvas: Canvas) {
        val r = ring ?: return
        val p = settle.firstOrNull()?.x ?: 1f
        val shown = p.coerceIn(0f, 1f)
        if (shown <= 0.001f) return
        val mid = if (way > 0) 0f else 180f
        val n = choices.size
        val span = way * dialSpan(n)
        val from = mid - way * (90f + G_PAST)
        val recording = voice?.recording == true
        canvas.withScale(0.9f + 0.1f * p, 0.9f + 0.1f * p) {
            // The X under the finger: the inner disc glows softly red.
            val gb = litBack.x.coerceIn(0f, 1f)
            if (gb > 0.01f) {
                redGlow.alpha = (255 * gb * shown).roundToInt()
                drawCircle(0f, 0f, dIn - 2 * dp, redGlow)
            }
            ringPieces(this, dIn, dOut, from, span, n, shown)
            val on = if (aimed in 0 until n) aimed else gridOf
            if (on in 0 until n) litPiece(this, dIn, dOut, from + span * on, span, shown)
            for (i in 0 until n) {
                if (i >= r.angles.size) break
                val c = choices[i]
                val rad = Math.toRadians(r.angles[i].toDouble())
                var ix = (r.radius * cos(rad)).toFloat()
                if (way > 0) ix = max(ix, 16 * dp) else ix = min(ix, -16 * dp)
                val iy = (r.radius * sin(rad)).toFloat()
                val rec = recording && c.id == "voicenote"
                icon.strokeWidth = 2.1f
                drawIcon(this, c.path, ix, iy, 21 * dp, if (rec) MINT else WHITE,
                    (255 * shown * (if (i == on || rec) 1f else D_ICON)).roundToInt())
                icon.strokeWidth = 1.5f
                // The front desk's untold briefs: a small bare red numeral near its icon, none at zero.
                if (c.id == "call" && untold > 0) {
                    badgeText.alpha = (255 * shown).roundToInt()
                    // On the open side of the icon, so the screen edge never cuts it.
                    drawText(untold.toString(), ix + way * 16 * dp, iy - 9 * dp, badgeText)
                }
            }
            if (gridOf >= 0) drawOuter(this, shown)
            if (menuWords && on in 0 until n && on < r.angles.size && aimed < TILE0) {
                val c = choices[on]
                val rec = recording && c.id == "voicenote"
                val word = if (rec) {
                    val sec = ((android.os.SystemClock.elapsedRealtime() - (voice?.startedAt ?: 0L)) / 1000).coerceAtLeast(0)
                    String.format(java.util.Locale.ROOT, "%d:%02d", sec / 60, sec % 60)
                } else c.short
                val outer = if (gridOf == on) oOut else dOut
                floatWord(this, r.angles[on], outer, word, null, rec, shown)
            }
        }
    }

    /* The Dial: a ring's wedges in charcoal with the faint darker partitions between them. */
    private fun ringPieces(canvas: Canvas, a: Float, b: Float, from: Float, span: Float, n: Int, shown: Float) {
        for (i in 0 until n) {
            dialSector(a, b, from + span * i, span)
            fill.shader = null
            fill.style = Paint.Style.FILL
            fill.color = D_WEDGE
            fill.alpha = ((D_WEDGE ushr 24) * shown).roundToInt()
            canvas.drawPath(sector, fill)
        }
        line.color = 0xFF000000.toInt()
        line.strokeCap = Paint.Cap.BUTT
        line.strokeWidth = 2.25f * dp
        line.alpha = (0x80 * shown).roundToInt()
        for (k in 0..n) tick(canvas, from + span * k, a, b)
    }

    /* The Dial: one wedge outlined and tinted mint. */
    private fun litPiece(canvas: Canvas, a: Float, b: Float, from: Float, span: Float, shown: Float) {
        dialSector(a, b, from, span)
        fill.shader = null
        fill.style = Paint.Style.FILL
        fill.color = MINT
        fill.alpha = (0x22 * shown).roundToInt()
        canvas.drawPath(sector, fill)
        fill.style = Paint.Style.STROKE
        fill.strokeWidth = 2.25f * dp
        fill.alpha = (0xF2 * shown).roundToInt()
        canvas.drawPath(sector, fill)
    }

    /* The Dial: a word floating level just outside a ring at an angle, on a small charcoal
       backing, with an optional dim second line; the recording's time and live level in mint. */
    private fun floatWord(canvas: Canvas, deg: Float, outer: Float, word: String, second: String?, rec: Boolean, shown: Float) {
        val am = Math.toRadians(deg.toDouble())
        val cs = cos(am).toFloat()
        val sn = sin(am).toFloat()
        val lr = outer + 20 * dp + 12 * dp * abs(sn)
        val lx = cs * lr + way * 2 * dp
        val ly = sn * lr
        val paint = if (rec) timerText else wordText
        paint.textAlign = if (way > 0) Paint.Align.LEFT else Paint.Align.RIGHT
        paint.alpha = (255 * shown).roundToInt()
        val levelW = if (rec) 7 * 5 * dp + 8 * dp else 0f
        val cut = second?.let { TextUtils.ellipsize(it, lineText, 170 * dp, TextUtils.TruncateAt.END).toString() }
        lineText.color = INK_DIM
        lineText.textAlign = paint.textAlign
        val w = max(paint.measureText(word) + levelW, cut?.let { lineText.measureText(it) } ?: 0f)
        val h = if (cut != null) 22 * dp else 13 * dp
        val bl = if (way > 0) lx - 8 * dp else lx - w - 8 * dp
        pill.set(bl, ly - h, bl + w + 16 * dp, ly + h)
        fill.shader = null
        fill.style = Paint.Style.FILL
        fill.color = D_WEDGE
        fill.alpha = ((D_WEDGE ushr 24) * shown).roundToInt()
        canvas.drawRoundRect(pill, 13 * dp, 13 * dp, fill)
        val tx = if (way > 0) lx + levelW else lx
        val wy = if (cut != null) ly - 6 * dp else ly
        canvas.drawText(word, tx, wy - (paint.ascent() + paint.descent()) / 2, paint)
        if (cut != null) {
            lineText.alpha = (255 * shown).roundToInt()
            canvas.drawText(cut, lx, ly + 14 * dp, lineText)
        }
        lineText.color = WHITE
        lineText.textAlign = Paint.Align.LEFT
        paint.textAlign = Paint.Align.CENTER
        if (rec) {
            val lv = voice?.level() ?: 0f
            val shape = floatArrayOf(0.45f, 0.75f, 1f, 0.6f, 0.9f, 0.5f, 0.7f)
            val x0 = if (way > 0) lx else lx - w + paint.measureText(word) + 8 * dp
            fill.color = MINT
            fill.alpha = (255 * shown).roundToInt()
            for (k in shape.indices) {
                val bh = (4 * dp + 12 * dp * lv) * shape[k]
                pill.set(x0 + k * 5 * dp, ly - bh / 2, x0 + k * 5 * dp + 3 * dp, ly + bh / 2)
                canvas.drawRoundRect(pill, 1.5f * dp, 1.5f * dp, fill)
            }
        }
    }

    /* The Dial's second ring: one wedge per agent over the same sweep, in the same charcoal and
       partitions, each with a monogram disc in the agent's own muted colour and, for a waiting
       agent, its small mark. The agent under the finger lights mint and its name floats outside,
       with its ask beside it. */
    private fun drawOuter(canvas: Canvas, shown: Float) {
        val agents = outerAgents()
        if (agents.isEmpty()) return
        val mid = if (way > 0) 0f else 180f
        val n = agents.size
        val span = way * dialSpan(n)
        val from = mid - way * (90f + G_PAST)
        ringPieces(canvas, oIn, oOut, from, span, n, shown)
        val angles = dialAngles(n, mid)
        val rm = (oIn + oOut) / 2
        for (k in agents.indices) {
            val a = agents[k]
            val lit = aimed == TILE0 + k
            if (lit) litPiece(canvas, oIn, oOut, from + span * k, span, shown)
            val rad = Math.toRadians(angles[k].toDouble())
            var x = (rm * cos(rad)).toFloat()
            if (way > 0) x = max(x, 18 * dp) else x = min(x, -18 * dp)
            val y = (rm * sin(rad)).toFloat()
            if (a.kind == EMPTY) {
                dimLabel.alpha = (255 * shown).roundToInt()
                canvas.drawText(a.title, x + way * 24 * dp, y - (dimLabel.ascent() + dimLabel.descent()) / 2, dimLabel)
                continue
            }
            fill.shader = null
            fill.style = Paint.Style.FILL
            fill.color = agentColour(a.title)
            fill.alpha = (255 * shown).roundToInt()
            canvas.drawCircle(x, y, 15 * dp, fill)
            if (lit) {
                fill.style = Paint.Style.STROKE
                fill.strokeWidth = 1.5f * dp
                fill.color = WHITE
                canvas.drawCircle(x, y, 15 * dp, fill)
            }
            monogramText.alpha = (255 * shown).roundToInt()
            canvas.drawText(monogram(a.title), x, y - (monogramText.ascent() + monogramText.descent()) / 2, monogramText)
            if (a.kind == PERMISSION || a.kind == QUESTION_KIND) {
                val mark = if (a.kind == PERMISSION) prompt else question
                drawIcon(canvas, mark, x + 16 * dp, y + 13 * dp, 12 * dp, INK, (230 * shown).roundToInt())
            }
        }
        val k = aimed - TILE0
        if (menuWords && k in agents.indices && agents[k].kind != EMPTY) {
            val a = agents[k]
            floatWord(canvas, angles[k], oOut, a.title, a.line.ifBlank { null }, false, shown)
        }
    }

    /* The Dial: the middle angle of each of n wedges' parts on screen. */
    private fun dialAngles(n: Int, mid: Float): FloatArray {
        val span = dialSpan(n)
        return FloatArray(n) { i ->
            val lo = (-90f - G_PAST + span * i).coerceIn(-90f, 90f)
            val hi = (-90f - G_PAST + span * (i + 1)).coerceIn(-90f, 90f)
            mid + way * (lo + hi) / 2
        }
    }

    /* Two letters for an agent: the first of each of its first two words, or its first two. */
    private fun monogram(id: String): String {
        val parts = id.split('-', '_', ' ').filter { it.isNotBlank() }
        val m = if (parts.size >= 2) "${parts[0][0]}${parts[1][0]}" else id.take(2)
        return m.uppercase(java.util.Locale.ROOT)
    }

    /* An agent's own muted colour, the same every time: picked from the palette by its id. */
    private fun agentColour(id: String): Int {
        var h = 0
        for (ch in id) h = h * 31 + ch.code
        return AGENT_PALETTE[Math.floorMod(h, AGENT_PALETTE.size)]
    }

    /* The Dial: an annulus sector from radius a to b, from angle `from` through `sweep`. */
    private fun dialSector(a: Float, b: Float, from: Float, sweep: Float) {
        sector.reset()
        oval.set(-b, -b, b, b)
        sector.arcTo(oval, from, sweep, true)
        oval.set(-a, -a, a, a)
        sector.arcTo(oval, from + sweep, -sweep, false)
        sector.close()
    }

    /* The Dial's centre: a small red X floating on its own, nothing bounding it, in from the edge
       line just enough to be whole. It grows in from the bubble as the dial opens. */
    private fun drawDialX(canvas: Canvas, x: Float, y: Float) {
        val m = morph.x.coerceIn(0f, 1f)
        val out = (1 - m / 0.5f).coerceIn(0f, 1f)
        if (out > 0.01f) {
            fill.shader = null
            fill.style = Paint.Style.FILL
            fill.color = STONE_RAISED
            fill.alpha = (255 * out).roundToInt()
            canvas.drawCircle(ax, ay, bubbleR, fill)
            house?.let { d ->
                val half = (15 * dp).roundToInt()
                d.setBounds(ax.roundToInt() - half, ay.roundToInt() - half, ax.roundToInt() + half, ay.roundToInt() + half)
                d.alpha = (out * 255).roundToInt()
                d.draw(canvas)
            }
        }
        val into = ((m - 0.35f) / 0.65f).coerceIn(0f, 1f)
        if (into <= 0.01f) return
        val gx = x + way * (dX + 10 * dp)
        val arm = dX * (0.8f + 0.2f * into)
        fill.shader = null
        fill.style = Paint.Style.STROKE
        fill.strokeWidth = 1.75f * dp
        fill.strokeCap = Paint.Cap.ROUND
        fill.color = RED
        fill.alpha = (into * 255).roundToInt()
        canvas.drawLine(gx - arm, y - arm, gx + arm, y + arm, fill)
        canvas.drawLine(gx - arm, y + arm, gx + arm, y - arm, fill)
        fill.strokeCap = Paint.Cap.BUTT
    }

    /* The Glass theme's ring: each choice at the middle of the part of its segment that is on
       screen, top to bottom. */
    private fun glassRing(n: Int, mid: Float): Ring {
        val span = (180f + 2 * gPast) / n.coerceAtLeast(1)
        return Ring((gIn + gOut) / 2, FloatArray(n) { i ->
            val lo = (-90f - gPast + span * i).coerceIn(-90f, 90f)
            val hi = (-90f - gPast + span * (i + 1)).coerceIn(-90f, 90f)
            mid + way * (lo + hi) / 2
        })
    }

    /* The segments themselves, fanned from the hub on the edge over a little more than a half
       turn, so the first and last run past the edge: the edge cuts them flush, and every corner
       left on screen is rounded. */
    private fun glassSegments(n: Int, mid: Float): List<Segment> {
        val span = (180f + 2 * gPast) / n.coerceAtLeast(1)
        return List(n) { i ->
            val a = mid + way * (-90f - gPast + span * i)
            val b = a + way * span
            Segment(segmentPath(min(a, b), max(a, b)), (a + b) / 2)
        }
    }

    /* One segment from angle `from` to `to` (degrees, low before high). The gap to its
       neighbours is gGap wide all the way out, and every corner is rounded: gCornerOut at the
       outer edge, gCornerIn at the inner. */
    private fun segmentPath(from: Float, to: Float): Path {
        val ri = gIn
        val ro = gOut
        val di = Math.toDegrees(asin((gGap / 2 / ri).toDouble())).toFloat()
        val dO = Math.toDegrees(asin((gGap / 2 / ro).toDouble())).toFloat()
        val ko = Math.toDegrees((gCornerOut / ro).toDouble()).toFloat()
        val ki = Math.toDegrees((gCornerIn / ri).toDouble()).toFloat()
        val oA = from + dO
        val oB = to - dO
        val iA = from + di
        val iB = to - di
        fun pt(r: Float, deg: Float) = PointF(
            (r * cos(Math.toRadians(deg.toDouble()))).toFloat(), (r * sin(Math.toRadians(deg.toDouble()))).toFloat(),
        )
        fun toward(p: PointF, q: PointF, k: Float): PointF {
            val len = hypot(q.x - p.x, q.y - p.y)
            return PointF(p.x + (q.x - p.x) * k / len, p.y + (q.y - p.y) * k / len)
        }
        val cA = pt(ro, oA)
        val cB = pt(ro, oB)
        val cC = pt(ri, iB)
        val cD = pt(ri, iA)
        val path = Path()
        val s = pt(ro, oA + ko)
        path.moveTo(s.x, s.y)
        path.arcTo(RectF(-ro, -ro, ro, ro), oA + ko, oB - oA - 2 * ko, false)
        val b1 = toward(cB, cC, gCornerOut)
        path.quadTo(cB.x, cB.y, b1.x, b1.y)
        val c0 = toward(cC, cB, gCornerIn)
        path.lineTo(c0.x, c0.y)
        val c1 = pt(ri, iB - ki)
        path.quadTo(cC.x, cC.y, c1.x, c1.y)
        path.arcTo(RectF(-ri, -ri, ri, ri), iB - ki, -(iB - iA - 2 * ki), false)
        val d1 = toward(cD, cA, gCornerIn)
        path.quadTo(cD.x, cD.y, d1.x, d1.y)
        val a0 = toward(cA, cD, gCornerOut)
        path.lineTo(a0.x, a0.y)
        path.quadTo(cA.x, cA.y, s.x, s.y)
        path.close()
        return path
    }

    /* Where the hub is now: on the bubble, on its way to the rings' centre, or there. */
    private fun hubX() = ax + (cx - ax) * slide.x
    private fun hubY() = ay + (cy - ay) * slide.x

    /* The smallest turn from a to b, in degrees, from -180 to 180. */
    private fun turn(a: Float, b: Float): Float {
        var d = (b - a) % 360f
        if (d > 180f) d -= 360f
        if (d < -180f) d += 360f
        return d
    }

    /* What is under a point in this view: a choice's index, BAR0 plus a bar's index, BACK, or
       NONE. The whole wedge around a choice, from the inner arcs out past the dial, is its hit
       area; a bar's is its pill and a little around it. */
    private fun under(x: Float, y: Float): Int {
        val r = ring ?: return NONE
        val dx = x - hubX()
        val dy = y - hubY()
        val d = hypot(dx, dy)
        // Glass 2: the agents' pills, when they show, come first.
        if (glass2 && gridOf >= 0) {
            val agents = outerAgents()
            for (k in agents.indices) {
                pillRect(k, agents.size)
                if (pill.contains(dx, dy)) return if (agents[k].kind == EMPTY) NONE else TILE0 + k
            }
        }
        // The Dial: the X, then the second ring when it is open, then a main wedge out past its
        // word.
        if (dialTheme) {
            if (d < dIn) return BACK
            val deg = Math.toDegrees(atan2(dy, dx).toDouble()).toFloat()
            val mid = if (way > 0) 0f else 180f
            val rel = way * turn(mid, deg) + 90f + G_PAST
            if (gridOf >= 0) {
                if (d > oOut + 10 * dp) return NONE
                if (d >= oIn - 3 * dp) {
                    val agents = outerAgents()
                    val k = (rel / dialSpan(agents.size)).toInt().coerceIn(0, agents.size - 1)
                    return if (agents[k].choice.id == "none") NONE else TILE0 + k
                }
            } else if (d > dHit) return NONE
            return (rel / dialSpan(choices.size)).toInt().coerceIn(0, choices.size - 1)
        }
        if (!bubbles && d < (if (glass) gIn - 4 * dp else arcIn - 4 * dp)) return BACK
        if (d > reach + 4 * dp) {
            for (i in bars.indices) {
                val b = bars[i]
                if ((barIn.getOrNull(i)?.x ?: 0f) < 0.5f) continue
                if (dx >= b.left - 4 * dp && dx <= b.left + b.w + 4 * dp &&
                    abs(dy - b.y) <= barH / 2 + barGap / 2
                ) {
                    return BAR0 + i
                }
            }
            if (d > reach + 8 * dp) return NONE
        }
        // The Bubbles theme: the hub and its ring, or a disc and a little around it. The gaps
        // between discs are nothing, so a stray tap there closes the wheel.
        if (bubbles) {
            if (d < bRing) return BACK
            var hit = NONE
            var close = bDisc + 10 * dp
            for (i in r.angles.indices) {
                val rad = Math.toRadians(r.angles[i].toDouble())
                val e = hypot(dx - (r.radius * cos(rad)).toFloat(), dy - (r.radius * sin(rad)).toFloat())
                if (e < close) {
                    close = e
                    hit = i
                }
            }
            return hit
        }
        val deg = Math.toDegrees(atan2(dy, dx).toDouble()).toFloat()
        val half = when {
            glass -> (180f + 2 * gPast) / r.angles.size.coerceAtLeast(1) / 2 + 8f
            r.angles.size > 1 -> CHOICE_SPREAD / (r.angles.size - 1) / 2
            else -> 90f
        }
        var best = NONE
        var near = Float.MAX_VALUE
        for (i in r.angles.indices) {
            val a = abs(turn(r.angles[i], deg))
            if (a < near) {
                near = a
                best = i
            }
        }
        // Glass 2 with its pills showing: the open ground between the ring and the pills keeps
        // them, so the finger can cross to a pill; on the ring itself any segment takes over.
        if (glass2 && gridOf >= 0 && best != gridOf && d > gOut) return NONE
        return if (near <= (if (glass) half else max(half, 40f))) best else NONE
    }

    private fun setAimed(next: Int) {
        if (closing || next == aimed) return
        val was = aimed
        aimed = next
        if (dialTheme || glass2) dialAimed(was, next)
        for (i in lit.indices) lit[i].set(if (i == next) 1f else 0f, LIT_K, LIT_Z)
        for (i in barLit.indices) barLit[i].set(if (BAR0 + i == next) 1f else 0f, LIT_K, LIT_Z)
        litBack.set(if (next == BACK) 1f else 0f, LIT_K, LIT_Z)
        val r = ring
        if (next >= 0 && next < BAR0 && r != null && next < r.angles.size) {
            if (accentOn.x < 0.05f) accentAt.jump(r.angles[next])
            accentAt.set(r.angles[next], ACCENT_K, ACCENT_Z)
            accentPlaced = true
            accentOn.set(1f, LIT_K, 1f)
        } else {
            accentOn.set(0f, LIT_K, 1f)
        }
        if (speed == 0f) {
            for (s in lit) s.jump(s.target)
            for (s in barLit) s.jump(s.target)
            litBack.jump(litBack.target)
            accentAt.jump(accentAt.target)
            accentOn.jump(accentOn.target)
        }
        if (next != NONE) performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
        kick()
        invalidate()
    }

    /* Aim at a point on screen, for the press and hold gesture from the bubble. */
    fun aimAt(rawX: Float, rawY: Float) {
        if (dead) return
        guard {
            getLocationOnScreen(at)
            releasedAsTap = false
            setAimed(under(rawX - at[0], rawY - at[1]))
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        if (dead) return false
        guard { touch(e) }
        return true
    }

    private fun touch(e: MotionEvent) {
        onTouched()
        if (BuildConfig.DEBUG && testFault == "touch") {
            testFault = null
            throw IllegalStateException("test fault in the wheel's touch")
        }
        // A touch during the close does not wait for it.
        if (closing) {
            if (e.actionMasked == MotionEvent.ACTION_DOWN) away()
            return
        }
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = e.x
                downY = e.y
                moved = false
                val on = under(e.x, e.y)
                if (on == NONE) away() else setAimed(on)
            }
            MotionEvent.ACTION_MOVE -> {
                if (hypot(e.x - downX, e.y - downY) > 12 * dp) moved = true
                setAimed(under(e.x, e.y))
            }
            MotionEvent.ACTION_UP -> {
                releasedAsTap = !moved
                setAimed(under(e.x, e.y))
                onDone(aimedChoice)
            }
            MotionEvent.ACTION_CANCEL -> away()
        }
    }

    override fun onDraw(canvas: Canvas) {
        if (dead) return
        guard { paint(canvas) }
    }

    private fun paint(canvas: Canvas) {
        if (BuildConfig.DEBUG && testFault == "draw") {
            testFault = null
            throw IllegalStateException("test fault in the wheel's draw")
        }
        val ground = (backdrop.coerceIn(0f, 1f) * 255).roundToInt()
        // The owner preferred 1.9's lighter ground over the 1.10 frost and grain, so they are not
        // drawn. The paints stay, in case a denser look is wanted again.
        // The Glass theme with no blur behind it (the owner's phone): a light warm frost and a
        // faint grain, so sharp icons behind soften and the glass reads. HUD draws as it was.
        if ((glass || bubbles) && !blurOn && !dialTheme && !glass2) {
            frost.alpha = ground
            canvas.drawPaint(frost)
            grain.alpha = ground
            canvas.drawPaint(grain)
        }
        // The Dial draws no ground at all: the screen behind stays as it is.
        if (!dialTheme && !glass2) {
            scrim.alpha = ground
            canvas.drawPaint(scrim)
        }
        val hx = hubX()
        val hy = hubY()
        if (dialTheme) {
            canvas.withTranslation(hx, hy) {
                drawDial(this)
            }
            drawDialX(canvas, hx, hy)
        } else if (bubbles) {
            canvas.withTranslation(hx, hy) {
                drawBubbles(this)
                drawGlassBars(this)
            }
            drawBubbleHub(canvas, hx, hy)
        } else if (glass) {
            canvas.withTranslation(hx, hy) {
                drawSegments(this)
                if (glass2) drawMenuPills(this) else drawGlassBars(this)
            }
            drawGlassHub(canvas, hx, hy)
        } else {
            canvas.withTranslation(hx, hy) {
                drawRings(this)
                ring?.let { drawChoices(this, it) }
                drawBars(this)
            }
            drawHub(canvas, hx, hy)
        }
        if (firstFrame) {
            firstFrame = false
            // The real bubble hides only when the hub, its copy, is whole inside the window.
            if (ax - bubbleR >= 0 && ay - bubbleR >= 0 && ax + bubbleR <= width && ay + bubbleR <= height) {
                post { if (!dead) onFirstFrame() }
            }
        }
    }

    /* A full ring of radius r, shown as far as p (0 to 1) along the sweep. Rings are drawn as
       whole circles, which render as exact rings; while one draws on, a wedge clip shows only
       its swept part. At rest no clip is needed: the rest of the circle is off screen. */
    private fun ringOn(canvas: Canvas, r: Float, p: Float, paint: Paint) {
        if (p <= 0.001f) return
        if (p >= 0.999f) {
            canvas.drawCircle(0f, 0f, r, paint)
            return
        }
        val far = reach * 2
        wedge.reset()
        wedge.moveTo(0f, 0f)
        oval.set(-far, -far, far, far)
        wedge.arcTo(oval, start, way * sweep * p, false)
        wedge.close()
        canvas.withClip(wedge) { drawCircle(0f, 0f, r, paint) }
    }

    /* A filled disc of radius r, shown as far as p along the sweep, like ringOn. */
    private fun ringFill(canvas: Canvas, r: Float, p: Float, paint: Paint) {
        if (p >= 0.999f) {
            canvas.drawCircle(0f, 0f, r, paint)
            return
        }
        wedge.reset()
        wedge.moveTo(0f, 0f)
        oval.set(-r * 2, -r * 2, r * 2, r * 2)
        wedge.arcTo(oval, start, way * sweep * p, false)
        wedge.close()
        canvas.withClip(wedge) { drawCircle(0f, 0f, r, paint) }
    }

    /* The light in the glass, the band, the dial and its ticks, the green arc and the inner
       broken arcs. The canvas is at the rings' centre. */
    private fun drawRings(canvas: Canvas) {
        val pDial = drawn(0f, 240f, 70f, 150f)
        val pBand = drawn(40f, 240f, 35f, 150f)
        val pInner = drawn(90f, 220f, 0f, 140f)
        val pTicks = drawn(120f, 200f, 0f, 90f)

        // The bloom, lit as the band draws on, and ending under the dial's line.
        if (pBand > 0.001f) {
            bloom.alpha = (255 * pBand).roundToInt()
            ringFill(canvas, dial, pBand, bloom)
        }

        // The band: a faint smoke, then the frost, then hairlines on both edges.
        ringOn(canvas, bandMid, pBand, smoke)
        ringOn(canvas, bandMid, pBand, band)
        if (bandBoost > 0) {
            smoke.color = WHITE
            smoke.alpha = bandBoost
            ringOn(canvas, bandMid, pBand, smoke)
            smoke.color = if (blurOn) 0x140B0A09 else 0x380B0A09
        }
        line.color = WHITE
        line.strokeWidth = 0.75f * dp
        line.alpha = 56
        ringOn(canvas, bandOut, pBand, line)
        line.alpha = 36
        ringOn(canvas, bandIn, pBand, line)

        // The dial.
        line.strokeWidth = dp
        line.alpha = 110
        ringOn(canvas, dial, pDial, line)

        // Ticks: a fine one every few degrees, a longer one at each choice.
        if (pTicks > 0.01f) {
            val r = ring
            line.strokeWidth = 0.8f * dp
            var a = 0f
            while (a <= sweep) {
                val deg = start + way * a
                if (a <= sweep * pDial) {
                    line.alpha = (60 * pTicks).roundToInt()
                    tick(canvas, deg, dial, dial + 3.5f * dp)
                }
                a += TICK_DEG
            }
            if (r != null) {
                for (i in r.angles.indices) {
                    val g = lit.getOrNull(i)?.x?.coerceIn(0f, 1f) ?: 0f
                    line.strokeWidth = (1f + 0.4f * g) * dp
                    line.color = blend(WHITE, MINT, g)
                    line.alpha = ((150 + 105 * g) * pTicks).roundToInt()
                    tick(canvas, r.angles[i], dial - 2 * dp, dial + 8 * dp)
                    line.color = WHITE
                }
            }
        }

        // The active choice: a soft light around it, the green arc behind it on the band's
        // outer edge, and a shorter one on its inner edge.
        val on = accentOn.x.coerceIn(0f, 1f) * min(pBand, 1f)
        if (on > 0.01f) {
            val a = accentAt.x
            val rad = Math.toRadians(a.toDouble())
            canvas.withTranslation((bandMid * cos(rad)).toFloat(), (bandMid * sin(rad)).toFloat()) {
                glow.alpha = (255 * on).roundToInt()
                drawCircle(0f, 0f, (GLOW_R + 4) * dp, glow)
            }
            accent.alpha = (255 * on).roundToInt()
            accent.strokeWidth = 2 * dp
            oval.set(-bandOut, -bandOut, bandOut, bandOut)
            canvas.drawArc(oval, a - ACCENT_DEG / 2, ACCENT_DEG, false, accent)
            accent.alpha = (150 * on).roundToInt()
            accent.strokeWidth = dp
            oval.set(-bandIn, -bandIn, bandIn, bandIn)
            canvas.drawArc(oval, a - ACCENT_DEG / 2, ACCENT_DEG, false, accent)
        }

        // The inner broken arcs, turning slowly during a call.
        if (pInner > 0.001f) {
            val mid = start + way * sweep / 2
            canvas.withRotation(spin) {
                line.strokeWidth = dp
                line.alpha = (120 * pInner).roundToInt()
                broken(this, arcIn, mid, INNER_A, pInner)
                line.strokeWidth = 0.75f * dp
                line.alpha = (70 * pInner).roundToInt()
                broken(this, arcOut, mid, INNER_B, pInner)
            }
        }
    }

    private fun tick(canvas: Canvas, deg: Float, from: Float, to: Float) {
        val c = cos(Math.toRadians(deg.toDouble())).toFloat()
        val s = sin(Math.toRadians(deg.toDouble())).toFloat()
        canvas.drawLine(c * from, s * from, c * to, s * to, line)
    }

    /* Arcs on radius r around mid, given as pairs of offsets in degrees, each drawn on by p. */
    private fun broken(canvas: Canvas, r: Float, mid: Float, parts: FloatArray, p: Float) {
        oval.set(-r, -r, r, r)
        var i = 0
        while (i + 1 < parts.size) {
            val a = mid + way * parts[i]
            val len = (parts[i + 1] - parts[i]) * p
            canvas.drawArc(oval, a, way * len, false, line)
            i += 2
        }
    }

    /* The choices on the band: a large glyph and its name under it. The canvas is at the rings'
       centre. */
    private fun drawChoices(canvas: Canvas, r: Ring) {
        for (i in r.angles.indices) {
            if (i >= choices.size || i >= settle.size) break
            val p = settle[i].x
            val shown = p.coerceIn(0f, 1f)
            if (shown <= 0.001f) continue
            val g = lit[i].x.coerceIn(0f, 1f)
            val rad = Math.toRadians(r.angles[i].toDouble())
            // Each choice settles out from a little nearer the hub.
            val gr = r.radius - (1 - p) * 14 * dp
            val x = (gr * cos(rad)).toFloat()
            val y = (gr * sin(rad)).toFloat()
            val size = GLYPH_DP * dp * (0.9f + 0.1f * p) * (1 + 0.06f * g)
            drawIcon(canvas, choices[i].path, x, y - 9 * dp, size, WHITE,
                (255 * shown * (GLYPH_DIM + (1 - GLYPH_DIM) * g)).roundToInt())
            labelText.alpha = (255 * shown * (LABEL_DIM + (1 - LABEL_DIM) * g)).roundToInt()
            canvas.drawText(choices[i].short, x, y + 19 * dp, labelText)
        }
    }

    /* The second layer: a level glass pill per waiting agent, listed beyond the dial. Each
       slides in from behind the dial. The canvas is at the rings' centre. */
    private fun drawBars(canvas: Canvas) {
        for (i in bars.indices) {
            val q = barIn.getOrNull(i)?.x ?: continue
            val shown = q.coerceIn(0f, 1f)
            if (shown <= 0.001f) continue
            val b = bars[i]
            val g = barLit.getOrNull(i)?.x?.coerceIn(0f, 1f) ?: 0f
            val a = (255 * shown).roundToInt()
            // In from behind the dial: from nearer the rings, toward the open side.
            val slideBy = -way * (1 - q) * BAR_SLIDE_DP * dp
            val l = b.left + slideBy
            pill.set(l, b.y - barH / 2, l + b.w, b.y + barH / 2)
            // Lit glass: a touch brighter at the leading end, like the band.
            fill.style = Paint.Style.FILL
            fill.shader = LinearGradient(
                pill.left, 0f, pill.right, 0f,
                if (blurOn) 0x2EFFFFFF else 0x38FFFFFF, if (blurOn) 0x12FFFFFF else 0x1AFFFFFF,
                Shader.TileMode.CLAMP,
            )
            fill.alpha = a
            canvas.drawRoundRect(pill, barH / 2, barH / 2, fill)
            fill.shader = null
            fill.style = Paint.Style.STROKE
            fill.strokeWidth = (0.8f + 0.6f * g) * dp
            fill.color = blend(0x59FFFFFF, 0xF266DBAF.toInt(), g)
            fill.alpha = ((fill.color ushr 24) * shown).roundToInt()
            canvas.drawRoundRect(pill, barH / 2, barH / 2, fill)
            // The disc at the leading end, the same hairline glass, with its mark in off white.
            val dx = pill.left + barH / 2
            val dr = barH / 2 - 5 * dp
            fill.style = Paint.Style.FILL
            fill.color = 0x400B0A09
            fill.alpha = (0x40 * shown).roundToInt()
            canvas.drawCircle(dx, b.y, dr, fill)
            fill.style = Paint.Style.STROKE
            fill.strokeWidth = 0.8f * dp
            fill.color = 0x59FFFFFF
            fill.alpha = (0x59 * shown).roundToInt()
            canvas.drawCircle(dx, b.y, dr, fill)
            val mark = when (b.kind) {
                PERMISSION -> prompt
                MORE -> plus
                else -> question
            }
            drawIcon(canvas, mark, dx, b.y, 16 * dp, TEXT, (a * (0.85f + 0.15f * g)).roundToInt())
            // The two lines, beside the disc, level, cut to the room there is.
            val tx = pill.left + barH + 2 * dp
            val room = pill.right - barH / 2 - tx
            if (room > 12 * dp) {
                titleText.textAlign = Paint.Align.LEFT
                lineText.textAlign = Paint.Align.LEFT
                titleText.alpha = (255 * shown * (0.9f + 0.1f * g)).roundToInt()
                lineText.alpha = (255 * shown * 0.66f).roundToInt()
                val t1 = TextUtils.ellipsize(b.title, titleText, room, TextUtils.TruncateAt.END)
                val t2 = TextUtils.ellipsize(b.line, lineText, room, TextUtils.TruncateAt.END)
                canvas.drawText(t1, 0, t1.length, tx, b.y - 3 * dp, titleText)
                canvas.drawText(t2, 0, t2.length, tx, b.y + 11 * dp, lineText)
            }
        }
    }

    /* The Glass theme's segments: frosted glass, a clean light rim on every edge and a soft
       highlight just inside it; a glyph, its word and a small number. The active one lights its
       rim and glyph green, with a faint green glow inside. Each grows out of the hub as it
       settles. The canvas is at the rings' centre. */
    private fun drawSegments(canvas: Canvas) {
        val r = ring ?: return
        for (i in segments.indices) {
            if (i >= choices.size || i >= settle.size || i >= r.angles.size) break
            val p = settle[i].x
            val shown = p.coerceIn(0f, 1f)
            if (shown <= 0.001f) continue
            val g = if (glass2 && (i == gridOf || (choices[i].id == "voicenote" && voice?.recording == true))) 1f
            else lit[i].x.coerceIn(0f, 1f)
            val seg = segments[i]
            val scale = 0.72f + 0.28f * p
            canvas.withScale(scale, scale) {
                // Dark smoked glass in the console's surface colour, the home screen barely
                // showing through.
                fill.shader = null
                fill.style = Paint.Style.FILL
                fill.color = if (blurOn) GLASS_FILL_BLUR else GLASS_FILL
                fill.alpha = ((fill.color ushr 24) * shown).roundToInt()
                drawPath(seg.shape, fill)
                // A faint highlight from above and, when active, the green glow, both kept inside
                // the shape.
                withClip(seg.shape) {
                    fill.style = Paint.Style.FILL
                    fill.shader = topLight
                    fill.alpha = (255 * shown).roundToInt()
                    drawPath(seg.shape, fill)
                    fill.shader = null
                    fill.style = Paint.Style.STROKE
                    if (g > 0.01f) {
                        fill.strokeWidth = 22 * dp
                        fill.color = MINT
                        fill.alpha = (0x30 * g * shown).roundToInt()
                        drawPath(seg.shape, fill)
                    }
                }
                // The rim.
                fill.style = Paint.Style.STROKE
                fill.strokeWidth = (1f + 0.4f * g) * dp
                val rim = blend(GLASS_RIM, 0xF266DBAF.toInt(), g)
                fill.color = rim
                fill.alpha = ((rim ushr 24) * shown).roundToInt()
                drawPath(seg.shape, fill)
                // The glyph, the word, and the number under it.
                val rad = Math.toRadians(r.angles[i].toDouble())
                var x = (r.radius * cos(rad)).toFloat()
                val y = (r.radius * sin(rad)).toFloat()
                val c = choices[i]
                // Glass 2: icons only inside, larger and centred; the word of the segment under the
                // finger, or whose pills are open, floats just outside it, one at a time.
                if (glass2) {
                    val ix = if (way > 0) max(x, 22 * dp) else min(x, -22 * dp)
                    val open = i == gridOf
                    val rec = c.id == "voicenote" && voice?.recording == true
                    val gg = if (open || rec) 1f else g
                    val size = GLYPH_DP * 1.3f * dp
                    icon.strokeWidth = GLASS_STROKE
                    drawIcon(this, c.path, ix, y, size, blend(INK, MINT, gg), (255 * shown).roundToInt())
                    icon.strokeWidth = 1.5f
                    if (c.id == "call" && untold > 0) {
                        badgeText.alpha = (255 * shown).roundToInt()
                        drawText(untold.toString(), ix + way * (size / 2 + 6 * dp), y + 4 * dp, badgeText)
                    }
                    if (menuWords && (aimed == i || open || rec)) {
                        val word = if (rec) {
                            val sec = ((android.os.SystemClock.elapsedRealtime() - (voice?.startedAt ?: 0L)) / 1000).coerceAtLeast(0)
                            String.format(java.util.Locale.ROOT, "%d:%02d", sec / 60, sec % 60)
                        } else c.short
                        outsideWord(this, r.angles[i], word, rec, shown)
                    }
                    continue
                }
                if (glass2) {
                    // The top and bottom words stay whole beside the screen edge.
                    wordText.textSize = 12f * sp
                    val half = wordText.measureText(c.short) / 2 + 8 * dp
                    x = if (way > 0) max(x, half) else min(x, -half)
                }
                // Glass 2: the wedge whose pills are open stays lit; Voice note records in mint.
                val open = glass2 && i == gridOf
                val rec = glass2 && c.id == "voicenote" && voice?.recording == true
                val gg = if (open || rec) 1f else g
                icon.strokeWidth = GLASS_STROKE
                drawIcon(this, c.path, x, y - 12 * dp, GLYPH_DP * dp, blend(INK, MINT, gg),
                    (255 * shown).roundToInt())
                icon.strokeWidth = 1.5f
                if (glass2) wordText.textSize = 12f * sp
                val word = if (rec) {
                    val sec = ((android.os.SystemClock.elapsedRealtime() - (voice?.startedAt ?: 0L)) / 1000).coerceAtLeast(0)
                    String.format(java.util.Locale.ROOT, "%d:%02d", sec / 60, sec % 60)
                } else c.short
                wordText.color = if (rec) MINT else INK
                wordText.alpha = (255 * shown).roundToInt()
                drawText(word, x, y + 16 * dp, wordText)
                wordText.color = INK
                wordText.textSize = 13f * sp
                // The front desk's untold briefs: a small bare red numeral beside its glyph, none at zero.
                if (glass2 && c.id == "call" && untold > 0) {
                    badgeText.alpha = (255 * shown).roundToInt()
                    // Beside the glyph on the open side, level with it, clear of Needs you above.
                    drawText(untold.toString(), x + way * 19 * dp, y - 8 * dp, badgeText)
                }
                if (GLASS_HINTS) {
                    hintText.alpha = (255 * shown * 0.45f).roundToInt()
                    drawText((i + 1).toString(), x, y + 30 * dp, hintText)
                }
            }
        }
    }

    /* Glass 2: where a segment's floating word sits: level with the segment's middle, just past
       its outer edge at that height, kept inside the window. Its middle, in the hub's
       coordinates. */
    private fun wordAt(deg: Float): PointF {
        val sn = sin(Math.toRadians(deg.toDouble())).toFloat()
        val lr = gOut + 12 * dp
        var ly = sn * (gIn + gOut) / 2
        val lx = way * (sqrt(max(0f, lr * lr - ly * ly)) + 6 * dp)
        val insets = ViewCompat.getRootWindowInsets(this)
            ?.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
        val top = (insets?.top ?: 0) + pad + 16 * dp - cy
        val bottom = height - (insets?.bottom ?: 0) - pad - 16 * dp - cy
        ly = ly.coerceIn(top, bottom)
        return PointF(lx, ly)
    }

    /* Glass 2's floating word: white, level, on a very faint dark rounded backing with a soft
       shadow, so it holds on a bright wallpaper; the recording's time in mint. */
    private fun outsideWord(canvas: Canvas, deg: Float, word: String, rec: Boolean, shown: Float) {
        val at = wordAt(deg)
        outText.textAlign = if (way > 0) Paint.Align.LEFT else Paint.Align.RIGHT
        outText.color = if (rec) MINT else INK
        outText.alpha = (255 * shown).roundToInt()
        val w = outText.measureText(word)
        val bl = if (way > 0) at.x - 9 * dp else at.x - w - 9 * dp
        pill2.set(bl, at.y - 14 * dp, bl + w + 18 * dp, at.y + 14 * dp)
        fill.shader = null
        fill.style = Paint.Style.FILL
        fill.color = WORD_BACKING
        fill.alpha = ((WORD_BACKING ushr 24) * shown).roundToInt()
        canvas.drawRoundRect(pill2, 14 * dp, 14 * dp, fill)
        canvas.drawText(word, at.x, at.y - (outText.ascent() + outText.descent()) / 2, outText)
    }

    /* Glass 2: pill k of n, in the place of Glass's waiting pills: a level list on the open side
       beyond the segments, centred on the hub and kept inside the window. Into `pill`, in the
       hub's coordinates. */
    private fun pillRect(k: Int, n: Int) {
        val insets = ViewCompat.getRootWindowInsets(this)
            ?.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
        val top = (insets?.top ?: 0) + pad - cy
        val bottom = height - (insets?.bottom ?: 0) - pad - cy
        val room = if (way > 0) width - pad - (cx + barFrom) else cx - barFrom - pad
        val w = min(width * BAR_WIDTH, room).coerceAtLeast(0f)
        val total = n * barH + (n - 1) * barGap
        var first = -total / 2
        // The open segment's word floats beside the pills' start; the list moves up or down
        // just enough to keep clear of it.
        val r = ring
        if (menuWords && r != null && gridOf in r.angles.indices) {
            val wy = wordAt(r.angles[gridOf]).y
            val wTop = wy - 18 * dp
            val wBottom = wy + 18 * dp
            if (first < wBottom && first + total > wTop) {
                first = if (wy < 0) wBottom else wTop - total
            }
        }
        first = first.coerceIn(top, (bottom - total).coerceAtLeast(top))
        val left = if (way > 0) barFrom else -barFrom - w
        val t = first + k * (barH + barGap)
        pill.set(left, t, left + w, t + barH)
    }

    /* Glass 2's agent pills, only while Needs you or Recent is reached or pinned: dark glass as
       Glass's waiting pill, a monogram disc in the agent's own muted colour, its name, and for a
       waiting agent its mark and a dim ask. The pill under the finger lights mint. */
    private fun drawMenuPills(canvas: Canvas) {
        if (gridOf < 0) return
        val agents = outerAgents()
        for (k in agents.indices) {
            val a = agents[k]
            pillRect(k, agents.size)
            val lit = aimed == TILE0 + k
            val corner = barH / 2
            fill.shader = null
            fill.style = Paint.Style.FILL
            fill.color = if (lit) blend(GLASS_FILL, 0xF2123028.toInt(), 0.5f) else GLASS_FILL
            canvas.drawRoundRect(pill, corner, corner, fill)
            fill.style = Paint.Style.STROKE
            fill.strokeWidth = (if (lit) 1.5f else 1f) * dp
            fill.color = if (lit) 0xF266DBAF.toInt() else GLASS_RIM
            canvas.drawRoundRect(pill, corner, corner, fill)
            val dx = pill.left + barH / 2
            val cyP = pill.centerY()
            val tx = pill.left + barH + 4 * dp
            val room = pill.right - barH / 2 - tx
            if (a.kind == EMPTY) {
                dimLabel.textAlign = Paint.Align.LEFT
                canvas.drawText(a.title, pill.left + 18 * dp, cyP - (dimLabel.ascent() + dimLabel.descent()) / 2, dimLabel)
                continue
            }
            fill.style = Paint.Style.FILL
            fill.color = agentColour(a.title)
            canvas.drawCircle(dx, cyP, barH / 2 - 7 * dp, fill)
            monogramText.alpha = 255
            canvas.drawText(monogram(a.title), dx, cyP - (monogramText.ascent() + monogramText.descent()) / 2, monogramText)
            if (a.kind == PERMISSION || a.kind == QUESTION_KIND) {
                val mark = if (a.kind == PERMISSION) prompt else question
                drawIcon(canvas, mark, dx + 13 * dp, cyP + 12 * dp, 11 * dp, INK, 230)
            }
            titleText.textAlign = Paint.Align.LEFT
            titleText.color = INK
            titleText.alpha = 255
            if (a.line.isNotBlank()) {
                lineText.textAlign = Paint.Align.LEFT
                lineText.color = INK_DIM
                val t1 = TextUtils.ellipsize(a.title, titleText, room, TextUtils.TruncateAt.END)
                val t2 = TextUtils.ellipsize(a.line, lineText, room, TextUtils.TruncateAt.END)
                canvas.drawText(t1, 0, t1.length, tx, cyP - 3 * dp, titleText)
                canvas.drawText(t2, 0, t2.length, tx, cyP + 11 * dp, lineText)
                lineText.color = WHITE
            } else {
                val t1 = TextUtils.ellipsize(a.title, titleText, room, TextUtils.TruncateAt.END)
                canvas.drawText(t1, 0, t1.length, tx, cyP - (titleText.ascent() + titleText.descent()) / 2, titleText)
            }
            titleText.color = WHITE
        }
    }

    /* The Glass theme's pills: light glass with a clean rim, a glass disc with its mark, and the
       agent's name and ask; the first is Ask anything, with a return mark at its end. Each slides
       in from behind the segments. The canvas is at the rings' centre. */
    private fun drawGlassBars(canvas: Canvas) {
        for (i in bars.indices) {
            val q = barIn.getOrNull(i)?.x ?: continue
            val shown = q.coerceIn(0f, 1f)
            if (shown <= 0.001f) continue
            val b = bars[i]
            val g = barLit.getOrNull(i)?.x?.coerceIn(0f, 1f) ?: 0f
            val a = (255 * shown).roundToInt()
            val l = b.left - way * (1 - q) * BAR_SLIDE_DP * dp
            pill.set(l, b.y - barH / 2, l + b.w, b.y + barH / 2)
            val corner = barH / 2
            fill.shader = null
            fill.style = Paint.Style.FILL
            fill.color = if (blurOn) GLASS_FILL_BLUR else GLASS_FILL
            fill.alpha = ((fill.color ushr 24) * shown).roundToInt()
            canvas.drawRoundRect(pill, corner, corner, fill)
            fill.style = Paint.Style.STROKE
            fill.strokeWidth = (1f + 0.4f * g) * dp
            val rim = blend(GLASS_RIM, 0xF266DBAF.toInt(), g)
            fill.color = rim
            fill.alpha = ((rim ushr 24) * shown).roundToInt()
            canvas.drawRoundRect(pill, corner, corner, fill)
            // The disc at the leading end.
            val dx = pill.left + barH / 2
            val dr = barH / 2 - 6 * dp
            fill.style = Paint.Style.FILL
            fill.color = 0x0FFFFFFF
            fill.alpha = (0x0F * shown).roundToInt()
            canvas.drawCircle(dx, b.y, dr, fill)
            // A plain rim for every disc. The owner asked twice for no red on the waiting agents.
            fill.style = Paint.Style.STROKE
            fill.strokeWidth = dp
            fill.color = GLASS_RIM
            fill.alpha = ((GLASS_RIM ushr 24) * shown).roundToInt()
            canvas.drawCircle(dx, b.y, dr, fill)
            val mark = when (b.kind) {
                PERMISSION -> prompt
                MORE -> plus
                else -> question
            }
            drawIcon(canvas, mark, dx, b.y, 15 * dp, blend(INK, MINT, g), a)
            val tx = pill.left + barH + 2 * dp
            val room = pill.right - barH / 2 - tx
            if (room > 12 * dp) {
                titleText.textAlign = Paint.Align.LEFT
                lineText.textAlign = Paint.Align.LEFT
                titleText.color = INK
                lineText.color = INK_DIM
                titleText.alpha = (255 * shown).roundToInt()
                lineText.alpha = (255 * shown).roundToInt()
                val t1 = TextUtils.ellipsize(b.title, titleText, room, TextUtils.TruncateAt.END)
                val t2 = TextUtils.ellipsize(b.line, lineText, room, TextUtils.TruncateAt.END)
                canvas.drawText(t1, 0, t1.length, tx, b.y - 3 * dp, titleText)
                canvas.drawText(t2, 0, t2.length, tx, b.y + 11 * dp, lineText)
                titleText.color = WHITE
                lineText.color = WHITE
            }
        }
    }

    /* The Bubbles theme's discs: frosted smoke with a crisp light rim and a glossy sheen at the
       top, an icon over a short word. The picked one is darker, with a mint rim and a mint icon.
       Behind them, two faint thin arcs centred on the hub, like a dial's guides: one through the
       discs, one further out, each spanning the same angles above and below the hub's line. Each
       disc floats out of the hub as it settles, so the wallpaper shows through the gaps. The
       canvas is at the hub. */
    private fun drawBubbles(canvas: Canvas) {
        val r = ring ?: return
        val mid = if (way > 0) 0f else 180f
        val pLines = drawn(60f, 260f, 0f, 120f)
        if (pLines > 0.001f) {
            // The arcs run behind the discs, never through them: each disc is cut out first.
            holes.reset()
            for (i in r.angles.indices) {
                if (i >= settle.size) break
                val p = settle[i].x
                if (p <= 0.001f) continue
                val rad = Math.toRadians(r.angles[i].toDouble())
                holes.addCircle(
                    (r.radius * p * cos(rad)).toFloat(), (r.radius * p * sin(rad)).toFloat(),
                    bDisc * (0.6f + 0.4f * p) + dp, Path.Direction.CW,
                )
            }
            canvas.save()
            canvas.clipOutPath(holes)
            line.color = WHITE
            line.strokeCap = Paint.Cap.ROUND
            line.strokeWidth = 1.2f * dp
            line.alpha = (46 * pLines).roundToInt()
            oval.set(-bOrbit, -bOrbit, bOrbit, bOrbit)
            canvas.drawArc(oval, mid - B_ORBIT_HALF * pLines, 2 * B_ORBIT_HALF * pLines, false, line)
            line.strokeWidth = dp
            line.alpha = (30 * pLines).roundToInt()
            oval.set(-bOuter, -bOuter, bOuter, bOuter)
            canvas.drawArc(oval, mid - B_OUTER_HALF * pLines, 2 * B_OUTER_HALF * pLines, false, line)
            line.strokeCap = Paint.Cap.BUTT
            canvas.restore()
        }
        for (i in r.angles.indices) {
            if (i >= choices.size || i >= settle.size) break
            val p = settle[i].x
            val shown = p.coerceIn(0f, 1f)
            if (shown <= 0.001f) continue
            val g = lit[i].x.coerceIn(0f, 1f)
            val rad = Math.toRadians(r.angles[i].toDouble())
            val x = (r.radius * p * cos(rad)).toFloat()
            val y = (r.radius * p * sin(rad)).toFloat()
            val rr = bDisc * (0.6f + 0.4f * p) * (1 + 0.04f * g)
            fill.shader = null
            fill.style = Paint.Style.FILL
            fill.color = blend(if (blurOn) B_FILL_BLUR else B_FILL, B_FILL_PICKED, g)
            fill.alpha = ((fill.color ushr 24) * shown).roundToInt()
            canvas.drawCircle(x, y, rr, fill)
            // The sheen: glossy light from above, fading by the middle.
            canvas.withTranslation(x, y) {
                sheenFill.alpha = (255 * shown * (1 - 0.5f * g)).roundToInt()
                scale(rr / bDisc, rr / bDisc)
                drawCircle(0f, 0f, bDisc, sheenFill)
            }
            fill.style = Paint.Style.STROKE
            fill.strokeWidth = (1f + 0.5f * g) * dp
            val rim = blend(B_RIM, 0xF266DBAF.toInt(), g)
            fill.color = rim
            fill.alpha = ((rim ushr 24) * shown).roundToInt()
            canvas.drawCircle(x, y, rr, fill)
            icon.strokeWidth = GLASS_STROKE
            drawIcon(canvas, choices[i].path, x, y - 9 * dp, 26 * dp * (0.8f + 0.2f * p), blend(INK, MINT, g),
                (255 * shown).roundToInt())
            icon.strokeWidth = 1.5f
            wordText.alpha = (255 * shown).roundToInt()
            canvas.drawText(choices[i].short, x, y + 20 * dp, wordText)
        }
    }

    /* The Bubbles theme's hub: first the bubble, then layered glass. A big dark smoked disc with a
       thin light rim and a darker inner ring, and in it a small solid black disc with a white X.
       The X turns mint when the finger is on it. */
    private fun drawBubbleHub(canvas: Canvas, x: Float, y: Float) {
        val m = morph.x.coerceIn(0f, 1f)
        val gb = litBack.x.coerceIn(0f, 1f)
        fill.shader = null
        if (m > 0.01f) {
            canvas.withTranslation(x, y) {
                val s = 0.6f + 0.4f * m
                scale(s, s)
                hubGlass.alpha = (255 * m).roundToInt()
                drawCircle(0f, 0f, bRing, hubGlass)
                fill.style = Paint.Style.STROKE
                // The darker inner ring, then the thin light rim.
                fill.strokeWidth = 3 * dp
                fill.color = 0x59000000
                fill.alpha = (0x59 * m).roundToInt()
                drawCircle(0f, 0f, bRing - 9 * dp, fill)
                fill.strokeWidth = dp
                fill.color = blend(0x40FFFFFF, 0xCC66DBAF.toInt(), gb)
                fill.alpha = ((fill.color ushr 24) * m).roundToInt()
                drawCircle(0f, 0f, bRing, fill)
            }
        }
        val r = (bubbleR + (bHub - bubbleR) * morph.x) * (1 + 0.04f * gb)
        fill.style = Paint.Style.FILL
        fill.color = blend(STONE_RAISED, 0xFA000000.toInt(), m)
        canvas.drawCircle(x, y, r, fill)
        fill.style = Paint.Style.STROKE
        fill.strokeWidth = dp
        fill.color = blend(LINE, 0x1AFFFFFF, m)
        canvas.drawCircle(x, y, r, fill)
        val out = (1 - m / 0.5f).coerceIn(0f, 1f)
        val into = ((m - 0.35f) / 0.65f).coerceIn(0f, 1f)
        house?.let { d ->
            if (out > 0.01f) {
                val half = (15 * dp).roundToInt()
                d.setBounds(x.roundToInt() - half, y.roundToInt() - half, x.roundToInt() + half, y.roundToInt() + half)
                d.alpha = (out * 255).roundToInt()
                d.draw(canvas)
            }
        }
        if (into <= 0.01f) return
        val arm = 8.5f * dp * (0.8f + 0.2f * into)
        fill.strokeWidth = 2 * dp
        fill.strokeCap = Paint.Cap.ROUND
        fill.color = blend(WHITE, MINT, gb)
        fill.alpha = (into * 255).roundToInt()
        canvas.drawLine(x - arm, y - arm, x + arm, y + arm, fill)
        canvas.drawLine(x - arm, y + arm, x + arm, y - arm, fill)
        fill.strokeCap = Paint.Cap.BUTT
    }

    /* The Glass theme's hub: first the bubble, then dark smoked glass with a 1dp rim, sitting on
       the edge line, so half of it shows. Its off white X sits in the half on screen, and turns
       green when the finger is on it. */
    private fun drawGlassHub(canvas: Canvas, x: Float, y: Float) {
        val m = morph.x.coerceIn(0f, 1f)
        val gb = litBack.x.coerceIn(0f, 1f)
        val r = (bubbleR + (gHub - bubbleR) * morph.x) * (1 + 0.04f * gb)
        fill.shader = null
        fill.style = Paint.Style.FILL
        fill.color = blend(STONE_RAISED, if (blurOn) GLASS_FILL_BLUR else GLASS_FILL, m)
        canvas.drawCircle(x, y, r, fill)
        fill.style = Paint.Style.STROKE
        fill.strokeWidth = dp
        fill.color = blend(LINE, blend(GLASS_RIM, 0xF266DBAF.toInt(), gb), m)
        canvas.drawCircle(x, y, r, fill)
        val out = (1 - m / 0.5f).coerceIn(0f, 1f)
        val into = ((m - 0.35f) / 0.65f).coerceIn(0f, 1f)
        // The house sits on the bubble while it morphs into the hub.
        house?.let { d ->
            if (out > 0.01f) {
                val half = (15 * dp).roundToInt()
                d.setBounds(x.roundToInt() - half, y.roundToInt() - half, x.roundToInt() + half, y.roundToInt() + half)
                d.alpha = (out * 255).roundToInt()
                d.draw(canvas)
            }
        }
        // No mark on the hub: the owner wants it bare. It still closes the wheel on a tap, and
        // its rim still turns green under the finger.
    }

    /* The hub: first the bubble, then round glass with a red X on it, and during a call a small
       LIVE under the X. */
    private fun drawHub(canvas: Canvas, x: Float, y: Float) {
        val m = morph.x.coerceIn(0f, 1f)
        val gb = litBack.x.coerceIn(0f, 1f)
        val r = (bubbleR + (hubR - bubbleR) * morph.x) * (1 + 0.05f * gb)
        fill.shader = null
        fill.style = Paint.Style.FILL
        fill.color = blend(STONE_RAISED, if (blurOn) 0x990E0D0C.toInt() else 0xE60E0D0C.toInt(), m)
        canvas.drawCircle(x, y, r, fill)
        // A little light in the hub's glass too.
        fill.color = 0x14FFFFFF
        fill.alpha = (0x14 * m).roundToInt()
        canvas.drawCircle(x, y, r, fill)
        fill.style = Paint.Style.STROKE
        fill.strokeWidth = dp
        fill.color = blend(LINE, blend(0x4DFFFFFF, 0xE6E25958.toInt(), gb), m)
        canvas.drawCircle(x, y, r, fill)
        // The house fades out over the first part; the X fades in over the rest.
        val out = (1 - m / 0.5f).coerceIn(0f, 1f)
        val into = ((m - 0.35f) / 0.65f).coerceIn(0f, 1f)
        house?.let { d ->
            if (out > 0.01f) {
                val half = (15 * dp).roundToInt()
                d.setBounds(x.roundToInt() - half, y.roundToInt() - half, x.roundToInt() + half, y.roundToInt() + half)
                d.alpha = (out * 255).roundToInt()
                d.draw(canvas)
            }
        }
        if (into <= 0.01f) return
        val a = (into * 255).roundToInt()
        val xy = if (onCall) y - 4 * dp else y
        val arm = 7.5f * dp * (0.8f + 0.2f * into)
        fill.strokeWidth = 2 * dp
        fill.strokeCap = Paint.Cap.ROUND
        fill.color = RED
        fill.alpha = a
        canvas.drawLine(x - arm, xy - arm, x + arm, xy + arm, fill)
        canvas.drawLine(x - arm, xy + arm, x + arm, xy - arm, fill)
        fill.strokeCap = Paint.Cap.BUTT
        if (onCall) {
            monoText.alpha = (a * 0.8f).roundToInt()
            canvas.drawText("LIVE", x, y + 18 * dp, monoText)
        }
    }

    private fun drawIcon(canvas: Canvas, path: Path, x: Float, y: Float, size: Float, tint: Int, a: Int) {
        icon.color = tint
        icon.alpha = a
        // A stroke of 1.5 on the 24 unit square, so 1.5 dp at a 24 dp glyph.
        canvas.withTranslation(x - size / 2, y - size / 2) {
            scale(size / 24, size / 24)
            drawPath(path, icon)
        }
    }

    /* A colour between two, by t from 0 to 1, alpha included. */
    private fun blend(from: Int, to: Int, t: Float): Int {
        fun ch(shift: Int): Int {
            val f = from ushr shift and 0xFF
            val o = to ushr shift and 0xFF
            return (f + (o - f) * t).roundToInt().coerceIn(0, 255)
        }
        return (ch(24) shl 24) or (ch(16) shl 16) or (ch(8) shl 8) or ch(0)
    }

    /* A damped spring on one number, mass 1, stepped in small fixed steps so it stays stable. */
    private class Spring(var target: Float) {
        var x = 0f
        private var v = 0f
        private var k = LIT_K
        private var c = 2 * LIT_Z * sqrt(LIT_K)

        fun set(to: Float, stiffness: Float, damping: Float) {
            target = to
            k = stiffness
            c = 2 * damping * sqrt(stiffness)
        }

        fun jump(value: Float) {
            target = value
            x = value
            v = 0f
        }

        fun step(dt: Float) {
            var left = dt
            while (left > 0f) {
                val h = min(left, 0.004f)
                v += (-k * (x - target) - c * v) * h
                x += v * h
                left -= h
            }
            if (still()) {
                x = target
                v = 0f
            }
        }

        fun still() = abs(x - target) < 0.002f && abs(v) < 0.02f
    }

    companion object {
        private const val TAG = "CCWheel"

        private var grainBitmap: android.graphics.Bitmap? = null

        /* A 96 pixel tile of light and dark specks at a few percent, the same every time. */
        private fun grainTile(): android.graphics.Bitmap = grainBitmap ?: run {
            val n = 96
            val rnd = java.util.Random(7)
            val px = IntArray(n * n) {
                val v = rnd.nextInt(256)
                when {
                    v < 40 -> 0x14FFFFFF
                    v < 70 -> 0x0A000000
                    v < 90 -> 0x0CFFFFFF
                    else -> 0
                }
            }
            android.graphics.Bitmap.createBitmap(px, n, n, android.graphics.Bitmap.Config.ARGB_8888)
                .also { grainBitmap = it }
        }

        /* Debug builds only: where the open wheel throws next ("draw", "frame", "touch"), or
           "stall" to stop its frames. The release build never reads it. */
        @Volatile
        var testFault: String? = null

        /* Inter for every word, JetBrains Mono Light for the LIVE readout; each loaded once for
           the life of the process. Both are OFL; their licences are in assets/licenses. */
        private var interFace: Typeface? = null
        private var monoFace: Typeface? = null

        private fun inter(c: Context) =
            interFace ?: ResourcesCompat.getFont(c, R.font.inter_medium).also { interFace = it }
        private fun mono(c: Context) =
            monoFace ?: ResourcesCompat.getFont(c, R.font.jetbrains_mono_light).also { monoFace = it }

        const val STONE_RAISED = 0xFF161412.toInt()
        const val LINE = 0xFF2A2724.toInt()
        const val TEXT = 0xFFECE8E1.toInt()
        const val MINT = 0xFF66DBAF.toInt()
        private const val WHITE = 0xFFFFFFFF.toInt()
        /* The owner's red, used only for the HUD hub's X. */
        private const val RED = 0xFFE25958.toInt()

        /* An idle choice's light; the active one is full white. */
        private const val GLYPH_DIM = 0.74f
        private const val LABEL_DIM = 0.84f
        private const val GLYPH_DP = 28f
        private const val GLOW_R = 52f

        private const val NONE = -1
        private const val BACK = -2
        private const val BAR0 = 1000

        /* The bars: how many before "+N more", their spacing, and how far the fan may turn. */
        private const val MAX_BARS = 4
        /* The widest a bar may be, as a share of the screen, and how far it slides in. */
        private const val BAR_WIDTH = 0.6f
        private const val BAR_SLIDE_DP = 36f
        private const val BAR_STAGGER = 40f
        private const val QUESTION_KIND = 0
        private const val PERMISSION = 1
        private const val MORE = 2
        /* The Dial: its cards' kinds and index base, the most cards, its colours, and how long
           the finger rests on Voice note before it records. */
        private const val NAME = 4
        private const val EMPTY = 5
        private const val TILE0 = 2000
        private const val MAX_TILES = 4
        private const val D_WEDGE = 0xD91C1A1C.toInt()
        private const val D_ICON = 0.65f
        private const val VOICE_REST_MS = 350L
        /* The very faint dark backing under Glass 2's floating word: #0f0e0d at 45%. */
        private const val WORD_BACKING = 0x730F0E0D
        /* The agents' muted colours on charcoal: teal, amber, violet, sky, sand, sage. */
        private val AGENT_PALETTE = intArrayOf(
            0xFF5E9E97.toInt(), 0xFFC49A52.toInt(), 0xFF9486C0.toInt(),
            0xFF6F9FC4.toInt(), 0xFFC2AE8A.toInt(), 0xFF8FA886.toInt(),
        )

        /* The Glass theme: its glyphs' stroke on the 24 unit square (1.8 dp at 28 dp), and whether
           each segment carries its small number. */
        private const val GLASS_STROKE = 1.55f

        /* The Glass theme's palette, from the console (web-react src/styles/app.css): the stone
           surface as smoked glass, its text and dim text, a quiet rim, and how far the edge cuts
           past the half turn. */
        private const val GLASS_FILL = 0xC7171514.toInt()
        private const val GLASS_FILL_BLUR = 0xB8171514.toInt()
        private const val GLASS_RIM = 0x29FFFFFF
        private const val INK = 0xFFE8E3DC.toInt()
        private const val INK_DIM = 0xFFA8A29E.toInt()
        private const val G_PAST = 14f

        /* The Bubbles theme: how far the discs spread, their frosted smoke, the picked one's
           darker fill, and their thin light rim. */
        private const val B_SPREAD = 160f
        /* Half the span of the orbit through the discs, and of the outer arc, in degrees. */
        private const val B_ORBIT_HALF = 100f
        private const val B_OUTER_HALF = 62f
        private const val B_FILL = 0x8C3A3734.toInt()
        private const val B_FILL_BLUR = 0x733A3734
        private const val B_FILL_PICKED = 0xD90F0E0D.toInt()
        private const val B_RIM = 0x73FFFFFF
        private const val GLASS_HINTS = false

        /* The degrees the choices spread over, the green arc's length, and the fine ticks'
           spacing. */
        private const val CHOICE_SPREAD = 172f
        private const val ACCENT_DEG = 22f
        private const val TICK_DEG = 4f

        /* The inner broken arcs, as offsets from the middle of the sweep in degrees. */
        private val INNER_A = floatArrayOf(-86f, -40f, -30f, 12f, 24f, 70f)
        private val INNER_B = floatArrayOf(-64f, -16f, 4f, 30f, 44f, 80f)
        private const val SPIN_DEG_PER_S = 5f

        /* The springs: stiffness, and damping as a ratio of critical. */
        private const val SETTLE_K = 900f
        private const val SETTLE_Z = 0.8f
        private const val CLOSE_K = 1400f
        private const val CLOSE_Z = 1f
        private const val LIT_K = 900f
        private const val LIT_Z = 0.8f
        private const val ACCENT_K = 520f
        private const val ACCENT_Z = 0.78f
        private const val MORPH_K = 900f
        private const val MORPH_Z = 1f

        /* The timeline, in milliseconds at the normal animator scale. */
        private const val GLYPH_DELAY = 110f
        private const val OPEN_STAGGER = 30f
        private const val CLOSE_STAGGER = 15f
        private const val OPEN_END_MS = 340f
        private const val CLOSE_END_MS = 220f
        private const val BACKDROP_IN_MS = 180f
        private const val BACKDROP_OUT_MS = 200f

        /* The bars' marks, on a 24 unit square: a question, a command prompt, and a plus. */
        private const val QUESTION = "M9 9.2a3 3 0 1 1 4.4 2.7c-.9.5-1.4 1.2-1.4 2.1v.8M12 17.6v.2"
        private const val PROMPT = "M6.5 8.5l3.5 3.5-3.5 3.5M12 16h5.5"
        private const val PLUS = "M12 6.5v11M6.5 12h11"
    }
}
