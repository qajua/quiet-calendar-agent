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
        require(method in setOf("create", "cleanup", "reschedule", "undo")) { "Unknown command" }
        val request = JSONObject(checkNotNull(arg) { "Missing JSON request" })
        val token = request.optString("bridge_token")
        check(store.authorize(token)) { "Unauthorized" }
        val taskId = request.getString("task_id")
        val commandId = request.getString("command_id")
        val action = when (method) {
            "create" -> "dev.qajua.quietcalendar.CREATE_TASK"
            "cleanup" -> "dev.qajua.quietcalendar.CLEANUP_TASK"
            "reschedule" -> "dev.qajua.quietcalendar.RESCHEDULE_TASK"
            else -> "dev.qajua.quietcalendar.UNDO_TASK"
        }
        val intent = Intent(action).apply {
            putExtra("bridge_token", token)
            putExtra("task_id", taskId)
            putExtra("command_id", commandId)
            if (method == "create") {
                putExtra("calendar_id", request.getLong("calendar_id"))
                putExtra("title", request.getString("title"))
                putExtra("start_ms", request.getLong("start_ms"))
                putExtra("minutes", request.getInt("minutes"))
            } else if (method == "reschedule") {
                putExtra("event_id", request.getLong("event_id"))
                putExtra("calendar_id", request.getLong("calendar_id"))
                putExtra("owner_task_id", request.getString("owner_task_id"))
                putExtra("title", request.getString("title"))
                putExtra("expected_start_ms", request.getLong("expected_start_ms"))
                putExtra("expected_end_ms", request.getLong("expected_end_ms"))
                putExtra("new_start_ms", request.getLong("new_start_ms"))
                putExtra("new_end_ms", request.getLong("new_end_ms"))
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
