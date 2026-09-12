"""Library orchestration for scans, channel checks, downloads, and playback.

Example: `LibraryService(out_dir, paths).check_all(report)`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import chapters
from yt_whisper_subs import cfg
from yt_whisper_subs import library_artifacts
from yt_whisper_subs import library_db
from yt_whisper_subs import library_feed
from yt_whisper_subs import library_pipeline
from yt_whisper_subs import library_types as types
from yt_whisper_subs import library_yields
from yt_whisper_subs import playback
from yt_whisper_subs import playback_progress
from yt_whisper_subs import proc
from yt_whisper_subs import task_cancel
from yt_whisper_subs import youtube


ReportFn = Callable[[str], None]


def _ignore_report(message: str) -> None:
    """Provide a no-op progress callback for non-GUI use and tests.

    Example: `service.scan_local(_ignore_report)`.
    """


class MetadataBackfillResult(NamedTuple):
    """Describe one paced lookup without exposing scheduling to the service.

    Example: `result.remaining` updates the GUI queue indicator.
    """

    attempted: bool
    completed: bool
    remaining: int


class LibraryService:
    """Coordinate cohesive catalog operations independently from Qt widgets.

    Example: `service.track_channel("@OpenAI", False)` persists immediately.
    """

    def __init__(
        self,
        out_dir: Path,
        paths: dict[str, Path],
        *,
        feed: library_feed.YtDlpFeed | None = None,
        downloader: library_pipeline.PipelineDownloader | None = None,
    ) -> None:
        self.out_dir = out_dir.resolve()
        self.paths = paths
        self.video_dir = self.out_dir / "videos"
        self.metadata_dir = self.out_dir / "metadata"
        self.db = library_db.LibraryDb(self.out_dir / "library" / "catalog.sqlite3")
        self.db.initialize()
        cookies = self.cookies_from_browser()
        self._feed = feed or library_feed.YtDlpFeed(paths["python"], cookies)
        self._downloader = downloader or library_pipeline.PipelineDownloader(
            paths["python"],
            self.out_dir,
            cookies,
        )
        self._playback = playback.PlaybackControl()
        self._artifacts = library_artifacts.ArtifactCache(self.out_dir)

    def catalog(self) -> library_artifacts.CatalogState:
        """Load one catalog snapshot with filesystem-derived state precomputed.

        Example: `service.catalog()` feeds a model without paint-time I/O.
        """

        return self._artifacts.catalog(self.db.videos())

    def pipeline_issue(self, record: types.VideoRecord) -> str | None:
        """Resolve cached local subtitle health for actions and filtering.

        Example: `service.pipeline_issue(record)` decides Play versus Repair.
        """

        return self._artifacts.issue(record)

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

    def backfill_metadata(self, report: ReportFn = _ignore_report) -> MetadataBackfillResult:
        """Attempt at most one fair, cooldown-aware metadata lookup.

        Example: a GUI timer calls `service.backfill_metadata(report)` once.
        """

        retry_seconds = round(cfg.DEFAULT_LIBRARY_METADATA_RETRY_HOURS * 3600)
        retry_before = int(time.time()) - retry_seconds
        video_ids = self.db.metadata_backfill_ids(1, retry_before)
        remaining = self.db.metadata_backlog_count()
        if not video_ids:
            return MetadataBackfillResult(False, False, remaining)

        video_id = video_ids[0]
        self.db.mark_metadata_attempt(video_id)
        report(f"Reading queued metadata · {video_id}")
        try:
            info = self._feed.video_info(f"https://www.youtube.com/watch?v={video_id}")
        except task_cancel.CancelledError:
            raise
        except Exception as exc:
            hours = cfg.DEFAULT_LIBRARY_METADATA_RETRY_HOURS
            report(f"Metadata deferred for {hours:g} hours · {video_id} · {exc}")
            return MetadataBackfillResult(True, False, remaining)

        self.db.upsert_video(info.meta)
        policy = self._channel_policy()
        record = self.db.video(video_id)
        expired = (
            policy.published_after is not None
            and info.meta.origin.published_at is not None
            and info.meta.origin.published_at < policy.published_after
            and record is not None
            and not record.downloaded
        )
        if expired:
            removed = self.db.prune_remote_before(policy.published_after)
            remaining = self.db.metadata_backlog_count()
            report(f"Metadata expired by retention · {video_id} · {removed:,} removed")
            return MetadataBackfillResult(True, True, remaining)
        self._write_metadata(info)
        remaining = self.db.metadata_backlog_count()
        report(f"Metadata saved · {info.meta.identity.title} · {remaining:,} queued")
        return MetadataBackfillResult(True, True, remaining)

    def track_channel(self, value: str, auto_download: bool) -> types.Channel:
        """Persist a recognizable subscription placeholder without network work.

        Example: `track_channel("@ruis", False)` can refresh the sidebar immediately.
        """

        url = library_feed.normalize_channel_url(value)
        title = library_feed.channel_placeholder(url)
        return self.db.add_channel(url, title, auto_download)

    def initialize_channel(
        self,
        channel_id: int,
        report: ReportFn = _ignore_report,
    ) -> types.Channel:
        """Fetch one new subscription's title and safe initial history baseline.

        Example: `initialize_channel(channel.channel_id, report)` runs in the shared serial lane.
        """

        channel = self.db.channel(channel_id)
        if not channel:
            raise RuntimeError("tracked channel no longer exists")
        report(f"Checking {channel.url}")
        try:
            self._check_channel(channel, report)
        except task_cancel.CancelledError:
            raise
        except Exception as exc:
            self.db.set_channel_error(channel.channel_id, str(exc))
            raise
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
            except task_cancel.CancelledError:
                raise
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
            except task_cancel.CancelledError:
                raise
            except Exception as exc:
                report(f"Auto-download failed: {exc}")

        self.db.set_setting("last_check_at", int(time.time()))
        return len(auto_ids)

    def download(self, video_id: str, report: ReportFn = _ignore_report) -> None:
        """Run the full subtitle pipeline for one remote catalog entry.

        Example: `service.download(video_id, report)` is the GUI button action.
        """

        record = self.db.video(video_id)
        if not record:
            raise RuntimeError(f"unknown video: {video_id}")
        if self._is_live(record) and not record.downloaded:
            raise RuntimeError("live and upcoming videos cannot be downloaded from the library yet")
        self.db.set_download_error(video_id, None)
        try:
            tracker = library_pipeline.PipelineJobTracker(
                self.db,
                video_id,
                types.PipelineKind.DOWNLOAD,
                report,
            )
            with tracker as tracked_report:
                self._downloader.download(record, tracked_report)
                self.scan_local(tracked_report)
        except task_cancel.CancelledError:
            self.scan_local()
            raise
        except Exception as exc:
            self.db.set_download_error(video_id, str(exc))
            raise

    def video_yields(self, video_id: str) -> library_yields.VideoYields:
        """Build the exact deletion manifest for one known catalog video.

        Example: the GUI previews `service.video_yields(id).paths`.
        """

        if not self.db.video(video_id):
            raise RuntimeError(f"unknown video: {video_id}")
        return library_yields.VideoYields.inspect(self.out_dir, video_id)

    def remove_yields(
        self,
        manifest: library_yields.VideoYields,
        report: ReportFn = _ignore_report,
    ) -> int:
        """Remove one pre-inspected manifest and reconcile its catalog state.

        Example: the exact paths confirmed in the GUI are the paths removed.
        """

        if manifest.root != self.out_dir or not self.db.video(manifest.video_id):
            raise RuntimeError("refusing a yield manifest outside this library")
        removed = manifest.remove(report)
        self.scan_local(report)
        self.db.set_download_error(manifest.video_id, None)
        return removed

    def recover_pipeline_jobs(self) -> list[types.PipelineJob]:
        """Load work left live by an unclean prior application exit.

        Example: the window re-queues the single pipeline active at a crash.
        """

        return self.db.recover_pipeline_jobs()

    def resume_pipeline_job(
        self,
        job: types.PipelineJob,
        report: ReportFn = _ignore_report,
    ) -> None:
        """Dispatch a recovered job through the same idempotent pipeline path.

        Example: completed media is reused while missing subtitle stages rerun.
        """

        if job.kind is types.PipelineKind.CHAPTERS:
            self.generate_chapters(job.video_id, report)
            return
        self.download(job.video_id, report)

    def set_pipeline_paused(self, video_id: str, paused: bool) -> None:
        """Mirror live suspension state into the durable recovery record.

        Example: a crash while paused remains recoverable at next startup.
        """

        self.db.set_pipeline_job_paused(video_id, paused)

    def chapter_set(self, video_id: str) -> chapters.ChapterSet | None:
        """Load the selected video's durable chapter plan when available.

        Example: `service.chapter_set(video_id)` feeds the inspector pane.
        """

        return self._artifacts.chapter_set(video_id)

    def generate_chapters(
        self,
        video_id: str,
        report: ReportFn = _ignore_report,
    ) -> chapters.ChapterSet:
        """Regenerate chapters for an existing downloaded video.

        Example: `service.generate_chapters(video_id, report)` backs the GUI action.
        """

        record = self.db.video(video_id)
        if not record or not record.local or not record.local.path.exists():
            raise RuntimeError("download this video before generating chapters")
        tracker = library_pipeline.PipelineJobTracker(
            self.db,
            video_id,
            types.PipelineKind.CHAPTERS,
            report,
        )
        with tracker as tracked_report:
            self._downloader.generate_chapters(record, tracked_report)
        chapter_set = self.chapter_set(video_id)
        if not chapter_set:
            raise RuntimeError("chapter pipeline completed without a readable chapter plan")
        return chapter_set

    def apply_retention(self) -> int:
        """Prune dated remote-only rows immediately after settings change.

        Example: `service.apply_retention()` removes known pre-April entries.
        """

        return self.db.prune_remote_before(self._channel_policy().published_after)

    def play(
        self,
        video_id: str,
        report: ReportFn = _ignore_report,
        *,
        start_seconds: float | None = None,
    ) -> None:
        """Open a downloaded record through the exact shared mpv policy.

        Example: `service.play(video_id, report, start_seconds=90)` jumps to a chapter.
        """

        record = self.db.video(video_id)
        if not record or not record.local or not record.local.path.exists():
            raise RuntimeError("download this video before playing it")
        proc.require_command("mpv")
        srts = playback.sidecar_subtitles(record.local.path)

        def save(update: playback_progress.Update) -> None:
            """Persist and publish one paced observation from mpv's IPC thread.

            Example: `save(update)` refreshes both SQLite and the live table bar.
            """

            self.db.record_playback(update)
            report(playback_progress.encode(update))

        observer = playback_progress.Observer(video_id, save)
        chapter_path = chapters.ChapterFiles.for_video(self.out_dir, video_id).ensure_mpv()
        session = playback.PlaybackSession(
            observer=observer,
            chapter_path=chapter_path,
            start_seconds=start_seconds,
            control=self._playback,
        )
        playback.play_video(
            record.local.path,
            srts,
            playback.PlaybackPrefs.defaults(),
            session,
        )

    def seek(self, video_id: str, seconds: float) -> bool:
        """Seek the matching library-owned mpv process when it is active.

        Example: `service.seek(video_id, 90)` avoids opening a second player.
        """

        return self._playback.seek(video_id, seconds)

    def _check_channel(self, channel: types.Channel, report: ReportFn) -> list[str]:
        """Persist one snapshot and select safe future auto-download candidates.

        Example: `_check_channel(channel, report)` returns new IDs after baseline.
        """

        initial_check = channel.baseline_at is None
        known_ids = self.db.channel_video_ids(channel.channel_id)
        snapshot = self._feed.channel(channel.url, self._channel_policy(), known_ids)
        result = self.db.store_snapshot(channel.channel_id, snapshot)
        if not snapshot.complete:
            warning = "Partial refresh: Streams could not be checked; history preserved"
            self.db.set_channel_error(channel.channel_id, warning)
            report(f"{snapshot.title}: {warning}")
        entry_word = "entry" if result.pruned == 1 else "entries"
        pruned = f", {result.pruned} old remote {entry_word} removed"
        report(
            f"{snapshot.title}: {len(snapshot.videos)} retained, "
            f"{len(result.new_ids)} new{pruned if result.pruned else ''}"
        )
        if initial_check or not channel.auto_download:
            return []
        return result.new_ids

    def _channel_policy(self) -> library_feed.ChannelScanPolicy:
        """Build bounded discovery policy from validated persisted settings.

        Example: the default scans 50 recent entries from each channel tab.
        """

        raw_limit = self.db.setting(
            "channel_recent_limit",
            str(cfg.DEFAULT_LIBRARY_CHANNEL_RECENT_LIMIT),
        )
        try:
            requested_limit = int(raw_limit)
        except ValueError:
            requested_limit = cfg.DEFAULT_LIBRARY_CHANNEL_RECENT_LIMIT
        limit = min(
            cfg.MAX_LIBRARY_CHANNEL_RECENT_LIMIT,
            max(1, requested_limit),
        )
        published_after = None
        cutoff = self.db.setting("channel_published_after", "").strip()
        try:
            if cutoff:
                value = datetime.strptime(cutoff, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                published_after = int(value.timestamp())
        except ValueError:
            pass
        return library_feed.ChannelScanPolicy(
            limit,
            cfg.MAX_LIBRARY_CHANNEL_RECENT_LIMIT,
            published_after,
        )

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
        path.parent.mkdir(parents=True, exist_ok=True)
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
