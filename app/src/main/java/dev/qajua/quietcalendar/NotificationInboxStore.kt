package dev.qajua.quietcalendar

import android.content.Context
import android.util.AtomicFile
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/** Private, bounded notification inbox. Nothing is accepted until a source allowlist is configured. */
class NotificationInboxStore(private val context: Context) {
    fun configure(packages: JSONArray): JSONObject = synchronized(LOCK) {
        val accepted = linkedSetOf<String>()
        for (index in 0 until packages.length()) {
            val value = packages.getString(index).trim()
            require(PACKAGE_NAME.matches(value)) { "Invalid package name" }
            accepted += value
        }
        require(accepted.size in 1..20) { "Configure 1–20 notification source packages" }
        val result = JSONObject().apply {
            put("state", "configured")
            put("packages", JSONArray(accepted.toList()))
            put("updated_at_ms", System.currentTimeMillis())
        }
        writeAtomic(configFile, result.toString())
        result
    }

    fun allowed(packageName: String): Boolean = synchronized(LOCK) {
        val config = readObject(configFile) ?: return false
        val packages = config.optJSONArray("packages") ?: return false
        (0 until packages.length()).any { packages.optString(it) == packageName }
    }

    fun append(
        notificationKey: String,
        packageName: String,
        postedAtMs: Long,
        title: String,
        text: String,
    ) = synchronized(LOCK) {
        if (!allowed(packageName)) return
        val safeTitle = title.trim().take(MAX_FIELD_LENGTH)
        val safeText = text.trim().take(MAX_FIELD_LENGTH)
        if (safeTitle.isEmpty() && safeText.isEmpty()) return
        val id = digest("$packageName\n$notificationKey\n$postedAtMs")
        val old = readArray(inboxFile)
        val retained = mutableListOf<JSONObject>()
        for (index in 0 until old.length()) {
            val item = old.optJSONObject(index) ?: continue
            if (item.optString("id") != id) retained += item
        }
        retained += JSONObject().apply {
            put("id", id)
            put("package", packageName)
            put("posted_at_ms", postedAtMs)
            put("captured_at_ms", System.currentTimeMillis())
            put("title", safeTitle)
            put("text", safeText)
        }
        val bounded = retained.takeLast(MAX_ITEMS)
        writeAtomic(inboxFile, JSONArray(bounded).toString())
    }

    fun clear() = synchronized(LOCK) {
        writeAtomic(inboxFile, "[]")
    }

    private val inboxFile: File get() = File(context.filesDir, INBOX_FILE)
    private val configFile: File get() = File(context.filesDir, CONFIG_FILE)

    private fun readArray(file: File): JSONArray =
        runCatching { if (file.exists()) JSONArray(file.readText()) else JSONArray() }.getOrElse { JSONArray() }

    private fun readObject(file: File): JSONObject? =
        runCatching { if (file.exists()) JSONObject(file.readText()) else null }.getOrNull()

    private fun writeAtomic(file: File, value: String) {
        val atomic = AtomicFile(file)
        val stream = atomic.startWrite()
        try {
            stream.write(value.toByteArray(Charsets.UTF_8))
            atomic.finishWrite(stream)
        } catch (error: Exception) {
            atomic.failWrite(stream)
            throw error
        }
    }

    private fun digest(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray())
        .take(12)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    companion object {
        const val INBOX_FILE = "notification-inbox.json"
        const val CONFIG_FILE = "notification-config.json"
        private const val MAX_ITEMS = 50
        private const val MAX_FIELD_LENGTH = 2_000
        private val PACKAGE_NAME = Regex("[A-Za-z0-9_]+(?:\\.[A-Za-z0-9_]+)+")
        private val LOCK = Any()
    }
}
