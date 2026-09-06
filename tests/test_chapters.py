"""Tests for bilingual AI chapters, persistence, and exact local timing.

Example: `python -m unittest tests.test_chapters`.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from yt_whisper_subs import chapters
from yt_whisper_subs import openai_chapters
from yt_whisper_subs import openai_client
from yt_whisper_subs import srt


class ChapterTests(unittest.TestCase):
    """Exercise chapter generation without making a real OpenAI request.

    Example: `ChapterTests("test_one_hour_gets_at_least_fifteen_chapters")`.
    """

    def setUp(self) -> None:
        """Preserve the shared request function before installing a mock.

        Example: handled by `unittest`.
        """

        self._responses_api_request = openai_client.responses_api_request

    def tearDown(self) -> None:
        """Restore the shared request function after every API-free test.

        Example: handled by `unittest`.
        """

        openai_client.responses_api_request = self._responses_api_request

    def test_one_hour_gets_at_least_fifteen_chapters(self) -> None:
        """Generate a dense bilingual plan with exact transcript-derived times.

        Example: a 60-minute transcript requires no fewer than 15 chapters.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "video.srt"
            english = root / "video.en.srt"
            primary.write_text(self._hour_srt("Onderwerp"), encoding="utf-8")
            english.write_text(self._hour_srt("Topic"), encoding="utf-8")
            files = chapters.ChapterFiles.for_video(root, "video")
            observed_payloads: list[dict[str, object]] = []

            def request(args: argparse.Namespace, payload: dict[str, object]) -> dict[str, object]:
                """Return a deterministic 15-chapter structured response.

                Example: installed in place of the network client.
                """

                del args
                observed_payloads.append(payload)
                items = [
                    {
                        "segment_index": index,
                        "primary_title": f"Onderwerp {number}",
                        "english_title": f"Topic {number}",
                    }
                    for number, index in enumerate(range(1, 114, 8), start=1)
                ]
                return {"output_text": json.dumps({"chapters": items})}

            openai_client.responses_api_request = request
            source = openai_chapters.ChapterSource("video", "nl", primary, english)
            chapter_set = openai_chapters.generate_chapters(source, files, self._args())

            self.assertEqual(openai_chapters.chapter_count(3_600_000), 15)
            self.assertEqual(len(chapter_set.chapters), 15)
            self.assertEqual(chapter_set.chapters[0].start_ms, 0)
            self.assertEqual(chapter_set.chapters[1].start_ms, 240_000)
            self.assertTrue(files.archive.exists())
            self.assertTrue(files.mpv.exists())
            self.assertEqual(files.load(), chapter_set)
            payload = observed_payloads[0]
            self.assertFalse(payload["store"])
            self.assertNotIn("conversation", payload)
            self.assertNotIn("previous_response_id", payload)
            self.assertTrue(payload["text"]["format"]["strict"])

    def test_invalid_plan_is_repaired_with_a_fresh_request(self) -> None:
        """Retry once when the model returns too few chapters or a bad first index.

        Example: a malformed first plan never reaches the durable archive.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "video.srt"
            primary.write_text(self._hour_srt("Onderwerp"), encoding="utf-8")
            files = chapters.ChapterFiles.for_video(root, "video")
            responses = [
                {"chapters": [{"segment_index": 2, "primary_title": "Fout", "english_title": "Wrong"}]},
                {
                    "chapters": [
                        {
                            "segment_index": index,
                            "primary_title": f"Deel {number}",
                            "english_title": f"Part {number}",
                        }
                        for number, index in enumerate(range(1, 114, 8), start=1)
                    ]
                },
            ]
            prompts: list[str] = []

            def request(args: argparse.Namespace, payload: dict[str, object]) -> dict[str, object]:
                """Serve one invalid plan followed by a valid repair.

                Example: the second prompt includes a local validation error.
                """

                del args
                prompts.append(str(payload["input"]))
                return {"output_text": json.dumps(responses.pop(0))}

            openai_client.responses_api_request = request
            source = openai_chapters.ChapterSource("video", "nl", primary, None)
            chapter_set = openai_chapters.generate_chapters(source, files, self._args())

            self.assertEqual(len(chapter_set.chapters), 15)
            self.assertEqual(len(prompts), 2)
            self.assertIn("previous plan was unusable", prompts[1])

    def test_ffmetadata_is_bilingual_and_escaped(self) -> None:
        """Render safe mpv chapters without rewriting the downloaded container.

        Example: punctuation remains visible in the chapter title.
        """

        chapter_set = chapters.ChapterSet(
            video_id="video",
            primary_language="nl",
            model="mock",
            generated_at=1,
            duration_ms=120_000,
            chapters=[
                chapters.Chapter(0, "Vraag = antwoord", "Question = answer"),
                chapters.Chapter(60_000, "Slot", "Conclusion"),
            ],
        )

        metadata = chapters.render_ffmetadata(chapter_set)

        self.assertIn("title=Vraag \\= antwoord • Question \\= answer", metadata)
        self.assertIn("START=60000\nEND=120000", metadata)

    def test_media_duration_controls_density_beyond_last_spoken_cue(self) -> None:
        """Use the container length when speech ends before a long video does.

        Example: 50 minutes of speech in a 60-minute file still requires 15 chapters.
        """

        primary = [
            srt.SubtitleCue(index * 30_000, (index + 1) * 30_000, f"Topic {index}")
            for index in range(100)
        ]

        segments, duration_ms = openai_chapters.build_segments(primary, [], 3_600_000)

        self.assertEqual(duration_ms, 3_600_000)
        self.assertGreaterEqual(len(segments), 15)
        self.assertEqual(openai_chapters.chapter_count(duration_ms), 15)

    @staticmethod
    def _hour_srt(prefix: str) -> str:
        """Build 120 aligned 30-second cues spanning exactly one hour.

        Example: `_hour_srt("Topic")` supplies deterministic segmentation.
        """

        blocks = []
        for index in range(120):
            start = index * 30
            end = start + 30
            blocks.append(
                f"{index + 1}\n"
                f"{ChapterTests._srt_time(start)} --> {ChapterTests._srt_time(end)}\n"
                f"{prefix} {index + 1}."
            )
        return "\n\n".join(blocks) + "\n"

    @staticmethod
    def _srt_time(seconds: int) -> str:
        """Format whole seconds as an SRT timestamp for test fixtures.

        Example: `_srt_time(90)` returns `00:01:30,000`.
        """

        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02},000"

    @staticmethod
    def _args() -> argparse.Namespace:
        """Build the compact option namespace consumed by chapter generation.

        Example: `_args()` requests the default four-minute density.
        """

        return argparse.Namespace(
            chapter_minutes=4.0,
            openai_translation_model="mock",
            openai_reasoning_effort="low",
        )


if __name__ == "__main__":
    unittest.main()
