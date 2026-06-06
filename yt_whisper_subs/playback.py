"""mpv playback and ASS styling helpers for dual subtitles.

Example: `playback.play_video(video, [primary, english], args)`.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from yt_whisper_subs import cfg
from yt_whisper_subs import proc
from yt_whisper_subs import srt


def dual_sub_primary_font_size(args: argparse.Namespace) -> float:
    """Derive the native mpv primary subtitle font size.

    Example: `dual_sub_primary_font_size(args)`.
    """

    if args.dual_sub_primary_font_size is not None:
        return args.dual_sub_primary_font_size
    return args.dual_sub_font_size * cfg.DEFAULT_DUAL_SUB_PRIMARY_FONT_SCALE


def dual_sub_secondary_font_size(args: argparse.Namespace) -> float:
    """Derive the generated ASS secondary subtitle font size.

    Example: `dual_sub_secondary_font_size(args)`.
    """

    if args.dual_sub_secondary_font_size is not None:
        return args.dual_sub_secondary_font_size
    return args.dual_sub_font_size


def parse_css_color(value: str) -> tuple[int, int, int, int]:
    """Parse #RRGGBB or #RRGGBBAA subtitle colors.

    Example: `parse_css_color("#FFE066")`.
    """

    hex_value = value.strip()
    if hex_value.startswith("#"):
        hex_value = hex_value[1:]

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


def dual_subtitle_playback_paths(srt_paths: list[Path], args: argparse.Namespace, temp_dir: Path) -> list[Path]:
    """Replace the secondary SRT with a temporary styled ASS path for mpv.

    Example: `dual_subtitle_playback_paths([nl, en], args, temp_dir)`.
    """

    secondary_ass = temp_dir / f"{srt_paths[1].stem}.secondary.ass"

    write_ass_subtitle(
        srt_paths[1],
        secondary_ass,
        color=args.dual_sub_secondary_color,
        position=args.dual_sub_secondary_pos,
        font_size=dual_sub_secondary_font_size(args),
    )

    return [srt_paths[0], secondary_ass, *srt_paths[2:]]


def play_video(video_path: Path, srt_paths: list[Path], args: argparse.Namespace) -> None:
    """Open mpv with selected subtitle paths and optional dual-sub display.

    Example: `play_video(video, [primary, english], args)`.
    """

    cmd: list[str | os.PathLike[str]] = ["mpv", "--sub-auto=no"]
    existing_srt_paths = [srt_path for srt_path in srt_paths if srt_path.exists()]

    temp_dir_context = None
    try:
        if args.dual_subs and len(existing_srt_paths) >= 2:
            temp_dir_context = tempfile.TemporaryDirectory(prefix="yt-whisper-subs-ass-")
            temp_dir = Path(temp_dir_context.__enter__())
            subtitle_paths = dual_subtitle_playback_paths(existing_srt_paths, args, temp_dir)
        else:
            subtitle_paths = existing_srt_paths

        for subtitle_path in subtitle_paths:
            cmd.append(f"--sub-file={subtitle_path}")

        if args.dual_subs and len(existing_srt_paths) >= 2:
            cmd += [
                "--sid=1",
                "--secondary-sid=2",
                f"--sub-color={mpv_subtitle_color(args.dual_sub_primary_color)}",
                f"--sub-font-size={dual_sub_primary_font_size(args):g}",
                f"--sub-pos={args.dual_sub_primary_pos:g}",
                "--secondary-sub-ass-override=no",
            ]

        cmd.append(video_path)
        proc.run(cmd, silence_seconds=None)
    finally:
        if temp_dir_context is not None:
            temp_dir_context.__exit__(None, None, None)
