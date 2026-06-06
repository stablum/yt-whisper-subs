"""Command-line parser for the subtitle pipeline.

Example: `cli.parse_args()` returns the normalized source fields.
"""

from __future__ import annotations

import argparse

from yt_whisper_subs import cfg
from yt_whisper_subs import opts


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments and normalize the single source input.

    Example: `parse_args()` turns a positional URL into `args.url`.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Download a YouTube video or use a local video, generate an SRT "
            "with local Whisper, and optionally play it in mpv."
        )
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="YouTube/video URL or local video file path. Use --url or --video-file for clarity.",
    )

    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--url", help="YouTube/video URL to download with yt-dlp.")
    source_group.add_argument("--video-file", help="Local video file to subtitle.")

    parser.add_argument(
        "--out-dir",
        help=f"Output root directory. Default: {cfg.DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--log-file",
        help=(
            "Write a comprehensive run log to this path. By default a timestamped log "
            "is written under the output root's logs directory."
        ),
    )
    parser.add_argument(
        "--language",
        default="nl",
        help="Whisper language code, or 'auto' to let Whisper detect it. Default: nl.",
    )
    parser.add_argument("--model", choices=cfg.MODEL_CHOICES, default="turbo")
    parser.add_argument(
        "--english-model",
        choices=cfg.MODEL_CHOICES,
        help=(
            "Whisper model for Dutch-to-English audio translation when "
            "--english-translation-provider whisper is used. Defaults to medium "
            "when --model is turbo, otherwise defaults to --model."
        ),
    )
    parser.add_argument(
        "--english-translation-provider",
        choices=("openai", "whisper"),
        default="openai",
        help=(
            "How Dutch-to-English subtitles are generated. The default, openai, sends the "
            "primary cue text to the OpenAI Responses API in bounded indexed chunks and preserves exact cue timings. "
            "Use whisper for the previous local audio translation path."
        ),
    )
    parser.add_argument(
        "--openai-translation-model",
        default=cfg.DEFAULT_OPENAI_TRANSLATION_MODEL,
        help=(
            "OpenAI model used for SRT English translation. "
            f"Default: {cfg.DEFAULT_OPENAI_TRANSLATION_MODEL}."
        ),
    )
    parser.add_argument(
        "--openai-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh"),
        default=cfg.DEFAULT_OPENAI_TRANSLATION_REASONING,
        help=(
            "OpenAI reasoning effort for SRT English translation. "
            f"Default: {cfg.DEFAULT_OPENAI_TRANSLATION_REASONING}."
        ),
    )
    parser.add_argument(
        "--openai-timeout",
        type=float,
        default=cfg.DEFAULT_OPENAI_TIMEOUT,
        help=f"Seconds to wait for the OpenAI translation request. Default: {cfg.DEFAULT_OPENAI_TIMEOUT:g}.",
    )
    parser.add_argument(
        "--openai-max-retries",
        type=int,
        default=cfg.DEFAULT_OPENAI_MAX_RETRIES,
        help=(
            "Retries for transient OpenAI network/server failures after the initial request. "
            f"Default: {cfg.DEFAULT_OPENAI_MAX_RETRIES}."
        ),
    )
    parser.add_argument(
        "--openai-translation-chunk-cues",
        type=int,
        default=cfg.DEFAULT_OPENAI_TRANSLATION_CHUNK_CUES,
        help=(
            "Maximum subtitle cues per OpenAI translation request. Use 0 for one full cue-list request. "
            f"Default: {cfg.DEFAULT_OPENAI_TRANSLATION_CHUNK_CUES}."
        ),
    )
    parser.add_argument(
        "--openai-translation-context-cues",
        type=int,
        default=cfg.DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES,
        help=(
            "Neighboring cues sent as context around each OpenAI translation chunk. "
            f"Default: {cfg.DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES}."
        ),
    )
    parser.add_argument(
        "--openai-env-file",
        default=str(cfg.DEFAULT_OPENAI_ENV_FILE),
        help=(
            "Optional .env file to load before calling OpenAI. "
            f"Default: {cfg.DEFAULT_OPENAI_ENV_FILE}."
        ),
    )
    parser.add_argument("--task", choices=("transcribe", "translate"), default="transcribe")
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument(
        "--audio-format",
        choices=cfg.AUDIO_FORMAT_CHOICES,
        default="opus",
        help="Lossy audio format to keep for Whisper input. Default: opus.",
    )
    parser.add_argument(
        "--video-format",
        default="bv*+ba/b",
        help=(
            "yt-dlp format selector. The default stores YouTube's already-lossy "
            "compressed A/V streams without creating a lossless video."
        ),
    )
    parser.add_argument(
        "--merge-output-format",
        choices=("mkv", "mp4", "webm"),
        default="mkv",
        help="Container for downloaded video streams. Default: mkv.",
    )
    parser.add_argument(
        "--download-progress-delta",
        type=float,
        default=cfg.DEFAULT_DOWNLOAD_PROGRESS_DELTA,
        help=f"Minimum seconds between yt-dlp progress updates. Default: {cfg.DEFAULT_DOWNLOAD_PROGRESS_DELTA:g}.",
    )
    parser.add_argument(
        "--torch-index-url",
        default=cfg.DEFAULT_TORCH_INDEX_URL,
        help="PyTorch package index URL used when installing CUDA wheels.",
    )
    parser.add_argument(
        "--install-tools",
        action="store_true",
        help="Install/update ffmpeg and mpv via scoop when available.",
    )
    parser.add_argument(
        "--install-python-deps",
        action="store_true",
        help="Create/update .venv beside this script with uv and install torch, yt-dlp, and openai-whisper.",
    )
    parser.add_argument(
        "--python-version",
        default=cfg.DEFAULT_PYTHON_VERSION,
        help=(
            "Python version for uv-managed .venv creation. "
            f"Default: {cfg.DEFAULT_PYTHON_VERSION}."
        ),
    )
    parser.add_argument("--no-play", action="store_true", help="Only create subtitles; do not open mpv.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download URL videos and regenerate subtitles even if yields already exist.",
    )
    parser.add_argument(
        "--force-english",
        action="store_true",
        help=(
            "Regenerate the Dutch-to-English subtitle yield without forcing the video download "
            "or primary Dutch Whisper transcription."
        ),
    )
    parser.add_argument(
        "--no-english-for-dutch",
        dest="english_for_dutch",
        action="store_false",
        default=True,
        help="Do not create an additional English .en.srt when --language is Dutch.",
    )
    parser.add_argument(
        "--no-dual-subs",
        dest="dual_subs",
        action="store_false",
        default=True,
        help="Load multiple subtitles as selectable tracks instead of displaying two at once in mpv.",
    )
    parser.add_argument(
        "--dual-sub-primary-color",
        default=cfg.DEFAULT_DUAL_SUB_PRIMARY_COLOR,
        help=(
            "Text color for the primary subtitle track when dual subtitles are shown, "
            f"as #RRGGBB or #RRGGBBAA. Default: {cfg.DEFAULT_DUAL_SUB_PRIMARY_COLOR}."
        ),
    )
    parser.add_argument(
        "--dual-sub-secondary-color",
        default=cfg.DEFAULT_DUAL_SUB_SECONDARY_COLOR,
        help=(
            "Text color for the secondary subtitle track when dual subtitles are shown, "
            f"as #RRGGBB or #RRGGBBAA. Default: {cfg.DEFAULT_DUAL_SUB_SECONDARY_COLOR}."
        ),
    )
    parser.add_argument(
        "--dual-sub-primary-pos",
        type=float,
        default=cfg.DEFAULT_DUAL_SUB_PRIMARY_POS,
        help=f"Subtitle position hint for the primary subtitles. Default: {cfg.DEFAULT_DUAL_SUB_PRIMARY_POS}.",
    )
    parser.add_argument(
        "--dual-sub-secondary-pos",
        type=float,
        default=cfg.DEFAULT_DUAL_SUB_SECONDARY_POS,
        help=f"Subtitle position hint for the secondary subtitles. Default: {cfg.DEFAULT_DUAL_SUB_SECONDARY_POS}.",
    )
    parser.add_argument(
        "--dual-sub-font-size",
        type=float,
        default=cfg.DEFAULT_DUAL_SUB_FONT_SIZE,
        help=(
            "Visual subtitle font-size target for dual subtitles. The primary mpv-native "
            "track is scaled to match the secondary ASS track. "
            f"Default: {cfg.DEFAULT_DUAL_SUB_FONT_SIZE}."
        ),
    )
    parser.add_argument(
        "--dual-sub-primary-font-size",
        type=float,
        help=(
            "Override the native mpv font size for the primary subtitle track. "
            "By default this is derived from --dual-sub-font-size."
        ),
    )
    parser.add_argument(
        "--dual-sub-secondary-font-size",
        type=float,
        help=(
            "Override the ASS font size for the secondary subtitle track. "
            "By default this is --dual-sub-font-size."
        ),
    )
    parser.add_argument(
        "--compact-subs",
        choices=("english", "all", "none"),
        default="english",
        help=(
            "Compact fragmented SRT cues after generation and on existing files. Default: english. "
            "When OpenAI English translation is enabled, the primary SRT is also compacted first "
            "so translation uses those exact cue timings."
        ),
    )
    parser.add_argument(
        "--no-compact-subs",
        dest="compact_subs",
        action="store_const",
        const="none",
        help="Disable subtitle compaction.",
    )
    parser.add_argument(
        "--compact-soft-periods",
        choices=("english", "all", "none"),
        default=cfg.DEFAULT_COMPACT_SOFT_PERIODS,
        help=(
            "Treat likely false period boundaries as mergeable during compaction. "
            f"Default: {cfg.DEFAULT_COMPACT_SOFT_PERIODS}."
        ),
    )
    parser.add_argument(
        "--no-compact-soft-periods",
        dest="compact_soft_periods",
        action="store_const",
        const="none",
        help="Do not merge across period boundaries during subtitle compaction.",
    )
    parser.add_argument(
        "--compact-gap",
        type=float,
        default=cfg.DEFAULT_COMPACT_GAP,
        help=f"Maximum cue gap in seconds that may be merged. Default: {cfg.DEFAULT_COMPACT_GAP}.",
    )
    parser.add_argument(
        "--compact-max-duration",
        type=float,
        default=cfg.DEFAULT_COMPACT_MAX_DURATION,
        help=f"Maximum merged cue duration in seconds. Default: {cfg.DEFAULT_COMPACT_MAX_DURATION}.",
    )
    parser.add_argument(
        "--compact-max-chars",
        type=int,
        default=cfg.DEFAULT_COMPACT_MAX_CHARS,
        help=f"Maximum merged cue text length. Default: {cfg.DEFAULT_COMPACT_MAX_CHARS}.",
    )
    parser.add_argument(
        "--compact-max-cps",
        type=float,
        default=cfg.DEFAULT_COMPACT_MAX_CPS,
        help=f"Maximum merged cue reading speed in characters per second. Default: {cfg.DEFAULT_COMPACT_MAX_CPS}.",
    )
    parser.add_argument(
        "--compact-line-width",
        type=int,
        default=cfg.DEFAULT_COMPACT_LINE_WIDTH,
        help=f"Target subtitle line width when rewriting compacted cues. Default: {cfg.DEFAULT_COMPACT_LINE_WIDTH}.",
    )
    parser.add_argument(
        "--subtitle-gap-extension",
        type=float,
        default=cfg.DEFAULT_SUBTITLE_GAP_EXTENSION,
        help=(
            "Seconds to extend each subtitle cue into following silence, capped by the next cue start. "
            f"Use 0 to disable. Default: {cfg.DEFAULT_SUBTITLE_GAP_EXTENSION:g}."
        ),
    )
    audio_group = parser.add_mutually_exclusive_group()
    audio_group.add_argument(
        "--keep-audio",
        dest="keep_audio",
        action="store_true",
        default=True,
        help="Keep the extracted lossy audio file. This is the default.",
    )
    audio_group.add_argument(
        "--delete-audio",
        dest="keep_audio",
        action="store_false",
        help="Delete the extracted audio after subtitles are generated.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        help="Pass through to yt-dlp, for example: firefox, chrome, edge.",
    )

    args = parser.parse_args()
    explicit_sources = [value for value in (args.source, args.url, args.video_file) if value]
    if len(explicit_sources) != 1:
        parser.error("provide exactly one source: positional SOURCE, --url, or --video-file")
    if args.openai_max_retries < 0:
        parser.error("--openai-max-retries must be 0 or greater")
    if args.openai_translation_chunk_cues < 0:
        parser.error("--openai-translation-chunk-cues must be 0 or greater")
    if args.openai_translation_context_cues < 0:
        parser.error("--openai-translation-context-cues must be 0 or greater")
    if args.subtitle_gap_extension < 0:
        parser.error("--subtitle-gap-extension must be 0 or greater")
    if (
        args.openai_translation_model == "gpt-5-mini"
        and args.openai_reasoning_effort not in cfg.GPT_5_MINI_REASONING_EFFORTS
    ):
        allowed = ", ".join(cfg.GPT_5_MINI_REASONING_EFFORTS)
        parser.error(
            "--openai-reasoning-effort must be one of "
            f"{allowed} when --openai-translation-model is gpt-5-mini"
        )

    if args.source:
        if opts.looks_like_url(args.source):
            args.url = args.source
        else:
            args.video_file = args.source

    return args
