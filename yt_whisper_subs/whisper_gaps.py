"""Retry bounded holes in raw Whisper coverage without replacing existing speech.

Example: a decoded utterance ending early must not hide the next twenty seconds.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from pathlib import Path

from yt_whisper_subs import proc
from yt_whisper_subs import srt
from yt_whisper_subs import yield_files


type Gap = tuple[int, int]  # An uncovered [start, end] range in source milliseconds.
_MIN_GAP_MS = 8_000
_MAX_GAP_MS = 30_000
_MAX_GAPS = 8


def find(cues: list[srt.SubtitleCue]) -> list[Gap]:
    """Limit retries to short internal gaps before cue extension; e.g. 8:22–8:45."""

    gaps, covered_end = [], None
    for cue in sorted(cues, key=lambda cue: cue.start_ms):
        if covered_end is not None and _MIN_GAP_MS <= cue.start_ms - covered_end <= _MAX_GAP_MS:
            gaps.append((covered_end, cue.start_ms))
        covered_end = max(covered_end or 0, cue.end_ms)
    return gaps[:_MAX_GAPS]


def recovered_cues(document: dict, gaps: list[Gap]) -> list[srt.SubtitleCue]:
    """Accept confident speech wholly inside an uncovered range; e.g. reject noise and overlapping text."""

    cues = []
    for segment in document["segments"]:
        if not all(math.isfinite(segment[key]) for key in ("start", "end")):
            continue
        start, end = round(segment["start"] * 1000), round(segment["end"] * 1000)
        if not start < end or not any(a <= start < end <= b for a, b in gaps):
            continue
        words = segment.get("words", [])
        if not words or len(srt.WORD_RE.findall(segment["text"])) < 3:
            continue
        # Mean word probability p̄ and decoder likelihood reject speculative noise transcripts.
        prob = sum(word["probability"] for word in words) / len(words)
        scores = (prob, segment["avg_logprob"], segment["no_speech_prob"], segment["compression_ratio"])
        if not all(math.isfinite(score) for score in scores):
            continue
        if prob < 0.8 or scores[1] < -0.5 or scores[2] > 0.35 or scores[3] > 2.0:
            continue
        text = srt.normalize_subtitle_text(segment["text"].splitlines())
        cue = srt.SubtitleCue(start, end, text)
        if not cues or start >= cues[-1].end_ms:
            cues.append(cue)
    repeated = Counter(cue.text.casefold() for cue in cues)
    if any(count >= 3 for count in repeated.values()) or srt.repeated_phrase_loop(cues):
        return []
    return cues


def recover(path: Path, command: list[str | os.PathLike[str]], args: argparse.Namespace) -> bool:
    """Reuse the same decoding policy on narrow audio windows; e.g. one extra Whisper launch for all gaps."""

    cues = srt.parse_srt(path.read_text(encoding="utf-8-sig"))
    if not (gaps := find(cues)):
        return False
    retry_dir = path.parent / "gap-recovery"
    retry_dir.mkdir(exist_ok=True)
    retry = list(command)
    retry[retry.index("--output_format") + 1] = "json"
    retry[retry.index("--output_dir") + 1] = retry_dir
    seconds = ",".join(f"{value / 1000:.3f}" for gap in gaps for value in gap)
    retry.extend(["--clip_timestamps", seconds])
    print(f"Checking {len(gaps)} uncovered subtitle audio window(s) with Whisper...", flush=True)
    proc.run(retry)
    document = json.loads((retry_dir / path.with_suffix(".json").name).read_text(encoding="utf-8"))
    additions = recovered_cues(document, gaps)
    if not additions:
        print("No confident speech recovered from subtitle gaps.", flush=True)
        return False
    merged = sorted([*cues, *additions], key=lambda cue: cue.start_ms)
    content = srt.render_srt(merged, args)
    if srt.repeated_phrase_loop(merged):
        raise RuntimeError("Gap recovery produced a repeated speech-recognition loop")
    yield_files.atomic_write(path, content)
    print(f"Recovered {len(additions)} subtitle cue(s) from previously uncovered audio.", flush=True)
    return True
