"""GUI actions that generate chapters and launch chapter-aware playback.

Example: `LibraryWindow` composes `ChapterActionsMixin` with its Qt shell.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore

from yt_whisper_subs import openai_chapters


class ChapterActionsMixin:
    """Add contextual chapter generation and playback to a library window.

    Example: `window._play_chapter(90.0)` opens mpv at the chosen chapter.
    """

    def _generate_chapters_selected(self) -> None:
        """Regenerate the selected download's bilingual chapter sidecars.

        Example: the inspector's Generate action invokes this method.
        """

        record = self._selected_record()
        if not record or not record.downloaded:
            return
        video_id = record.meta.identity.video_id

        def generate(report: Callable[[str], None]) -> object:
            """Bind the selected ID into the worker-safe service call.

            Example: `generate(report)` runs outside the GUI thread.
            """

            return self._service.generate_chapters(video_id, report)

        self._queue_video_task(
            video_id,
            "Creating bilingual chapters…",
            generate,
            lambda _: self.refresh(),
        )

    def _play_selected(self) -> None:
        """Open the selected download through chapter-aware shared playback.

        Example: the Play button invokes `_play_selected()`.
        """

        self._play_from(None)

    @QtCore.Slot(float)
    def _play_chapter(self, start_seconds: float) -> None:
        """Seek its active mpv session or open at an inspector boundary.

        Example: double-clicking a chapter invokes `_play_chapter(750)`.
        """

        record = self._selected_record()
        if record and self._service.seek(record.meta.identity.video_id, start_seconds):
            timestamp = openai_chapters.format_time(round(start_seconds * 1000))
            title = record.meta.identity.title
            message = f"Seek → {timestamp} · {title}"
            self.statusBar().showMessage(message, 3_000)
            self._ui.trace.append_message(message)
            return
        self._play_from(start_seconds)

    def _play_from(self, start_seconds: float | None) -> None:
        """Dispatch mpv without blocking Qt, optionally seeking at launch.

        Example: `_play_from(None)` preserves mpv's normal resume policy.
        """

        record = self._selected_record()
        if not record:
            return
        if not record.downloaded:
            self._download_selected()
            return
        video_id = record.meta.identity.video_id

        def play(report: Callable[[str], None]) -> None:
            """Keep mpv and temporary playback assets off the GUI thread.

            Example: `play(report)` blocks only its background worker.
            """

            report(f"Playing {record.meta.identity.title}")
            self._service.play(video_id, report, start_seconds=start_seconds)

        status = "Opening chapter in mpv…" if start_seconds is not None else "Opening mpv…"
        self._run_playback(status, play, lambda _: self.refresh())
