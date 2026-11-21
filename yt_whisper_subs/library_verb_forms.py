"""Native conjugation practice for one infinitive and a short typed answer."""

from yt_whisper_subs import i18n
from yt_whisper_subs import library_verb_controls
from yt_whisper_subs import library_written
from yt_whisper_subs import library_written_controls as written
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import verb_forms
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_service


class FormCard(written.WritingCard):
    """Show one infinitive, e.g. opbellen, with its requested subject and tense."""

    def __init__(self) -> None:
        """Size the editor for a form such as ‘zijn aangekomen’."""

        super().__init__()
        self.editor.setPlaceholderText(i18n.tr('Type the Dutch verb form…'))
        self.editor.setAccessibleName(i18n.tr('Your Dutch verb form'))
        self.editor.setFixedHeight(64)

    def present(self, row: dict) -> None:
        """Give the conjugation target without requiring or revealing a sentence."""

        subject = i18n.tr(verbs.SUBJECTS[row["subject"]])
        tense = verbs.Tense(row["tense"]).label
        instruction = i18n.tr(
            'Use the verb form from a statement that starts with the given subject. Leave out the subject '
            'and include any auxiliary, separable particle or reflexive pronoun.'
        )
        self.show_question(written.Question(source=row["lemma"], hint=f"{subject}  ·  {tense}",
                                            instruction=instruction, draft=row["answer_text"]))

    def feedback(self, row: dict) -> None:
        """Show the corrected form and the audited explanation after submission."""

        grade = row["grade"]
        self.show_feedback(written.Feedback(correct=grade["correct"], corrected=grade["corrected"],
                                            translation="", explanation=grade["explanation"], errors=[]))

    def dutch_speech(self, row: dict) -> str:
        """Offer the cached model form once the learner has answered."""

        return row["grade"]["corrected"] if row["grade"] and row["outcome"] != "invalid" else ""


class FormPanel(library_written.WrittenPanel):
    """Use separate settings and resumable rounds for standalone conjugation."""

    def __init__(self, db, *, engine: verb_service.VerbService | None = None) -> None:
        """Bind the form card to the same durable typed-practice lifecycle as sentences."""

        track = practice_tracks.Track.VERB_FORMS
        options = library_verb_controls.VerbOptions(db.setting(track.settings_key, ""), verb_forms.Options)
        mode = library_written.Mode(key="verb", settings_key=track.settings_key, label=track.label,
                                    tab_name=track.label, intro=i18n.tr('Conjugate a Dutch verb for a given subject and tense.'),
                                    view=written.WritingView(options, FormCard()), check_label=i18n.tr('Check verb form'),
                                    ops=library_written.SessionOps.for_db(db, track.kind, track.stock_key))
        super().__init__(db, mode, engine=engine or verb_service.VerbService(db))
