"""Run bounded resumable vocabulary enrichment independently of playback and dictation.

Example: a verifier failure leaves local listening feedback and saved proposals intact.
"""

import argparse

from yt_whisper_subs import vocabulary_accretion as accretion


class Worker:
    """Keep one provider policy per leased batch; e.g. clicks and practice failures share cached discoveries."""

    def __init__(self, db, owner: str, generator=None) -> None:
        """Allow isolated linguistic providers; e.g. integration tests never need API credentials."""

        self._db, self._owner = db, owner
        self._generator = generator

    def step(self) -> int:
        """Finish one stage and attach ready results; e.g. successful generation is durable before verification begins."""

        attached = self._db.accretion_apply()
        jobs = self._db.accretion_claim(self._owner)
        if not jobs:
            return attached
        try:
            args = argparse.Namespace(openai_practice_model=jobs[0]["model"], openai_practice_reasoning=jobs[0]["reasoning"])
            generator = self._generator or accretion.Generator(args)
            if jobs[0]["proposal"] is None:
                proposals = generator.analyse(jobs)
                if len(proposals) != len(jobs) or {item.id for item in proposals} != {row["id"] for row in jobs}:
                    raise ValueError("Incomplete vocabulary discovery batch")
                self._db.accretion_proposed(self._owner, proposals)
            else:
                verdicts = generator.verify(jobs)
                if set(verdicts) != {row["id"] for row in jobs}:
                    raise ValueError("Incomplete vocabulary verification batch")
                self._db.accretion_verified(self._owner, verdicts)
        except Exception as exc:
            self._db.accretion_failed(self._owner, str(exc))
        return len(jobs) + attached
