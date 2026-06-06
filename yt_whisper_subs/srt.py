"""SRT parsing, rendering, compaction, and timing adjustment.

Example: `srt.render_srt(srt.parse_srt(text), args)`.
"""

from __future__ import annotations

import argparse
import json
import re
import textwrap
from typing import NamedTuple

from yt_whisper_subs import cfg
from yt_whisper_subs import opts

SRT_TIME_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)
STRONG_PUNCTUATION_RE = re.compile(r"[.!?][\"')\]]*$")
TERMINAL_PERIOD_RE = re.compile(r"\.(?P<trailer>[\"')\]]*)$")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
FALSE_PERIOD_END_WORDS = frozenset(
    """
    a about after against although among an and are around as at be because
    been before being between but by can could did do does during for from
    had has have how if in into is may might must of on or shall should since
    so than that the through to under unless until was were what when
    where whether which while who whom whose why will with within without
    would
    """.split()
)
FALSE_PERIOD_START_WORDS = frozenset(
    """
    about after although and are as at because been before being but by can
    could did do does for from had has have how if in into is not of on or
    shall should since so than that through to under unless until was were what
    when where whether which while who whom whose why will with within without
    would
    """.split()
)
COORDINATING_SOFT_PERIOD_START_WORDS = frozenset("and but or so".split())
LOWERCASE_AFTER_SOFT_PERIOD_WORDS = FALSE_PERIOD_START_WORDS | frozenset(
    """
    a an the their them there these they this those it its our we you your he
    her his she
    """.split()
)


class SubtitleCue(NamedTuple):
    """Timestamped subtitle text stored in milliseconds for stable transforms.

    Example: `SubtitleCue(0, 1200, "Hallo")`.
    """

    start_ms: int
    end_ms: int
    text: str


def parse_srt_timestamp(value: str) -> int:
    """Parse an SRT timestamp into milliseconds.

    Example: `parse_srt_timestamp("00:00:01,200")`.
    """

    hours = int(value[0:2])
    minutes = int(value[3:5])
    seconds = int(value[6:8])
    milliseconds = int(value[9:12])
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + milliseconds


def format_srt_timestamp(milliseconds: int) -> str:
    """Render milliseconds as a normalized SRT timestamp.

    Example: `format_srt_timestamp(1200)`.
    """

    milliseconds = max(0, milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def normalize_subtitle_text(lines: list[str]) -> str:
    """Collapse subtitle text lines into the single cue text representation.

    Example: `normalize_subtitle_text(["Goed", "morgen"])`.
    """

    return re.sub(r"\s+", " ", " ".join(line.strip() for line in lines)).strip()


def parse_srt(content: str) -> list[SubtitleCue]:
    """Parse pragmatic SRT content into cue objects.

    Example: `parse_srt("1\\n00:00:00,000 --> ...")`.
    """

    normalized = content.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    cues: list[SubtitleCue] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if lines[0].isdigit():
            lines = lines[1:]
        if len(lines) < 2:
            continue

        match = SRT_TIME_RE.search(lines[0])
        if not match:
            continue

        text = normalize_subtitle_text(lines[1:])
        if not text:
            continue

        cues.append(
            SubtitleCue(
                start_ms=parse_srt_timestamp(match.group("start")),
                end_ms=parse_srt_timestamp(match.group("end")),
                text=text,
            )
        )

    return cues


def cue_reading_speed(text: str, start_ms: int, end_ms: int) -> float:
    """Compute cue reading speed in characters per second.

    Example: `cue_reading_speed("Hello", 0, 1000)`.
    """

    duration_seconds = max((end_ms - start_ms) / 1000, 0.001)
    return len(text) / duration_seconds


def words_in_text(text: str) -> list[str]:
    """Extract simple Latin-script words for compaction heuristics.

    Example: `words_in_text("and then")`.
    """

    return WORD_RE.findall(text)


def first_word(text: str) -> str:
    """Return the first comparison word in casefolded form.

    Example: `first_word("And then")`.
    """

    words = words_in_text(text)
    return words[0].casefold() if words else ""


def last_word(text: str) -> str:
    """Return the last comparison word in casefolded form.

    Example: `last_word("kind of")`.
    """

    words = words_in_text(text)
    return words[-1].casefold() if words else ""


def terminal_period_is_soft(first: str, second: str, args: argparse.Namespace, *, is_english: bool) -> bool:
    """Detect likely false sentence boundaries produced by Whisper.

    Example: `terminal_period_is_soft("because.", "it", args, is_english=True)`.
    """

    if not opts.should_soften_period_boundaries(args, is_english=is_english):
        return False
    if not TERMINAL_PERIOD_RE.search(first.strip()):
        return False

    first_tail = last_word(first)
    second_head = first_word(second)
    if not first_tail or not second_head:
        return False
    first_words = words_in_text(first)
    if first_tail in FALSE_PERIOD_END_WORDS:
        return True
    if len(first_words) <= 1:
        return False
    if second_head in COORDINATING_SOFT_PERIOD_START_WORDS:
        return False

    return second_head in FALSE_PERIOD_START_WORDS


def should_force_lowercase_after_soft_period(first: str) -> bool:
    """Decide whether the continuation should be forced lowercase after a soft period.

    Example: `should_force_lowercase_after_soft_period("because.")`.
    """

    return last_word(first) in FALSE_PERIOD_END_WORDS


def remove_terminal_period(text: str) -> str:
    """Remove only the final period while preserving trailing quote/bracket text.

    Example: `remove_terminal_period("because.")`.
    """

    stripped = text.rstrip()
    match = TERMINAL_PERIOD_RE.search(stripped)
    if not match:
        return stripped

    return f"{stripped[: match.start()]}{match.group('trailer')}".strip()


def lowercase_soft_period_continuation(text: str, *, force: bool = False) -> str:
    """Lowercase the first continuation word when a soft period is merged away.

    Example: `lowercase_soft_period_continuation("It happened", force=True)`.
    """

    stripped = text.lstrip()
    leading_space = text[: len(text) - len(stripped)]
    match = re.match(r"(?P<prefix>[\"'(\[]*)(?P<word>[A-Za-z][A-Za-z']*)(?P<rest>.*)", stripped, re.DOTALL)
    if not match:
        return text

    word = match.group("word")
    if word.casefold() not in LOWERCASE_AFTER_SOFT_PERIOD_WORDS and not force:
        return text
    if len(word) > 1 and word.isupper():
        return text
    if force and word.casefold() not in LOWERCASE_AFTER_SOFT_PERIOD_WORDS:
        following_words = words_in_text(match.group("rest"))
        if not following_words or following_words[0][:1].isupper():
            return text

    lowered_word = word[:1].lower() + word[1:]
    return f"{leading_space}{match.group('prefix')}{lowered_word}{match.group('rest')}"


def cue_text_for_merge(
    first: str,
    second: str,
    *,
    soft_period: bool = False,
    force_lowercase: bool = False,
) -> str:
    """Build the cue text created when two adjacent cues are merged.

    Example: `cue_text_for_merge("because.", "It", soft_period=True)`.
    """

    if soft_period:
        first = remove_terminal_period(first)
        second = lowercase_soft_period_continuation(second, force=force_lowercase)
    return normalize_subtitle_text([first, second])


def may_merge_cues(first: SubtitleCue, second: SubtitleCue, args: argparse.Namespace, *, is_english: bool) -> bool:
    """Apply the cue compaction constraints to a neighboring cue pair.

    Example: `may_merge_cues(cue_a, cue_b, args, is_english=True)`.
    """

    gap_seconds = (second.start_ms - first.end_ms) / 1000
    if gap_seconds > args.compact_gap:
        return False

    soft_period = terminal_period_is_soft(first.text, second.text, args, is_english=is_english)
    if STRONG_PUNCTUATION_RE.search(first.text) and not soft_period:
        return False

    combined_text = cue_text_for_merge(
        first.text,
        second.text,
        soft_period=soft_period,
        force_lowercase=should_force_lowercase_after_soft_period(first.text),
    )
    combined_duration = (second.end_ms - first.start_ms) / 1000
    if combined_duration > args.compact_max_duration:
        return False
    if len(combined_text) > args.compact_max_chars:
        return False
    if cue_reading_speed(combined_text, first.start_ms, second.end_ms) > args.compact_max_cps:
        return False

    return True


def compact_cues(cues: list[SubtitleCue], args: argparse.Namespace, *, is_english: bool = False) -> list[SubtitleCue]:
    """Merge fragmented adjacent cues while respecting readability constraints.

    Example: `compact_cues(cues, args, is_english=False)`.
    """

    if not cues:
        return []

    compacted: list[SubtitleCue] = []
    current = cues[0]

    for next_cue in cues[1:]:
        if may_merge_cues(current, next_cue, args, is_english=is_english):
            soft_period = terminal_period_is_soft(current.text, next_cue.text, args, is_english=is_english)
            current = SubtitleCue(
                start_ms=current.start_ms,
                end_ms=next_cue.end_ms,
                text=cue_text_for_merge(
                    current.text,
                    next_cue.text,
                    soft_period=soft_period,
                    force_lowercase=should_force_lowercase_after_soft_period(current.text),
                ),
            )
        else:
            compacted.append(current)
            current = next_cue

    compacted.append(current)
    return compacted


def wrap_subtitle_text(text: str, line_width: int) -> str:
    """Wrap cue text without breaking long words or hyphenated names.

    Example: `wrap_subtitle_text("some subtitle text", 50)`.
    """

    width = max(10, line_width)
    return "\n".join(
        textwrap.wrap(
            text,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def render_srt(cues: list[SubtitleCue], args: argparse.Namespace) -> str:
    """Serialize cue objects into normalized SRT content.

    Example: `render_srt(cues, args)`.
    """

    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_srt_timestamp(cue.start_ms)} --> {format_srt_timestamp(cue.end_ms)}",
                    wrap_subtitle_text(cue.text, args.compact_line_width),
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def interleaved_cue_order(cue_count: int) -> list[int]:
    """Return a cue order that separates immediate neighbors in batched prompts.

    Example: `interleaved_cue_order(4)` returns `[0, 2, 1, 3]`.
    """

    even_idxs = list(range(0, cue_count, 2))
    odd_idxs = list(range(1, cue_count, 2))
    return even_idxs + odd_idxs


def openai_source_cues_json(cues: list[SubtitleCue], *, order: list[int] | None = None) -> str:
    """Render cue text as indexed JSON for the OpenAI translation contract.

    Example: `openai_source_cues_json(cues, order=[0, 2, 1])`.
    """

    cue_order = order if order is not None else list(range(len(cues)))
    payload = [
        {
            "index": cue_idx + 1,
            "text": cues[cue_idx].text,
        }
        for cue_idx in cue_order
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def subtitle_gap_extension_ms(args: argparse.Namespace) -> int:
    """Convert the cue gap-extension option from seconds to milliseconds.

    Example: `subtitle_gap_extension_ms(args)`.
    """

    seconds = float(getattr(args, "subtitle_gap_extension", cfg.DEFAULT_SUBTITLE_GAP_EXTENSION))
    return max(0, int(round(seconds * 1000)))


def extend_subtitle_gaps(
    cues: list[SubtitleCue],
    args: argparse.Namespace,
) -> tuple[list[SubtitleCue], bool]:
    """Extend cue end times into silence without crossing the next cue.

    Example: `extend_subtitle_gaps(cues, args)`.
    """

    extension_ms = subtitle_gap_extension_ms(args)
    if extension_ms <= 0 or len(cues) < 2:
        return cues, False

    extended: list[SubtitleCue] = []
    changed = False
    for index, cue in enumerate(cues):
        if index == len(cues) - 1:
            extended.append(cue)
            continue

        next_start_ms = cues[index + 1].start_ms
        if cue.end_ms >= next_start_ms:
            extended.append(cue)
            continue

        end_ms = min(cue.end_ms + extension_ms, next_start_ms)
        changed = changed or end_ms != cue.end_ms
        extended.append(SubtitleCue(cue.start_ms, end_ms, cue.text))

    return extended, changed
