"""Prepare a full sentence builder round using the same vocabulary selector as Word quiz."""

from collections import Counter, defaultdict
import json
import operator
import random
import time

from yt_whisper_subs import i18n
from yt_whisper_subs import practice
from yt_whisper_subs import practice_ready
from yt_whisper_subs import practice_round
from yt_whisper_subs import word_order
from yt_whisper_subs import word_order_generate
from yt_whisper_subs import word_select


class Service:
    """Audit all proposed exercises before storing them, including those generated in the background."""

    def __init__(self, db, provider=None) -> None:
        """Accept a fixture provider so a full round can be tested without network calls."""

        self._db = db
        self._provider = provider or word_order_generate.Generator()

    @practice_ready.serialized("word_order", word_order_generate.VERSION)
    def start(self, opts: word_order.Options, report, *, cached_first=True, background=False) -> str:
        """Use checked stock first, then generate only the missing distinct exercises."""

        review = practice.ReviewPolicy()
        selector = word_select.Selector(review)
        rows = self._db.word_candidates(practice.Direction.EN_NL.value)
        if not rows:
            raise practice_round.NothingToPractice("no_words", i18n.tr('Save some Dutch words with translations first.'))
        if background:
            rows = self._db.practice_spare_candidates("word_order", opts, rows)
        policy = practice_ready.question_policy(self)
        reserved = self._db.practice_reserved("word_order", opts) if background else set()
        stock = defaultdict(list)
        for q in self._db.word_order_stock(opts, policy):
            if q["id"] not in reserved:
                stock[q["lookup_id"]].append(q)
        for row in rows:
            row["unused"] = any(not q["uses"] for q in stock.get(row["lookup_id"], []))

        def choose(pool, count):
            """Use eligible unseen builds first, keeping the shared vocabulary weights inside each pool."""

            return selector.select(pool, opts.model_copy(update={"count": count}), time.time(), cached_first=cached_first)

        patterns = _Patterns(opts.variant, self._db.word_order_results(opts.variant))
        build = practice_round.Build(opts=opts, report=report, cached_first=cached_first, background=background)
        plan = practice_round.Plan(candidates=rows, initial=choose(rows, opts.count), select=choose,
                                   key=operator.itemgetter("word_id"),
                                   load=lambda selected, build: self._prepare(selected, build, stock, patterns, policy))
        questions = build.fill(plan)
        if background:
            return self._db.practice_stage("word_order", opts, questions)
        return self._db.word_order_create(opts, questions, review.document())

    def _prepare(self, rows, build, stock, patterns, policy) -> dict:
        """Keep checked variants on outage, and never pay to regenerate untouched stock."""

        ready, pending = {}, []
        for row in rows:
            if variants := stock.get(row["lookup_id"]):
                ready[row["word_id"]] = variants[0]
                if variants[0]["uses"] == 0:
                    continue
            pending.append(row)
        for offset in range(0, len(pending), 4):
            batch = build.take(pending[offset:offset + 4])
            if not batch:
                break
            build.report(i18n.tr('Preparing Dutch sentence builds…'))
            try:
                ready.update(self._audit_batch(batch, build.opts, stock, patterns, policy))
            except ValueError as exc:
                for row in batch:
                    row["quality_feedback"] = str(exc)
            except Exception:
                if any(row["word_id"] not in ready for row in batch):
                    raise
        return ready

    def _audit_batch(self, rows, opts, stock, patterns, policy) -> dict:
        """Check exact vocabulary and block membership before publishing blind-solved variants."""

        targets = []
        for row in rows:
            data = {key: row[key] for key in ("word_id", "lemma", "surface", "meaning")}
            data["pattern"] = patterns.pick()
            data["recent_examples"] = [json.loads(q["body_json"])["english"] for q in stock.get(row["lookup_id"], [])[:2]]
            data["quality_feedback"] = row.get("quality_feedback", "")
            targets.append(data)
        drafts = _by_word(self._provider.generate(targets, opts), targets)
        proposals = []
        for row, target in zip(rows, targets, strict=True):
            draft = drafts[row["word_id"]]
            try:
                draft.check(row["word_id"], opts)
                if draft.pattern != target["pattern"]:
                    raise ValueError("Keep the requested clause or verb-order pattern.")
            except ValueError as exc:
                row["quality_feedback"] = str(exc)
                continue
            proposals.append((target, draft))
        verdicts = _by_word(self._provider.audit(proposals, opts.variant), [target for target, _ in proposals]) if proposals else {}
        ready = {}
        for row in rows:
            word_id = row["word_id"]
            if verdict := verdicts.get(word_id):
                try:
                    ready[word_id] = self._db.word_order_cache(drafts[word_id], verdict, row, opts, policy)
                except ValueError as exc:
                    row["quality_feedback"] = str(exc)
        return ready


def _by_word(items: list, expected: list[dict]) -> dict:
    """Require a complete, unique batch response, e.g. one missing audit cannot become an approval."""

    by_id = {item.word_id: item for item in items}
    if by_id.keys() != {row["word_id"] for row in expected} or len(items) != len(by_id):
        raise ValueError("The sentence builder response did not cover every requested word once")
    return by_id


class _Patterns:
    """Favor troublesome Dutch structures while avoiding repetitive rounds."""

    def __init__(self, variant: word_order.Variant, history: list[dict]) -> None:
        """Use the shared smoothed error weighting for constructions, e.g. missed verb clusters recur."""

        attempts = Counter(row["pattern"] for row in history)
        failures = Counter(row["pattern"] for row in history if row["outcome"] == "wrong")
        self._labels = variant.patterns
        self._weights = word_select.error_weight([attempts[key] for key in self._labels], [failures[key] for key in self._labels])
        self._chosen = Counter()

    def pick(self) -> str:
        """Discount patterns already chosen for this round and leave room for new ones."""

        weights = [weight / (1 + self._chosen[key]) ** 2 for key, weight in zip(self._labels, self._weights, strict=True)]
        key = random.choices(self._labels, weights=weights, k=1)[0]
        self._chosen[key] += 1
        return key
