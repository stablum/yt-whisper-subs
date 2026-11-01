"""Verify discovery boundaries, aggregation, settings, and dismissible tray UI.

Example: `python -m unittest tests.test_library_notifications` needs no network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore  # noqa: E402
from PySide6 import QtGui  # noqa: E402
from PySide6 import QtTest  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import library_gui  # noqa: E402
from yt_whisper_subs import library_notifications as notifications  # noqa: E402
from yt_whisper_subs import library_service  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import library_views as views  # noqa: E402
from yt_whisper_subs import library_widgets  # noqa: E402
from yt_whisper_subs import task_cancel  # noqa: E402


def _meta(video_id: str, channel: str) -> types.VideoMeta:
    """Make remote entries whose title also exercises HTML escaping.

    Example: a channel named `Alpha & Friends` stays literal in the balloon.
    """

    ident = types.VideoIdentity(video_id, f"https://youtu.be/{video_id}", f"News <{video_id}>")
    origin = types.VideoOrigin(channel, f"UC-{channel}", 1000)
    return types.VideoMeta(ident, origin, types.VideoDetails(90, None, "", None, "not_live"))


class TrayNotificationTests(unittest.TestCase):
    """Exercise actual SQLite discovery and Qt widgets with isolated subscriptions.

    Example: a channel baseline never enters the same alert as its next upload.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Keep a single GUI event loop for worker delivery and native controls.

        Example: queued discovery signals must run on this application's thread.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Create two pinned channels and one ordinary channel with empty baselines.

        Example: no test reads the user's catalog or calls YouTube.
        """

        self._tmp = tempfile.TemporaryDirectory()
        self._feed = mock.Mock()
        self._service = library_service.LibraryService(
            Path(self._tmp.name), {"python": Path(sys.executable)}, feed=self._feed,
        )
        self._channels = [self._service.track_channel(f"@{name}", False) for name in ("alpha", "beta", "other")]
        self._snapshots = {}
        for channel, title in zip(self._channels, ("Alpha & Friends", "Beta", "Other")):
            snapshot = types.ChannelSnapshot(channel.url, f"UC-{title}", title, [], True)
            self._service.db.store_snapshot(channel.channel_id, snapshot)
            self._snapshots[channel.url] = snapshot
        for channel in self._channels[:2]:
            self._service.db.set_channel_pinned(channel.channel_id, True)
        self._feed.channel.side_effect = lambda url, _policy, _known: self._snapshots[url]
        self._release = threading.Event()
        self._window = library_gui.LibraryWindow(self._service)
        self._window._timer.stop()
        self._window._media_timer.stop()
        self._window._metadata_timer.stop()
        self._notifier = self._window._notifications
        self._notifier.configure(notifications.NotificationPrefs(duration_seconds=0))

    def tearDown(self) -> None:
        """Release workers before destroying their notification owner and database.

        Example: a failed gated test cannot strand a Qt worker.
        """

        self._release.set()
        if self._window._active_task:
            self._window._active_task.cancel()
        self._window._pool.waitForDone(5000)
        self._app.processEvents()
        self._notifier.stop()
        self._window._quitting = True
        self._window._timer.stop()
        self._window._metadata_timer.stop()
        self._window._media_timer.stop()
        self._window._tray.hide()
        self._window.close()
        self._window.deleteLater()
        self._app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self._tmp.cleanup()

    def _wait_for(self, predicate: Callable[[], bool]) -> None:
        """Allow real queued work to finish within a bounded deadline.

        Example: wait until a worker's batch has reached the visible balloon.
        """

        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self._app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def _discover(self) -> list[types.VideoMeta]:
        """Populate all three feeds after their established baseline.

        Example: return values can also drive controller-only UI checks.
        """

        videos = []
        for channel, video_id in zip(self._channels, ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")):
            snapshot = self._snapshots[channel.url]
            meta = _meta(video_id, snapshot.title)
            self._snapshots[channel.url] = snapshot._replace(videos=[meta])
            videos.append(meta)
        return videos

    def test_worker_groups_all_channels_before_download_and_on_gui_thread(self) -> None:
        """Keep a slow channel in the same balloon as earlier pinned discoveries.

        Example: auto-download starts only after the combined discovery signal.
        """

        self._discover()
        entered = threading.Event()
        first = self._service.db.channels()[0]

        def fetch(url: str, _policy: object, _known: object) -> types.ChannelSnapshot:
            """Gate a later feed after the first pinned channel is committed.

            Example: no balloon may open while the remaining batch is still scanning.
            """

            if url != first.url:
                entered.set()
                self._release.wait(5)
            return self._snapshots[url]

        self._feed.channel.side_effect = fetch
        self._notifier.configure(notifications.NotificationPrefs(delay_seconds=0, duration_seconds=0))
        balloon = self._notifier._balloon
        threads = []
        original = balloon.present

        def present(*args: object) -> None:
            """Observe thread affinity at the actual widget update boundary.

            Example: the worker never directly opens a QWidget.
            """

            threads.append(QtCore.QThread.currentThread())
            original(*args)

        with mock.patch.object(balloon, "present", side_effect=present) as shown:
            self._window.check_now()
            self._wait_for(entered.is_set)
            self.assertFalse(balloon.isVisible())
            self.assertFalse(self._notifier._pending)
            self._release.set()
            self._wait_for(balloon.isVisible)
            self._wait_for(lambda: not self._window._busy)
            self.assertEqual(shown.call_count, 1)
        self.assertEqual(threads, [self._app.thread()])
        text = balloon._videos.toPlainText()
        self.assertIn("Alpha & Friends", text)
        self.assertIn("Beta", text)
        self.assertIn("News <aaaaaaaaaaa>", text)
        self.assertNotIn("Other", text)
        self.assertNotIn("ccccccccccc", text)
        self.assertIn("2 new videos", balloon._heading.text())

    def test_batch_is_emitted_before_auto_download_attempts(self) -> None:
        """Notify about catalog discovery without waiting for expensive pipeline work.

        Example: the signal is already published when the first download begins.
        """

        self._discover()
        channel = self._channels[0]
        self._service.db.set_channel_auto_download(channel.channel_id, True)
        batches = []
        before_download = []

        def download(_video_id: str, _report: object) -> None:
            """Require discovery publication at the first expensive operation.

            Example: a later failed download cannot erase the notification batch.
            """

            before_download.append([len(batch) for batch in batches])

        with mock.patch.object(self._service, "download", side_effect=download) as run:
            self.assertEqual(self._service.check_all(videos_discovered=batches.append), 1)
            run.assert_called_once()
        self.assertEqual(before_download, [[3]])

    def test_baseline_rediscovery_and_archived_metadata_stay_quiet(self) -> None:
        """Exclude subscription imports, repeat scans, and known archived entries.

        Example: pinning a channel does not turn its historical rows into new uploads.
        """

        self._discover()
        fresh = self._service.track_channel("@fresh", False)
        self._service.db.set_channel_pinned(fresh.channel_id, True)
        self._snapshots[fresh.url] = types.ChannelSnapshot(
            fresh.url, "UC-fresh", "Fresh", [_meta("ddddddddddd", "Fresh")], True,
        )
        path = self._service.metadata_dir / "aaaaaaaaaaa.info.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": "aaaaaaaaaaa", "title": "Archived", "channel": "Alpha"}), encoding="utf-8")
        batches = []
        self._service.check_all(videos_discovered=batches.append)
        ids = {meta.identity.video_id for meta in batches[0]}
        self.assertEqual(ids, {"bbbbbbbbbbb", "ccccccccccc"})
        self._service.check_all(videos_discovered=batches.append)
        self.assertEqual(len(batches), 1)

    def test_cancel_after_commit_preserves_discoveries(self) -> None:
        """Publish saved discoveries even at the first cancellation checkpoint.

        Example: cancellation during a saved channel's progress report still notifies.
        """

        self._discover()
        first = self._service.db.channels()[0]
        batches = []

        def report(message: str) -> None:
            """Stop at the first post-commit progress message.

            Example: simulate the Cancel button racing a channel's SQLite commit.
            """

            if "retained" in message:
                raise task_cancel.CancelledError()

        with self.assertRaises(task_cancel.CancelledError):
            self._service.check_all(report, videos_discovered=batches.append)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][0].origin.channel, first.title)
        self.assertIsNotNone(self._service.db.video(batches[0][0].identity.video_id))

    def test_delay_deduplicates_and_updates_one_visible_balloon(self) -> None:
        """Bound grouping time and merge later arrivals without stacking windows.

        Example: duplicate IDs and another check produce one readable list.
        """

        videos = self._discover()
        self._service.check_all()
        balloon = self._notifier._balloon
        self._notifier.add(videos[:1])
        remaining = self._notifier._timer.remainingTime()
        self._notifier.add(videos)
        self.assertLessEqual(self._notifier._timer.remainingTime(), remaining)
        self.assertFalse(balloon.isVisible())
        self.assertEqual(len(self._notifier._pending), 2)
        self._notifier._show_pending()
        self._notifier.add(videos)
        self.assertIs(self._notifier._balloon, balloon)
        self.assertEqual(balloon._videos.toPlainText().count("News <aaaaaaaaaaa>"), 1)
        self.assertEqual(len(self._notifier._pending), 2)

    def test_unpin_or_remove_during_delay_excludes_titles(self) -> None:
        """Use current subscription state when the grouping delay expires.

        Example: stopping tracking before an alert prevents a stale notification.
        """

        videos = self._discover()
        self._service.check_all()
        self._notifier.add(videos)
        self._service.db.set_channel_pinned(self._channels[0].channel_id, False)
        self._service.db.remove_channel(self._channels[1].channel_id)
        self._notifier._show_pending()
        self.assertFalse(self._notifier._balloon.isVisible())
        self.assertFalse(self._notifier._pending)

    def test_disable_discards_visible_and_queued_notifications(self) -> None:
        """Make deactivation immediate and prevent replay when re-enabled.

        Example: Save off closes the balloon and cancels its queued timer.
        """

        videos = self._discover()
        self._service.check_all()
        for visible in (False, True):
            self._notifier.configure(notifications.NotificationPrefs(duration_seconds=0))
            self._notifier.add(videos)
            if visible:
                self._notifier._show_pending()
            self._notifier.configure(notifications.NotificationPrefs(enabled=False))
            self.assertFalse(self._notifier._pending)
            self.assertFalse(self._notifier._timer.isActive())
            self.assertFalse(self._notifier._balloon.isVisible())
        self._notifier.add(videos)
        self.assertFalse(self._notifier._pending)

    def test_foreground_suppression_and_hidden_window_delivery(self) -> None:
        """Respect the optional hidden-only setting when delivery becomes due.

        Example: foreground arrivals are consumed, while tray arrivals show normally.
        """

        videos = self._discover()
        self._service.check_all()
        self._notifier.configure(notifications.NotificationPrefs(duration_seconds=0, only_hidden=True))
        self._window.show()
        self._notifier.add(videos)
        self._notifier._show_pending()
        self.assertFalse(self._notifier._balloon.isVisible())
        self.assertFalse(self._notifier._pending)
        self._window.hide()
        self._notifier.add(videos)
        self._notifier._show_pending()
        self.assertTrue(self._notifier._balloon.isVisible())

    def test_close_escape_timeout_hover_and_shutdown(self) -> None:
        """Consume each balloon on dismissal and keep reading interactions safe.

        Example: Escape, close, and timeout cannot cause a second queued alert.
        """

        videos = self._discover()
        self._service.check_all()
        balloon = self._notifier._balloon
        for dismiss in (
            lambda: QtTest.QTest.keyClick(balloon, QtCore.Qt.Key.Key_Escape),
            lambda: balloon.findChild(QtWidgets.QToolButton, "dismissNotification").click(),
        ):
            self._notifier.add(videos)
            self._notifier._show_pending()
            dismiss()
            self.assertFalse(balloon.isVisible())
            self.assertFalse(self._notifier._pending)
        self._notifier.configure(notifications.NotificationPrefs(duration_seconds=1))
        self._notifier.add(videos)
        self._notifier._show_pending()
        enter = QtGui.QEnterEvent(QtCore.QPointF(), QtCore.QPointF(), QtCore.QPointF())
        self._app.sendEvent(balloon, enter)
        self.assertFalse(balloon._timer.isActive())
        self._app.sendEvent(balloon, QtCore.QEvent(QtCore.QEvent.Type.Leave))
        self.assertTrue(balloon._timer.isActive())
        self._app.processEvents()
        balloon._timer.start(10)
        self._wait_for(lambda: not balloon.isVisible())
        self.assertFalse(balloon.isVisible())
        self.assertFalse(self._notifier._pending)
        self._notifier.stop()
        self._notifier.add(videos)
        self.assertFalse(self._notifier._timer.isActive())

    def test_title_click_opens_library_selects_video_and_clears_filters(self) -> None:
        """Navigate to the exact upload without automatically starting work.

        Example: a previous search cannot hide the title clicked in a balloon.
        """

        videos = self._discover()
        self._service.check_all()
        self._window._ui.header.search.setText("no matches")
        self._window._video_view_changed(int(views.VideoView.WATCHED))
        self._notifier.add(videos)
        self._notifier._show_pending()
        self._notifier._balloon._videos.anchorClicked.emit(QtCore.QUrl("aaaaaaaaaaa"))
        self.assertTrue(self._window.isVisible())
        self.assertEqual(self._window._filter_key, ("pinned", None))
        self.assertEqual(self._window._ui.header.search.text(), "")
        self.assertEqual(self._window._selected_record().meta.identity.video_id, "aaaaaaaaaaa")
        self.assertFalse(self._window._busy)
        self.assertFalse(self._notifier._balloon.isVisible())

    def test_settings_save_reload_and_disabled_controls(self) -> None:
        """Persist every tuning control and apply deactivation immediately.

        Example: reopening Settings retains a two-minute grouping delay.
        """

        prefs = notifications.NotificationPrefs(False, 120, 0, True)
        current = library_widgets.SettingsValues(4, "", True, False, 50, "", notifications=prefs)
        dialog = library_widgets.SettingsDialog(current, self._window)
        self.assertEqual(dialog.values().notifications, prefs)
        enabled = dialog.findChild(QtWidgets.QCheckBox, "notifyEnabled")
        delay = dialog.findChild(QtWidgets.QSpinBox, "notifyDelay")
        self.assertFalse(delay.isEnabled())
        enabled.setChecked(True)
        self.assertTrue(delay.isEnabled())
        delay.setValue(90)
        self.assertEqual(dialog.values().notifications.delay_seconds, 90)
        with (
            mock.patch("yt_whisper_subs.library_window_actions.windows_startup.is_enabled", return_value=False),
            mock.patch("yt_whisper_subs.library_window_actions.windows_startup.set_enabled"),
            mock.patch("yt_whisper_subs.library_window_actions.library_widgets.SettingsDialog") as dialog_cls,
            mock.patch.object(self._service, "reload_clients"),
        ):
            dialog_cls.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
            dialog_cls.return_value.values.return_value = current
            self._window._edit_settings()
        self.assertEqual(notifications.NotificationPrefs.load(self._service.db), prefs)
        self.assertEqual(self._notifier._prefs, prefs)
        self._service.db.set_setting("notify_delay_seconds", "broken")
        self._service.db.set_setting("notify_duration_seconds", "999999999")
        loaded = notifications.NotificationPrefs.load(self._service.db)
        self.assertEqual(loaded.delay_seconds, 30)
        self.assertEqual(loaded.duration_seconds, 600)

    def test_large_batch_is_scrollable_and_keeps_all_titles(self) -> None:
        """Keep the entire list accessible instead of truncating a native toast.

        Example: the last of fifty titles is still present below the scrollbar.
        """

        snapshot = self._snapshots[self._channels[0].url]
        videos = [_meta(f"{idx:011d}", snapshot.title) for idx in range(50)]
        self._snapshots[snapshot.url] = snapshot._replace(videos=videos)
        self._service.check_all(videos_discovered=self._notifier.add)
        self._notifier._show_pending()
        self._app.processEvents()
        balloon = self._notifier._balloon
        self.assertIn("50 new videos", balloon._heading.text())
        self.assertIn("News <00000000049>", balloon._videos.toPlainText())
        self.assertGreater(balloon._videos.verticalScrollBar().maximum(), 0)
        self.assertLessEqual(balloon.height(), 440)
        self.assertLessEqual(balloon.width(), 440)

    def test_popup_stays_on_monitor_for_each_taskbar_edge_and_fallback(self) -> None:
        """Fit tray balloons to usable monitor bounds, including negative coordinates.

        Example: a left-side second monitor never sends the balloon off screen.
        """

        available = QtCore.QRect(-1920, 40, 1920, 1000)
        anchors = (
            QtCore.QRect(-40, 1040, 24, 24), QtCore.QRect(-40, 0, 24, 24),
            QtCore.QRect(-1944, 40, 24, 24), QtCore.QRect(0, 40, 24, 24), QtCore.QRect(),
        )
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                rect = notifications.popup_rect(anchor, available, QtCore.QSize(440, 360))
                self.assertTrue(available.contains(rect))


if __name__ == "__main__":
    unittest.main()
