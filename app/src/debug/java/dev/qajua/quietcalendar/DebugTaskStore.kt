package dev.qajua.quietcalendar

import android.Manifest
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.provider.CalendarContract
import android.util.AtomicFile
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.TimeZone

/** Debug-only task logic. No Activity is launched and only tagged events can be cleaned up. */
internal class DebugTaskStore(private val context: Context) {
    private val resolver = context.contentResolver

    fun bootstrap() { synchronized(LOCK) { ensureToken() } }

    fun authorize(presented: String?): Boolean {
        if (presented == null) return false
        return synchronized(LOCK) {
            MessageDigest.isEqual(ensureToken().toByteArray(), presented.toByteArray())
        }
    }

    fun report(id: String): JSONObject? = synchronized(LOCK) {
        if (TASK_ID.matches(id)) read(id) else null
    }

    fun handle(intent: Intent) {
        synchronized(LOCK) { handleLocked(intent) }
    }

    private fun handleLocked(intent: Intent) {
        if (!authorize(intent.getStringExtra("bridge_token"))) return
        val id = intent.getStringExtra("task_id") ?: return
        if (!TASK_ID.matches(id)) return
        val commandId = intent.getStringExtra("command_id") ?: return
        if (!TASK_ID.matches(commandId)) return
        try {
            requirePermissions()
            when (intent.action) {
                ACTION_CREATE -> create(id, commandId, intent)
                ACTION_CLEANUP -> cleanup(id, commandId)
                ACTION_RESCHEDULE -> reschedule(id, commandId, intent)
                ACTION_UNDO -> undo(id, commandId)
                else -> error("Unknown command")
            }
        } catch (error: Exception) {
            val old = read(id)
            val report = old ?: JSONObject().put("task_id", id)
            report.put("state", "failed")
            report.put("command_id", commandId)
            report.put("error", error.message ?: error.javaClass.simpleName)
            write(id, report)
        }
    }

    private fun create(id: String, commandId: String, intent: Intent) {
        val calendarId = intent.getLongExtra("calendar_id", -1L)
        val start = intent.getLongExtra("start_ms", -1L)
        val minutes = intent.getIntExtra("minutes", -1)
        val title = intent.getStringExtra("title")?.trim().orEmpty()
        require(calendarId > 0) { "Invalid calendar ID" }
        require(start > 0) { "Invalid start time" }
        require(minutes in 1..1440) { "Duration must be 1–1440 minutes" }
        require(title.length in 1..200) { "Title must be 1–200 characters" }
        val end = start + minutes * 60_000L
        require(end > start) { "Invalid end time" }

        val previous = read(id)
        check(previous?.optString("state") != "deleted") { "Task was already cleaned up; use a new ID" }
        if (previous != null && previous.has("calendar_id")) {
            check(previous.optLong("calendar_id") == calendarId &&
                previous.optLong("start_ms") == start &&
                previous.optLong("end_ms") == end &&
                previous.optString("title") == title) { "Task ID was used with different fields" }
        }

        val calendar = resolver.query(
            CalendarContract.Calendars.CONTENT_URI,
            arrayOf(CalendarContract.Calendars._ID),
            "${CalendarContract.Calendars._ID}=? AND ${CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL}>=?",
            arrayOf(calendarId.toString(), CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR.toString()),
            null,
        )?.use { it.moveToFirst() } ?: false
        check(calendar) { "Calendar is not writable" }

        val marker = marker(id)
        var eventId = findByMarker(calendarId, marker)
        val replayed = eventId != null
        if (eventId == null) {
            check(previous?.has("event_id") != true) { "Previously recorded event is missing; use a new ID" }
            val values = ContentValues().apply {
                put(CalendarContract.Events.CALENDAR_ID, calendarId)
                put(CalendarContract.Events.TITLE, title)
                put(CalendarContract.Events.DESCRIPTION, marker)
                put(CalendarContract.Events.DTSTART, start)
                put(CalendarContract.Events.DTEND, end)
                put(CalendarContract.Events.EVENT_TIMEZONE, TimeZone.getDefault().id)
            }
            val uri = checkNotNull(resolver.insert(CalendarContract.Events.CONTENT_URI, values)) {
                "Calendar refused event insertion"
            }
            eventId = ContentUris.parseId(uri)
        }
        verify(eventId, calendarId, title, start, end, marker)
        write(id, JSONObject().apply {
            put("task_id", id)
            put("command_id", commandId)
            put("state", "complete")
            put("event_id", eventId)
            put("calendar_id", calendarId)
            put("title", title)
            put("start_ms", start)
            put("end_ms", end)
            put("replayed", replayed)
        })
    }

    private fun cleanup(id: String, commandId: String) {
        val previous = checkNotNull(read(id)) { "Unknown task ID" }
        if (previous.optString("state") == "deleted") {
            previous.put("command_id", commandId)
            previous.put("replayed", true)
            write(id, previous)
            return
        }
        val eventId = previous.optLong("event_id", -1L)
        check(eventId > 0) { "No recorded event to clean up" }
        val calendarId = previous.getLong("calendar_id")
        verify(
            eventId, calendarId, previous.getString("title"),
            previous.getLong("start_ms"), previous.getLong("end_ms"), marker(id),
        )
        val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, eventId)
        check(resolver.delete(uri, null, null) == 1) { "Calendar did not delete the event" }
        check(findByMarker(calendarId, marker(id)) == null) { "Event still exists after deletion" }
        previous.put("state", "deleted")
        previous.put("command_id", commandId)
        previous.put("replayed", false)
        previous.remove("error")
        write(id, previous)
    }

    private fun reschedule(id: String, commandId: String, intent: Intent) {
        val eventId = intent.getLongExtra("event_id", -1L)
        val calendarId = intent.getLongExtra("calendar_id", -1L)
        val ownerTaskId = intent.getStringExtra("owner_task_id").orEmpty()
        val title = intent.getStringExtra("title")?.trim().orEmpty()
        val oldStart = intent.getLongExtra("expected_start_ms", -1L)
        val oldEnd = intent.getLongExtra("expected_end_ms", -1L)
        val newStart = intent.getLongExtra("new_start_ms", -1L)
        val newEnd = intent.getLongExtra("new_end_ms", -1L)
        val reminderMinutes = intent.getIntExtra("reminder_minutes", -1)
        require(eventId > 0 && calendarId > 0) { "Invalid event or calendar ID" }
        require(TASK_ID.matches(ownerTaskId)) { "Invalid owner task ID" }
        require(title.length in 1..200) { "Title must be 1–200 characters" }
        require(oldStart > 0 && oldEnd > oldStart && newStart > 0 && newEnd > newStart) {
            "Invalid original or new time range"
        }
        require(reminderMinutes == -1 || reminderMinutes in 0..10_080) {
            "Reminder must be -1 or 0–10080 minutes"
        }

        val previous = read(id)
        check(previous?.optString("state") != "undone") { "Task was already undone; use a new ID" }
        if (previous != null && previous.has("event_id")) {
            check(previous.optLong("event_id") == eventId &&
                previous.optLong("calendar_id") == calendarId &&
                previous.optString("owner_task_id") == ownerTaskId &&
                previous.optString("title") == title &&
                previous.optLong("original_start_ms") == oldStart &&
                previous.optLong("original_end_ms") == oldEnd &&
                previous.optLong("start_ms") == newStart &&
                previous.optLong("end_ms") == newEnd &&
                previous.optInt("reminder_minutes", -1) == reminderMinutes) {
                "Task ID was used with different fields"
            }
        }

        val eventMarker = marker(ownerTaskId)
        val alreadyUpdated = matches(eventId, calendarId, title, newStart, newEnd, eventMarker)
        var updatedNow = false
        if (!alreadyUpdated) {
            check(matches(eventId, calendarId, title, oldStart, oldEnd, eventMarker)) {
                "Event changed since planning; refusing to update"
            }
            val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, eventId)
            val values = ContentValues().apply {
                put(CalendarContract.Events.DTSTART, newStart)
                put(CalendarContract.Events.DTEND, newEnd)
            }
            check(resolver.update(uri, values, null, null) == 1) { "Calendar did not update the event" }
            updatedNow = true
        }
        var reminderId: Long? = null
        var reminderCreated = false
        if (reminderMinutes >= 0) {
            if (previous != null && previous.has("event_id")) {
                reminderCreated = previous.optBoolean("reminder_created", false)
                reminderId = if (reminderCreated) previous.optLong("reminder_id", -1L) else
                    findReminder(eventId, reminderMinutes)
                check(reminderId != null && reminderId > 0 &&
                    reminderMatches(reminderId, eventId, reminderMinutes)) {
                    "Reminder changed after task completion; refusing to replay"
                }
            } else {
                reminderId = findReminder(eventId, reminderMinutes)
                if (reminderId == null) {
                    try {
                        val values = ContentValues().apply {
                            put(CalendarContract.Reminders.EVENT_ID, eventId)
                            put(CalendarContract.Reminders.MINUTES, reminderMinutes)
                            put(CalendarContract.Reminders.METHOD, CalendarContract.Reminders.METHOD_ALERT)
                        }
                        val uri = checkNotNull(resolver.insert(CalendarContract.Reminders.CONTENT_URI, values)) {
                            "Calendar refused reminder insertion"
                        }
                        reminderId = ContentUris.parseId(uri)
                        reminderCreated = true
                    } catch (error: Exception) {
                        if (updatedNow) {
                            val eventUri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, eventId)
                            val rollback = ContentValues().apply {
                                put(CalendarContract.Events.DTSTART, oldStart)
                                put(CalendarContract.Events.DTEND, oldEnd)
                            }
                            check(resolver.update(eventUri, rollback, null, null) == 1) {
                                "Reminder failed and event rollback also failed"
                            }
                        }
                        throw error
                    }
                }
            }
        }
        verify(eventId, calendarId, title, newStart, newEnd, eventMarker)
        if (reminderMinutes >= 0) {
            check(reminderId != null && reminderMatches(reminderId, eventId, reminderMinutes)) {
                "Reminder read-back failed"
            }
        }
        write(id, JSONObject().apply {
            put("task_id", id)
            put("command_id", commandId)
            put("operation", "reschedule")
            put("state", "complete")
            put("event_id", eventId)
            put("calendar_id", calendarId)
            put("owner_task_id", ownerTaskId)
            put("title", title)
            put("original_start_ms", oldStart)
            put("original_end_ms", oldEnd)
            put("start_ms", newStart)
            put("end_ms", newEnd)
            put("reminder_minutes", reminderMinutes)
            put("reminder_created", reminderCreated)
            if (reminderId != null) put("reminder_id", reminderId)
            put("replayed", alreadyUpdated)
        })
    }

    private fun undo(id: String, commandId: String) {
        val previous = checkNotNull(read(id)) { "Unknown task ID" }
        check(previous.optString("operation") == "reschedule") { "Task is not a reschedule operation" }
        if (previous.optString("state") == "undone") {
            previous.put("command_id", commandId)
            previous.put("replayed", true)
            write(id, previous)
            return
        }
        val eventId = previous.getLong("event_id")
        val calendarId = previous.getLong("calendar_id")
        val title = previous.getString("title")
        val eventMarker = marker(previous.getString("owner_task_id"))
        val oldStart = previous.getLong("original_start_ms")
        val oldEnd = previous.getLong("original_end_ms")
        val newStart = previous.getLong("start_ms")
        val newEnd = previous.getLong("end_ms")
        val reminderMinutes = previous.optInt("reminder_minutes", -1)
        val reminderCreated = previous.optBoolean("reminder_created", false)
        val reminderId = previous.optLong("reminder_id", -1L)
        if (reminderCreated) {
            check(reminderId > 0 && reminderMinutes >= 0 &&
                reminderMatches(reminderId, eventId, reminderMinutes)) {
                "Agent-created reminder changed; refusing to undo"
            }
        }
        val alreadyUndone = matches(eventId, calendarId, title, oldStart, oldEnd, eventMarker)
        var restoredNow = false
        if (!alreadyUndone) {
            check(matches(eventId, calendarId, title, newStart, newEnd, eventMarker)) {
                "Event changed after rescheduling; refusing to undo"
            }
            val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, eventId)
            val values = ContentValues().apply {
                put(CalendarContract.Events.DTSTART, oldStart)
                put(CalendarContract.Events.DTEND, oldEnd)
            }
            check(resolver.update(uri, values, null, null) == 1) { "Calendar did not undo the update" }
            restoredNow = true
        }
        if (reminderCreated) {
            val reminderUri = ContentUris.withAppendedId(CalendarContract.Reminders.CONTENT_URI, reminderId)
            try {
                check(resolver.delete(reminderUri, null, null) == 1) { "Calendar did not delete the reminder" }
            } catch (error: Exception) {
                if (restoredNow) {
                    val eventUri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, eventId)
                    val rollback = ContentValues().apply {
                        put(CalendarContract.Events.DTSTART, newStart)
                        put(CalendarContract.Events.DTEND, newEnd)
                    }
                    check(resolver.update(eventUri, rollback, null, null) == 1) {
                        "Reminder undo failed and event rollback also failed"
                    }
                }
                throw error
            }
        }
        verify(eventId, calendarId, title, oldStart, oldEnd, eventMarker)
        if (reminderCreated) check(!reminderMatches(reminderId, eventId, reminderMinutes)) {
            "Reminder still exists after undo"
        }
        previous.put("state", "undone")
        previous.put("command_id", commandId)
        previous.put("replayed", alreadyUndone)
        previous.remove("error")
        write(id, previous)
    }

    private fun findByMarker(calendarId: Long, marker: String): Long? {
        resolver.query(
            CalendarContract.Events.CONTENT_URI,
            arrayOf(CalendarContract.Events._ID),
            "${CalendarContract.Events.CALENDAR_ID}=? AND ${CalendarContract.Events.DESCRIPTION}=?",
            arrayOf(calendarId.toString(), marker),
            null,
        )?.use { cursor ->
            if (!cursor.moveToFirst()) return null
            val id = cursor.getLong(0)
            check(!cursor.moveToNext()) { "Multiple events have the same task marker" }
            return id
        }
        return null
    }

    private fun findReminder(eventId: Long, minutes: Int): Long? {
        resolver.query(
            CalendarContract.Reminders.CONTENT_URI,
            arrayOf(CalendarContract.Reminders._ID),
            "${CalendarContract.Reminders.EVENT_ID}=? AND ${CalendarContract.Reminders.MINUTES}=? AND " +
                "${CalendarContract.Reminders.METHOD}=?",
            arrayOf(
                eventId.toString(), minutes.toString(),
                CalendarContract.Reminders.METHOD_ALERT.toString(),
            ),
            null,
        )?.use { cursor ->
            if (cursor.moveToFirst()) return cursor.getLong(0)
        }
        return null
    }

    private fun reminderMatches(reminderId: Long, eventId: Long, minutes: Int): Boolean {
        val uri = ContentUris.withAppendedId(CalendarContract.Reminders.CONTENT_URI, reminderId)
        return resolver.query(
            uri,
            arrayOf(
                CalendarContract.Reminders.EVENT_ID, CalendarContract.Reminders.MINUTES,
                CalendarContract.Reminders.METHOD,
            ),
            null, null, null,
        )?.use { cursor ->
            cursor.moveToFirst() && cursor.getLong(0) == eventId && cursor.getInt(1) == minutes &&
                cursor.getInt(2) == CalendarContract.Reminders.METHOD_ALERT
        } ?: false
    }

    private fun verify(
        eventId: Long, calendarId: Long, title: String, start: Long, end: Long, marker: String,
    ) {
        val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, eventId)
        resolver.query(
            uri,
            arrayOf(
                CalendarContract.Events.CALENDAR_ID, CalendarContract.Events.TITLE,
                CalendarContract.Events.DTSTART, CalendarContract.Events.DTEND,
                CalendarContract.Events.DESCRIPTION,
            ),
            null, null, null,
        )?.use { cursor ->
            check(cursor.moveToFirst()) { "Event not found on read-back" }
            check(cursor.getLong(0) == calendarId && cursor.getString(1) == title &&
                cursor.getLong(2) == start && cursor.getLong(3) == end &&
                cursor.getString(4) == marker) { "Event changed; refusing to claim success or delete" }
            return
        }
        error("Calendar read-back failed")
    }

    private fun matches(
        eventId: Long, calendarId: Long, title: String, start: Long, end: Long, marker: String,
    ): Boolean {
        val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, eventId)
        return resolver.query(
            uri,
            arrayOf(
                CalendarContract.Events.CALENDAR_ID, CalendarContract.Events.TITLE,
                CalendarContract.Events.DTSTART, CalendarContract.Events.DTEND,
                CalendarContract.Events.DESCRIPTION,
            ),
            null, null, null,
        )?.use { cursor ->
            cursor.moveToFirst() && cursor.getLong(0) == calendarId && cursor.getString(1) == title &&
                cursor.getLong(2) == start && cursor.getLong(3) == end && cursor.getString(4) == marker
        } ?: false
    }

    private fun requirePermissions() {
        check(context.checkSelfPermission(Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED &&
            context.checkSelfPermission(Manifest.permission.WRITE_CALENDAR) == PackageManager.PERMISSION_GRANTED) {
            "Open the app once and grant calendar permissions"
        }
    }

    private fun marker(id: String): String = "[Quiet Calendar Agent task:$id]"

    private fun ensureToken(): String {
        val file = File(context.filesDir, "bridge-token.txt")
        if (!file.exists()) {
            val bytes = ByteArray(32)
            SecureRandom().nextBytes(bytes)
            file.writeText(bytes.joinToString("") { "%02x".format(it.toInt() and 0xff) })
        }
        return file.readText().trim()
    }

    private fun file(id: String): File = File(context.filesDir, "task-$id.json")

    private fun read(id: String): JSONObject? = file(id).takeIf { it.exists() }?.readText()?.let(::JSONObject)

    private fun write(id: String, report: JSONObject) {
        val atomic = AtomicFile(file(id))
        val stream = atomic.startWrite()
        try {
            stream.write(report.toString().toByteArray(Charsets.UTF_8))
            atomic.finishWrite(stream)
        } catch (error: Exception) {
            atomic.failWrite(stream)
            throw error
        }
    }

    private companion object {
        val LOCK = Any()
        val TASK_ID = Regex("[A-Za-z0-9_-]{1,64}")
        const val ACTION_CREATE = "dev.qajua.quietcalendar.CREATE_TASK"
        const val ACTION_CLEANUP = "dev.qajua.quietcalendar.CLEANUP_TASK"
        const val ACTION_RESCHEDULE = "dev.qajua.quietcalendar.RESCHEDULE_TASK"
        const val ACTION_UNDO = "dev.qajua.quietcalendar.UNDO_TASK"
    }
}
