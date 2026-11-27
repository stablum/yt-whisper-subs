"""Exercise conservative source edits and recovery without paid requests.

Example: `python -m unittest tests.test_transcription_review`.
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

from yt_whisper_subs import cfg
from yt_whisper_subs import cli
from yt_whisper_subs import library_artifacts
from yt_whisper_subs import library_types as types
from yt_whisper_subs import openai_client
from yt_whisper_subs import srt
from yt_whisper_subs import subtitle_files
from yt_whisper_subs import transcription_audio
from yt_whisper_subs import transcription_review as review
from yt_whisper_subs import yield_files
from tests import test_library as fixtures
from tests import test_yield_recovery as recovery


def edit(**changes) -> dict:
    """Supply a close Dutch ASR confusion; e.g. pain makes 'lijden' unambiguous."""

    return {"cue": 1, "word": 4, "original": "leiden", "replacement": "lijden",
            "confidence": "very_high", "context_reason": "Pain requires suffering, not leading.",
            "sound_reason": "Dutch ei and ij have the same pronunciation.", **changes}


def verdict(approve: bool = True) -> dict:
    """Build complete independent verification; e.g. reject a merely plausible alternative."""

    return {"decisions": [{"index": 1, "approve": approve, "reason": "Checked sound and context."}]}


class TranscriptionReviewTests(unittest.TestCase):
    """Test real file publication around mocked model output; e.g. no broad rewrite can reach disk."""

    def setUp(self) -> None:
        """Create an original containing punctuation and a line break; e.g. exact formatting must survive."""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self._pair = subtitle_files.SubtitlePair(root / "video.srt", root / "archive.srt")
        self._source = "1\n00:00:01,123 --> 00:00:02,456\nZe bleef erg leiden\nonder de pijn.\n\n"
        self._pair.sidecar.write_text(self._source, encoding="utf-8")
        self._pair.sync_archive()
        self._args = argparse.Namespace(language="nl")
        self._review = review.TranscriptionReview(self._pair, self._args)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def _run(self, *outputs: dict | Exception) -> mock.Mock:
        """Wrap structured replies in the normal transport; e.g. a second request can fail."""

        pending = iter(outputs)

        def request(_args, payload) -> dict:
            """Fill realistic audit coverage by default; e.g. explicit bad coverage still reaches validation."""

            out = next(pending)
            if isinstance(out, Exception):
                raise out
            if isinstance(out, dict) and "edits" in out:
                cues = json.loads(payload["input"])["cues"]
                indexes = [cue["cue"] for cue in cues if cue["editable"]]
                out = {"formatting": [], "reviewed_cues": indexes, **out}
            return {"output_text": json.dumps(out)}

        api = mock.Mock(side_effect=request)
        with mock.patch.object(openai_client, "responses_api_request", api):
            self._review.run()
        self.assertEqual(api.call_count, len(outputs))
        return api

    def test_verified_word_changes_only_exact_token_and_preserves_original(self) -> None:
        """Keep timings, punctuation, and wrapping byte-for-byte; e.g. leiden → lijden only."""

        self._review.begin()
        api = self._run({"edits": [edit()]}, verdict())
        expected = self._source.replace("leiden", "lijden")
        self.assertEqual(self._pair.sidecar.read_text(), expected)
        self.assertEqual(self._pair.archive.read_text(), expected)
        self.assertTrue(self._pair.ready())
        self.assertFalse(review.pending_path(self._pair.archive).exists())
        report = self._pair.archive.with_name("archive.srt.transcription-review.json")
        state = json.loads(report.read_text())
        self.assertEqual(state["source"], self._source)
        self.assertEqual(state["edits"][0]["replacement"], "lijden")
        for call in api.call_args_list:
            payload = call.args[1]
            self.assertEqual(payload["model"], cfg.DEFAULT_OPENAI_TRANSLATION_MODEL)
            self.assertEqual(payload["reasoning"], {"effort": "medium"})
            self.assertFalse(payload["store"])
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertNotIn("previous_response_id", payload)

    def test_clean_or_uncertain_batch_stays_identical_without_verification(self) -> None:
        """Do not manufacture corrections; e.g. uncertainty costs only the initial request."""

        for proposal in ([], [edit(confidence="uncertain")]):
            with self.subTest(proposal=proposal):
                self._review.begin()
                self._run({"edits": proposal})
                self.assertEqual(self._pair.sidecar.read_text(), self._source)

    def test_local_guard_rejects_rewrites_names_negation_and_invalid_targets(self) -> None:
        """Enforce the edit boundary independently of model instructions; e.g. no phrase substitutions."""

        bad = [edit(replacement="verdriet hebben"), edit(replacement="veranderen"),
               edit(replacement="Lijden"), edit(replacement="leiden."), edit(original="wrong"),
               edit(cue=2), edit(word=3), edit(context_reason=""), edit(sound_reason=""),
               edit(original="erg", word=3, replacement="er"),
               edit(original="leiden", replacement="leiden")]
        for proposal in bad:
            with self.subTest(proposal=proposal):
                self._review.begin()
                self._run({"edits": [proposal]})
                self.assertEqual(self._pair.sidecar.read_text(), self._source)
        for original, replacement in (("niet", "niets"), ("kan", "kon"), ("hen", "hun"),
                                      ("zeven", "geven"), ("Piet", "Pien"), ("haar", "daar"),
                                      ("hem", "hen"), ("mij", "bij"), ("nee", "neef"),
                                      ("wil", "wit"), ("their", "there")):
            cues = [srt.SubtitleCue(0, 1000, original)]
            candidate = review.Edit(**edit(word=1, original=original, replacement=replacement))
            self.assertIsNotNone(review._word_rejection(candidate, cues, range(1, 2)))

    def test_duplicate_cue_edits_are_all_rejected(self) -> None:
        """Reject duplicate proposals for the same word index, e.g. two copies of leiden → lijden."""

        self._review.begin()
        self._run({"edits": [edit(), edit()]})
        self.assertEqual(self._pair.sidecar.read_text(), self._source)

    def test_repeated_regional_word_repair_survives_verification_and_resume(self) -> None:
        """Repair both AT5 occurrences under one cue allowance, including after a failed publish."""

        source = "1\n00:04:17,140 --> 00:04:19,440\nAls jij gaat binaren,\nga ik ook binaren klaar.\n\n"
        self._pair.sidecar.write_text(source, encoding="utf-8")
        self._pair.sync_archive()
        self._review.begin()
        first = edit(word=4, original="binaren", replacement="pinaren",
                     context_reason="Pinaren means to go without in Surinamese Dutch.",
                     sound_reason="The initial consonants differ only in voicing.")
        second = {**first, "word": 8}
        decisions = verdict()["decisions"]
        decisions.append({**decisions[0], "index": 2})
        with mock.patch.object(self._review, "_publish", side_effect=OSError("File locked")):
            with self.assertRaisesRegex(OSError, "File locked"):
                self._run({"edits": [first, second]}, {"decisions": decisions})
        self.assertEqual(self._pair.sidecar.read_text(), source)
        with mock.patch.object(openai_client, "responses_api_request") as api:
            self._review.run()
        api.assert_not_called()
        expected = source.replace("binaren", "pinaren")
        self.assertEqual(self._pair.sidecar.read_text(), expected)
        self.assertEqual(self._pair.archive.read_text(), expected)

    def test_reported_compound_error_is_fixed_without_rewriting_speech(self) -> None:
        """Cover the screenshot's real failure; e.g. keep disfluency but repair middelig gebruik."""

        text = ("Iemand kan ook op een gegeven moment, ziet hij helemaal geen uitweg, "
                "en beginnen aan middelig\ngebruik.")
        source = "1\n00:03:26,860 --> 00:03:33,160\n" + text + "\n\n"
        self._pair.sidecar.write_text(source, encoding="utf-8")
        self._pair.sync_archive()
        self._review.begin()
        proposal = edit(word=16, original="middelig gebruik", replacement="middelengebruik",
                        context_reason="Substance use is meant, confirmed by the neighboring alcohol/drugs cue.",
                        sound_reason="A close sound-alike recognition also split the Dutch compound.")
        self._run({"edits": [proposal]}, verdict())
        self.assertEqual(self._pair.sidecar.read_text(), source.replace("middelig\ngebruik", "middelengebruik"))
        self.assertEqual(self._pair.archive.read_text(), self._pair.sidecar.read_text())

    def test_hyphenated_sound_alike_reaches_verifier(self) -> None:
        """Allow three letter changes inside the reported compound; e.g. ex-fro → ex-vrouw."""

        source = "1\n00:00:57,860 --> 00:01:00,020\nIk draag het huis over aan mijn ex-fro.\n\n"
        self._pair.sidecar.write_text(source, encoding="utf-8")
        self._pair.sync_archive()
        proposal = edit(word=8, original="ex-fro", replacement="ex-vrouw")
        for approve in (False, True):
            with self.subTest(approve=approve):
                self._review.begin()
                self._run({"edits": [proposal]}, verdict(approve))
                expected = source.replace("ex-fro", "ex-vrouw") if approve else source
                self.assertEqual(self._pair.sidecar.read_text(), expected)
                self.assertEqual(self._pair.archive.read_text(), expected)

    def test_capitalized_title_repairs_reach_verifier(self) -> None:
        """Cover both recognitions of the reported law title, including a compound split across lines."""

        for typo in ("warewet", "ware\nwet"):
            with self.subTest(typo=typo):
                source = f"1\n00:16:33,900 --> 00:16:40,680\ndie onder de {typo} vallen.\n\n"
                self._pair.sidecar.write_text(source, encoding="utf-8")
                self._pair.sync_archive()
                proposal = edit(word=4, original=typo.replace("\n", " "), replacement="Warenwet",
                                context_reason="The regulator is referring to the named law governing product sales.",
                                sound_reason="Whisper omitted the weak linking n.")
                for approve in (False, True):
                    self._review.begin()
                    self._run({"edits": [proposal]}, verdict(approve))
                    expected = source.replace(typo, "Warenwet") if approve else source
                    self.assertEqual(self._pair.sidecar.read_text(), expected)
                    self.assertEqual(self._pair.archive.read_text(), expected)

    def test_title_allowance_still_rejects_broader_edits(self) -> None:
        """Keep the capitalization exception narrow; e.g. no new acronym or substitution of another law."""

        for original, replacement in (
            ("warenwet", "Warenwet"),  # Pure casing goes through formatting.
            ("warewet", "WARENWET"),
            ("warewet", "WarenWet"),
            ("warewet", "Waren wet"),
            ("warewet", "Opiumwet"),
            ("Warewet", "Warenwet"),
        ):
            with self.subTest(original=original, replacement=replacement):
                proposal = review.Edit(**edit(word=1, original=original, replacement=replacement))
                cues = [srt.SubtitleCue(0, 1000, original)]
                self.assertIsNotNone(review._word_rejection(proposal, cues, range(1, 2)))

    def test_bounded_word_and_hyphen_changes(self) -> None:
        """Admit close compound repairs while retaining structural guards; e.g. do not alter names or numbers."""

        for original, replacement, allowed in (
            ("asverstrooi", "asverstrooiing", True),
            ("koerswindverhouding", "koers-winstverhouding", True),
            ("koers-winstverhouding", "koerswinstverhouding", True),
            ("haar-vro", "haar-vrouw", False),
            ("ex-Fro", "ex-vrouw", False),
            ("ex-fro", "ex-vrouw!", False),
            ("ex-fro", "ex-vrouw-kind", False),
            ("ex-fro", "ex-vrouw2", False),
            ("ex-fro", "ex-baas", False),
            ("woord", "baard", False),
        ):
            with self.subTest(original=original, replacement=replacement):
                candidate = review.Edit(**edit(word=1, original=original, replacement=replacement))
                cues = [srt.SubtitleCue(0, 1000, original)]
                self.assertEqual(review._word_rejection(candidate, cues, range(1, 2)) is None, allowed)

    def test_ineligible_edit_does_not_block_another_edit_in_same_cue(self) -> None:
        """Keep one valid candidate when a second fails locally; e.g. a short-word change is excluded."""

        self._review.begin()
        other = edit(word=3, original="erg", replacement="er")
        api = self._run({"edits": [other, edit()]}, verdict())
        doc = json.loads(api.call_args.args[1]["input"])
        self.assertEqual([item["original"] for item in doc["candidates"]], ["leiden"])
        self.assertEqual(self._pair.sidecar.read_text(), self._source.replace("leiden", "lijden"))

    def test_compound_boundaries_do_not_allow_phrase_rewrites_or_punctuation_removal(self) -> None:
        """Keep the exception within one lexical span; e.g. no crossing commas, names or negation."""

        cases = [("middelig, gebruik", "middelig gebruik", "middelengebruik"),
                 ("heel goed", "heel goed", "zeer goed"),
                 ("Middelen gebruik", "middelen gebruik", "middelengebruik"),
                 ("niet goed", "niet goed", "nietgoed"),
                 ("te veel", "te veel", "teveel"),
                 ("middelen gebruik", "middelen gebruik", "drugsgebruik"),
                 ("middelig", "middelig gebruik", "middelengebruik")]
        for text, original, replacement in cases:
            with self.subTest(text=text, replacement=replacement):
                candidate = review.Edit(**edit(word=1, original=original, replacement=replacement))
                cues = [srt.SubtitleCue(0, 1000, text)]
                self.assertIsNotNone(review._word_rejection(candidate, cues, range(1, 2)))

    def test_compound_split_and_join_preserve_surrounding_cue_text(self) -> None:
        """Permit only close boundary repairs; e.g. word positions still refer to the unedited cue."""

        for old, new in (("middelen gebruik", "middelengebruik"), ("misschienmorgen", "misschien morgen")):
            with self.subTest(old=old, new=new):
                source = self._source.replace("leiden", old)
                self._pair.sidecar.write_text(source, encoding="utf-8")
                self._pair.sync_archive()
                self._review.begin()
                self._run({"edits": [edit(original=old, replacement=new)]}, verdict())
                self.assertEqual(self._pair.sidecar.read_text(), source.replace(old, new))

    def test_review_coverage_must_include_all_and_only_editable_cues(self) -> None:
        """Reject skimmed or malformed audits before checkpointing; e.g. missing or repeated cue indexes."""

        for coverage in ([], [1, 1], [2], [True], "1"):
            with self.subTest(coverage=coverage):
                self._review.begin()
                with self.assertRaises(RuntimeError):
                    self._run({"edits": [], "reviewed_cues": coverage})
                state = json.loads(review.pending_path(self._pair.archive).read_text())
                self.assertEqual(state["next_cue"], 1)

    def test_older_pending_policy_restarts_all_cues_without_changing_the_source(self) -> None:
        """Apply the revised audit to unfinished old work; e.g. a previous clean result cannot skip the new checks."""

        self._review.begin()
        path = review.pending_path(self._pair.archive)
        state = json.loads(path.read_text())
        state["policy"].update(version=1, batch_cues=80)
        state["next_cue"] = 2
        yield_files.atomic_write(path, json.dumps(state))
        api = self._run({"edits": []})
        document = json.loads(api.call_args.args[1]["input"])
        self.assertEqual([cue["cue"] for cue in document["cues"] if cue["editable"]], [1])

    def test_independent_verifier_can_reject_candidate(self) -> None:
        """Keep the original if the second judgment doubts the edit; e.g. both words could be spoken."""

        self._review.begin()
        self._run({"edits": [edit()]}, verdict(False))
        self.assertEqual(self._pair.sidecar.read_text(), self._source)

    def test_two_to_one_requires_both_audio_and_independent_verification(self) -> None:
        """Never publish an audio hypothesis on either check alone, e.g. een hamper → amper."""

        source = "1\n00:02:54,380 --> 00:02:58,180\nWe hebben eigenlijk een hamper kunnen spelen.\n\n"
        expected_audio = "hebben eigenlijk amper kunnen spelen"
        candidate = edit(word=4, original="een hamper", replacement="amper", confidence="needs_audio")
        for audio, approve in (({}, False), ({1: expected_audio}, False), ({1: expected_audio}, True)):
            with self.subTest(audio=audio, approve=approve):
                self._pair.sidecar.write_text(source, encoding="utf-8")
                self._pair.sync_archive()
                probe = mock.Mock(spec=transcription_audio.Probe)
                probe.confirm.return_value = audio
                self._review = review.TranscriptionReview(self._pair, self._args, probe)
                self._review.begin()
                responses = ({"edits": [candidate]}, verdict(approve)) if audio else ({"edits": [candidate]},)
                api = self._run(*responses)
                requested = probe.confirm.call_args.args[0][1]
                self.assertEqual(" ".join(requested.words), expected_audio)
                self.assertEqual(requested.cue.start_ms, 174380)
                if audio:
                    payload = json.loads(api.call_args.args[1]["input"])
                    self.assertEqual(payload["candidates"][0]["audio_match"], expected_audio)
                expected = source.replace("een hamper", "amper") if approve else source
                self.assertEqual(self._pair.sidecar.read_text(), expected)
                self.assertEqual(self._pair.archive.read_text(), expected)
                audit = json.loads(self._pair.archive.with_name("archive.srt.transcription-review.json").read_text())
                self.assertEqual(audit["audio_checks"], {"1": audio.get(1)})

    def test_audio_check_cannot_drop_protected_words_or_make_bare_deletions(self) -> None:
        """Keep negation, pronouns and actual numbers protected, even for audio proposals."""

        candidates = (
            ("geen hamper", "amper", "needs_audio"),
            ("haar hamper", "amper", "needs_audio"),
            ("twee hamper", "amper", "needs_audio"),
            ("een hamer", "hamer", "needs_audio"),
            ("een hamper", "amper", "very_high"),
            ("een hamper kunnen", "amper", "needs_audio"),
            ("hamper", "amper", "needs_audio"),
            ("stoïs zijn", "stoïs", "needs_audio"),
            ("stoïs zijn", "stoïcijns", "very_high"),
            ("stoïs geen", "stoïcijns", "needs_audio"),
        )
        for original, replacement, confidence in candidates:
            with self.subTest(original=original, confidence=confidence):
                cues = [srt.SubtitleCue(0, 1000, f"We hebben {original} gespeeld.")]
                candidate = review.Edit(**edit(word=3, original=original, replacement=replacement, confidence=confidence))
                self.assertIsNotNone(review._word_rejection(candidate, cues, range(1, 2)))

    def test_audio_completes_partial_split_and_prioritizes_verified_span(self) -> None:
        """Finish an incomplete proposal using raw timing, while a text-only rival waits."""

        source = ("1\n00:04:12,440 --> 00:04:18,780\n"
                  "eigenlijk niks zei en vooral heel stoïs zijn\nurenlang voor schuit kon staren.\n\n")
        raw = "1\n00:04:12,440 --> 00:04:14,400\neigenlijk niks zei en vooral heel stoïs zijn\n\n"
        partial = edit(word=7, original="stoïs", replacement="stoïcijns")
        whole = {**partial, "original": "stoïs zijn", "confidence": "needs_audio"}
        other = edit(word=11, original="schuit", replacement="zich uit")
        exact = "vooral heel stoïcijns urenlang"
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                self._pair.sidecar.write_text(source, encoding="utf-8")
                self._pair.sync_archive()
                raw_path = subtitle_files.uncompacted_backup_path(self._pair.archive)
                raw_path.write_text(raw, encoding="utf-8")
                probe = mock.Mock(spec=transcription_audio.Probe)
                probe.confirm.return_value = {1: exact} if confirmed else {}
                self._review = review.TranscriptionReview(self._pair, self._args, probe)
                self._review.begin()
                api = self._run({"edits": [partial, whole, other]}, verdict(confirmed))
                match = probe.confirm.call_args.args[0][1]
                self.assertEqual(match.focus, (252440, 254400))
                self.assertEqual(" ".join(match.words), exact)
                candidate = json.loads(api.call_args.args[1]["input"])["candidates"][0]
                self.assertEqual(candidate["original"], "stoïs zijn" if confirmed else "schuit")
                expected = source.replace("stoïs zijn", "stoïcijns") if confirmed else source
                self.assertEqual(self._pair.sidecar.read_text(encoding="utf-8"), expected)

    def test_partial_proposal_does_not_cross_punctuation(self) -> None:
        """Scope expansion stays within a single exact split phrase."""

        cues = [srt.SubtitleCue(0, 3000, "vooral heel stoïs, zijn urenlang op pad")]
        proposal = review.Edit(**edit(word=3, original="stoïs", replacement="stoïcijns"))
        self.assertEqual(review._complete_split(proposal, cues, range(1, 2)), proposal)

    def test_focused_repair_cannot_pick_between_identical_anchors(self) -> None:
        """Abstain if the audio window could point to another instance of the same split."""

        phrase = "vooral heel stoïs zijn urenlang"
        cue = srt.SubtitleCue(0, 6000, f"{phrase}, {phrase}")
        proposal = review.Edit(**edit(word=3, original="stoïs zijn", replacement="stoïcijns", confidence="needs_audio"))
        self.assertIsNone(review._audio_match(cue, proposal))

    def test_resume_preserves_audio_evidence_and_budget(self) -> None:
        """Resume after a verified batch without decoding its audio twice."""

        source = ("1\n00:00:01,000 --> 00:00:03,000\nWe hebben eigenlijk een hamper kunnen spelen.\n\n"
                  "2\n00:00:03,000 --> 00:00:04,000\nWe hebben een hapart geleerd.\n\n")
        self._pair.sidecar.write_text(source, encoding="utf-8")
        self._pair.sync_archive()
        probe = mock.Mock(spec=transcription_audio.Probe)
        probe.confirm.return_value = {1: "hebben eigenlijk amper kunnen spelen"}
        self._review = review.TranscriptionReview(self._pair, self._args, probe)
        first = edit(word=4, original="een hamper", replacement="amper", confidence="needs_audio")
        second = edit(cue=2, word=3, original="een hapart", replacement="apart", confidence="needs_audio")
        with mock.patch.object(review, "BATCH_CUES", 1), mock.patch.object(transcription_audio, "MAX_CHECKS", 1):
            self._review.begin()
            with self.assertRaisesRegex(RuntimeError, "offline"):
                self._run({"edits": [first]}, verdict(), RuntimeError("offline"))
            state_path = review.pending_path(self._pair.archive)
            state = json.loads(state_path.read_text())
            self.assertEqual(state["audio_checks"], {"1": "hebben eigenlijk amper kunnen spelen"})
            yield_files.atomic_write(state_path, json.dumps({**state, "audio_checks": {}}))
            with self.assertRaisesRegex(RuntimeError, "Missing audio evidence"):
                self._review.run()
            yield_files.atomic_write(state_path, json.dumps(state))
            self._run({"edits": [second]})
        probe.confirm.assert_called_once()
        self.assertEqual(self._pair.sidecar.read_text(), source.replace("een hamper", "amper"))

    def test_invalid_or_failed_reviews_leave_pending_original(self) -> None:
        """Refuse malformed coverage or truncated requests; e.g. a verifier cannot silently omit candidates."""

        responses = [({"oops": []},), ({"edits": [{"cue": True}]},),
                     ({"edits": [edit()]}, {"decisions": []}),
                     ({"edits": [edit()]}, RuntimeError("network failed"))]
        for outputs in responses:
            with self.subTest(outputs=outputs):
                self._review.begin()
                with self.assertRaises(RuntimeError):
                    self._run(*outputs)
                self.assertEqual(self._pair.sidecar.read_text(), self._source)
                self.assertTrue(review.pending_path(self._pair.archive).exists())
                self.assertFalse(self._pair.ready())

    def test_resume_reuses_only_completed_batches_and_original_context(self) -> None:
        """Avoid repeat charges and chained corrections; e.g. fail the second two-cue batch then resume."""

        content = self._source + "2\n00:00:03,000 --> 00:00:04,000\nNog een zin.\n\n"
        self._pair.sidecar.write_text(content, encoding="utf-8")
        self._pair.sync_archive()
        with mock.patch.object(review, "BATCH_CUES", 1):
            self._review.begin()
            with self.assertRaisesRegex(RuntimeError, "offline"):
                self._run({"edits": [edit()]}, verdict(), RuntimeError("offline"))
            self.assertEqual(self._pair.sidecar.read_text(), content)
            api = self._run({"edits": []})
        document = json.loads(api.call_args.args[1]["input"])
        self.assertEqual([cue["cue"] for cue in document["cues"] if cue["editable"]], [2])
        self.assertIn("leiden", document["cues"][0]["text"])
        self.assertEqual(self._pair.sidecar.read_text(), content.replace("leiden", "lijden"))

    def test_excessive_edits_reject_whole_batch(self) -> None:
        """Count only verified edits toward the density cap; e.g. one of two proposals can survive."""

        source = self._source + self._source.replace("1\n00:", "2\n00:")
        for approve_both in (False, True):
            with self.subTest(approve_both=approve_both):
                self._pair.sidecar.write_text(source, encoding="utf-8")
                self._pair.sync_archive()
                self._review.begin()
                decisions = verdict()
                decisions["decisions"].append({"index": 2, "approve": approve_both, "reason": "Checked context."})
                self._run({"edits": [edit(), edit(cue=2)]}, decisions)
                expected = source if approve_both else source.replace("leiden", "lijden", 1)
                self.assertEqual(self._pair.sidecar.read_text(), expected)

    def test_edit_allowance_rounds_up_for_partial_groups(self) -> None:
        """Avoid a gap in proportionate allowances; e.g. 101 words can contain two verified errors."""

        for words, limit in ((0, 1), (100, 1), (101, 2), (200, 2), (201, 3)):
            with self.subTest(words=words):
                cues = [srt.SubtitleCue(0, 1000, "tekst " * words)]
                self.assertEqual(review._edit_limit(cues), limit)

    def test_external_edits_and_corrupt_progress_are_preserved(self) -> None:
        """Do not overwrite manual work or silently skip cues; e.g. malformed checkpoint bounds."""

        self._review.begin()
        self._pair.sidecar.write_text(self._source.replace("erg", "heel"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "changed during review"):
            self._review.run()
        self.assertIn("heel", self._pair.sidecar.read_text())
        self._pair.sidecar.write_text(self._source, encoding="utf-8")
        path = review.pending_path(self._pair.archive)
        state = json.loads(path.read_text())
        state["next_cue"] = 99
        yield_files.atomic_write(path, json.dumps(state))
        with self.assertRaisesRegex(RuntimeError, "checkpoint progress"):
            self._review.run()

    def test_punctuation_and_capitalization_preserve_words_and_line_breaks(self) -> None:
        """Allow the requested cleanup without rephrasing; e.g. lowercase start plus a missing period."""

        source = self._source.replace("Ze bleef erg leiden\nonder de pijn.", "ze bleef erg leiden\nonder de pijn")
        self._pair.sidecar.write_text(source, encoding="utf-8")
        self._pair.sync_archive()
        self._review.begin()
        fmt = {"cue": 1, "original": "ze bleef erg leiden onder de pijn",
               "replacement": "Ze bleef erg leiden onder de pijn.", "reason": "Sentence start and end."}
        decisions = {"decisions": [{"index": i, "approve": True, "reason": "Clear correction."} for i in (1, 2)]}
        self._run({"edits": [edit()], "formatting": [fmt]}, decisions)
        self.assertEqual(self._pair.sidecar.read_text(), self._source.replace("leiden", "lijden"))

    def test_formatting_cannot_smuggle_word_quantity_or_spacing_changes(self) -> None:
        """Keep cleanup lexically neutral; e.g. no synonyms, deleted negation or altered decimal numbers."""

        cases = [("het doet pijn", "Het doet zeer."), ("dit is niet goed", "Dit is goed."),
                 ("we hebben 1,5 liter", "We hebben 15 liter."), ("we hebben 1.000 euro", "We hebben 1,000 euro."),
                 ("het is goed", "HET IS GOED!"), ("het is goed", "Het is goed!!!!!"),
                 ("het is goed", "Het  is goed."), ("dat kan niet", "Dat kan-niet."),
                 ("a, b c, d e, f", "a b, c d, e f,"),
                 ("het doet pijn", "Het doet lijden")]
        for original, replacement in cases:
            with self.subTest(original=original, replacement=replacement):
                fmt = review.FormatEdit(1, original, replacement, "Claimed cleanup.")
                self.assertFalse(review._format_safe(fmt, [srt.SubtitleCue(0, 1000, original)], range(1, 2)))

    def test_formatting_alone_is_independently_verified(self) -> None:
        """Do not charge formatting against the rare word budget; e.g. a clear missing comma."""

        self._review.begin()
        fmt = {"cue": 1, "original": "Ze bleef erg leiden onder de pijn.",
               "replacement": "Ze bleef erg leiden, onder de pijn.", "reason": "Comma separates a phrase."}
        self._run({"edits": [], "formatting": [fmt]}, verdict(False))
        self.assertEqual(self._pair.sidecar.read_text(), self._source)

    def test_long_cue_can_pass_more_than_four_punctuation_edits(self) -> None:
        """Permit several boundaries in long speech; e.g. a recap with four commas and a period."""

        original = ("Na afloop zag ik Emma bij de ingang daarna sprak ik Noor bij de uitgang "
                    "toen kwam ik thuis aten we samen en gingen we naar bed")
        replacement = ("Na afloop zag ik Emma bij de ingang, daarna sprak ik Noor bij de uitgang, "
                       "toen kwam ik thuis, aten we samen, en gingen we naar bed.")
        source = self._source.replace("Ze bleef erg leiden\nonder de pijn.", original)
        self._pair.sidecar.write_text(source, encoding="utf-8")
        self._pair.sync_archive()
        self._review.begin()
        fmt = {"cue": 1, "original": original, "replacement": replacement, "reason": "Clear pauses."}
        self._run({"edits": [], "formatting": [fmt]}, verdict())
        self.assertEqual(self._pair.sidecar.read_text(), source.replace(original, replacement))

    def test_publish_failure_and_policy_change_do_not_retain_unreported_edits(self) -> None:
        """Resume a partially published pair safely; e.g. the new model rejects the old suggestion."""

        self._review.begin()
        with mock.patch.object(subtitle_files.SubtitlePair, "sync_archive", side_effect=OSError("disk failed")):
            with self.assertRaisesRegex(OSError, "disk failed"):
                self._run({"edits": [edit()]}, verdict())
        self.assertIn("lijden", self._pair.sidecar.read_text())
        self.assertIn("leiden", self._pair.archive.read_text())
        self._args.openai_translation_model = "different-model"
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self._run(RuntimeError("offline"))
        self._run({"edits": []})
        self.assertEqual(self._pair.sidecar.read_text(), self._source)
        self.assertEqual(self._pair.archive.read_text(), self._source)

    def test_explicit_fresh_review_retains_a_differing_archive_for_audit(self) -> None:
        """Make the suggested recovery actionable; e.g. review a manually edited sidecar without losing the old archive."""

        changed = self._source.replace("erg", "heel")
        self._pair.sidecar.write_text(changed, encoding="utf-8")
        self._review.begin()
        self._run({"edits": []})
        self.assertEqual(self._pair.archive.read_text(), changed)
        report = self._pair.archive.with_name("archive.srt.transcription-review.json")
        self.assertEqual(json.loads(report.read_text())["previous_archive"], self._source)


class PipelineReviewTests(recovery.YieldRecoveryFixture, unittest.TestCase):
    """Exercise shared CLI/library routing; e.g. failed review must precede translation and chapters."""

    def test_review_failure_resumes_without_whisper_or_downstream_publication(self) -> None:
        """Keep primary pending and resume its audit; e.g. no repeat transcription after an API outage."""

        with mock.patch.object(openai_client, "responses_api_request", side_effect=RuntimeError("review failed")), \
                mock.patch.object(self.runner, "_generate_english_subs") as translate:
            with self.assertRaisesRegex(RuntimeError, "review failed"):
                self.runner.run()
        self.whisper.assert_called_once()
        translate.assert_not_called()
        self.assertFalse(self.yields.all_ready())
        self.assertTrue(review.pending_path(self.yields.primary.archive).exists())
        with mock.patch.object(self.runner.__class__, "_generate_openai_english_subs",
                               side_effect=self._finish_english), \
                mock.patch.object(self.runner.__class__, "_generate_chapters", side_effect=self._finish_chapters):
            self.assertEqual(self._runner().run(), 0)
        self.whisper.assert_called_once()
        self.assertTrue(self.yields.all_ready())

    def _finish_english(self, *_args, **_kwargs) -> None:
        """Finalize a local English fixture; e.g. isolate source audit from translation transport."""

        self._translate(None, self.yields.english.sidecar, None)
        self.yields.english.sync_archive()
        self.yields.english.complete()

    def _finish_chapters(self, *_args, **_kwargs) -> None:
        """Finalize a chapter fixture; e.g. confirm pending state no longer blocks the run."""

        self.yields.chapters.write(self._plan("New chapter"))

    def test_existing_subtitles_stay_cheap_unless_explicitly_requested(self) -> None:
        """Review cached primary only on demand; e.g. --force-transcription-review skips Whisper."""

        self.yields.primary.sidecar.write_text(recovery.cue("Dutch"), encoding="utf-8")
        self.yields.primary.sync_archive()
        self.args.english_for_dutch = False
        self.args.chapters = False
        with mock.patch.object(openai_client, "responses_api_request", return_value={"output_text": '{"edits":[],"formatting":[],"reviewed_cues":[1]}'}) as api:
            self.assertEqual(self.runner.run(), 0)
            api.assert_not_called()
            self.args.force_transcription_review = True
            self.assertEqual(self._runner().run(), 0)
            api.assert_called_once()
        self.whisper.assert_not_called()
        self.assertFalse(self.yields.english.ready())
        self.assertFalse(self.yields.chapters.ready())

    def test_local_only_opt_out_skips_api(self) -> None:
        """Retain a fully local transcription path; e.g. --no-transcription-review --no-english-for-dutch."""

        self.args.transcription_review = False
        self.args.english_for_dutch = False
        self.args.chapters = False
        with mock.patch.object(openai_client, "responses_api_request") as api:
            self.assertEqual(self.runner.run(), 0)
        api.assert_not_called()
        self.assertTrue(self.yields.primary.ready())

    def test_incompatible_cli_flags_fail_early(self) -> None:
        """Reject contradictory requests before any tool runs; e.g. review of translated audio."""

        for flags in (("--no-transcription-review",), ("--task", "translate")):
            argv = ["test", "video.mkv", "--force-transcription-review", *flags]
            with self.subTest(flags=flags), mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parse_args()

    def test_pending_review_updates_gui_health_without_retranscription(self) -> None:
        """Expose unfinished audit as a repairable issue; e.g. its marker invalidates a cached healthy row."""

        self.yields.primary.sidecar.write_text(recovery.cue("Dutch"), encoding="utf-8")
        self.yields.primary.sync_archive()
        record = types.VideoRecord(fixtures.make_meta("aaaaaaaaaaa", "Example"), None, 1,
            types.LocalMedia(self.video, 1, 5), None, None)
        cache = library_artifacts.ArtifactCache(self.root)
        self.assertIsNone(cache.issue(record))
        review.TranscriptionReview(self.yields.primary, self.args).begin()
        self.assertEqual(cache.issue(record), "transcription review needs completion")

    def test_corrected_primary_feeds_translation_and_chapters(self) -> None:
        """Publish audio-supported text and formatting before translation and chapters."""

        source = "we hebben eigenlijk een hamper kunnen spelen"
        expected = "We hebben eigenlijk amper kunnen spelen."
        self.whisper.side_effect = lambda _audio, target, *_args: target.write_text(recovery.cue(source), encoding="utf-8")
        fmt = {"cue": 1, "original": source, "replacement": source.capitalize() + ".", "reason": "Sentence casing/end."}
        decisions = {"decisions": [{"index": i, "approve": True, "reason": "Clear correction."} for i in (1, 2)]}
        candidate = edit(word=4, original="een hamper", replacement="amper", confidence="needs_audio")
        proposals = {"edits": [candidate], "formatting": [fmt], "reviewed_cues": [1]}
        replies = [{"output_text": json.dumps(out)} for out in (proposals, decisions)]

        def translate(primary, target, args) -> None:
            """Assert the real translation boundary sees final text; e.g. no raw ASR mistake escapes."""

            self.assertEqual(srt.parse_srt(primary.read_text())[0].text, expected)
            self._translate(primary, target, args)

        def chapter(source, files, args):
            """Assert chapters follow both successful subtitle stages; e.g. ready state is not premature."""

            self.assertEqual(srt.parse_srt(self.yields.primary.archive.read_text())[0].text, expected)
            self.assertTrue(self.yields.english.ready())
            return self._chapter(source, files, args)

        with mock.patch.object(openai_client, "responses_api_request", side_effect=replies), \
                mock.patch.object(transcription_audio.Probe, "confirm", autospec=True,
                                  return_value={1: "hebben eigenlijk amper kunnen spelen"}) as probe, \
                mock.patch("yt_whisper_subs.openai_translate.translate_srt_with_openai", side_effect=translate), \
                mock.patch("yt_whisper_subs.openai_chapters.generate_chapters", side_effect=chapter):
            self.assertEqual(self.runner.run(), 0)
        self.assertEqual(probe.call_args.args[0]._media, self.video)
        self.assertTrue(self.yields.all_ready())
