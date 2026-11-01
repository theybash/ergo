package dev.ergo.app

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorFilter
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.RectF
import android.graphics.drawable.Drawable
import android.view.View
import kotlin.math.max
import kotlin.math.roundToInt

/* Frosted glass for the assistant bar, the same look as the app's --glass: the screen behind,
   blurred and a little more saturated, under a dark tint.

   Android blurs behind a window only on some phones, and never on the emulator, so the bar does
   it itself: the assistant already gets the screenshot, and each card draws the blurred copy of
   the part of the screen it covers. With no screenshot (the lock screen, an app that hides its
   screen, or screen use off) the cards stay near solid, so they read on anything. */
class Frost {
    var bitmap: Bitmap? = null
        private set
    var edge: Bitmap? = null      // the finer, true-colour blur the soft edge draws
        private set
    var mix = 0f          // 0 solid, 1 full glass; it rises over 220 ms when the blur is ready
        private set
    var glass = GlassDrawable.GLASS   // how dark the tint is once the blur is in
        private set
    private val hosts = mutableListOf<View>()
    private var startedAt = 0L

    fun attach(v: View) { hosts += v }

    fun clear() {
        bitmap = null
        edge = null
        mix = 0f
        hosts.forEach { it.invalidate() }
    }

    fun set(b: Bitmap?, e: Bitmap? = null) {
        bitmap = b
        edge = e
        glass = b?.let { tintFor(it) } ?: GlassDrawable.GLASS
        startedAt = android.os.SystemClock.uptimeMillis()
        step()
    }

    private fun step() {
        if (bitmap == null) return
        mix = ((android.os.SystemClock.uptimeMillis() - startedAt) / 220f).coerceIn(0f, 1f)
        hosts.forEach { it.invalidate() }
        if (mix < 1f) hosts.firstOrNull()?.postOnAnimation { step() }
    }

    companion object {
        /* A white page under the app's 62% tint comes out a washed grey with weak text, so the
           tint follows how bright the lower part of the screen is, where the bar sits: the app's
           glass over a dark screen, up to 82% over a white one. */
        private fun tintFor(b: Bitmap): Float {
            val y0 = b.height * 55 / 100
            var sum = 0.0
            var n = 0
            for (y in y0 until b.height) for (x in 0 until b.width) {
                val c = b.getPixel(x, y)
                sum += 0.2126 * ((c shr 16) and 255) + 0.7152 * ((c shr 8) and 255) + 0.0722 * (c and 255)
                n++
            }
            val light = if (n == 0) 0.0 else sum / n / 255.0
            return (GlassDrawable.GLASS + (0.82f - GlassDrawable.GLASS) * ((light - 0.35) / 0.55).coerceIn(0.0, 1.0)).toFloat()
        }

        /* A small, soft, more saturated copy of the screen. Shrinking it fourteen times does most of
           the blur; three box passes take out the blocks; the filter on draw smooths the rest. */
        fun blur(src: Bitmap): Bitmap? = blur(src, 14, 3, 1.7f)

        /* The soft edge spreads over half the screen, so the cards' small copy shows its blocks
           there when the phone scales it up (The owner, 26 Oct: "the bottom blur is weird"). The edge
           gets a copy four times finer with a wider blur, and no extra colour, so a bright button
           under it stays a faint glow instead of a blob. */
        fun blurEdge(src: Bitmap): Bitmap? = blur(src, 4, 12, 1f)

        private fun blur(src: Bitmap, shrink: Int, radius: Int, saturation: Float): Bitmap? = try {
            val soft = if (src.config == Bitmap.Config.HARDWARE) src.copy(Bitmap.Config.ARGB_8888, false) else src
            val w = max(1, soft.width / shrink)
            val h = max(1, (soft.height.toLong() * w / soft.width).toInt())
            val small = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
            val paint = Paint(Paint.FILTER_BITMAP_FLAG).apply {
                if (saturation != 1f) colorFilter = ColorMatrixColorFilter(ColorMatrix().apply { setSaturation(saturation) })
            }
            Canvas(small).drawBitmap(soft, null, RectF(0f, 0f, w.toFloat(), h.toFloat()), paint)
            val px = IntArray(w * h)
            small.getPixels(px, 0, w, 0, 0, w, h)
            repeat(3) { boxBlur(px, w, h, radius) }
            small.setPixels(px, 0, w, 0, 0, w, h)
            small
        } catch (_: Throwable) {
            null
        }

        private fun boxBlur(px: IntArray, w: Int, h: Int, r: Int) {
            val tmp = IntArray(px.size)
            pass(px, tmp, w, h, r, true)
            pass(tmp, px, w, h, r, false)
        }

        private fun pass(src: IntArray, dst: IntArray, w: Int, h: Int, r: Int, across: Boolean) {
            val lines = if (across) h else w
            val len = if (across) w else h
            for (line in 0 until lines) {
                var a = 0; var rr = 0; var g = 0; var b = 0
                fun at(i: Int): Int {
                    val k = i.coerceIn(0, len - 1)
                    return if (across) src[line * w + k] else src[k * w + line]
                }
                for (i in -r..r) {
                    val c = at(i)
                    a += c ushr 24; rr += (c shr 16) and 255; g += (c shr 8) and 255; b += c and 255
                }
                val n = 2 * r + 1
                for (i in 0 until len) {
                    val out = ((a / n) shl 24) or ((rr / n) shl 16) or ((g / n) shl 8) or (b / n)
                    if (across) dst[line * w + i] = out else dst[i * w + line] = out
                    val add = at(i + r + 1)
                    val sub = at(i - r)
                    a += (add ushr 24) - (sub ushr 24)
                    rr += ((add shr 16) and 255) - ((sub shr 16) and 255)
                    g += ((add shr 8) and 255) - ((sub shr 8) and 255)
                    b += (add and 255) - (sub and 255)
                }
            }
        }
    }
}

/* One glass card: the blurred screen behind its own spot, the tint, and a hairline edge. */
class GlassDrawable(
    private val host: View,
    private val frost: Frost,
    private val radius: Float,
    private val line: Float,
) : Drawable() {
    private val shot = Paint(Paint.FILTER_BITMAP_FLAG or Paint.ANTI_ALIAS_FLAG)
    private val tint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val edge = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = line
        color = EDGE
    }
    private val clip = Path()
    private val box = RectF()
    private val at = IntArray(2)

    override fun draw(c: Canvas) {
        box.set(bounds)
        clip.reset()
        clip.addRoundRect(box, radius, radius, Path.Direction.CW)
        val b = frost.bitmap
        val mix = frost.mix
        c.save()
        c.clipPath(clip)
        if (b != null && mix > 0f) {
            // Where this card sits on the screen decides which part of the shot shows through.
            host.getLocationOnScreen(at)
            val screenW = host.rootView.width.toFloat()
            val screenH = screenW * b.height / b.width
            shot.alpha = (255 * mix).roundToInt()
            c.drawBitmap(b, null, RectF(-at[0].toFloat(), -at[1].toFloat(),
                -at[0] + screenW, -at[1] + screenH), shot)
        }
        // From near solid to the app's glass as the blur arrives.
        val alpha = SOLID + (frost.glass - SOLID) * mix
        tint.color = ((alpha * 255).roundToInt() shl 24) or TINT
        c.drawRect(box, tint)
        c.restore()
        box.inset(line / 2, line / 2)
        c.drawRoundRect(box, radius, radius, edge)
    }

    override fun setAlpha(alpha: Int) {}
    override fun setColorFilter(colorFilter: ColorFilter?) {}
    @Deprecated("Deprecated in Java")
    override fun getOpacity() = PixelFormat.TRANSLUCENT

    companion object {
        const val TINT = 0x171514          // the app's --glass, rgba(23,21,20,.62)
        const val GLASS = 0.64f            // the app's own .62, a hair darker
        const val SOLID = 0.96f
        const val EDGE = 0x1AE8E3DC        // --glass-line, rgba(232,227,220,.10)
    }
}

/* The screen behind the bar: a flat dim, and under it the bottom of the screen frosting over
   toward the bar, so the dock and whatever else sits there melt away instead of showing sharp
   behind the cards (The owner, 26 Oct, picked from three mockups). The top of the screen stays
   sharp, so he still sees what he is asking about. With no screenshot it is the dim alone. */
class EdgeDrawable(private val frost: Frost, private val dim: Int) : Drawable() {
    private val shot = Paint(Paint.FILTER_BITMAP_FLAG)
    private val flat = Paint().apply { color = dim }
    private val shade = Paint()
    private var shaderFor: Any? = null
    private var level = 255

    override fun draw(c: Canvas) {
        val w = bounds.width().toFloat()
        val h = bounds.height().toFloat()
        val b = frost.edge
        if (b != null && frost.mix > 0f) {
            // One pass: the blurred screen, its strength rising down the band. No layer, so there
            // is no layer edge for the phone to draw as a line (The owner, 26 Oct: "There is a line").
            val key = Triple(b, bounds.width(), bounds.height())
            if (shaderFor != key) {
                shaderFor = key
                val image = android.graphics.BitmapShader(b, android.graphics.Shader.TileMode.CLAMP,
                    android.graphics.Shader.TileMode.CLAMP).apply {
                    setLocalMatrix(android.graphics.Matrix().apply { setScale(w / b.width, w / b.width) })
                }
                val ramp = android.graphics.LinearGradient(0f, h * BAND_TOP, 0f, h * BAND_FULL,
                    0x00000000, 0xFF000000.toInt(), android.graphics.Shader.TileMode.CLAMP)
                shot.shader = android.graphics.ComposeShader(image, ramp, android.graphics.PorterDuff.Mode.DST_IN)
                // The frost also darkens toward the bar, so it reads as one calm surface under
                // the cards and not as the app's colours smeared.
                shade.shader = android.graphics.LinearGradient(0f, h * BAND_TOP, 0f, h,
                    0x00000000, (SHADE shl 24) or GlassDrawable.TINT, android.graphics.Shader.TileMode.CLAMP)
            }
            shot.alpha = (255 * frost.mix).roundToInt()
            c.drawRect(0f, 0f, w, h, shot)
            shade.alpha = shot.alpha
            c.drawRect(0f, h * BAND_TOP, w, h, shade)
        }
        flat.alpha = (Color.alpha(dim) * level / 255)
        c.drawRect(0f, 0f, w, h, flat)
    }

    override fun setAlpha(alpha: Int) { level = alpha; invalidateSelf() }
    override fun setColorFilter(colorFilter: ColorFilter?) {}
    @Deprecated("Deprecated in Java")
    override fun getOpacity() = PixelFormat.TRANSLUCENT

    companion object {
        const val BAND_TOP = 0.52f     // the frost starts about the middle of the screen
        const val BAND_FULL = 0.73f    // and is full from about three quarters down
        const val SHADE = 0x8C         // how dark it is at the very bottom, about 55%
    }
}
