"""Exercise semantic correction, publication and resume boundaries without network calls.

Example: `python -m unittest tests.test_translation_review`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import cli
from yt_whisper_subs import openai_client
from yt_whisper_subs import openai_translate
from yt_whisper_subs import srt
from yt_whisper_subs import translation_response
from yt_whisper_subs import translation_review


def translations(*texts: str) -> dict:
    """Build indexed provider replies in tests; e.g. `translations('One.', 'Two.')`."""

    return {"translations": [{"index": idx, "text": text} for idx, text in enumerate(texts, 1)]}


def review(indexes: list[int], *bad: int) -> dict:
    """Give explicit review coverage and findings; e.g. flag both indexes in a swapped pair."""

    return {
        "reviewed_indexes": indexes,
        "issues": [{"index": idx, "kind": "alignment", "reason": "The meaning belongs to its neighbor."}
                   for idx in bad],
    }


class ReviewFlowTests(unittest.TestCase):
    """Use the real renderer and checkpoints around a queued provider; e.g. a failed second chunk."""

    def setUp(self) -> None:
        """Create independent source/output files; e.g. the English file may already contain a usable yield."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._src, self._dst = Path(tmp.name) / "nl.srt", Path(tmp.name) / "en.srt"
        self._args = argparse.Namespace(openai_translation_chunk_cues=120, compact_line_width=50)
        self._cues = [srt.SubtitleCue(idx * 1000, idx * 1000 + 900, text)
                      for idx, text in enumerate(("Een.", "Twee.", "Drie.", "Vier."))]
        self._src.write_text(srt.render_srt(self._cues, self._args), encoding="utf-8")

    def _run(self, *replies: dict | Exception) -> None:
        """Drive the full translation pipeline through exact replies; e.g. malformed correction JSON."""

        responses = [reply if isinstance(reply, Exception) else {"output_text": json.dumps(reply)}
                     for reply in replies]
        self._api = mock.Mock(side_effect=responses)
        with mock.patch.object(openai_client, "responses_api_request", self._api), contextlib.redirect_stdout(io.StringIO()):
            openai_translate.translate_srt_with_openai(self._src, self._dst, self._args)
        self.assertEqual(self._api.call_count, len(replies))

    def test_swap_correction_preserves_other_cues_and_timings(self) -> None:
        """Repair a swapped span, then independently review again; e.g. a wrong cue index."""

        self._run(
            translations("Two.", "One.", "Three.", "Four."), review([1, 2, 3, 4], 1, 2),
            {"translations": [{"index": 2, "text": "Two."}, {"index": 1, "text": "One."},
                              {"index": 3, "text": "Three."}]},
            review([1, 2, 3, 4]),
        )
        cues = srt.parse_srt(self._dst.read_text(encoding="utf-8"))
        self.assertEqual([cue.text for cue in cues], ["One.", "Two.", "Three.", "Four."])
        self.assertEqual([(c.start_ms, c.end_ms) for c in cues], [(c.start_ms, c.end_ms) for c in self._cues])
        repair = self._api.call_args_list[2].args[1]
        targets = json.loads(repair["input"].split("\n", 1)[1])["targets"]
        self.assertEqual([target["source"] for target in targets], ["Een.", "Twee.", "Drie."])
        for call in self._api.call_args_list:
            self.assertEqual(call.args[1]["model"], "gpt-6-luna")
            self.assertEqual(call.args[1]["reasoning"], {"effort": "medium"})
            self.assertFalse(call.args[1]["store"])

    def test_unresolved_errors_do_not_replace_existing_english(self) -> None:
        """Bound repeated correction and preserve a prior yield; e.g. a reviewer keeps rejecting cue one."""

        original = b"existing English yield\n"
        self._dst.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "after 2 corrections"):
            self._run(
                translations("Wrong.", "Two.", "Three.", "Four."), review([1, 2, 3, 4], 1),
                translations("Still wrong.", "Two."), review([1, 2, 3], 1),
                translations("Wrong again.", "Two."), review([1, 2, 3], 1),
            )
        self.assertEqual(self._api.call_count, 6)
        self.assertEqual(self._dst.read_bytes(), original)
        self.assertFalse(openai_translate.openai_translation_checkpoint_path(self._dst).exists())

    def test_resume_reuses_only_reviewed_chunks(self) -> None:
        """Retain a passed chunk while a later correction fails; e.g. resume without paying for it again."""

        self._args.openai_translation_chunk_cues = 2
        with self.assertRaisesRegex(RuntimeError, "Missing cue"):
            self._run(
                translations("One.", "Two."), review([1, 2]),
                translations("Four.", "Three."), review([1, 2], 1, 2), translations(),
            )
        path = openai_translate.openai_translation_checkpoint_path(self._dst)
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["translations"], ["One.", "Two.", None, None])
        self.assertEqual(checkpoint["metadata"]["semantic_review"], translation_review.PROMPT_VERSION)
        self.assertFalse(self._dst.exists())
        self._run(translations("Three.", "Four."), review([1, 2]))
        self.assertFalse(path.exists())
        self.assertEqual([c.text for c in srt.parse_srt(self._dst.read_text())], ["One.", "Two.", "Three.", "Four."])

    def test_incomplete_review_never_counts_as_success(self) -> None:
        """Reject an empty or partially covered approval; e.g. a provider forgets to review the last cue."""

        for reply in (review([]), review([1, 2, 3]), review([1, 2, 3, 4, 4]), {}):
            with self.subTest(reply=reply), self.assertRaisesRegex(RuntimeError, "semantic review"):
                self._run(translations("One.", "Two.", "Three.", "Four."), reply)
            self.assertFalse(self._dst.exists())

    def test_review_network_failure_preserves_completed_work(self) -> None:
        """Surface an unavailable reviewer without publishing unchecked text; e.g. a timeout after generation."""

        with self.assertRaisesRegex(RuntimeError, "review timeout"):
            self._run(translations("One.", "Two.", "Three.", "Four."), RuntimeError("review timeout"))
        self.assertFalse(self._dst.exists())

    def test_missing_translation_is_repaired_before_review(self) -> None:
        """Compose structural repair and semantic review; e.g. only the second source cue is missing."""

        self._run(
            {"translations": [{"index": 1, "text": "One."}, {"index": 3, "text": "Three."},
                              {"index": 4, "text": "Four."}]},
            translations("Two."), review([1, 2, 3, 4]),
        )
        pairs = json.loads(self._api.call_args_list[-1].args[1]["input"])["review"]
        self.assertEqual([p["translation"] for p in pairs], ["One.", "Two.", "Three.", "Four."])

    def test_checkpoint_identity_excludes_unreviewed_or_different_policy(self) -> None:
        """Invalidate old review policy, model and reasoning; e.g. a pre-review completed checkpoint."""

        checkpoint = openai_translate.TranslationCheckpoint.for_run(
            self._src.read_text(), self._cues, self._dst, self._args, 120,
        )
        for key, value in (("schema", 2), ("semantic_review", -1), ("model", "gpt-5-mini"),
                           ("reasoning_effort", "low")):
            with self.subTest(key=key):
                altered = {**checkpoint.metadata, key: value}
                data = {"metadata": altered, "translations": ["One.", "Two.", "Three.", "Four."]}
                checkpoint.path.write_text(json.dumps(data), encoding="utf-8")
                self.assertIsNone(checkpoint.load())
        checkpoint.save(["One.", "Two.", "Three.", "Four."])
        with mock.patch.object(openai_client, "responses_api_request", side_effect=AssertionError("unexpected API")):
            openai_translate.translate_srt_with_openai(self._src, self._dst, self._args)

    def test_whole_file_mode_is_reviewed_and_checkpointed(self) -> None:
        """Zero chunk size changes batching only; e.g. one whole-file request still needs review."""

        self._args.openai_translation_chunk_cues = 0
        self._run(translations("One.", "Two.", "Three.", "Four."), review([1, 2, 3, 4]))
        self.assertTrue(self._dst.exists())


class ReviewContractTests(unittest.TestCase):
    """Keep batch indexing, malformed replies and model settings explicit; e.g. forty-cue boundaries."""

    def test_batch_context_and_sparse_repairs_keep_original_indexes(self) -> None:
        """Check both sides of a review boundary and only replace sparse targets; e.g. cues 40 and 42."""

        cues = [srt.SubtitleCue(idx, idx + 1, f"Bron {idx}.") for idx in range(1, 44)]
        texts = [f"Text {idx}." for idx in range(1, 44)]
        replies = [
            review(list(range(1, 41)), 40), review([41, 42, 43], 42),
            translations("Text 39.", "Fixed 40.", "Text 41.", "Fixed 42.", "Text 43."),
            review([38, 39, 40]), review([41, 42, 43]),
        ]
        api = mock.Mock(side_effect=[{"output_text": json.dumps(reply)} for reply in replies])
        reviewer = translation_review.TranslationReview(argparse.Namespace())
        with mock.patch.object(openai_client, "responses_api_request", api), contextlib.redirect_stdout(io.StringIO()):
            result = reviewer.check_and_repair(cues, texts, context=("Earlier fragment", "Following fragment"))
        self.assertEqual([idx for idx, (a, b) in enumerate(zip(texts, result), 1) if a != b], [40, 42])
        second = json.loads(api.call_args_list[1].args[1]["input"])
        self.assertEqual([pair["index"] for pair in second["before"]], [38, 39, 40])
        self.assertEqual(second["outside_source_context"]["after_chunk"], "Following fragment")
        final = json.loads(api.call_args_list[-1].args[1]["input"])["review"]
        self.assertEqual(final[1]["translation"], "Fixed 42.")

    def test_malformed_review_findings_fail_closed(self) -> None:
        """Reject invalid indexes, duplicate findings and empty reasons; e.g. a boolean masquerading as one."""

        cases = [
            {"reviewed_indexes": [True], "issues": []}, {"reviewed_indexes": [1], "issues": None},
            review([1], 2), review([1], 1, 1),
            {"reviewed_indexes": [1], "issues": [{"index": 1, "kind": "style", "reason": "Cosmetic"}]},
            {"reviewed_indexes": [1], "issues": [{"index": 1, "kind": "omission", "reason": " "}]},
            {"reviewed_indexes": [1], "issues": [{"index": True, "kind": "omission", "reason": "No"}]},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                translation_review.parse_review(json.dumps(case), [1])
        with self.assertRaisesRegex(RuntimeError, "JSON"):
            translation_review.parse_review("not JSON", [1])

    def test_ambiguous_duplicate_translation_requires_repair(self) -> None:
        """Do not pick arbitrarily between duplicate translations; e.g. two conflicting texts at index one."""

        output = json.dumps({"translations": [{"index": 1, "text": "One"}, {"index": 1, "text": "Two"}]})
        result = translation_response.collect(output, 1)
        self.assertEqual(result.missing_indexes, [1])
        with self.assertRaisesRegex(RuntimeError, "duplicate translation index"):
            translation_response.parse(output, 1)

    def test_cli_luna_defaults_and_reasoning_validation(self) -> None:
        """Accept Luna's max effort and reject its unsupported minimal effort; e.g. explicit CLI overrides."""

        for flags, effort in (([], "medium"), (["--openai-reasoning-effort", "max"], "max")):
            with mock.patch.object(sys, "argv", ["test", "video.mkv", *flags]):
                args = cli.parse_args()
            self.assertEqual((args.openai_translation_model, args.openai_reasoning_effort), ("gpt-6-luna", effort))
        with mock.patch.object(sys, "argv", ["test", "video.mkv", "--openai-reasoning-effort", "minimal"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parse_args()
        with mock.patch.object(sys, "argv", ["test", "video.mkv", "--openai-translation-model", "gpt-5-mini",
                                            "--openai-reasoning-effort", "minimal"]):
            args = cli.parse_args()
        self.assertEqual(args.openai_translation_model, "gpt-5-mini")


if __name__ == "__main__":
    unittest.main()
