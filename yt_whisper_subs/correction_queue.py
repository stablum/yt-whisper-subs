"""Lease durable correction jobs independently from their native views.

Example: translated sentences and comprehension answers share recovery semantics while local dictation keeps its own lane.
"""

import time

from yt_whisper_subs import practice_ready


class Queue:
    """Bind trusted table names to one correction strategy; e.g. a failed job waits for explicit retry rather than spending tokens repeatedly."""

    def __init__(self, db, kind="sentence", *, local=False) -> None:
        """Choose the immutable answer family once; e.g. local=True selects only dictation answers."""

        if kind not in {"sentence", "comprehension"} or local and kind != "sentence":
            raise ValueError("Unknown correction lane")
        self._db, self._kind, self._local = db, kind, local

    def claim(self, owner: str) -> dict | None:
        """Claim the oldest unanswered grade and recover dead owners; e.g. reopening a dialog cannot duplicate an in-flight correction."""

        kind, now = self._kind, time.time()
        with self._db._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"INSERT OR IGNORE INTO {kind}_checks(presentation_id,state) SELECT id,'queued' "
                         f"FROM {kind}_presentations WHERE answer_text IS NOT NULL AND outcome IS NULL")
            conn.execute(f"UPDATE {kind}_checks SET state='done',owner=NULL,lease_until=0 WHERE state<>'done' AND EXISTS "
                         f"(SELECT 1 FROM {kind}_presentations p WHERE p.id=presentation_id AND p.outcome IS NOT NULL)")
            for row in conn.execute(f"SELECT presentation_id,owner,lease_until FROM {kind}_checks WHERE state='checking'").fetchall():
                if row['lease_until'] <= now or not practice_ready.owner_alive(row['owner']):
                    conn.execute(f"UPDATE {kind}_checks SET state='queued',owner=NULL,lease_until=0 WHERE presentation_id=?", (row['presentation_id'],))
            scope = "(q.direction='listen_nl')=?" if kind == 'sentence' else "q.kind='open'"
            params = (self._local,) if kind == 'sentence' else ()
            row = conn.execute(f"SELECT p.id FROM {kind}_presentations p JOIN {kind}_checks c ON c.presentation_id=p.id "
                               f"JOIN {kind}_questions q ON q.id=p.question_id WHERE c.state='queued' AND p.outcome IS NULL "
                               f"AND {scope} ORDER BY p.answered_at,p.id LIMIT 1", params).fetchone()
            if row is None:
                return None
            conn.execute(f"UPDATE {kind}_checks SET state='checking',owner=?,lease_until=?,attempts=attempts+1,error='' "
                         "WHERE presentation_id=?", (owner, now + 900, row['id']))
            return {'id': row['id']}

    def touch(self, presentation_id: int, owner: str) -> None:
        """Renew ownership between provider operations; e.g. lexical metadata repair stays within the same job."""

        with self._db._connect() as conn:
            cursor = conn.execute(f"UPDATE {self._kind}_checks SET lease_until=? WHERE presentation_id=? AND owner=? AND state='checking'",
                                  (time.time() + 900, presentation_id, owner))
            if cursor.rowcount != 1:
                raise RuntimeError("Another worker owns this correction")

    def done(self, presentation_id: int, owner: str, *, error="", cancelled=False) -> None:
        """Keep operational failure distinct from an incorrect answer; e.g. cancellation leaves the first attempt queued."""

        with self._db._connect() as conn:
            row = conn.execute(f"SELECT outcome FROM {self._kind}_presentations WHERE id=?", (presentation_id,)).fetchone()
            if row is None:
                return
            state = 'done' if row['outcome'] else ('queued' if cancelled else 'failed' if error else 'done')
            conn.execute(f"UPDATE {self._kind}_checks SET state=?,error=?,owner=NULL,lease_until=0 WHERE presentation_id=? AND owner=?",
                         (state, error[:2000], presentation_id, owner))

    def retry(self, presentation_id: int) -> None:
        """Retry only a failed saved answer; e.g. pressing Retry never adds another activity event."""

        with self._db._connect() as conn:
            conn.execute(f"UPDATE {self._kind}_checks SET state='queued',error='' WHERE presentation_id=? AND state='failed' "
                         f"AND EXISTS(SELECT 1 FROM {self._kind}_presentations p WHERE p.id=presentation_id AND p.outcome IS NULL)", (presentation_id,))
