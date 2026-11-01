package dev.ergo.app

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/* Asks for the laptop's address: on the first run, from the error page, and from the
   "Change address" shortcut on the app icon (long-press). */
class SetupActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (24 * resources.displayMetrics.density).toInt()
        val title = TextView(this).apply {
            text = "Your laptop's address"
            setTextColor(Color.parseColor("#ECE8E1"))
            textSize = 22f
        }
        val help = TextView(this).apply {
            text = "Your laptop's Tailscale address, where Ergo runs. For example " +
                "https://my-laptop.your-tailnet.ts.net"
            setTextColor(Color.parseColor("#9A948B"))
            textSize = 14f
            setPadding(0, pad / 2, 0, pad)
        }
        val field = EditText(this).apply {
            setText(Daemon.base)
            hint = "https://my-laptop.your-tailnet.ts.net"
            setHintTextColor(Color.parseColor("#57534E"))
            setTextColor(Color.parseColor("#ECE8E1"))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            imeOptions = EditorInfo.IME_ACTION_DONE
            isSingleLine = true
        }
        val error = TextView(this).apply {
            setTextColor(Color.parseColor("#E5786D"))
            textSize = 14f
            setPadding(0, pad / 3, 0, pad / 3)
        }
        val save = Button(this).apply { text = "Save" }
        val go = {
            val url = Daemon.clean(field.text.toString())
            if (url == null) {
                error.text = "That is not an address. Use http:// or https://, a host, and a port if needed."
            } else {
                Daemon.save(url)
                startActivity(Intent(this, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
                finish()
            }
        }
        save.setOnClickListener { go() }
        field.setOnEditorActionListener { _, id, _ -> if (id == EditorInfo.IME_ACTION_DONE) { go(); true } else false }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(Color.parseColor("#0F0E0D"))
            setPadding(pad, pad, pad, pad)
            addView(title); addView(help); addView(field); addView(error); addView(save)
        })
    }
}
