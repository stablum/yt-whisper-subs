"""Native weekly activity bars and a browsable calendar, shared with reminder balloons.

Example: `ActivityPanel(db).refresh()` visualizes submitted answers without counting idle window time.
"""

from __future__ import annotations

import math
from datetime import date

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import practice_activity


class ActivityChart(QtWidgets.QWidget):
    """Keep seven explicit days and a meaningful zero baseline; e.g. reuse identical bars in the reminder."""

    def __init__(self, parent=None) -> None:
        """Plot answers by direction with readable weekday labels; e.g. inactive Tuesday still occupies a bar slot."""

        super().__init__(parent)
        self._days: list[practice_activity.Day] = []
        self._hits: list[tuple[QtCore.QRectF, str]] = []
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.setAccessibleName("Practice activity over the last seven days, in questions answered")
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def set_days(self, days: list[practice_activity.Day]) -> None:
        """Accept the shared zero-filled week; e.g. both surfaces show the same counts for Friday."""

        self._days = days
        description = "; ".join(f"{row.day:%A %d %B}: {row.answers} answers" for row in days)
        self.setAccessibleDescription(description)
        self.update()

    def paintEvent(self, _event) -> None:
        """Draw stacked counts rather than an accuracy score; e.g. mistakes contribute to the day's effort."""

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor("#1c212b"))
        painter.setPen(QtGui.QColor("#d9e3ef"))
        painter.drawText(12, 23, "Answers · last 7 days")
        for x, text, color in ((12, "NL → EN", "#66d9ef"), (108, "EN → NL", "#ffd166"),
                               (204, "Verbs", "#c4a1ff"), (285, "Sentences", "#ffac78")):
            painter.setPen(QtGui.QColor(color))
            painter.drawText(x, 44, text)
        self._hits = []
        if not self._days:
            return
        plot = QtCore.QRectF(36, 65, max(1, self.width() - 50), max(1, self.height() - 115))
        maximum = max(2, 2 * math.ceil(max(row.answers for row in self._days) / 2))
        for value in (0, maximum // 2, maximum):
            y = plot.bottom() - plot.height() * value / maximum
            painter.setPen(QtGui.QColor("#343f50"))
            painter.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
            painter.setPen(QtGui.QColor("#9fadc0"))
            painter.drawText(QtCore.QRectF(0, y - 10, 29, 20), QtCore.Qt.AlignmentFlag.AlignRight, str(value))
        step = plot.width() / len(self._days)
        width = min(50, step * 0.6)
        for idx, row in enumerate(self._days):
            center = plot.left() + step * (idx + 0.5)
            baseline = plot.bottom()
            for count, color in ((row.nl_en, "#66d9ef"), (row.en_nl, "#ffd166"),
                                 (row.verbs, "#c4a1ff"), (row.sentences, "#ffac78")):
                height = plot.height() * count / maximum
                painter.fillRect(QtCore.QRectF(center - width / 2, baseline - height, width, height), QtGui.QColor(color))
                baseline -= height
            painter.setPen(QtGui.QColor("#f2f5fa"))
            painter.drawText(QtCore.QRectF(center - step / 2, baseline - 22, step, 20),
                             QtCore.Qt.AlignmentFlag.AlignCenter, str(row.answers))
            painter.setPen(QtGui.QColor("#aebed1"))
            painter.drawText(QtCore.QRectF(center - step / 2, plot.bottom() + 7, step, 40),
                             QtCore.Qt.AlignmentFlag.AlignCenter, row.day.strftime("%a\n%d %b"))
            hint = (f"{row.day:%A, %d %B %Y}\n{row.answers} answers · {row.words} words · {row.verb_lemmas} verbs\n"
                    f"NL → EN: {row.nl_en} · EN → NL: {row.en_nl} · Verbs: {row.verbs}\n"
                    f"Sentences EN → NL: {row.sentence_en_nl} · NL → EN: {row.sentence_nl_en}")
            self._hits.append((QtCore.QRectF(center - step / 2, plot.top(), step, plot.height() + 45), hint))

    def mouseMoveEvent(self, event) -> None:
        """Reveal dates and direction counts on hover; e.g. inspect a small stacked bar."""

        for rect, text in self._hits:
            if rect.contains(event.position()):
                QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), text, self)
                return
        QtWidgets.QToolTip.hideText()


class ActivityCalendar(QtWidgets.QCalendarWidget):
    """Show answer counts within a familiar month calendar; e.g. keyboard navigation reveals a day's details."""

    def __init__(self, parent=None) -> None:
        """Keep native month/year navigation and date selection; e.g. browse a past November."""

        super().__init__(parent)
        self._days: dict[date, practice_activity.Day] = {}
        self._today = date.today()
        self.setObjectName("practiceCalendar")
        self.setAccessibleName("Practice activity calendar, stronger teal means more answers")
        self.setFirstDayOfWeek(QtCore.Qt.DayOfWeek.Monday)
        self.setVerticalHeaderFormat(QtWidgets.QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setMinimumHeight(330)
        self.setMinimumWidth(390)
        self.selectionChanged.connect(self.updateCells)

    def set_activity(self, activity: practice_activity.Activity) -> None:
        """Replace derived counts while preserving the browsed month; e.g. a new answer colors today's tile."""

        self._days = {row.day: row for row in activity.days}
        self._today = activity.today
        self.setMaximumDate(QtCore.QDate(activity.today.year, activity.today.month, activity.today.day))
        self.updateCells()

    def paintCell(self, painter: QtGui.QPainter, rect: QtCore.QRect, day: QtCore.QDate) -> None:
        """Keep numbers visible alongside intensity; e.g. color is never the only activity indicator."""

        stamp = date(day.year(), day.month(), day.day())
        row = self._days.get(stamp)
        count = row.answers if row else 0
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        tile = QtCore.QRectF(rect.adjusted(3, 3, -3, -3))
        if count:
            tint = ("#21474f", "#245c64", "#267680", "#278d98")[min(3, (count - 1).bit_length())]
        else:
            tint = "#232b38"
        painter.setOpacity(1 if day.month() == self.monthShown() and stamp <= self._today else 0.35)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(tint))
        painter.drawRoundedRect(tile, 5, 5)
        if day == self.selectedDate() or stamp == self._today:
            painter.setPen(QtGui.QPen(QtGui.QColor("#ffd166" if day == self.selectedDate() else "#66d9ef"), 2))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(tile, 5, 5)
        painter.setPen(QtGui.QColor("#e8f0f8"))
        painter.drawText(tile.adjusted(6, 3, -3, -3), QtCore.Qt.AlignmentFlag.AlignLeft, str(day.day()))
        if count:
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(tile.adjusted(3, 3, -6, -3),
                             QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignRight, str(count))
        painter.restore()


class ActivityPanel(QtWidgets.QWidget):
    """Combine daily effort, a seven-day chart and calendar details; e.g. reach it directly from a reminder."""

    def __init__(self, db, parent=None) -> None:
        """Keep activity beside the existing progress analytics; e.g. share the catalog and grading definitions."""

        super().__init__(parent)
        self._db = db
        self._data = practice_activity.Activity(today=date.today())
        self._totals = QtWidgets.QLabel()
        self._totals.setWordWrap(True)
        self._totals.setObjectName("practiceStats")
        self._chart = ActivityChart()
        self._calendar = ActivityCalendar()
        self._month = QtWidgets.QLabel()
        self._day = QtWidgets.QLabel()
        self._day.setObjectName("practiceDay")
        self._details = QtWidgets.QLabel()
        self._details.setWordWrap(True)
        today = QtWidgets.QPushButton("Today")
        today.clicked.connect(self._select_today)
        self._calendar.selectionChanged.connect(self._show_day)
        self._calendar.currentPageChanged.connect(self._show_month)
        detail = QtWidgets.QVBoxLayout()
        detail.addWidget(self._month)
        detail.addSpacing(20)
        detail.addWidget(self._day)
        detail.addWidget(self._details)
        detail.addStretch()
        detail.addWidget(today, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self._calendar, 3)
        bottom.addLayout(detail, 2)
        note = QtWidgets.QLabel("Activity counts submitted answers, including mistakes and answers to later-reported questions. "
                               "Opening a round or reporting before answering adds no activity. Accuracy excludes reported questions. "
                               "Each day uses your local calendar date.")
        note.setWordWrap(True)
        note.setObjectName("settingsNote")
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._totals)
        layout.addWidget(self._chart, 1)
        layout.addLayout(bottom, 2)
        layout.addWidget(note)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self._refresh_day)
        self._timer.start()

    def _refresh_day(self) -> None:
        """Roll a visible week over at local midnight; e.g. leave Activity open through Friday night."""

        if self.isVisible() and self._data.today != date.today():
            self.refresh()

    def refresh(self) -> None:
        """Read saved activity when opened or after an answer; e.g. no minute-by-minute full-history scan."""

        self._data = practice_activity.read(self._db)
        current, longest = self._data.streaks
        week = self._data.week
        active = sum(row.answers > 0 for row in week)
        self._totals.setText(
            f"This week · {sum(row.answers for row in week)} answers · {active}/7 active days · {current} day streak\n"
            f"All time · {self._data.answers} answers · {len(self._data.days)} active days · "
            f"{self._data.distinct_words} words · {self._data.distinct_verbs} verbs · longest streak {longest} days")
        self._chart.set_days(week)
        self._calendar.set_activity(self._data)
        self._show_day()
        self._show_month()

    def _show_day(self) -> None:
        """Describe the selected date even when inactive; e.g. zero is effort information, not missing data."""

        selected = self._calendar.selectedDate()
        stamp = date(selected.year(), selected.month(), selected.day())
        row = next((row for row in self._data.days if row.day == stamp), practice_activity.Day(day=stamp))
        self._day.setText(stamp.strftime("%A\n%d %B %Y"))
        if not row.answers:
            self._details.setText("No answers on this day.")
            return
        accuracy = f"{row.correct / row.graded:.0%} correct ({row.correct}/{row.graded} graded)" if row.graded else "No graded answers"
        self._details.setText(f"{row.answers} answers · {row.words} words · {row.verb_lemmas} verbs\n\n"
                              f"Words Dutch → English: {row.nl_en}\nWords English → Dutch: {row.en_nl}\nVerb transformations: {row.verbs}\n"
                              f"Sentences English → Dutch: {row.sentence_en_nl}\nSentences Dutch → English: {row.sentence_nl_en}\n\n{accuracy}\n"
                              f"{row.answers - row.graded} not graded (pending or reported)")

    def _show_month(self, *_args) -> None:
        """Summarize the displayed month independently of date selection; e.g. navigate to last month."""

        year, month = self._calendar.yearShown(), self._calendar.monthShown()
        rows = [row for row in self._data.days if row.day.year == year and row.day.month == month]
        self._month.setText(f"{date(year, month, 1):%B %Y}\n{sum(row.answers for row in rows)} answers · {len(rows)} active days")

    def _select_today(self) -> None:
        """Return both selection and month to today; e.g. finish browsing historical activity."""

        today = self._data.today
        self._calendar.setSelectedDate(QtCore.QDate(today.year, today.month, today.day))
        self._calendar.showSelectedDate()
