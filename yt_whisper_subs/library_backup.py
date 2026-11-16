"""Verified catalog snapshots and their independent startup worker.

Example: `StartupBackup(db.path, app)` keeps ten backups without delaying Qt.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from PySide6 import QtCore

from yt_whisper_subs import library_workers
from yt_whisper_subs import task_cancel


KEEP = 10
TIMEOUT_SECONDS = 120
_NAME = re.compile(r"catalog-\d{8}T\d{12}Z")


class Backups:
    """Publish complete SQLite snapshots before rotating older ones; e.g. a live WAL catalog."""

    def __init__(self, path: Path) -> None:
        """Keep backups beside their catalog; e.g. a custom output root stays self-contained."""

        self._path = path.resolve()
        self._folder = self._path.parent / "backups"

    def run(self, report: Callable[[str], None]) -> Path:
        """Copy committed state and retain ten verified snapshots; e.g. on every library launch."""

        self._folder.mkdir(parents=True, exist_ok=True)
        for stale in self._files(".partial"):
            self._discard(stale)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = self._folder / f"catalog-{stamp}.sqlite3"
        partial = target.with_suffix(".partial")
        partial.touch(exist_ok=False)
        try:
            report("Backup · Saving catalog in the background…")
            self._copy(partial)
            partial.rename(target)
            # Finish rotation even if Quit arrives after publication.
            for old in self._files(".sqlite3")[:-KEEP]:
                old.unlink()
        finally:
            self._discard(partial)
        report(f"Backup · Saved {target} · newest {KEEP} retained")
        return target

    def _copy(self, partial: Path) -> None:
        """Bound and verify SQLite's live copy; e.g. committed WAL pages belong in the snapshot."""

        control = task_cancel.current()
        deadline = time.monotonic() + TIMEOUT_SECONDS

        def checkpoint(*_progress: int) -> None:
            """Bound copying and lock retries; e.g. Quit cancels the next page batch."""

            if control:
                control.checkpoint()
            if time.monotonic() > deadline:
                raise TimeoutError("Catalog backup exceeded two minutes")

        checkpoint()
        uri = self._path.as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as src:
                with closing(sqlite3.connect(partial)) as dst:
                    src.backup(dst, pages=256, progress=checkpoint, sleep=0.1)
                    # A single standalone file must remain usable without WAL companions.
                    dst.execute("PRAGMA journal_mode=DELETE")
                    dst.set_progress_handler(
                        lambda: bool(control and control.cancelled) or time.monotonic() > deadline,
                        1000,
                    )
                    integrity = dst.execute("PRAGMA integrity_check").fetchall()
                    if integrity != [("ok",)]:
                        raise RuntimeError(f"Catalog backup failed integrity check: {integrity}")
        except sqlite3.OperationalError:
            checkpoint()
            raise
        checkpoint()

    def _files(self, suffix: str) -> list[Path]:
        """Select only owned timestamped files; e.g. a manual rescue copy is never rotated."""

        paths = self._folder.glob(f"catalog-*{suffix}")
        return sorted(
            path for path in paths
            if _NAME.fullmatch(path.stem) and path.is_file() and not path.is_symlink()
        )

    @staticmethod
    def _discard(path: Path) -> None:
        """Remove an unfinished snapshot and SQLite companions; e.g. after an interrupted launch."""

        for suffix in ("", "-journal", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


class StartupBackup(QtCore.QObject):
    """Schedule one cancellable backup outside the pipeline queue; e.g. quiet tray startup."""

    message = QtCore.Signal(str)

    def __init__(self, path: Path, parent: QtCore.QObject) -> None:
        """Defer all backup I/O until a worker runs; e.g. the window can show immediately."""

        super().__init__(parent)
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._task: library_workers.BackgroundTask | None = library_workers.BackgroundTask(Backups(path).run)
        self._task.signals.progress.connect(self.message)
        self._task.signals.failed.connect(self._failed)
        self._task.signals.finished.connect(self._finished)
        self._task.signals.cancelled.connect(self._finished)
        QtCore.QTimer.singleShot(0, self, self._start)

    def _start(self) -> None:
        """Dispatch at most once after Qt starts; e.g. no copying happens in the constructor."""

        if self._task:
            self._pool.start(self._task)

    def _failed(self, message: str, _trace: str) -> None:
        """Keep failures nonmodal and visible in the activity trace; e.g. a full backup disk."""

        self.message.emit(f"✗ Catalog backup · {message}")
        self._finished()

    def _finished(self, *_args: object) -> None:
        """Release the completed worker reference; e.g. later Quit has nothing left to cancel."""

        self._task = None

    def stop(self) -> None:
        """Cancel without waiting on the GUI thread; e.g. Quit during a locked-source retry."""

        if self._task:
            self._task.cancel()
        self._pool.clear()
