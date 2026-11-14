"""Native two-direction sentence, word, form and context progress statistics.

Example: compare Dutch writing accuracy with vocabulary meaning credit and recurring superlative mistakes.
"""

from __future__ import annotations

from datetime import datetime

from PySide6 import QtWidgets

from yt_whisper_subs import library_practice_progress as charts
from yt_whisper_subs import library_practice_tables
from yt_whisper_subs import practice
from yt_whisper_subs import sentence_practice as sentences
from yt_whisper_subs import sentence_stats


class SentenceProgress(QtWidgets.QWidget):
    """Show sentence learning with explicit denominators; e.g. unassessable adjective endings do not lower accuracy."""

    def __init__(self, db) -> None:
        """Build shared native charts and tables; e.g. viewing an empty history creates no exposure or model requests."""

        super().__init__()
        self._db = db
        self._totals = QtWidgets.QLabel()
        self._totals.setObjectName("practiceStats")
        self._totals.setWordWrap(True)
        self._accuracy = charts.TrendChart("Whole-sentence accuracy", True)
        self._activity = charts.TrendChart("Sentences checked per day", False)
        self._tabs = library_practice_tables.StatsTables({
            "Words": ["Word", "Saved meaning", "Direction", "Shown", "Assessed", "Word correct", "Meaning correct", "Target used",
                      "Form assessed", "Form mistakes", "Accuracy %", "Next review"],
            "Forms": ["Form", "Direction", "Assessed", "Correct", "Mistakes", "Accuracy %"],
            "Life contexts": ["Context", "Direction", "Answers", "Correct", "Accuracy %"],
            "Mistakes": ["Date", "Direction", "Context", "Error", "Your sentence", "Correction", "Explanation"],
            "Rounds": ["Started", "Direction", "Status", "Planned", "Submitted", "Correct", "To revisit"],
        })
        refresh = QtWidgets.QPushButton("Refresh sentence statistics")
        refresh.clicked.connect(self.refresh)
        plots = QtWidgets.QHBoxLayout()
        plots.addWidget(self._accuracy)
        plots.addWidget(self._activity)
        note = QtWidgets.QLabel("Meaning and grammatical form are assessed separately for saved target words. "
                               "Other words are part of sentence correctness. A global syntax error can bring the sentence back "
                               "without failing every vocabulary target. Pending and reported answers do not affect accuracy.")
        note.setWordWrap(True)
        note.setObjectName("settingsNote")
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._totals)
        layout.addLayout(plots, 1)
        layout.addWidget(self._tabs, 2)
        layout.addWidget(note)
        layout.addWidget(refresh)

    def refresh(self) -> None:
        """Derive both language directions from immutable history; e.g. submitted effort stays visible if its question is reported."""

        data = sentence_stats.snapshot(self._db)
        accuracy = f"{data['correct'] / data['graded']:.0%}" if data["graded"] else "—"
        words = len({row["word_id"] for row in data["terms"]})
        self._totals.setText(f"{data['submitted']} submitted · {data['correct']} correct · {data['wrong']} to revisit · "
                             f"{accuracy} sentence accuracy · {words} saved words practised\n"
                             f"{data['question_due']} sentences due · {data['word_due']} word skills due · {data['pending']} awaiting correction")
        accuracy_series, activity_series = [], []
        for direction, color in ((practice.Direction.NL_EN, "#66d9ef"), (practice.Direction.EN_NL, "#ffd166")):
            rows = [row for row in data["daily"] if row["direction"] == direction.value]
            points = [(row["day"], row["correct"] / row["attempts"] * 100, f"{row['correct']}/{row['attempts']} correct") for row in rows]
            accuracy_series.append({"label": direction.label, "color": color, "points": points})
            points = [(row["day"], row["attempts"], f"{row['attempts']} sentences") for row in rows]
            activity_series.append({"label": direction.label, "color": color, "points": points})
        self._accuracy.set_series(accuracy_series)
        self._activity.set_series(activity_series)
        self._word_table(data["terms"])
        self._tabs.fill("Forms", [[sentences.FORMS[row["feature"]], practice.Direction(row["direction"]).label,
                                    row["attempts"], row["correct"], row["failures"],
                                    round(row["correct"] / row["attempts"] * 100, 1) if row["attempts"] else None]
                                   for row in data["forms"]])
        self._tabs.fill("Life contexts", [[sentences.SCENES[row["scene"]], practice.Direction(row["direction"]).label,
                                           row["attempts"], row["correct"], round(row["correct"] / row["attempts"] * 100, 1)]
                                          for row in data["scenes"]])
        self._tabs.fill("Mistakes", [[(datetime.fromtimestamp(row["answered_at"]).strftime("%d %b %Y %H:%M"), row["answered_at"]),
                                      practice.Direction(row["direction"]).label, sentences.SCENES[row["scene"]], row["kind"],
                                      row["answer_text"], row["corrected"], row["explanation"]] for row in data["errors"]])
        self._tabs.fill("Rounds", [[(datetime.fromtimestamp(row["created_at"]).strftime("%d %b %Y %H:%M"), row["created_at"]),
                                    practice.Direction(row["direction"]).label, row["status"], row["planned"], row["submitted"],
                                    row["correct"], row["wrong"]] for row in data["rounds"]])

    def _word_table(self, rows: list[dict]) -> None:
        """Keep vocabulary meaning, inflection and whole-word success visible together; e.g. word-order mistakes do not lower unrelated meaning counts."""

        cells = []
        for row in rows:
            due = (datetime.fromtimestamp(row["due_at"]).strftime("%d %b %Y %H:%M"), row["due_at"]) if row["due_at"] else None
            accuracy = round(row["correct"] / row["attempts"] * 100, 1) if row["attempts"] else None
            cells.append([row["word"], row["meaning"], practice.Direction(row["direction"]).label, row["shown"], row["attempts"],
                          row["correct"], row["meaning_correct"], row["target_used"], row["form_attempts"], row["form_failures"], accuracy, due])
        self._tabs.fill("Words", cells)
