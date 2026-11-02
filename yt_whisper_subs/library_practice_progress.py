"""Native progress charts and sortable per-term analytics without a web view.

Example: `ProgressPanel(db).refresh()` redraws both practice directions from saved events.
"""

from __future__ import annotations

from datetime import datetime

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import practice
from yt_whisper_subs import practice_stats


class MetricItem(QtWidgets.QTableWidgetItem):
    """Sort formatted metrics by underlying values; e.g. 100% sorts after 75% rather than before it."""

    def __init__(self, label: str | int, value: str | float) -> None:
        """Separate human formatting from comparison; e.g. a local date retains its UTC timestamp as sort key."""

        super().__init__()
        self.setData(QtCore.Qt.ItemDataRole.DisplayRole, label)
        self.setData(QtCore.Qt.ItemDataRole.UserRole, value)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        """Compare like-typed column keys; e.g. missing metrics sort below real observations."""

        return self.data(QtCore.Qt.ItemDataRole.UserRole) < other.data(QtCore.Qt.ItemDataRole.UserRole)


class TrendChart(QtWidgets.QWidget):
    """Draw dated series with inspectable points; e.g. hover daily accuracy to see its sample size."""

    def __init__(self, title: str, percent: bool, parent=None) -> None:
        """Keep plotting independent from SQL; e.g. cumulative coverage shares this renderer."""

        super().__init__(parent)
        self._title, self._percent = title, percent
        self._series: list[dict] = []
        self._hits: list[tuple[QtCore.QPointF, str]] = []
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def set_series(self, series: list[dict]) -> None:
        """Use actual observations rather than invented zero days; e.g. no practice yesterday is a gap."""

        self._series = series
        self.update()

    def paintEvent(self, _event) -> None:
        """Render date-scaled lines and labels; e.g. a single practice day still has a visible dot."""

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor("#1c212b"))
        painter.setPen(QtGui.QColor("#edf2f8"))
        painter.drawText(18, 26, self._title)
        points = [point for series in self._series for point in series["points"]]
        self._hits = []
        if not points:
            painter.setPen(QtGui.QColor("#99a6b8"))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "Your practice history will appear here")
            return
        plot = QtCore.QRectF(60, 55, max(1, self.width() - 90), max(1, self.height() - 95))
        dates = sorted({point[0] for point in points})
        stamps = {day: datetime.fromisoformat(day).toordinal() for day in dates}
        lo, hi = min(stamps.values()), max(stamps.values())
        top = 100 if self._percent else max(1, max(point[1] for point in points))
        for ratio in (0, 0.5, 1):
            y = plot.bottom() - plot.height() * ratio
            painter.setPen(QtGui.QColor("#333c4a"))
            painter.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
            painter.setPen(QtGui.QColor("#98a6b8"))
            label = f"{top * ratio:.0f}" + ("%" if self._percent else "")
            painter.drawText(QtCore.QRectF(2, y - 10, 50, 20), QtCore.Qt.AlignmentFlag.AlignRight, label)
        painter.drawText(QtCore.QRectF(plot.left(), plot.bottom() + 12, 120, 24), dates[0])
        if len(dates) > 1:
            painter.drawText(QtCore.QRectF(plot.right() - 120, plot.bottom() + 12, 120, 24),
                             QtCore.Qt.AlignmentFlag.AlignRight, dates[-1])
        legend_x = 18
        for series_idx, series in enumerate(self._series):
            pen = QtGui.QPen(QtGui.QColor(series["color"]), 2.5)
            if series_idx:
                pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawText(legend_x, 46, series["label"])
            legend_x += painter.fontMetrics().horizontalAdvance(series["label"]) + 24
            path = QtGui.QPainterPath()
            for idx, (day, value, hint) in enumerate(series["points"]):
                x = plot.center().x() if hi == lo else plot.left() + plot.width() * (stamps[day] - lo) / (hi - lo)
                point = QtCore.QPointF(x, plot.bottom() - plot.height() * value / top)
                path.moveTo(point) if idx == 0 else path.lineTo(point)
                painter.drawEllipse(point, 3.5, 3.5)
                self._hits.append((point, f"{day} · {series['label']}\n{hint}"))
            painter.drawPath(path)

    def mouseMoveEvent(self, event) -> None:
        """Expose the denominator behind each point; e.g. 100% can mean one answer or fifty."""

        for point, text in self._hits:
            if (point - event.position()).manhattanLength() < 16:
                QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), text, self)
                return
        QtWidgets.QToolTip.hideText()


class ProgressPanel(QtWidgets.QWidget):
    """Combine direction-specific trends, vocabulary coverage, and review counts."""

    def __init__(self, db, parent=None) -> None:
        """Share the existing catalog; e.g. no separate analytics store to synchronize."""

        super().__init__(parent)
        self._db = db
        self._totals = QtWidgets.QLabel()
        self._totals.setObjectName("practiceStats")
        self._totals.setWordWrap(True)
        self._accuracy = TrendChart("Daily answer accuracy", True)
        self._coverage = TrendChart("Cumulative distinct words practiced", False)
        self._terms = QtWidgets.QTableWidget(0, 8)
        self._terms.setHorizontalHeaderLabels(["Word", "Direction", "Shown", "Correct", "Wrong", "Accuracy", "Median time", "Next review"])
        self._terms.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._terms.setAlternatingRowColors(True)
        self._terms.verticalHeader().hide()
        self._terms.horizontalHeader().setStretchLastSection(True)
        self._rounds = QtWidgets.QTableWidget(0, 8)
        self._rounds.setHorizontalHeaderLabels(["Started", "Direction", "Shown", "Correct", "Wrong", "Accuracy", "Answer time", "Status"])
        self._rounds.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._rounds.setAlternatingRowColors(True)
        self._rounds.verticalHeader().hide()
        self._rounds.horizontalHeader().setStretchLastSection(True)
        history = QtWidgets.QTabWidget()
        history.addTab(self._terms, "Words")
        history.addTab(self._rounds, "Rounds")
        note = QtWidgets.QLabel("Accuracy reflects changing review difficulty, not an overall proficiency score. "
                               "Established = 3+ correct answers, latest correct, and review stability of 21+ days. "
                               "Reported questions do not affect accuracy or scheduling. Dates use local time.")
        note.setWordWrap(True)
        note.setObjectName("settingsNote")
        charts = QtWidgets.QHBoxLayout()
        charts.addWidget(self._accuracy)
        charts.addWidget(self._coverage)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._totals)
        layout.addLayout(charts, 1)
        layout.addWidget(history, 1)
        layout.addWidget(note)

    def refresh(self) -> None:
        """Recompute when the tab opens or an answer changes; e.g. no periodic full-history scan."""

        data = practice_stats.snapshot(self._db)
        totals = data["totals"]
        accuracy = f"{totals['accuracy']:.0%}" if totals["accuracy"] is not None else "—"
        self._totals.setText(f"{totals['shown']} presentations    ·    {totals['attempts']} graded answers    ·    "
                             f"{accuracy} correct    ·    {data['due']} due reviews    ·    "
                             f"{data['established']} established words (per direction)    ·    {data['streak']} day streak")
        series = []
        for direction, tint in ((practice.Direction.NL_EN, "#66d9ef"), (practice.Direction.EN_NL, "#ffd166")):
            points = [(row["day"], row["accuracy"] * 100, f"{row['correct']}/{row['attempts']} correct")
                      for row in data["daily"] if row["direction"] == direction.value and row["accuracy"] is not None]
            series.append({"label": direction.label, "color": tint, "points": points})
        self._accuracy.set_series(series)
        points = [(row["day"], row["words"], f"{row['words']} distinct words") for row in data["coverage"]]
        self._coverage.set_series([{"label": "Words explored", "color": "#9ae78f", "points": points}])
        self._show_terms(data["terms"])
        self._show_rounds(data["rounds"])

    def _show_terms(self, terms: list[dict]) -> None:
        """Keep word-level table formatting together; e.g. compare recall difficulty across directions."""

        self._terms.setSortingEnabled(False)
        self._terms.setRowCount(len(terms))
        for idx, row in enumerate(sorted(terms, key=lambda item: (-item["wrong"], item["word"]))):
            accuracy = f"{row['accuracy']:.0%}" if row["accuracy"] is not None else "—"
            elapsed = f"{row['median_ms'] / 1000:.1f}s" if row["median_ms"] is not None else "—"
            due = datetime.fromtimestamp(row["due_at"]).strftime("%d %b %H:%M") if row["due_at"] else "Not graded"
            values = [row["word"], practice.Direction(row["direction"]).label, row["shown"], row["correct"],
                      row["wrong"], accuracy, elapsed, due]
            keys = [row["word"].casefold(), row["direction"], row["shown"], row["correct"], row["wrong"],
                    row["accuracy"] if row["accuracy"] is not None else -1,
                    row["median_ms"] if row["median_ms"] is not None else -1, row["due_at"] or -1]
            for col, (value, key) in enumerate(zip(values, keys)):
                self._terms.setItem(idx, col, MetricItem(value, key))
        self._terms.resizeColumnsToContents()
        self._terms.setSortingEnabled(True)

    def _show_rounds(self, rounds: list[dict]) -> None:
        """Show immutable round results with explicit denominators; e.g. a stopped round can still have useful answers."""

        self._rounds.setSortingEnabled(False)
        self._rounds.setRowCount(len(rounds))
        for idx, row in enumerate(rounds):
            attempts = row["correct"] + row["wrong"]
            accuracy = f"{row['correct'] / attempts:.0%}" if attempts else "—"
            values = [datetime.fromtimestamp(row["created_at"]).strftime("%d %b %Y %H:%M"),
                      practice.Direction(row["direction"]).label, row["shown"], row["correct"], row["wrong"],
                      accuracy, f"{row['answer_ms'] / 1000:.1f}s", row["status"].capitalize()]
            keys = [row["created_at"], row["direction"], row["shown"], row["correct"], row["wrong"],
                    row["correct"] / attempts if attempts else -1, row["answer_ms"], row["status"]]
            for col, (value, key) in enumerate(zip(values, keys)):
                self._rounds.setItem(idx, col, MetricItem(value, key))
        self._rounds.resizeColumnsToContents()
        self._rounds.setSortingEnabled(True)
