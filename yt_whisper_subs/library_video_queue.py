"""Coordinate user-selected video pipelines above the serial worker lane.

Example: `VideoQueueMixin._queue_video_task(...)` accepts work while busy.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from typing import NamedTuple

from yt_whisper_subs import pipeline_progress as progress


class VideoWork(NamedTuple):
    """Retain one in-memory pipeline request until it reaches the worker.

    Example: insertion order supplies deterministic FIFO execution.
    """

    video_id: str
    label: str
    fn: Callable[[Callable[[str], None]], Any]
    finished: Callable[[object], None] | None


class VideoQueueMixin:
    """Accept multiple video actions while preserving one-at-a-time execution.

    Example: a second Download appears as `Queued · next` during Whisper.
    """

    def _queue_video_task(
        self,
        video_id: str,
        label: str,
        fn: Callable[[Callable[[str], None]], Any],
        finished: Callable[[object], None] | None = None,
    ) -> bool:
        """Append a unique video request and start it when the lane is free.

        Example: three selected videos retain their click order.
        """

        if self._is_video_pending(video_id):
            title = self._ui.catalog.model.title_for(video_id)
            self.statusBar().showMessage(f"Already queued or processing · {title}", 4_000)
            return False
        work = VideoWork(video_id, label, fn, finished)
        self._video_queue[video_id] = work
        self._update_video_queue()
        if self._busy:
            position = len(self._video_queue)
            title = self._ui.catalog.model.title_for(video_id)
            self._ui.trace.append_message(f"○ Pipeline queued · #{position} · {title}")
            self._update_actions()
        else:
            self._start_next_video()
        return True

    def _is_video_pending(self, video_id: str) -> bool:
        """Check active and waiting work in constant time where possible.

        Example: duplicate Download clicks for one ID are ignored.
        """

        if video_id == self._active_video_id or video_id in self._video_queue:
            return True
        active = self._active_progress
        return bool(active and active.video_id == video_id)

    def _is_video_active(self, video_id: str) -> bool:
        """Distinguish the current pipeline from entries waiting behind it.

        Example: the current row says Processing while later rows say Queued.
        """

        if video_id == self._active_video_id:
            return True
        active = self._active_progress
        return bool(active and active.video_id == video_id)

    def _start_next_video(self) -> None:
        """Dispatch the oldest waiting request into the shared heavy lane.

        Example: completion of video A starts video B before video C.
        """

        if self._busy or not self._video_queue:
            return
        video_id = next(iter(self._video_queue))
        work = self._video_queue.pop(video_id)
        self._active_video_id = video_id
        self._update_video_queue()
        self._run_task(work.label, work.fn, work.finished)

    def _continue_video_queue(self) -> None:
        """Advance after any foreground outcome or resume quiet maintenance.

        Example: failure of one video does not strand later requests.
        """

        self._update_video_queue()
        if self._video_queue:
            self._start_next_video()
            return
        self._update_actions()
        self._schedule_metadata_backfill()

    def _release_video_slot(self) -> None:
        """Forget only the current request while retaining the waiting FIFO.

        Example: Cancel removes the active ID but leaves later videos queued.
        """

        self._active_video_id = None

    def _update_video_queue(self) -> None:
        """Refresh waiting row labels and the compact status-bar summary.

        Example: after A completes, B becomes next and C becomes number two.
        """

        for position, work in enumerate(self._video_queue.values(), start=1):
            label = "Queued · next" if position == 1 else f"Queued · #{position}"
            update = progress.make(work.video_id, progress.Stage.QUEUED, 0.0, label)
            self._ui.catalog.model.set_progress(update)
        waiting = len(self._video_queue)
        active = self._active_video_id is not None or self._active_progress is not None
        if active and waiting:
            text = f"Pipeline queue: 1 active · {waiting} waiting"
        elif active:
            text = "Pipeline: active"
        elif waiting:
            text = f"Pipeline queue: {waiting} waiting"
        else:
            text = ""
        self._ui.queue_status.setText(text)
