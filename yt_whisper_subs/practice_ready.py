"""Coordinate durable ready rounds and single-owner generation across practice modes.

Example: opening Sentences during background preparation waits for that job instead of paying for a second batch.
"""

from __future__ import annotations

import functools
import json
import os
import time
import uuid

import psutil


KINDS = ("practice", "verb", "sentence")
SCHEMA = """
CREATE TABLE IF NOT EXISTS practice_preparation (
    kind TEXT PRIMARY KEY CHECK(kind IN ('practice','verb','sentence')),
    owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
"""


class ReadyDbMixin:
    """Own preparation leases separately from answer history; e.g. waiting and caching add no learning activity."""

    def practice_prepare_claim(self, kind: str, owner: str) -> bool:
        """Elect one generator atomically; e.g. the foreground and startup worker cannot prepare duplicate rounds."""

        now = time.time()
        with self._connect() as conn:
            row = conn.execute("SELECT owner FROM practice_preparation WHERE kind=? AND lease_until>?", (kind, now)).fetchone()
            if row and row[0] and not _owner_alive(row[0]):
                conn.execute("UPDATE practice_preparation SET lease_until=0 WHERE kind=? AND owner=?", (kind, row[0]))
            cursor = conn.execute("INSERT INTO practice_preparation(kind,owner,lease_until) VALUES(?,?,?) "
                                  "ON CONFLICT(kind) DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until "
                                  "WHERE practice_preparation.lease_until<=?", (kind, owner, now + 900, now))
            return cursor.rowcount == 1

    def practice_prepare_touch(self, kind: str, owner: str) -> bool:
        """Renew on actual work milestones; e.g. a long checked round retains ownership between bounded API calls."""

        with self._connect() as conn:
            cursor = conn.execute("UPDATE practice_preparation SET lease_until=? WHERE kind=? AND owner=?", (time.time() + 900, kind, owner))
            return cursor.rowcount == 1

    def practice_prepare_done(self, kind: str, owner: str, error: str, *, delay: int = 60) -> None:
        """Release ownership with persistent failure backoff; e.g. a provider outage cannot trigger paid retries every minute."""

        with self._connect() as conn:
            conn.execute("UPDATE practice_preparation SET owner=NULL,lease_until=0,next_at=?,error=? WHERE kind=? AND owner=?",
                         (time.time() + delay, error[:2000], kind, owner))

    def practice_prepare_status(self, kind: str) -> dict:
        """Read readiness without doing work; e.g. startup respects a failed request's cooldown after a restart."""

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM practice_preparation WHERE kind=?", (kind,)).fetchone()
            return dict(row) if row else {"next_at": 0, "lease_until": 0, "error": ""}

    def practice_ready(self, kind: str, opts, policy: dict) -> str | None:
        """Reuse a matching unexposed round; e.g. prefetch never marks a question shown or shuffles its saved choices."""

        if kind not in KINDS:
            raise ValueError("Unknown practice mode")
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {kind}_sessions WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
            if row is None:
                return None
            saved = ({"direction": row["direction"], "count": row["requested_count"], "include_early": bool(row["include_early"])}
                     if kind == "practice" else json.loads(row["options_json"]))
            if saved != opts.model_dump(mode="json"):
                return None
            rows = conn.execute(f"SELECT p.shown_at,q.model,q.reasoning,q.prompt_version,q.flagged_at FROM {kind}_presentations p "
                                f"JOIN {kind}_questions q ON q.id=p.question_id WHERE p.session_id=?", (row["id"],)).fetchall()
            healthy = rows and all(item["shown_at"] is None and item["flagged_at"] is None
                                   and all(item[key] == value for key, value in policy.items()) for item in rows)
            return row["id"] if healthy else None

    def practice_unused(self, kind: str, policy: dict) -> set[int]:
        """Locate approved questions never presented; e.g. stopping a round returns its unseen questions to useful stock."""

        key = {"practice": "lookup_id", "verb": "target_id"}[kind]
        where = " AND ".join(f"q.{name}=:{name}" for name in policy)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT q.{key} FROM {kind}_questions q LEFT JOIN {kind}_presentations p ON p.question_id=q.id "
                                f"WHERE q.flagged_at IS NULL AND {where} GROUP BY q.id HAVING COUNT(p.shown_at)=0", policy)
            return {row[0] for row in rows}


def _owner_alive(owner: str) -> bool:
    """Check PID and process birth time together; e.g. a crash or recycled PID cannot block preparation until lease expiry."""

    try:
        pid, born, _ = owner.split(":", 2)
        return abs(psutil.Process(int(pid)).create_time() - float(born)) < 0.01
    except psutil.NoSuchProcess:
        return False
    except (ValueError, psutil.AccessDenied):
        return True


def consume_first(rows: list[dict], opts, choose) -> list[dict]:
    """Apply normal adaptive selection to unused stock first; e.g. new targets fill only the remaining round slots."""

    selected = choose([row for row in rows if row.get("unused")], opts)
    remaining = opts.count - len(selected)
    if remaining:
        used = {id(row) for row in selected}
        selected.extend(choose([row for row in rows if id(row) not in used], opts.model_copy(update={"count": remaining})))
    return selected


def serialized(kind: str, version: int):
    """Wrap paid preparation with shared ownership; e.g. each mode keeps its own linguistic strategy and SQL transactions."""

    def decorate(fn):
        """Preserve the service signature for normal callers; e.g. injected providers still exercise real coordination."""

        @functools.wraps(fn)
        def prepare(engine, opts, report, *, background: bool = False, **kwargs):
            """Wait cooperatively and reuse ready work; e.g. cancellation cannot unlock another worker's generation lease."""

            db = engine._db
            owner = f"{os.getpid()}:{psutil.Process().create_time()}:{uuid.uuid4().hex}"
            provider = engine._provider
            policy = {"model": provider.model, "reasoning": provider.reasoning, "prompt_version": version}
            while not db.practice_prepare_claim(kind, owner):
                report("Finishing background preparation for this mode…")
                time.sleep(0.25)
            error, delay = "", 60
            try:
                # Recheck under ownership: the learner may have opened a round while this job waited.
                if background and (active := getattr(db, f"{kind}_active")()) is not None:
                    return active
                if session := db.practice_ready(kind, opts, policy):
                    report("Using your ready round")
                    return session

                def progress(message):
                    """Keep cancellation and lease renewal together; e.g. a closed window leaves safely cached work."""

                    report(message)
                    if not db.practice_prepare_touch(kind, owner):
                        raise RuntimeError("A newer preparation owns this mode. Use its ready round.")

                return fn(engine, opts, progress, **kwargs)
            except Exception as exc:
                from yt_whisper_subs import practice_service

                error = str(exc)
                healthy = isinstance(exc, practice_service.NothingToPractice) and exc.reason in {"not_due", "no_words"}
                delay = 60 if healthy else 3600
                raise
            finally:
                db.practice_prepare_done(kind, owner, error, delay=delay)

        return prepare

    return decorate
