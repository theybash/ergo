package dev.ergo.app

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.view.View
import androidx.core.graphics.PathParser
import androidx.core.graphics.withTranslation

/* The trash target a drag of the bubble shows at the top of the screen, after the chat heads
   dismiss target: a round target and a soft dark scrim along the top edge. It fades and slides
   in when a drag starts and out when it ends. While the bubble is stuck to it, it grows a little
   and its ring and icon turn mint. It never takes a touch.

   The window sits at the top left of the screen, so this view's coordinates are screen
   coordinates, and `cy` is where the target's centre is on screen. */
@SuppressLint("ViewConstructor")
class DismissView(context: Context) : View(context) {

    private val dp = resources.displayMetrics.density
    val radius = 30 * dp
    var cy = 0f

    private val trash: Path = PathParser.createPathFromPathData(TRASH)
    private var shown = 0f
    private var grown = 0f
    private var showing: ValueAnimator? = null
    private var growing: ValueAnimator? = null
    private var stuck = false

    private val scrim = Paint()
    private val disc = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = WheelView.STONE_RAISED }
    private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1.5f * dp
    }
    private val icon = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1.75f
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    init {
        contentDescription = "Drop here to hide the bubble"
        visibility = GONE
    }

    /* A drag started: fade and slide in. */
    fun appear() {
        stuck = false
        grown = 0f
        growing?.cancel()
        visibility = VISIBLE
        animateShown(1f, 180)
    }

    /* The drag ended somewhere else: fade and slide out, then take no room at all. */
    fun disappear() {
        animateShown(0f, 160)
    }

    /* The bubble came within the magnet, or left it. */
    fun stick(on: Boolean) {
        if (on == stuck) return
        stuck = on
        growing?.cancel()
        growing = ValueAnimator.ofFloat(grown, if (on) 1f else 0f).apply {
            duration = 120
            addUpdateListener {
                grown = it.animatedValue as Float
                invalidate()
            }
            start()
        }
    }

    private fun animateShown(to: Float, ms: Long) {
        showing?.cancel()
        showing = ValueAnimator.ofFloat(shown, to).apply {
            duration = ms
            addUpdateListener {
                shown = it.animatedValue as Float
                if (shown == 0f && to == 0f) visibility = GONE
                invalidate()
            }
            start()
        }
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        scrim.shader = LinearGradient(0f, 0f, 0f, h.toFloat(), SCRIM, 0, Shader.TileMode.CLAMP)
    }

    override fun onDraw(canvas: Canvas) {
        if (shown <= 0f) return
        val a = (shown * 255).toInt()
        scrim.alpha = a
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), scrim)

        val cx = width / 2f
        // Slides down from a little above its place as it fades in.
        val y = cy - (1 - shown) * 24 * dp
        val r = radius * (1 + 0.18f * grown)
        disc.alpha = a
        canvas.drawCircle(cx, y, r, disc)
        val tint = if (stuck) WheelView.MINT else WheelView.TEXT
        ring.color = if (stuck) WheelView.MINT else WheelView.LINE
        ring.alpha = a
        canvas.drawCircle(cx, y, r, ring)
        icon.color = tint
        icon.alpha = a
        val s = 22 * dp * (1 + 0.1f * grown)
        canvas.withTranslation(cx - s / 2, y - s / 2) {
            scale(s / 24, s / 24)
            drawPath(trash, icon)
        }
    }

    private companion object {
        // The stone at most of its strength at the top edge, gone by the bottom of the window.
        const val SCRIM = 0xB30F0E0D.toInt()

        /* Trash, after the Feather set (MIT), on a 24 unit square. */
        const val TRASH = "M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" +
            "M10 11v6M14 11v6"
    }
}
