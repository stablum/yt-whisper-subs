"""Durable bilingual chapter models plus mpv-compatible sidecar rendering.

Example: `ChapterFiles.for_video(out_dir, video_id).load()` reads a plan.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import NamedTuple


SCHEMA_VERSION = 1


class Chapter(NamedTuple):
    """Represent one exact chapter boundary with two display titles.

    Example: `Chapter(60_000, "Onderwerp", "Topic")`.
    """

    start_ms: int
    primary_title: str
    english_title: str


class ChapterSet(NamedTuple):
    """Describe one generated chapter plan and the transcript it belongs to.

    Example: `chapter_set.chapters[0].start_ms` is the opening boundary.
    """

    video_id: str
    primary_language: str
    model: str
    generated_at: int
    duration_ms: int
    chapters: list[Chapter]


class ChapterFiles(NamedTuple):
    """Own the canonical JSON archive and derived mpv chapter sidecar.

    Example: `ChapterFiles.for_video(out_dir, "abc").write(chapter_set)`.
    """

    archive: Path
    mpv: Path

    @classmethod
    def for_video(cls, out_dir: Path, video_id: str) -> ChapterFiles:
        """Derive both chapter paths from one output root and video ID.

        Example: `ChapterFiles.for_video(Path("out"), "abc")`.
        """

        return cls.in_directory(out_dir / "chapters", video_id)

    @classmethod
    def in_directory(cls, directory: Path, video_id: str) -> ChapterFiles:
        """Derive both filenames when the chapter directory is already known.

        Example: `ChapterFiles.in_directory(chapter_dir, "abc")`.
        """

        return cls(
            archive=directory / f"{video_id}.chapters.json",
            mpv=directory / f"{video_id}.chapters.ffmetadata",
        )

    def ready(self) -> bool:
        """Report whether the authoritative chapter archive exists and parses.

        Example: `files.ready()` gates regeneration.
        """

        return self.load() is not None

    def load(self) -> ChapterSet | None:
        """Read a valid archive, treating damaged or partial files as absent.

        Example: `files.load()` supplies chapters to the GUI.
        """

        if not self.archive.exists():
            return None
        try:
            return parse_document(self.archive.read_text(encoding="utf-8"))
        except (OSError, RuntimeError, json.JSONDecodeError):
            return None

    def write(self, chapter_set: ChapterSet) -> None:
        """Atomically persist JSON and refresh the derived mpv sidecar.

        Example: `files.write(chapter_set)` completes one AI generation.
        """

        validate_chapter_set(chapter_set)
        document = {
            "schema": SCHEMA_VERSION,
            "video_id": chapter_set.video_id,
            "primary_language": chapter_set.primary_language,
            "model": chapter_set.model,
            "generated_at": chapter_set.generated_at,
            "duration_ms": chapter_set.duration_ms,
            "chapters": [chapter._asdict() for chapter in chapter_set.chapters],
        }
        json_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        atomic_write(self.archive, json_text)
        atomic_write(self.mpv, render_ffmetadata(chapter_set))

    def ensure_mpv(self) -> Path | None:
        """Recreate the derived mpv file when only the JSON archive remains.

        Example: `files.ensure_mpv()` returns a launchable sidecar path.
        """

        chapter_set = self.load()
        if not chapter_set:
            return None
        mpv_stale = not self.mpv.exists() or self.mpv.stat().st_mtime_ns < self.archive.stat().st_mtime_ns
        if mpv_stale:
            atomic_write(self.mpv, render_ffmetadata(chapter_set))
        return self.mpv


def new_chapter_set(
    video_id: str,
    primary_language: str,
    model: str,
    duration_ms: int,
    chapter_items: list[Chapter],
) -> ChapterSet:
    """Stamp a validated chapter plan at the persistence boundary.

    Example: `new_chapter_set("abc", "nl", "gpt", 60_000, items)`.
    """

    chapter_set = ChapterSet(
        video_id=video_id,
        primary_language=primary_language,
        model=model,
        generated_at=int(time.time()),
        duration_ms=duration_ms,
        chapters=chapter_items,
    )
    validate_chapter_set(chapter_set)
    return chapter_set


def parse_document(text: str) -> ChapterSet:
    """Parse and validate the versioned JSON chapter representation.

    Example: `parse_document(path.read_text())` returns a `ChapterSet`.
    """

    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        raise RuntimeError("unsupported chapter document")
    raw_chapters = data.get("chapters")
    if not isinstance(raw_chapters, list):
        raise RuntimeError("chapter document has no chapter list")
    try:
        chapter_items = [
            Chapter(
                start_ms=item["start_ms"],
                primary_title=item["primary_title"],
                english_title=item["english_title"],
            )
            for item in raw_chapters
            if isinstance(item, dict)
        ]
        if len(chapter_items) != len(raw_chapters):
            raise RuntimeError("chapter document contains a non-object item")
        chapter_set = ChapterSet(
            video_id=data["video_id"],
            primary_language=data["primary_language"],
            model=data["model"],
            generated_at=data["generated_at"],
            duration_ms=data["duration_ms"],
            chapters=chapter_items,
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeError("chapter document is incomplete") from exc
    validate_chapter_set(chapter_set)
    return chapter_set


def validate_chapter_set(chapter_set: ChapterSet) -> None:
    """Enforce stable ordering, useful titles, and bounded timestamps.

    Example: `validate_chapter_set(chapter_set)` runs before every write.
    """

    text_fields = (
        chapter_set.video_id,
        chapter_set.primary_language,
        chapter_set.model,
    )
    if not all(isinstance(value, str) and value.strip() for value in text_fields):
        raise RuntimeError("chapter metadata is incomplete")
    if not isinstance(chapter_set.generated_at, int) or isinstance(chapter_set.generated_at, bool):
        raise RuntimeError("chapter generation time must be an integer timestamp")
    if not isinstance(chapter_set.duration_ms, int) or chapter_set.duration_ms <= 0:
        raise RuntimeError("chapter duration must be positive")
    if not chapter_set.chapters:
        raise RuntimeError("chapter plan is empty")
    if chapter_set.chapters[0].start_ms != 0:
        raise RuntimeError("the first chapter must start at zero")

    previous = -1
    for chapter in chapter_set.chapters:
        if not isinstance(chapter.start_ms, int) or isinstance(chapter.start_ms, bool):
            raise RuntimeError("chapter timestamps must be integer milliseconds")
        if chapter.start_ms <= previous or chapter.start_ms >= chapter_set.duration_ms:
            raise RuntimeError("chapter timestamps must be increasing and within the video")
        if not _clean_title(chapter.primary_title) or not _clean_title(chapter.english_title):
            raise RuntimeError("chapter titles must be non-empty strings")
        previous = chapter.start_ms


def render_ffmetadata(chapter_set: ChapterSet) -> str:
    """Render bilingual titles in the FFmetadata format accepted by mpv.

    Example: `render_ffmetadata(chapter_set)` becomes `--chapters-file` input.
    """

    lines = [";FFMETADATA1"]
    for index, chapter in enumerate(chapter_set.chapters):
        next_start = (
            chapter_set.chapters[index + 1].start_ms
            if index + 1 < len(chapter_set.chapters)
            else chapter_set.duration_ms
        )
        primary = _clean_title(chapter.primary_title)
        english = _clean_title(chapter.english_title)
        title = primary if primary.casefold() == english.casefold() else f"{primary}  •  {english}"
        lines.extend(
            [
                "",
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={chapter.start_ms}",
                f"END={max(chapter.start_ms + 1, next_start)}",
                f"title={ffmetadata_escape(title)}",
            ]
        )
    return "\n".join(lines) + "\n"


def ffmetadata_escape(text: str) -> str:
    """Escape FFmetadata control characters without losing Unicode titles.

    Example: `ffmetadata_escape("A=B")` returns `A\\=B`.
    """

    escaped = _clean_title(text).replace("\\", "\\\\")
    for char in ("=", ";", "#"):
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def atomic_write(path: Path, text: str) -> None:
    """Replace one UTF-8 text file only after its full content is durable.

    Example: `atomic_write(path, json_text)` avoids partial GUI reads.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(text, encoding="utf-8", newline="\n")
    temp_path.replace(path)


def _clean_title(value: object) -> str:
    """Collapse title whitespace for JSON, GUI, and mpv consistency.

    Example: `_clean_title("A\nB")` returns `A B`.
    """

    return " ".join(value.split()) if isinstance(value, str) else ""
