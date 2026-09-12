"""Tests for the native chapter inspector's state and jump signal.

Example: `python -m unittest tests.test_library_widgets`.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import chapters  # noqa: E402
from yt_whisper_subs import library_chapter_actions  # noqa: E402
from yt_whisper_subs import library_gui  # noqa: E402
from yt_whisper_subs import library_model  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import library_window_support  # noqa: E402
from yt_whisper_subs import library_widgets  # noqa: E402
from yt_whisper_subs import library_workers  # noqa: E402
from yt_whisper_subs import playback_progress  # noqa: E402


class DetailPanelTests(unittest.TestCase):
    """Verify chapter presentation without showing a native window.

    Example: `DetailPanelTests("test_bilingual_chapters_emit_seek_time")`.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Create the single QApplication required by widget construction.

        Example: handled once by `unittest`.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_bilingual_chapters_emit_seek_time(self) -> None:
        """Show both titles and emit the exact double-click timestamp.

        Example: a 90-second chapter emits `90.0` to the window action.
        """

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aaaaaaaaaaa.mkv"
            path.write_bytes(b"video")
            record = self._record(path)
            chapter_set = chapters.ChapterSet(
                video_id="aaaaaaaaaaa",
                primary_language="nl",
                model="mock",
                generated_at=1,
                duration_ms=180_000,
                chapters=[
                    chapters.Chapter(0, "Inleiding", "Introduction"),
                    chapters.Chapter(90_000, "Vragen", "Questions"),
                ],
            )
            panel = library_widgets.DetailPanel()
            starts: list[float] = []
            panel.chapter_activated.connect(starts.append)

            panel.set_record(record, chapter_set)
            chapter_list = panel.findChild(QtWidgets.QListWidget, "chapterList")
            generate = panel.findChild(QtWidgets.QPushButton, "chapterGenerate")
            self.assertEqual(chapter_list.count(), 2)
            self.assertIn("Vragen", chapter_list.item(1).text())
            self.assertIn("Questions", chapter_list.item(1).text())
            panel._activate_chapter(chapter_list.item(1))
            self.assertEqual(starts, [90.0])
            self.assertTrue(generate.isEnabled())
            panel.set_busy(True)
            self.assertFalse(generate.isEnabled())

    def test_settings_round_trip_recent_history_and_date_cutoff(self) -> None:
        """Expose bounded history and an optional calendar cutoff natively.

        Example: April 2026 is returned as `2026-04-01` after Save.
        """

        values = library_widgets.SettingsValues(4, "firefox", True, 50, "2026-04-01")
        dialog = library_widgets.SettingsDialog(values)

        self.assertEqual(dialog.values(), values)

    def test_chapter_action_seeks_matching_active_player(self) -> None:
        """Prefer the running mpv process without entering the busy task lane.

        Example: a chapter double-click moves the already visible player.
        """

        record = self._record(Path(__file__))
        window = mock.Mock()
        window._selected_record.return_value = record
        window._service.seek.return_value = True
        window._ui = SimpleNamespace(trace=mock.Mock())

        library_chapter_actions.ChapterActionsMixin._play_chapter(window, 90.0)

        window._service.seek.assert_called_once_with("aaaaaaaaaaa", 90.0)
        window._play_from.assert_not_called()
        window.statusBar().showMessage.assert_called_once_with(
            "Seek → 1:30 · Example",
            3_000,
        )
        window._ui.trace.append_message.assert_called_once()

    def test_chapter_action_launches_when_no_matching_player(self) -> None:
        """Retain launch-at-time behavior when the selected video is not active.

        Example: a stopped video opens directly at the chosen chapter.
        """

        window = mock.Mock()
        window._selected_record.return_value = self._record(Path(__file__))
        window._service.seek.return_value = False

        library_chapter_actions.ChapterActionsMixin._play_chapter(window, 90.0)

        window._play_from.assert_called_once_with(90.0)

    def test_playback_dispatches_outside_busy_work_lane(self) -> None:
        """Keep a downloaded video playable while another video is processing.

        Example: an active Whisper task does not queue or reject an mpv launch.
        """

        record = self._record(Path(__file__))
        window = mock.Mock()
        window._busy = True
        window._selected_record.return_value = record

        library_chapter_actions.ChapterActionsMixin._play_from(window, None)

        window._run_task.assert_not_called()
        window._run_playback.assert_called_once()
        label, play, _finished = window._run_playback.call_args.args
        self.assertEqual(label, "Opening mpv…")
        report = mock.Mock()
        play(report)
        window._service.play.assert_called_once_with(
            "aaaaaaaaaaa",
            report,
            start_seconds=None,
        )

    def test_play_button_remains_enabled_during_processing(self) -> None:
        """Separate play availability from the heavy-operation busy flag.

        Example: Download and Check stay disabled while Play remains enabled.
        """

        record = self._record(Path(__file__))
        header = SimpleNamespace(
            download=mock.Mock(),
            play=mock.Mock(),
            check=mock.Mock(),
        )
        detail = mock.Mock()
        window = SimpleNamespace(
            _busy=True,
            _selected_record=lambda: record,
            _ui=SimpleNamespace(
                header=header,
                catalog=SimpleNamespace(detail=detail),
            ),
        )

        library_gui.LibraryWindow._update_actions(window)

        detail.set_busy.assert_called_once_with(True)
        header.download.setEnabled.assert_called_once_with(False)
        header.play.setEnabled.assert_called_once_with(True)
        header.check.setEnabled.assert_called_once_with(False)

    @staticmethod
    def _record(path: Path) -> types.VideoRecord:
        """Build one downloaded row for inspector rendering.

        Example: `_record(path)` supplies title, origin, and local state.
        """

        ident = types.VideoIdentity("aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa", "Example")
        origin = types.VideoOrigin("Example channel", "UC-example", 1)
        details = types.VideoDetails(180, 10, "Description", None, "not_live")
        local = types.LocalMedia(path, 2, path.stat().st_size)
        return types.VideoRecord(types.VideoMeta(ident, origin, details), 1, 1, local, None, None)


class HeaderLayoutTests(unittest.TestCase):
    """Verify interactive video columns and durable native header state.

    Example: `HeaderLayoutTests("test_width_and_order_round_trip")`.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Create the QApplication required by table and header widgets.

        Example: handled once when this test class runs independently.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_width_and_order_round_trip(self) -> None:
        """Restore a moved and resized column into a fresh table.

        Example: Title remains first and 333 pixels wide after restart.
        """

        source, _model = self._table()
        source_header = source.horizontalHeader()
        source_header.moveSection(source_header.visualIndex(library_model.TITLE_COLUMN), 0)
        source.setColumnWidth(library_model.TITLE_COLUMN, 333)
        source_db = mock.Mock()
        source_window = self._window(source, source_db)

        library_gui.LibraryWindow._store_table_layout(source_window)

        key, encoded = source_db.set_setting.call_args.args
        self.assertEqual(key, "video_table_header_v1")
        target, _model = self._table()
        target_db = mock.Mock()
        target_db.setting.return_value = encoded
        target_window = self._window(target, target_db)
        library_gui.LibraryWindow._restore_table_layout(target_window)
        target_header = target.horizontalHeader()
        self.assertEqual(target_header.visualIndex(library_model.TITLE_COLUMN), 0)
        self.assertEqual(target.columnWidth(library_model.TITLE_COLUMN), 333)

    def test_default_layout_is_movable_resizable_and_resettable(self) -> None:
        """Keep every section interactive and recover the shipped arrangement.

        Example: View → Reset column layout puts Title back in column three.
        """

        table, model = self._table()
        header = table.horizontalHeader()
        self.assertTrue(header.sectionsMovable())
        for column in range(model.columnCount()):
            self.assertEqual(
                header.sectionResizeMode(column),
                QtWidgets.QHeaderView.ResizeMode.Interactive,
            )
        header.moveSection(header.visualIndex(library_model.TITLE_COLUMN), 0)
        table.setColumnWidth(library_model.TITLE_COLUMN, 333)

        library_gui.LibraryWindow._apply_default_table_layout(table, model)

        self.assertEqual(header.visualIndex(library_model.TITLE_COLUMN), library_model.TITLE_COLUMN)
        self.assertEqual(table.columnWidth(library_model.TITLE_COLUMN), 420)

    @staticmethod
    def _table() -> tuple[QtWidgets.QTableView, library_model.VideoTableModel]:
        """Create one table using the production default-layout policy.

        Example: `_table()` supplies a source or simulated restarted table.
        """

        model = library_model.VideoTableModel()
        table = QtWidgets.QTableView()
        table.setModel(model)
        library_gui.LibraryWindow._apply_default_table_layout(table, model)
        return table, model

    @staticmethod
    def _window(table: QtWidgets.QTableView, db: mock.Mock) -> SimpleNamespace:
        """Build the small window-shaped collaborator used by persistence methods.

        Example: `_window(table, db)` avoids booting background library work.
        """

        catalog = SimpleNamespace(table=table)
        return SimpleNamespace(
            _ui=SimpleNamespace(catalog=catalog),
            _service=SimpleNamespace(db=db),
        )


class ChannelQueueTests(unittest.TestCase):
    """Keep subscription input immediate while execution remains serial.

    Example: `ChannelQueueTests("test_add_is_allowed_during_video_work")`.
    """

    @mock.patch("yt_whisper_subs.library_gui.library_widgets.AddChannelDialog")
    def test_add_is_allowed_during_video_work(self, dialog_cls: mock.Mock) -> None:
        """Persist and display a channel even while the video lane is busy.

        Example: several handles can be entered during one Whisper run.
        """

        dialog_cls.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
        dialog_cls.return_value.values.return_value = ("@ruis", False)
        channel = SimpleNamespace(channel_id=7, title="@ruis")
        window = mock.Mock()
        window._busy = True
        window._service.track_channel.return_value = channel

        library_gui.LibraryWindow._add_channel(window)

        window._service.track_channel.assert_called_once_with("@ruis", False)
        window.refresh.assert_called_once()
        label, initialize = window._queue_channel_task.call_args.args
        self.assertEqual(label, "Adding @ruis…")
        report = mock.Mock()
        initialize(report)
        window._service.initialize_channel.assert_called_once_with(7, report)

    def test_channel_lookups_share_the_single_worker_queue(self) -> None:
        """Queue multiple lookups on the same one-thread pool as video jobs.

        Example: two new channels wait behind an active download in entry order.
        """

        window = SimpleNamespace(
            _busy=True,
            _channel_tasks={},
            _metadata_timer=mock.Mock(),
            _pool=mock.Mock(),
            _ui=SimpleNamespace(trace=mock.Mock()),
            _report_channel=mock.Mock(),
        )
        task_fn = mock.Mock()

        library_window_support.WindowRuntimeMixin._queue_channel_task(
            window,
            "Adding first…",
            task_fn,
        )
        library_window_support.WindowRuntimeMixin._queue_channel_task(
            window,
            "Adding second…",
            task_fn,
        )

        self.assertEqual(window._pool.start.call_count, 2)
        self.assertEqual(len(window._channel_tasks), 2)
        queued = [call.args[0] for call in window._ui.trace.append_message.call_args_list]
        self.assertIn("○ Channel queued · Adding first…", queued)
        self.assertIn("○ Channel queued · Adding second…", queued)

    def test_single_worker_executes_waiting_channels_in_order(self) -> None:
        """Verify queued work cannot overlap and retains submission order.

        Example: first and second channel lookups follow the active download.
        """

        pool = QtCore.QThreadPool()
        pool.setMaxThreadCount(1)
        entered = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def active(_report: object) -> None:
            """Occupy the sole execution slot until both lookups are waiting.

            Example: this represents a long-running subtitle pipeline.
            """

            entered.set()
            release.wait(2)

        def first(_report: object) -> None:
            """Record the first queued channel lookup.

            Example: the first submitted handle resolves first.
            """

            order.append("first")

        def second(_report: object) -> None:
            """Record the second queued channel lookup.

            Example: the second submitted handle resolves second.
            """

            order.append("second")

        pool.start(library_workers.BackgroundTask(active))
        self.assertTrue(entered.wait(2))
        pool.start(library_workers.BackgroundTask(first))
        pool.start(library_workers.BackgroundTask(second))
        release.set()

        self.assertTrue(pool.waitForDone(5_000))
        self.assertEqual(order, ["first", "second"])


class PlaybackLaneTests(unittest.TestCase):
    """Verify playback can run beside the serialized heavy-operation queue.

    Example: mpv starts while a simulated Whisper worker occupies its lane.
    """

    def test_playback_progress_preserves_active_pipeline_status(self) -> None:
        """Keep concurrent watched updates out of the busy status bar.

        Example: an mpv observation updates its row without hiding Translation.
        """

        window = mock.Mock()
        window._busy = True
        update = playback_progress.make("aaaaaaaaaaa", 30, 100)

        library_window_support.WindowRuntimeMixin._report_playback(
            window,
            playback_progress.encode(update),
        )

        window._show_watched_progress.assert_called_once_with(
            update,
            update_status=False,
        )

    def test_playback_starts_while_heavy_worker_is_occupied(self) -> None:
        """Use independent thread pools for visible playback and compute work.

        Example: playback enters before the blocked heavy task is released.
        """

        heavy_pool = QtCore.QThreadPool()
        heavy_pool.setMaxThreadCount(1)
        playback_pool = QtCore.QThreadPool()
        playback_pool.setMaxThreadCount(2)
        heavy_entered = threading.Event()
        release_heavy = threading.Event()
        playback_entered = threading.Event()

        def heavy(_report: object) -> None:
            """Hold the serial lane to model an active subtitle pipeline.

            Example: release_heavy ends the simulated Whisper operation.
            """

            heavy_entered.set()
            release_heavy.wait(2)

        def play(_report: object) -> None:
            """Record immediate entry into the independent playback lane.

            Example: setting playback_entered represents mpv launch.
            """

            playback_entered.set()

        window = SimpleNamespace(
            _busy=True,
            _playback_pool=playback_pool,
            _playback_tasks={},
            _ui=SimpleNamespace(trace=mock.Mock()),
            _report_playback=mock.Mock(),
            statusBar=mock.Mock(),
        )
        heavy_pool.start(library_workers.BackgroundTask(heavy))
        self.assertTrue(heavy_entered.wait(2))

        library_window_support.WindowRuntimeMixin._run_playback(
            window,
            "Opening mpv…",
            play,
        )

        self.assertTrue(playback_entered.wait(2))
        release_heavy.set()
        self.assertTrue(heavy_pool.waitForDone(5_000))
        self.assertTrue(playback_pool.waitForDone(5_000))


if __name__ == "__main__":
    unittest.main()
