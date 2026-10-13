"""Verify committed channel results reach Qt before the whole check finishes.

Example: `python -m unittest tests.test_library_channel_refresh` uses gated feeds.
"""

from __future__ import annotations

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
from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import library_gui  # noqa: E402
from yt_whisper_subs import library_model  # noqa: E402
from yt_whisper_subs import library_service  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import library_views as views  # noqa: E402
from yt_whisper_subs import task_cancel  # noqa: E402


def _meta(video_id: str, published_at: int) -> types.VideoMeta:
    """Build sortable remote news entries without network or file dependencies.

    Example: a newer publication sorts above the existing browsing anchor.
    """

    ident = types.VideoIdentity(video_id, f"https://youtu.be/{video_id}", f"News {video_id}")
    origin = types.VideoOrigin("Alpha", "UC-alpha", published_at)
    details = types.VideoDetails(90, None, "", None, "not_live")
    return types.VideoMeta(ident, origin, details)


class ChannelRefreshTests(unittest.TestCase):
    """Exercise actual SQLite commits, workers, widgets, and cancellation.

    Example: pause Beta's lookup and inspect Alpha's already visible results.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Keep one application for queued cross-thread catalog signals.

        Example: native button clicks dispatch workers into the production pool.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Seed two subscriptions and enough rows to verify stable scrolling.

        Example: Alpha already has history while Beta has not been initialized.
        """

        self._tmp = tempfile.TemporaryDirectory()
        self._feed = mock.Mock()
        self._service = library_service.LibraryService(
            Path(self._tmp.name), {"python": Path(sys.executable)}, feed=self._feed,
        )
        db = self._service.db
        self._alpha = db.add_channel("https://youtube.com/@alpha/videos", "Alpha", False)
        self._beta = db.add_channel("https://youtube.com/@beta/videos", "Beta", False)
        self._old = [_meta(f"{idx:011d}", idx + 100) for idx in range(60)]
        self._snapshots = {
            self._alpha.url: types.ChannelSnapshot(
                self._alpha.url, "UC-alpha", "Alpha", self._old, True,
            ),
            self._beta.url: types.ChannelSnapshot(
                self._beta.url, "UC-beta", "Beta", [_meta("bbbbbbbbbbb", 2000)], True,
            ),
        }
        db.store_snapshot(self._alpha.channel_id, self._snapshots[self._alpha.url])
        self._snapshots[self._alpha.url] = self._snapshots[self._alpha.url]._replace(
            videos=[_meta("aaaaaaaaaaa", 1000), *self._old],
        )
        self._feed.channel.side_effect = lambda url, _policy, _known: self._snapshots[url]
        self._release = threading.Event()
        self._window = library_gui.LibraryWindow(self._service)
        self._window._timer.stop()
        self._window._metadata_timer.stop()
        self._window.resize(1050, 650)
        self._window.show()
        self._app.processEvents()

    def tearDown(self) -> None:
        """Release gated workers before destroying their window and catalog.

        Example: even a failed assertion cannot leave Beta blocking the Qt pool.
        """

        self._release.set()
        if self._window._active_task:
            self._window._active_task.cancel()
        self._window._pool.waitForDone(5000)
        self._app.processEvents()
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
        """Process real GUI events until a bounded worker observation succeeds.

        Example: wait for a visible new row while another lookup is gated.
        """

        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self._app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def _visible_ids(self) -> set[str]:
        """Read displayed records through the production proxy model.

        Example: a scoped refresh must not leak Beta's videos into Alpha.
        """

        proxy = self._window._ui.catalog.proxy
        return {
            proxy.index(row, 0).data(QtCore.Qt.ItemDataRole.UserRole).meta.identity.video_id
            for row in range(proxy.rowCount())
        }

    def test_each_notification_follows_commit_and_precedes_next_lookup(self) -> None:
        """Expose only committed channel results before moving to another feed.

        Example: Alpha's new video exists when Beta has not been requested yet.
        """

        observed = []

        def changed() -> None:
            """Record durable catalog state at each channel completion boundary.

            Example: a reader opens its own SQLite connection after the commit.
            """

            ids = {record.meta.identity.video_id for record in self._service.db.videos()}
            observed.append((self._feed.channel.call_count, ids))

        self.assertEqual(self._service.check_all(channel_checked=changed), 0)
        self.assertEqual([count for count, _ids in observed], [1, 2])
        self.assertIn("aaaaaaaaaaa", observed[0][1])
        self.assertNotIn("bbbbbbbbbbb", observed[0][1])
        self.assertIn("bbbbbbbbbbb", observed[1][1])

    def test_check_now_refreshes_on_gui_thread_while_next_channel_waits(self) -> None:
        """Display new results without finishing the batch or losing browsing state.

        Example: a scrolled Alpha news view stays anchored after a newer upload arrives.
        """

        entered = threading.Event()

        def fetch(url: str, _policy: object, _known: set[str]) -> types.ChannelSnapshot:
            """Pause the second feed to separate channel completion from batch completion.

            Example: Beta remains blocked until the GUI has verified Alpha's refresh.
            """

            if url == self._beta.url:
                entered.set()
                if not self._release.wait(5):
                    raise TimeoutError("Test did not release Beta")
            return self._snapshots[url]

        self._feed.channel.side_effect = fetch
        ui = self._window._ui.catalog
        self._window._filter_key = ("channel", self._alpha.channel_id)
        self._window._refresh_channels()
        self._window._ui.header.search.setText("News")
        self._window._video_view_changed(int(views.VideoView.AVAILABLE))
        ui.table.selectRow(20)
        ui.table.verticalScrollBar().setValue(10)
        ui.table.horizontalScrollBar().setValue(200)
        self._app.processEvents()
        selected_id = self._window._selected_record().meta.identity.video_id
        top = ui.table.indexAt(QtCore.QPoint(0, 0))
        anchor_id = top.data(QtCore.Qt.ItemDataRole.UserRole).meta.identity.video_id
        scroll_x = ui.table.horizontalScrollBar().value()
        threads = []
        ui.model.modelReset.connect(lambda: threads.append(QtCore.QThread.currentThread()))

        self._window._ui.header.check.click()
        self._wait_for(lambda: entered.is_set() and "aaaaaaaaaaa" in self._visible_ids())

        self.assertTrue(self._window._busy)
        self.assertNotIn("bbbbbbbbbbb", self._visible_ids())
        self.assertEqual(self._window._filter_key, ("channel", self._alpha.channel_id))
        self.assertEqual(self._window._ui.header.search.text(), "News")
        self.assertEqual(ui.proxy.view, views.VideoView.AVAILABLE)
        self.assertEqual(ui.proxy.sortColumn(), library_model.PUBLISHED_COLUMN)
        self.assertEqual(ui.proxy.sortOrder(), QtCore.Qt.SortOrder.DescendingOrder)
        self.assertEqual(ui.proxy.facet_counts()[views.VideoView.ALL], 61)
        self.assertEqual(self._window._selected_record().meta.identity.video_id, selected_id)
        top = ui.table.indexAt(QtCore.QPoint(0, 0))
        self.assertEqual(top.data(QtCore.Qt.ItemDataRole.UserRole).meta.identity.video_id, anchor_id)
        self.assertEqual(ui.table.horizontalScrollBar().value(), scroll_x)
        self.assertTrue(threads)
        self.assertTrue(all(thread == self._app.thread() for thread in threads))
        self.assertFalse(self._window._ui.header.check.isEnabled())
        self.assertTrue(self._window._ui.header.cancel.isEnabled())
        self._release.set()
        self._wait_for(lambda: not self._window._busy)
        self.assertIsNotNone(self._service.db.video("bbbbbbbbbbb"))

    def test_failed_channel_publishes_warning_and_preserves_existing_history(self) -> None:
        """Refresh saved errors after a failed lookup while continuing the batch.

        Example: Alpha's feed fails, but its old videos remain and Beta still appears.
        """

        self._feed.channel.side_effect = [RuntimeError("Offline"), self._snapshots[self._beta.url]]
        observed = []

        def changed() -> None:
            """Observe errors through fresh database reads at the completion boundary.

            Example: Alpha's warning is committed before Beta starts.
            """

            observed.append(self._service.db.channel(self._alpha.channel_id).last_error)

        self.assertEqual(self._service.check_all(channel_checked=changed), 0)
        self.assertEqual(observed, ["Offline", "Offline"])
        self.assertIsNotNone(self._service.db.video(self._old[0].identity.video_id))
        self.assertIsNotNone(self._service.db.video("bbbbbbbbbbb"))

    def test_cancelled_check_keeps_visible_results_from_completed_channels(self) -> None:
        """Stop a later lookup without rolling back an earlier channel's visible rows.

        Example: cancelling Beta keeps Alpha's newly discovered upload in the table.
        """

        entered = threading.Event()

        def fetch(url: str, _policy: object, _known: set[str]) -> types.ChannelSnapshot:
            """Model a cooperative cancelled lookup after one successful channel.

            Example: release Beta only after pressing the real Cancel control.
            """

            if url == self._beta.url:
                entered.set()
                self._release.wait(5)
                control = task_cancel.current()
                assert control is not None
                control.checkpoint()
            return self._snapshots[url]

        self._feed.channel.side_effect = fetch
        self._window._ui.header.check.click()
        self._wait_for(lambda: entered.is_set() and "aaaaaaaaaaa" in self._visible_ids())
        self._window._ui.header.cancel.click()
        self._release.set()
        self._wait_for(lambda: not self._window._busy)
        self.assertIn("aaaaaaaaaaa", self._visible_ids())
        self.assertNotIn("bbbbbbbbbbb", self._visible_ids())
        self.assertIsNone(self._service.db.channel(self._beta.channel_id).last_error)
        self.assertEqual(self._service.db.setting("last_check_at", "0"), "0")

    def test_refresh_at_top_exposes_new_uploads_without_scrolling_to_selection(self) -> None:
        """Keep the newest uploads visible when the user is browsing the top.

        Example: an older selected row cannot pull the viewport away from a new upload.
        """

        ui = self._window._ui.catalog
        ui.table.selectRow(20)
        ui.table.verticalScrollBar().setValue(0)
        self._service.check_all()
        self._window.refresh()
        self._app.processEvents()
        self.assertEqual(ui.table.verticalScrollBar().value(), 0)
        top = ui.table.indexAt(QtCore.QPoint(0, 0))
        self.assertEqual(top.data(QtCore.Qt.ItemDataRole.UserRole).meta.identity.video_id, "bbbbbbbbbbb")


if __name__ == "__main__":
    unittest.main()
