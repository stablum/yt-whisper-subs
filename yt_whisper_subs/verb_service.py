"""Coordinate adaptive target selection, checked content and durable answer grading.

Example: `VerbService(db).start(Options(), report)` prepares a round without exposing its answers in the UI.
"""

from __future__ import annotations

import operator
import time

from yt_whisper_subs import i18n
from yt_whisper_subs import practice_round
from yt_whisper_subs import practice
from yt_whisper_subs import practice_ready
from yt_whisper_subs import verb_generate
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_stats
from yt_whisper_subs import verb_syllabus
from yt_whisper_subs import verb_vocabulary


class VerbService:
    """Keep language-provider calls outside SQL transactions and native widgets."""

    def __init__(self, db, provider: verb_generate.Provider | None = None) -> None:
        """Inject an isolated provider when needed; e.g. tests never spend API credits."""

        self._db = db
        self._provider = provider or verb_generate.VerbGenerator()
        self._vocabulary = verb_vocabulary.VocabularySync(db, self._provider)

    def warm_vocabulary(self, report) -> bool:
        """Fill a bounded slice on its own lease, even when a standby round already exists."""

        status = self._db.practice_prepare_status(verb_vocabulary.TRACK)
        if max(status["lease_until"], status["next_at"]) > time.time():
            return False
        if not self._vocabulary.sync(report, batches=2):
            return False
        self._db.practice_prepare_reconsider("verb")
        return True

    @practice_ready.serialized("verb", verb_generate.PROMPT_VERSION)
    def start(self, opts: verbs.Options, report, *, cached_first: bool = True, background: bool = False) -> str:
        """Build the whole approved round before replacing another; e.g. failures preserve existing practice."""

        policy = practice.ReviewPolicy()
        if opts.source != "starter":
            self._vocabulary.link_known()
        generation = practice_ready.question_policy(self)
        unused = self._db.practice_unused("verb", generation)
        selector, metrics = verb_syllabus.Selector(policy), verb_stats.metrics(self._db)
        candidates = self._candidates(opts, unused, background=background)
        selected = selector.select(candidates, metrics, opts, time.time())
        while not background and opts.source != "starter" and len(selected) < opts.count:
            if not self._vocabulary.sync(report, batches=1):
                break
            candidates = self._candidates(opts, unused, background=False)
            selected = selector.select(candidates, metrics, opts, time.time())
        if not candidates:
            message = (i18n.tr('No checked vocabulary verbs match these filters. Save Dutch verb words or include starter verbs.')
                       if opts.source == "vocabulary" else i18n.tr('These filters have no valid Dutch forms. Change the round options.'))
            raise practice_round.NothingToPractice("no_words" if opts.source == "vocabulary" else "no_questions", message)
        if not selected:
            raise practice_round.NothingToPractice("not_due", i18n.tr('These verb forms are not due yet. Try other forms or practise early.'))
        build = practice_round.Build(opts=opts, report=report, cached_first=cached_first, background=background)
        key = operator.itemgetter("verb_id", "tense", "subject", "structure")

        def load(rows, job):
            """Bind stable relational targets only when selected; e.g. replacements never materialize the entire grammar cross-product."""

            targets = [self._db.verb_target(row) for row in rows]
            for row, target in zip(rows, targets, strict=True):
                target["previous_rejection"] = row.get("previous_rejection", "")
            checked = {row["target_id"]: row for row in self._content(targets, job)}
            for row, target in zip(rows, targets, strict=True):
                row["previous_rejection"] = target["previous_rejection"]
            return {key(row): checked[row["target_id"]] for row in targets if row["target_id"] in checked}

        plan = practice_round.Plan(candidates=candidates, initial=selected, key=key, load=load,
                                   select=lambda rows, n: selector.select(rows, metrics, opts.model_copy(update={"count": n}), time.time()))
        questions = build.fill(plan)
        report(i18n.tr('{len} checked verb exercises ready', len=len(questions)))
        if background:
            return self._db.practice_stage("verb", opts, questions)
        return self._db.verb_create(opts, questions, policy.document())

    def _candidates(self, opts: verbs.Options, unused: set[int], *, background: bool) -> list[dict]:
        """Read only checked verbs, protecting current targets when building a spare."""

        candidates = verb_syllabus.candidates(self._db.verb_catalog(), self._db.verb_targets(), opts)
        if background:
            candidates = self._db.practice_spare_candidates("verb", opts, candidates)
        for row in candidates:
            row["unused"] = row.get("target_id") in unused
        return candidates

    def _content(self, targets: list[dict], build: practice_round.Build) -> list[dict]:
        """Cache each initial batch before shared replacement work; e.g. one rejection never regenerates its approved neighbours."""

        policy = {"model": self._provider.model, "reasoning": self._provider.reasoning,
                  "prompt_version": verb_generate.PROMPT_VERSION}
        ready, pending = {}, []
        reserved = self._db.practice_reserved("verb", build.opts) if build.background else set()
        for row in targets:
            cached = [item for item in self._db.verb_cached(row["target_id"], policy) if item["id"] not in reserved]
            if cached:
                ready[row["target_id"]] = cached[0]
            if not cached or (not build.cached_first and len(cached) < 3 and cached[0]["uses"]):
                pending.append(row)
        for offset in range(0, len(pending), 4):
            batch = build.take(pending[offset:offset + 4])
            if not batch:
                break
            document = [{key: row[key] for key in ("target_id", "lemma", "family", "transitive", "tense", "subject", "structure", "previous_rejection")}
                        for row in batch]
            build.report(i18n.tr('Preparing and checking {len} verb exercises…', len=len(document)))
            try:
                approved, rejected = self._checked(document, policy)
            except ValueError as exc:
                approved, rejected = {}, {row["target_id"]: str(exc) for row in batch}
            except Exception:
                if all(row["target_id"] in ready for row in batch):
                    continue
                raise
            ready.update(approved)
            for row in batch:
                if row["target_id"] in rejected:
                    row["previous_rejection"] = rejected[row["target_id"]]
        return [ready[row["target_id"]] for row in targets if row["target_id"] in ready]

    def _checked(self, targets: list[dict], policy: dict) -> tuple[dict, dict]:
        """Validate exact IDs and preserve rejections for one bounded repair; e.g. an audit cannot silently omit a target."""

        generated = self._provider.generate(targets)
        expected = {row["target_id"] for row in targets}
        approved, rejected, checked = {}, {}, set()
        validated = []
        for item in generated:
            draft = verbs.Draft.model_validate(item["draft"])
            verdict = verbs.Verdict.model_validate(item["verdict"])
            if draft.target_id not in expected or draft.target_id in checked or verdict.target_id != draft.target_id:
                raise ValueError("Invalid verb generation IDs")
            checked.add(draft.target_id)
            validated.append((draft, verdict))
        if checked != expected:
            raise ValueError("The provider omitted verb exercises")
        for draft, verdict in validated:
            if verdict.valid:
                approved[draft.target_id] = self._db.verb_cache(draft, verdict, policy)
            else:
                rejected[draft.target_id] = verdict.reason
        return approved, rejected

    def check(self, presentation_id: int, report) -> dict:
        """Grade only a durable first attempt; e.g. an exact approved alternative needs no network request."""

        row = self._db.verb_get(presentation_id)
        if row["answer_text"] is None:
            raise ValueError("Submit your sentence before checking it")
        if row["outcome"] is not None:
            return row
        content = row["content"]
        accepted = [content["answer"], *content["alternatives"]]
        exact = verbs.normalized(row["answer_text"]) in {verbs.normalized(text) for text in accepted}
        report(i18n.tr('Checking your sentence…'))
        if exact:
            grade = verbs.Grade(confident=True, correct=True, corrected=row["answer_text"],
                                corrected_english=content.get("answer_english") or self._provider.translate_sentence(row["answer_text"]),
                                explanation=content["explanation"], errors=[],
                                axes=[verbs.Axis(axis=axis, correct=True) for axis in verbs.AXES], vocabulary=[])
        else:
            target = {key: row[key] for key in ("lemma", "family", "tense", "subject", "structure")}
            grade = self._provider.grade({"target": target, "exercise": content, "learner_answer": row["answer_text"]})
        grade.check(row["answer_text"])
        report(i18n.tr('Saving correction…'))
        provenance = {"model": "approved_exact_match" if exact else self._provider.model,
                      "reasoning": "none" if exact else self._provider.reasoning, "grade_version": verb_generate.GRADE_VERSION}
        return self._db.verb_grade(presentation_id, grade, provenance)

    def english(self, question_id: int, report) -> str:
        """Reveal only an original sentence meaning, with persistent caching; e.g. an unfinished older round gets the same help."""

        content = self._db.verb_question(question_id)
        if text := content.get("source_english"):
            return text
        policy = {"model": self._provider.model, "reasoning": self._provider.reasoning,
                  "prompt_version": verb_generate.TRANSLATION_VERSION}
        if text := self._db.verb_translation(question_id, policy):
            return text
        report(i18n.tr('Translating the original sentence…'))
        text = self._provider.translate_sentence(content["source"])
        report(i18n.tr('Saving sentence translation…'))
        self._db.verb_translation_save(question_id, text, policy)
        return text
