"""Subtitle sidecar/archive maintenance and file-level transforms.

Example: `subtitle_files.hydrate_subtitle_pair("primary", sidecar, archive, args, is_english=False, force=False)`.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from yt_whisper_subs import opts
from yt_whisper_subs import srt


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


def save_uncompacted_backup(path: Path, content: str, *, label: str) -> Path | None:
    """Save the first pre-compaction SRT backup for later restoration.

    Example: `save_uncompacted_backup(path, content, label="primary")`.
    """

    backup_path = uncompacted_backup_path(path)
    if backup_path.exists():
        return None

    backup_path.write_text(content, encoding="utf-8", newline="\n")
    print(f"Saved {label} uncompacted subtitle backup: {backup_path}")
    return backup_path


def restore_subtitle_from_uncompacted_backup(
    path: Path,
    args: argparse.Namespace,
    *,
    is_english: bool,
    label: str,
) -> bool:
    """Rebuild a missing subtitle from its uncompacted backup when available.

    Example: `restore_subtitle_from_uncompacted_backup(path, args, is_english=False, label="primary")`.
    """

    if path.exists():
        return False

    backup_path = uncompacted_backup_path(path)
    if not backup_path.exists():
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
    path.write_text(restored_content, encoding="utf-8", newline="\n")
    print(f"{action} {label} subtitles from uncompacted backup: {path}")
    return True


def compact_srt_file(path: Path, args: argparse.Namespace, *, is_english: bool, label: str) -> bool:
    """Compact one SRT file in place while preserving an uncompacted backup.

    Example: `compact_srt_file(path, args, is_english=True, label="English")`.
    """

    if not path.exists():
        return False

    original = path.read_text(encoding="utf-8-sig")
    compacted = compact_srt_content(original, args, is_english=is_english)
    if not compacted:
        return False

    if original.replace("\r\n", "\n") == compacted:
        return False

    save_uncompacted_backup(path, original, label=label)
    path.write_text(compacted, encoding="utf-8", newline="\n")
    print(f"Compacted {label} subtitles: {path}")
    return True


def extend_subtitle_gaps_file(path: Path, args: argparse.Namespace, *, label: str) -> bool:
    """Extend subtitle cue gaps in one file when the option is enabled.

    Example: `extend_subtitle_gaps_file(path, args, label="primary")`.
    """

    if not path.exists():
        return False

    original = path.read_text(encoding="utf-8-sig")
    extended, changed = extend_subtitle_gaps_srt_content(original, args)
    if not changed or not extended:
        return False

    path.write_text(extended, encoding="utf-8", newline="\n")
    print(f"Extended {label} subtitle gaps: {path}")
    return True


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


def align_subtitle_timings_to_reference_file(
    reference_srt_path: Path,
    target_srt_path: Path,
    args: argparse.Namespace,
    *,
    label: str,
) -> bool:
    """Align one target SRT file to the timing authority reference SRT.

    Example: `align_subtitle_timings_to_reference_file(primary, english, args, label="English")`.
    """

    if not reference_srt_path.exists() or not target_srt_path.exists():
        return False

    reference = reference_srt_path.read_text(encoding="utf-8-sig")
    target = target_srt_path.read_text(encoding="utf-8-sig")
    aligned, changed = align_subtitle_timings_to_reference_content(reference, target, args)
    if not changed or not aligned:
        return False

    target_srt_path.write_text(aligned, encoding="utf-8", newline="\n")
    print(f"Aligned {label} subtitle timings to primary subtitles: {target_srt_path}")
    return True


def ensure_compacted_subtitle_pair(
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    is_english: bool,
    label: str,
    force: bool,
) -> bool:
    """Compact sidecar/archive subtitle pairs and resync their durable copy.

    Example: `ensure_compacted_subtitle_pair(sidecar, archive, args, is_english=False, label="primary", force=False)`.
    """

    if force or not opts.should_compact_subtitles(args, is_english=is_english):
        return False

    if sidecar_srt_path.exists():
        sidecar_changed = compact_srt_file(
            sidecar_srt_path,
            args,
            is_english=is_english,
            label=f"{label} sidecar",
        )
    else:
        sidecar_changed = False

    if archive_srt_path.exists():
        archive_changed = compact_srt_file(
            archive_srt_path,
            args,
            is_english=is_english,
            label=f"{label} archive",
        )
    else:
        archive_changed = False

    changed = sidecar_changed or archive_changed

    if sidecar_srt_path.exists():
        sync_subtitle_archive(sidecar_srt_path, archive_srt_path)
    elif archive_srt_path.exists():
        seed_sidecar_from_archive(sidecar_srt_path, archive_srt_path)

    return changed


def ensure_matching_subtitle_timing_pair(
    reference_srt_path: Path,
    target_sidecar_srt_path: Path,
    target_archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    label: str,
    force: bool,
) -> bool:
    """Keep English sidecar/archive timings aligned to the primary SRT.

    Example: `ensure_matching_subtitle_timing_pair(primary, en_sidecar, en_archive, args, label="English", force=False)`.
    """

    if force:
        return False

    changed = align_subtitle_timings_to_reference_file(
        reference_srt_path,
        target_sidecar_srt_path,
        args,
        label=f"{label} sidecar",
    )

    if target_sidecar_srt_path.exists():
        sync_subtitle_archive(target_sidecar_srt_path, target_archive_srt_path)
    elif target_archive_srt_path.exists():
        archive_changed = align_subtitle_timings_to_reference_file(
            reference_srt_path,
            target_archive_srt_path,
            args,
            label=f"{label} archive",
        )
        changed = changed or archive_changed
        seed_sidecar_from_archive(target_sidecar_srt_path, target_archive_srt_path)

    return changed


def ensure_extended_subtitle_gap_pair(
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    label: str,
    force: bool,
) -> bool:
    """Extend cue gaps for a sidecar/archive pair and resync after changes.

    Example: `ensure_extended_subtitle_gap_pair(sidecar, archive, args, label="primary", force=False)`.
    """

    if force or srt.subtitle_gap_extension_ms(args) <= 0:
        return False

    sidecar_changed = extend_subtitle_gaps_file(
        sidecar_srt_path,
        args,
        label=f"{label} sidecar",
    )
    archive_changed = extend_subtitle_gaps_file(
        archive_srt_path,
        args,
        label=f"{label} archive",
    )
    changed = sidecar_changed or archive_changed

    if sidecar_srt_path.exists():
        sync_subtitle_archive(sidecar_srt_path, archive_srt_path)
    elif archive_srt_path.exists():
        seed_sidecar_from_archive(sidecar_srt_path, archive_srt_path)

    return changed


def finalize_subtitle_pair(
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    is_english: bool,
    label: str,
) -> None:
    """Apply final compaction/archive/gap-extension rules after generation.

    Example: `finalize_subtitle_pair(sidecar, archive, args, is_english=False, label="primary")`.
    """

    if opts.should_compact_subtitles(args, is_english=is_english):
        ensure_compacted_subtitle_pair(
            sidecar_srt_path,
            archive_srt_path,
            args,
            is_english=is_english,
            label=label,
            force=False,
        )
    else:
        sync_subtitle_archive(sidecar_srt_path, archive_srt_path)

    ensure_extended_subtitle_gap_pair(
        sidecar_srt_path,
        archive_srt_path,
        args,
        label=label,
        force=False,
    )


def seed_sidecar_from_archive(sidecar_srt_path: Path, archive_srt_path: Path) -> bool:
    """Copy an archive subtitle beside the video when the sidecar is missing.

    Example: `seed_sidecar_from_archive(sidecar, archive)`.
    """

    if sidecar_srt_path.exists() or not archive_srt_path.exists():
        return False

    sidecar_srt_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_srt_path, sidecar_srt_path)
    return True


def sync_subtitle_archive(sidecar_srt_path: Path, archive_srt_path: Path) -> None:
    """Copy a sidecar subtitle into the durable archive when needed.

    Example: `sync_subtitle_archive(sidecar, archive)`.
    """

    if not sidecar_srt_path.exists() or sidecar_srt_path.resolve() == archive_srt_path.resolve():
        return

    archive_srt_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sidecar_srt_path, archive_srt_path)


def hydrate_subtitle_pair(
    label: str,
    sidecar_srt_path: Path,
    archive_srt_path: Path,
    args: argparse.Namespace,
    *,
    is_english: bool,
    force: bool,
) -> None:
    """Repair missing sidecar/archive subtitles before expensive generation.

    Example: `hydrate_subtitle_pair("primary", sidecar, archive, args, is_english=False, force=False)`.
    """

    if force:
        return

    restore_subtitle_from_uncompacted_backup(
        sidecar_srt_path,
        args,
        is_english=is_english,
        label=f"{label} sidecar",
    )
    restore_subtitle_from_uncompacted_backup(
        archive_srt_path,
        args,
        is_english=is_english,
        label=f"{label} archive",
    )

    if seed_sidecar_from_archive(sidecar_srt_path, archive_srt_path):
        print()
        print(f"Copied existing {label} subtitle archive next to the video for mpv auto-detection.")
    elif sidecar_srt_path.exists() and not archive_srt_path.exists():
        sync_subtitle_archive(sidecar_srt_path, archive_srt_path)
        print()
        print(f"Copied existing {label} subtitle sidecar into the subtitle archive.")


def subtitle_pair_ready(sidecar_srt_path: Path, archive_srt_path: Path) -> bool:
    """Check whether both playback and archive subtitle yields exist.

    Example: `subtitle_pair_ready(sidecar, archive)`.
    """

    return sidecar_srt_path.exists() and archive_srt_path.exists()
