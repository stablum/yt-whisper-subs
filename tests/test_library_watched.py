"""Verify configurable and manual watched status across SQLite, IPC, and Qt.

Example: `python -m unittest tests.test_library_watched` uses isolated catalogs.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import library_db  # noqa: E402
from yt_whisper_subs import library_gui  # noqa: E402
from yt_whisper_subs import library_model  # noqa: E402
from yt_whisper_subs import library_service  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import library_views as views  # noqa: E402
from yt_whisper_subs import library_widgets  # noqa: E402
from yt_whisper_subs import mpv_ipc  # noqa: E402
from yt_whisper_subs import playback_progress as playback  # noqa: E402


class WatchedTests(unittest.TestCase):
    """Exercise watched decisions without downloading videos or running mpv.

    Example: manual completion must preserve a 20-percent observed position.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Reuse one Qt application for native action and dialog tests.

        Example: the shared QAction controls both the menu and toolbar button.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Create known- and unknown-duration remote entries in a fresh library.

        Example: no test can change the user's watch history or login settings.
        """

        self._tmp = tempfile.TemporaryDirectory()
        self._service = library_service.LibraryService(
            Path(self._tmp.name), {"python": Path(sys.executable)},
        )
        self._db = self._service.db
        for video_id, duration in (("aaaaaaaaaaa", 100), ("bbbbbbbbbbb", None)):
            ident = types.VideoIdentity(video_id, f"https://youtu.be/{video_id}", video_id)
            origin = types.VideoOrigin("Example", "UC-example", 100)
            details = types.VideoDetails(duration, None, "", None, "not_live")
            self._db.upsert_video(types.VideoMeta(ident, origin, details))
        self._window = library_gui.LibraryWindow(self._service)
        self._window._timer.stop()
        self._window._metadata_timer.stop()
        self._window.resize(1050, 650)
        self._window.show()
        self._app.processEvents()

    def tearDown(self) -> None:
        """Destroy watchers and widgets before removing the temporary catalog.

        Example: SQLite files cannot outlive a queued window deletion.
        """

        self._window._quitting = True
        self._window._timer.stop()
        self._window._metadata_timer.stop()
        self._window._media_timer.stop()
        self._window._tray.hide()
        self._window.close()
        self._window.deleteLater()
        self._app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self._tmp.cleanup()

    def _select(self, video_id: str) -> None:
        """Select by stable ID through the real sorted proxy.

        Example: the manual action never targets the wrong sorted row.
        """

        ui = self._window._ui.catalog
        row = ui.model.row_for(video_id)
        index = ui.proxy.mapFromSource(ui.model.index(row, 0))
        ui.table.selectRow(index.row())

    def test_custom_threshold_preserves_strict_boundary_in_ipc_db_and_model(self) -> None:
        """Keep a higher setting from being overridden by the old default during decoding.

        Example: at a 98-percent threshold, 96 and 98 remain unfinished.
        """

        self._db.set_watch_policy(playback.WatchPolicy(98))
        updates = []
        observer = playback.Observer("aaaaaaaaaaa", updates.append, self._db.watch_policy)
        for position in (96, 98, 98.01):
            observer.publish(position, 100)
            decoded = playback.parse(playback.encode(updates[-1]))
            expected = position > 98
            self.assertEqual(decoded.completed, expected)
            effective = self._db.record_playback(decoded)
            self.assertEqual(effective.completed, expected)
            self._window.refresh()
            row = self._window._ui.catalog.model.row_for("aaaaaaaaaaa")
            watched = self._window._ui.catalog.model.watched_at(row)
            self.assertEqual(watched.completed, expected)
            self.assertEqual(watched.fraction, position / 100)
        self._db.set_watch_policy(playback.WatchPolicy(100))
        self.assertIsNotNone(self._db.video("aaaaaaaaaaa").playback.completed_at)

    def test_setting_changes_reach_running_tracker_and_saved_progress(self) -> None:
        """Re-read policy during one playback session and retain prior completions.

        Example: lowering to 80 promotes saved 85-percent progress without replaying.
        """

        updates = []
        observer = playback.Observer("aaaaaaaaaaa", updates.append, self._db.watch_policy)
        tracker = mpv_ipc.MpvEventTracker(observer, emit_interval=0)
        tracker.ingest({"event": "property-change", "name": "duration", "data": 100}, now=0)
        tracker.ingest({"event": "property-change", "name": "time-pos", "data": 85}, now=0)
        self.assertFalse(updates[-1].completed)
        self._db.record_playback(updates[-1])
        self._db.set_watch_policy(playback.WatchPolicy(80))
        self.assertIsNotNone(self._db.video("aaaaaaaaaaa").playback.completed_at)
        tracker.ingest({"event": "property-change", "name": "time-pos", "data": 86}, now=1)
        self.assertTrue(updates[-1].completed)
        self._db.set_watch_policy(playback.WatchPolicy(99))
        observer.publish(10, 100)
        effective = self._db.record_playback(updates[-1])
        self.assertTrue(effective.completed)
        self.assertEqual(effective.position_seconds, 85)

    def test_settings_control_saves_threshold_and_survives_restart(self) -> None:
        """Use real edited values and the production settings persistence action.

        Example: reopening the library retains a manually chosen 87.5 percent.
        """

        values = library_widgets.SettingsValues(4, "", True, False, 50, "")
        dialog = library_widgets.SettingsDialog(values)
        self.addCleanup(dialog.close)
        control = dialog.findChild(QtWidgets.QDoubleSpinBox, "watchedPercent")
        self.assertEqual(control.value(), 95)
        self.assertEqual((control.minimum(), control.maximum()), (1, 100))
        control.setValue(87.5)
        values = dialog.values()
        self.assertEqual(values.watch.percent, 87.5)
        with (
            mock.patch("yt_whisper_subs.library_window_actions.windows_startup.is_enabled", return_value=False),
            mock.patch("yt_whisper_subs.library_window_actions.windows_startup.set_enabled"),
            mock.patch("yt_whisper_subs.library_window_actions.library_widgets.SettingsDialog") as dialog_cls,
        ):
            dialog_cls.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
            dialog_cls.return_value.values.return_value = values
            self._window._edit_settings()
        reopened = library_db.LibraryDb(self._db.path)
        reopened.initialize()
        self.assertEqual(reopened.watch_policy().percent, 87.5)
        self.assertEqual(self._window._ui.catalog.model._watch_policy.percent, 87.5)

    def test_invalid_saved_threshold_falls_back_and_eof_still_completes(self) -> None:
        """Handle damaged settings and the valid 100-percent endpoint safely.

        Example: NaN cannot cause unexpected automatic completion.
        """

        for value in ("bad", "nan", "inf", "0", "101", "-10"):
            with self.subTest(value=value):
                self._db.set_setting("watched_percent", value)
                self.assertEqual(self._db.watch_policy().percent, 95)
        policy = playback.WatchPolicy(100)
        self.assertFalse(playback.make("aaaaaaaaaaa", 100, 100, policy=policy).completed)
        self.assertTrue(playback.make("aaaaaaaaaaa", 100, 100, True, policy=policy).completed)
        self.assertFalse(playback.make("bbbbbbbbbbb", 100, None, policy=policy).completed)
        self.assertTrue(playback.make("bbbbbbbbbbb", 0, None, True, policy=policy).completed)

    def test_raising_threshold_keeps_previously_qualifying_saved_progress(self) -> None:
        """Persist an already visible watched classification before changing its boundary.

        Example: imported 96-percent progress stays watched when raising the setting to 99.
        """

        with self._db._connect() as conn:
            conn.execute(
                "INSERT INTO playback(video_id, position_seconds, duration_seconds, updated_at) VALUES (?, 96, 100, 1)",
                ("aaaaaaaaaaa",),
            )
        self._window.refresh()
        row = self._window._ui.catalog.model.row_for("aaaaaaaaaaa")
        self.assertTrue(self._window._ui.catalog.model.watched_at(row).completed)
        self._db.set_watch_policy(playback.WatchPolicy(99))
        self._window.refresh()
        self.assertIsNotNone(self._db.video("aaaaaaaaaaa").playback.completed_at)
        self.assertTrue(self._window._ui.catalog.model.watched_at(row).completed)

    def test_toolbar_marks_watched_without_fabricating_progress(self) -> None:
        """Mark an entry during other work and immediately update counts and actions.

        Example: a remote video observed at 20 percent becomes checked while staying at 20.
        """

        self._db.record_playback(playback.Update("aaaaaaaaaaa", 20, 100, False))
        self._window.refresh()
        self._select("aaaaaaaaaaa")
        self._window._busy = True
        self._window._update_actions()
        self.assertTrue(self._window._ui.header.watched.isEnabled())
        self._window._ui.header.watched.click()
        stored = self._db.video("aaaaaaaaaaa").playback
        self.assertEqual(stored.position_seconds, 20)
        self.assertIsNotNone(stored.completed_at)
        ui = self._window._ui.catalog
        self.assertEqual(ui.model.watched_at(ui.model.row_for("aaaaaaaaaaa")).label, "✓ 20%")
        self.assertEqual(ui.proxy.facet_counts()[views.VideoView.WATCHED], 1)
        self.assertEqual(ui.proxy.facet_counts()[views.VideoView.CONTINUE], 0)
        self.assertFalse(self._window._ui.header.watched.isEnabled())
        self.assertFalse(self._service.video_dir.exists())
        self._window._busy = False
        effective = self._db.record_playback(playback.Update("aaaaaaaaaaa", 5, 100, False))
        self.assertTrue(effective.completed)
        self.assertEqual(effective.position_seconds, 20)
        self.assertEqual(self._db.video("aaaaaaaaaaa").playback.completed_at, stored.completed_at)

    def test_context_menu_targets_clicked_row_and_manual_status_survives_pruning(self) -> None:
        """Keep manual completion for remote and removed entries without touching files.

        Example: right-click the unknown-duration row while another entry is selected.
        """

        self._select("aaaaaaaaaaa")
        ui = self._window._ui.catalog
        index = ui.proxy.mapFromSource(ui.model.index(ui.model.row_for("bbbbbbbbbbb"), 0))
        pos = ui.table.visualRect(index).center()
        with mock.patch("yt_whisper_subs.library_window_actions.QtWidgets.QMenu") as menu_cls:
            menu_cls.return_value.exec.side_effect = lambda _pos: self._window._watched_action.trigger()
            self._window._show_video_context_menu(pos)
            menu_cls.return_value.addAction.assert_called_once_with(self._window._watched_action)
        self.assertIsNone(self._db.video("aaaaaaaaaaa").playback)
        marked = self._db.video("bbbbbbbbbbb")
        self.assertEqual(marked.playback.position_seconds, 0)
        self.assertIsNotNone(marked.playback.completed_at)
        self._db.prune_remote_before(1000)
        self.assertIsNone(self._db.video("aaaaaaaaaaa"))
        self._db.record_yield_removal("bbbbbbbbbbb")
        self._service.apply_retention()
        reopened = library_db.LibraryDb(self._db.path)
        reopened.initialize()
        marked = reopened.video("bbbbbbbbbbb")
        self.assertTrue(marked.removed)
        self.assertEqual(library_model.watched_progress(marked).label, "✓ Watched")
        self.assertEqual(marked.playback.position_seconds, 0)

    def test_toolbar_fits_minimum_window_during_active_work(self) -> None:
        """Check the new common action does not clip neighboring controls.

        Example: at 1050 pixels, pause and cancel remain accessible beside Watched.
        """

        self._window._ui.header.pause.show()
        self._window._ui.header.cancel.show()
        self._app.processEvents()
        header = self._window._ui.header
        buttons = (header.search, header.check, header.download, header.pause, header.cancel,
                   header.play, header.watched, header.remove)
        for left, right in zip(buttons, buttons[1:]):
            self.assertLessEqual(left.geometry().right(), right.geometry().left())
        self.assertLessEqual(header.remove.geometry().right(), header.remove.parentWidget().width())


if __name__ == "__main__":
    unittest.main()
