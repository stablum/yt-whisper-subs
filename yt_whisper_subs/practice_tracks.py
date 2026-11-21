"""Name independent practice choices and their saved preferences.

Example: English-to-Dutch sentences and Dutch-to-English sentences keep separate resumable rounds.
"""

import enum

from yt_whisper_subs import i18n
from yt_whisper_subs import practice
from yt_whisper_subs import sentence_practice
from yt_whisper_subs import comprehension
from yt_whisper_subs import verb_forms
from yt_whisper_subs import word_order


class Track(str, enum.Enum):
    """Keep navigation, preparation and settings on one identity; e.g. a direction is a practice destination rather than a hidden option."""

    WORD_NL_EN = "practice:nl_en"
    WORD_EN_NL = "practice:en_nl"
    SOUND = "practice:sound_nl"
    VERBS = "verb"
    VERB_FORMS = "verb:forms"
    SENTENCE_NL_EN = "sentence:nl_en"
    SENTENCE_EN_NL = "sentence:en_nl"
    WORD_ORDER = "word_order"
    PHRASE_ORDER = "word_order:phrases"
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
        if self.kind in {"verb", "word_order"}:
            return None
        direction = {'sentence': sentence_practice.Direction, 'comprehension': comprehension.Direction}.get(self.kind, practice.Direction)
        return direction(value) if value else None

    @property
    def variant(self) -> word_order.Variant | None:
        """Distinguish the two ordering modes that share a persistence family."""

        if self.kind == "word_order":
            return word_order.Variant.PHRASES if self == self.PHRASE_ORDER else word_order.Variant.WORDS
        return None

    @property
    def session_filter(self) -> tuple[str, str] | None:
        """Choose the scoped column and value for sessions and cached questions."""

        if self.kind == "verb":
            return "variant", self.stock_key
        if self.variant:
            return "variant", self.variant.value
        if direction := self.direction:
            return "direction", direction.value
        return None

    @property
    def stock_key(self) -> str:
        """Use the track suffix to keep standalone and directional stock independent."""

        return self.value.partition(":")[2]

    @property
    def label(self) -> str:
        """Use compact two-line top-level labels; e.g. both sentence directions remain one click away."""

        if self == self.SOUND:
            return i18n.tr('Sound alike\nListen and choose')
        if self.variant:
            return self.variant.label
        if self == self.LISTENING:
            return i18n.tr('Listening\nDutch dictation')
        if self == self.VERB_FORMS:
            return i18n.tr('Verb forms')
        if self.kind == 'comprehension':
            return self.direction.label + '\n' + i18n.tr('Text and questions')
        title = {"practice": i18n.tr('Word quiz'), "sentence": i18n.tr('Sentences'), "verb": i18n.tr('Verbs')}[self.kind]
        return f"{title}\n{self.direction.label}" if self.direction else i18n.tr('Verbs\nDutch transformations')

    @property
    def button_label(self) -> str:
        """Use the group heading for context, e.g. a Passages button needs only 'Reading'."""

        if self.variant:
            return self.label
        if self == self.VERBS:
            return i18n.tr('Verb transformations')
        if self == self.VERB_FORMS:
            return self.label
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
        if kind == "word_order":
            return cls.PHRASE_ORDER if opts.variant == word_order.Variant.PHRASES else cls.WORD_ORDER
        if kind == "verb":
            return cls.VERB_FORMS if opts.variant == verb_forms.VARIANT else cls.VERBS
        return cls(f"{kind}:{direction.value}" if direction else kind)


# Domain kinds are shared by relational stock and generation ownership.
QUESTION_KINDS = ('practice', 'verb', 'sentence', 'word_order')
