"""Tests for cooperative task cancellation and exact video-yield removal.

Example: `python -m unittest tests.test_task_cancel`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import library_yields
from yt_whisper_subs import task_cancel


class TaskControlTests(unittest.TestCase):
    """Keep pause and cancellation scoped to active blocking work.

    Example: a registered process stop hook runs once on the first request.
    """

    def test_cancel_invokes_active_stop_and_raises_at_checkpoint(self) -> None:
        """Stop active work immediately and expose a distinct terminal exception.

        Example: the worker maps this exception to its cancelled Qt signal.
        """

        control = task_cancel.TaskControl()
        stop = mock.Mock()
        with self.assertRaises(task_cancel.CancelledError), control.controllable(
            stop,
            mock.Mock(),
            mock.Mock(),
        ):
            self.assertTrue(control.cancel())
        self.assertFalse(control.cancel())
        stop.assert_called_once_with()

    def test_pause_and_resume_invoke_active_process_hooks(self) -> None:
        """Toggle the active process tree without cancelling the worker.

        Example: Whisper can continue after an arbitrary user pause.
        """

        control = task_cancel.TaskControl()
        pause = mock.Mock()
        resume = mock.Mock()
        with control.controllable(mock.Mock(), pause, resume):
            self.assertTrue(control.pause())
            self.assertTrue(control.paused)
            self.assertTrue(control.resume())

        pause.assert_called_once_with()
        resume.assert_called_once_with()
        self.assertFalse(control.paused)


class VideoYieldRemovalTests(unittest.TestCase):
    """Prove removal cannot cross a managed folder or neighboring video ID.

    Example: removing `abcdefghijk` leaves `abcdefghijk2` untouched.
    """

    def test_manifest_removes_each_exact_video_file_only(self) -> None:
        """Inventory ID-delimited yields without recursion or wildcard deletion.

        Example: media, logs, and partial translation state are removed individually.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets = [
                root / "videos" / "abcdefghijk.mkv",
                root / "videos" / "abcdefghijk.en.partial.json",
                root / "subtitles" / "abcdefghijk.srt",
                root / "logs" / "abcdefghijk-20260101-120000.log",
            ]
            neighbor = root / "videos" / "abcdefghijk2.mkv"
            for path in [*targets, neighbor]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("yield", encoding="utf-8")

            manifest = library_yields.VideoYields.inspect(root, "abcdefghijk")
            reports: list[str] = []
            removed = manifest.remove(reports.append)

            self.assertEqual(set(manifest.paths), {path.absolute() for path in targets})
            self.assertEqual(removed, len(targets))
            self.assertEqual(len(reports), len(targets))
            self.assertTrue(neighbor.exists())
            self.assertTrue(all(not path.exists() for path in targets))
