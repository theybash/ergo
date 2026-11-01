package dev.ergo.app

import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/* Who is waiting on the owner: the agents with a question or a permission ask open.

   The daemon answers its voice tool pending_asks with either the words "Nobody is waiting on
   you." or a list of {agent, ask_id, kind, asking}. The wheel reads it once when it opens, on a
   background thread with short timeouts, and never waits for it. */
object Asks {

    class Ask(val agent: String, val kind: String, val asking: String, val id: String)

    /* The waiting asks, empty when nobody waits. Throws on a network or format error. Never
       call it on the main thread. */
    fun fetch(): List<Ask> {
        val conn = URL(Daemon.asks).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = CONNECT_MS
            conn.readTimeout = READ_MS
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write("""{"name":"pending_asks","args":{}}""".toByteArray()) }
            if (conn.responseCode !in 200..299) error("pending_asks answered ${conn.responseCode}")
            val body = conn.inputStream.use { it.readBytes() }.decodeToString()
            return parse(body)
        } finally {
            conn.disconnect()
        }
    }

    fun parse(body: String): List<Ask> {
        val list = JSONObject(body).opt("result") as? JSONArray ?: return emptyList()
        return List(list.length()) { i -> list.optJSONObject(i) }.filterNotNull().mapNotNull { o ->
            val agent = o.optString("agent").takeIf { it.isNotBlank() } ?: return@mapNotNull null
            Ask(agent, o.optString("kind"), o.optString("asking"), o.optString("ask_id").ifBlank { agent + ":" + o.optString("asking") })
        }
    }

    private const val CONNECT_MS = 1500
    private const val READ_MS = 2500
}
