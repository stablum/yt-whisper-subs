"""Launch, control, and checkpoint GUI-owned subtitle pipelines.

Example: `PipelineDownloader(python, root).download(record, report)`.
"""

from __future__ import annotations

import subprocess
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from types import TracebackType

from yt_whisper_subs import cfg
from yt_whisper_subs import library_db
from yt_whisper_subs import library_types as types
from yt_whisper_subs import pipeline_progress as progress
from yt_whisper_subs import proc
from yt_whisper_subs import task_cancel


ReportFn = Callable[[str], None]


class PipelineDownloader:
    """Run the existing CLI pipeline as the library's download strategy.

    Example: `downloader.download(record, report)` preserves CLI behavior.
    """

    def __init__(self, python_exe: Path, out_dir: Path, cookies: str | None = None) -> None:
        self._python_exe = python_exe
        self._out_dir = out_dir
        self._cookies = cookies

    def with_cookies(self, cookies: str | None) -> PipelineDownloader:
        """Return a downloader configured for private or age-gated feeds.

        Example: `downloader.with_cookies("firefox")`.
        """

        return type(self)(self._python_exe, self._out_dir, cookies or None)

    def download(self, record: types.VideoRecord, report: ReportFn) -> None:
        """Generate all normal durable yields without opening mpv afterward.

        Example: `download(record, status.emit)` runs the shared CLI.
        """

        cmd = [
            str(self._python_exe),
            str(cfg.PROJECT_DIR / "yt_whisper_subs.py"),
            "--url",
            record.meta.identity.url,
            "--out-dir",
            str(self._out_dir),
            "--no-play",
            "--chapters",
        ]
        if self._cookies:
            cmd += ["--cookies-from-browser", self._cookies]
        self._run(record, cmd, "Download", report)

    def generate_chapters(self, record: types.VideoRecord, report: ReportFn) -> None:
        """Regenerate chapters through the CLI while reusing local durable yields.

        Example: `generate_chapters(record, report)` avoids Whisper and yt-dlp.
        """

        if not record.local:
            raise RuntimeError("download this video before generating chapters")
        cmd = [
            str(self._python_exe),
            str(cfg.PROJECT_DIR / "yt_whisper_subs.py"),
            "--video-file",
            str(record.local.path),
            "--out-dir",
            str(self._out_dir),
            "--no-play",
            "--chapters",
            "--force-chapters",
        ]
        self._run(record, cmd, "Chapter generation", report)

    def _run(
        self,
        record: types.VideoRecord,
        cmd: list[str],
        operation: str,
        report: ReportFn,
    ) -> None:
        """Stream one hidden CLI operation into trace and structured progress.

        Example: `_run(record, cmd, "Download", report)` keeps one protocol.
        """

        child_kwargs = proc.isolated_process_kwargs()
        child_env = dict(child_kwargs["env"])
        child_env[progress.ENV_VIDEO_ID] = record.meta.identity.video_id
        child_kwargs["env"] = child_env
        current = progress.make(record.meta.identity.video_id, progress.Stage.QUEUED)
        report(progress.encode(current))
        process = subprocess.Popen(
            cmd,
            **child_kwargs,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        output_tail: deque[str] = deque(maxlen=30)
        control = task_cancel.current()
        stop = lambda: proc.request_process_tree_termination(process)
        tree = proc.ProcessTreeControl(process.pid)
        context = (
            control.controllable(stop, tree.suspend, tree.resume)
            if control
            else nullcontext()
        )
        with context:
            for message in proc.iter_output_records(process.stdout):
                if update := progress.parse(message):
                    current = update
                    report(message)
                    continue
                if update := progress.derive_tool_update(message, current):
                    current = update
                    report(progress.encode(update))
                output_tail.append(message)
                report(message)
            returncode = process.wait()
            if control:
                control.checkpoint()
        if returncode != 0:
            error_lines = [line for line in output_tail if line.casefold().startswith("error:")]
            detail = (
                error_lines[-1].removeprefix("error: ")
                if error_lines
                else "subtitle pipeline failed"
            )
            if current.stage is not progress.Stage.FAILED:
                current = progress.make(
                    record.meta.identity.video_id,
                    progress.Stage.FAILED,
                    progress.overall_fraction(current),
                    f"Failed · {detail}",
                )
                report(progress.encode(current))
            raise RuntimeError(f"{operation} failed for {record.meta.identity.title}: {detail}")
        if current.stage is not progress.Stage.READY:
            ready = progress.make(record.meta.identity.video_id, progress.Stage.READY, 1.0)
            report(progress.encode(ready))


class PipelineJobTracker:
    """Checkpoint one GUI pipeline while leaving clean exits non-recoverable.

    Example: `with PipelineJobTracker(db, id, kind, report) as tracked: ...`.
    """

    def __init__(
        self,
        db: library_db.LibraryDb,
        video_id: str,
        kind: types.PipelineKind,
        report: ReportFn,
    ) -> None:
        self._db = db
        self._video_id = video_id
        self._kind = kind
        self._report = report
        self._checkpoint: tuple[progress.Stage, int] | None = None

    def __enter__(self) -> ReportFn:
        """Durably mark work running before its first child process starts.

        Example: the returned reporter persists meaningful phase movement.
        """

        self._db.begin_pipeline_job(self._video_id, self._kind)
        return self.report

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        """Clear handled outcomes while retaining markers after hard interruption.

        Example: a normal error is not confused with a system-crash recovery.
        """

        if exc_type is None or issubclass(exc_type, Exception):
            self._db.finish_pipeline_job(self._video_id)
        return False

    def report(self, message: str) -> None:
        """Persist stage changes and percentage points before forwarding output.

        Example: many character-level trace lines do not cause SQLite writes.
        """

        update = progress.parse(message)
        if update:
            overall_percent = round(progress.overall_fraction(update) * 100)
            checkpoint = (update.stage, overall_percent)
            if checkpoint != self._checkpoint:
                self._db.update_pipeline_job(update)
                self._checkpoint = checkpoint
        self._report(message)
