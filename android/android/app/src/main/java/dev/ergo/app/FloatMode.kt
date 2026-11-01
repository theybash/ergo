package dev.ergo.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.RectF
import android.graphics.Rect
import android.view.HapticFeedbackConstants
import android.view.VelocityTracker
import androidx.dynamicanimation.animation.FloatValueHolder
import androidx.dynamicanimation.animation.SpringAnimation
import androidx.dynamicanimation.animation.SpringForce
import android.graphics.drawable.GradientDrawable
import android.media.AudioAttributes
import android.media.SoundPool
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.animation.ValueAnimator
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.text.TextUtils
import android.util.TypedValue
import android.view.animation.DecelerateInterpolator
import androidx.core.content.res.ResourcesCompat
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.min
import kotlin.math.roundToInt

/* Float mode: agents' faces on an edge over any app, and their chat in a window beside it
   (The owner, 31 October 2026: "I can work while playing Minecraft").

   Floated agents share one bubble on an edge, as Messenger's chat heads (The owner, 31 October:
   "Facebook does that. Can you copy Facebook?"). One agent shows his own face; two or more show
   the Ergo mark ("if there are two or more, let it be the icon be the logo"). A thin grey ring
   turns while any of them works. When a turn ends the agent goes to the top of the pile, a small
   red count shows and his chime plays, mixed over the game's sound, never ducking it. Nothing
   opens by itself.

   A tap on the bubble opens the chat: docked to the bubble's side in landscape and to the bottom
   in portrait, a third of the screen, and its grip resizes it from a sixth to two thirds. With two
   or more, their faces line up in a row over the chat; a tap on one opens its chat in the same
   window, a tap on the open one folds it back into the bubble.

   The bubble moves as a Telegram bubble ("let the gravity and physics be like telegram bubble"):
   drag it anywhere, let go and a spring throws it to the nearer side edge, keeping the throw's
   speed. While anything is dragged a round x shows at the bottom and catches it when near. The
   bubble dropped there ends float mode; a face dragged out of the row and dropped there lets only
   that agent go. A long press on the bubble floats another agent; on a face in the row, it puts
   another agent in its place.

   The faces never take focus, so the game under them keeps its focus and goes on. The chat window
   takes it only while it is touched or typed in; a touch outside gives it back. */
class FloatMode(private val service: CommandService) {

    private val ctx: Context = service
    private val wm = ctx.getSystemService(WindowManager::class.java)
    private val dp = ctx.resources.displayMetrics.density
    private val prefs = ctx.getSharedPreferences("float", Context.MODE_PRIVATE)
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private val slop = ViewConfiguration.get(ctx).scaledTouchSlop

    private val agents = ArrayList<String>()
    private val names = HashMap<String, String>()
    private val unread = LinkedHashSet<String>()
    private var working: Map<String, Boolean> = emptyMap()

    private var panel: Panel? = null
    /* The chat window while it is closed: kept, with its page loaded and live, so a tap on the
       bubble opens it at once (The owner, 31 October 2026: "the chats take time to load. Can we keep
       em live in bg"). One page serves every floated agent; a switch only changes its address.
       It goes when float mode ends. */
    private var parked: Panel? = null
    private var picker: View? = null
    // The last agent list, so the picker opens full at once and only corrects itself after.
    private var roster: List<Pair<String, String>>? = null
    private val inter = try { ResourcesCompat.getFont(ctx, R.font.inter_medium) } catch (_: Throwable) { null }

    private val sounds = SoundPool.Builder().setMaxStreams(1).setAudioAttributes(
        AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ASSISTANCE_SONIFICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build(),
    ).build()
    private val chimes = HashMap<String, Int>()

    init {
        prefs.getString("agents", "").orEmpty().split(',').filter { it.isNotBlank() }.forEach { agents.add(it) }
        try {
            val n = JSONObject(prefs.getString("names", "{}").orEmpty())
            for (k in n.keys()) names[k] = n.optString(k)
        } catch (_: Throwable) { }
        prefs.getString("unread", "").orEmpty().split(',').filter { it.isNotBlank() && it in agents }.forEach { unread.add(it) }
    }

    fun active() = agents.isNotEmpty()

    fun floated(id: String) = id in agents

    /* At the service's start: the bubble that was up comes back. */
    fun restore() = guard { if (agents.isNotEmpty()) { showBubble(); preload() } }

    /* Float this agent: it joins the top of the pile, or stays where it is. */
    fun add(id: String, name: String?): Unit = guard {
        // The app's Float button calls in on Capacitor's own thread. A window belongs to the
        // thread that made it, and a face loading later touches it from the main one: the app
        // crashed on the second float. So every window is made and touched on the main thread.
        if (Looper.myLooper() != Looper.getMainLooper()) { main.post { add(id, name) }; return@guard }
        if (id.isBlank()) return@guard
        if (!Settings.canDrawOverlays(ctx)) {
            Log.i(TAG, "no overlay permission, nothing to float")
            return@guard
        }
        if (!name.isNullOrBlank()) names[id] = name
        if (id !in agents) agents.add(0, id)
        save()
        FaceView.fetch(id, io, main) { redraw() }
        showBubble()
        changed()
        preload()
    }

    /* The chat page loads as soon as anyone floats, before the first tap. */
    private fun preload() {
        if (panel != null || parked != null || agents.isEmpty()) return
        val p = Panel(agents[0])
        p.hidden(true)
        parked = p
        wm.addView(p, p.params)
    }

    fun remove(id: String) = guard {
        agents.remove(id)
        unread.remove(id)
        save()
        if (agents.isEmpty()) { hideAll(); return@guard }
        if (panel?.agent == id) panel?.switchTo(agents[0])
        if (parked?.agent == id) parked?.switchTo(agents[0])
        changed()
    }

    private fun swap(old: String, id: String, name: String) = guard {
        val i = agents.indexOf(old)
        if (i < 0) return@guard
        if (id in agents) agents.remove(old) else agents[i] = id
        unread.remove(old)
        names[id] = name
        save()
        FaceView.fetch(id, io, main) { redraw() }
        if (panel?.agent == old) panel?.switchTo(id)
        if (parked?.agent == old) parked?.switchTo(id)
        changed()
    }

    /* Each digest: who works now. A floated agent whose work just stopped has finished its turn:
       its red 1 shows, unless its chat is the one open. The row keeps its order, so a face never
       moves under his finger; a tap on the bubble opens the first that finished. */
    fun saw(now: Map<String, Boolean>) = guard {
        val before = working
        working = now
        var done = false
        for (id in agents) {
            if (before[id] == true && now[id] == false && panel?.agent != id) {
                unread.add(id)
                done = true
            }
        }
        if (done) {
            save()
            chime()
        }
        redraw()
    }

    /* The phone turned: the window takes the other shape, and the bubble its edge. */
    fun relayout() = guard {
        panel?.let { p -> p.shape(); try { wm.updateViewLayout(p, p.params) } catch (_: Throwable) { } }
        fitBubble()
    }

    fun hideAll() {
        closePicker()
        closePanel()
        parked?.let { dispose(it) }
        parked = null
        drop?.end()
        drop = null
        bubble?.let { safeRemove(it) }
        bubble = null
    }

    /* The pile changed: the bubble and the row in the chat follow. */
    private fun changed() {
        fitBubble()
        panel?.rowChanged()
    }

    private fun redraw() {
        bubble?.invalidate()
        panel?.row?.invalidate()
    }

    // -- drawing a face, or the Ergo mark ---------------------------------------------------------

    private val face = (46 * dp).roundToInt()
    private val gap = (10 * dp).roundToInt()
    private val pad = (6 * dp).roundToInt()

    private val clip = Path()
    private val grey = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = 2.5f * dp; color = 0xFFA8A29E.toInt(); strokeCap = Paint.Cap.ROUND }
    private val track = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = 2.5f * dp; color = 0xFF2A2725.toInt() }
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFF2A2725.toInt() }
    private val plate = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFF1C1A18.toInt() }
    private val ink = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFFE8E3DC.toInt(); textAlign = Paint.Align.CENTER }
    private val red = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFFE25958.toInt(); textSize = 13 * dp; isFakeBoldText = true; textAlign = Paint.Align.CENTER }
    private val chosen = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = 2 * dp; color = 0xFFE8E3DC.toInt() }

    // The launcher icon's ring layer: the Ergo mark, drawn on a dark plate.
    private val mark: android.graphics.Bitmap? by lazy {
        try { android.graphics.BitmapFactory.decodeResource(ctx.resources, R.mipmap.ic_launcher_foreground) } catch (_: Throwable) { null }
    }

    /* One face (id), or the Ergo mark (id null), with its ring while working and its red count.
       Returns whether anything turns, so the view knows to draw again. */
    private fun drawFace(c: Canvas, cx: Float, cy: Float, id: String?, spinning: Boolean, count: Int, on: Boolean = false): Boolean {
        val r = face / 2f - 3 * dp
        val dst = RectF(cx - r, cy - r, cx + r, cy + r)
        if (id == null) {
            c.drawCircle(cx, cy, r, plate)
            mark?.let { b ->
                // The icon layer is drawn on a 108 unit canvas; the mark fills its middle 66.
                val k = r * 108f / 58f
                c.save(); clip.reset(); clip.addCircle(cx, cy, r, Path.Direction.CW); c.clipPath(clip)
                c.drawBitmap(b, null, RectF(cx - k, cy - k, cx + k, cy + k), null)
                c.restore()
            }
        } else {
            val bmp = FaceView.faces[id]
            if (bmp != null) {
                c.save(); clip.reset(); clip.addCircle(cx, cy, r, Path.Direction.CW); c.clipPath(clip)
                c.drawBitmap(bmp, null, dst, null)
                c.restore()
            } else {
                c.drawCircle(cx, cy, r, fill)
                ink.textSize = r * 0.9f
                c.drawText((names[id] ?: id).take(1).uppercase(), cx, cy - (ink.descent() + ink.ascent()) / 2f, ink)
            }
        }
        val ring = RectF(cx - r - 1.5f * dp, cy - r - 1.5f * dp, cx + r + 1.5f * dp, cy + r + 1.5f * dp)
        if (on) c.drawOval(RectF(ring.left - 1.5f * dp, ring.top - 1.5f * dp, ring.right + 1.5f * dp, ring.bottom + 1.5f * dp), chosen)
        if (spinning) {
            val spin = (SystemClock.uptimeMillis() % 1200) / 1200f * 360f
            c.drawOval(ring, track)
            c.drawArc(ring, spin, 90f, false, grey)
        } else if (count > 0) {
            c.drawText(count.toString(), cx - r + 1 * dp, cy - r + 6 * dp, red)
        }
        return spinning
    }

    // -- the bubble: one face, or the Ergo mark for two or more -----------------------------------

    private var bubble: Bubble? = null
    private var bubbleParams: WindowManager.LayoutParams? = null

    private fun showBubble() {
        if (bubble != null || agents.isEmpty()) return
        val v = Bubble()
        val p = overlayParams(face + 2 * pad, face + 2 * pad,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS).apply {
            title = "ergo float bubble"
        }
        bubbleParams = p
        bubble = v
        place(p)
        wm.addView(v, p)
        for (id in agents) FaceView.fetch(id, io, main) { redraw() }
    }

    /* On the edge he last threw it to, at the height he left it, inside the safe area: the screen
       less its notch and system bars, so a full-screen game never hides half a face. */
    private fun place(p: WindowManager.LayoutParams) {
        val s = safe()
        p.x = if (onRight()) s.right - p.width else s.left
        val want = s.top + (prefs.getFloat(yKey(), 0.35f) * s.height()).roundToInt()
        p.y = want.coerceIn(s.top, (s.bottom - p.height).coerceAtLeast(s.top))
    }

    private fun onRight() = prefs.getString("side", "R") != "L"

    private fun yKey() = if (landscape()) "yL" else "yP"

    private fun fitBubble() {
        val v = bubble ?: return
        val p = bubbleParams ?: return
        stopSprings()
        place(p)
        v.visibility = if (panel != null) View.GONE else View.VISIBLE
        v.alpha = 1f
        move(v, p)
        v.invalidate()
    }

    // -- the throw: a spring carries the bubble to the nearer edge, as a Telegram bubble ----------

    private var springX: SpringAnimation? = null
    private var springY: SpringAnimation? = null

    private fun stopSprings() {
        springX?.cancel(); springY?.cancel()
        springX = null; springY = null
    }

    /* Let go with a speed: the throw decides the side, the spring keeps the speed and settles. */
    private fun settle(vx: Float, vy: Float) {
        val v = bubble ?: return
        val p = bubbleParams ?: return
        val s = safe()
        val right = p.x + p.width / 2f + vx * THROW_S > s.exactCenterX()
        val tx = (if (right) s.right - p.width else s.left).toFloat()
        val ty = (p.y + vy * THROW_S * 0.6f).coerceIn(s.top.toFloat(), (s.bottom - p.height).coerceAtLeast(s.top).toFloat())
        prefs.edit {
            putString("side", if (right) "R" else "L")
            putFloat(yKey(), (ty - s.top) / s.height().coerceAtLeast(1))
        }
        stopSprings()
        springX = spring(p.x.toFloat(), tx, vx) { p.x = it.roundToInt(); move(v, p) }
        springY = spring(p.y.toFloat(), ty, vy) { p.y = it.roundToInt(); move(v, p) }
    }

    private fun spring(from: Float, to: Float, speed: Float, set: (Float) -> Unit) =
        SpringAnimation(FloatValueHolder(from)).apply {
            spring = SpringForce(to).setStiffness(STIFFNESS).setDampingRatio(DAMPING)
            setStartVelocity(speed)
            addUpdateListener { _, value, _ -> set(value) }
            start()
        }

    private fun move(v: View, p: WindowManager.LayoutParams) {
        try { if (v.parent != null) wm.updateViewLayout(v, p) } catch (_: Throwable) { }
    }

    // -- a drag toward the x: the x at the bottom, and a face that follows the finger ------------

    private var drop: Drop? = null

    /* Made fresh for each drag, so both windows sit over everything else, the chat included. The
       face that follows the finger is its own window: the bubble or the row stays where it is. */
    private inner class Drop(val id: String?) {
        private val size = (96 * dp).roundToInt()
        private val t = Target()
        private val tp = overlayParams(size, size, NOT_TOUCH)
        private val g = Ghost(id)
        private val gp = overlayParams(face + 2 * pad, face + 2 * pad, NOT_TOUCH)
        var caught = false
            private set

        init {
            val s = safe()
            tp.title = "ergo float target"
            tp.x = s.centerX() - size / 2
            tp.y = s.bottom - size - (28 * dp).roundToInt()
            t.alpha = 0f
            wm.addView(t, tp)
            t.animate().alpha(1f).setDuration(150).start()
            gp.title = "ergo float drag"
            wm.addView(g, gp)
        }

        /* The finger moved: the face follows, or jumps into the x when it is near. */
        fun follow(x: Float, y: Float, haptic: View) {
            val cx = tp.x + size / 2f
            val cy = tp.y + size / 2f
            val near = hypot(x - cx, y - cy) < MAGNET_DP * dp
            if (near && !caught) haptic.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
            caught = near
            t.hot = near
            val at = if (near) cx to cy else x to y
            gp.x = (at.first - gp.width / 2f).roundToInt()
            gp.y = (at.second - gp.height / 2f).roundToInt()
            move(g, gp)
        }

        fun where() = gp.x to gp.y

        fun end() {
            safeRemove(g)
            t.animate().alpha(0f).setDuration(120).withEndAction { safeRemove(t) }.start()
        }
    }

    private inner class Target : View(ctx) {
        var hot = false
            set(v) {
                if (field == v) return
                field = v
                animate().scaleX(if (v) 1.22f else 1f).scaleY(if (v) 1.22f else 1f).setDuration(120).start()
            }
        private val disc = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xE61C1917.toInt() }
        private val rim = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = 1.5f * dp; color = 0x4DFFFFFF }
        private val x = Paint(Paint.ANTI_ALIAS_FLAG).apply { strokeWidth = 2.5f * dp; strokeCap = Paint.Cap.ROUND; color = 0xFFE8E3DC.toInt() }

        override fun onDraw(c: Canvas) {
            val m = width / 2f
            val r = 30 * dp
            c.drawCircle(m, m, r, disc)
            c.drawCircle(m, m, r, rim)
            val a = 8 * dp
            c.drawLine(m - a, m - a, m + a, m + a, x)
            c.drawLine(m - a, m + a, m + a, m - a, x)
        }
    }

    private inner class Ghost(val id: String?) : View(ctx) {
        override fun onDraw(c: Canvas) {
            drawFace(c, width / 2f, height / 2f, id, false, 0)
        }
    }

    // The window measures speed on the screen, not in the view, since the view may move.
    private fun track(t: VelocityTracker?, e: MotionEvent) {
        val m = MotionEvent.obtain(e)
        m.setLocation(e.rawX, e.rawY)
        t?.addMovement(m)
        m.recycle()
    }

    private inner class Bubble : View(ctx) {
        private var downX = 0f
        private var downY = 0f
        private var mode = 0     // 0 idle, 1 pending, 2 dragging
        private var speed: VelocityTracker? = null
        private val longPress = Runnable { if (mode == 1) { mode = 0; openPicker(null) } }

        override fun onDraw(c: Canvas) {
            val one = agents.size == 1
            val spinning = agents.any { working[it] == true }
            if (drawFace(c, width / 2f, height / 2f, if (one) agents[0] else null, spinning, unread.size)) postInvalidateOnAnimation()
        }

        @SuppressLint("ClickableViewAccessibility")
        override fun onTouchEvent(e: MotionEvent): Boolean {
            val p = bubbleParams ?: return false
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    stopSprings()
                    downX = e.rawX; downY = e.rawY
                    mode = 1
                    speed?.recycle()
                    speed = VelocityTracker.obtain()
                    track(speed, e)
                    postDelayed(longPress, LONG_MS)
                }
                MotionEvent.ACTION_MOVE -> {
                    track(speed, e)
                    if (mode == 1 && (abs(e.rawX - downX) > slop || abs(e.rawY - downY) > slop)) {
                        removeCallbacks(longPress)
                        mode = 2
                        guard { drop = Drop(if (agents.size == 1) agents[0] else null) }
                        alpha = 0f
                    }
                    if (mode == 2) drop?.follow(e.rawX, e.rawY, this)
                }
                MotionEvent.ACTION_UP -> {
                    removeCallbacks(longPress)
                    track(speed, e)
                    if (mode == 1) { performClick(); openPanel(unread.firstOrNull { it in agents } ?: agents[0]) }
                    if (mode == 2) {
                        val d = drop
                        drop = null
                        d?.end()
                        if (d?.caught == true) {
                            // The whole bubble to the x: float mode ends.
                            agents.clear(); unread.clear(); save(); hideAll()
                        } else {
                            // The bubble takes the place the finger left it, then the throw.
                            d?.where()?.let { (x, y) -> p.x = x; p.y = y; move(this, p) }
                            alpha = 1f
                            val t = speed
                            t?.computeCurrentVelocity(1000, MAX_FLING * dp)
                            settle(t?.xVelocity ?: 0f, t?.yVelocity ?: 0f)
                        }
                    }
                    end()
                }
                MotionEvent.ACTION_CANCEL -> {
                    removeCallbacks(longPress)
                    if (mode == 2) { drop?.end(); drop = null; alpha = 1f; settle(0f, 0f) }
                    end()
                }
            }
            return true
        }

        private fun end() {
            mode = 0
            speed?.recycle()
            speed = null
        }

        override fun performClick(): Boolean = super.performClick()
    }

    // -- long press: add an agent, or put another in a face's place --------------------------------

    /* old null: the bubble was held, and the pick joins the pile. old an agent: its face in the
       row was held, and the pick takes its place. The same card as the assistant bar's list:
       faces with names, four across (The owner, 31 October 2026, of a plain list of names: "Do i
       need to hook u up with ui skills?"). */
    private fun openPicker(old: String?) = guard {
        closePicker()
        val s = safe()
        val w = min(s.width() - px(24), px(340))
        val grid = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }
        val scroll = ScrollView(ctx).apply { isVerticalScrollBarEnabled = false; addView(grid) }
        val head = words(if (old == null) "Float another agent" else "In place of " + (names[old] ?: old), DIM, 13f)
        val card = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            background = GradientDrawable().apply { setColor(0xFF161413.toInt()); cornerRadius = 20 * dp; setStroke(px(1), 0xFF292524.toInt()) }
            clipToOutline = true
            setPadding(px(8), px(14), px(8), px(8))
            addView(head, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { leftMargin = px(10); bottomMargin = px(6) })
            addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        }
        val p = overlayParams(w, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH).apply {
            title = "ergo float picker"
        }
        card.setOnTouchListener { _, e -> if (e.actionMasked == MotionEvent.ACTION_OUTSIDE) closePicker(); false }
        picker = card

        /* Beside the bubble when the card fits there, else across the screen just under or over
           it. A held face in the row: the card sits over the chat, near the top. */
        fun place(h: Int) {
            val bp = bubbleParams
            if (old == null && bp != null) {
                val beside = if (onRight()) bp.x - w - px(8) >= s.left else bp.x + bp.width + px(8) + w <= s.right
                if (beside) {
                    p.x = if (onRight()) bp.x - w - px(8) else bp.x + bp.width + px(8)
                    p.y = (bp.y + bp.height / 2 - h / 2).coerceIn(s.top + px(8), (s.bottom - h - px(8)).coerceAtLeast(s.top))
                } else {
                    p.x = s.centerX() - w / 2
                    val below = bp.y + bp.height + px(8)
                    p.y = if (below + h <= s.bottom - px(8)) below else (bp.y - h - px(8)).coerceAtLeast(s.top + px(8))
                }
                card.pivotX = if (onRight()) w.toFloat() else 0f
                card.pivotY = (bp.y + bp.height / 2 - p.y).toFloat().coerceIn(0f, h.toFloat())
            } else {
                p.x = s.centerX() - w / 2
                p.y = s.top + px(40)
                card.pivotX = w / 2f
                card.pivotY = 0f
            }
        }

        /* The faces, and a height that fits them up to half the screen; past that the grid scrolls. */
        fun fill(list: List<Pair<String, String>>?, loading: Boolean = false) {
            grid.removeAllViews()
            val pick = list?.filter { it.first !in agents }
            when {
                loading -> grid.addView(words("Loading", DIM, 14f).apply { setPadding(px(10), px(8), px(10), px(12)) })
                pick == null -> grid.addView(words("The laptop seems to be offline", DIM, 14f).apply { setPadding(px(10), px(8), px(10), px(12)) })
                pick.isEmpty() -> grid.addView(words("Everyone is floating", DIM, 14f).apply { setPadding(px(10), px(8), px(10), px(12)) })
                else -> {
                    var line: LinearLayout? = null
                    pick.forEachIndexed { k, (id, name) ->
                        if (k % 4 == 0) line = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL }.also { grid.addView(it) }
                        line!!.addView(LinearLayout(ctx).apply {
                            orientation = LinearLayout.VERTICAL
                            gravity = Gravity.CENTER_HORIZONTAL
                            setPadding(px(2), px(8), px(2), px(8))
                            background = android.graphics.drawable.RippleDrawable(
                                android.content.res.ColorStateList.valueOf(0x1FFFFFFF), null,
                                GradientDrawable().apply { cornerRadius = 14 * dp; setColor(Color.WHITE) })
                            val face = FaceView(ctx, 48).apply { agent(AssistBar.Agent(id, name)); isClickable = false }
                            FaceView.fetch(id, io, main) { face.invalidate() }
                            addView(face, LinearLayout.LayoutParams(px(48), px(48)))
                            addView(words(name, INK, 13f).apply {
                                setSingleLine(true); ellipsize = TextUtils.TruncateAt.END; gravity = Gravity.CENTER
                            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(6) })
                            setOnClickListener {
                                closePicker()
                                if (old == null) add(id, name) else swap(old, id, name)
                            }
                        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                    }
                    while ((line?.childCount ?: 4) < 4) line!!.addView(View(ctx), LinearLayout.LayoutParams(0, 1, 1f))
                }
            }
            card.measure(View.MeasureSpec.makeMeasureSpec(w, View.MeasureSpec.EXACTLY), View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED))
            val h = min(card.measuredHeight, s.height() / 2)
            p.height = h
            place(h)
        }

        val known = roster
        fill(known, loading = known == null)
        // Opens as the bar's cards do: a short fade, growing out of the bubble.
        card.alpha = 0f
        card.scaleX = 0.92f
        card.scaleY = 0.92f
        wm.addView(card, p)
        card.animate().alpha(1f).scaleX(1f).scaleY(1f).setDuration(160).setInterpolator(DecelerateInterpolator()).start()
        io.execute {
            val got = try {
                val o = get("/api/assist/agents")
                val a = o.optJSONArray("agents") ?: JSONArray()
                (0 until a.length()).map { a.getJSONObject(it) }.map { it.getString("id") to it.optString("name", it.getString("id")) }
            } catch (e: Throwable) {
                null
            }
            main.post {
                if (got != null) roster = got
                if (picker !== card) return@post
                if (got == known) return@post
                fill(got ?: known)
                try { wm.updateViewLayout(card, p) } catch (_: Throwable) { }
            }
        }
    }

    private fun words(s: String, color: Int, sp: Float) = TextView(ctx).apply {
        text = s
        setTextColor(color)
        typeface = inter
        setTextSize(TypedValue.COMPLEX_UNIT_SP, sp)
    }

    private fun closePicker() {
        picker?.let { safeRemove(it) }
        picker = null
    }

    // -- the chat window ------------------------------------------------------------------------

    private fun openPanel(id: String) = guard {
        closePicker()
        val open = panel
        if (open != null) { open.switchTo(id); return@guard }
        unread.remove(id)
        save()
        // The kept window, its page already loaded: only its shape and its agent are set again.
        val kept = parked
        parked = null
        val p = kept ?: Panel(id)
        panel = p
        if (kept != null && kept.parent != null) {
            p.shape()
            p.switchTo(id)
            p.hidden(false)
        } else {
            if (kept != null) { p.shape(); p.switchTo(id); p.hidden(false) }
            wm.addView(p, p.params)
        }
        fitBubble()
    }

    /* Closed, not ended: the window stays, unseen and untouchable, its page loaded and drawn.
       Taken off the screen instead, it came back blank for a moment while it drew again. */
    private fun closePanel() {
        val p = panel ?: return
        panel = null
        p.drop()
        p.hidden(true)
        parked?.let { if (it !== p) dispose(it) }
        parked = p
        fitBubble()
    }

    private fun dispose(p: Panel) {
        safeRemove(p)
        try { p.web.destroy() } catch (_: Throwable) { }
    }

    @SuppressLint("SetJavaScriptEnabled", "ViewConstructor", "ClickableViewAccessibility")
    private inner class Panel(var agent: String) : LinearLayout(ctx) {
        val params = WindowManager.LayoutParams(
            0, 0,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            // Not touch modal: a touch outside goes to the game even while this window has focus.
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
            PixelFormat.TRANSLUCENT,
        ).apply {
            title = "ergo float chat"
            softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
            // The keyboard too: a floating window is not pushed up for it, so it is laid out above it.
            if (Build.VERSION.SDK_INT >= 30) fitInsetsTypes = WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or WindowInsets.Type.ime()
        }
        val web = WebView(ctx)
        val row = Row()
        private val body = LinearLayout(ctx).apply { orientation = VERTICAL }
        private var rowShown = false
        private val grip = Grip()
        private var wantKeys = false
        private var focusable = false
        // A while after a touch that was not typing, the game gets its focus back.
        private val release = Runnable { if (!wantKeys) focus(false) }

        init {
            background = GradientDrawable().apply { setColor(0xF2121010.toInt()); cornerRadius = 18 * dp; setStroke(px(1), 0xFF292524.toInt()) }
            clipToOutline = true
            web.setBackgroundColor(Color.TRANSPARENT)
            web.settings.javaScriptEnabled = true
            web.settings.domStorageEnabled = true
            web.settings.mediaPlaybackRequiresUserGesture = false
            web.addJavascriptInterface(Bridge(), "ErgoFloat")
            web.webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView?, url: String?) {
                    view?.evaluateJavascript(WATCH_TYPING, null)
                }

                override fun shouldOverrideUrlLoading(view: WebView?, req: WebResourceRequest?): Boolean {
                    val u = req?.url ?: return false
                    if (Daemon.isSet && u.toString().startsWith(Daemon.base)) return false
                    try {
                        ctx.startActivity(Intent(Intent.ACTION_VIEW, u).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    } catch (_: Throwable) { }
                    return true
                }
            }
            web.webChromeClient = object : WebChromeClient() {
                // The chat's own mic: allowed when the app may record.
                override fun onPermissionRequest(request: PermissionRequest) {
                    val mic = ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
                    if (mic && PermissionRequest.RESOURCE_AUDIO_CAPTURE in request.resources) {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                    } else request.deny()
                }

                /* A photo or file for the chat: the phone's picker, through FilePick. The faces and
                   the window step aside while it is open, since they draw over every app. */
                override fun onShowFileChooser(view: WebView?, cb: ValueCallback<Array<android.net.Uri>>?, fp: FileChooserParams?): Boolean {
                    if (cb == null || fp == null) return false
                    FilePick.callback?.onReceiveValue(null)
                    val ask = try { fp.createIntent() } catch (_: Throwable) { null } ?: return false
                    if (fp.mode == FileChooserParams.MODE_OPEN_MULTIPLE) ask.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                    FilePick.callback = cb
                    FilePick.asked = ask
                    FilePick.done = { main.post { tuck(false) } }
                    tuck(true)
                    return try {
                        ctx.startActivity(Intent(ctx, FilePick::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION))
                        true
                    } catch (e: Throwable) {
                        Log.w(TAG, "no file picker", e)
                        FilePick.callback = null; FilePick.asked = null; FilePick.done = null
                        tuck(false)
                        false
                    }
                }
            }
            shape()
            web.loadUrl(Daemon.base + "/?embed=1&screen=chat#/a/" + android.net.Uri.encode(agent))
        }

        /* Another face in the row was tapped: its chat, in this same window. Only the address
           after # changes, so the page does not load again. */
        fun switchTo(id: String) {
            unread.remove(id)
            save()
            if (id == agent) { redraw(); return }
            drop()
            agent = id
            web.evaluateJavascript("location.hash = '#/a/' + encodeURIComponent(" + JSONObject.quote(id) + ")", null)
            redraw()
        }

        /* The row shows only for two or more; crossing that line changes the window's shape. */
        fun rowChanged() {
            if ((agents.size >= 2) != rowShown) {
                shape()
                try { if (parent != null) wm.updateViewLayout(this, params) } catch (_: Throwable) { }
            }
            row.invalidate()
        }

        /* The keyboard comes only to a window that has focus, and focus comes only after the
           window may take it. So a touch in the window lets it take focus at once, and the
           keyboard is asked for when the focus has really arrived, not before: asked too early,
           Android drops the request, which is why a tap in the text box did nothing. A touch
           outside hands the focus back to the game. */
        override fun dispatchTouchEvent(e: MotionEvent): Boolean {
            when (e.actionMasked) {
                MotionEvent.ACTION_OUTSIDE -> {
                    if (focusable) {
                        web.evaluateJavascript("document.activeElement && document.activeElement.blur()", null)
                        keyboard(false)
                        focus(false)
                    }
                    return true
                }
                MotionEvent.ACTION_DOWN -> { removeCallbacks(release); focus(true) }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> { removeCallbacks(release); postDelayed(release, 400) }
            }
            return super.dispatchTouchEvent(e)
        }

        override fun onWindowFocusChanged(has: Boolean) {
            super.onWindowFocusChanged(has)
            if (has && wantKeys) showKeys()
        }

        private fun focus(on: Boolean) {
            if (on == focusable) return
            focusable = on
            params.flags = if (on) params.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE.inv()
            else params.flags or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
            try { if (parent != null) wm.updateViewLayout(this, params) } catch (_: Throwable) { }
        }

        private fun showKeys() {
            web.requestFocus()
            web.evaluateJavascript("window.__ergoT && window.__ergoT.focus()", null)
            web.postDelayed({
                ctx.getSystemService(InputMethodManager::class.java)?.showSoftInput(web, 0)
            }, 60)
        }

        /* Landscape: docked right, full height, a share of the width. Portrait: docked to the
           bottom, full width, a share of the height. Each keeps its own share. */
        fun shape() {
            removeAllViews()
            body.removeAllViews()
            rowShown = agents.size >= 2
            if (rowShown) body.addView(row, LayoutParams(LayoutParams.MATCH_PARENT, px(ROW_DP)))
            body.addView(web, LayoutParams(LayoutParams.MATCH_PARENT, 0, 1f))
            val (w, h) = screen()
            val wide = landscape()
            val share = prefs.getFloat(if (wide) "shareL" else "shareP", THIRD).coerceIn(SIXTH, TWO_THIRDS)
            orientation = if (wide) HORIZONTAL else VERTICAL
            grip.vertical = wide
            if (wide) {
                // On the side the faces rest on, its grip on the inner edge.
                val right = onRight()
                grip.left = !right
                params.width = (w * share).roundToInt()
                params.height = WindowManager.LayoutParams.MATCH_PARENT
                params.gravity = (if (right) Gravity.END else Gravity.START) or Gravity.TOP
                if (right) addView(grip, LayoutParams(px(22), LayoutParams.MATCH_PARENT))
                addView(body, LayoutParams(0, LayoutParams.MATCH_PARENT, 1f))
                if (!right) addView(grip, LayoutParams(px(22), LayoutParams.MATCH_PARENT))
            } else {
                params.width = WindowManager.LayoutParams.MATCH_PARENT
                params.height = (h * share).roundToInt()
                params.gravity = Gravity.BOTTOM or Gravity.START
                addView(grip, LayoutParams(LayoutParams.MATCH_PARENT, px(26)))
                addView(body, LayoutParams(LayoutParams.MATCH_PARENT, 0, 1f))
            }
            params.x = 0
            params.y = 0
        }

        /* Unseen and untouchable: every touch goes through to the app under it. The whole window
           is see-through, not only its view, since Android lets touches pass only a window that
           is clear enough. */
        fun hidden(on: Boolean) {
            params.alpha = if (on) 0f else 1f
            params.flags = if (on) params.flags or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
            else params.flags and WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE.inv()
            if (on) focusable = false
            try { if (parent != null) wm.updateViewLayout(this, params) } catch (_: Throwable) { }
        }

        /* The page says a text field was tapped or left. */
        fun keyboard(on: Boolean) {
            wantKeys = on
            if (on) {
                removeCallbacks(release)
                focus(true)
                if (hasWindowFocus()) showKeys()
            } else {
                ctx.getSystemService(InputMethodManager::class.java)?.hideSoftInputFromWindow(windowToken, 0)
            }
        }

        fun drop() {
            removeCallbacks(release)
            keyboard(false)
        }

        private inner class Bridge {
            @JavascriptInterface
            fun keyboard(on: Boolean) {
                // this@Panel: a bare keyboard() here is this method, and it would only post itself again.
                main.post { if (panel === this@Panel) this@Panel.keyboard(on) }
            }
        }

        /* The faces in a row over the chat, as Messenger lines up its chat heads: tap one to open
           its chat here, tap the open one to fold the window back into the bubble, hold one to
           put another agent in its place, drag one to the x to let it go. */
        inner class Row : View(ctx) {
            private var held = -1
            private var downX = 0f
            private var downY = 0f
            private var mode = 0     // 0 idle, 1 pending, 2 dragging
            private val longPress = Runnable { if (mode == 1 && held >= 0) { mode = 0; openPicker(agents[held]) } }
            private val label = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFFE8E3DC.toInt(); textSize = 15 * dp }

            private fun cx(i: Int) = px(14) + face / 2f + i * (face + gap)

            override fun onDraw(c: Canvas) {
                var turning = false
                val cy = height / 2f
                for ((i, id) in agents.withIndex()) {
                    if (drawFace(c, cx(i), cy, id, working[id] == true, if (id in unread) 1 else 0, on = id == agent)) turning = true
                }
                val x = cx(agents.size - 1) + face / 2f + px(12)
                val name = names[agent] ?: agent
                if (x + label.measureText(name) < width - px(8)) c.drawText(name, x, cy - (label.descent() + label.ascent()) / 2f, label)
                if (turning) postInvalidateOnAnimation()
            }

            private fun at(x: Float): Int {
                val i = ((x - px(14) + gap / 2f) / (face + gap)).toInt()
                return if (i in agents.indices) i else -1
            }

            @SuppressLint("ClickableViewAccessibility")
            override fun onTouchEvent(e: MotionEvent): Boolean {
                when (e.actionMasked) {
                    MotionEvent.ACTION_DOWN -> {
                        held = at(e.x)
                        downX = e.rawX; downY = e.rawY
                        mode = if (held >= 0) 1 else 0
                        if (mode == 1) postDelayed(longPress, LONG_MS)
                    }
                    MotionEvent.ACTION_MOVE -> {
                        if (mode == 1 && (abs(e.rawX - downX) > slop || abs(e.rawY - downY) > slop)) {
                            removeCallbacks(longPress)
                            mode = 2
                            guard { drop = Drop(agents[held]) }
                        }
                        if (mode == 2) drop?.follow(e.rawX, e.rawY, this)
                    }
                    MotionEvent.ACTION_UP -> {
                        removeCallbacks(longPress)
                        val id = agents.getOrNull(held)
                        if (mode == 1 && id != null) {
                            performClick()
                            if (id == agent) closePanel() else switchTo(id)
                        }
                        if (mode == 2) {
                            val d = drop
                            drop = null
                            d?.end()
                            if (d?.caught == true && id != null) remove(id)
                        }
                        mode = 0; held = -1
                    }
                    MotionEvent.ACTION_CANCEL -> {
                        removeCallbacks(longPress)
                        if (mode == 2) { drop?.end(); drop = null }
                        mode = 0; held = -1
                    }
                }
                return true
            }

            override fun performClick(): Boolean = super.performClick()
        }

        /* The edge he holds: drag to resize, tap the arrow to tuck the window back to the faces. */
        private inner class Grip : View(ctx) {
            var vertical = true
            var left = false      // the window is docked left: the grip is on its right edge
            private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFF57534E.toInt(); strokeWidth = 3 * dp; strokeCap = Paint.Cap.ROUND }
            private val arrow = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFF78716C.toInt(); strokeWidth = 2 * dp; strokeCap = Paint.Cap.ROUND; style = Paint.Style.STROKE; strokeJoin = Paint.Join.ROUND }
            private var down = 0f
            private var moved = false
            private var start = 0

            override fun onDraw(c: Canvas) {
                val u = dp
                if (vertical) {
                    val cx = width / 2f
                    c.drawLine(cx, height / 2f - 20 * u, cx, height / 2f + 20 * u, line)
                    // The arrow at the top: points back toward the faces' edge.
                    val ay = 26 * u
                    val k = if (left) -1 else 1
                    val p = Path().apply { moveTo(cx - 3 * u * k, ay - 6 * u); lineTo(cx + 3 * u * k, ay); lineTo(cx - 3 * u * k, ay + 6 * u) }
                    c.drawPath(p, arrow)
                } else {
                    val cy = height / 2f
                    c.drawLine(width / 2f - 20 * u, cy, width / 2f + 20 * u, cy, line)
                    // The arrow at the right: points down, back toward the faces.
                    val ax = width - 26 * u
                    val p = Path().apply { moveTo(ax - 6 * u, cy - 3 * u); lineTo(ax, cy + 3 * u); lineTo(ax + 6 * u, cy - 3 * u) }
                    c.drawPath(p, arrow)
                }
            }

            @SuppressLint("ClickableViewAccessibility")
            override fun onTouchEvent(e: MotionEvent): Boolean {
                val (w, h) = screen()
                when (e.actionMasked) {
                    MotionEvent.ACTION_DOWN -> {
                        down = if (vertical) e.rawX else e.rawY
                        start = if (vertical) params.width else params.height
                        moved = false
                    }
                    MotionEvent.ACTION_MOVE -> {
                        val d = (if (vertical) e.rawX else e.rawY) - down
                        if (abs(d) > slop) moved = true
                        if (moved) {
                            val total = if (vertical) w else h
                            // Past two thirds it follows the finger to the edge: let go there and the app opens.
                            val size = (if (vertical && left) start + d else start - d).roundToInt().coerceIn((total * SIXTH).roundToInt(), total)
                            if (vertical) params.width = size else params.height = size
                            try { wm.updateViewLayout(this@Panel, params) } catch (_: Throwable) { }
                        }
                    }
                    MotionEvent.ACTION_UP -> {
                        if (moved) {
                            val total = if (vertical) w else h
                            val now = if (vertical) params.width else params.height
                            val share = now / total.toFloat()
                            when {
                                share >= HAND_OFF -> grow(now, total) { handOff(agent) }
                                share > TWO_THIRDS -> grow(now, (total * TWO_THIRDS).roundToInt()) {
                                    prefs.edit { putFloat(if (vertical) "shareL" else "shareP", TWO_THIRDS) }
                                }
                                else -> prefs.edit { putFloat(if (vertical) "shareL" else "shareP", share) }
                            }
                        } else {
                            // A tap near the arrow tucks the window away.
                            val nearArrow = if (vertical) e.y < 56 * dp else e.x > width - 56 * dp
                            if (nearArrow) { performClick(); closePanel() }
                        }
                    }
                }
                return true
            }

            /* The window's size slides to a new one in a short ease, then the next step runs. */
            private fun grow(from: Int, to: Int, then: () -> Unit) {
                ValueAnimator.ofInt(from, to).apply {
                    duration = 200
                    interpolator = DecelerateInterpolator()
                    addUpdateListener {
                        if (panel !== this@Panel) return@addUpdateListener
                        val v = it.animatedValue as Int
                        if (vertical) params.width = v else params.height = v
                        try { wm.updateViewLayout(this@Panel, params) } catch (_: Throwable) { }
                    }
                    addListener(object : android.animation.AnimatorListenerAdapter() {
                        override fun onAnimationEnd(a: android.animation.Animator) { if (panel === this@Panel) then() }
                    })
                    start()
                }
            }

            override fun performClick(): Boolean = super.performClick()
        }
    }

    /* While the phone's picker is open, the float windows are there but unseen and untouchable. */
    private fun tuck(on: Boolean) = guard {
        panel?.let { p ->
            if (on) p.drop()
            p.params.alpha = if (on) 0f else 1f
            p.params.flags = if (on) p.params.flags or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
            else p.params.flags and WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE.inv()
            if (p.parent != null) wm.updateViewLayout(p, p.params)
        }
        val b = bubble
        val bp = bubbleParams
        if (b != null && bp != null) {
            bp.alpha = if (on) 0f else 1f
            bp.flags = if (on) bp.flags or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
            else bp.flags and WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE.inv()
            if (b.parent != null) wm.updateViewLayout(b, bp)
        }
    }

    /* The window was pulled past two thirds: it grows to the whole screen, and the app opens on
       the same chat ("if i expand it to cover full screen just open it in app. Have it smooth"). */
    private fun handOff(id: String) = guard {
        val i = Intent(ctx, MainActivity::class.java)
            .putExtra(MainActivity.EXTRA_GO, "chat:$id")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(i)
        // Gone once the app is over it, so the game never shows between the two.
        main.postDelayed({ if (panel?.agent == id) closePanel() }, 450)
    }

    // -- the chime ------------------------------------------------------------------------------

    /* His chime, mixed into whatever plays: no audio focus is asked, so the game is not ducked. */
    private fun chime() {
        val pick = Notices.chime(ctx)
        if (pick == "off") return
        val res = when (pick) {
            "marimba" -> R.raw.chime_marimba
            "drop" -> R.raw.chime_drop
            "bell" -> R.raw.chime_bell
            else -> R.raw.chime_kalimba
        }
        val loaded = chimes[pick]
        if (loaded != null) {
            sounds.play(loaded, 0.8f, 0.8f, 1, 0, 1f)
            return
        }
        val sid = sounds.load(ctx, res, 1)
        chimes[pick] = sid
        sounds.setOnLoadCompleteListener { pool, id, ok -> if (ok == 0 && id == sid) pool.play(id, 0.8f, 0.8f, 1, 0, 1f) }
    }

    // -- small parts ----------------------------------------------------------------------------

    private fun save() = prefs.edit {
        putString("agents", agents.joinToString(","))
        putString("unread", unread.joinToString(","))
        putString("names", JSONObject(names as Map<*, *>).toString())
    }

    private fun landscape() = ctx.resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE

    private fun screen(): Pair<Int, Int> {
        if (Build.VERSION.SDK_INT >= 30) {
            val b = wm.currentWindowMetrics.bounds
            return b.width() to b.height()
        }
        val m = ctx.resources.displayMetrics
        return m.widthPixels to m.heightPixels
    }

    private fun px(v: Int) = (v * dp).roundToInt()

    /* A window placed in whole-display pixels, the same as a finger's raw position. Android would
       otherwise lay it out inside the area left after the notch and the bars, and a face placed
       against the full width slid off the edge in a full-screen game ("like a crescent"). */
    private fun overlayParams(w: Int, h: Int, flags: Int) = WindowManager.LayoutParams(
        w, h, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, flags, PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.TOP or Gravity.START
        if (Build.VERSION.SDK_INT >= 30) {
            fitInsetsTypes = 0
            layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
        } else if (Build.VERSION.SDK_INT >= 28) {
            layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
        }
    }

    /* Where a face may rest: the screen less the notch and the system bars, hidden or not. */
    private fun safe(): Rect {
        val (w, h) = screen()
        if (Build.VERSION.SDK_INT < 30) return Rect(0, 0, w, h)
        val i = wm.currentWindowMetrics.windowInsets.getInsetsIgnoringVisibility(
            WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
        return Rect(i.left, i.top, w - i.right, h - i.bottom)
    }

    private fun get(path: String): JSONObject {
        val c = URL(Daemon.base + path).openConnection() as HttpURLConnection
        try {
            c.connectTimeout = 3000
            c.readTimeout = 4000
            return JSONObject(c.inputStream.bufferedReader().use { it.readText() })
        } finally {
            c.disconnect()
        }
    }

    private fun safeRemove(v: View) {
        try {
            if (v.parent != null) wm.removeView(v)
        } catch (e: Throwable) {
            Log.w(TAG, "a float window would not go", e)
        }
    }

    /* Any fault here takes the float windows down. The game under them must never be stuck. */
    private inline fun guard(block: () -> Unit) {
        try {
            block()
        } catch (e: Throwable) {
            Log.e(TAG, "float failed", e)
            try { hideAll() } catch (_: Throwable) { }
        }
    }

    companion object {
        private const val TAG = "CCFloat"
        private const val INK = 0xFFE8E3DC.toInt()
        private const val DIM = 0xFFA8A29E.toInt()
        private const val LONG_MS = 500L
        private const val ROW_DP = 64
        private const val NOT_TOUCH = WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
        private const val MAGNET_DP = 72
        private const val MAX_FLING = 4000f      // dp a second
        private const val THROW_S = 0.18f        // how far a throw carries, in seconds of its speed
        private const val STIFFNESS = 500f       // between Android's low and medium springs
        private const val DAMPING = 0.72f        // a small bounce at the edge
        const val SIXTH = 1f / 6f
        const val THIRD = 1f / 3f
        const val TWO_THIRDS = 2f / 3f
        const val HAND_OFF = 0.8f         // pulled this far, the window becomes the app

        /* In the chat page: a tap or focus in a text field asks for the keyboard; leaving it gives
           the focus back. The field is kept so it can take the focus once the window may. */
        private const val WATCH_TYPING = """
            (function () {
              if (window.__ergoFloat) return; window.__ergoFloat = 1;
              function typing(t) { return t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.isContentEditable); }
              document.addEventListener('pointerdown', function (e) { if (typing(e.target)) { window.__ergoT = e.target; ErgoFloat.keyboard(true); } }, true);
              document.addEventListener('focusin', function (e) { if (typing(e.target)) { window.__ergoT = e.target; ErgoFloat.keyboard(true); } }, true);
              document.addEventListener('focusout', function (e) {
                if (!typing(e.target)) return;
                setTimeout(function () { if (!typing(document.activeElement)) ErgoFloat.keyboard(false); }, 150);
              }, true);
            })();
        """
    }
}
