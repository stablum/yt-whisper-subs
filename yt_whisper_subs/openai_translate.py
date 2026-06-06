"""OpenAI Responses API translation for indexed SRT cue text.

Example: `openai_translate.translate_srt_with_openai(src, dst, args)`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import textwrap
from pathlib import Path
from typing import NamedTuple

from yt_whisper_subs import cfg
from yt_whisper_subs import openai_client
from yt_whisper_subs import srt


class OpenAITranslationParseResult(NamedTuple):
    """Partial parse result that lets incomplete chunks be repaired cheaply.

    Example: `OpenAITranslationParseResult([None], ["missing"])`.
    """

    texts: list[str | None]
    errors: list[str]

    @property
    def missing_indexes(self) -> list[int]:
        """Return one-based indexes that still lack usable translations.

        Example: `result.missing_indexes`.
        """

        return [index + 1 for index, text in enumerate(self.texts) if text is None]

    @property
    def complete(self) -> bool:
        """Report whether every source cue has exactly one translation.

        Example: `if result.complete: ...`.
        """

        return not self.missing_indexes


def openai_translation_response_format() -> dict[str, object]:
    """Return the strict JSON schema requested from the Responses API.

    Example: `openai_translation_response_format()`.
    """

    return {
        "type": "json_schema",
        "name": "srt_translation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["translations"],
            "properties": {
                "translations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["index", "text"],
                        "properties": {
                            "index": {"type": "integer"},
                            "text": {"type": "string"},
                        },
                    },
                }
            },
        },
    }


def openai_translation_prompt(
    source_cues_json: str,
    cue_count: int,
    *,
    chunk_label: str | None = None,
    previous_context: str = "",
    next_context: str = "",
) -> str:
    """Build the main cue-translation prompt with local context only as context.

    Example: `openai_translation_prompt(cue_json, 12)`.
    """

    chunk_note = ""
    if chunk_label:
        chunk_note = (
            f"You are translating {chunk_label} from a longer subtitle file. "
            "Return translations only for the SOURCE CUES JSON block in this request.\n"
        )

    context_note = ""
    if previous_context or next_context:
        context_note = "Neighbor context for terminology only; do not translate it:\n"
        if previous_context:
            context_note += f"Before: {previous_context}\n"
        if next_context:
            context_note += f"After: {next_context}\n"
        context_note += "\n"

    return (
        "Translate Dutch subtitle cue texts into natural, concise English.\n"
        f"{chunk_note}"
        "Preserve meaning, names, institutions, speaker intent, and political terms. "
        "Avoid literal Dutch phrasing when natural English is clearer.\n"
        "The source is a JSON array of cue objects. Each source index is the required output index. "
        "For each output item, translate only the text field of the source cue with the same index. "
        "Do not pull text from neighboring cues into the current cue, even when a sentence continues "
        "across cue boundaries. If a source cue is a fragment or starts/ends with ellipses, return a "
        "matching English fragment rather than completing it with adjacent cue text.\n\n"
        f"{context_note}"
        "Return JSON only with this shape: {\"translations\":[{\"index\":1,\"text\":\"...\"}]}.\n"
        f"Translate exactly {cue_count} cues, indexes 1 through {cue_count}. "
        "Do not merge, split, omit, add cues, or output timestamps. Keep text subtitle-length. "
        "Before returning, count the translations array and ensure it contains every requested index.\n\n"
        "SOURCE CUES JSON:\n"
        "```json\n"
        f"{source_cues_json.rstrip()}\n"
        "```"
    )


def openai_translation_repair_prompt(
    source_cues_json: str,
    cue_count: int,
    *,
    chunk_label: str | None,
    validation_errors: list[str],
    previous_context: str = "",
    next_context: str = "",
) -> str:
    """Build a narrowed repair prompt for missing translations only.

    Example: `openai_translation_repair_prompt(cue_json, 2, chunk_label=None, validation_errors=[])`.
    """

    chunk_note = ""
    if chunk_label:
        chunk_note = f"This is a repair request for {chunk_label}.\n"

    context_note = ""
    if previous_context or next_context:
        context_note = "Neighbor context for terminology only; do not translate it:\n"
        if previous_context:
            context_note += f"Before: {previous_context}\n"
        if next_context:
            context_note += f"After: {next_context}\n"
        context_note += "\n"

    error_note = ""
    if validation_errors:
        error_note = "The previous response could not be used because:\n"
        for error in validation_errors[:8]:
            error_note += f"- {error}\n"
        error_note += "\n"

    return (
        "Repair an incomplete Dutch-to-English SRT translation.\n"
        f"{chunk_note}"
        f"{error_note}"
        f"{context_note}"
        "Translate only the SOURCE CUES JSON below into natural, concise English.\n"
        "Each source index is local to this repair request and is the required output index. "
        "Translate only the text field of the source cue with the same index. Do not use neighboring "
        "cue text to complete fragments.\n"
        "Return JSON only with this shape: {\"translations\":[{\"index\":1,\"text\":\"...\"}]}.\n"
        f"Return exactly {cue_count} translations, indexes 1 through {cue_count}. "
        "Do not merge, split, omit, add cues, or output timestamps. Keep text subtitle-length. "
        "Before returning, count the translations array and ensure it contains every requested index.\n\n"
        "SOURCE CUES JSON:\n"
        "```json\n"
        f"{source_cues_json.rstrip()}\n"
        "```"
    )


def cue_context_text(cues: list[srt.SubtitleCue], max_chars: int = 1200) -> str:
    """Compact neighboring cue text for terminology context.

    Example: `cue_context_text(cues[:3])`.
    """

    text = " ".join(cue.text for cue in cues)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return textwrap.shorten(text, width=max_chars, placeholder=" ...")


def text_sha256(text: str) -> str:
    """Hash SRT source text for translation checkpoint identity.

    Example: `text_sha256(source_srt)`.
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def openai_translation_checkpoint_path(english_srt_path: Path) -> Path:
    """Place translation checkpoints beside the English sidecar.

    Example: `openai_translation_checkpoint_path(dst)`.
    """

    return english_srt_path.with_name(f"{english_srt_path.stem}.partial.json")


def openai_translation_metadata(
    source_srt: str,
    source_cues: list[srt.SubtitleCue],
    args: argparse.Namespace,
    chunk_size: int,
) -> dict[str, object]:
    """Build the metadata key that decides whether a checkpoint is reusable.

    Example: `openai_translation_metadata(text, cues, args, 120)`.
    """

    return {
        "schema": 2,
        "source_format": "cue_json_v1",
        "source_sha256": text_sha256(source_srt),
        "cue_count": len(source_cues),
        "model": getattr(args, "openai_translation_model", cfg.DEFAULT_OPENAI_TRANSLATION_MODEL),
        "reasoning_effort": getattr(args, "openai_reasoning_effort", cfg.DEFAULT_OPENAI_TRANSLATION_REASONING),
        "chunk_cues": chunk_size,
        "context_cues": int(
            getattr(args, "openai_translation_context_cues", cfg.DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES)
        ),
    }


def load_openai_translation_checkpoint(
    checkpoint_path: Path,
    metadata: dict[str, object],
) -> list[str | None] | None:
    """Load a reusable partial OpenAI translation checkpoint.

    Example: `load_openai_translation_checkpoint(path, metadata)`.
    """

    if not checkpoint_path.exists():
        return None
    try:
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("metadata") != metadata:
        return None
    translations = data.get("translations")
    cue_count = metadata.get("cue_count")
    if not isinstance(cue_count, int) or not isinstance(translations, list):
        return None
    if len(translations) != cue_count:
        return None
    if not all(item is None or isinstance(item, str) for item in translations):
        return None
    return translations


def save_openai_translation_checkpoint(
    checkpoint_path: Path,
    metadata: dict[str, object],
    translations: list[str | None],
) -> None:
    """Atomically save partial translations after each completed chunk.

    Example: `save_openai_translation_checkpoint(path, metadata, texts)`.
    """

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = checkpoint_path.with_suffix(f"{checkpoint_path.suffix}.tmp")
    payload = {
        "metadata": metadata,
        "translations": translations,
    }
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    temp_path.replace(checkpoint_path)


def openai_translation_chunk_size(args: argparse.Namespace, cue_count: int) -> int:
    """Resolve the actual chunk size, treating zero as whole-file mode.

    Example: `openai_translation_chunk_size(args, len(cues))`.
    """

    requested = int(getattr(args, "openai_translation_chunk_cues", cfg.DEFAULT_OPENAI_TRANSLATION_CHUNK_CUES))
    if requested <= 0:
        return cue_count
    return max(1, requested)


def openai_translation_payload(args: argparse.Namespace, prompt: str) -> dict[str, object]:
    """Build a Responses API payload for one translation request.

    Example: `openai_translation_payload(args, prompt)`.
    """

    return {
        "model": getattr(args, "openai_translation_model", cfg.DEFAULT_OPENAI_TRANSLATION_MODEL),
        "input": prompt,
        "reasoning": {"effort": getattr(args, "openai_reasoning_effort", cfg.DEFAULT_OPENAI_TRANSLATION_REASONING)},
        "text": {"format": openai_translation_response_format()},
        "store": False,
    }


def format_index_list(indexes: list[int], *, limit: int = 12) -> str:
    """Format cue indexes compactly for human error messages.

    Example: `format_index_list([1, 2, 3])`.
    """

    if len(indexes) <= limit:
        return ", ".join(str(index) for index in indexes)
    shown = ", ".join(str(index) for index in indexes[:limit])
    return f"{shown}, ... ({len(indexes)} total)"


def repair_cue_chunk_translations_with_openai(
    source_cues: list[srt.SubtitleCue],
    parse_result: OpenAITranslationParseResult,
    args: argparse.Namespace,
    *,
    chunk_label: str | None = None,
    previous_context: str = "",
    next_context: str = "",
) -> list[str]:
    """Ask OpenAI only for missing cue translations from an incomplete chunk.

    Example: `repair_cue_chunk_translations_with_openai(cues, result, args)`.
    """

    missing_indexes = parse_result.missing_indexes
    if not missing_indexes:
        return [text for text in parse_result.texts if text is not None]

    label = f" for {chunk_label}" if chunk_label else ""
    print(
        f"OpenAI returned an incomplete translation{label}; "
        f"requesting repair for cue(s) {format_index_list(missing_indexes)}."
    )

    missing_cues = [source_cues[index - 1] for index in missing_indexes]
    repair_source_cues = srt.openai_source_cues_json(missing_cues)
    repair_label = f"{chunk_label} repair" if chunk_label else "repair"
    payload = openai_translation_payload(
        args,
        openai_translation_repair_prompt(
            repair_source_cues,
            len(missing_cues),
            chunk_label=chunk_label,
            validation_errors=parse_result.errors,
            previous_context=previous_context,
            next_context=next_context,
        ),
    )
    response = openai_client.responses_api_request(args, payload)
    print_openai_usage(response, chunk_label=repair_label)
    repaired_texts = parse_openai_translations(openai_client.response_output_text(response), len(missing_cues))

    texts = list(parse_result.texts)
    for missing_index, repaired_text in zip(missing_indexes, repaired_texts):
        texts[missing_index - 1] = repaired_text

    still_missing = [index + 1 for index, text in enumerate(texts) if text is None]
    if still_missing:
        raise RuntimeError(
            "OpenAI translation repair did not fill cue(s): "
            f"{format_index_list(still_missing)}"
        )

    return [text for text in texts if text is not None]


def translate_cue_chunk_with_openai(
    source_cues: list[srt.SubtitleCue],
    args: argparse.Namespace,
    *,
    chunk_label: str | None = None,
    previous_context: str = "",
    next_context: str = "",
) -> list[str]:
    """Translate one chunk and repair it if the JSON is valid but incomplete.

    Example: `translate_cue_chunk_with_openai(cues, args)`.
    """

    source_cues_json = srt.openai_source_cues_json(source_cues)
    payload = openai_translation_payload(
        args,
        openai_translation_prompt(
            source_cues_json,
            len(source_cues),
            chunk_label=chunk_label,
            previous_context=previous_context,
            next_context=next_context,
        ),
    )
    response = openai_client.responses_api_request(args, payload)
    print_openai_usage(response, chunk_label=chunk_label)
    parse_result = collect_openai_translations(openai_client.response_output_text(response), len(source_cues))
    if parse_result.complete:
        return [text for text in parse_result.texts if text is not None]
    return repair_cue_chunk_translations_with_openai(
        source_cues,
        parse_result,
        args,
        chunk_label=chunk_label,
        previous_context=previous_context,
        next_context=next_context,
    )


def print_openai_usage(data: dict[str, object], *, chunk_label: str | None = None) -> None:
    """Print token usage when the Responses API returns usage accounting.

    Example: `print_openai_usage(response, chunk_label="chunk 1")`.
    """

    usage = data.get("usage")
    if not isinstance(usage, dict):
        return

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    output_details = usage.get("output_tokens_details")
    reasoning_tokens = None
    if isinstance(output_details, dict):
        reasoning_tokens = output_details.get("reasoning_tokens")

    parts: list[str] = []
    if isinstance(input_tokens, int):
        parts.append(f"input={input_tokens}")
    if isinstance(output_tokens, int):
        parts.append(f"output={output_tokens}")
    if isinstance(reasoning_tokens, int):
        parts.append(f"reasoning={reasoning_tokens}")
    if isinstance(total_tokens, int):
        parts.append(f"total={total_tokens}")
    if not parts:
        return

    label = f" for {chunk_label}" if chunk_label else ""
    print(f"OpenAI token usage{label}: " + ", ".join(parts))


def translate_cues_with_openai(
    source_srt: str,
    source_cues: list[srt.SubtitleCue],
    english_srt_path: Path,
    args: argparse.Namespace,
) -> list[str]:
    """Translate all cues using whole-file or checkpointed chunked mode.

    Example: `translate_cues_with_openai(source_srt, cues, dst, args)`.
    """

    chunk_size = openai_translation_chunk_size(args, len(source_cues))
    if chunk_size >= len(source_cues):
        return translate_cue_chunk_with_openai(source_cues, args)

    checkpoint_path = openai_translation_checkpoint_path(english_srt_path)
    metadata = openai_translation_metadata(source_srt, source_cues, args, chunk_size)
    context_cues = int(getattr(args, "openai_translation_context_cues", cfg.DEFAULT_OPENAI_TRANSLATION_CONTEXT_CUES))
    translated_texts = load_openai_translation_checkpoint(checkpoint_path, metadata)
    if translated_texts is None:
        translated_texts = [None] * len(source_cues)

    chunks = [
        (start, min(start + chunk_size, len(source_cues)))
        for start in range(0, len(source_cues), chunk_size)
    ]
    print(
        f"OpenAI translation will use {len(chunks)} chunks "
        f"of up to {chunk_size} cues; checkpoint: {checkpoint_path}"
    )

    for chunk_number, (start, end) in enumerate(chunks, start=1):
        if all(translated_texts[start:end]):
            print(f"Reusing completed OpenAI translation chunk {chunk_number}/{len(chunks)}.")
            continue

        previous_start = max(0, start - context_cues)
        next_end = min(len(source_cues), end + context_cues)
        chunk_label = f"chunk {chunk_number}/{len(chunks)} (global cues {start + 1}-{end})"
        print(f"Translating OpenAI subtitle {chunk_label}...")
        chunk_translations = translate_cue_chunk_with_openai(
            source_cues[start:end],
            args,
            chunk_label=chunk_label,
            previous_context=cue_context_text(source_cues[previous_start:start]),
            next_context=cue_context_text(source_cues[end:next_end]),
        )
        translated_texts[start:end] = chunk_translations
        save_openai_translation_checkpoint(checkpoint_path, metadata, translated_texts)

    missing_indexes = [index + 1 for index, text in enumerate(translated_texts) if text is None]
    if missing_indexes:
        raise RuntimeError(f"OpenAI translation checkpoint is incomplete at cues: {missing_indexes[:10]}")

    return [text for text in translated_texts if text is not None]


def strip_json_code_fence(text: str) -> str:
    """Accept JSON wrapped in a Markdown code fence despite strict prompting.

    Example: `strip_json_code_fence("```json\\n{}\\n```")`.
    """

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def collect_openai_translations(output_text: str, expected_count: int) -> OpenAITranslationParseResult:
    """Validate translated cue JSON while retaining partial valid entries.

    Example: `collect_openai_translations(json_text, 10)`.
    """

    try:
        payload = json.loads(strip_json_code_fence(output_text))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI returned invalid translation JSON: {exc}") from exc

    translations = payload.get("translations") if isinstance(payload, dict) else None
    if not isinstance(translations, list):
        raise RuntimeError("OpenAI translation JSON is missing a translations list.")

    texts: list[str | None] = [None] * expected_count
    errors: list[str] = []
    if len(translations) != expected_count:
        errors.append(
            f"OpenAI returned {len(translations)} translations for {expected_count} subtitle cues."
        )

    for response_index, item in enumerate(translations, start=1):
        if not isinstance(item, dict):
            errors.append(f"OpenAI translation response item #{response_index} is not an object.")
            continue
        index = item.get("index")
        text = item.get("text")
        if not isinstance(index, int) or isinstance(index, bool):
            errors.append(f"OpenAI translation response item #{response_index} has non-integer index {index!r}.")
            continue
        if index < 1 or index > expected_count:
            errors.append(
                f"OpenAI translation response item #{response_index} has out-of-range index {index!r}."
            )
            continue
        if not isinstance(text, str):
            errors.append(f"OpenAI translation #{index} text is not a string.")
            continue
        text = srt.normalize_subtitle_text(text.splitlines())
        if not text:
            errors.append(f"OpenAI translation #{index} is empty.")
            continue
        if texts[index - 1] is not None:
            errors.append(f"OpenAI returned duplicate translation index {index}.")
            continue
        texts[index - 1] = text

    return OpenAITranslationParseResult(texts=texts, errors=errors)


def parse_openai_translations(output_text: str, expected_count: int) -> list[str]:
    """Require complete translated cue JSON and return ordered text only.

    Example: `parse_openai_translations(json_text, 2)`.
    """

    result = collect_openai_translations(output_text, expected_count)
    if not result.complete:
        message = "; ".join(result.errors) if result.errors else "OpenAI returned incomplete translations."
        raise RuntimeError(
            f"{message} Missing cue(s): {format_index_list(result.missing_indexes)}."
        )
    texts = [text for text in result.texts if text is not None]
    if len(texts) != expected_count:
        raise RuntimeError(f"OpenAI returned {len(texts)} usable translations for {expected_count} subtitle cues.")
    return texts


def translate_srt_with_openai(primary_srt_path: Path, english_srt_path: Path, args: argparse.Namespace) -> None:
    """Translate a primary SRT while preserving its cue count and timings.

    Example: `translate_srt_with_openai(primary, english, args)`.
    """

    source_srt = primary_srt_path.read_text(encoding="utf-8-sig")
    source_cues = srt.parse_srt(source_srt)
    if not source_cues:
        raise RuntimeError(f"no subtitle cues found in primary SRT: {primary_srt_path}")

    translated_texts = translate_cues_with_openai(source_srt, source_cues, english_srt_path, args)
    translated_cues = [
        srt.SubtitleCue(cue.start_ms, cue.end_ms, translated_text)
        for cue, translated_text in zip(source_cues, translated_texts)
    ]

    english_srt_path.parent.mkdir(parents=True, exist_ok=True)
    english_srt_path.write_text(srt.render_srt(translated_cues, args), encoding="utf-8", newline="\n")
    checkpoint_path = openai_translation_checkpoint_path(english_srt_path)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
