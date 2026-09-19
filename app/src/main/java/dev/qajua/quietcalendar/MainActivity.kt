package dev.qajua.quietcalendar

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var probe: CalendarProbe
    private lateinit var status: TextView
    private lateinit var calendarPicker: Spinner
    private var calendars = emptyList<CalendarChoice>()
    private var pendingAction: (() -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        probe = CalendarProbe(this)

        val page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(24, 24, 24, 24)
        }
        page.addView(TextView(this).apply {
            text = "Quiet Calendar Agent · 设备兼容性探针"
            textSize = 21f
        })
        page.addView(TextView(this).apply {
            text = "先选择测试日历。本阶段只在你点击按钮后创建一条事件，并能回读和删除。"
            textSize = 15f
        })
        calendarPicker = Spinner(this)
        page.addView(calendarPicker)
        addButton(page, "1. 检查可写日历") { runWithPermissions { loadCalendars() } }
        addButton(page, "2. 创建测试事件") {
            runWithPermissions {
                val selected = calendarPicker.selectedItem as? CalendarChoice
                if (selected == null) showStatus("请先检查并选择可写日历。")
                else runProbe { probe.createTestEvent(selected.id) }
            }
        }
        addButton(page, "3. 回读验证") { runWithPermissions { runProbe { probe.verifyTestEvent() } } }
        addButton(page, "4. 删除测试事件") { runWithPermissions { runProbe { probe.deleteTestEvent() } } }
        status = TextView(this).apply { text = "尚未检查设备。"; textSize = 16f }
        page.addView(status)
        setContentView(ScrollView(this).apply { addView(page) })
    }

    private fun addButton(parent: LinearLayout, label: String, action: () -> Unit) {
        parent.addView(Button(this).apply {
            text = label
            setOnClickListener { action() }
        }, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
    }

    private fun runWithPermissions(action: () -> Unit) {
        val permissions = arrayOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR)
        if (permissions.all { checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }) action()
        else {
            pendingAction = action
            requestPermissions(permissions, REQUEST_CALENDAR)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_CALENDAR) return
        val action = pendingAction
        pendingAction = null
        if (grantResults.isNotEmpty() && grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
            action?.invoke()
        } else showStatus("日历权限未授予；无法执行测试。")
    }

    private fun loadCalendars() {
        runProbe {
            val found = probe.writableCalendars()
            runOnUiThread {
                calendars = found
                calendarPicker.adapter = ArrayAdapter(
                    this,
                    android.R.layout.simple_spinner_dropdown_item,
                    calendars,
                )
            }
            "找到 ${found.size} 个可写日历。请选择测试目标。"
        }
    }

    private fun runProbe(action: () -> String) {
        showStatus("执行中…")
        worker.execute {
            val result = runCatching(action).fold(
                onSuccess = { it },
                onFailure = { "失败：${it.message ?: it.javaClass.simpleName}" },
            )
            runOnUiThread { showStatus(result) }
        }
    }

    private fun showStatus(message: String) { status.text = message }

    override fun onDestroy() {
        worker.shutdown()
        super.onDestroy()
    }

    private companion object { const val REQUEST_CALENDAR = 1001 }
}
