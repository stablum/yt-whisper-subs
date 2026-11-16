"""Native two-direction sentence, word, form and context progress statistics.

Example: compare Dutch writing accuracy with vocabulary meaning credit and recurring superlative mistakes.
"""

from __future__ import annotations

from datetime import datetime

from PySide6 import QtWidgets

from yt_whisper_subs import library_practice_labels as labels
from yt_whisper_subs import i18n
from yt_whisper_subs import library_practice_progress as charts
from yt_whisper_subs import library_practice_tables
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_stats


class SentenceProgress(QtWidgets.QWidget):
    """Show sentence learning with explicit denominators; e.g. unassessable adjective endings do not lower accuracy."""

    def __init__(self, db, *, listening: bool = False) -> None:
        """Build shared native charts and tables; e.g. viewing an empty history creates no exposure or model requests."""

        super().__init__()
        self._db = db
        self._listening = listening
        self._totals = QtWidgets.QLabel()
        self._totals.setObjectName("practiceStats")
        self._totals.setWordWrap(True)
        self._accuracy = charts.TrendChart(i18n.tr('Whole-sentence accuracy'), True)
        self._partial = charts.TrendChart(i18n.tr('Transcription word score') if listening else i18n.tr('Partial sentence score'), True)
        self._activity = charts.TrendChart(i18n.tr('Sentences checked per day'), False)
        self._tabs = library_practice_tables.StatsTables({
            i18n.mark('Sentences'): [i18n.tr('Date'), i18n.tr('Direction'), i18n.tr('Context'), i18n.tr('Partial score %'), i18n.tr('Whole sentence correct'), i18n.tr('Your sentence'), i18n.tr('Correction')],
            i18n.mark('Words'): [i18n.tr('Word'), i18n.tr('Saved meaning'), i18n.tr('Direction'), i18n.tr('Shown'), i18n.tr('Assessed'), i18n.tr('Word correct'), i18n.tr('Transcribed') if listening else i18n.tr('Meaning correct'), i18n.tr('Target used'),
                      i18n.tr('Form assessed'), i18n.tr('Form mistakes'), i18n.tr('Accuracy %'), i18n.tr('Next review')],
            i18n.mark('Forms'): [i18n.tr('Form'), i18n.tr('Direction'), i18n.tr('Assessed'), i18n.tr('Correct'), i18n.tr('Mistakes'), i18n.tr('Accuracy %')],
            i18n.mark('Life contexts'): [i18n.tr('Context'), i18n.tr('Direction'), i18n.tr('Answers'), i18n.tr('Correct'), i18n.tr('Accuracy %'), i18n.tr('Partial score %')],
            i18n.mark('Mistakes'): [i18n.tr('Date'), i18n.tr('Direction'), i18n.tr('Context'), i18n.tr('Error'), i18n.tr('Your sentence'), i18n.tr('Correction'), i18n.tr('Explanation')],
            i18n.mark('Rounds'): [i18n.tr('Started'), i18n.tr('Direction'), i18n.tr('Status'), i18n.tr('Planned'), i18n.tr('Submitted'), i18n.tr('Correct'), i18n.tr('To revisit'), i18n.tr('Partial score %')],
        })
        refresh = QtWidgets.QPushButton(i18n.tr('Refresh sentence statistics'))
        refresh.clicked.connect(self.refresh)
        plots = QtWidgets.QHBoxLayout()
        plots.addWidget(self._partial)
        plots.addWidget(self._accuracy)
        plots.addWidget(self._activity)
        note = QtWidgets.QLabel(i18n.tr(
            'Partial score measures word edits needed to reach your minimally corrected sentence, '
            'including supporting words, omissions and word order. Accepted alternative wording earns'
            ' full credit. Each checked sentence counts equally in averages. Whole-sentence accuracy '
            'counts fully correct answers. Pending and reported answers are excluded; saved-word '
            'meaning and form remain separate skills.',
        ))
        note.setWordWrap(True)
        if listening:
            refresh.setText(i18n.tr('Refresh listening statistics'))
            note.setText(i18n.tr(
                'Listening scores compare your transcription with the actual spoken Dutch script. '
                'Capitalization and sentence punctuation are ignored; spelling and word order count. '
                'Partial score measures word edits. Saved-target results measure transcription, not a '
                'separate meaning test. Replays and slow listens are recorded separately; only submitted '
                'answers count as practice activity. Writing/translation totals stay separate.',
            ))
        note.setObjectName("settingsNote")
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._totals)
        layout.addLayout(plots, 1)
        layout.addWidget(self._tabs, 2)
        layout.addWidget(note)
        layout.addWidget(refresh)

    def refresh(self) -> None:
        """Derive both language directions from immutable history; e.g. submitted effort stays visible if its question is reported."""

        data = sentence_stats.snapshot(self._db, listening=self._listening)
        accuracy = i18n.number(data['correct'] / data['graded'], '.0%') if data["graded"] else "—"
        partial = i18n.number(data['partial_score'], '.0%') if data["partial_score"] is not None else "—"
        words = len({row["word_id"] for row in data["terms"]})
        self._totals.setText(i18n.tr(
            '{partial} partial score · {accuracy} whole-sentence accuracy\n{submitted} submitted · '
            '{correct} correct · {wrong} to revisit · {words} saved words practised\n{question_due} '
            'sentences due · {word_due} word skills due · {pending} awaiting correction',
            partial=partial,
            accuracy=accuracy,
            submitted=data['submitted'],
            correct=data['correct'],
            wrong=data['wrong'],
            words=words,
            question_due=data['question_due'],
            word_due=data['word_due'],
            pending=data['pending'],
        ))
        if self._listening:
            first = f"{data['first_listen_correct']}/{data['first_listen_answers']}" if data['first_listen_answers'] else "—"
            self._totals.setText(self._totals.text() + i18n.tr(
                '\n{listens} listens · {slow_listens} slow listens for checked answers · {first} correct '
                'after one normal listen',
                listens=data['listens'],
                slow_listens=data['slow_listens'],
                first=first,
            ))
        self._charts(data["daily"])
        self._answer_table(data["answers"])
        self._word_table(data["terms"])
        self._tabs.fill("Forms", [[i18n.tr(sentences.FORMS[row["feature"]]), sentences.Direction(row["direction"]).label,
                                    row["attempts"], row["correct"], row["failures"],
                                    round(row["correct"] / row["attempts"] * 100, 1) if row["attempts"] else None]
                                   for row in data["forms"]])
        self._tabs.fill("Life contexts", [[i18n.tr(sentences.SCENES[row["scene"]]), sentences.Direction(row["direction"]).label,
                                           row["attempts"], row["correct"], round(row["correct"] / row["attempts"] * 100, 1),
                                           round(row["partial_score"] * 100, 1)]
                                          for row in data["scenes"]])
        self._tabs.fill("Mistakes", [[(i18n.date_text(datetime.fromtimestamp(row['answered_at']), '%d %b %Y %H:%M'), row["answered_at"]),
                                      sentences.Direction(row["direction"]).label, i18n.tr(sentences.SCENES[row["scene"]]), labels.term(row["kind"]),
                                      row["answer_text"], row["corrected"], row["explanation"]] for row in data["errors"]])
        self._tabs.fill("Rounds", [[(i18n.date_text(datetime.fromtimestamp(row['created_at']), '%d %b %Y %H:%M'), row["created_at"]),
                                    sentences.Direction(row["direction"]).label, labels.term(row["status"]), row["planned"], row["submitted"],
                                    row["correct"], row["wrong"],
                                    round(row["partial_score"] * 100, 1) if row["partial_score"] is not None else None]
                                   for row in data["rounds"]])

    def _charts(self, daily: list[dict]) -> None:
        """Keep gradual credit beside strict accuracy in dated series; e.g. a day with no perfect sentences can still show improvement."""

        accuracy_series, partial_series, activity_series = [], [], []
        directions = [(sentences.Direction.LISTEN, "#83e5af")] if self._listening else [
            (sentences.Direction.NL_EN, "#66d9ef"), (sentences.Direction.EN_NL, "#ffd166")]
        for direction, color in directions:
            rows = [row for row in daily if row["direction"] == direction.value]
            points = [(row["day"], row["correct"] / row["attempts"] * 100, i18n.tr(
                '{correct}/{attempts} correct',
                correct=row['correct'],
                attempts=row['attempts'],
            )) for row in rows]
            accuracy_series.append({"label": direction.label, "color": color, "points": points})
            points = [(row["day"], row["partial_score"] * 100,
                       i18n.tr(
                           '{partial_score:.1%} average partial score · {attempts} checked sentences',
                           partial_score=row['partial_score'],
                           attempts=row['attempts'],
                       )) for row in rows]
            partial_series.append({"label": direction.label, "color": color, "points": points})
            points = [(row["day"], row["attempts"], f"{row['attempts']} sentences") for row in rows]
            activity_series.append({"label": direction.label, "color": color, "points": points})
        self._accuracy.set_series(accuracy_series)
        self._partial.set_series(partial_series)
        self._activity.set_series(activity_series)

    def _answer_table(self, rows: list[dict]) -> None:
        """Expose each score's actual wording and correction; e.g. averaging cannot hide an individual sentence's mistakes."""

        rows = sorted(rows, key=lambda row: row["answered_at"], reverse=True)
        self._tabs.fill("Sentences", [[(i18n.date_text(datetime.fromtimestamp(row['answered_at']), '%d %b %Y %H:%M'), row["answered_at"]),
                                       sentences.Direction(row["direction"]).label, i18n.tr(sentences.SCENES[row["scene"]]),
                                       round(row["partial_score"] * 100, 1), i18n.tr('Yes') if row["outcome"] == "correct" else i18n.tr('No'),
                                       row["answer_text"], row["corrected"]] for row in rows])

    def _word_table(self, rows: list[dict]) -> None:
        """Keep vocabulary meaning, inflection and whole-word success visible together; e.g. word-order mistakes do not lower unrelated meaning counts."""

        cells = []
        for row in rows:
            due = (i18n.date_text(datetime.fromtimestamp(row['due_at']), '%d %b %Y %H:%M'), row["due_at"]) if row["due_at"] else None
            accuracy = round(row["correct"] / row["attempts"] * 100, 1) if row["attempts"] else None
            cells.append([row["word"], row["meaning"], sentences.Direction(row["direction"]).label, row["shown"], row["attempts"],
                          row["correct"], row["meaning_correct"], row["target_used"], row["form_attempts"], row["form_failures"], accuracy, due])
        self._tabs.fill("Words", cells)
