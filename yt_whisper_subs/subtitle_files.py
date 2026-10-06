"""Subtitle sidecar/archive maintenance and file-level transforms.

Example: `subtitle_files.SubtitlePair(sidecar, archive).hydrate("primary", args, is_english=False, force=False)`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import opts
from yt_whisper_subs import srt
from yt_whisper_subs import yield_files


def compact_srt_content(content: str, args: argparse.Namespace, *, is_english: bool = False) -> str:
    """Compact parsed SRT content and render it back to normalized SRT.

    Example: `compact_srt_content(text, args, is_english=True)`.
    """

    return srt.render_srt(srt.compact_cues(srt.parse_srt(content), args, is_english=is_english), args)


def extend_subtitle_gaps_srt_content(content: str, args: argparse.Namespace) -> tuple[str, bool]:
    """Extend cue gaps in SRT content and report whether anything changed.

    Example: `extend_subtitle_gaps_srt_content(text, args)`.
    """

    cues, changed = srt.extend_subtitle_gaps(srt.parse_srt(content), args)
    if not cues:
        return "", False
    return srt.render_srt(cues, args), changed


def uncompacted_backup_path(path: Path) -> Path:
    """Derive the reversible compaction backup path for an SRT file.

    Example: `uncompacted_backup_path(Path("x.srt"))`.
    """

    return path.with_name(f"{path.stem}.uncompact{path.suffix}")


def align_subtitle_timings_to_reference_content(
    reference_content: str,
    target_content: str,
    args: argparse.Namespace,
) -> tuple[str, bool]:
    """Copy reference cue timings onto target cue text when cue counts match.

    Example: `align_subtitle_timings_to_reference_content(primary, english, args)`.
    """

    reference_cues = srt.parse_srt(reference_content)
    target_cues = srt.parse_srt(target_content)
    if not reference_cues or len(reference_cues) != len(target_cues):
        return "", False

    changed = False
    aligned_cues: list[srt.SubtitleCue] = []
    for reference_cue, target_cue in zip(reference_cues, target_cues):
        changed = changed or (
            reference_cue.start_ms != target_cue.start_ms
            or reference_cue.end_ms != target_cue.end_ms
        )
        aligned_cues.append(
            srt.SubtitleCue(
                reference_cue.start_ms,
                reference_cue.end_ms,
                target_cue.text,
            )
        )

    if not changed:
        return "", False
    return srt.render_srt(aligned_cues, args), True


class SubtitlePair(NamedTuple):
    """Sidecar/archive subtitle yield pair with repair and transform behavior.

    Example: `SubtitlePair(sidecar, archive).ready()`.
    """

    sidecar: Path
    archive: Path

    def ready(self) -> bool:
        """Require usable, current playback and archive subtitle yields.

        Example: `pair.ready()`.
        """

        usable = srt.file_is_usable(self.sidecar) and srt.file_is_usable(self.archive)
        return usable and not yield_files.stale_path(self.archive).exists()

    def invalidate(self) -> None:
        """Persist dependency invalidation before upstream subtitles change.

        Example: an interrupted primary repair leaves English marked stale.
        """

        yield_files.atomic_write(yield_files.stale_path(self.archive), "Upstream subtitles changed\n")

    def complete(self) -> None:
        """Clear invalidation only after both replacement copies are usable.

        Example: failed translation cannot make old English ready again.
        """

        if not all(srt.file_is_usable(path) for path in self):
            raise RuntimeError("subtitle generation finished without usable sidecar and archive yields")
        yield_files.stale_path(self.archive).unlink(missing_ok=True)

    def seed_sidecar_from_archive(self) -> bool:
        """Restore an absent or damaged sidecar from a usable archive.

        Example: `pair.seed_sidecar_from_archive()`.
        """

        if srt.file_is_usable(self.sidecar) or not srt.file_is_usable(self.archive):
            return False

        self.sidecar.parent.mkdir(parents=True, exist_ok=True)
        yield_files.atomic_copy(self.archive, self.sidecar)
        return True

    def sync_archive(self) -> None:
        """Copy the sidecar subtitle into the durable archive when needed.

        Example: `pair.sync_archive()`.
        """

        if not srt.file_is_usable(self.sidecar) or self.sidecar.resolve() == self.archive.resolve():
            return

        self.archive.parent.mkdir(parents=True, exist_ok=True)
        yield_files.atomic_copy(self.sidecar, self.archive)

    def accept_sidecar_replacement(self) -> None:
        """Make a new sidecar authoritative before applying final transforms.

        Example: `pair.accept_sidecar_replacement()` precedes finalizing repaired cues.
        """

        if not srt.file_is_usable(self.sidecar):
            raise RuntimeError("replacement subtitle sidecar is unusable; previous archive was preserved")
        self.sync_archive()
        for path in self:
            uncompacted_backup_path(path).unlink(missing_ok=True)

    def hydrate(
        self,
        label: str,
        args: argparse.Namespace,
        *,
        is_english: bool,
        force: bool,
    ) -> None:
        """Repair absent or damaged subtitle copies before expensive generation.

        Example: `pair.hydrate("primary", args, is_english=False, force=False)`.
        """

        if force:
            return

        # Prefer a current canonical copy over an older uncompacted backup.
        if not any(srt.file_is_usable(path) for path in self):
            for path in self:
                restored = self._restore_from_uncompacted_backup(
                    path, args, is_english=is_english, label=f"{label} {path.name}",
                )
                if restored:
                    break
        if self.seed_sidecar_from_archive():
            print()
            print(f"Copied existing {label} subtitle archive next to the video for mpv auto-detection.")
        elif srt.file_is_usable(self.sidecar) and not srt.file_is_usable(self.archive):
            self.sync_archive()
            print()
            print(f"Copied existing {label} subtitle sidecar into the subtitle archive.")

    def ensure_compacted(
        self,
        args: argparse.Namespace,
        *,
        is_english: bool,
        label: str,
        force: bool,
    ) -> bool:
        """Compact sidecar/archive subtitles and resync their durable copy.

        Example: `pair.ensure_compacted(args, is_english=True, label="English", force=False)`.
        """

        if force or not opts.should_compact_subtitles(args, is_english=is_english):
            return False

        sidecar_changed = self._compact_file(
            self.sidecar,
            args,
            is_english=is_english,
            label=f"{label} sidecar",
        )
        archive_changed = self._compact_file(
            self.archive,
            args,
            is_english=is_english,
            label=f"{label} archive",
        )
        self._resync_existing()
        return sidecar_changed or archive_changed

    def ensure_extended_gaps(
        self,
        args: argparse.Namespace,
        *,
        label: str,
        force: bool,
    ) -> bool:
        """Extend cue gaps for a sidecar/archive pair and resync after changes.

        Example: `pair.ensure_extended_gaps(args, label="primary", force=False)`.
        """

        if force or srt.subtitle_gap_extension_ms(args) <= 0:
            return False

        sidecar_changed = self._extend_gaps_file(self.sidecar, args, label=f"{label} sidecar")
        archive_changed = self._extend_gaps_file(self.archive, args, label=f"{label} archive")
        self._resync_existing()
        return sidecar_changed or archive_changed

    def align_timings_to(
        self,
        reference_srt_path: Path,
        args: argparse.Namespace,
        *,
        label: str,
        force: bool,
    ) -> bool:
        """Keep this pair's cue timings aligned to a reference SRT.

        Example: `english_pair.align_timings_to(primary.sidecar, args, label="English", force=False)`.
        """

        if force:
            return False

        changed = self._align_file(reference_srt_path, self.sidecar, args, label=f"{label} sidecar")

        if srt.file_is_usable(self.sidecar):
            self.sync_archive()
        elif srt.file_is_usable(self.archive):
            archive_changed = self._align_file(reference_srt_path, self.archive, args, label=f"{label} archive")
            changed = changed or archive_changed
            self.seed_sidecar_from_archive()

        return changed

    def finalize(
        self,
        args: argparse.Namespace,
        *,
        is_english: bool,
        label: str,
    ) -> None:
        """Apply final compaction/archive/gap-extension rules after generation.

        Example: `pair.finalize(args, is_english=False, label="primary")`.
        """

        if opts.should_compact_subtitles(args, is_english=is_english):
            self.ensure_compacted(args, is_english=is_english, label=label, force=False)
        else:
            self.sync_archive()

        self.ensure_extended_gaps(args, label=label, force=False)

    def _resync_existing(self) -> None:
        """Mirror only usable subtitle content so repair cannot destroy archives.

        Example: called after pair transforms.
        """

        if srt.file_is_usable(self.sidecar):
            self.sync_archive()
        elif srt.file_is_usable(self.archive):
            self.seed_sidecar_from_archive()

    def _save_uncompacted_backup(self, path: Path, content: str, *, label: str) -> Path | None:
        """Save the first pre-compaction SRT backup for later restoration.

        Example: `pair._save_uncompacted_backup(path, text, label="primary")`.
        """

        backup_path = uncompacted_backup_path(path)
        if backup_path.exists():
            return None

        yield_files.atomic_write(backup_path, content)
        print(f"Saved {label} uncompacted subtitle backup: {backup_path}")
        return backup_path

    def _restore_from_uncompacted_backup(
        self,
        path: Path,
        args: argparse.Namespace,
        *,
        is_english: bool,
        label: str,
    ) -> bool:
        """Rebuild an unusable subtitle from a usable uncompacted backup.

        Example: `pair._restore_from_uncompacted_backup(path, args, is_english=False, label="primary")`.
        """

        if srt.file_is_usable(path):
            return False

        backup_path = uncompacted_backup_path(path)
        if not srt.file_is_usable(backup_path):
            return False

        backup_content = backup_path.read_text(encoding="utf-8-sig")
        if opts.should_compact_subtitles(args, is_english=is_english):
            restored_content = compact_srt_content(backup_content, args, is_english=is_english)
            action = "Rebuilt compacted"
        else:
            restored_content = backup_content.replace("\r\n", "\n").replace("\r", "\n")
            action = "Restored"

        if not restored_content:
            return False

        path.parent.mkdir(parents=True, exist_ok=True)
        yield_files.atomic_write(path, restored_content)
        print(f"{action} {label} subtitles from uncompacted backup: {path}")
        return True

    def _compact_file(self, path: Path, args: argparse.Namespace, *, is_english: bool, label: str) -> bool:
        """Compact one SRT file in place while preserving an uncompacted backup.

        Example: `pair._compact_file(path, args, is_english=True, label="English")`.
        """

        if not srt.file_is_usable(path):
            return False

        original = path.read_text(encoding="utf-8-sig")
        compacted = compact_srt_content(original, args, is_english=is_english)
        if not compacted:
            return False

        if original.replace("\r\n", "\n") == compacted:
            return False

        self._save_uncompacted_backup(path, original, label=label)
        yield_files.atomic_write(path, compacted)
        print(f"Compacted {label} subtitles: {path}")
        return True

    def _extend_gaps_file(self, path: Path, args: argparse.Namespace, *, label: str) -> bool:
        """Extend subtitle cue gaps in one file when the option is enabled.

        Example: `pair._extend_gaps_file(path, args, label="primary")`.
        """

        if not srt.file_is_usable(path):
            return False

        original = path.read_text(encoding="utf-8-sig")
        extended, changed = extend_subtitle_gaps_srt_content(original, args)
        if not changed or not extended:
            return False

        yield_files.atomic_write(path, extended)
        print(f"Extended {label} subtitle gaps: {path}")
        return True

    def _align_file(
        self,
        reference_srt_path: Path,
        target_srt_path: Path,
        args: argparse.Namespace,
        *,
        label: str,
    ) -> bool:
        """Align one target SRT file to the timing authority reference SRT.

        Example: `pair._align_file(primary, english, args, label="English")`.
        """

        if not srt.file_is_usable(reference_srt_path) or not srt.file_is_usable(target_srt_path):
            return False

        reference = reference_srt_path.read_text(encoding="utf-8-sig")
        target = target_srt_path.read_text(encoding="utf-8-sig")
        aligned, changed = align_subtitle_timings_to_reference_content(reference, target, args)
        if not changed or not aligned:
            return False

        yield_files.atomic_write(target_srt_path, aligned)
        print(f"Aligned {label} subtitle timings to primary subtitles: {target_srt_path}")
        return True
