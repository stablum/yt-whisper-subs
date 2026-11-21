"""Check a proposed subtitle word against an unprompted local audio decode."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from functools import cached_property
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import proc
from yt_whisper_subs import srt
from yt_whisper_subs import whisper_local


MAX_CHECKS = 6
MAX_CUE_MS = 12_000
_PAD_MS = 300


class Match(NamedTuple):
    """Anchor one proposed word inside its cue, e.g. 'eigenlijk amper kunnen'."""

    cue: srt.SubtitleCue
    words: tuple[str, ...]


def _confirmed(document: dict, match: Match, offset_ms: int) -> bool:
    """Require one confident, time-aligned match of the complete anchored phrase."""

    count = 0
    confident = False
    size = len(match.words)
    for segment in document["segments"]:
        clear_segment = (-0.5 <= segment["avg_logprob"] <= 0 and 0 <= segment["no_speech_prob"] <= 0.35
                         and 0 < segment["compression_ratio"] <= 2)
        words = segment.get("words", [])
        for idx in range(len(words) - size + 1):
            window = words[idx:idx + size]
            actual = tuple(w["word"].strip(" \t\r\n.,;:!?…()[]\"“”„«»").casefold() for w in window)
            if actual != match.words:
                continue
            start = offset_ms + window[0]["start"] * 1000
            end = offset_ms + window[-1]["end"] * 1000
            if match.cue.start_ms <= start < end <= match.cue.end_ms:
                count += 1
                confident = clear_segment and all(0.8 <= w["probability"] <= 1 for w in window)
    return count == 1 and confident


class Probe:
    """Decode short clips with the configured cached Whisper model, e.g. on a forced review."""

    def __init__(self, media: Path, paths: dict[str, Path], args: argparse.Namespace) -> None:
        """Bind the final video's audio and the pipeline's local Whisper settings."""

        self._media = media
        self._paths = paths
        self._args = args

    @cached_property
    def _model(self) -> Path | None:
        """Resolve an existing model file so Whisper cannot download a model during review."""

        model = whisper_local.whisper_model_url(self._paths, self._args.model)
        if model:
            file = whisper_local.whisper_cache_root() / model.rsplit("/", 1)[-1]
            if file.is_file():
                return file
        return None

    def confirm(self, matches: dict[int, Match]) -> dict[int, str]:
        """Return independently decoded anchor phrases for supported edits only."""

        if not matches:
            return {}
        try:
            if not (self._media.is_file() and self._paths["whisper"].is_file() and self._model):
                print("Local audio check unavailable: media or cached Whisper model is missing.", flush=True)
                return {}
            with tempfile.TemporaryDirectory(prefix="subtitle-audio-") as tmp:
                return self._decode(matches, Path(tmp))
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Local audio check unavailable: {exc}", flush=True)
            return {}

    def _decode(self, matches: dict[int, Match], root: Path) -> dict[int, str]:
        """Extract short clips and decode all candidates in one Whisper process."""

        offsets = {}
        for idx, match in matches.items():
            start = max(0, match.cue.start_ms - _PAD_MS)
            duration = match.cue.end_ms + _PAD_MS - start
            clip = root / f"{idx}.wav"
            copy = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start / 1000:.3f}",
                    "-i", self._media, "-t", f"{duration / 1000:.3f}", "-vn", "-ac", "1", "-ar", "16000", clip]
            if proc.run(copy, check=False, timeout=30).returncode == 0:
                offsets[idx] = start
        if not offsets:
            return {}
        clips = [root / f"{idx}.wav" for idx in offsets]
        args = self._args
        command = [
            self._paths["whisper"], *clips, "--model", self._model, "--task", "transcribe",
            "--output_format", "json", "--output_dir", root, "--device", args.device,
            "--fp16", str(args.device == "cuda"), "--condition_on_previous_text", "False",
            "--word_timestamps", "True", "--temperature_increment_on_fallback", "None",
        ]
        if args.language and args.language != "auto":
            command += ["--language", args.language]
        proc.run(command, timeout=180)

        confirmed = {}
        for idx, start in offsets.items():
            try:
                document = json.loads((root / f"{idx}.json").read_text(encoding="utf-8"))
                if _confirmed(document, matches[idx], start):
                    confirmed[idx] = " ".join(matches[idx].words)
            except (OSError, ValueError, TypeError, KeyError):
                print(f"Could not read local audio evidence for cue {idx}.", flush=True)
        return confirmed
