"""Structural contract for indexed translation responses, separate from semantic review.

Example: `translation_response.parse(response_text, 40)` orders translated cues.
"""

from __future__ import annotations

import argparse
import json
from typing import NamedTuple

from yt_whisper_subs import cfg
from yt_whisper_subs import openai_client
from yt_whisper_subs import srt


class ParseResult(NamedTuple):
    """Partial parse result that lets incomplete chunks be repaired cheaply.

    Example: `ParseResult([None], ["missing"])`.
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

    def complete_texts(self) -> list[str]:
        """Return non-empty translations after the caller has verified completeness.

        Example: `texts = result.complete_texts()`.
        """

        return [text for text in self.texts if text is not None]


def response_format() -> dict[str, object]:
    """Return the strict JSON schema requested from the Responses API.

    Example: `response_format()`.
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


def payload(args: argparse.Namespace, prompt: str) -> dict[str, object]:
    """Build a Responses API payload for one translation request.

    Example: `payload(args, prompt)`.
    """

    return {
        "model": getattr(args, "openai_translation_model", cfg.DEFAULT_OPENAI_TRANSLATION_MODEL),
        "input": prompt,
        "reasoning": {"effort": getattr(args, "openai_reasoning_effort", cfg.DEFAULT_OPENAI_TRANSLATION_REASONING)},
        "text": {"format": response_format()},
        "store": False,
    }


def collect(output_text: str, expected_count: int) -> ParseResult:
    """Validate translated cue JSON while retaining partial valid entries.

    Example: `collect(json_text, 10)`.
    """

    try:
        payload = json.loads(openai_client.strip_json_code_fence(output_text))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI returned invalid translation JSON: {exc}") from exc

    translations = payload.get("translations") if isinstance(payload, dict) else None
    if not isinstance(translations, list):
        raise RuntimeError("OpenAI translation JSON is missing a translations list.")

    texts: list[str | None] = [None] * expected_count
    seen: set[int] = set()
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
        if index in seen:
            errors.append(f"OpenAI returned duplicate translation index {index}.")
            texts[index - 1] = None
            continue
        seen.add(index)
        if not isinstance(text, str):
            errors.append(f"OpenAI translation #{index} text is not a string.")
            continue
        text = srt.normalize_subtitle_text(text.splitlines())
        if not text:
            errors.append(f"OpenAI translation #{index} is empty.")
            continue
        texts[index - 1] = text

    return ParseResult(texts=texts, errors=errors)


def parse(output_text: str, expected_count: int) -> list[str]:
    """Require complete translated cue JSON and return ordered text only.

    Example: `parse(json_text, 2)`.
    """

    result = collect(output_text, expected_count)
    if not result.complete or result.errors:
        message = "; ".join(result.errors) if result.errors else "OpenAI returned incomplete translations."
        raise RuntimeError(
            f"{message} Missing cue(s): {openai_client.format_index_list(result.missing_indexes)}."
        )
    texts = result.complete_texts()
    if len(texts) != expected_count:
        raise RuntimeError(f"OpenAI returned {len(texts)} usable translations for {expected_count} subtitle cues.")
    return texts


