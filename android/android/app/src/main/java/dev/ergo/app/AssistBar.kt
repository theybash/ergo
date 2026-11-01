package dev.ergo.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.KeyguardManager
import android.app.assist.AssistStructure
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.GradientDrawable
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.service.voice.VoiceInteractionSession
import android.speech.RecognitionListener
import android.speech.RecognitionService
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.text.Editable
import android.text.TextUtils
import android.text.TextWatcher
import android.util.Base64
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.core.content.res.ResourcesCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/* The bar Ergo opens when the owner holds the power key. One slim glass line at the bottom, over a
   flat dim of the app: the text box, the screen button, the mic and Send. Above it, only what has
   something to say.

   - He types, and Jev picks the agent as he types: the Send button turns into that agent's face.
     Before he types it shows the agent he used last. A long press on Send lists the agents, and a
     tap there sends to that one, or keeps it for the next message when the box is empty.
   - Hold the mic: a voice memo, sent the moment he lets go, written down on the laptop. Slide
     left while holding to throw it away. Tap the mic: dictation, the words appear in the box.
   - The screen button, left of the mic, adds the screen: the screenshot, the text Android hands
     over and the page link. Added, it shows a small copy of the screen. Without that tap the
     message is only his words. Above the bar, the asks that fit the app, on one line.
   - After a send he can leave at once. The thread card keeps his question and, when it comes,
     the answer; it is there the next time the bar opens (AssistThread), and a quiet notification
     says when the answer came while the bar was closed. Typing continues it with the same agent;
     the x clears it.
   - Selected text in the app shows as a quote; a tap puts it in the box.
   - The laptop out of reach: "The laptop seems to be offline". Nothing is queued.

   Every network call runs off the main thread and lands back on it; every failure becomes one
   line in the bar, never a crash in the assistant window. */
class AssistBar(context: Context) : VoiceInteractionSession(context) {

    private val ctx: Context = context
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newFixedThreadPool(3)
    private val dp = ctx.resources.displayMetrics.density
    private val inter = try {
        ResourcesCompat.getFont(ctx, R.font.inter_medium)
    } catch (_: Throwable) {
        null
    }
    private val prefs = ctx.getSharedPreferences("assist", Context.MODE_PRIVATE)

    class Agent(val id: String, val name: String)

    // What the screen gave for this press. Cleared at every show.
    private var shot: Bitmap? = null
    private var screenText: String? = null
    private var ocrText: String? = null    // the words read off the screenshot, when the app gave few
    private var selection: String? = null
    private var app: String? = null
    private var pkg: String? = null
    private var argQuote: String? = null   // text he selected or shared before the bar opened
    private var url: String? = null
    private var locked = false
    private var screenAdded = false
    private var sharedPic = false          // a picture shared to Ergo is the screen this time
    private val frost = Frost()

    private var agents: List<Agent> = emptyList()
    private var jev: Agent? = null
    private var chosen: Agent? = null       // his pick, or the agent of the last send in this bar
    private var chosenByHand = false
    private var routeSeq = 0
    private var replySeq = 0

    // Views.
    private lateinit var root: FrameLayout
    private lateinit var column: LinearLayout
    private lateinit var note: TextView
    private lateinit var quote: TextView
    private lateinit var chips: LinearLayout
    private lateinit var peek: LinearLayout
    private lateinit var peekFace: FaceView
    private lateinit var peekTitle: TextView
    private lateinit var peekSpin: ProgressBar
    private lateinit var peekText: TextView
    private lateinit var peekScroll: ScrollView
    private lateinit var list: LinearLayout
    private lateinit var listRows: LinearLayout
    private lateinit var bar: LinearLayout
    private lateinit var panel: LinearLayout
    private lateinit var peekAsk: TextView
    private lateinit var seeCard: LinearLayout
    private lateinit var shotBtn: IconView
    private lateinit var floatBtn: IconView
    // Float mode: the agent's face stays on the edge after Send (on by itself in landscape).
    private var floating = false
    private lateinit var box: EditText
    private lateinit var recLine: TextView
    private lateinit var mic: IconView
    private lateinit var send: FaceView
    private lateinit var fan: FrameLayout
    private lateinit var memoLayer: FrameLayout
    private lateinit var edgeMic: RoundKey
    private lateinit var memoKeys: List<RoundKey>      // discard, send, edit
    private lateinit var memoTime: TextView
    private var memoHover = -1
    private var memoLocked = false
    private lateinit var fanName: TextView
    private val fanItems = mutableListOf<Pair<Agent?, View>>()   // null agent: the dots for everyone
    private var fanHover = -1

    // -- the session ----------------------------------------------------------------------------

    override fun onCreateContentView(): View {
        build()
        return root
    }

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        guard {
            window?.window?.let { w ->
                w.setBackgroundDrawable(ColorDrawable(Color.TRANSPARENT))
                if (Build.VERSION.SDK_INT >= 30) w.setDecorFitsSystemWindows(false)
                @Suppress("DEPRECATION")
                w.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_NOTHING or
                    android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN)
            }
            shot = null; screenText = null; ocrText = null; selection = null; app = null; url = null; pkg = null
            frost.clear()
            screenAdded = false
            sharedPic = args?.getString(AssistService.ARG_SHARED) != null
            argQuote = args?.getString(AssistService.ARG_QUOTE)?.trim()?.ifBlank { null }
            chips.removeAllViews()
            chips.visibility = View.GONE
            chosen = null; chosenByHand = false; jev = null
            locked = ctx.getSystemService(KeyguardManager::class.java)?.isKeyguardLocked == true
            peek.visibility = View.GONE
            list.visibility = View.GONE
            quote.visibility = View.GONE
            shotBtn.visibility = if (locked) View.GONE else View.VISIBLE
            shotBtn.pic = null
            shotBtn.on = false
            floating = ctx.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE
            floatBtn.on = floating
            floatBtn.visibility = if (locked) View.GONE else View.VISIBLE
            recLine.visibility = View.GONE
            box.visibility = View.VISIBLE
            // A draft left in the last ten minutes comes back: a bar closed by a stray tap loses
            // nothing.
            val draft = prefs.getString("draft", "").orEmpty()
            val at = prefs.getLong("draftAt", 0L)
            box.setText(if (draft.isNotBlank() && System.currentTimeMillis() - at < 10 * 60_000) draft else "")
            box.setSelection(box.text.length)
            send.agent(null)
            root.background?.alpha = 0
            root.animate().cancel()
            android.animation.ValueAnimator.ofInt(0, 255).apply {
                duration = 180
                addUpdateListener { root.background?.alpha = it.animatedValue as Int }
                start()
            }
            say(null)
            root.performHapticFeedback(if (Build.VERSION.SDK_INT >= 30) HapticFeedbackConstants.GESTURE_START else HapticFeedbackConstants.VIRTUAL_KEY)
            showQuote()
            args?.getString(AssistService.ARG_SHARED)?.let { sharedPicture(it) }
            loadAgents()
            // The keyboard waits until he taps the box (The owner, 26 Oct: "Keyboard should open up
            // only when I go to type").
            box.clearFocus()
            column.isFocusableInTouchMode = true
            column.requestFocus()
            // The last thread comes back: the answer he left the bar for, or the spinner if the
            // agent is still on it. Typing now continues it with the same agent.
            AssistThread.listener = { t -> main.post { showThread(t, fresh = true) } }
            val t = if (locked) null else AssistThread.get(ctx)
            if (t != null) {
                showThread(t, fresh = false)
                AssistThread.resume(ctx, t)
            }
            routeSoon(0)
            fitCard()
        }
    }

    override fun onHandleAssist(state: AssistState) {
        if (!state.isFocused) return
        guard {
            val structure = state.assistStructure
            val content = state.assistContent
            url = content?.webUri?.toString()
            // Opened from Ergo's own notification, the screen behind is Ergo's blank stand-in:
            // nothing worth adding.
            if (structure?.activityComponent?.packageName == ctx.packageName) {
                pkg = ctx.packageName
                url = null
                main.post { showSeen(); showChips() }
                return@guard
            }
            if (structure != null) {
                pkg = structure.activityComponent?.packageName
                app = pkg?.let { label(it) }
                val words = StringBuilder()
                var picked: String? = null
                for (i in 0 until structure.windowNodeCount) {
                    walk(structure.getWindowNodeAt(i).rootViewNode, words) { s -> if (picked == null) picked = s }
                }
                screenText = words.toString().trim().ifBlank { null }
                selection = picked
            }
            main.post { showQuote(); showSeen(); showChips() }
        }
    }

    override fun onHandleScreenshot(screenshot: Bitmap?) {
        if (sharedPic) return
        shot = screenshot
        if (screenshot != null) readWords(screenshot)
        if (screenshot != null) io.execute {
            val soft = Frost.blur(screenshot)
            val edge = Frost.blurEdge(screenshot)
            main.post {
                if (shot === screenshot) {
                    frost.set(soft, edge)
                    showSeen()
                }
            }
        }
    }

    override fun onHide() {
        AssistThread.listener = null
        guard {
            // Closed while recording: the mic stops, and the words so far stay as the draft.
            live?.let { it.cancel(); memoEnd() }
            dictateStop()
            prefs.edit().putString("draft", box.text.toString()).putLong("draftAt", System.currentTimeMillis()).apply()
        }
        super.onHide()
    }

    override fun onBackPressed() {
        if (list.visibility == View.VISIBLE) {
            list.visibility = View.GONE
            AssistThread.get(ctx)?.let { showThread(it, fresh = false) }
            fitCard()
        } else hide()
    }

    override fun onLockscreenShown() {
        hide()
    }

    // -- building the bar -----------------------------------------------------------------------

    @SuppressLint("ClickableViewAccessibility")
    private fun build() {
        // A flat dim over the app, so the bar stands out on any screen. A tap on it closes the bar.
        root = FrameLayout(ctx).apply {
            background = EdgeDrawable(frost, DIM_SCREEN)
            // A tap on the dim closes the bar, but never while a memo records: that would lose it.
            setOnClickListener { if (live == null) hide() }
        }
        column = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            isClickable = true
        }
        root.addView(column, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))
        frost.attach(root)
        root.setOnApplyWindowInsetsListener { _, insets ->
            val bottom = if (Build.VERSION.SDK_INT >= 30) {
                max(insets.getInsets(WindowInsets.Type.ime()).bottom,
                    insets.getInsets(WindowInsets.Type.navigationBars()).bottom)
            } else {
                @Suppress("DEPRECATION") insets.systemWindowInsetBottom
            }
            column.setPadding(px(10), 0, px(10), bottom + px(10))
            insets
        }

        // The agent list, for the long press on Send.
        listRows = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }
        list = card().apply {
            addView(ScrollView(ctx).apply {
                addView(listRows)
                isVerticalScrollBarEnabled = false
            }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
            visibility = View.GONE
        }
        column.addView(list, spaced())

        // The thread: who he asked, what he asked, and the answer or a spinner. It stays until he
        // clears it with the x, so he can leave and find the answer here next time.
        peekFace = FaceView(ctx, 24)
        peekTitle = text("", INK, 14f).apply { setSingleLine(true); ellipsize = TextUtils.TruncateAt.END }
        peekSpin = ProgressBar(ctx, null, android.R.attr.progressBarStyleSmall).apply {
            indeterminateTintList = android.content.res.ColorStateList.valueOf(DIM)
        }
        val close = IconView(ctx, IconView.CLOSE).apply {
            setOnClickListener { clearThread() }
        }
        val head = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(peekFace, LinearLayout.LayoutParams(px(24), px(24)))
            addView(peekTitle, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { leftMargin = px(10) })
            addView(peekSpin, LinearLayout.LayoutParams(px(16), px(16)).apply { rightMargin = px(6) })
            addView(close, LinearLayout.LayoutParams(px(32), px(32)))
        }
        peekAsk = text("", DIM, 13f).apply { setSingleLine(true); ellipsize = TextUtils.TruncateAt.END }
        peekText = text("", INK, 15f).apply { setLineSpacing(0f, 1.25f) }
        peek = card().apply {
            setPadding(px(16), px(10), px(8), px(14))
            addView(head)
            addView(peekAsk, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(4); rightMargin = px(8) })
            peekScroll = ScrollView(ctx).apply {
                addView(peekText)
                isVerticalScrollBarEnabled = false
            }
            addView(peekScroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(8); rightMargin = px(8) })
            visibility = View.GONE
            setOnClickListener { openChat() }
            addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> clearOfMic() }
        }
        column.addView(peek, spaced())

        // The screen button, left of the mic (The owner, 26 Oct). Added, it shows a small copy of
        // the screen with a mint edge.
        shotBtn = IconView(ctx, IconView.IMAGE).apply {
            setOnClickListener { toggleScreen() }
        }
        floatBtn = IconView(ctx, IconView.FLOAT).apply {
            setOnClickListener {
                floating = !floating
                on = floating
                performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                say(if (floating) "Float: the face stays on the edge after Send" else null)
            }
        }

        // One quiet line: how to use the mic, the laptop being away, or what went wrong.
        note = text("", DIM, 13f).apply {
            setPadding(px(14), px(8), px(14), px(8))
            visibility = View.GONE
        }

        // The text he selected in the app, as a quote; a tap puts it in the box.
        quote = text("", DIM, 14f).apply {
            maxLines = 2
            ellipsize = TextUtils.TruncateAt.END
            setPadding(px(14), px(8), px(14), px(8))
            visibility = View.GONE
            setOnClickListener { useQuote() }
        }

        // One-tap asks that fit the app on screen: one line of plain words, as many as fit.
        chips = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, 0, px(4), px(4))
            visibility = View.GONE
        }

        seeCard = card().apply {
            setPadding(px(4), px(4), px(4), px(4))
            addView(quote)
            addView(chips)
            addView(note)
            visibility = View.GONE
        }
        column.addView(seeCard, spaced())

        // The bar: one slim line, the same size from the first moment.
        box = EditText(ctx).apply {
            background = null
            setTextColor(INK)
            setHintTextColor(HINT)
            hint = "Ask an agent"
            typeface = inter
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f)
            maxLines = 5
            minHeight = px(48)
            setPadding(px(14), px(10), px(4), px(10))
            imeOptions = EditorInfo.IME_ACTION_SEND
            inputType = EditorInfo.TYPE_CLASS_TEXT or EditorInfo.TYPE_TEXT_FLAG_MULTI_LINE or
                EditorInfo.TYPE_TEXT_FLAG_CAP_SENTENCES
            setOnClickListener { showKeyboard() }
            setOnFocusChangeListener { _, has -> if (has) showKeyboard() }
            setOnEditorActionListener { _, action, _ ->
                if (action == EditorInfo.IME_ACTION_SEND) { sendNow(); true } else false
            }
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun afterTextChanged(s: Editable?) {
                    if (::chips.isInitialized) {
                        chips.visibility = if (s.isNullOrBlank() && chips.childCount > 0) View.VISIBLE else View.GONE
                        fitCard()
                    }
                    if (!dictating && live == null) routeSoon(350)
                }
            })
        }
        recLine = text("", INK, 16f).apply {
            gravity = Gravity.CENTER_VERTICAL
            minHeight = px(48)
            setPadding(px(14), 0, px(4), 0)
            visibility = View.GONE
        }
        mic = IconView(ctx, IconView.MIC)     // not on the bar any more; dictation still lights it
        send = FaceView(ctx, 40).apply {
            setOnTouchListener { v, e -> sendTouch(v, e) }
        }
        bar = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(box, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(recLine, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(shotBtn, LinearLayout.LayoutParams(px(44), px(48)))
            addView(floatBtn, LinearLayout.LayoutParams(px(40), px(48)))
            addView(send, LinearLayout.LayoutParams(px(48), px(48)))
        }
        panel = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            isClickable = true
            glass(this, 28)
            setPadding(px(2), px(4), px(4), px(4))
            addView(bar)
        }
        column.addView(panel)

        // The picker: held Send raises a column of faces straight up from it.
        fanName = text("", INK, 16f).apply {
            setSingleLine(true)
            setPadding(px(14), px(6), px(14), px(6))
            visibility = View.GONE
        }
        glass(fanName, 18)
        fan = FrameLayout(ctx).apply {
            visibility = View.GONE
            addView(fanName, FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        }
        root.addView(fan, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        // The dictaphone: a mic on the right edge, just left of the power button, so the thumb
        // that held power slides straight onto it (The owner, 26 Oct). Hold it and it records at
        // once. Slide left to throw it away, down to send it for Jev to route, up to put the
        // words in the box. Let go anywhere else and it keeps recording, for as long as he
        // talks, with the three keys left on screen to tap.
        memoLayer = FrameLayout(ctx)
        edgeMic = RoundKey(ctx, RoundKey.MIC).apply { setOnTouchListener { v, e -> edgeTouch(v, e) } }
        glass(edgeMic, EDGE_MIC / 2)
        memoKeys = listOf(RoundKey.DISCARD, RoundKey.SEND, RoundKey.EDIT).map { k ->
            RoundKey(ctx, k).apply {
                visibility = View.GONE
                glass(this, MEMO_KEY / 2)
                setOnClickListener { memoAct(k) }
            }
        }
        memoLayer.addView(edgeMic, FrameLayout.LayoutParams(px(EDGE_MIC), px(EDGE_MIC)))
        memoKeys.forEach { memoLayer.addView(it, FrameLayout.LayoutParams(px(MEMO_KEY), px(MEMO_KEY))) }
        memoTime = text("", INK, 14f).apply {
            gravity = Gravity.CENTER
            setShadowLayer(6f * dp, 0f, 0f, 0xCC000000.toInt())
            visibility = View.GONE
        }
        memoLayer.addView(memoTime, FrameLayout.LayoutParams(px(MEMO_KEY + 40), px(24)))
        root.addView(memoLayer, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> placeEdge() }
    }

    /* The mic sits at the power button's height on the right edge, the three keys around it:
       discard to its left, send below, edit above. */
    private fun placeEdge() {
        val h = root.height
        val w = root.width
        if (h == 0 || w == 0) return
        val m = px(EDGE_MIC)
        val k = px(MEMO_KEY)
        val cx = w - px(EDGE_GAP) - m / 2
        val cy = (h * POWER_Y).roundToInt()
        fun put(v: View, x: Int, y: Int, w: Int, h: Int = w) {
            val lp = v.layoutParams as FrameLayout.LayoutParams
            if (lp.leftMargin != x - w / 2 || lp.topMargin != y - h / 2) {
                lp.gravity = Gravity.TOP or Gravity.START
                lp.leftMargin = x - w / 2
                lp.topMargin = y - h / 2
                v.layoutParams = lp
            }
        }
        val d = px(MEMO_REACH)
        put(edgeMic, cx, cy, m)
        put(memoKeys[0], cx - d, cy, k)
        put(memoKeys[1], cx, cy + d, k)
        put(memoKeys[2], cx, cy - d, k)
        put(memoTime, cx - px(20), cy + d + k / 2 + px(16), px(MEMO_KEY + 40), px(24))
    }

    /* A long answer made the card tall enough that its top, and the x on it, sat under the edge
       mic (The owner, 31 Oct: "I can't see x to close agent window because of mic"). The card's top
       stays below the mic; the answer scrolls inside it instead. */
    private fun clearOfMic() {
        if (peek.visibility != View.VISIBLE || root.height == 0) return
        val at = IntArray(2)
        val base = IntArray(2)
        peek.getLocationInWindow(at)
        root.getLocationInWindow(base)
        val micBottom = (root.height * POWER_Y).roundToInt() + px(EDGE_MIC) / 2 + px(8)
        val over = micBottom - (at[1] - base[1])
        if (over <= 0) return
        val lp = peekScroll.layoutParams
        val want = (peekScroll.height - over).coerceAtLeast(px(72))
        if (lp.height == want) return
        lp.height = want
        main.post { peekScroll.layoutParams = lp }
    }

    private fun ripple() = android.graphics.drawable.RippleDrawable(
        android.content.res.ColorStateList.valueOf(0x1FFFFFFF), null,
        GradientDrawable().apply { cornerRadius = px(14).toFloat(); setColor(Color.WHITE) })

    /* The card above the bar shows only when something in it does. */
    private fun fitCard() {
        val any = quote.visibility == View.VISIBLE ||
            chips.visibility == View.VISIBLE || note.visibility == View.VISIBLE
        seeCard.visibility = if (any && list.visibility != View.VISIBLE) View.VISIBLE else View.GONE
    }

    private fun card(): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        glass(this, 20)
        setPadding(px(16), px(14), px(16), px(14))
        isClickable = true
    }

    /* A card in the app's frosted glass. It redraws when it moves, since what shows through
       depends on where it sits: the keyboard lifts the whole bar. */
    private fun glass(v: View, radiusDp: Int) {
        v.background = GlassDrawable(v, frost, px(radiusDp).toFloat(), max(1f, dp))
        frost.attach(v)
        v.addOnLayoutChangeListener { view, _, _, _, _, _, _, _, _ -> view.invalidate() }
    }

    private fun spaced(gap: Int = 8) = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = px(gap) }

    private fun text(s: String, color: Int, sp: Float) = TextView(ctx).apply {
        text = s
        setTextColor(color)
        typeface = inter
        setTextSize(TypedValue.COMPLEX_UNIT_SP, sp)
    }

    private fun px(v: Int) = (v * dp).roundToInt()

    // -- the quiet line -------------------------------------------------------------------------

    private fun say(s: String?, color: Int = DIM) {
        if (s.isNullOrBlank()) {
            note.visibility = View.GONE
        } else {
            note.text = s
            note.setTextColor(color)
            note.visibility = View.VISIBLE
        }
        if (::seeCard.isInitialized) fitCard()
    }

    private fun showQuote() {
        val s = argQuote ?: selection
        if (s.isNullOrBlank() || locked) {
            quote.visibility = View.GONE
            return
        }
        quote.text = "“" + s.trim() + "”"
        quote.visibility = View.VISIBLE
        fitCard()
    }

    private fun useQuote() {
        val s = (argQuote ?: selection)?.trim() ?: return
        val now = box.text.toString()
        val quoted = "“$s” "
        if (!now.contains(quoted)) {
            box.setText(quoted + now)
            box.setSelection(box.text.length)
        }
        quote.visibility = View.GONE
        fitCard()
        box.requestFocus()
    }

    // -- the screen -----------------------------------------------------------------------------

    private fun toggleScreen() {
        if (locked) return
        if (!screenAdded && shot == null && screenText == null && url == null) {
            say("This app hides its screen, or screen use is off in the digital assistant settings.")
            shotBtn.performHapticFeedback(HapticFeedbackConstants.REJECT)
            return
        }
        screenOn(!screenAdded)
        shotBtn.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
    }

    /* The screen button: there unless the phone is locked or the screen is Ergo's own. */
    private fun showSeen() {
        shotBtn.visibility = if (locked || ownScreen()) View.GONE else View.VISIBLE
        shotBtn.pic = shot
        screenOn(screenAdded)
    }

    private fun ownScreen() = pkg == ctx.packageName && !sharedPic

    /* The words on the screen for Jev and the agent: what the app handed over, and when that is
       thin, the words read off the screenshot as well (The owner, 26 Oct: Jev should judge by the
       screenshot too). */
    private fun screenWords(): String? {
        val given = screenText
        if (given != null && given.length >= THIN_TEXT) return given
        return listOfNotNull(given, ocrText).joinToString("\n").ifBlank { null }
    }

    /* Read the words in a picture on the phone, only when the app gave little text: a photo, a
       game, an app that draws its own screen. When they come, Jev looks again. */
    private fun readWords(b: Bitmap) {
        if ((screenText?.length ?: 0) >= THIN_TEXT) return
        try {
            val soft = if (b.config == Bitmap.Config.HARDWARE) b.copy(Bitmap.Config.ARGB_8888, false) else b
            com.google.mlkit.vision.text.TextRecognition
                .getClient(com.google.mlkit.vision.text.latin.TextRecognizerOptions.DEFAULT_OPTIONS)
                .process(com.google.mlkit.vision.common.InputImage.fromBitmap(soft, 0))
                .addOnSuccessListener { got ->
                    if (shot !== b) return@addOnSuccessListener
                    ocrText = got.text.trim().ifBlank { null }?.take(SCREEN_CHARS)
                    if (screenAdded && ocrText != null) routeSoon(0)
                }
                .addOnFailureListener { Log.i(TAG, "no words read off the screen: $it") }
        } catch (e: Throwable) {
            Log.i(TAG, "the text reader is not on this phone: $e")
        }
    }

    private fun screenOn(on: Boolean) {
        val changed = on != screenAdded
        screenAdded = on
        if (::shotBtn.isInitialized) shotBtn.on = on
        // The screen can change who it is for: Jev looks again, words or not.
        if (changed && ::box.isInitialized) routeSoon(0)
    }

    private fun host(u: String): String = try {
        android.net.Uri.parse(u).host?.removePrefix("www.") ?: u
    } catch (_: Throwable) {
        u
    }

    private fun label(pkg: String): String = try {
        val pm = ctx.packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (_: Throwable) {
        pkg
    }

    private fun walk(node: AssistStructure.ViewNode?, out: StringBuilder, selected: (String) -> Unit) {
        if (node == null || out.length > SCREEN_CHARS) return
        if (node.visibility != View.VISIBLE) return
        val t = node.text?.toString()
        if (!t.isNullOrBlank()) {
            out.append(t.trim()).append('\n')
            val a = node.textSelectionStart
            val b = node.textSelectionEnd
            if (a >= 0 && b > a && b <= t.length) selected(t.substring(a, b))
        } else {
            node.contentDescription?.toString()?.takeIf { it.isNotBlank() }?.let { out.append(it.trim()).append('\n') }
        }
        for (i in 0 until node.childCount) walk(node.getChildAt(i), out, selected)
    }

    private fun screenJpeg(): String? {
        val b = shot ?: return null
        return try {
            val long = max(b.width, b.height)
            val scaled = if (long > 1440) {
                val f = 1440f / long
                Bitmap.createScaledBitmap(b, (b.width * f).roundToInt(), (b.height * f).roundToInt(), true)
            } else b
            val out = ByteArrayOutputStream()
            scaled.compress(Bitmap.CompressFormat.JPEG, 82, out)
            Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
        } catch (e: Throwable) {
            Log.w(TAG, "the screenshot would not compress", e)
            null
        }
    }

    /* Up to three asks that fit what is on screen. A tap adds the screen and sends the ask, so the
       most common question about a page is one touch. */
    private fun showChips() {
        chips.removeAllViews()
        if (locked || ownScreen() || (shot == null && screenText == null && url == null)) {
            chips.visibility = View.GONE
            fitCard()
            return
        }
        val p = pkg.orEmpty()
        val asks = mutableListOf<Pair<String, Int>>()
        when {
            p.contains("youtube") -> asks += "What is this video about" to IconView.PLAY
            p.contains("maps") -> asks += "Save this place for later" to IconView.PIN
            p.contains("whatsapp") || p.contains("messaging") || p.contains("telegram") -> asks += "Draft my reply" to IconView.REPLY
            p.startsWith("com.google.android.gm") -> asks += "Draft my reply" to IconView.REPLY
        }
        asks += "Make this a task" to IconView.TASK
        asks += "Explain this" to IconView.ASK
        // As many as fit on one line, in order: a cut-off ask looks broken.
        val room = (root.width.takeIf { it > 0 } ?: ctx.resources.displayMetrics.widthPixels) - px(10 + 4 + 4 + 10) - px(8)
        val pen = Paint().apply { typeface = inter; textSize = 14f * ctx.resources.displayMetrics.scaledDensity }
        var used = 0
        for ((ask, _) in asks.distinctBy { it.first }.take(3)) {
            val w = pen.measureText(ask).roundToInt() + px(24) + (if (chips.childCount > 0) px(14) else 0)
            if (used + w > room) break
            used += w
            if (chips.childCount > 0) chips.addView(text("·", DIM, 14f).apply { setPadding(px(2), 0, px(2), 0) })
            chips.addView(text(ask, MINT, 14f).apply {
                setSingleLine(true)
                gravity = Gravity.CENTER_VERTICAL
                minHeight = px(40)
                setPadding(px(10), 0, px(10), 0)
                background = ripple()
                setOnClickListener { askChip(ask) }
            })
        }
        chips.visibility = if (box.text.isNullOrBlank() && chips.childCount > 0) View.VISIBLE else View.GONE
        fitCard()
    }

    /* An ask goes with the screen, and Jev picks the agent from the ask and the page, unless he
       chose one by hand. The face turns into Jev's pick when the send lands. */
    private fun askChip(ask: String) {
        if (!chosenByHand) chosen = null
        jev = null
        screenOn(true)
        box.setText(ask)
        box.setSelection(box.text.length)
        main.removeCallbacks(routeRun)
        sendNow()
    }

    /* A picture shared to Ergo rides along as the screen does. */
    private fun sharedPicture(uri: String) {
        io.execute {
            val b = try {
                ctx.contentResolver.openInputStream(android.net.Uri.parse(uri))?.use { BitmapFactory.decodeStream(it) }
            } catch (e: Throwable) {
                Log.w(TAG, "the shared picture would not open", e)
                null
            }
            main.post {
                if (b != null) {
                    shot = b
                    screenAdded = true
                    readWords(b)
                    showSeen()
                }
            }
        }
    }

    // -- the agents and Jev ---------------------------------------------------------------------

    private fun loadAgents() {
        io.execute {
            val got = try {
                val o = call("GET", "/api/assist/agents", null, 2500)
                val a = o.optJSONArray("agents") ?: JSONArray()
                (0 until a.length()).map { a.getJSONObject(it) }.map { Agent(it.getString("id"), it.optString("name", it.getString("id"))) }
            } catch (e: Throwable) {
                Log.i(TAG, "agents not loaded: $e")
                null
            }
            main.post {
                if (got == null) {
                    say("The laptop seems to be offline", ROSE)
                } else {
                    agents = got
                    if (note.text?.toString() == "The laptop seems to be offline") say(null)
                    got.forEach { FaceView.fetch(it.id, io, main) { send.invalidate(); peekFace.invalidate() } }
                }
            }
        }
    }

    private val routeRun = Runnable { route() }

    private fun routeSoon(delay: Long) {
        main.removeCallbacks(routeRun)
        main.postDelayed(routeRun, delay)
    }

    private fun route() {
        val words = box.text.toString().trim()
        if (chosen != null) {
            showFace(chosen)
            return
        }
        if (words.isEmpty() && !screenAdded) {
            // Before he types, the face of the agent he used last, so Send never looks empty.
            jev = null
            val last = prefs.getString("lastAgent", null)
            showFace(last?.let { Agent(it, prefs.getString("lastName", it) ?: it) })
            return
        }
        val seq = ++routeSeq
        io.execute {
            val got = try {
                // With the screen added, Jev reads the page too (The owner, 26 Oct: "let jev infer
                // from context and decide").
                val ask = JSONObject().put("text", words).put("app", app ?: JSONObject.NULL)
                if (screenAdded) {
                    url?.let { ask.put("url", it) }
                    screenWords()?.let { ask.put("screen_text", it.take(JEV_SCREEN_CHARS)) }
                }
                val o = call("POST", "/api/assist/route", ask, 5000)
                Agent(o.getString("agent"), o.optString("name", o.getString("agent")))
            } catch (e: Throwable) {
                Log.i(TAG, "no route: $e")
                null
            }
            main.post {
                if (seq != routeSeq || chosen != null) return@post
                if (got != null) {
                    jev = got
                    showFace(got)
                }
            }
        }
    }

    private fun showFace(a: Agent?) {
        val before = send.id
        send.agent(a)
        if (a != null && before != a.id) send.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
    }

    // -- the picker: hold Send, slide up a column of faces, let go on one --------------------------

    private var held = false
    private val holdSend = Runnable { openFan() }

    @SuppressLint("ClickableViewAccessibility")
    private fun sendTouch(v: View, e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                held = false
                v.isPressed = true
                main.postDelayed(holdSend, HOLD_SEND_MS)
            }
            MotionEvent.ACTION_MOVE -> if (held) hoverAt(e.rawX, e.rawY)
            MotionEvent.ACTION_UP -> {
                main.removeCallbacks(holdSend)
                v.isPressed = false
                if (!held) {
                    sendNow()
                } else {
                    hoverAt(e.rawX, e.rawY)
                    val chosenItem = fanItems.getOrNull(fanHover)
                    closeFan()
                    when {
                        chosenItem == null -> {}
                        chosenItem.first == null -> showAll()
                        else -> pick(chosenItem.first!!)
                    }
                }
            }
            MotionEvent.ACTION_CANCEL -> {
                main.removeCallbacks(holdSend)
                v.isPressed = false
                if (held) closeFan()
            }
        }
        return true
    }

    /* The last five agents he sent to, nearest his thumb first, then the dots for everyone. */
    private fun recent(): List<Agent> {
        val byId = agents.associateBy { it.id }
        val mine = prefs.getString("recent", "").orEmpty().split(',').mapNotNull { byId[it] }
        return (mine + agents).distinctBy { it.id }.take(5)
    }

    private fun openFan() {
        if (agents.isEmpty()) {
            say("The laptop seems to be offline", ROSE)
            return
        }
        held = true
        if (live == null) edgeMic.visibility = View.GONE
        send.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
        fan.removeViews(1, fan.childCount - 1)
        fanItems.clear()
        fanHover = -1
        val here = IntArray(2)
        val at = IntArray(2)
        root.getLocationOnScreen(here)
        send.getLocationOnScreen(at)
        val cx = at[0] - here[0] + send.width / 2
        panel.getLocationOnScreen(at)
        var y = at[1] - here[1] - px(14)
        val size = px(FAN_FACE)
        val faces: List<Agent?> = recent() + listOf(null)
        faces.forEachIndexed { k, a ->
            val v: View = if (a != null) FaceView(ctx, FAN_FACE).apply { agent(a); isClickable = false }
            else IconView(ctx, IconView.ALL).apply { isClickable = false; glass(this, FAN_FACE / 2) }
            y -= size
            v.alpha = 0f
            v.translationY = px(18).toFloat()
            fan.addView(v, FrameLayout.LayoutParams(size, size).apply { leftMargin = cx - size / 2; topMargin = y })
            v.animate().alpha(1f).translationY(0f).setStartDelay(k * 22L).setDuration(160).start()
            fanItems += a to v
            y -= px(FAN_GAP)
        }
        // Only the column and the bar while he chooses.
        seeCard.visibility = View.GONE
        peek.visibility = View.GONE
        list.visibility = View.GONE
        fanName.visibility = View.GONE
        fan.visibility = View.VISIBLE
    }

    private fun hoverAt(x: Float, y: Float) {
        var hit = -1
        val at = IntArray(2)
        fanItems.forEachIndexed { k, (_, v) ->
            v.getLocationOnScreen(at)
            val slack = px(FAN_GAP) / 2f + px(10)
            if (x >= at[0] - px(40) && x <= at[0] + v.width + px(40) &&
                y >= at[1] - slack && y <= at[1] + v.height + slack) hit = k
        }
        if (hit == fanHover) return
        fanItems.getOrNull(fanHover)?.second?.let { it.animate().scaleX(1f).scaleY(1f).setDuration(110).start(); (it as? FaceView)?.ring = false }
        fanHover = hit
        val item = fanItems.getOrNull(hit)
        if (item == null) {
            fanName.visibility = View.GONE
            return
        }
        val v = item.second
        v.animate().scaleX(1.3f).scaleY(1.3f).setDuration(110).start()
        (v as? FaceView)?.ring = true
        v.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
        fanName.text = item.first?.name ?: "All agents"
        fanName.visibility = View.VISIBLE
        fanName.measure(View.MeasureSpec.UNSPECIFIED, View.MeasureSpec.UNSPECIFIED)
        val lp = v.layoutParams as FrameLayout.LayoutParams
        (fanName.layoutParams as FrameLayout.LayoutParams).apply {
            leftMargin = lp.leftMargin - fanName.measuredWidth - px(18)
            topMargin = lp.topMargin + (lp.height - fanName.measuredHeight) / 2
        }
        fanName.requestLayout()
    }

    private fun closeFan() {
        held = false
        edgeMic.visibility = View.VISIBLE
        fan.visibility = View.GONE
        fanName.visibility = View.GONE
        AssistThread.get(ctx)?.let { showThread(it, fresh = false) }
        fitCard()
    }

    /* Everyone, as faces with names, four across. A tap picks one. */
    private fun showAll() {
        listRows.removeAllViews()
        var row: LinearLayout? = null
        agents.forEachIndexed { k, a ->
            if (k % 4 == 0) {
                row = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL }
                listRows.addView(row)
            }
            row!!.addView(LinearLayout(ctx).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER_HORIZONTAL
                setPadding(0, px(8), 0, px(8))
                background = ripple()
                addView(FaceView(ctx, 48).apply { agent(a); isClickable = false }, LinearLayout.LayoutParams(px(48), px(48)))
                addView(text(a.name, if (a.id == (chosen ?: jev)?.id) MINT else INK, 13f).apply {
                    setSingleLine(true); ellipsize = TextUtils.TruncateAt.END; gravity = Gravity.CENTER
                }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = px(6) })
                setOnClickListener { pick(a) }
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        }
        while ((row?.childCount ?: 4) < 4) row!!.addView(View(ctx), LinearLayout.LayoutParams(0, 1, 1f))
        peek.visibility = View.GONE
        list.visibility = View.VISIBLE
        fitCard()
    }

    private fun pick(a: Agent) {
        list.visibility = View.GONE
        fitCard()
        AssistThread.get(ctx)?.let { if (it.agent != a.id) clearThread() else showThread(it, fresh = false) }
        chosen = a
        chosenByHand = true
        showFace(a)
        if (box.text.isNotBlank() || screenAdded) sendNow()
    }

    // -- sending --------------------------------------------------------------------------------

    private fun body(): JSONObject {
        val target = chosen ?: jev
        val o = JSONObject()
        if (target != null) o.put("agent", target.id)
        if (chosen != null && chosenByHand) o.put("picked", true)
        jev?.let { o.put("jev", it.id) }
        app?.let { o.put("app", it) }
        if (screenAdded) {
            url?.let { o.put("url", it) }
            screenWords()?.let { o.put("screen_text", it.take(SCREEN_CHARS)) }
            screenJpeg()?.let { o.put("screenshot", it) }
        }
        return o
    }

    private fun sendNow() {
        val typed = box.text.toString().trim()
        // Text he selected or shared on purpose always goes with the message.
        val q = argQuote
        val words = if (q != null && !typed.contains(q)) (if (typed.isEmpty()) "“$q”" else "“$q”\n\n$typed") else typed
        if (words.isEmpty() && !screenAdded) {
            box.requestFocus()
            return
        }
        main.removeCallbacks(routeRun)
        val o = body().put("text", words)
        send.isEnabled = false
        hideKeyboard()
        io.execute {
            val r = try {
                Result(call("POST", "/api/assist/send", o, 20_000), null)
            } catch (e: Offline) {
                Result(null, "The laptop seems to be offline")
            } catch (e: Throwable) {
                Result(null, e.message ?: "That did not send")
            }
            main.post { sent(r) }
        }
    }

    private class Result(val o: JSONObject?, val error: String?)

    private fun sent(r: Result) = guard {
        send.isEnabled = true
        val o = r.o
        if (o == null) {
            say(r.error, ROSE)
            send.performHapticFeedback(HapticFeedbackConstants.REJECT)
            return@guard
        }
        send.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
        val a = Agent(o.optString("agent"), o.optString("name", o.optString("agent")))
        val asked = box.text.toString().trim().ifBlank { if (screenAdded) "About this screen" else "" }
        box.setText("")
        val recentIds = (listOf(a.id) + prefs.getString("recent", "").orEmpty().split(',')).filter { it.isNotBlank() }.distinct().take(8)
        prefs.edit().remove("draft").putString("lastAgent", a.id).putString("lastName", a.name)
            .putString("recent", recentIds.joinToString(",")).apply()
        screenOn(false)
        quote.visibility = View.GONE
        argQuote = null
        chips.visibility = View.GONE
        say(null)
        // The next message in this bar goes to the same agent: it is a conversation now. The
        // thread waits for the answer even after he leaves.
        chosen = a
        chosenByHand = false
        showFace(a)
        AssistThread.start(ctx, a.id, a.name, asked, o.optInt("turns", 0))
        if (floating && a.id.isNotBlank()) {
            // The face on the edge takes over from here; the bar steps out of the way.
            CommandService.float(ctx, a.id, a.name)
            main.postDelayed({ hide() }, 250)
            return@guard
        }
        AssistThread.get(ctx)?.let { showThread(it, fresh = true) }
    }

    /* The thread card: the answer when it has come, the spinner while the agent works. */
    private fun showThread(t: AssistThread.State, fresh: Boolean) = guard {
        val a = Agent(t.agent, t.name)
        if (!chosenByHand) {
            chosen = a
            showFace(a)
        }
        peekFace.agent(a)
        FaceView.fetch(a.id, io, main) { peekFace.invalidate(); send.invalidate() }
        peekTitle.text = t.name
        peekAsk.text = t.question
        peekAsk.visibility = if (t.question.isBlank()) View.GONE else View.VISIBLE
        if (t.answer == null) {
            peekSpin.visibility = View.VISIBLE
            peekText.text = "On it. You can leave; the answer will wait here."
            peekText.setTextColor(DIM)
        } else {
            peekSpin.visibility = View.GONE
            val changed = peekText.text.toString() != t.answer
            peekText.text = t.answer
            peekText.setTextColor(INK)
            peekText.maxLines = 12
            // A new answer measures afresh; clearOfMic trims it again if it is too tall.
            if (changed) peekScroll.layoutParams = peekScroll.layoutParams.apply { height = ViewGroup.LayoutParams.WRAP_CONTENT }
            AssistThread.markRead(ctx)
            if (fresh) peek.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
        }
        peekText.visibility = View.VISIBLE
        if (peek.visibility != View.VISIBLE && list.visibility != View.VISIBLE) {
            peek.visibility = View.VISIBLE
            // It rises out of the bar.
            peek.alpha = 0f
            peek.translationY = px(16).toFloat()
            peek.animate().alpha(1f).translationY(0f).setDuration(200).start()
        }
    }

    private fun clearThread() {
        AssistThread.clear(ctx)
        peek.animate().alpha(0f).translationY(px(16).toFloat()).setDuration(150).withEndAction {
            peek.visibility = View.GONE
            peek.alpha = 1f
            peek.translationY = 0f
        }.start()
        chosen = null
        chosenByHand = false
        routeSoon(0)
    }

    private fun openChat() {
        val a = chosen ?: return
        try {
            val i = Intent(ctx, MainActivity::class.java)
                .putExtra(MainActivity.EXTRA_GO, "chat:" + a.id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            startAssistantActivity(i)
        } catch (e: Throwable) {
            Log.w(TAG, "the chat would not open", e)
        }
        hide()
    }

    // -- the dictaphone on the edge ---------------------------------------------------------------

    private var live: LiveMemo? = null
    private var memoBefore = ""          // what the box held when he started talking

    private val memoClock = object : Runnable {
        override fun run() {
            val m = live ?: return
            val s = ((SystemClock.elapsedRealtime() - m.startedAt) / 1000).toInt()
            memoTime.text = "●  %d:%02d".format(s / 60, s % 60)
            main.postDelayed(this, 500)
        }
    }

    private fun micAllowed(): Boolean {
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) return true
        say("The mic needs Ergo's microphone permission. Open Ergo once and allow it.", ROSE)
        return false
    }

    /* The mic opens at once; OpenAI's words fill the box while he talks. */
    private fun memoStart() {
        if (!micAllowed()) return
        dictateStop()
        val m = LiveMemo(ctx.cacheDir, main,
            session = { call("POST", "/api/dictate/session", JSONObject().put("engine", "openai"), 15_000) },
            used = { s -> call("POST", "/api/openai/used", JSONObject().put("seconds", s), 10_000) },
            onText = { t -> if (live != null) showLive(t) },
            onLevel = { l -> edgeMic.level = l })
        if (!m.start()) {
            say("The microphone is busy right now.", ROSE)
            return
        }
        live = m
        memoBefore = box.text.toString().trimEnd()
        // A long memo outlasts the screen's timeout, and a dark screen would close the bar.
        window?.window?.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        edgeMic.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
        say(null)
        memoTime.text = "●  0:00"
        memoTime.visibility = View.VISIBLE
        main.post(memoClock)
    }

    private fun joined(t: String) = listOf(memoBefore, t.trim()).filter { it.isNotBlank() }.joinToString(" ")

    private fun showLive(t: String) {
        box.setText(joined(t))
        box.setSelection(box.text.length)
    }

    /* The mic and its keys are done, whatever comes next. */
    private fun memoEnd() {
        live = null
        main.removeCallbacks(memoClock)
        memoTime.visibility = View.GONE
        window?.window?.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        memoKeysOff()
    }

    private fun memoCancel() {
        val m = live ?: return
        m.cancel()
        memoEnd()
        box.setText(memoBefore)
        box.setSelection(box.text.length)
    }

    /* Down sends it, up leaves it in the box. Either way the words are OpenAI's, made live; only
       when the live connection failed does the recording go to the laptop to be written down. */
    private fun memoFinish(toBox: Boolean) {
        val m = live ?: return
        val took = SystemClock.elapsedRealtime() - m.startedAt
        memoEnd()
        // Jev's last pick was made before he spoke, from the screen alone. The words decide now,
        // unless he picked the agent himself (The owner, 26 Oct: "What should I do about the master agent?"
        // went to the assistant on that stale pick).
        if (!chosenByHand) { chosen = null; jev = null; showFace(null) }
        if (took < MEMO_MIN_MS || m.peak < SILENT) {
            m.cancel()
            box.setText(memoBefore)
            say(if (m.peak < SILENT) "Nothing heard" else "Too short")
            return
        }
        edgeMic.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
        val was = box.hint
        if (m.words().isBlank()) box.hint = "Writing it down..."
        m.stop { words ->
            box.hint = was
            if (!m.streamed) {
                fallback(m.file, toBox)
                return@stop
            }
            m.file.delete()
            if (words.isBlank()) {
                box.setText(memoBefore)
                say("Nothing heard")
                return@stop
            }
            showLive(words)
            if (toBox) routeSoon(0) else sendNow()
        }
    }

    /* The live connection failed: the laptop writes the recording down instead. */
    private fun fallback(f: File, toBox: Boolean) {
        if (f.length() > FALLBACK_MAX_BYTES) {
            f.delete()
            say("The live connection dropped and this is too long to send whole.", ROSE)
            return
        }
        say(if (toBox) "Writing it down..." else "Sending the memo...")
        val o = if (toBox) JSONObject() else body()
        io.execute {
            val got = try {
                o.put("mime", "audio/wav").put("audio", Base64.encodeToString(f.readBytes(), Base64.NO_WRAP))
                if (toBox) kotlin.Result.success(call("POST", "/api/voice/heard", o, 60_000).optString("text").trim())
                else { call("POST", "/api/assist/send", o, 30_000); kotlin.Result.success("") }
            } catch (e: Offline) {
                kotlin.Result.failure(Exception("The laptop seems to be offline"))
            } catch (e: Throwable) {
                kotlin.Result.failure(Exception(e.message ?: "It did not go through"))
            } finally {
                f.delete()
            }
            main.post {
                val text = got.getOrNull()
                when {
                    text == null -> say(got.exceptionOrNull()?.message, ROSE)
                    !toBox -> { say("Memo sent"); main.postDelayed({ hide() }, 700) }
                    text.isBlank() -> say("Nothing heard")
                    else -> { say(null); showLive(text); routeSoon(0) }
                }
            }
        }
    }

    private fun edgeTouch(v: View, e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                if (live != null) return true      // already recording: the keys do the rest
                memoStart()
                if (live == null) return true
                hideKeyboard()
                memoLocked = false
                memoHover = -1
                edgeDown[0] = e.rawX
                edgeDown[1] = e.rawY
                edgeMic.on = true
                memoKeys.forEach { k ->
                    k.visibility = View.VISIBLE
                    k.on = false
                    k.scaleX = 0.6f; k.scaleY = 0.6f; k.alpha = 0f
                    k.animate().scaleX(1f).scaleY(1f).alpha(1f).setDuration(140).start()
                }
            }
            MotionEvent.ACTION_MOVE -> {
                if (live == null || memoLocked) return true
                val dx = e.rawX - edgeDown[0]
                val dy = e.rawY - edgeDown[1]
                val far = px(MEMO_PULL)
                val pick = when {
                    -dx > far && -dx > abs(dy) -> 0
                    dy > far && dy > abs(dx) -> 1
                    -dy > far && -dy > abs(dx) -> 2
                    else -> -1
                }
                if (pick != memoHover) {
                    memoHover = pick
                    memoKeys.forEachIndexed { i, k ->
                        k.on = i == pick
                        val sc = if (i == pick) 1.25f else 1f
                        k.animate().scaleX(sc).scaleY(sc).setDuration(90).start()
                    }
                    if (pick >= 0) v.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (live == null || memoLocked) return true
                val pick = memoHover
                if (pick >= 0 && e.actionMasked == MotionEvent.ACTION_UP) {
                    memoAct(listOf(RoundKey.DISCARD, RoundKey.SEND, RoundKey.EDIT)[pick])
                } else {
                    // Let go with no slide: it keeps recording, hands free, for as long as he talks.
                    memoLocked = true
                    memoKeys.forEach { it.on = false; it.animate().scaleX(1f).scaleY(1f).setDuration(90).start() }
                    v.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                }
            }
        }
        return true
    }

    private val edgeDown = FloatArray(2)      // where the thumb came down on the edge mic

    private fun memoKeysOff() {
        if (!::memoKeys.isInitialized) return
        memoLocked = false
        memoHover = -1
        edgeMic.on = false
        edgeMic.level = 0f
        memoKeys.forEach { it.visibility = View.GONE; it.on = false; it.scaleX = 1f; it.scaleY = 1f }
    }

    private fun memoAct(kind: Int) {
        if (live == null) return
        when (kind) {
            RoundKey.DISCARD -> {
                memoCancel()
                edgeMic.performHapticFeedback(HapticFeedbackConstants.REJECT)
            }
            RoundKey.SEND -> memoFinish(toBox = false)
            RoundKey.EDIT -> memoFinish(toBox = true)
        }
    }

    // Dictation: the phone's own recognizer, chosen by name, never Ergo's stub.
    private var recognizer: SpeechRecognizer? = null
    private var dictating = false
    private var before = ""

    private fun dictateToggle() {
        if (dictating) dictateStop() else dictateStart()
    }

    private fun dictateStart() {
        if (!micAllowed()) return
        val r = makeRecognizer()
        if (r == null) {
            say("This phone has no speech recognizer for dictation. Hold the mic for a memo instead.")
            return
        }
        recognizer = r
        dictating = true
        mic.on = true
        before = box.text.toString().let { if (it.isBlank() || it.endsWith(" ")) it else "$it " }
        r.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) { say("Listening...") }
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) { mic.level = ((rmsdB + 2f) / 12f).coerceIn(0f, 1f) }
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() {}
            override fun onError(error: Int) {
                say(if (error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT) "Nothing heard" else null)
                dictateStop()
            }
            override fun onResults(results: Bundle?) {
                words(results)
                say(null)
                dictateStop()
                routeSoon(0)
            }
            override fun onPartialResults(partialResults: Bundle?) { words(partialResults) }
            override fun onEvent(eventType: Int, params: Bundle?) {}
        })
        val i = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-IN")
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, ctx.packageName)
        try {
            r.startListening(i)
        } catch (e: Throwable) {
            Log.w(TAG, "dictation would not start", e)
            dictateStop()
        }
    }

    private fun words(b: Bundle?) {
        val s = b?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull() ?: return
        box.setText(before + s)
        box.setSelection(box.text.length)
    }

    private fun dictateStop() {
        val r = recognizer
        recognizer = null
        dictating = false
        mic.on = false
        mic.level = 0f
        try { r?.destroy() } catch (_: Throwable) {}
    }

    private fun makeRecognizer(): SpeechRecognizer? {
        val pm = ctx.packageManager
        val services = try {
            pm.queryIntentServices(Intent(RecognitionService.SERVICE_INTERFACE), 0)
                .map { it.serviceInfo }.filter { it.packageName != ctx.packageName }
        } catch (_: Throwable) {
            emptyList()
        }
        val pick = services.firstOrNull { it.packageName == "com.google.android.googlequicksearchbox" }
            ?: services.firstOrNull { it.packageName.startsWith("com.google") } ?: services.firstOrNull()
        if (pick != null) {
            return try {
                SpeechRecognizer.createSpeechRecognizer(ctx, ComponentName(pick.packageName, pick.name))
            } catch (_: Throwable) {
                null
            }
        }
        if (Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(ctx)) {
            return try { SpeechRecognizer.createOnDeviceSpeechRecognizer(ctx) } catch (_: Throwable) { null }
        }
        return null
    }

    // -- the daemon -----------------------------------------------------------------------------

    private class Offline : Exception()

    /* One request to the daemon. Offline when the laptop cannot be reached at all; any other
       failure carries the daemon's own sentence. */
    private fun call(method: String, path: String, body: JSONObject?, timeout: Int): JSONObject {
        val conn = try {
            (URL(Daemon.base + path).openConnection() as HttpURLConnection)
        } catch (e: Throwable) {
            throw Offline()
        }
        try {
            conn.connectTimeout = 3000
            conn.readTimeout = timeout
            conn.requestMethod = method
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            val code = try {
                conn.responseCode
            } catch (e: java.io.IOException) {
                throw Offline()
            }
            val text = (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.use { it.readBytes() }?.decodeToString().orEmpty()
            val o = try { JSONObject(text) } catch (_: Throwable) { JSONObject() }
            if (code !in 200..299) throw RuntimeException(o.optString("error").ifBlank { "The laptop said no ($code)" })
            return o
        } catch (e: java.net.ConnectException) {
            throw Offline()
        } catch (e: java.net.UnknownHostException) {
            throw Offline()
        } catch (e: java.net.SocketTimeoutException) {
            throw Offline()
        } finally {
            conn.disconnect()
        }
    }

    /* After a send the keyboard goes, so the answer and the page are in view. */
    private fun hideKeyboard() = guard {
        ctx.getSystemService(android.view.inputmethod.InputMethodManager::class.java)
            ?.hideSoftInputFromWindow(box.windowToken, 0)
        box.clearFocus()
        column.requestFocus()
    }

    private fun showKeyboard() = guard {
        box.requestFocus()
        ctx.getSystemService(android.view.inputmethod.InputMethodManager::class.java)
            ?.showSoftInput(box, android.view.inputmethod.InputMethodManager.SHOW_IMPLICIT)
    }

    private fun locked() = ctx.getSystemService(KeyguardManager::class.java)?.isKeyguardLocked == true

    private inline fun guard(block: () -> Unit) {
        try {
            block()
        } catch (e: Throwable) {
            Log.w(TAG, "assistant bar fault", e)
        }
    }

    companion object {
        private const val TAG = "CCAssist"
        const val CARD = 0xFF1C1A1C.toInt()
        const val EDGE = 0x29FFFFFF
        const val INK = 0xFFE8E3DC.toInt()
        const val DIM = 0xFFA8A29E.toInt()
        const val MINT = 0xFF66DBAF.toInt()
        const val HINT = 0x99E8E3DC.toInt()
        const val DIM_SCREEN = 0x40000000
        const val ROSE = 0xFFE25958.toInt()
        private const val HOLD_MS = 280L
        private const val HOLD_SEND_MS = 320L
        private const val FAN_FACE = 46
        private const val FAN_GAP = 14
        private const val MEMO_MIN_MS = 700L
        private const val MEMO_MAX_MS = 30 * 60_000      // long sessions: half an hour, about 15 MB
        private const val FALLBACK_MAX_BYTES = 24L * 1024 * 1024   // what OpenAI takes in one file
        private const val EDGE_MIC = 60                  // dp
        private const val MEMO_KEY = 52
        private const val MEMO_REACH = 84                // from the mic's middle to each key's
        private const val MEMO_PULL = 44                 // how far the thumb moves to pick one
        private const val EDGE_GAP = 10
        private const val POWER_Y = 0.36f                // the power button's height on his phone
        private const val SILENT = 0.06f
        private const val SCREEN_CHARS = 20_000
        private const val JEV_SCREEN_CHARS = 1500
        private const val THIN_TEXT = 600     // below this, mostly the app's own buttons: read the picture too
    }
}

/* The image button and the mic: thin line drawings, mint when on. The mic swells a little with
   the voice while it records. */
class IconView(context: Context, private val kind: Int) : View(context) {
    private val dp = context.resources.displayMetrics.density
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1.8f * dp
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }
    private val path = Path()
    var on = false
        set(v) { field = v; invalidate() }
    var level = 0f
        set(v) { field = v; invalidate() }
    var pic: Bitmap? = null
        set(v) { field = v; invalidate() }
    private val picPaint = Paint(Paint.FILTER_BITMAP_FLAG or Paint.ANTI_ALIAS_FLAG)
    private val dots = Paint(Paint.ANTI_ALIAS_FLAG)
    private val picClip = Path()

    init {
        isClickable = true
        isFocusable = true
    }

    override fun onDraw(c: Canvas) {
        val cx = width / 2f
        val cy = height / 2f
        val u = dp
        paint.color = if (on || kind >= PAGE) AssistBar.MINT else AssistBar.DIM
        if (kind == ALL) {
            dots.color = AssistBar.INK
            for (dx in intArrayOf(-1, 1)) for (dy in intArrayOf(-1, 1)) c.drawCircle(cx + dx * 5 * u, cy + dy * 5 * u, 2.4f * u, dots)
            return
        }
        if (kind == FLOAT) {
            // A screen with a window docked to its right: float mode.
            val r = RectF(cx - 10 * u, cy - 8 * u, cx + 10 * u, cy + 8 * u)
            c.drawRoundRect(r, 3 * u, 3 * u, paint)
            c.drawLine(cx + 3 * u, cy - 8 * u, cx + 3 * u, cy + 8 * u, paint)
            if (on) {
                dots.color = AssistBar.MINT
                c.drawRoundRect(RectF(cx + 3 * u, cy - 8 * u, cx + 10 * u, cy + 8 * u), 3 * u, 3 * u, dots)
            }
            return
        }
        if (kind == CLOSE) {
            paint.strokeWidth = 1.6f * u
            c.drawLine(cx - 5 * u, cy - 5 * u, cx + 5 * u, cy + 5 * u, paint)
            c.drawLine(cx + 5 * u, cy - 5 * u, cx - 5 * u, cy + 5 * u, paint)
            return
        }
        if (kind >= PAGE) { ask(c, cx, cy, u); return }
        c.save()
        if (kind == MIC && level > 0f) c.scale(1f + level * 0.18f, 1f + level * 0.18f, cx, cy)
        val p = pic
        if (kind == IMAGE && on && p != null) {
            // The screen itself, small, cropped from the top, with a mint edge.
            val r = RectF(cx - 9 * u, cy - 13 * u, cx + 9 * u, cy + 13 * u)
            val srcH = min(p.height, (p.width * 26f / 18f).roundToInt())
            picClip.reset()
            picClip.addRoundRect(r, 4 * u, 4 * u, Path.Direction.CW)
            c.save()
            c.clipPath(picClip)
            c.drawBitmap(p, android.graphics.Rect(0, 0, p.width, srcH), r, picPaint)
            c.restore()
            c.drawRoundRect(r, 4 * u, 4 * u, paint)
            c.restore()
            return
        }
        if (kind == IMAGE) {
            val r = RectF(cx - 10 * u, cy - 8 * u, cx + 10 * u, cy + 8 * u)
            c.drawRoundRect(r, 3 * u, 3 * u, paint)
            c.drawCircle(cx + 4.5f * u, cy - 3 * u, 1.8f * u, paint)
            path.reset()
            path.moveTo(cx - 10 * u, cy + 5 * u)
            path.lineTo(cx - 3 * u, cy - 1 * u)
            path.lineTo(cx + 3 * u, cy + 4 * u)
            path.lineTo(cx + 6 * u, cy + 2 * u)
            path.lineTo(cx + 10 * u, cy + 5.5f * u)
            c.drawPath(path, paint)
        } else {
            val r = RectF(cx - 4 * u, cy - 10 * u, cx + 4 * u, cy + 3 * u)
            c.drawRoundRect(r, 4 * u, 4 * u, paint)
            path.reset()
            path.addArc(RectF(cx - 7.5f * u, cy - 4 * u, cx + 7.5f * u, cy + 7 * u), 0f, 180f)
            c.drawPath(path, paint)
            c.drawLine(cx, cy + 7 * u, cx, cy + 10 * u, paint)
        }
        c.restore()
    }

    /* The small marks before each ask, 16 dp, in the same thin line. */
    private fun ask(c: Canvas, cx: Float, cy: Float, u: Float) {
        paint.strokeWidth = 1.6f * u
        path.reset()
        when (kind) {
            PAGE -> {
                c.drawRoundRect(RectF(cx - 6 * u, cy - 8 * u, cx + 6 * u, cy + 8 * u), 2 * u, 2 * u, paint)
                for (k in 0..2) c.drawLine(cx - 3 * u, cy - 3.5f * u + k * 3.5f * u, cx + 3 * u, cy - 3.5f * u + k * 3.5f * u, paint)
            }
            TASK -> {
                c.drawCircle(cx, cy, 8 * u, paint)
                path.moveTo(cx - 3.5f * u, cy + 0.2f * u)
                path.lineTo(cx - 0.8f * u, cy + 3 * u)
                path.lineTo(cx + 4 * u, cy - 2.8f * u)
                c.drawPath(path, paint)
            }
            ASK -> {
                c.drawCircle(cx, cy, 8 * u, paint)
                path.addArc(RectF(cx - 2.8f * u, cy - 5 * u, cx + 2.8f * u, cy + 0.6f * u), 180f, 230f)
                path.lineTo(cx, cy + 2 * u)
                c.drawPath(path, paint)
                c.drawPoint(cx, cy + 4.6f * u, paint)
            }
            REPLY -> {
                path.moveTo(cx - 2 * u, cy - 6 * u)
                path.lineTo(cx - 7 * u, cy - 1.5f * u)
                path.lineTo(cx - 2 * u, cy + 3 * u)
                path.moveTo(cx - 7 * u, cy - 1.5f * u)
                path.cubicTo(cx + 2 * u, cy - 1.5f * u, cx + 7 * u, cy, cx + 7 * u, cy + 6 * u)
                c.drawPath(path, paint)
            }
            PLAY -> {
                c.drawRoundRect(RectF(cx - 8 * u, cy - 6 * u, cx + 8 * u, cy + 6 * u), 3 * u, 3 * u, paint)
                path.moveTo(cx - 2 * u, cy - 3 * u)
                path.lineTo(cx + 3 * u, cy)
                path.lineTo(cx - 2 * u, cy + 3 * u)
                path.close()
                c.drawPath(path, paint)
            }
            PIN -> {
                path.addArc(RectF(cx - 6 * u, cy - 8 * u, cx + 6 * u, cy + 4 * u), 150f, 240f)
                path.lineTo(cx, cy + 8.5f * u)
                path.close()
                c.drawPath(path, paint)
                c.drawCircle(cx, cy - 2 * u, 2 * u, paint)
            }
        }
    }

    companion object {
        const val IMAGE = 1
        const val MIC = 2
        const val CLOSE = 3
        const val ALL = 4
        const val FLOAT = 5
        const val PAGE = 10
        const val TASK = 11
        const val ASK = 12
        const val REPLY = 13
        const val PLAY = 14
        const val PIN = 15
    }
}

/* An agent's face, round: its avatar from the daemon, or its first letter. With no agent, the
   send arrow. Faces are fetched once per process and kept. */
class FaceView(context: Context, private val sizeDp: Int) : View(context) {
    private val dp = context.resources.displayMetrics.density
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0x24E8E3DC }
    private val ink = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = AssistBar.INK
        textAlign = Paint.Align.CENTER
        textSize = 15f * dp
    }
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 2f * dp
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
        color = AssistBar.INK
    }
    private val clip = Path()
    var id: String? = null
        private set
    var ring = false     // the picked face in the picker: a thin mint edge
        set(v) { field = v; invalidate() }
    private val edge = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; color = AssistBar.MINT }
    private var name: String? = null
    private var shownAt = 0L

    init {
        isClickable = true
        isFocusable = true
    }

    fun agent(a: AssistBar.Agent?) {
        if (a?.id == id) return
        id = a?.id
        name = a?.name
        shownAt = SystemClock.uptimeMillis()
        invalidate()
    }

    override fun onDraw(c: Canvas) {
        val s = min(width, height).toFloat()
        val r = min(s / 2f, sizeDp * dp / 2f)
        val cx = width / 2f
        val cy = height / 2f
        // A face that just changed grows into place over 160 ms.
        val t = ((SystemClock.uptimeMillis() - shownAt) / 160f).coerceIn(0f, 1f)
        if (t < 1f) postInvalidateOnAnimation()
        val rr = r * (0.82f + 0.18f * t)
        val key = id
        if (key == null) {
            fill.alpha = if (isEnabled) 0x24 else 0x12
            c.drawCircle(cx, cy, rr, fill)
            c.drawLine(cx, cy + 6 * dp, cx, cy - 6 * dp, line)
            c.drawLine(cx - 5 * dp, cy - 1 * dp, cx, cy - 6 * dp, line)
            c.drawLine(cx + 5 * dp, cy - 1 * dp, cx, cy - 6 * dp, line)
            return
        }
        val bmp = faces[key]
        if (bmp != null) {
            c.save()
            clip.reset()
            clip.addCircle(cx, cy, rr, Path.Direction.CW)
            c.clipPath(clip)
            c.drawBitmap(bmp, null, RectF(cx - rr, cy - rr, cx + rr, cy + rr), null)
            c.restore()
        } else {
            c.drawCircle(cx, cy, rr, fill)
            ink.textSize = rr * 0.9f
            val letter = (name ?: key).take(1).uppercase()
            c.drawText(letter, cx, cy - (ink.descent() + ink.ascent()) / 2f, ink)
        }
        if (ring) {
            edge.strokeWidth = 2f * dp
            c.drawCircle(cx, cy, rr - dp, edge)
        }
    }

    companion object {
        val faces = ConcurrentHashMap<String, Bitmap>()
        private val asked = ConcurrentHashMap.newKeySet<String>()

        fun fetch(id: String, io: java.util.concurrent.Executor, main: Handler, done: () -> Unit) {
            if (!asked.add(id)) return
            io.execute {
                try {
                    val conn = URL(Daemon.base + "/file/" + android.net.Uri.encode(id) + "/avatar.png")
                        .openConnection() as HttpURLConnection
                    conn.connectTimeout = 3000
                    conn.readTimeout = 5000
                    if (conn.responseCode == 200) {
                        val b = conn.inputStream.use { BitmapFactory.decodeStream(it) }
                        if (b != null) {
                            faces[id] = b
                            main.post(done)
                        }
                    } else asked.remove(id)
                    conn.disconnect()
                } catch (_: Throwable) {
                    asked.remove(id)
                }
            }
        }
    }
}

/* The dictaphone's round keys on the edge: the mic, and the three it offers while it records.
   The glass behind is the bar's own; this draws the mark, mint when it is the one picked. */
class RoundKey(context: Context, val kind: Int) : View(context) {
    private val dp = context.resources.displayMetrics.density
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 2f * dp
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }
    private val dot = Paint(Paint.ANTI_ALIAS_FLAG)
    private val path = Path()
    var on = false
        set(v) { field = v; invalidate() }
    var level = 0f
        set(v) { field = v; invalidate() }

    init {
        isClickable = true
    }

    override fun onDraw(c: Canvas) {
        val cx = width / 2f
        val cy = height / 2f
        val u = dp
        when (kind) {
            MIC -> {
                if (on) {
                    // Recording: a red dot that breathes with his voice, under the mic.
                    dot.color = (0x55 shl 24) or (AssistBar.ROSE and 0xFFFFFF)
                    c.drawCircle(cx, cy, width / 2f * (0.62f + 0.3f * level), dot)
                }
                line.color = if (on) AssistBar.INK else AssistBar.MINT
                c.drawRoundRect(RectF(cx - 5 * u, cy - 12 * u, cx + 5 * u, cy + 3 * u), 5 * u, 5 * u, line)
                path.reset()
                path.addArc(RectF(cx - 9 * u, cy - 5 * u, cx + 9 * u, cy + 8 * u), 0f, 180f)
                c.drawPath(path, line)
                c.drawLine(cx, cy + 8 * u, cx, cy + 12 * u, line)
            }
            DISCARD -> {
                line.color = AssistBar.ROSE
                line.strokeWidth = (if (on) 2.6f else 2f) * u
                c.drawLine(cx - 7 * u, cy - 7 * u, cx + 7 * u, cy + 7 * u, line)
                c.drawLine(cx + 7 * u, cy - 7 * u, cx - 7 * u, cy + 7 * u, line)
            }
            SEND -> {
                line.color = if (on) AssistBar.MINT else AssistBar.INK
                c.drawLine(cx, cy - 9 * u, cx, cy + 9 * u, line)
                path.reset()
                path.moveTo(cx - 7 * u, cy + 2 * u)
                path.lineTo(cx, cy + 9 * u)
                path.lineTo(cx + 7 * u, cy + 2 * u)
                c.drawPath(path, line)
            }
            EDIT -> {
                line.color = if (on) AssistBar.MINT else AssistBar.INK
                path.reset()
                path.moveTo(cx - 8 * u, cy + 8 * u)
                path.lineTo(cx - 7 * u, cy + 3 * u)
                path.lineTo(cx + 4 * u, cy - 8 * u)
                path.lineTo(cx + 8 * u, cy - 4 * u)
                path.lineTo(cx - 3 * u, cy + 7 * u)
                path.close()
                c.drawPath(path, line)
                c.drawLine(cx + 1 * u, cy - 5 * u, cx + 5 * u, cy - 1 * u, line)
            }
        }
    }

    companion object {
        const val MIC = 0
        const val DISCARD = 1
        const val SEND = 2
        const val EDIT = 3
    }
}
