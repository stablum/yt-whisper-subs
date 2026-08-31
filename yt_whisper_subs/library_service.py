"""Library orchestration for scans, channel checks, downloads, and playback.

Example: `LibraryService(out_dir, paths).check_all(report)`.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from yt_whisper_subs import cfg
from yt_whisper_subs import library_db
from yt_whisper_subs import library_feed
from yt_whisper_subs import library_types as types
from yt_whisper_subs import playback
from yt_whisper_subs import pipeline_progress as progress
from yt_whisper_subs import proc
from yt_whisper_subs import youtube


ReportFn = Callable[[str], None]


def _ignore_report(message: str) -> None:
    """Provide a no-op progress callback for non-GUI use and tests.

    Example: `service.scan_local(_ignore_report)`.
    """


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
        ]
        if self._cookies:
            cmd += ["--cookies-from-browser", self._cookies]
        child_kwargs = proc.child_process_kwargs()
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
        if process.wait() != 0:
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
            raise RuntimeError(f"Download failed for {record.meta.identity.title}: {detail}")
        if current.stage is not progress.Stage.READY:
            report(progress.encode(progress.make(record.meta.identity.video_id, progress.Stage.READY, 1.0)))


class LibraryService:
    """Coordinate cohesive catalog operations independently from Qt widgets.

    Example: `service.add_channel("@OpenAI", False, report)`.
    """

    def __init__(
        self,
        out_dir: Path,
        paths: dict[str, Path],
        *,
        feed: library_feed.YtDlpFeed | None = None,
        downloader: PipelineDownloader | None = None,
    ) -> None:
        self.out_dir = out_dir.resolve()
        self.paths = paths
        self.video_dir = self.out_dir / "videos"
        self.metadata_dir = self.out_dir / "metadata"
        self.db = library_db.LibraryDb(self.out_dir / "library" / "catalog.sqlite3")
        self.db.initialize()
        cookies = self.cookies_from_browser()
        self._feed = feed or library_feed.YtDlpFeed(paths["python"], cookies)
        self._downloader = downloader or PipelineDownloader(paths["python"], self.out_dir, cookies)

    def reload_clients(self) -> None:
        """Apply changed cookie settings to subsequent network operations.

        Example: `service.reload_clients()` after the settings dialog.
        """

        cookies = self.cookies_from_browser()
        self._feed = self._feed.with_cookies(cookies)
        self._downloader = self._downloader.with_cookies(cookies)

    def cookies_from_browser(self) -> str | None:
        """Read the optional yt-dlp browser cookie source.

        Example: `service.cookies_from_browser()` may return `firefox`.
        """

        return self.db.setting("cookies_from_browser", "").strip() or None

    def scan_local(self, report: ReportFn = _ignore_report) -> int:
        """Reconcile downloaded media and ingest every available info sidecar.

        Example: `service.scan_local()` makes old downloads visible immediately.
        """

        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        sidecars = self._metadata_sidecars()
        scanned: list[types.ScannedMedia] = []
        media_paths = sorted(
            path
            for path in self.video_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in cfg.MEDIA_SUFFIXES
        )
        for path in media_paths:
            video_id = youtube.video_id_from_filename(path)
            if not video_id:
                continue
            metadata_complete = video_id in sidecars
            meta = sidecars.get(video_id) or self._placeholder_meta(path, video_id)
            stat = path.stat()
            downloaded_at = int(getattr(stat, "st_birthtime", stat.st_ctime))
            local = types.LocalMedia(path.resolve(), downloaded_at, stat.st_size)
            scanned.append(types.ScannedMedia(meta, local, metadata_complete))
        self.db.reconcile_media(scanned)
        report(f"Indexed {len(scanned)} downloaded video(s)")
        return len(scanned)

    def backfill_metadata(self, report: ReportFn = _ignore_report) -> int:
        """Fetch and persist missing metadata for pre-catalog downloads once.

        Example: `service.backfill_metadata(report)` restores old titles.
        """

        video_ids = self.db.missing_metadata_ids(cfg.DEFAULT_LIBRARY_METADATA_BATCH)
        completed = 0
        for idx, video_id in enumerate(video_ids, start=1):
            report(f"Reading metadata {idx}/{len(video_ids)} · {video_id}")
            try:
                info = self._feed.video_info(f"https://www.youtube.com/watch?v={video_id}")
            except Exception as exc:
                self.db.set_download_error(video_id, f"Metadata: {exc}")
                continue
            self.db.upsert_video(info.meta)
            self._write_metadata(info)
            self.db.set_download_error(video_id, None)
            completed += 1
        return completed

    def add_channel(self, value: str, auto_download: bool, report: ReportFn = _ignore_report) -> types.Channel:
        """Subscribe and establish an initial no-auto-download history baseline.

        Example: `service.add_channel("@handle", True, report)`.
        """

        url = library_feed.normalize_channel_url(value)
        channel = self.db.add_channel(url, auto_download)
        report(f"Checking {url}")
        try:
            self._check_channel(channel, report)
        except Exception as exc:
            self.db.set_channel_error(channel.channel_id, str(exc))
            raise
        self.backfill_metadata(report)
        updated = self.db.channel(channel.channel_id)
        assert updated is not None
        return updated

    def check_all(self, report: ReportFn = _ignore_report) -> int:
        """Refresh subscriptions and auto-download only later new discoveries.

        Example: `service.check_all(report)` is called by the four-hour timer.
        """

        self.scan_local(report)
        auto_ids: list[str] = []
        channels = self.db.channels()
        for idx, channel in enumerate(channels, start=1):
            report(f"Checking channel {idx}/{len(channels)} · {channel.title}")
            try:
                auto_ids.extend(self._check_channel(channel, report))
            except Exception as exc:
                self.db.set_channel_error(channel.channel_id, str(exc))
                report(f"Could not check {channel.title}: {exc}")

        for idx, video_id in enumerate(dict.fromkeys(auto_ids), start=1):
            record = self.db.video(video_id)
            if not record or record.downloaded or self._is_live(record):
                continue
            report(f"Auto-download {idx}/{len(auto_ids)} · {record.meta.identity.title}")
            try:
                self.download(video_id, report)
            except Exception as exc:
                report(f"Auto-download failed: {exc}")

        self.backfill_metadata(report)
        self.db.set_setting("last_check_at", int(time.time()))
        return len(auto_ids)

    def download(self, video_id: str, report: ReportFn = _ignore_report) -> None:
        """Run the full subtitle pipeline for one remote catalog entry.

        Example: `service.download(video_id, report)` is the GUI button action.
        """

        record = self.db.video(video_id)
        if not record:
            raise RuntimeError(f"unknown video: {video_id}")
        if record.downloaded:
            return
        if self._is_live(record):
            raise RuntimeError("live and upcoming videos cannot be downloaded from the library yet")
        self.db.set_download_error(video_id, None)
        try:
            self._downloader.download(record, report)
            self.scan_local(report)
        except Exception as exc:
            self.db.set_download_error(video_id, str(exc))
            raise

    def play(self, video_id: str) -> None:
        """Open a downloaded record through the exact shared mpv policy.

        Example: `service.play(video_id)` handles a table double-click.
        """

        record = self.db.video(video_id)
        if not record or not record.local or not record.local.path.exists():
            raise RuntimeError("download this video before playing it")
        proc.require_command("mpv")
        srts = playback.sidecar_subtitles(record.local.path)
        playback.play_video(record.local.path, srts, playback.PlaybackPrefs.defaults())

    def _check_channel(self, channel: types.Channel, report: ReportFn) -> list[str]:
        """Persist one snapshot and select safe future auto-download candidates.

        Example: `_check_channel(channel, report)` returns new IDs after baseline.
        """

        initial_check = channel.baseline_at is None
        snapshot = self._feed.channel(channel.url)
        new_ids = self.db.store_snapshot(channel.channel_id, snapshot)
        report(f"{snapshot.title}: {len(snapshot.videos)} known, {len(new_ids)} new")
        if initial_check or not channel.auto_download:
            return []
        return new_ids

    def _metadata_sidecars(self) -> dict[str, types.VideoMeta]:
        """Load new metadata-directory and older video-directory info JSON.

        Example: `_metadata_sidecars()[video_id]` supplies a scanned title.
        """

        paths = [*self.metadata_dir.glob("*.info.json"), *self.video_dir.glob("*.info.json")]
        metas: dict[str, types.VideoMeta] = {}
        for path in paths:
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                meta = library_feed.video_meta(info)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            metas[meta.identity.video_id] = meta
        return metas

    def _write_metadata(self, info: types.VideoInfo) -> None:
        """Keep the complete extractor sidecar for GUI-backfilled videos.

        Example: `_write_metadata(info)` creates `metadata/id.info.json`.
        """

        path = self.metadata_dir / f"{info.meta.identity.video_id}.info.json"
        content = json.dumps(info.document, ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _placeholder_meta(path: Path, video_id: str) -> types.VideoMeta:
        """Derive the best offline title from a canonical or legacy filename.

        Example: `_placeholder_meta(Path("Title [id].mkv"), id)`.
        """

        title = path.stem.removesuffix(f"[{video_id}]").strip() or video_id
        ident = types.VideoIdentity(video_id, f"https://www.youtube.com/watch?v={video_id}", title)
        origin = types.VideoOrigin("Unknown channel", None, None)
        details = types.VideoDetails(None, None, "", None, None)
        return types.VideoMeta(ident, origin, details)

    @staticmethod
    def _is_live(record: types.VideoRecord) -> bool:
        """Prevent automatic pipeline work for active or scheduled streams.

        Example: `_is_live(record)` checks yt-dlp's live status.
        """

        return record.meta.details.live_status in {"is_live", "is_upcoming"}
