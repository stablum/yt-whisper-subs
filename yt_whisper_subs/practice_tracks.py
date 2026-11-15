"""Name the five independent practice choices and their saved preferences.

Example: English-to-Dutch sentences and Dutch-to-English sentences keep separate resumable rounds.
"""

import enum

from yt_whisper_subs import practice


class Track(str, enum.Enum):
    """Keep navigation, preparation and settings on one identity; e.g. a direction is a practice destination rather than a hidden option."""

    WORD_NL_EN = "practice:nl_en"
    WORD_EN_NL = "practice:en_nl"
    VERBS = "verb"
    SENTENCE_EN_NL = "sentence:en_nl"
    SENTENCE_NL_EN = "sentence:nl_en"

    @property
    def kind(self) -> str:
        """Select the domain's persistence family; e.g. both word directions use practice_questions."""

        return self.value.partition(":")[0]

    @property
    def direction(self) -> practice.Direction | None:
        """Bind translation direction once; e.g. verb transformations operate only in Dutch."""

        value = self.value.partition(":")[2]
        return practice.Direction(value) if value else None

    @property
    def label(self) -> str:
        """Use compact two-line top-level labels; e.g. both sentence directions remain one click away."""

        title = {"practice": "Word quiz", "sentence": "Sentences", "verb": "Verbs"}[self.kind]
        return f"{title}\n{self.direction.label}" if self.direction else "Verbs\nDutch transformations"

    @property
    def settings_key(self) -> str:
        """Persist each destination's own round options; e.g. changing writing length does not change reading length."""

        return self.value + "_options"

    @classmethod
    def for_options(cls, kind: str, opts) -> Track:
        """Derive preparation ownership from the actual request; e.g. opposite directions cannot share a generation lease."""

        return cls(f"{kind}:{opts.direction.value}" if kind != "verb" else kind)


# Domain kinds are shared by relational stock and generation ownership.
KINDS = tuple(dict.fromkeys(track.kind for track in Track))
