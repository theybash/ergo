package dev.ergo.app

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.core.content.FileProvider
import androidx.core.content.edit
import androidx.core.content.pm.PackageInfoCompat
import androidx.core.net.toUri
import org.json.JSONObject
import java.io.File
import java.lang.ref.WeakReference
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.concurrent.Executors

/* The app updates itself from the daemon, when the build names an update address
   (-PupdateUrl=https://<laptop>/app/version.json). With none, this does nothing.

   At start, and at most once an hour, it reads version.json next to the APK. When that names a
   higher versionCode than this app, it asks "Update ready, install?". A yes downloads the APK
   into the cache, checks its SHA-256 against version.json, and hands it to Android's installer.
   An APK whose hash does not match is deleted and never offered to the installer.

   Android asks the owner to allow "install unknown apps" for this app once; until then a yes
   goes to that settings page, and the install goes on when the app comes back. */
class Updater(activity: Activity) {

    private val host = WeakReference(activity)
    private val app: Context = activity.applicationContext
    private val prefs = app.getSharedPreferences("update", Context.MODE_PRIVATE)
    private val main = Handler(Looper.getMainLooper())

    /* A verified APK that waits for the install permission. */
    private var waiting: File? = null
    private var busy = false

    /* At start and on every return to the screen, at most once a minute, so an update shows the
       next time the app is opened rather than an hour later. */
    fun check() {
        // No update address in this build: the updater is off.
        if (BuildConfig.UPDATE_URL.isEmpty()) return
        val now = System.currentTimeMillis()
        if (busy || now - prefs.getLong("checked", 0) < BuildConfig.UPDATE_EVERY_MS) return
        busy = true
        io.execute {
            val offer = try {
                JSONObject(fetch(BuildConfig.UPDATE_URL).decodeToString())
            } catch (e: Exception) {
                Log.i(TAG, "no update check: $e")
                null
            }
            main.post {
                busy = false
                // Only a check that got an answer counts toward the minute.
                if (offer != null) prefs.edit { putLong("checked", now) }
                if (offer != null && offer.optLong("versionCode") > installed()) ask(offer)
            }
        }
    }

    /* Back from the settings page: the waiting APK installs if the permission is now there. */
    fun resume() {
        val file = waiting ?: return
        if (!app.packageManager.canRequestPackageInstalls()) return
        waiting = null
        install(file)
    }

    private fun installed(): Long =
        PackageInfoCompat.getLongVersionCode(app.packageManager.getPackageInfo(app.packageName, 0))

    private fun ask(offer: JSONObject) {
        val activity = host.get()?.takeUnless { it.isFinishing || it.isDestroyed } ?: return
        AlertDialog.Builder(activity)
            .setTitle("Update ready, install?")
            .setMessage("Version ${offer.optString("versionName")} is ready on Ergo.")
            .setPositiveButton("Install") { _, _ -> download(offer) }
            .setNegativeButton("Not now", null)
            .show()
    }

    private fun download(offer: JSONObject) {
        val want = offer.optString("sha256").lowercase()
        val apkUrl = offer.optString("url").ifEmpty { BuildConfig.UPDATE_URL.substringBeforeLast('/') + "/ergo.apk" }
        toast("Downloading the update")
        busy = true
        io.execute {
            val dir = File(app.cacheDir, "update").apply { mkdirs() }
            val file = File(dir, "ergo-${offer.optLong("versionCode")}.apk")
            // Older downloads go, but not one an installer screen may still have open: deleting
            // that under it made the installer say it could not parse the package.
            val hour = System.currentTimeMillis() - 60 * 60 * 1000L
            dir.listFiles()?.filter { it.name != file.name && it.lastModified() < hour }?.forEach { it.delete() }
            val ok = try {
                val body = fetch(apkUrl)
                val got = MessageDigest.getInstance("SHA-256").digest(body).joinToString("") { "%02x".format(it) }
                if (want.length == 64 && got == want) {
                    file.writeBytes(body)
                    true
                } else {
                    Log.w(TAG, "update refused: sha256 $got, version.json says $want")
                    false
                }
            } catch (e: Exception) {
                Log.w(TAG, "update download failed: $e")
                null
            }
            main.post {
                busy = false
                when (ok) {
                    true -> {
                        Log.i(TAG, "update verified: ${file.length()} bytes")
                        install(file)
                    }
                    false -> {
                        file.delete()
                        toast("The update did not match its checksum. Nothing was installed.")
                    }
                    null -> toast("The update did not download. Try again later.")
                }
            }
        }
    }

    private fun install(file: File) {
        val activity = host.get()?.takeUnless { it.isFinishing || it.isDestroyed } ?: return
        if (!app.packageManager.canRequestPackageInstalls()) {
            waiting = file
            AlertDialog.Builder(activity)
                .setTitle("Allow updates")
                .setMessage("Turn on \"Allow from this source\" for Ergo, then come back.")
                .setPositiveButton("Open settings") { _, _ ->
                    activity.startActivity(
                        Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, "package:${app.packageName}".toUri())
                    )
                }
                .setNegativeButton("Not now") { _, _ -> waiting = null }
                .show()
            return
        }
        val uri = FileProvider.getUriForFile(app, "${app.packageName}.fileprovider", file)
        val open = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, APK)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        Log.i(TAG, "opening the installer for $uri")
        activity.startActivity(open)
    }

    private fun toast(what: String) = Toast.makeText(app, what, Toast.LENGTH_LONG).show()

    private companion object {
        const val TAG = "CCUpdate"
        const val APK = "application/vnd.android.package-archive"
        val io = Executors.newSingleThreadExecutor()

        /* The whole body of a GET, or an exception. The APK is a few megabytes. */
        fun fetch(url: String): ByteArray {
            val c = URL(url).openConnection() as HttpURLConnection
            c.connectTimeout = 10_000
            c.readTimeout = 30_000
            c.useCaches = false
            try {
                if (c.responseCode != 200) error("HTTP ${c.responseCode} for $url")
                return c.inputStream.use { it.readBytes() }
            } finally {
                c.disconnect()
            }
        }
    }
}
