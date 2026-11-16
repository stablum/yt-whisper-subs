"""Persist shared lexical audits, their leases and source-linked vocabulary additions.

Example: identical missed words reuse a verified result while retaining separate review events.
"""

from __future__ import annotations

import json
import time

from yt_whisper_subs import cfg
from yt_whisper_subs import practice_ready
from yt_whisper_subs import vocabulary
from yt_whisper_subs import vocabulary_accretion as accretion
from yt_whisper_subs import vocabulary_accretion_schema as schema
from yt_whisper_subs import vocabulary_learning as learning


class AccretionDbMixin:
    """Keep paid work outside transactions and application views; e.g. a listening grade commits before enrichment."""

    @staticmethod
    def _accretion_job(conn, document: dict) -> int:
        """Deduplicate both positive and negative analyses by linguistic input; e.g. repeat errors reuse one audit."""

        model, reasoning = cfg.DEFAULT_PRACTICE_MODEL, cfg.DEFAULT_PRACTICE_REASONING
        key = accretion.cache_key(document, model, reasoning)
        conn.execute("INSERT OR IGNORE INTO vocab_audits(cache_key,document_json,model,reasoning,version) VALUES(?,?,?,?,?)",
                     (key, json.dumps(document, ensure_ascii=False, sort_keys=True), model, reasoning, accretion.VERSION))
        return conn.execute("SELECT id FROM vocab_audits WHERE cache_key=?", (key,)).fetchone()[0]

    def _accretion_compound(self, conn, lookup_id: int) -> None:
        """Schedule a dictionary sense once across capture and practice; e.g. inflected compounds share morphology."""

        row = conn.execute("SELECT l.*,w.language FROM vocab_lookups l JOIN vocab_words w ON w.id=l.word_id WHERE l.id=?",
                           (lookup_id,)).fetchone()
        if row["language"] != "nl" or row["state"] != "ready" or row["ambiguous"] or not row["lemma"]:
            return
        lemma = vocabulary.normalized(row["lemma"].strip())
        if len(lemma) < 4 or not lemma.replace("-", "").isalpha() or not self._word_allowed(conn, "nl", lemma):
            return
        lexeme = dict(lemma=lemma, meaning=row["lemma_meaning"] or row["meaning"], part_of_speech=row["part_of_speech"] or "word")
        document = {"kind": "compound", "lexemes": [lexeme]}
        job = self._accretion_job(conn, document)
        conn.execute("INSERT OR IGNORE INTO vocab_audit_sources(audit_id,lookup_id) VALUES(?,?)", (job, lookup_id))

    def _accretion_mistake(self, conn, kind: str, row: dict) -> None:
        """Queue only real failed answers using their fixed feedback; e.g. correct or reported questions never spend tokens."""

        if row["outcome"] != "wrong":
            return
        if kind == "quiz":
            content = json.loads(row["body_json"])
            selected = json.loads(row["choices_json"])[row["selected_idx"]]
            target = conn.execute("SELECT * FROM vocab_lookups WHERE id=?", (row["lookup_id"],)).fetchone()
            lexeme = dict(lemma=target["lemma"], meaning=target["lemma_meaning"] or target["meaning"],
                          part_of_speech=target["part_of_speech"] or "word")
            document = dict(kind=kind, origin=kind, direction=row["direction"], correct=False,
                            answer=selected["text"], dutch=content["source_sentence"], english=content["meaning"],
                            errors=[selected], lexemes=[lexeme])
        else:
            document = learning.document("sentence", row)
            document.update(kind="listening", origin="listening", lexemes=[])
        job = self._accretion_job(conn, document)
        column = "practice_id" if kind == "quiz" else "sentence_id"
        conn.execute(f"INSERT OR IGNORE INTO vocab_audit_sources(audit_id,{column}) VALUES(?,?)", (job, row["id"]))

    def accretion_seed(self) -> None:
        """Backfill saved vocabulary and old failures without regrading; e.g. upgrading also covers cached compound lookups."""

        from yt_whisper_subs import practice_db
        from yt_whisper_subs import sentence_db

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute(f"SELECT DISTINCT lookup_id FROM ({schema.LIVE_LOOKUPS})").fetchall():
                self._accretion_compound(conn, row[0])
            rows = conn.execute(practice_db.PRESENTATION + " WHERE p.outcome='wrong' AND NOT EXISTS("
                                "SELECT 1 FROM vocab_audit_sources a WHERE a.practice_id=p.id)").fetchall()
            for row in rows:
                self._accretion_mistake(conn, "quiz", dict(row))
            rows = conn.execute(sentence_db.PRESENTATION + " WHERE p.outcome='wrong' AND s.direction='listen_nl' AND NOT EXISTS("
                                "SELECT 1 FROM vocab_audit_sources a WHERE a.sentence_id=p.id)").fetchall()
            for row in rows:
                self._accretion_mistake(conn, "listening", self._sentence_row(row))

    def accretion_claim(self, owner: str, *, kind: str | None = None) -> list[dict]:
        """Lease one stage of a bounded batch across processes; e.g. verifier retry never regenerates saved proposals."""

        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE vocab_audits SET state=CASE WHEN proposal_json IS NULL THEN 'pending' ELSE 'verify' END,owner=NULL "
                         "WHERE state IN ('working','verifying') AND lease_until<?", (now,))
            for row in conn.execute("SELECT id,owner,proposal_json FROM vocab_audits WHERE state IN ('working','verifying')").fetchall():
                if not practice_ready.owner_alive(row["owner"]):
                    conn.execute("UPDATE vocab_audits SET state=?,owner=NULL WHERE id=?",
                                 ("pending" if row["proposal_json"] is None else "verify", row["id"]))
            # Verification is cheaper and publishes already generated work before taking another batch.
            scope = "(? IS NULL OR json_extract(job.document_json,'$.kind')=?)"
            state = "verify" if conn.execute(f"SELECT 1 FROM vocab_audits job WHERE state='verify' AND {schema.ACTIVE_JOB} AND {scope} LIMIT 1",
                                             (kind, kind)).fetchone() else "pending"
            rows = conn.execute(f"SELECT * FROM vocab_audits job WHERE state=? AND {schema.ACTIVE_JOB} AND {scope} ORDER BY id LIMIT ?",
                                (state, kind, kind, accretion.BATCH_SIZE)).fetchall()
            if rows:
                policy = rows[0]["model"], rows[0]["reasoning"], rows[0]["version"]
                rows = [row for row in rows if (row["model"], row["reasoning"], row["version"]) == policy]
            for row in rows:
                conn.execute("UPDATE vocab_audits SET state=?,owner=?,lease_until=? WHERE id=?",
                             ("verifying" if state == "verify" else "working", owner, now + 300, row["id"]))
            jobs = []
            for row in rows:
                document = json.loads(row["document_json"])
                known = []
                for word in document["lexemes"]:
                    key = accretion.cache_key(dict(kind="compound", lexemes=[word]), row["model"], row["reasoning"])
                    if conn.execute("SELECT 1 FROM vocab_audits WHERE cache_key=? AND state='done'", (key,)).fetchone():
                        known.append(word["lemma"])
                jobs.append({**dict(row), "document": document, "known_morphology": known,
                             "repair": json.loads(row["repair_json"]) if row["repair_json"] else None,
                             "proposal": json.loads(row["proposal_json"]) if row["proposal_json"] else None})
            return jobs

    def accretion_proposed(self, owner: str, proposals: list[accretion.Analysis]) -> None:
        """Checkpoint before independent checking; e.g. a verifier outage preserves all generation tokens."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for proposal in proposals:
                row = conn.execute("SELECT document_json FROM vocab_audits WHERE id=? AND owner=? AND state='working'",
                                   (proposal.id, owner)).fetchone()
                if row is None:
                    continue
                try:
                    proposal.check(json.loads(row[0]))
                except ValueError as exc:
                    repair = dict(previous_rejection=str(exc), previous_proposal=proposal.model_dump())
                    conn.execute("UPDATE vocab_audits SET state='failed',repair_json=?,error=?,owner=NULL,lease_until=0 WHERE id=?",
                                 (json.dumps(repair, ensure_ascii=False), str(exc), proposal.id))
                    continue
                positive = bool(proposal.vocabulary or proposal.splits)
                conn.execute("UPDATE vocab_audits SET proposal_json=?,result_json=?,state=?,repair_json=NULL,error='',owner=NULL,lease_until=0 WHERE id=?",
                             (proposal.model_dump_json(), None if positive else proposal.model_dump_json(),
                              "verify" if positive else "done", proposal.id))

    def accretion_verified(self, owner: str, verdicts: dict[int, bool]) -> None:
        """Publish only owned complete verdicts; e.g. a late worker cannot overwrite a newer lease."""

        with self._connect() as conn:
            for key, valid in verdicts.items():
                empty = accretion.Analysis(id=key, vocabulary=[], splits=[]).model_dump_json()
                conn.execute("UPDATE vocab_audits SET result_json=CASE WHEN ? THEN proposal_json ELSE ? END,"
                             "state='done',owner=NULL,lease_until=0 WHERE id=? AND owner=? AND state='verifying'",
                             (valid, empty, key, owner))

    def accretion_failed(self, owner: str, error: str) -> None:
        """Stop automatic paid retries after a failure; e.g. restored credentials require the normal Retry action."""

        with self._connect() as conn:
            conn.execute("UPDATE vocab_audits SET state='failed',error=?,owner=NULL,lease_until=0 WHERE owner=?",
                         (error[:2000], owner))

    def accretion_retry(self, *, kind: str | None = None) -> int:
        """Retry the unfinished stage only; e.g. verification reuses its durable proposal."""

        with self._connect() as conn:
            return conn.execute("UPDATE vocab_audits SET state=CASE WHEN proposal_json IS NULL THEN 'pending' ELSE 'verify' END,"
                                "error='' WHERE state='failed' AND (? IS NULL OR json_extract(document_json,'$.kind')=?)",
                                (kind, kind)).rowcount

    def accretion_status(self) -> dict[str, int]:
        """Expose bounded work and failures for the vocabulary window; e.g. background errors remain discoverable."""

        with self._connect() as conn:
            return dict(conn.execute(f"SELECT job.state,COUNT(*) FROM vocab_audits job WHERE {schema.ACTIVE_JOB} GROUP BY job.state").fetchall())

    def word_compounds(self, lookup_id: int) -> list[accretion.Split]:
        """Read a verified breakdown without scheduling work; e.g. the vocabulary detail explains the linker in kinderwagen."""

        with self._connect() as conn:
            row = conn.execute("SELECT lemma,lemma_meaning,meaning,part_of_speech FROM vocab_lookups WHERE id=?", (lookup_id,)).fetchone()
            word = dict(lemma=vocabulary.normalized(row["lemma"] or "").strip(), meaning=row["lemma_meaning"] or row["meaning"],
                        part_of_speech=row["part_of_speech"] or "word")
            key = accretion.cache_key(dict(kind="compound", lexemes=[word]), cfg.DEFAULT_PRACTICE_MODEL, cfg.DEFAULT_PRACTICE_REASONING)
            result = conn.execute("SELECT result_json FROM vocab_audits WHERE cache_key=? AND state='done'", (key,)).fetchone()
            return accretion.Analysis.model_validate_json(result[0]).splits if result else []

    def accretion_apply(self) -> int:
        """Attach cached discoveries to each still-valid source atomically; e.g. repeated errors keep separate dates."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(f"SELECT a.*,j.result_json,j.document_json,j.model,j.reasoning,"
                                f"COALESCE(p.answered_at,s.answered_at,root.updated_at) AS answered_at {schema.SOURCE} "
                                f"WHERE j.state='done' AND a.applied=0 AND {schema.VALID} ORDER BY a.id LIMIT 100").fetchall()
            for row in rows:
                self._accretion_apply_source(conn, dict(row))
            return len(rows)

    def _accretion_apply_source(self, conn, source: dict) -> None:
        """Reuse the normal dictionary factory and preserve inferred versus observed knowledge; e.g. components never fail review."""

        document = json.loads(source["document_json"])
        result = accretion.Analysis.model_validate_json(source["result_json"])
        result.check(document)
        provenance = dict(model=source["model"], reasoning=source["reasoning"])
        context = {**document, "direction": document.get("direction", "nl_en")}
        capture = learning.Capture(row=source, context=context, provenance=provenance)
        added = self._accretion_import(conn, capture, result)
        # Mistake proposals seed the morphology cache below; their root lookup owns component evidence.
        components = result.components() if document["kind"] == "compound" else []
        for parent, word in components:
            if not self._word_allowed(conn, "nl", word.lemma):
                continue
            note = f"Constituent of {parent}."
            fix = learning.Correction(typed="", dutch=[word.lemma], english=[word.meaning], intended=word, confused=None, note=note)
            component = learning.Capture(row=source, provenance=provenance,
                                        context=dict(origin="component", direction="nl_en", dutch=word.lemma,
                                                     english=word.meaning, answer=""))
            lookup_id = self._learning_lookup(conn, fix, "intended", component)
            conn.execute("INSERT OR IGNORE INTO vocab_additions(source_id,lookup_id,role,parent,review) VALUES(?,?,'component',?,0)",
                         (source["id"], lookup_id, parent))
        conn.execute("UPDATE vocab_audit_sources SET applied=1 WHERE id=?", (source["id"],))
        direction = "nl_en" if document.get("direction") == "listen_nl" else document.get("direction", "nl_en")
        ids = conn.execute("SELECT DISTINCT l.word_id FROM vocab_additions d JOIN vocab_lookups l ON l.id=d.lookup_id "
                           "WHERE d.source_id=? AND d.review=1", (source["id"],)).fetchall()
        for row in ids:
            self._word_replay(conn, row[0], direction)
        for lookup_id in added:
            self._accretion_compound(conn, lookup_id)
        self._accretion_cache_splits(conn, document, result)

    def _accretion_import(self, conn, capture: learning.Capture, result: accretion.Analysis) -> list[int]:
        """Add genuine concepts without duplicating direct target assessments; e.g. a missed quiz target already has its failure."""

        source, added = capture.row, []
        for fix in result.vocabulary:
            for role in ("intended", "confused"):
                word = getattr(fix, role)
                if word is None or not self._word_allowed(conn, "nl", word.lemma):
                    continue
                lookup_id = self._learning_lookup(conn, fix, role, capture)
                word_id = conn.execute("SELECT word_id FROM vocab_lookups WHERE id=?", (lookup_id,)).fetchone()[0]
                observed = conn.execute("SELECT correct FROM word_transfers WHERE sentence_id=? AND word_id=? UNION ALL "
                                        "SELECT 0 FROM practice_questions q JOIN practice_presentations p ON p.question_id=q.id "
                                        "JOIN vocab_lookups l ON l.id=q.lookup_id WHERE p.id=? AND l.word_id=?",
                                        (source["sentence_id"], word_id, source["practice_id"], word_id)).fetchone()
                conn.execute("INSERT OR IGNORE INTO vocab_additions(source_id,lookup_id,role,review) VALUES(?,?,?,?)",
                             (source["id"], lookup_id, role, int(observed is None or observed[0])))
                added.append(lookup_id)
        return added

    def _accretion_cache_splits(self, conn, document: dict, result: accretion.Analysis) -> None:
        """Reuse morphology discovered during mistake analysis; e.g. imported compounds need no additional generation call."""

        lexemes = list(document["lexemes"])
        for fix in result.vocabulary:
            lexemes.extend(word.model_dump() for word in (fix.intended, fix.confused) if word)
        lexemes.extend(word.model_dump() for _, word in result.components())
        for lexeme in lexemes:
            job = self._accretion_job(conn, dict(kind="compound", lexemes=[lexeme]))
            pending = [lexeme["lemma"]]
            remaining = {split.lemma: split for split in result.splits}
            splits = []
            while pending:
                if split := remaining.pop(pending.pop(), None):
                    splits.append(split)
                    pending.extend(part.word.lemma for part in split.parts if part.word)
            cached = accretion.Analysis(id=job, vocabulary=[], splits=splits).model_dump_json()
            conn.execute("UPDATE vocab_audits SET state='done',proposal_json=?,result_json=? "
                         "WHERE id=? AND state='pending'", (cached, cached, job))

    def _accretion_unlink(self, conn, kind: str, question_id: int) -> None:
        """Rebuild confused-word schedules after source reporting; e.g. a rejected quiz cannot keep an imported word due."""

        column, table = ("practice_id", "practice_presentations") if kind == "quiz" else ("sentence_id", "sentence_presentations")
        rows = conn.execute(f"SELECT DISTINCT r.word_id,r.direction FROM ({schema.REVIEWS}) r "
                            "JOIN vocab_additions d ON d.id=r.event_id JOIN vocab_audit_sources a ON a.id=d.source_id "
                            f"JOIN {table} p ON p.id=a.{column} WHERE p.question_id=?", (question_id,)).fetchall()
        for row in rows:
            self._word_replay(conn, row["word_id"], row["direction"])
