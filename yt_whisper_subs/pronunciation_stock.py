"""Prepare optional Dutch clips from the same saved rounds practice opens."""

import itertools
import json

from yt_whisper_subs import practice_ready
from yt_whisper_subs import practice_tracks as tracks
from yt_whisper_subs import pronunciation


# The UI reads these approved answers, so speech can be ready before grading.
_FIELDS = {
    tracks.Track.WORD_NL_EN: ('surface',),
    tracks.Track.WORD_EN_NL: ('answer',),
    tracks.Track.VERBS: ('source', 'answer'),
    tracks.Track.SENTENCE_EN_NL: ('dutch',),
    tracks.Track.SENTENCE_NL_EN: ('dutch',),
}


class Stock:
    """Warm resumable and standby rounds without recording any exposure."""

    def __init__(self, db, *, audio=None) -> None:
        """Share the playback cache, e.g. no work is repeated when reading a warmed prompt."""

        self._db = db
        self._audio = audio or pronunciation.Audio(db)

    def warm(self, requests, report) -> bool:
        """Advance each eligible mode in turn, starting with the last-used mode."""

        queues = []
        for track, opts, engine in requests:
            if track in _FIELDS:
                queues.append(self._questions(track, opts, engine))
            elif track == tracks.Track.READ_COMPREHENSION:
                questions, passage = self._reading(opts, engine)
                queues.extend((questions, passage))
        texts = [text for group in itertools.zip_longest(*queues) for text in group if text]
        return self._audio.warm(texts, report)

    def prepare_session(self, track: tracks.Track, session_id: str, report) -> None:
        """Finish a cold round's prompts and model answers before exposing its first question."""

        if track == tracks.Track.READ_COMPREHENSION:
            questions, passage = self._passage(session_id)
            texts = questions + passage
        else:
            texts = self._session_texts(track, (session_id, None))
        self._audio.prepare(texts, report)

    def _questions(self, track, opts, engine) -> list[str]:
        """Read bounded round content in play order, e.g. queued feedback precedes new standby."""

        kind = track.kind
        get_active = getattr(self._db, f'{kind}_active')
        active_id = get_active(track.direction.value) if track.direction else get_active()
        review_id = self._db.sentence_unseen_session(track.direction.value) if kind == 'sentence' else None
        texts = self._session_texts(track, (active_id, review_id))
        policy = practice_ready.question_policy(engine)
        for row in self._db.practice_stock(track, opts, policy):
            body = json.loads(row['body_json'])
            texts.extend(body[key] for key in _FIELDS[track])
        return texts

    def _session_texts(self, track, sessions: tuple) -> list[str]:
        """Read the prompt and approved answer without consuming a saved presentation."""

        kind = track.kind
        with self._db._connect() as conn:
            active = conn.execute(
                f"SELECT q.body_json FROM {kind}_presentations p "
                f"JOIN {kind}_questions q ON q.id=p.question_id "
                "WHERE p.session_id IN (?,?) AND q.flagged_at IS NULL AND p.outcome IS NOT 'invalid' "
                "ORDER BY p.session_id IS NOT ?,p.outcome IS NOT NULL,p.ordinal", (*sessions, sessions[0])
            ).fetchall()
        texts = []
        for row in active:
            body = json.loads(row['body_json'])
            texts.extend(body[key] for key in _FIELDS[track])
        return texts

    def _reading(self, opts, engine) -> tuple[list[str], list[str]]:
        """Prepare visible prompts and passage text, e.g. the article is ready before Read passage aloud."""

        ready = self._db.comprehension_ready(opts, engine.policy)
        active = self._db.comprehension_resume(opts.direction.value)
        questions, paragraphs = [], []
        for ident in (active, ready['id'] if ready else None):
            if ident is None:
                continue
            prompts, passage = self._passage(ident)
            questions.extend(prompts)
            paragraphs.extend(passage)
        return questions, paragraphs

    def _passage(self, ident: str) -> tuple[list[str], list[str]]:
        """Collect the visible reading material, including the full manual narration."""

        rows = self._db.comprehension_history(ident)
        prompts = [row['content']['prompt'] for row in rows if row['outcome'] != 'invalid']
        session = self._db.comprehension_session(ident)
        passage = self._db.comprehension_passage(session['passage_id'])['content']
        return prompts, passage['paragraphs']
