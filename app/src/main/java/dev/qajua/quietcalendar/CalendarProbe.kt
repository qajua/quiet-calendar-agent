package dev.qajua.quietcalendar

import android.content.ContentUris
import android.content.Context
import android.provider.CalendarContract
import java.util.TimeZone

data class CalendarChoice(val id: Long, val name: String, val account: String) {
    override fun toString(): String = "$name ($account)"
}

/** Explicit, reversible compatibility probe; it never touches an event it did not create. */
class CalendarProbe(private val context: Context) {
    private val resolver = context.contentResolver
    private val prefs = context.getSharedPreferences("probe", Context.MODE_PRIVATE)

    fun writableCalendars(): List<CalendarChoice> {
        val result = mutableListOf<CalendarChoice>()
        val columns = arrayOf(
            CalendarContract.Calendars._ID,
            CalendarContract.Calendars.CALENDAR_DISPLAY_NAME,
            CalendarContract.Calendars.ACCOUNT_NAME,
        )
        val selection = "${CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL} >= ?"
        val args = arrayOf(CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR.toString())
        resolver.query(CalendarContract.Calendars.CONTENT_URI, columns, selection, args, null)?.use { cursor ->
            while (cursor.moveToNext()) {
                result += CalendarChoice(
                    cursor.getLong(0),
                    cursor.getString(1) ?: "Unnamed calendar",
                    cursor.getString(2) ?: "Unknown account",
                )
            }
        }
        return result
    }

    fun createTestEvent(calendarId: Long): String {
        check(prefs.getLong(KEY_EVENT_ID, -1L) == -1L) {
            "已有测试事件，请先验证或删除，避免重复创建。"
        }
        val start = System.currentTimeMillis() + 24 * 60 * 60 * 1000L
        val values = android.content.ContentValues().apply {
            put(CalendarContract.Events.CALENDAR_ID, calendarId)
            put(CalendarContract.Events.TITLE, PROBE_TITLE)
            put(CalendarContract.Events.DESCRIPTION, "Created only after tapping the probe button.")
            put(CalendarContract.Events.DTSTART, start)
            put(CalendarContract.Events.DTEND, start + 30 * 60 * 1000L)
            put(CalendarContract.Events.EVENT_TIMEZONE, TimeZone.getDefault().id)
        }
        val uri = checkNotNull(resolver.insert(CalendarContract.Events.CONTENT_URI, values)) {
            "日历拒绝创建测试事件。"
        }
        val id = ContentUris.parseId(uri)
        prefs.edit().putLong(KEY_EVENT_ID, id).putLong(KEY_CALENDAR_ID, calendarId).apply()
        return "已创建测试事件，ID=$id；${verifyTestEvent()}"
    }

    fun verifyTestEvent(): String {
        val id = prefs.getLong(KEY_EVENT_ID, -1L)
        check(id != -1L) { "尚未创建测试事件。" }
        val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, id)
        val columns = arrayOf(
            CalendarContract.Events.TITLE,
            CalendarContract.Events.CALENDAR_ID,
            CalendarContract.Events.DTSTART,
        )
        resolver.query(uri, columns, null, null, null)?.use { cursor ->
            check(cursor.moveToFirst()) { "未回读到测试事件，ID=$id。" }
            check(cursor.getString(0) == PROBE_TITLE) { "事件标题不匹配，停止操作。" }
            check(cursor.getLong(1) == prefs.getLong(KEY_CALENDAR_ID, -1L)) {
                "事件所属日历不匹配，停止操作。"
            }
            return "回读成功：${cursor.getString(0)}，开始时间=${cursor.getLong(2)}"
        }
        error("日历查询返回空结果。")
    }

    fun deleteTestEvent(): String {
        val id = prefs.getLong(KEY_EVENT_ID, -1L)
        check(id != -1L) { "没有本应用记录的测试事件。" }
        verifyTestEvent()
        val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, id)
        check(resolver.delete(uri, null, null) == 1) { "删除未成功，请检查日历。" }
        prefs.edit().remove(KEY_EVENT_ID).remove(KEY_CALENDAR_ID).apply()
        return "已删除测试事件，ID=$id。"
    }

    private companion object {
        const val PROBE_TITLE = "[Quiet Calendar Agent] 兼容性测试"
        const val KEY_EVENT_ID = "event_id"
        const val KEY_CALENDAR_ID = "calendar_id"
    }
}
