package dev.qajua.quietcalendar

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Intent
import android.database.Cursor
import android.net.Uri
import android.os.Bundle
import org.json.JSONObject

/** An ADB-callable debug bridge that does not pass through Huawei's broadcast queue. */
class DebugTaskProvider : ContentProvider() {
    override fun onCreate(): Boolean = true

    override fun call(method: String, arg: String?, extras: Bundle?): Bundle {
        val store = DebugTaskStore(checkNotNull(context))
        if (method == "bootstrap") {
            store.bootstrap()
            return Bundle().apply { putString("status", "ready") }
        }
        require(method == "create" || method == "cleanup") { "Unknown command" }
        val request = JSONObject(checkNotNull(arg) { "Missing JSON request" })
        val token = request.optString("bridge_token")
        check(store.authorize(token)) { "Unauthorized" }
        val taskId = request.getString("task_id")
        val commandId = request.getString("command_id")
        val intent = Intent(
            if (method == "create") "dev.qajua.quietcalendar.CREATE_TASK"
            else "dev.qajua.quietcalendar.CLEANUP_TASK",
        ).apply {
            putExtra("bridge_token", token)
            putExtra("task_id", taskId)
            putExtra("command_id", commandId)
            if (method == "create") {
                putExtra("calendar_id", request.getLong("calendar_id"))
                putExtra("title", request.getString("title"))
                putExtra("start_ms", request.getLong("start_ms"))
                putExtra("minutes", request.getInt("minutes"))
            }
        }
        store.handle(intent)
        return Bundle().apply { putString("report", store.report(taskId)?.toString() ?: "") }
    }

    override fun query(
        uri: Uri, projection: Array<out String>?, selection: String?,
        selectionArgs: Array<out String>?, sortOrder: String?,
    ): Cursor? = throw UnsupportedOperationException("Use call()")

    override fun insert(uri: Uri, values: ContentValues?): Uri? =
        throw UnsupportedOperationException("Use call()")

    override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int =
        throw UnsupportedOperationException("Use call()")

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int =
        throw UnsupportedOperationException("Use call()")

    override fun getType(uri: Uri): String? = null
}
