package dev.ergo.app

import android.app.Application
import android.content.Context
import androidx.core.content.edit
import com.getcapacitor.CapConfig
import org.json.JSONObject
import java.io.File

/* The address of the owner's laptop, where the daemon runs.

   The app has no address built in. On the first run SetupActivity asks for one, checks its form
   and saves it here. Every part of the app reads it from here: the console page, the wheel, the
   notices, the calls, the assist bar. A build may still bake one in with -PdaemonUrl=..., and then
   it is the address until the owner saves another. */
object Daemon {

    private const val PREFS = "daemon"
    private const val KEY = "url"

    @Volatile
    private var saved: String? = null
    private lateinit var app: Context

    fun init(ctx: Context) {
        app = ctx.applicationContext
        saved = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, null)
    }

    /* The address with no slash at the end, or "" when none is set yet. */
    val base: String
        get() = (saved ?: BuildConfig.DAEMON_URL).trimEnd('/')

    val isSet: Boolean get() = base.isNotEmpty()

    val phone get() = "$base/api/phone"
    val state get() = "$base/api/state"
    val note get() = "$base/api/voice/note"
    val asks get() = "$base/api/voice/tool"
    val events get() = "$base/api/events"

    fun save(url: String) {
        val clean = url.trim().trimEnd('/')
        app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit(commit = true) { putString(KEY, clean) }
        saved = clean
    }

    /* The form check: http or https, a host, an optional port, nothing after the path.
       Returns the cleaned address, or null when it is not one. A bare host or host:port gets
       http:// in front, since most laptops on a home network serve plain http. */
    fun clean(typed: String): String? {
        var s = typed.trim().trimEnd('/')
        if (s.isEmpty()) return null
        if (!s.contains("://")) s = "http://$s"
        val uri = try { java.net.URI(s) } catch (_: Exception) { return null }
        if (uri.scheme != "http" && uri.scheme != "https") return null
        val host = uri.host ?: return null
        if (host.isBlank() || host.contains(' ')) return null
        if (uri.port != -1 && uri.port !in 1..65535) return null
        if (uri.query != null || uri.fragment != null) return null
        if (!uri.path.isNullOrEmpty() && uri.path != "/") return null
        return s
    }

    /* Capacitor's config with server.url set to the saved address. Capacitor reads its config
       from a file, so the bundled one is copied with the address put in. */
    fun capConfig(ctx: Context): CapConfig {
        val json = JSONObject(ctx.assets.open("capacitor.config.json").use { it.readBytes().decodeToString() })
        val server = json.optJSONObject("server") ?: JSONObject().also { json.put("server", it) }
        server.put("url", base)
        val dir = File(ctx.filesDir, "capconfig").apply { mkdirs() }
        File(dir, "capacitor.config.json").writeText(json.toString())
        return CapConfig.loadFromFile(ctx, dir.path)
    }
}

/* Loads the saved address before any activity, service or receiver runs. */
class ErgoApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Daemon.init(this)
    }
}
