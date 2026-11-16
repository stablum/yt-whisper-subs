"""Incrementally connect saved Dutch vocabulary to the practice lexicon.

Example: an already-known lopen needs no new request; an unfamiliar separable verb receives a lexical audit.
"""

from __future__ import annotations

import json
import re

from yt_whisper_subs import i18n
from yt_whisper_subs import verb_generate
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import vocabulary


_VERB_POS = re.compile(r"\b(?:verbs?|werkwoord|hulpwerkwoord|koppelwerkwoord|modal|auxiliary|participle|deelwoord)\b", re.I)
_MIXED_POS = re.compile(r"\b(?:noun|adjective|zelfstandig|bijvoeglijk)\b", re.I)


class VocabularySync:
    """Reuse validated dictionary forms and cache contextual lexical audits independently of question generation."""

    def __init__(self, db, provider: verb_generate.Provider) -> None:
        """Share the practice provider without changing word translation; e.g. existing saved lemmas remain authoritative evidence."""

        self._db, self._provider = db, provider

    def sync(self, report) -> None:
        """Import new occurrences once, preserving lookup links; e.g. repeated videos increase priority without duplicating verbs."""

        policy = {"model": self._provider.model, "reasoning": self._provider.reasoning,
                  "prompt_version": verb_generate.LEXICON_VERSION}
        catalog = {row["lemma"]: row for row in self._db.verb_catalog()}
        rows = self._db.verb_vocab_pending(policy)
        local, pending = [], []
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
            local.append({"lexeme": lexeme.model_dump(), "verdict": verdict.model_dump(),
                          "strategy": "saved_lemma" if is_verb else "saved_part_of_speech"})
        if local:
            local_ids = {item["lexeme"]["lookup_id"] for item in local}
            self._db.verb_vocab_save([row for row in rows if row["lookup_id"] in local_ids], local, policy)
        for offset in range(0, len(pending), 12):
            batch = pending[offset:offset + 12]
            report(i18n.tr('Checking saved vocabulary verbs {value}–{value2} / {len}…', value=offset + 1, value2=offset + len(batch), len=len(pending)))
            self._audit(batch, policy, report)

    def _audit(self, rows: list[dict], policy: dict, report) -> None:
        """Audit contextual lexical facts with one repair; e.g. invalid proposals retry later without seeding bad exercises."""

        document = []
        for row in rows:
            context = json.loads(row["context_json"])
            document.append({"lookup_id": row["lookup_id"], "lemma": row["lemma"],
                             "part_of_speech": row["part_of_speech"], "meaning": row["meaning"],
                             "expression": row["expression"] or "", "note": row["note"] or "", "context": context})
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
