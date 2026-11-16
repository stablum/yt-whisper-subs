"""Exercise live SQLite snapshots, bounded retention, and responsive startup.

Example: `python -m unittest tests.test_library_backup -v` uses isolated catalogs.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402

from yt_whisper_subs import library_app  # noqa: E402
from yt_whisper_subs import library_backup  # noqa: E402
from yt_whisper_subs import task_cancel  # noqa: E402


class BackupTests(unittest.TestCase):
    """Protect complete copies and their retention boundary; e.g. a writer still has WAL pages."""

    def setUp(self) -> None:
        """Use a tiny live catalog with uncheckpointed state; e.g. vocabulary saved while opening."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._path = Path(tmp.name) / "library" / "catalog.sqlite3"
        self._path.parent.mkdir()
        self._conn = sqlite3.connect(self._path)
        self.addCleanup(self._conn.close)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=0")
        self._conn.execute("CREATE TABLE words (word TEXT PRIMARY KEY)")
        self._conn.execute("INSERT INTO words VALUES ('fiets')")
        self._conn.commit()
        self._folder = self._path.parent / "backups"
        self._backups = library_backup.Backups(self._path)

    def _run(self) -> Path:
        """Save a snapshot without UI output; e.g. repeated launches exercise rotation."""

        return self._backups.run(lambda _message: None)

    def test_snapshot_includes_committed_wal_and_excludes_uncommitted_edits(self) -> None:
        """Reopen a standalone verified snapshot; e.g. an active writer need not close first."""

        self._conn.execute("INSERT INTO words VALUES ('uncommitted')")
        backup = self._run()
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("SELECT word FROM words").fetchall(), [("fiets",)])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(list(self._folder.iterdir()), [backup])
        self.assertEqual(self._conn.execute("SELECT COUNT(*) FROM words").fetchone()[0], 2)
        self._conn.rollback()

    def test_rotation_retains_newest_ten_and_preserves_unrelated_files(self) -> None:
        """Age by snapshot names rather than mtimes; e.g. copying files cannot invert retention."""

        self._folder.mkdir()
        rescue = self._folder / "manual-rescue.sqlite3"
        rescue.write_bytes(b"keep me")
        other = self._folder / "catalog-not-a-timestamp.sqlite3"
        other.write_bytes(b"keep me too")
        snapshots = []
        for idx in range(13):
            self._conn.execute("INSERT INTO words VALUES (?)", (str(idx),))
            self._conn.commit()
            snapshots.append(self._run())
            self.assertLessEqual(len(list(self._folder.glob("catalog-????????T????????????Z.sqlite3"))), 10)
        managed = sorted(self._folder.glob("catalog-????????T????????????Z.sqlite3"))
        self.assertEqual(managed, snapshots[-10:])
        self.assertEqual(rescue.read_bytes(), b"keep me")
        self.assertEqual(other.read_bytes(), b"keep me too")
        with closing(sqlite3.connect(managed[0])) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM words").fetchone()[0], 5)

    def test_failed_copy_preserves_ten_previous_snapshots_and_removes_partial(self) -> None:
        """Never rotate away a good copy to make room for a failed one; e.g. a corrupt source."""

        saved = [self._run() for _idx in range(10)]
        self._conn.close()
        self._path.write_bytes(b"not a SQLite database")
        with self.assertRaises(sqlite3.DatabaseError):
            self._run()
        self.assertEqual(sorted(self._folder.iterdir()), saved)

    def test_integrity_failure_keeps_previous_snapshot(self) -> None:
        """Reject a copy whose full check fails; e.g. pages copy but logical indexes are damaged."""

        saved = self._run()
        connect = sqlite3.connect

        # A connection subclass preserves the actual SQLite backup API boundary.
        class BadIntegrity(sqlite3.Connection):
            """Simulate a failed verification query; e.g. the copied file must stay unpublished."""

            def execute(self, sql, *args, **kwargs):
                """Change just the integrity result; e.g. real destination writes still take place."""

                if sql == "PRAGMA integrity_check":
                    return mock.Mock(fetchall=lambda: [("damaged index",)])
                return super().execute(sql, *args, **kwargs)

        def open_db(*args, **kwargs):
            """Keep genuine SQLite connections with an injected verdict; e.g. test publication failure."""

            return connect(*args, factory=BadIntegrity, **kwargs)

        with mock.patch.object(library_backup.sqlite3, "connect", side_effect=open_db):
            with self.assertRaisesRegex(RuntimeError, "integrity check"):
                self._run()
        self.assertEqual(list(self._folder.iterdir()), [saved])

    def test_stale_partials_and_their_journals_are_cleaned(self) -> None:
        """Recover bounded disk usage after a crash; e.g. a prior copy left WAL companions."""

        self._folder.mkdir()
        stale = self._folder / "catalog-20260101T000000000000Z.partial"
        for suffix in ("", "-wal", "-shm", "-journal"):
            stale.with_name(stale.name + suffix).write_bytes(b"unfinished")
        unrelated = self._folder / "manual.partial"
        unrelated.write_bytes(b"keep")
        backup = self._run()
        self.assertEqual(set(self._folder.iterdir()), {unrelated, backup})

    def test_cancellation_during_progress_removes_the_unpublished_copy(self) -> None:
        """Stop promptly without replacing history; e.g. Quit just after the worker starts."""

        saved = self._run()
        control = task_cancel.TaskControl()
        with task_cancel.activate(control):
            with self.assertRaises(task_cancel.CancelledError):
                self._backups.run(lambda _message: control.cancel())
        self.assertEqual(list(self._folder.iterdir()), [saved])

    def test_timeout_cleans_partial_and_preserves_previous_copy(self) -> None:
        """Bound source lock waits as well as normal copying; e.g. a busy catalog never hangs exit."""

        saved = self._run()
        with mock.patch.object(library_backup.time, "monotonic", side_effect=[0, 0, 121]):
            with self.assertRaises(TimeoutError):
                self._run()
        self.assertEqual(list(self._folder.iterdir()), [saved])

    def test_quit_cancels_retries_against_an_exclusively_locked_source(self) -> None:
        """Exercise actual SQLite busy retries; e.g. a lock cannot strand the backup on shutdown."""

        saved = self._run()
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("BEGIN EXCLUSIVE")
        control = task_cancel.TaskControl()
        attempted = threading.Event()
        errors = []

        def copy_locked() -> None:
            """Run real backup retries under cancellation; e.g. the UI owns only the stop token."""

            with task_cancel.activate(control):
                try:
                    self._backups.run(lambda _message: attempted.set())
                except Exception as exc:
                    errors.append(exc)

        worker = threading.Thread(target=copy_locked)
        worker.start()
        try:
            self.assertTrue(attempted.wait(2))
            control.cancel()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        finally:
            self._conn.rollback()
            worker.join(5)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], task_cancel.CancelledError)
        self.assertEqual(list(self._folder.iterdir()), [saved])


class StartupBackupTests(unittest.TestCase):
    """Exercise real worker dispatch and Qt events; e.g. a stalled backup cannot freeze startup."""

    @classmethod
    def setUpClass(cls) -> None:
        """Share Qt across isolated startup scenarios; e.g. no actual library window is required."""

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _wait(self, predicate) -> None:
        """Pump UI events with a short deadline; e.g. await the worker's queued finish signal."""

        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self._app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_start_is_deferred_and_ui_events_continue_during_backup(self) -> None:
        """Observe a UI heartbeat while an actual worker waits; e.g. backup I/O is off the main thread."""

        started, release = threading.Event(), threading.Event()
        threads = []

        def slow_backup(_self, report):
            """Hold the worker independently of Qt; e.g. model a slow disk without blocking the test."""

            threads.append(threading.get_ident())
            started.set()
            release.wait(5)
            report("saved")

        with mock.patch.object(library_backup.Backups, "run", slow_backup):
            backup = library_backup.StartupBackup(Path("unused.sqlite3"), self._app)
            messages = []
            backup.message.connect(messages.append)
            self.assertFalse(started.is_set())
            try:
                self._wait(started.is_set)
                heartbeat = []
                QtCore.QTimer.singleShot(0, lambda: heartbeat.append(True))
                self._wait(lambda: bool(heartbeat))
                self.assertEqual(messages, [])
                self.assertNotEqual(threads, [threading.get_ident()])
            finally:
                release.set()
                backup._pool.waitForDone(5000)
            self._wait(lambda: messages == ["saved"] and backup._task is None)
            backup.deleteLater()

    def test_failure_is_reported_without_escaping_to_the_event_loop(self) -> None:
        """Keep startup usable after a disk error; e.g. trace output replaces a modal dialog."""

        with mock.patch.object(library_backup.Backups, "run", side_effect=OSError("disk full")):
            backup = library_backup.StartupBackup(Path("unused.sqlite3"), self._app)
            messages = []
            backup.message.connect(messages.append)
            self._wait(lambda: backup._task is None)
            self.assertEqual(messages, ["✗ Catalog backup · disk full"])
            backup.deleteLater()

    def test_stopping_before_dispatch_never_opens_a_database(self) -> None:
        """Cancel a pending startup without doing I/O; e.g. immediate Quit."""

        with mock.patch.object(library_backup.Backups, "run") as run:
            backup = library_backup.StartupBackup(Path("unused.sqlite3"), self._app)
            backup.stop()
            self._wait(lambda: backup._task is None)
            run.assert_not_called()
            backup.deleteLater()

    def test_entrypoint_schedules_once_for_visible_and_tray_startup(self) -> None:
        """Wire both real launch paths to the configured catalog; e.g. Windows login also backs up."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = mock.Mock()
            service.db.path = root / "library" / "catalog.sqlite3"
            for hidden in (False, True):
                with (
                    self.subTest(hidden=hidden),
                    mock.patch.object(library_app.proc, "configure_stdio"),
                    mock.patch.object(library_app.proc, "venv_paths", return_value={}),
                    mock.patch.object(library_app.library_service, "LibraryService", return_value=service),
                    mock.patch.object(library_app.library_gui, "LibraryWindow") as window,
                    mock.patch.object(library_app.library_backup, "StartupBackup") as start,
                    mock.patch.object(library_app.library_checks, "Checks"),
                    mock.patch.object(library_app.library_practice_ready, "PracticeReady"),
                    mock.patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True),
                    mock.patch.object(self._app, "exec", return_value=0),
                ):
                    args = ["--out-dir", str(root)] + (["--start-hidden"] if hidden else [])
                    self.assertEqual(library_app.main(args), 0)
                    start.assert_called_once_with(service.db.path, self._app)
                    start.return_value.message.connect.assert_called_once_with(
                        window.return_value._ui.trace.append_message,
                    )
                    self.assertEqual(window.return_value.show.call_count, int(not hidden))


if __name__ == "__main__":
    unittest.main()
