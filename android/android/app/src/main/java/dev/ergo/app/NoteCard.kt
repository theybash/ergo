package dev.ergo.app

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.WindowInsets
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.window.OnBackInvokedCallback
import android.window.OnBackInvokedDispatcher
import androidx.core.content.res.ResourcesCompat
import kotlin.math.roundToInt

/* The check step of a voice note: a small card near the bubble. First "Writing it down...",
   then the words with Send, Edit and Cancel. Edit makes the words editable with the keyboard up.

   Safety, as strict as the wheel's. One card window at a time. It stays inside the system bars.
   Back closes it; while editing, back first puts the keyboard away. A tap outside closes it
   without sending. Sixty seconds with no touch closes it and throws the words away. Any error
   in its own code removes it at once, and every close is a plain remove that cannot hang. It
   takes the key focus only while it is up, and never talks to the keyboard until Edit. */
class NoteCard(private val ctx: Context) {

    private val wm = ctx.getSystemService(WindowManager::class.java)
    private val main = Handler(Looper.getMainLooper())
    private val dp = ctx.resources.displayMetrics.density
    private val inter = try {
        ResourcesCompat.getFont(ctx, R.font.inter_medium)
    } catch (_: Throwable) {
        null
    }

    private var root: FrameLayout? = null
    private var params: WindowManager.LayoutParams? = null
    private var backCallback: Any? = null
    private var editing = false

    /* What Send does with the words; set by whoever shows the card. */
    var onSend: ((String) -> Unit)? = null

    private val idle = Runnable { Log.i(TAG, "the note card closed itself after a minute"); close() }

    val showing: Boolean
        get() = root != null

    /* The first state: the card appears near the bubble, saying it is writing the note down. */
    fun writing(anchorX: Int, anchorY: Int, leftEdge: Boolean) = guard {
        close()
        val card = frame()
        card.addView(column(line("Writing it down...", DIM, 15f)))
        add(card, anchorX, anchorY, leftEdge)
    }

    /* The words, with Send, Edit and Cancel. */
    fun words(text: String) = guard {
        val card = root ?: return@guard
        card.removeAllViews()
        val words = line(text, INK, 15f).apply { maxLines = 8 }
        val row = row(
            button("Cancel", DIM, false) { close() },
            button("Edit", INK, false) { edit(text) },
            button("Send", MINT, true) { send(text) },
        )
        card.addView(column(words, row))
        touched()
    }

    /* One line of error, with Cancel. */
    fun error(text: String) = guard {
        val card = root ?: return@guard
        card.removeAllViews()
        card.addView(column(line(text, INK, 14f), row(button("Cancel", DIM, false) { close() })))
        touched()
    }

    /* Takes the card away. Safe to call at any time, from any state. */
    fun close() {
        main.removeCallbacks(idle)
        val card = root ?: return
        root = null
        editing = false
        try {
            (ctx.getSystemService(InputMethodManager::class.java))?.hideSoftInputFromWindow(card.windowToken, 0)
        } catch (_: Throwable) {
        }
        if (Build.VERSION.SDK_INT >= 33) {
            try {
                (backCallback as? OnBackInvokedCallback)?.let { card.findOnBackInvokedDispatcher()?.unregisterOnBackInvokedCallback(it) }
            } catch (_: Throwable) {
            }
        }
        backCallback = null
        try {
            if (card.parent != null) wm.removeViewImmediate(card)
        } catch (e: Throwable) {
            Log.w(TAG, "the note card would not go", e)
        }
    }

    private fun send(text: String) = guard {
        val words = text.trim()
        if (words.isEmpty()) {
            close()
            return@guard
        }
        val card = root ?: return@guard
        card.removeAllViews()
        card.addView(column(line("Sending...", DIM, 15f)))
        hideKeyboard()
        onSend?.invoke(words)
    }

    /* Edit: the words become editable, with Send and Cancel, and the keyboard comes up. The
       card moves up so the keyboard never covers it, and only now may it talk to the keyboard. */
    private fun edit(text: String) = guard {
        val card = root ?: return@guard
        val p = params ?: return@guard
        editing = true
        card.removeAllViews()
        val box = EditText(ctx).apply {
            setText(text)
            setSelection(text.length)
            setTextColor(INK)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            typeface = inter
            background = GradientDrawable().apply {
                cornerRadius = 10 * dp
                setColor(0x33000000)
                setStroke(dp.roundToInt(), 0x33FFFFFF)
            }
            val pad = (10 * dp).roundToInt()
            setPadding(pad, pad, pad, pad)
            minLines = 2
            maxLines = 6
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) = touched()
                override fun afterTextChanged(s: Editable?) {}
            })
        }
        val row = row(
            button("Cancel", DIM, false) { close() },
            button("Send", MINT, true) { send(box.text.toString()) },
        )
        card.addView(column(box, row))
        p.flags = p.flags and WindowManager.LayoutParams.FLAG_ALT_FOCUSABLE_IM.inv()
        p.softInputMode = WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_VISIBLE or
            WindowManager.LayoutParams.SOFT_INPUT_ADJUST_PAN
        p.y = (ctx.resources.displayMetrics.heightPixels * 0.12f).roundToInt()
        wm.updateViewLayout(card, p)
        box.requestFocus()
        box.post {
            try {
                ctx.getSystemService(InputMethodManager::class.java)?.showSoftInput(box, InputMethodManager.SHOW_IMPLICIT)
            } catch (_: Throwable) {
            }
        }
        touched()
    }

    /* Back: while editing with the keyboard up, it puts the keyboard away; otherwise it closes. */
    private fun back() = guard {
        val card = root ?: return@guard
        val keyboardUp = Build.VERSION.SDK_INT >= 30 &&
            card.rootWindowInsets?.isVisible(WindowInsets.Type.ime()) == true
        if (editing && keyboardUp) hideKeyboard() else close()
        touched()
    }

    private fun hideKeyboard() {
        val card = root ?: return
        try {
            ctx.getSystemService(InputMethodManager::class.java)?.hideSoftInputFromWindow(card.windowToken, 0)
        } catch (_: Throwable) {
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun add(card: FrameLayout, anchorX: Int, anchorY: Int, leftEdge: Boolean) {
        val metrics = ctx.resources.displayMetrics
        val w = (300 * dp).roundToInt()
        val p = WindowManager.LayoutParams(
            w, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            // Focusable so back reaches it, but not the keyboard until Edit; touches outside go
            // to the app below and tell the card, which then closes.
            WindowManager.LayoutParams.FLAG_ALT_FOCUSABLE_IM or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            if (Build.VERSION.SDK_INT >= 30) {
                fitInsetsTypes = WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or WindowInsets.Type.ime()
            }
            // Beside the bubble, on its open side, never over it.
            val beside = (72 * dp).roundToInt()
            x = if (leftEdge) beside.coerceAtMost((metrics.widthPixels - w).coerceAtLeast(0))
            else (metrics.widthPixels - w - beside).coerceAtLeast(0)
            y = (anchorY - (40 * dp).roundToInt()).coerceIn((40 * dp).roundToInt(), (metrics.heightPixels * 0.7f).roundToInt())
            title = "ergo note card"
        }
        card.isFocusable = true
        card.isFocusableInTouchMode = true
        card.setOnTouchListener { _, e ->
            if (e.actionMasked == MotionEvent.ACTION_OUTSIDE) {
                close()
                true
            } else {
                touched()
                false
            }
        }
        root = card
        params = p
        wm.addView(card, p)
        card.requestFocus()
        if (Build.VERSION.SDK_INT >= 33) {
            card.post {
                try {
                    val cb = OnBackInvokedCallback { back() }
                    card.findOnBackInvokedDispatcher()?.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_OVERLAY, cb)
                    backCallback = cb
                } catch (_: Throwable) {
                }
            }
        }
        touched()
    }

    private fun touched() {
        main.removeCallbacks(idle)
        if (root != null) main.postDelayed(idle, IDLE_MS)
    }

    /* Runs a piece of the card's work; any error takes the card away at once. */
    private inline fun guard(work: () -> Unit) {
        try {
            work()
        } catch (e: Throwable) {
            Log.e(TAG, "the note card failed, removing it", e)
            close()
        }
    }

    /* The card's frame. It takes back itself, whichever of its views has the focus; the
       keyboard, when up, takes the first back on its own. */
    private fun frame(): FrameLayout = object : FrameLayout(ctx) {
        override fun dispatchKeyEvent(event: KeyEvent): Boolean {
            if (event.keyCode == KeyEvent.KEYCODE_BACK) {
                if (event.action == KeyEvent.ACTION_UP) back()
                return true
            }
            return super.dispatchKeyEvent(event)
        }
    }.apply {
        background = GradientDrawable().apply {
            cornerRadius = 16 * dp
            setColor(CARD)
            setStroke(dp.roundToInt(), 0x29FFFFFF)
        }
        val pad = (16 * dp).roundToInt()
        setPadding(pad, pad, pad, pad)
        elevation = 8 * dp
    }

    private fun column(vararg views: View) = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        for ((i, v) in views.withIndex()) {
            val lp = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            if (i > 0) lp.topMargin = (12 * dp).roundToInt()
            addView(v, lp)
        }
    }

    private fun row(vararg buttons: View) = LinearLayout(ctx).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.END
        for ((i, b) in buttons.withIndex()) {
            val lp = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            if (i > 0) lp.marginStart = (8 * dp).roundToInt()
            addView(b, lp)
        }
    }

    private fun line(text: String, colour: Int, size: Float) = TextView(ctx).apply {
        this.text = text
        setTextColor(colour)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, size)
        typeface = inter
        setLineSpacing(0f, 1.15f)
    }

    private fun button(text: String, colour: Int, strong: Boolean, act: () -> Unit) = TextView(ctx).apply {
        this.text = text
        setTextColor(colour)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        typeface = inter
        val h = (14 * dp).roundToInt()
        val v = (8 * dp).roundToInt()
        setPadding(h, v, h, v)
        background = GradientDrawable().apply {
            cornerRadius = 18 * dp
            setColor(if (strong) 0x2666DBAF else 0x00000000)
            setStroke(dp.roundToInt(), if (strong) 0xCC66DBAF.toInt() else 0x29FFFFFF)
        }
        setOnClickListener { guard { touched(); act() } }
    }

    companion object {
        private const val TAG = "CCNoteCard"
        private const val IDLE_MS = 60_000L
        private const val CARD = 0xF21C1A1C.toInt()
        private const val INK = 0xFFE8E3DC.toInt()
        private const val DIM = 0xFFA8A29E.toInt()
        private const val MINT = 0xFF66DBAF.toInt()
    }
}
