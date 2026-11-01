"""Separate fast durable capture from slower, restartable contextual translation.

Example: `VocabularyService(db).submit(selection, notify)` acknowledges the SQLite commit first.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from collections.abc import Callable
from typing import NamedTuple

from yt_whisper_subs import library_db
from yt_whisper_subs import vocabulary as words
from yt_whisper_subs import vocabulary_translate


Notify = Callable[[dict], None]


class Capture(NamedTuple):
    """Queue one intentional click and its player callback; e.g. replay-safe acknowledgement."""

    selection: words.Selection
    notify: Notify


class VocabularyService:
    """Own independent save and lookup lanes; e.g. an API outage never blocks clicks."""

    def __init__(self, db: library_db.LibraryDb, translator: vocabulary_translate.WordTranslator | None = None) -> None:
        """Bind durable queues without starting idle threads; e.g. a newly opened library."""

        self._db = db
        self._translator = translator or vocabulary_translate.WordTranslator()
        self._queue: queue.Queue[Capture | None] = queue.Queue()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._listeners: dict[int, list[Notify]] = {}
        self._threads: tuple[threading.Thread, ...] = ()
        self._owner = uuid.uuid4().hex

    def start(self) -> None:
        """Start lazily and recover pending lookups; e.g. opening Vocabulary after a restart."""

        with self._lock:
            if self._threads or self._stop.is_set():
                return
            self._threads = tuple(threading.Thread(target=fn, name=name, daemon=True)
                                  for fn, name in ((self._save_loop, "vocabulary-save"),
                                                   (self._lookup_loop, "vocabulary-lookup")))
            for thread in self._threads:
                thread.start()

    def submit(self, selection: words.Selection, notify: Notify) -> None:
        """Enqueue an exact click without using the pipeline queue; e.g. while Whisper runs."""

        self.start()
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("vocabulary capture is closing")
            self._queue.put(Capture(selection, notify))

    def resume(self) -> None:
        """Recover durable work only when it exists; e.g. no polling thread for a fresh library."""

        if self._db.has_unfinished_word_lookups():
            self.start()

    def retry(self) -> int:
        """Wake failed translations after a user retry; e.g. repaired API credentials."""

        count = self._db.retry_word_lookups()
        self.start()
        self._wake.set()
        return count

    def marks(self, source_key: str) -> set[tuple[str, int, str, int]]:
        """Expose committed selections without exposing storage; e.g. reopen a video."""

        return self._db.word_marks(source_key)

    def close(self) -> None:
        """Drain accepted clicks without waiting for HTTP; e.g. quit a playing CLI."""

        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            self._queue.put(None)
            self._wake.set()
        if self._threads:
            self._threads[0].join()
            self._threads[1].join(timeout=0.1)

    @staticmethod
    def _notify(fn: Notify, result: dict) -> None:
        """Isolate closed-player callbacks from persistence; e.g. mpv exited during lookup."""

        try:
            fn(result)
        except (OSError, RuntimeError):
            pass

    def _save_loop(self) -> None:
        """Acknowledge only committed events; e.g. no green underline on a disk error."""

        while (capture := self._queue.get()) is not None:
            try:
                identity = self._translator.cache_identity(capture.selection.context)
                result = self._db.save_word(capture.selection, identity)
            except Exception as exc:
                self._notify(capture.notify, {"saved": False, "error": str(exc)})
                continue
            result["saved"] = True
            self._notify(capture.notify, result)
            if result["state"] != "ready":
                with self._lock:
                    self._listeners.setdefault(result["id"], []).append(capture.notify)
                self._wake.set()

    def _lookup_loop(self) -> None:
        """Lease durable work and deliver results; e.g. recover an interrupted request."""

        while not self._stop.is_set():
            try:
                row = self._db.claim_word_lookup(self._owner)
                if row:
                    self._resolve(row)
                self._publish_finished()
            except Exception as exc:
                print(f"Vocabulary background lookup: {exc}")
                row = None
            if row is None:
                self._wake.wait(2)
                self._wake.clear()

    def _resolve(self, row: dict) -> None:
        """Persist success or a retryable failure; e.g. a missing API key preserves the event."""

        try:
            if row["prompt_version"] != vocabulary_translate.PROMPT_VERSION:
                raise RuntimeError("translation prompt changed; select this word again")
            # Pending jobs keep the originally requested model across restarts.
            translator = self._translator
            if row["model"] != translator.model or row["reasoning"] != translator.reasoning:
                translator = translator.with_policy(row["model"], row["reasoning"])
            result = translator.translate(json.loads(row["context_json"]))
        except Exception as exc:
            result = str(exc)[:2000]
        self._db.finish_word_lookup(row["id"], self._owner, result)

    def _publish_finished(self) -> None:
        """Notify even when another process translated the word; e.g. simultaneous players."""

        with self._lock:
            ids = list(self._listeners)
        for lookup_id in ids:
            row = self._db.word_lookup(lookup_id)
            if row["state"] not in {"ready", "failed"}:
                continue
            with self._lock:
                listeners = self._listeners.pop(lookup_id, [])
            for notify in listeners:
                self._notify(notify, {**row, "saved": True})
