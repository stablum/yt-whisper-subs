"""Build exact-sized approved rounds with bounded, incremental replacement work.

Example: a 35-question request starts with 35 targets, then prepares only missing replacements within 70 draft attempts.
"""

from __future__ import annotations

from collections import abc

import pydantic as pd


class NothingToPractice(ValueError):
    """Represent recoverable preparation states; e.g. insufficient distinct due words need a smaller round or early review."""

    def __init__(self, reason: str, message: str) -> None:
        """Keep machine routing separate from learner text; e.g. budget exhaustion never displays internal limits."""

        super().__init__(message)
        self.reason = reason


class Plan(pd.BaseModel):
    """Inject domain selection and checked-content strategies; e.g. words, verb targets and sentence anchors retain their own identities."""

    model_config = pd.ConfigDict(arbitrary_types_allowed=True)
    candidates: pd.SkipValidation[list[dict]]
    initial: pd.SkipValidation[list[dict]]
    select: abc.Callable
    load: abc.Callable
    key: abc.Callable
    seed: dict = pd.Field(default_factory=dict)


class Build(pd.BaseModel):
    """Own quota, progress and preparation policy; e.g. all three modes share the same total attempt guard."""

    model_config = pd.ConfigDict(arbitrary_types_allowed=True)
    opts: pd.BaseModel
    report: abc.Callable
    cached_first: bool
    background: bool = False
    _spent: int = pd.PrivateAttr(default=0)

    @property
    def remaining(self) -> int:
        """Count draft attempts rather than HTTP audit calls; e.g. cached questions spend no generation quota."""

        return 2 * self.opts.count - self._spent

    def take(self, rows: list[dict]) -> list[dict]:
        """Reserve only the next submitted work; e.g. one missing question never generates an extra full batch."""

        chosen = rows[:self.remaining]
        self._spent += len(chosen)
        return chosen

    def fill(self, plan: Plan) -> list[dict]:
        """Keep approved questions and top up only deficits; e.g. a failed distractor set is replaced without repeating successful work."""

        ready = dict(plan.seed)
        if len(ready) + len(plan.initial) < self.opts.count:
            available = len(ready) + len(plan.initial)
            raise NothingToPractice("not_enough", f"Only {available} distinct targets are ready. Choose a smaller round or enable early review.")
        seen, batch = set(ready), plan.initial
        while len(ready) < self.opts.count:
            if not batch:
                fresh = [row for row in plan.candidates if plan.key(row) not in seen]
                batch = plan.select(fresh, self.opts.count - len(ready))
                if not batch:
                    failed = [row for row in plan.candidates if plan.key(row) not in ready]
                    batch = plan.select(failed, self.opts.count - len(ready))
            if not batch:
                break
            keys = {plan.key(row) for row in batch}
            if len(keys) != len(batch) or keys & ready.keys():
                raise ValueError("A round selected duplicate target identities")
            seen.update(keys)
            checked = plan.load(batch, self)
            if not checked.keys() <= keys:
                raise ValueError("Prepared questions did not match their selected targets")
            ready.update(checked)
            if len(ready) == self.opts.count:
                return list(ready.values())
            if not self.remaining:
                break
            self.report("Preparing checked replacement questions…")
            batch = []
        if len(ready) == self.opts.count:
            return list(ready.values())
        raise NothingToPractice("no_questions", "Could not prepare a full checked round. Your progress and approved questions are saved. Please try again.")
