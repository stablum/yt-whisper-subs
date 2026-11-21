"""Show word-order progress independently of vocabulary recall."""

from datetime import datetime

import polars as pl
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_practice_progress
from yt_whisper_subs import library_practice_tables
from yt_whisper_subs import word_order


class Progress(QtWidgets.QWidget):
    """Display daily accuracy, partial credit and which Dutch constructions need work."""

    def __init__(self, db) -> None:
        """Reuse practice charts and tables so this page stays read-only."""

        super().__init__()
        self._db = db
        self._chart = library_practice_progress.TrendChart(i18n.tr('Sentence builder accuracy'), percent=True)
        self._table = library_practice_tables.StatsTables({
            i18n.mark('Word-order patterns'): [i18n.tr('Pattern'), i18n.tr('Answers'), i18n.tr('Correct'),
                                             i18n.tr('Partial score')],
        })
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._chart, 1)
        layout.addWidget(self._table, 1)

    def refresh(self) -> None:
        """Plot graded observations only, e.g. a reported exercise does not affect accuracy."""

        rows = self._db.word_order_results()
        days, skills = [], []
        if rows:
            for row in rows:
                row["day"] = datetime.fromtimestamp(row["answered_at"]).date().isoformat()
                row["correct"] = row["outcome"] == "correct"
            frame = pl.DataFrame(rows)
            columns = (pl.len().alias("answers"), pl.col("correct").mean(), pl.col("partial").mean())
            days = frame.group_by("day").agg(*columns).sort("day").to_dicts()
            skills = frame.group_by("pattern").agg(*columns).sort("answers", descending=True).to_dicts()
        series = [{"label": label, "color": color,
                   "points": [(day["day"], day[key] * 100, day["answers"]) for day in days]}
                  for key, label, color in (("correct", i18n.tr('Correct'), "#91d8ae"),
                                            ("partial", i18n.tr('Partial score'), "#9dc7fa"))]
        self._chart.set_series(series)
        table = [[i18n.tr(word_order.PATTERNS[row["pattern"]]), row["answers"],
                  (i18n.number(row["correct"], ".0%"), row["correct"]),
                  (i18n.number(row["partial"], ".0%"), row["partial"])] for row in skills]
        self._table.fill('Word-order patterns', table)
