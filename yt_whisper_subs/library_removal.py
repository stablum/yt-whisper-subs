"""Coordinate confirmed yield removal independently from catalog discovery.

Example: `LibraryService.yield_selection(ids, report)` previews a filtered cleanup.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from yt_whisper_subs import library_yields
from yt_whisper_subs import output_lock


def _ignore_report(message: str) -> None:
    """Allow removal outside GUI workers; e.g. temporary-library tests."""


class RemovalServiceMixin:
    """Share ownership, history, and reconciliation rules for all removal actions.

    Example: `LibraryService` combines this with its channel operations.
    """

    def video_yields(self, video_id: str) -> library_yields.VideoYields:
        """Inspect one known catalog video's exact files; e.g. Shift+Delete preview."""

        if not self.db.video(video_id):
            raise RuntimeError(f"unknown video: {video_id}")
        return library_yields.VideoYields.inspect(self.out_dir, video_id)

    def yield_selection(
        self, video_ids: Iterable[str], report: Callable[[str], None],
        *, excluded: frozenset[str] = frozenset(),
    ) -> library_yields.YieldSelection:
        """Inventory only the supplied filter snapshot, excluding pending work and players.

        Example: IDs from the Watched proxy rows clean up just those matches.
        """

        records = {record.meta.identity.video_id: record for record in self.db.videos()}
        titles: dict[str, str] = {}
        skipped: list[str] = []
        for video_id in dict.fromkeys(video_ids):
            report(f"Inspecting yields · {video_id}")
            if record := records.get(video_id):
                title = record.meta.identity.title
                if self._removal_protected(video_id, excluded):
                    skipped.append(title)
                else:
                    titles[video_id] = title
        manifests = library_yields.VideoYields.inspect_many(self.out_dir, titles)
        items = tuple(library_yields.YieldItem(titles[m.video_id], m) for m in manifests if m.paths)
        size_bytes = 0
        for item in items:
            for path in item.yields.paths:
                report(f"Inspecting file · {path}")
                try:
                    size_bytes += path.lstat().st_size
                except FileNotFoundError:
                    pass
        return library_yields.YieldSelection(items, tuple(skipped), size_bytes)

    def _removal_protected(self, video_id: str, excluded: frozenset[str]) -> bool:
        """Protect queued, recoverable, and playing videos; e.g. a paused repair."""

        return bool(
            video_id in excluded or self.db.pipeline_job(video_id)
            or video_id in self._playback.active_video_ids()
        )

    def remove_yields(
        self, manifest: library_yields.VideoYields,
        report: Callable[[str], None] = _ignore_report,
    ) -> int:
        """Remove a single confirmed manifest; e.g. explicitly discard an interrupted job."""

        return self._remove_manifests((manifest,), report).files

    def remove_yield_selection(
        self, selection: library_yields.YieldSelection, report: Callable[[str], None],
        *, excluded: frozenset[str] = frozenset(),
    ) -> library_yields.RemovalResult:
        """Remove a confirmed batch while rechecking protection under the pipeline lock.

        Example: a video opened in mpv after preview is skipped at execution.
        """

        manifests = tuple(item.yields for item in selection.items)
        result = self._remove_manifests(manifests, report, excluded=excluded)
        return result._replace(skipped=result.skipped + len(selection.skipped))

    def _remove_manifests(
        self, manifests: tuple[library_yields.VideoYields, ...],
        report: Callable[[str], None], *, excluded: frozenset[str] | None = None,
    ) -> library_yields.RemovalResult:
        """Validate the whole batch before deleting and reconcile even after cancellation.

        Example: a stale manifest leaves every unapproved file untouched.
        """

        with output_lock.OutputLock(self.out_dir, "pipeline"):
            for manifest in manifests:
                if manifest.root != self.out_dir or not self.db.video(manifest.video_id):
                    raise RuntimeError("refusing a yield manifest outside this library")
                manifest.validate()
            current = library_yields.VideoYields.inspect_many(
                self.out_dir, (manifest.video_id for manifest in manifests),
            )
            for approved, fresh in zip(manifests, current, strict=True):
                if not set(fresh.paths).issubset(approved.paths):
                    raise RuntimeError("yield files remain outside the confirmed manifest; inspect again")
            completed: list[str] = []
            removed = skipped = 0
            try:
                for manifest in current:
                    video_id = manifest.video_id
                    if excluded is not None and self._removal_protected(video_id, excluded):
                        report(f"Skipping active or pending video · {video_id}")
                        skipped += 1
                        continue
                    report(f"Removing video · {video_id}")
                    removed += manifest.remove(report)
                    completed.append(video_id)
            finally:
                # Reconciliation must run without the cancelled report callback.
                remaining = library_yields.VideoYields.inspect_many(self.out_dir, completed)
                for manifest in remaining:
                    if not manifest.paths:
                        self.db.record_yield_removal(manifest.video_id)
                self.scan_local()
            if any(manifest.paths for manifest in remaining):
                raise RuntimeError("yield files remain; removal was not recorded as complete")
            return library_yields.RemovalResult(len(completed), removed, skipped)
