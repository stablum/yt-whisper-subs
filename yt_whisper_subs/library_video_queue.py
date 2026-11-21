"""Coordinate user-selected video pipelines above the serial worker lane.

Example: `VideoQueueMixin._queue_video_task(...)` accepts work while busy.
"""

from __future__ import annotations

import time
from datetime import datetime

from yt_whisper_subs import i18n
from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_workers
from yt_whisper_subs import pipeline_progress as progress


class VideoQueueMixin:
    """Accept multiple video actions while preserving one-at-a-time execution.

    Example: a second Download appears as `Queued · next` during Whisper.
    """

    def _queue_video_task(
        self,
        video_id: str,
        kind: types.PipelineKind,
        *,
        force_live_check: bool = False,
    ) -> bool:
        """Append a unique video request and start it when the lane is free.

        Example: three selected videos retain their click order.
        """

        if self._quitting:
            return False
        if self._is_video_pending(video_id) or not self._service.db.queue_pipeline_jobs(
            (video_id,), kind, force_live_check=force_live_check,
        ):
            title = self._ui.catalog.model.title_for(video_id)
            self.statusBar().showMessage(i18n.tr('Already queued or processing · {title}', title=title), 4_000)
            return False
        self._continue_video_queue()
        return True

    def _is_video_pending(self, video_id: str) -> bool:
        """Check active and waiting work in constant time where possible.

        Example: duplicate Download clicks for one ID are ignored.
        """

        return video_id == self._active_video_id or video_id in self._video_queue

    def _is_video_active(self, video_id: str) -> bool:
        """Distinguish the current pipeline from entries waiting behind it.

        Example: the current row says Processing while later rows say Queued.
        """

        return video_id == self._active_video_id

    def _start_next_video(self) -> None:
        """Dispatch the oldest waiting request into the shared heavy lane.

        Example: completion of video A starts video B before video C.
        """

        if self._busy or self._quitting:
            return
        db = self._service.db
        jobs = db.pipeline_jobs()
        if any(not job.state.waiting for job in jobs):
            self._service.recover_pipeline_jobs()
            jobs = db.pipeline_jobs()
            if any(not job.state.waiting for job in jobs):
                # A previous child may still hold the pipeline writer lock.
                self._pipeline_timer.start(5_000)
                return
        self._update_video_queue(jobs)
        if not self._video_queue:
            retry_times = [job.retry_at for job in jobs if job.retry_at is not None]
            if retry_times:
                self._pipeline_timer.start(max(1, min(retry_times) - int(time.time())) * 1000)
            else:
                self._pipeline_timer.stop()
            return
        self._pipeline_timer.stop()
        job = next(iter(self._video_queue.values()))
        self._active_video_id = job.video_id
        self._update_video_queue(jobs)
        if job.state is types.PipelineJobState.INTERRUPTED:
            label = i18n.tr('Resuming interrupted pipeline…')
        elif job.kind is types.PipelineKind.CHAPTERS:
            label = i18n.tr('Creating bilingual chapters…')
        else:
            label = i18n.tr('Starting download…')
        task = library_workers.BackgroundTask(lambda report: self._service.run_pipeline_job(job, report))
        self._run_task(label, task, lambda _: self.refresh(), retry_pipeline=True)

    def _continue_video_queue(self) -> None:
        """Advance after any foreground outcome or resume quiet maintenance.

        Example: failure of one video does not strand later requests.
        """

        if self._quitting:
            return
        if self._busy:
            self._update_video_queue()
        else:
            self._start_next_video()
        self._update_actions()
        if not self._busy:
            self._schedule_metadata_backfill()

    def _release_video_slot(self, *, succeeded: bool = False) -> None:
        """Release active work and consume its one-time playback request.

        Example: Cancel removes the active ID but leaves later videos queued.
        """

        active = self._active_progress
        video_id = self._active_video_id or (active.video_id if active else None)
        self._active_video_id = None
        if video_id:
            self._finish_video_playback(video_id, succeeded=succeeded)

    def _finish_video_playback(self, video_id: str, *, succeeded: bool) -> None:
        """Consume playback intent on any outcome and launch only after success.

        Example: repeated double-clicks result in one mpv launch for this ID.
        """

        if video_id not in self._play_after_pipeline:
            return
        self._play_after_pipeline.remove(video_id)
        if succeeded and not self._quitting:
            self._play_from(None, video_id=video_id)

    def _update_video_queue(self, jobs: list[types.PipelineJob] | None = None) -> None:
        """Refresh waiting row labels and the compact status-bar summary.

        Example: after A completes, B becomes next and C becomes number two.
        """

        if jobs is None:
            jobs = self._service.db.pipeline_jobs()
        self._video_queue.clear()
        deferred = 0
        now = int(time.time())
        for job in jobs:
            if not job.state.waiting or self._is_video_active(job.video_id):
                continue
            if job.retry_at is not None and job.retry_at > now:
                deferred += 1
                when = i18n.date_text(datetime.fromtimestamp(job.retry_at).astimezone(), '%a %H:%M')
                label = i18n.tr('Retry at {when}', when=when)
                self._ui.catalog.model.set_progress(
                    progress.make(job.video_id, progress.Stage.INTERRUPTED, job.fraction, label),
                )
                continue
            self._video_queue[job.video_id] = job
        for position, job in enumerate(self._video_queue.values(), start=1):
            label = i18n.tr('Queued · next') if position == 1 else i18n.tr('Queued · #{position}', position=position)
            update = progress.make(job.video_id, progress.Stage.QUEUED, 0.0, label)
            self._ui.catalog.model.set_progress(update)
        waiting = len(self._video_queue)
        active = self._active_video_id is not None or self._active_progress is not None
        if active and waiting:
            text = i18n.tr('Pipeline queue: 1 active · {waiting} waiting', waiting=waiting)
        elif active:
            text = i18n.tr('Pipeline: active')
        elif waiting:
            text = i18n.tr('Pipeline queue: {waiting} waiting', waiting=waiting)
        else:
            text = ""
        if deferred:
            deferred_text = i18n.tr('Pipeline retries scheduled: {count}', count=deferred)
            text = f"{text} · {deferred_text}" if text else deferred_text
        self._ui.queue_status.setText(text)

    def _cancel_selected_pipeline(self) -> None:
        """Remove waiting work for the selected row, e.g. stop its scheduled retry."""

        record = self._selected_record()
        if not record:
            return
        video_id = record.meta.identity.video_id
        if self._is_video_active(video_id) or not self._service.db.cancel_waiting_pipeline(video_id):
            return
        self._finish_video_playback(video_id, succeeded=False)
        self._ui.catalog.model.set_progress(progress.make(video_id, progress.Stage.CANCELLED))
        self.refresh()
        self._continue_video_queue()
