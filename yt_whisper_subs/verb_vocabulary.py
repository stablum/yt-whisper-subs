"""Incrementally connect saved Dutch vocabulary to the practice lexicon.

Example: an already-known lopen needs no new request; an unfamiliar separable verb receives a lexical audit.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from itertools import batched, islice

from yt_whisper_subs import i18n
from yt_whisper_subs import practice_ready
from yt_whisper_subs import verb_generate
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_syllabus
from yt_whisper_subs import vocabulary


_VERB_POS = re.compile(r"\b(?:verbs?|werkwoord|hulpwerkwoord|koppelwerkwoord|modal|auxiliary|participle|deelwoord)\b", re.I)
_MIXED_POS = re.compile(r"\b(?:noun|adjective|zelfstandig|bijvoeglijk)\b", re.I)
TRACK = "verb:lexicon"
_BATCH_SIZE = 12


class VocabularySync:
    """Reuse validated dictionary forms and cache contextual lexical audits independently of question generation."""

    def __init__(self, db, provider: verb_generate.Provider) -> None:
        """Share the practice provider without changing word translation; e.g. existing saved lemmas remain authoritative evidence."""

        self._db, self._provider = db, provider

    def _policy(self) -> dict:
        """Identify the audit policy, e.g. saved mapping decisions survive app restarts."""

        return {"model": self._provider.model, "reasoning": self._provider.reasoning,
                "prompt_version": verb_generate.LEXICON_VERSION}

    def link_known(self) -> None:
        """Link cheap dictionary matches while a separate worker audits unfamiliar forms."""

        self._collect(self._policy())

    def _collect(self, policy: dict) -> tuple[list[dict], int]:
        """Use only dictionary metadata here, avoiding the exercise weighting and review queries."""

        rows = self._db.verb_vocab_pending(policy)
        if not rows:
            return [], 0
        catalog = {lemma: dict(family=family, transitive=transitive)
                   for lemma, family, transitive in verb_syllabus.CATALOG}
        catalog.update(self._db.verb_lexemes())
        local_rows, local, pending = [], [], []
        for row in rows:
            lemma = vocabulary.normalized(" ".join(row["lemma"].split()))
            known = catalog.get(lemma)
            is_verb = bool(_VERB_POS.search(row["part_of_speech"] or ""))
            if is_verb and (known is None or _MIXED_POS.search(row["part_of_speech"] or "")):
                pending.append(row)
                continue
            lexeme = verbs.Lexeme(lookup_id=row["lookup_id"], is_verb=is_verb,
                                  lemma=lemma if is_verb else "", family=known["family"] if is_verb else None,
                                  transitive=bool(known["transitive"]) if is_verb else None,
                                  reason="Saved unambiguous verb lemma matches the lexicon." if is_verb else "Saved part of speech is not verbal.")
            verdict = verbs.LexemeCheck(lookup_id=row["lookup_id"], valid=True, reason=lexeme.reason)
            local_rows.append(row)
            local.append({"lexeme": lexeme.model_dump(), "verdict": verdict.model_dump(),
                          "strategy": "saved_lemma" if is_verb else "saved_part_of_speech"})
        if local:
            self._db.verb_vocab_save(local_rows, local, policy)
        return pending, len(local)

    def sync(self, report, *, batches: int | None = None) -> int:
        """Share one audit lease; e.g. a foreground request can reuse a background batch already checked."""

        policy = self._policy()
        if not self._db.verb_vocab_pending(policy):
            return 0
        with practice_ready.preparing(self._db, TRACK, report) as progress:
            pending, linked = self._collect(policy)
            chunks = [list(rows) for rows in islice(batched(pending, _BATCH_SIZE), batches)]
            if not chunks:
                return linked
            if len(chunks) == 1:
                self._audit(chunks[0], policy, progress)
            else:
                # Each batch keeps its own extraction, independent audit and durable save.
                with ThreadPoolExecutor(max_workers=2) as pool:
                    jobs = [pool.submit(copy_context().run, self._audit, rows, policy, progress) for rows in chunks]
                    for job in jobs:
                        job.result()
            return linked + sum(map(len, chunks))

    def _audit(self, rows: list[dict], policy: dict, report) -> None:
        """Audit contextual lexical facts with one repair; e.g. invalid proposals retry later without seeding bad exercises."""

        document = []
        for row in rows:
            context = json.loads(row["context_json"])
            document.append({"lookup_id": row["lookup_id"], "lemma": row["lemma"],
                             "part_of_speech": row["part_of_speech"], "meaning": row["meaning"],
                             "expression": row["expression"] or "", "note": row["note"] or "", "context": context})
        report(i18n.tr('Checking {len} saved vocabulary verbs…', len=len(rows)))
        for attempt in range(2):
            results = self._provider.lexemes(document)
            report(i18n.tr('Saving checked vocabulary verbs…'))
            chosen = {row["lookup_id"] for row in document}
            self._db.verb_vocab_save([row for row in rows if row["lookup_id"] in chosen], results, policy)
            rejected = {item["verdict"]["lookup_id"]: item["verdict"]["reason"]
                        for item in results if not item["verdict"]["valid"]}
            if not rejected or attempt:
                break
            report(i18n.tr('Refining {len} unconfirmed vocabulary verb mappings…', len=len(rejected)))
            document = [{**row, "previous_rejection": rejected[row["lookup_id"]]}
                        for row in document if row["lookup_id"] in rejected]
