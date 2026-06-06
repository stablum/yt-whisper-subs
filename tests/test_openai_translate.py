"""Tests for OpenAI SRT translation invariants.

Example: `python -m unittest tests.test_openai_translate`.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from yt_whisper_subs import openai_client
from yt_whisper_subs import openai_translate


class OpenAITranslateTests(unittest.TestCase):
    """Exercise the API-free translation path with mocked Responses data.

    Example: `OpenAITranslateTests("test_whole_file_translation_preserves_timings")`.
    """

    def setUp(self) -> None:
        """Save the real OpenAI request function before monkeypatching it.

        Example: handled by `unittest`.
        """

        self._responses_api_request = openai_client.responses_api_request

    def tearDown(self) -> None:
        """Restore the real OpenAI request function after each test.

        Example: handled by `unittest`.
        """

        openai_client.responses_api_request = self._responses_api_request

    def test_whole_file_translation_preserves_timings(self) -> None:
        """Translate a tiny SRT and keep source cue timestamps exactly.

        Example: one unchunked OpenAI response.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            src_path = Path(tmp_dir) / "nl.srt"
            dst_path = Path(tmp_dir) / "en.srt"
            src_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,200\nGoedemiddag allemaal.\n\n"
                "2\n00:00:01,200 --> 00:00:02,800\nWelkom bij de persconferentie.\n",
                encoding="utf-8",
            )

            openai_client.responses_api_request = self._mock_responses(
                [
                    [
                        {"index": 1, "text": "Good afternoon, everyone."},
                        {"index": 2, "text": "Welcome to the press conference."},
                    ]
                ]
            )

            openai_translate.translate_srt_with_openai(src_path, dst_path, self._args(chunk_cues=120))

            self.assertEqual(
                dst_path.read_text(encoding="utf-8"),
                "1\n00:00:00,000 --> 00:00:01,200\nGood afternoon, everyone.\n\n"
                "2\n00:00:01,200 --> 00:00:02,800\nWelcome to the press conference.\n",
            )

    def test_chunked_translation_repairs_and_removes_checkpoint(self) -> None:
        """Repair an incomplete chunk response and delete the partial checkpoint.

        Example: first chunk omits cue two, then a repair fills it.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            src_path = Path(tmp_dir) / "nl.srt"
            dst_path = Path(tmp_dir) / "en.srt"
            src_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nEen.\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nTwee.\n\n"
                "3\n00:00:02,000 --> 00:00:03,000\nDrie.\n",
                encoding="utf-8",
            )
            openai_client.responses_api_request = self._mock_responses(
                [
                    [{"index": 1, "text": "One."}],
                    [{"index": 1, "text": "Two."}],
                    [{"index": 1, "text": "Three."}],
                ]
            )

            openai_translate.translate_srt_with_openai(src_path, dst_path, self._args(chunk_cues=2))

            output = dst_path.read_text(encoding="utf-8")
            self.assertIn("00:00:01,000 --> 00:00:02,000\nTwo.", output)
            self.assertIn("00:00:02,000 --> 00:00:03,000\nThree.", output)
            checkpoint_path = openai_translate.openai_translation_checkpoint_path(dst_path)
            self.assertFalse(checkpoint_path.exists())

    def test_translation_prompt_disperses_adjacent_source_cues(self) -> None:
        """Send batched source cues in non-chronological order while preserving indexes.

        Example: four cue texts are sent as indexes 1, 3, 2, 4.
        """

        with tempfile.TemporaryDirectory() as tmp_dir:
            src_path = Path(tmp_dir) / "nl.srt"
            dst_path = Path(tmp_dir) / "en.srt"
            src_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nEen.\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nTwee.\n\n"
                "3\n00:00:02,000 --> 00:00:03,000\nDrie.\n\n"
                "4\n00:00:03,000 --> 00:00:04,000\nVier.\n",
                encoding="utf-8",
            )
            observed_indexes: list[int] = []

            def request(args: argparse.Namespace, payload: dict[str, object]) -> dict[str, object]:
                """Capture the source cue order from the generated OpenAI prompt.

                Example: invoked by `openai_translate`.
                """

                prompt = str(payload["input"])
                source_json = prompt.split("```json\n", 1)[1].split("\n```", 1)[0]
                observed_indexes.extend(item["index"] for item in json.loads(source_json))
                translations = [
                    {"index": 1, "text": "One."},
                    {"index": 2, "text": "Two."},
                    {"index": 3, "text": "Three."},
                    {"index": 4, "text": "Four."},
                ]
                return {"output_text": json.dumps({"translations": translations})}

            openai_client.responses_api_request = request

            openai_translate.translate_srt_with_openai(src_path, dst_path, self._args(chunk_cues=120))

            self.assertEqual(observed_indexes, [1, 3, 2, 4])

    def _args(self, *, chunk_cues: int) -> argparse.Namespace:
        """Build the small option namespace the translator reads.

        Example: `self._args(chunk_cues=2)`.
        """

        return argparse.Namespace(
            openai_translation_model="mock",
            openai_reasoning_effort="low",
            openai_translation_chunk_cues=chunk_cues,
            openai_translation_context_cues=1,
            compact_line_width=50,
        )

    def _mock_responses(
        self,
        translations: list[list[dict[str, object]]],
    ) -> Callable[[argparse.Namespace, dict[str, object]], dict[str, object]]:
        """Return a queue-backed Responses API stub for deterministic tests.

        Example: `openai_client.responses_api_request = self._mock_responses([[...]])`.
        """

        queued = list(translations)

        def request(args: argparse.Namespace, payload: dict[str, object]) -> dict[str, object]:
            """Pop one mocked Responses payload per OpenAI call.

            Example: invoked by `openai_translate`.
            """

            self.assertIn("SOURCE CUES JSON", payload["input"])
            self.assertTrue(queued)
            return {"output_text": json.dumps({"translations": queued.pop(0)})}

        return request


if __name__ == "__main__":
    unittest.main()
