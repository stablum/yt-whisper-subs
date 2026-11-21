"""Coordinate durable ready rounds and single-owner generation across practice modes.

Example: opening Sentences during background preparation waits for that job instead of paying for a second batch.
"""

from __future__ import annotations

import functools
import contextlib
import json
import os
import time
import uuid

import psutil

from yt_whisper_subs import practice
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import task_cancel


SCHEMA = """
CREATE TABLE IF NOT EXISTS practice_preparation (
    track TEXT PRIMARY KEY,
    owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    failures INTEGER NOT NULL DEFAULT 0
);
"""


class ReadyDbMixin:
    """Own preparation leases separately from answer history; e.g. waiting and caching add no learning activity."""

    @staticmethod
    def _practice_upgrade(conn) -> None:
        """Upgrade ephemeral preparation locks and direction indexes without changing answers; e.g. opposite directions can both stay active."""

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(practice_preparation)")}
        if "track" not in columns:
            conn.execute("DROP TABLE practice_preparation")
            conn.execute(SCHEMA)
        for kind in ("practice", "sentence"):
            name = f"idx_{kind}_active"
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
            if row and f"{kind}_sessions(direction)" not in row[0].replace(" ", ""):
                conn.execute(f"DROP INDEX {name}")
            conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {kind}_sessions(direction) WHERE status='active'")
        if conn.execute("SELECT 1 FROM settings WHERE key='practice_last_track'").fetchone() is None:
            row = conn.execute("SELECT value FROM settings WHERE key='practice_last_mode'").fetchone()
            kind = {"sentences": "sentence", "verbs": "verb"}.get(row[0] if row else "", "practice")
            saved = conn.execute("SELECT value FROM settings WHERE key=?", (kind + "_options",)).fetchone()
            try:
                direction = json.loads(saved[0]).get("direction") if saved else None
                track = practice_tracks.Track(kind if kind == "verb" else f"{kind}:{direction or ('en_nl' if kind == 'sentence' else 'nl_en')}")
            except (ValueError, TypeError):
                track = practice_tracks.Track.WORD_NL_EN
            conn.execute("INSERT INTO settings VALUES('practice_last_track',?)", (track.value,))
        row = conn.execute("SELECT value FROM settings WHERE key='sentence_cursor'").fetchone()
        try:
            cursor = json.loads(row[0]) if row else {}
            session = conn.execute("SELECT direction FROM sentence_sessions WHERE id=?", (cursor.get("session"),)).fetchone()
            if session:
                key = f"sentence:{session[0]}_cursor"
                conn.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, row[0]))
        except (ValueError, TypeError, AttributeError):
            pass

    def practice_prepare_claim(self, track: str, owner: str) -> bool:
        """Elect one generator atomically; e.g. the foreground and startup worker cannot prepare duplicate rounds."""

        now = time.time()
        with self._connect() as conn:
            row = conn.execute("SELECT owner FROM practice_preparation WHERE track=? AND lease_until>?", (track, now)).fetchone()
            if row and row[0] and not owner_alive(row[0]):
                conn.execute("UPDATE practice_preparation SET lease_until=0 WHERE track=? AND owner=?", (track, row[0]))
            cursor = conn.execute("INSERT INTO practice_preparation(track,owner,lease_until) VALUES(?,?,?) "
                                  "ON CONFLICT(track) DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until "
                                  "WHERE practice_preparation.lease_until<=?", (track, owner, now + 900, now))
            return cursor.rowcount == 1

    def practice_prepare_touch(self, track: str, owner: str) -> bool:
        """Renew on actual work milestones; e.g. a long checked round retains ownership between bounded API calls."""

        with self._connect() as conn:
            cursor = conn.execute("UPDATE practice_preparation SET lease_until=? WHERE track=? AND owner=?", (time.time() + 900, track, owner))
            return cursor.rowcount == 1

    def practice_prepare_done(self, track: str, owner: str, error: str, *, delay: int = 0) -> None:
        """Release ownership with persistent failure backoff; e.g. a provider outage cannot trigger paid retries every minute."""

        with self._connect() as conn:
            row = conn.execute("SELECT failures FROM practice_preparation WHERE track=? AND owner=?", (track, owner)).fetchone()
            if row is None:
                return
            failures = row[0] + 1 if error and not delay else 0
            backoff = min(1800, 120 * 2 ** min(failures - 1, 4)) if failures else delay
            conn.execute("UPDATE practice_preparation SET owner=NULL,lease_until=0,next_at=?,error=?,failures=? WHERE track=? AND owner=?",
                         (time.time() + backoff, error[:2000], failures, track, owner))

    def practice_prepare_status(self, track: str) -> dict:
        """Read readiness without doing work; e.g. startup respects a failed request's cooldown after a restart."""

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM practice_preparation WHERE track=?", (track,)).fetchone()
            return dict(row) if row else {"next_at": 0, "lease_until": 0, "error": ""}

    def practice_prepare_reconsider(self, track: str) -> None:
        """Wake a delayed round after its vocabulary changes, without interrupting an active owner."""

        with self._connect() as conn:
            conn.execute("UPDATE practice_preparation SET next_at=0 WHERE track=? AND owner IS NULL", (track,))

    def practice_ready(self, kind: str, opts, policy: dict) -> str | None:
        """Reuse a matching unexposed round; e.g. prefetch never marks a question shown or shuffles its saved choices."""

        if kind not in practice_tracks.QUESTION_KINDS:
            raise ValueError("Unknown practice mode")
        with self._connect() as conn:
            scope = practice_tracks.Track.for_options(kind, opts).session_filter
            where = f" AND {scope[0]}=?" if scope else ""
            params = (scope[1],) if scope else ()
            row = conn.execute(f"SELECT * FROM {kind}_sessions WHERE status='active'{where} ORDER BY created_at DESC LIMIT 1", params).fetchone()
            if row is None:
                return None
            saved = ({"direction": row["direction"], "count": row["requested_count"], "include_early": bool(row["include_early"])}
                     if kind == "practice" else json.loads(row["options_json"]))
            if saved != opts.model_dump(mode="json"):
                return None
            rows = conn.execute(f"SELECT p.shown_at,q.model,q.reasoning,q.prompt_version,q.flagged_at FROM {kind}_presentations p "
                                f"JOIN {kind}_questions q ON q.id=p.question_id WHERE p.session_id=?", (row["id"],)).fetchall()
            healthy = len(rows) == opts.count and all(item["shown_at"] is None and item["flagged_at"] is None
                                   and all(item[key] == value for key, value in policy.items()) for item in rows)
            return row["id"] if healthy else None

    @staticmethod
    def _practice_check_rounds(conn) -> None:
        """Return incomplete unused rounds to their cached stock; e.g. startup never offers a 34-question ready round requested as 35."""

        for kind in practice_tracks.QUESTION_KINDS:
            count = "s.requested_count" if kind == "practice" else "json_extract(s.options_json,'$.count')"
            conn.execute(f"UPDATE {kind}_sessions AS s SET status='stopped',finished_at=? WHERE s.status='active' "
                         f"AND (SELECT COUNT(*) FROM {kind}_presentations p WHERE p.session_id=s.id)<>{count} "
                         f"AND NOT EXISTS(SELECT 1 FROM {kind}_presentations p WHERE p.session_id=s.id AND p.shown_at IS NOT NULL)",
                         (time.time(),))

    def practice_unused(self, kind: str, policy: dict) -> set[int]:
        """Locate approved questions never presented; e.g. stopping a round returns its unseen questions to useful stock."""

        key = {"practice": "lookup_id", "verb": "target_id"}[kind]
        where = " AND ".join(f"q.{name}=:{name}" for name in policy)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT q.{key} FROM {kind}_questions q LEFT JOIN {kind}_presentations p ON p.question_id=q.id "
                                f"WHERE q.flagged_at IS NULL AND {where} GROUP BY q.id HAVING COUNT(p.shown_at)=0", policy)
            return {row[0] for row in rows}


def owner_alive(owner: str) -> bool:
    """Check PID and process birth time together; e.g. a crash or recycled PID cannot block preparation until lease expiry."""

    try:
        pid, born, _ = owner.split(":", 2)
        return abs(psutil.Process(int(pid)).create_time() - float(born)) < 0.01
    except psutil.NoSuchProcess:
        return False
    except (ValueError, psutil.AccessDenied):
        return True


def question_policy(engine) -> dict:
    """Identify checked question stock, e.g. speech uses the same round as the foreground."""

    provider = engine._provider
    return dict(model=provider.model, reasoning=provider.reasoning, prompt_version=engine.start.prompt_version)


@contextlib.contextmanager
def preparing(db, track: str, report):
    """Share generation ownership, cancellation and cooldown across round shapes; e.g. a passage audit cannot race a second foreground request."""

    owner = f"{os.getpid()}:{psutil.Process().create_time()}:{uuid.uuid4().hex}"
    while not db.practice_prepare_claim(track, owner):
        report('Finishing background preparation for this mode…')
        time.sleep(.25)
    error, delay = '', 0

    def progress(message):
        """Renew only the current lease at worker milestones; e.g. a cancelled task cannot publish readiness for another owner."""

        report(message)
        if not db.practice_prepare_touch(track, owner):
            raise RuntimeError('A newer preparation owns this mode.')

    try:
        yield progress
    except Exception as exc:
        from yt_whisper_subs import practice_round

        error = '' if isinstance(exc, task_cancel.CancelledError) else str(exc)
        healthy = isinstance(exc, practice_round.NothingToPractice) and exc.reason in {'not_due', 'no_words', 'not_enough'}
        delay = 60 if healthy else 0
        raise
    finally:
        db.practice_prepare_done(track, owner, error, delay=delay)


def serialized(kind: str, version: int, *, materialize: str | None = None):
    """Wrap paid preparation with shared ownership; e.g. each mode keeps its own linguistic strategy and SQL transactions."""

    def decorate(fn):
        """Preserve the service signature for normal callers; e.g. injected providers still exercise real coordination."""

        @functools.wraps(fn)
        def prepare(engine, opts, report, *, background: bool = False, **kwargs):
            """Wait cooperatively and reuse ready work; e.g. cancellation cannot unlock another worker's generation lease."""

            db = engine._db
            track = practice_tracks.Track.for_options(kind, opts).value
            provider = engine._provider
            policy = {"model": provider.model, "reasoning": provider.reasoning, "prompt_version": version}
            with preparing(db, track, report) as progress:
                # Recheck under ownership: the learner may have opened a round while this job waited.
                if session := db.practice_ready(kind, opts, policy):
                    if materialize:
                        with db._connect() as conn:
                            rows = [dict(row) for row in conn.execute(
                                f"SELECT q.* FROM {kind}_questions q JOIN {kind}_presentations p ON p.question_id=q.id "
                                "WHERE p.session_id=? ORDER BY p.ordinal", (session,))]
                        getattr(engine, materialize)(opts, rows, report)
                    report("Using your ready round")
                    return session
                if questions := db.practice_stock(practice_tracks.Track(track), opts, policy):
                    if materialize:
                        getattr(engine, materialize)(opts, questions, report)
                    if background:
                        return track
                    report("Using your prepared round")
                    options = opts.model_dump(mode="json") if kind == "practice" else opts
                    return getattr(db, f"{kind}_create")(options, questions, practice.ReviewPolicy().document())

                return fn(engine, opts, progress, background=background, **kwargs)

        prepare.prompt_version = version
        return prepare

    return decorate
