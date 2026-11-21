"""Native verb learning trends, dimension breakdowns and inspectable mistake history.

Example: compare perfect-tense success with jij agreement, then inspect the exact corrected sentences.
"""

from __future__ import annotations

from datetime import datetime

from PySide6 import QtWidgets

from yt_whisper_subs import library_practice_labels as labels
from yt_whisper_subs import i18n
from yt_whisper_subs import library_practice_progress as charts
from yt_whisper_subs import verb_practice as verbs
from yt_whisper_subs import verb_stats
from yt_whisper_subs import library_practice_tables


class VerbProgress(QtWidgets.QWidget):
    """Keep all diagnostics in the existing native practice window; e.g. no separate analytics database."""

    def __init__(self, db) -> None:
        """Create sortable evidence tables and reusable charts; e.g. one day still yields an inspectable accuracy point."""

        super().__init__()
        self._db = db
        self._totals = QtWidgets.QLabel()
        self._totals.setWordWrap(True)
        self._totals.setObjectName("practiceStats")
        self._accuracy = charts.TrendChart(i18n.tr('Verb transformation accuracy'), True)
        self._activity = charts.TrendChart(i18n.tr('Verb answers checked per day'), False)
        columns = {
            i18n.mark('Forms'): [i18n.tr('Dimension'), i18n.tr('Form'), i18n.tr('Assessed'), i18n.tr('Correct'), i18n.tr('Mistakes'), i18n.tr('Accuracy %')],
            i18n.mark('Verbs'): [i18n.tr('Verb'), i18n.tr('Source'), i18n.tr('Saved clicks'), i18n.tr('Videos'), i18n.tr('Shown'), i18n.tr('Submitted'), i18n.tr('Graded'), i18n.tr('Correct'), i18n.tr('Mistakes'), i18n.tr('Accuracy %'), i18n.tr('Median seconds')],
            i18n.mark('Error types'): [i18n.tr('Error'), i18n.tr('Affected answers'), i18n.tr('Occurrences')],
            i18n.mark('Mistakes'): [i18n.tr('Date'), i18n.tr('Verb'), i18n.tr('Tense'), i18n.tr('Subject'), i18n.tr('Error'), i18n.tr('Your text'), i18n.tr('Correction'), i18n.tr('Explanation')],
            i18n.mark('Rounds'): [i18n.tr('Started'), i18n.tr('Status'), i18n.tr('Planned'), i18n.tr('Submitted'), i18n.tr('Correct'), i18n.tr('To revisit')],
        }
        self._tabs = library_practice_tables.StatsTables(columns)
        self._tables = self._tabs.tables
        refresh = QtWidgets.QPushButton(i18n.tr('Refresh insights'))
        refresh.clicked.connect(self.refresh)
        plots = QtWidgets.QHBoxLayout()
        plots.addWidget(self._accuracy)
        plots.addWidget(self._activity)
        note = QtWidgets.QLabel(i18n.tr(
            'Form accuracy assesses tense/mood, subject, structure and verb formation separately. One'
            ' wrong sentence can contain several errors. Pending and reported answers do not affect '
            'mastery. Weak dimensions influence future targets; successful exact combinations receive'
            ' longer review intervals.',
        ))
        note.setWordWrap(True)
        note.setObjectName("settingsNote")
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._totals)
        layout.addLayout(plots, 1)
        layout.addWidget(self._tabs, 2)
        layout.addWidget(note)
        layout.addWidget(refresh)

    def refresh(self) -> None:
        """Derive counts from original evidence; e.g. reporting removes a bad exercise from every breakdown."""

        data = verb_stats.snapshot(self._db)
        accuracy = i18n.number(data['correct'] / data['graded'], '.0%') if data["graded"] else "—"
        self._totals.setText(i18n.tr(
            '{submitted} submitted · {correct} correct · {wrong} to revisit · {accuracy} accuracy · '
            '{due} forms due · {pending} awaiting correction',
            submitted=data['submitted'],
            correct=data['correct'],
            wrong=data['wrong'],
            accuracy=accuracy,
            due=data['due'],
            pending=data['pending'],
        ))
        points = [(row["day"], row["correct"] / row["attempts"] * 100,
                   i18n.tr('{correct}/{attempts} correct', correct=row['correct'], attempts=row['attempts'])) for row in data["daily"]]
        self._accuracy.set_series([{"label": i18n.tr('Full sentence'), "color": "#c4a1ff", "points": points}])
        points = [(row["day"], row["attempts"], labels.graded_accuracy(row["correct"], row["attempts"]))
                  for row in data["daily"]]
        self._activity.set_series([{"label": i18n.tr('Checked answers'), "color": "#66d9ef", "points": points}])
        catalog = {row["lemma"]: row for row in self._db.verb_catalog()}
        names = {str(row["id"]): row["lemma"] for row in catalog.values()}
        axes = {"tense": verbs.TENSE_LABELS, "subject": verbs.SUBJECTS, "structure": verbs.STRUCTURES, "verb": names}
        forms = []
        for row in data["metrics"]:
            label = axes[row["axis"]].get(row["value"], row["value"])
            forms.append([labels.term(row["axis"]), i18n.tr(label), row["attempts"], row["attempts"] - row["failures"],
                          row["failures"], round(row["accuracy"] * 100, 1)])
        self._tabs.fill("Forms", forms)
        terms = []
        for row in data["terms"]:
            entry = catalog[row["lemma"]]
            origin = i18n.tr('My vocabulary') if entry["lookups"] else (i18n.tr('Starter') if entry["starter"] else i18n.tr('Previous vocabulary'))
            if entry["starter"] and entry["lookups"]:
                origin += i18n.tr(' + starter')
            accuracy = round(row["correct"] / row["attempts"] * 100, 1) if row["attempts"] else "—"
            seconds = round(row["median_ms"] / 1000, 1) if row["median_ms"] is not None else "—"
            terms.append([row["lemma"], origin, entry["clicks"], entry["videos"], row["shown"], row["submitted"],
                          row["attempts"], row["correct"], row["attempts"] - row["correct"], accuracy, seconds])
        self._tabs.fill("Verbs", terms)
        self._tabs.fill("Error types", [[labels.term(row["kind"]), row["answers"], row["errors"]] for row in data["kinds"]])
        self._tabs.fill("Mistakes", [[(i18n.date_text(datetime.fromtimestamp(row['answered_at']), '%d %b %Y %H:%M'), row["answered_at"]), row["lemma"],
                                 i18n.tr(verbs.TENSE_LABELS[row["tense"]]), i18n.tr(verbs.SUBJECTS[row["subject"]]), labels.term(row["kind"]),
                                 row["answer_text"], f"{row['evidence'] or '(missing)'} → {row['correction']}", row["explanation"]]
                                for row in data["errors"]])
        self._tabs.fill("Rounds", [[(i18n.date_text(datetime.fromtimestamp(row['created_at']), '%d %b %Y %H:%M'), row["created_at"]), labels.term(row["status"]),
                               row["planned"], row["submitted"], row["correct"], row["wrong"]] for row in data["rounds"]])
