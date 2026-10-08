"""Exercise double-click playback intent through real Qt signals and catalog state.

Example: `python -m unittest tests.test_library_deferred_playback`.
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

from yt_whisper_subs import library_gui  # noqa: E402
from yt_whisper_subs import library_service  # noqa: E402
from yt_whisper_subs import library_types as types  # noqa: E402
from yt_whisper_subs import pipeline_progress as progress  # noqa: E402


class DeferredPlaybackTests(unittest.TestCase):
    """Verify playback targets and outcomes without external downloads or mpv.

    Example: finishing a controlled worker exercises the production completion slot.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Keep one Qt application for the real table and worker signals.

        Example: every test activates a production proxy index.
        """

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        """Create two remote rows and capture worker dispatch for controlled outcomes.

        Example: captured workers can finish or cancel without launching a CLI child.
        """

        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._service = library_service.LibraryService(self._root, {"python": Path(sys.executable)})
        for video_id, title in (("aaaaaaaaaaa", "First"), ("bbbbbbbbbbb", "Second")):
            ident = types.VideoIdentity(video_id, f"https://youtu.be/{video_id}", title)
            origin = types.VideoOrigin("Channel", "UC-example", 100)
            details = types.VideoDetails(90, None, "", None, "not_live")
            self._service.db.upsert_video(types.VideoMeta(ident, origin, details))
        self._window = library_gui.LibraryWindow(self._service)
        self._window._timer.stop()
        self._window._metadata_timer.stop()
        self._tasks = []
        self._players = []
        patches = (
            mock.patch.object(self._window._pool, "start", side_effect=self._tasks.append),
            mock.patch.object(self._window._playback_pool, "start", side_effect=self._players.append),
            mock.patch.object(self._service, "play"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        """Dispose the test window and watchers before removing isolated files.

        Example: a nonmodal failure dialog is closed with its parent window.
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

    def _activate(self, video_id: str) -> None:
        """Emit the table's actual double-click signal for a stable video ID.

        Example: sorting cannot redirect `_activate("aaaaaaaaaaa")` to the second row.
        """

        catalog = self._window._ui.catalog
        row = catalog.model.row_for(video_id)
        index = catalog.proxy.mapFromSource(catalog.model.index(row, 0))
        catalog.table.doubleClicked.emit(index)

    def _make_ready(self, video_id: str) -> None:
        """Reconcile usable media and subtitle yields into the real SQLite catalog.

        Example: completion reads this fresh record even while another row is selected.
        """

        video_dir = self._root / "videos"
        video_dir.mkdir(exist_ok=True)
        (video_dir / f"{video_id}.mkv").write_bytes(b"video")
        cue = "1\n00:00:00,000 --> 00:00:01,000\nHello.\n"
        for suffix in ("srt", "en.srt"):
            (video_dir / f"{video_id}.{suffix}").write_text(cue, encoding="utf-8")
        self._service.scan_local()

    def test_repeated_activation_plays_once_despite_selection_and_filter_changes(self) -> None:
        """Keep initial processing silent and pin one later playback request to its ID.

        Example: selecting and filtering to B still opens completed A exactly once.
        """

        self._activate("aaaaaaaaaaa")
        self.assertEqual(self._window._play_after_pipeline, set())
        self.assertEqual(len(self._tasks), 1)
        self._activate("aaaaaaaaaaa")
        self._activate("aaaaaaaaaaa")
        self.assertEqual(self._window._play_after_pipeline, {"aaaaaaaaaaa"})
        self.assertIn("Will play when processing finishes", self._window.statusBar().currentMessage())
        self.assertEqual(self._players, [])
        self._activate("bbbbbbbbbbb")
        self._window._ui.header.search.setText("Second")
        self._make_ready("aaaaaaaaaaa")
        self._tasks[0].signals.finished.emit(None)

        self.assertEqual(self._window._active_video_id, "bbbbbbbbbbb")
        self.assertEqual(self._window._play_after_pipeline, set())
        self.assertEqual(len(self._players), 1)
        self._players[0].run()
        self.assertEqual(self._service.play.call_args.args[0], "aaaaaaaaaaa")
        self._service.play.assert_called_once()

    def test_queued_request_survives_another_videos_completion(self) -> None:
        """Retain the waiting target's intent until that specific pipeline finishes.

        Example: finishing A advances to B without consuming B's playback request.
        """

        self._activate("aaaaaaaaaaa")
        self._activate("bbbbbbbbbbb")
        self._activate("bbbbbbbbbbb")
        self._make_ready("aaaaaaaaaaa")
        self._tasks[0].signals.finished.emit(None)
        self.assertEqual(self._window._play_after_pipeline, {"bbbbbbbbbbb"})
        self.assertEqual(self._players, [])
        self._make_ready("bbbbbbbbbbb")
        self._tasks[1].signals.finished.emit(None)

        self.assertEqual(len(self._players), 1)
        self._players[0].run()
        self.assertEqual(self._service.play.call_args.args[0], "bbbbbbbbbbb")

    def test_downloaded_paused_work_waits_for_worker_success(self) -> None:
        """Give pending work precedence over playable files and early Ready events.

        Example: chapter regeneration waits through pause and final catalog scanning.
        """

        self._make_ready("aaaaaaaaaaa")
        self._window.refresh()
        self._activate("aaaaaaaaaaa")
        self._players.clear()
        self._window._generate_chapters_selected()
        task = self._tasks[0]
        task.signals.progress.emit(progress.encode(progress.make("aaaaaaaaaaa", progress.Stage.PAUSED)))
        self._activate("aaaaaaaaaaa")
        task.signals.progress.emit(progress.encode(progress.make("aaaaaaaaaaa", progress.Stage.READY)))
        self._activate("aaaaaaaaaaa")
        self.assertEqual(self._players, [])
        task.signals.finished.emit(None)

        self.assertEqual(len(self._players), 1)
        self.assertEqual(self._window._play_after_pipeline, set())

    def test_failure_and_cancellation_clear_only_the_active_request(self) -> None:
        """Suppress playback after unsuccessful work while preserving waiting intent.

        Example: cancelling A leaves B queued with its own playback request.
        """

        for outcome in ("failed", "cancelled"):
            with self.subTest(outcome=outcome):
                self._activate("aaaaaaaaaaa")
                self._activate("aaaaaaaaaaa")
                self._activate("bbbbbbbbbbb")
                self._activate("bbbbbbbbbbb")
                task = self._tasks[-1]
                if outcome == "failed":
                    task.signals.failed.emit("Pipeline failed", "Controlled failure")
                else:
                    task.signals.cancelled.emit()
                self.assertEqual(self._players, [])
                self.assertEqual(self._window._play_after_pipeline, {"bbbbbbbbbbb"})
                self._tasks[-1].signals.cancelled.emit()
                self.assertEqual(self._window._play_after_pipeline, set())

    def test_incomplete_success_does_not_process_a_different_selected_row(self) -> None:
        """Check the requested catalog record before any deferred playback launch.

        Example: missing A yields must not start a fresh download of selected B.
        """

        self._activate("aaaaaaaaaaa")
        self._activate("aaaaaaaaaaa")
        self._window._ui.catalog.table.clearSelection()
        self._tasks[0].signals.finished.emit(None)

        self.assertEqual(self._players, [])
        self.assertEqual(len(self._tasks), 1)
        self.assertEqual(self._window._play_after_pipeline, set())

    def test_quitting_suppresses_deferred_playback(self) -> None:
        """Consume a completed request without launching a player during shutdown.

        Example: queued completion signals arriving after Quit remain harmless.
        """

        self._activate("aaaaaaaaaaa")
        self._activate("aaaaaaaaaaa")
        self._make_ready("aaaaaaaaaaa")
        self._window._quitting = True
        self._tasks[0].signals.finished.emit(None)

        self.assertEqual(self._players, [])
        self.assertEqual(self._window._play_after_pipeline, set())

    def test_auto_download_transition_consumes_the_finished_target(self) -> None:
        """Start a finished automatic target while a channel batch advances to its next video.

        Example: A starts playing when automatic processing switches to B.
        """

        self._window._run_task("Checking channels…", mock.Mock())
        self._window._report_progress(progress.encode(progress.make("aaaaaaaaaaa", progress.Stage.TRANSCRIBING)))
        self._activate("aaaaaaaaaaa")
        self._make_ready("aaaaaaaaaaa")
        self._window._report_progress(progress.encode(progress.make("aaaaaaaaaaa", progress.Stage.READY)))
        self.assertEqual(self._players, [])
        self._window._report_progress(progress.encode(progress.make("bbbbbbbbbbb", progress.Stage.QUEUED)))

        self.assertEqual(len(self._players), 1)
        self.assertEqual(self._window._play_after_pipeline, set())


if __name__ == "__main__":
    unittest.main()
