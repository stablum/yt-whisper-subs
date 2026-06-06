"""Small policy helpers derived from parsed command-line options.

Example: `opts.should_compact_subtitles(args, is_english=False)`.
"""

from __future__ import annotations

import argparse

from yt_whisper_subs import cfg


def looks_like_url(value: str) -> bool:
    """Detect source strings that should be routed to yt-dlp.

    Example: `looks_like_url("https://youtu.be/id")`.
    """

    lowered = value.lower()
    return lowered.startswith(("http://", "https://"))


def is_dutch_language(language: str | None) -> bool:
    """Recognize the language values that trigger Dutch-to-English subtitles.

    Example: `is_dutch_language("nl")`.
    """

    if not language:
        return False
    return language.casefold() in {"nl", "dutch", "nederlands"}


def english_model(args: argparse.Namespace) -> str:
    """Choose the local Whisper model used for audio translation.

    Example: `english_model(args)` returns `medium` when the main model is `turbo`.
    """

    if args.english_model:
        return args.english_model
    if args.model == "turbo":
        return "medium"
    return args.model


def uses_openai_english_translation(args: argparse.Namespace) -> bool:
    """Check whether English generation is the cue-text OpenAI path.

    Example: `uses_openai_english_translation(args)`.
    """

    return getattr(args, "english_translation_provider", "openai") == "openai"


def should_compact_subtitles(args: argparse.Namespace, *, is_english: bool) -> bool:
    """Centralize compaction routing so primary and English cues stay aligned.

    Example: `should_compact_subtitles(args, is_english=True)`.
    """

    if args.compact_subs == "none":
        return False
    if is_english and uses_openai_english_translation(args):
        return False
    if not is_english and getattr(args, "compact_primary_for_openai_translation", False):
        return True
    if args.compact_subs == "all":
        return True
    return is_english


def should_soften_period_boundaries(args: argparse.Namespace, *, is_english: bool) -> bool:
    """Gate heuristic period removal behind one option-aware decision point.

    Example: `should_soften_period_boundaries(args, is_english=False)`.
    """

    mode = getattr(args, "compact_soft_periods", cfg.DEFAULT_COMPACT_SOFT_PERIODS)
    if mode == "none":
        return False
    if mode == "all":
        return True
    return is_english
