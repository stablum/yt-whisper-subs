"""mpv playback and ASS styling helpers for dual subtitles.

Example: `playback.play_video(video, [primary, english], prefs)`.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import cfg
from yt_whisper_subs import mpv_ipc
from yt_whisper_subs import playback_progress as progress
from yt_whisper_subs import proc
from yt_whisper_subs import srt


class SubtitleStyle(NamedTuple):
    """Keep one subtitle track's mpv/ASS presentation settings cohesive.

    Example: `SubtitleStyle("#FFE066", 100, 36)`.
    """

    color: str
    position: float
    font_size: float


class PlaybackPrefs(NamedTuple):
    """Hold the complete shared playback policy for CLI and GUI launches.

    Example: `PlaybackPrefs.defaults()` opens the normal dual-subtitle view.
    """

    dual_subs: bool
    primary: SubtitleStyle
    secondary: SubtitleStyle

    @classmethod
    def defaults(cls) -> PlaybackPrefs:
        """Build playback preferences from the single source of defaults.

        Example: `prefs = PlaybackPrefs.defaults()`.
        """

        primary_size = cfg.DEFAULT_DUAL_SUB_FONT_SIZE * cfg.DEFAULT_DUAL_SUB_PRIMARY_FONT_SCALE
        return cls(
            dual_subs=True,
            primary=SubtitleStyle(
                cfg.DEFAULT_DUAL_SUB_PRIMARY_COLOR,
                cfg.DEFAULT_DUAL_SUB_PRIMARY_POS,
                primary_size,
            ),
            secondary=SubtitleStyle(
                cfg.DEFAULT_DUAL_SUB_SECONDARY_COLOR,
                cfg.DEFAULT_DUAL_SUB_SECONDARY_POS,
                cfg.DEFAULT_DUAL_SUB_FONT_SIZE,
            ),
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> PlaybackPrefs:
        """Translate CLI options once at the playback boundary.

        Example: `PlaybackPrefs.from_args(args)`.
        """

        primary_size = args.dual_sub_primary_font_size
        if primary_size is None:
            primary_size = args.dual_sub_font_size * cfg.DEFAULT_DUAL_SUB_PRIMARY_FONT_SCALE
        secondary_size = args.dual_sub_secondary_font_size or args.dual_sub_font_size
        return cls(
            dual_subs=args.dual_subs,
            primary=SubtitleStyle(
                args.dual_sub_primary_color,
                args.dual_sub_primary_pos,
                primary_size,
            ),
            secondary=SubtitleStyle(
                args.dual_sub_secondary_color,
                args.dual_sub_secondary_pos,
                secondary_size,
            ),
        )


class PlaybackSession(NamedTuple):
    """Hold launch-specific observation, chapter, and seek behavior together.

    Example: `PlaybackSession(chapter_path=path, start_seconds=90)`.
    """

    observer: progress.Observer | None = None
    chapter_path: Path | None = None
    start_seconds: float | None = None


def parse_css_color(value: str) -> tuple[int, int, int, int]:
    """Parse #RRGGBB or #RRGGBBAA subtitle colors.

    Example: `parse_css_color("#FFE066")`.
    """

    hex_value = value.strip().removeprefix("#")

    if len(hex_value) == 6:
        red, green, blue = (int(hex_value[index : index + 2], 16) for index in (0, 2, 4))
        alpha = 255
    elif len(hex_value) == 8:
        red, green, blue, alpha = (int(hex_value[index : index + 2], 16) for index in (0, 2, 4, 6))
    else:
        raise ValueError

    return red, green, blue, alpha


def css_color_to_ass_color(value: str) -> str:
    """Convert CSS RGBA color text to ASS BGR-alpha notation.

    Example: `css_color_to_ass_color("#FFE066")`.
    """

    red, green, blue, css_alpha = parse_css_color(value)
    ass_alpha = 255 - css_alpha
    return f"&H{ass_alpha:02X}{blue:02X}{green:02X}{red:02X}"


def css_color_to_mpv_color(value: str) -> str:
    """Convert CSS RGBA color text to mpv alpha-first notation.

    Example: `css_color_to_mpv_color("#FFE066")`.
    """

    red, green, blue, alpha = parse_css_color(value)
    return f"#{alpha:02X}{red:02X}{green:02X}{blue:02X}"


def mpv_subtitle_color(value: str) -> str:
    """Validate and convert a user subtitle color for mpv.

    Example: `mpv_subtitle_color("#66D9EF")`.
    """

    try:
        return css_color_to_mpv_color(value)
    except ValueError as exc:
        raise RuntimeError(f"invalid subtitle color '{value}'; use #RRGGBB or #RRGGBBAA") from exc


def ass_timestamp(milliseconds: int) -> str:
    """Render milliseconds in ASS timestamp syntax.

    Example: `ass_timestamp(1200)`.
    """

    milliseconds = max(0, milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    centiseconds = milliseconds // 10
    return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"


def ass_escape_text(text: str) -> str:
    """Escape SRT cue text for an ASS Dialogue line.

    Example: `ass_escape_text("hello\\nworld")`.
    """

    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", r"\N")
    )


def ass_alignment_from_sub_pos(position: float) -> int:
    """Map mpv-style vertical position into ASS alignment.

    Example: `ass_alignment_from_sub_pos(8)`.
    """

    if position <= 33:
        return 8
    if position >= 67:
        return 2
    return 5


def ass_margin_v_from_sub_pos(position: float) -> int:
    """Map mpv-style vertical position into an ASS vertical margin.

    Example: `ass_margin_v_from_sub_pos(100)`.
    """

    if position <= 33:
        return max(30, round(1080 * max(position, 0) / 100))
    if position >= 67:
        return max(35, round(1080 * max(100 - min(position, 100), 0) / 100))
    return 0


def write_ass_subtitle(
    srt_path: Path,
    ass_path: Path,
    *,
    color: str,
    position: float,
    font_size: float,
) -> None:
    """Render an SRT file as a styled ASS file for secondary subtitles.

    Example: `write_ass_subtitle(en_srt, tmp_ass, color="#66D9EF", position=8, font_size=80)`.
    """

    try:
        primary_color = css_color_to_ass_color(color)
    except ValueError as exc:
        raise RuntimeError(f"invalid subtitle color '{color}'; use #RRGGBB or #RRGGBBAA") from exc

    cues = srt.parse_srt(srt_path.read_text(encoding="utf-8-sig"))
    alignment = ass_alignment_from_sub_pos(position)
    margin_v = ass_margin_v_from_sub_pos(position)
    ass_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,Segoe UI,{font_size:g},{primary_color},"
            "&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,3,1,"
            f"{alignment},40,40,{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for cue in cues:
        lines.append(
            "Dialogue: "
            f"0,{ass_timestamp(cue.start_ms)},{ass_timestamp(cue.end_ms)},"
            f"Default,,0,0,0,,{ass_escape_text(cue.text)}"
        )

    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def dual_subtitle_playback_paths(
    srt_paths: list[Path],
    prefs: PlaybackPrefs,
    temp_dir: Path,
) -> list[Path]:
    """Replace the secondary SRT with a temporary styled ASS path for mpv.

    Example: `dual_subtitle_playback_paths([nl, en], args, temp_dir)`.
    """

    secondary_ass = temp_dir / f"{srt_paths[1].stem}.secondary.ass"

    write_ass_subtitle(
        srt_paths[1],
        secondary_ass,
        color=prefs.secondary.color,
        position=prefs.secondary.position,
        font_size=prefs.secondary.font_size,
    )

    return [srt_paths[0], secondary_ass, *srt_paths[2:]]


def sidecar_subtitles(video_path: Path) -> list[Path]:
    """Discover subtitle sidecars in the same English-first playback order.

    Example: `sidecar_subtitles(Path("abc.mkv"))`.
    """

    english = video_path.with_name(f"{video_path.stem}.en.srt")
    primary = video_path.with_suffix(".srt")
    return [path for path in (english, primary) if path.exists()]


def play_video(
    video_path: Path,
    srt_paths: list[Path],
    prefs: PlaybackPrefs,
    session: PlaybackSession | None = None,
) -> None:
    """Open mpv with selected subtitles and cohesive launch-only additions.

    Example: `play_video(video, srts, prefs, PlaybackSession(observer=observer))`.
    """

    session = session or PlaybackSession()
    cmd: list[str | os.PathLike[str]] = ["mpv", "--sub-auto=no"]
    monitor = mpv_ipc.MpvMonitor(session.observer) if session.observer else None
    if monitor:
        cmd.append(monitor.mpv_option)
    if session.chapter_path and session.chapter_path.exists():
        cmd.append(f"--chapters-file={session.chapter_path}")
    if session.start_seconds is not None:
        cmd.append(f"--start={max(0.0, session.start_seconds):g}")
    existing_srt_paths = [srt_path for srt_path in srt_paths if srt_path.exists()]

    temp_dir_context = None
    try:
        if prefs.dual_subs and len(existing_srt_paths) >= 2:
            temp_dir_context = tempfile.TemporaryDirectory(prefix="yt-whisper-subs-ass-")
            temp_dir = Path(temp_dir_context.__enter__())
            subtitle_paths = dual_subtitle_playback_paths(existing_srt_paths, prefs, temp_dir)
        else:
            subtitle_paths = existing_srt_paths

        for subtitle_path in subtitle_paths:
            cmd.append(f"--sub-file={subtitle_path}")

        if prefs.dual_subs and len(existing_srt_paths) >= 2:
            cmd += [
                "--sid=1",
                "--secondary-sid=2",
                f"--sub-color={mpv_subtitle_color(prefs.primary.color)}",
                f"--sub-font-size={prefs.primary.font_size:g}",
                f"--sub-pos={prefs.primary.position:g}",
                "--secondary-sub-ass-override=no",
            ]

        cmd.append(video_path)
        if monitor:
            with monitor:
                proc.run(cmd, silence_seconds=None, window=proc.ChildWindow.VISIBLE)
        else:
            proc.run(cmd, silence_seconds=None, window=proc.ChildWindow.VISIBLE)
    finally:
        if temp_dir_context is not None:
            temp_dir_context.__exit__(None, None, None)
