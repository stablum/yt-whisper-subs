"""Name independent practice choices and their saved preferences.

Example: English-to-Dutch sentences and Dutch-to-English sentences keep separate resumable rounds.
"""

import enum

from yt_whisper_subs import i18n
from yt_whisper_subs import practice
from yt_whisper_subs import sentence_practice
from yt_whisper_subs import comprehension


class Track(str, enum.Enum):
    """Keep navigation, preparation and settings on one identity; e.g. a direction is a practice destination rather than a hidden option."""

    WORD_NL_EN = "practice:nl_en"
    WORD_EN_NL = "practice:en_nl"
    SOUND = "practice:sound_nl"
    VERBS = "verb"
    SENTENCE_NL_EN = "sentence:nl_en"
    SENTENCE_EN_NL = "sentence:en_nl"
    WORD_ORDER = "word_order"
    LISTENING = "sentence:listen_nl"
    READ_COMPREHENSION = "comprehension:read"
    LISTEN_COMPREHENSION = "comprehension:listen"

    @property
    def kind(self) -> str:
        """Select the domain's persistence family; e.g. both word directions use practice_questions."""

        return self.value.partition(":")[0]

    @property
    def direction(self) -> practice.Direction | sentence_practice.Direction | comprehension.Direction | None:
        """Bind translation direction once; e.g. verb transformations operate only in Dutch."""

        value = self.value.partition(":")[2]
        direction = {'sentence': sentence_practice.Direction, 'comprehension': comprehension.Direction}.get(self.kind, practice.Direction)
        return direction(value) if value else None

    @property
    def label(self) -> str:
        """Use compact two-line top-level labels; e.g. both sentence directions remain one click away."""

        if self == self.SOUND:
            return i18n.tr('Sound alike\nListen and choose')
        if self == self.WORD_ORDER:
            return i18n.tr('Sentence builder')
        if self == self.LISTENING:
            return i18n.tr('Listening\nDutch dictation')
        if self.kind == 'comprehension':
            return self.direction.label + '\n' + i18n.tr('Text and questions')
        title = {"practice": i18n.tr('Word quiz'), "sentence": i18n.tr('Sentences'), "verb": i18n.tr('Verbs')}[self.kind]
        return f"{title}\n{self.direction.label}" if self.direction else i18n.tr('Verbs\nDutch transformations')

    @property
    def button_label(self) -> str:
        """Use the group heading for context, e.g. a Passages button needs only 'Reading'."""

        if self == self.WORD_ORDER:
            return self.label
        if self == self.VERBS:
            return i18n.tr('Verb transformations')
        if self.kind == 'comprehension':
            return i18n.tr('Reading') if self == self.READ_COMPREHENSION else i18n.tr('Listening')
        return self.direction.label

    @property
    def settings_key(self) -> str:
        """Persist each destination's own round options; e.g. changing writing length does not change reading length."""

        return self.value + "_options"

    @classmethod
    def for_options(cls, kind: str, opts) -> Track:
        """Derive preparation ownership from the actual request; e.g. opposite directions cannot share a generation lease."""

        direction = getattr(opts, "direction", None)
        return cls(f"{kind}:{direction.value}" if direction else kind)


# Domain kinds are shared by relational stock and generation ownership.
QUESTION_KINDS = ('practice', 'verb', 'sentence', 'word_order')
