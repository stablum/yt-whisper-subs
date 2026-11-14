"""Native verb learning trends, dimension breakdowns and inspectable mistake history.

Example: compare perfect-tense success with jij agreement, then inspect the exact corrected sentences.
"""

from __future__ import annotations

from datetime import datetime

from PySide6 import QtWidgets

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
        self._accuracy = charts.TrendChart("Verb transformation accuracy", True)
        self._activity = charts.TrendChart("Verb answers checked per day", False)
        columns = {
            "Forms": ["Dimension", "Form", "Assessed", "Correct", "Mistakes", "Accuracy %"],
            "Verbs": ["Verb", "Source", "Saved clicks", "Videos", "Shown", "Submitted", "Graded", "Correct", "Mistakes", "Accuracy %", "Median seconds"],
            "Error types": ["Error", "Affected answers", "Occurrences"],
            "Mistakes": ["Date", "Verb", "Tense", "Subject", "Error", "Your text", "Correction", "Explanation"],
            "Rounds": ["Started", "Status", "Planned", "Submitted", "Correct", "To revisit"],
        }
        self._tabs = library_practice_tables.StatsTables(columns)
        self._tables = self._tabs.tables
        refresh = QtWidgets.QPushButton("Refresh insights")
        refresh.clicked.connect(self.refresh)
        plots = QtWidgets.QHBoxLayout()
        plots.addWidget(self._accuracy)
        plots.addWidget(self._activity)
        note = QtWidgets.QLabel("Form accuracy assesses tense/mood, subject, structure and verb formation separately. "
                               "One wrong sentence can contain several errors. Pending and reported answers do not affect mastery. "
                               "Weak dimensions influence future targets; successful exact combinations receive longer review intervals.")
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
        accuracy = f"{data['correct'] / data['graded']:.0%}" if data["graded"] else "—"
        self._totals.setText(f"{data['submitted']} submitted · {data['correct']} correct · {data['wrong']} to revisit · "
                             f"{accuracy} accuracy · {data['due']} forms due · {data['pending']} awaiting correction")
        points = [(row["day"], row["correct"] / row["attempts"] * 100,
                   f"{row['correct']}/{row['attempts']} correct") for row in data["daily"]]
        self._accuracy.set_series([{"label": "Full sentence", "color": "#c4a1ff", "points": points}])
        points = [(row["day"], row["attempts"], f"{row['attempts']} checked answers") for row in data["daily"]]
        self._activity.set_series([{"label": "Checked answers", "color": "#66d9ef", "points": points}])
        catalog = {row["lemma"]: row for row in self._db.verb_catalog()}
        names = {str(row["id"]): row["lemma"] for row in catalog.values()}
        labels = {"tense": verbs.TENSE_LABELS, "subject": verbs.SUBJECTS, "structure": verbs.STRUCTURES, "verb": names}
        forms = []
        for row in data["metrics"]:
            label = labels[row["axis"]].get(row["value"], row["value"])
            forms.append([row["axis"].title(), label, row["attempts"], row["attempts"] - row["failures"],
                          row["failures"], round(row["accuracy"] * 100, 1)])
        self._tabs.fill("Forms", forms)
        terms = []
        for row in data["terms"]:
            entry = catalog[row["lemma"]]
            origin = "My vocabulary" if entry["lookups"] else ("Starter" if entry["starter"] else "Previous vocabulary")
            if entry["starter"] and entry["lookups"]:
                origin += " + starter"
            accuracy = round(row["correct"] / row["attempts"] * 100, 1) if row["attempts"] else "—"
            seconds = round(row["median_ms"] / 1000, 1) if row["median_ms"] is not None else "—"
            terms.append([row["lemma"], origin, entry["clicks"], entry["videos"], row["shown"], row["submitted"],
                          row["attempts"], row["correct"], row["attempts"] - row["correct"], accuracy, seconds])
        self._tabs.fill("Verbs", terms)
        self._tabs.fill("Error types", [[row["kind"].replace("_", " "), row["answers"], row["errors"]] for row in data["kinds"]])
        self._tabs.fill("Mistakes", [[(datetime.fromtimestamp(row["answered_at"]).strftime("%d %b %Y %H:%M"), row["answered_at"]), row["lemma"],
                                 verbs.TENSE_LABELS[row["tense"]], verbs.SUBJECTS[row["subject"]], row["kind"],
                                 row["answer_text"], f"{row['evidence'] or '(missing)'} → {row['correction']}", row["explanation"]]
                                for row in data["errors"]])
        self._tabs.fill("Rounds", [[(datetime.fromtimestamp(row["created_at"]).strftime("%d %b %Y %H:%M"), row["created_at"]), row["status"],
                               row["planned"], row["submitted"], row["correct"], row["wrong"]] for row in data["rounds"]])
