"""Exercise grouped channel navigation through real Qt widgets and SQLite.

Example: `python -m unittest tests.test_library_channel_scope`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore  # noqa: E402
from PySide6 import QtTest  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import library_gui  # noqa: E402
from yt_whisper_subs import library_service  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import library_views as views  # noqa: E402


class ChannelScopeTests(unittest.TestCase):
    """Verify sidebar clicks, composable filters, and live pin membership.

    Example: clicking Pinned excludes regular and unsubscribed videos.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Keep one application alive for production widget signals.

        Example: all tests send native Qt mouse events without a visible window.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Build a real isolated catalog without launching scheduled work.

        Example: two pinned channels share the combined sidebar scope.
        """

        self._tmp = tempfile.TemporaryDirectory()
        paths = {"python": Path(sys.executable)}
        self._service = library_service.LibraryService(Path(self._tmp.name), paths)
        db = self._service.db
        self._channels = [
            db.add_channel(f"https://youtube.com/@{title}/videos", title, False)
            for title in ("First", "Second", "Regular")
        ]
        for channel in self._channels[:2]:
            db.set_channel_pinned(channel.channel_id, True)
        videos = (
            ("aaaaaaaaaaa", "Pinned news", self._channels[0]),
            ("bbbbbbbbbbb", "Pinned interview", self._channels[1]),
            ("ccccccccccc", "Regular news", self._channels[2]),
            ("ddddddddddd", "Untracked news", None),
        )
        for video_id, title, channel in videos:
            ident = types.VideoIdentity(video_id, f"https://youtube.com/watch?v={video_id}", title)
            origin = types.VideoOrigin(channel.title if channel else "Untracked", None, 100)
            details = types.VideoDetails(90, None, "", None, "not_live")
            meta = types.VideoMeta(ident, origin, details)
            db.upsert_video(meta, channel.channel_id if channel else None)
        db.set_download_error("bbbbbbbbbbb", "Download failed")
        self._window = library_gui.LibraryWindow(self._service)
        self._window._timer.stop()
        self._window._metadata_timer.stop()
        self._window.show()
        self._app.processEvents()

    def tearDown(self) -> None:
        """Dispose widgets before removing the isolated catalog directory.

        Example: filesystem watchers cannot outlive their temporary output root.
        """

        self._window._quitting = True
        self._window._tray.hide()
        self._window._media_timer.stop()
        self._window.close()
        self._window.deleteLater()
        self._app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self._tmp.cleanup()

    def _click_scope(self, key: tuple[str, int | None]) -> None:
        """Use a mouse event to exercise selectable sidebar rows and wiring.

        Example: `_click_scope(("pinned", None))` clicks the Pinned heading.
        """

        widget = self._window._ui.catalog.channels
        item = next(
            widget.item(row) for row in range(widget.count())
            if widget.item(row).data(QtCore.Qt.ItemDataRole.UserRole) == key
        )
        pos = widget.visualItemRect(item).center()
        QtTest.QTest.mouseClick(widget.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=pos)

    def _visible_ids(self) -> set[str]:
        """Read displayed records through the production proxy mapping.

        Example: the combined Pinned table contains IDs from two subscriptions.
        """

        proxy = self._window._ui.catalog.proxy
        return {
            proxy.data(proxy.index(row, 0), QtCore.Qt.ItemDataRole.UserRole).meta.identity.video_id
            for row in range(proxy.rowCount())
        }

    def test_click_pinned_combines_channels_and_composes_filters(self) -> None:
        """Navigate from one channel to Pinned while retaining search and view.

        Example: Issues shows a failed video from the other pinned channel.
        """

        self._click_scope(("channel", self._channels[0].channel_id))
        self.assertEqual(self._visible_ids(), {"aaaaaaaaaaa"})
        self._click_scope(("pinned", None))
        self.assertEqual(self._visible_ids(), {"aaaaaaaaaaa", "bbbbbbbbbbb"})
        proxy = self._window._ui.catalog.proxy
        self._window._ui.header.search.setText("pinned")
        proxy.set_view(views.VideoView.ISSUES)
        self._click_scope(("channel", self._channels[0].channel_id))
        self.assertEqual(self._visible_ids(), set())
        self._click_scope(("pinned", None))
        self.assertEqual(self._visible_ids(), {"bbbbbbbbbbb"})
        counts = proxy.facet_counts()
        self.assertEqual(counts[views.VideoView.ALL], 2)
        self.assertEqual(counts[views.VideoView.ISSUES], 1)
        self._window._clear_filters()
        self.assertEqual(self._window._filter_key, ("pinned", None))
        self.assertEqual(self._visible_ids(), {"aaaaaaaaaaa", "bbbbbbbbbbb"})
        self._click_scope(("all", None))
        self.assertEqual(proxy.rowCount(), 4)

    def test_pin_changes_and_catalog_refresh_keep_combined_scope_current(self) -> None:
        """Recompute membership from subscription data without reselecting Pinned.

        Example: a newly pinned channel joins the displayed list after refresh.
        """

        self._click_scope(("pinned", None))
        db = self._service.db
        db.set_channel_pinned(self._channels[0].channel_id, False)
        db.set_channel_pinned(self._channels[2].channel_id, True)
        self._window.refresh()
        self.assertEqual(self._window._filter_key, ("pinned", None))
        self.assertEqual(self._visible_ids(), {"bbbbbbbbbbb", "ccccccccccc"})
        for channel in self._channels:
            db.set_channel_pinned(channel.channel_id, False)
        self._window.refresh()
        self.assertEqual(self._window._filter_key, ("all", None))
        self.assertEqual(len(self._visible_ids()), 4)

    def test_other_channels_excludes_pinned_and_untracked_videos(self) -> None:
        """Click Other channels while preserving search, view, and scoped counts.

        Example: Issues shows only a failed video from an unpinned subscription.
        """

        self._service.db.set_download_error("ccccccccccc", "Download failed")
        self._window.refresh()
        self._click_scope(("pinned", None))
        self._window._ui.header.search.setText("news")
        proxy = self._window._ui.catalog.proxy
        proxy.set_view(views.VideoView.ISSUES)
        self.assertEqual(self._visible_ids(), set())
        self._click_scope(("unpinned", None))
        self.assertEqual(self._visible_ids(), {"ccccccccccc"})
        self.assertEqual(proxy.view, views.VideoView.ISSUES)
        counts = proxy.facet_counts()
        self.assertEqual(counts[views.VideoView.ALL], 1)
        self.assertEqual(counts[views.VideoView.ISSUES], 1)
        self._window._clear_filters()
        self.assertEqual(self._window._filter_key, ("unpinned", None))
        self.assertEqual(self._visible_ids(), {"ccccccccccc"})
        self.assertIsNone(self._window._selected_channel())

    def test_other_channels_tracks_pin_changes_and_falls_back_when_empty(self) -> None:
        """Keep the unpinned group current after membership changes and refresh.

        Example: unpinning a channel adds its videos without another sidebar click.
        """

        self._click_scope(("unpinned", None))
        db = self._service.db
        db.set_channel_pinned(self._channels[0].channel_id, False)
        self._window.refresh()
        self.assertEqual(self._window._filter_key, ("unpinned", None))
        self.assertEqual(self._visible_ids(), {"aaaaaaaaaaa", "ccccccccccc"})
        for channel in self._channels:
            db.set_channel_pinned(channel.channel_id, True)
        self._window._refresh_channels()
        self.assertEqual(self._window._filter_key, ("all", None))
        self.assertEqual(len(self._visible_ids()), 4)

    def test_empty_subscription_scope_never_shows_unrelated_videos(self) -> None:
        """Distinguish no matching subscriptions from an unrestricted catalog.

        Example: an empty set shows zero rows; All videos uses None.
        """

        proxy = self._window._ui.catalog.proxy
        proxy.set_channels(frozenset())
        self.assertEqual(proxy.rowCount(), 0)
        self.assertTrue(all(count == 0 for count in proxy.facet_counts().values()))
        proxy.set_channels(None)
        self.assertEqual(proxy.rowCount(), 4)
