package dev.qajua.quietcalendar

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/** Captures only explicitly allowlisted sources into app-private storage; never opens UI. */
class QuietNotificationListener : NotificationListenerService() {
    override fun onNotificationPosted(notification: StatusBarNotification) {
        val store = NotificationInboxStore(this)
        if (!store.allowed(notification.packageName)) return
        val extras = notification.notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
            ?: extras.getCharSequence(Notification.EXTRA_TEXT)?.toString().orEmpty()
        store.append(notification.key, notification.packageName, notification.postTime, title, text)
    }
}
