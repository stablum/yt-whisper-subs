"""Replay a submitted sequence as moving chips inside the editable sentence area."""

from math import pi, sin

from PySide6 import QtCore, QtGui, QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_order_blocks as blocks


class Correction(QtWidgets.QWidget):
    """Offer an in-place horizontal replay and the existing comparison diagram."""

    def __init__(self) -> None:
        """Show the animation first, with a scrubber and a persistent view choice per panel."""

        super().__init__()
        self._canvas, self._arrows = _Canvas(), blocks.Moves()
        self._view = QtWidgets.QComboBox()
        self._view.setAccessibleName(i18n.tr('Correction view'))
        self._view.addItems([i18n.tr('Horizontal replay'), i18n.tr('Arrow diagram')])
        self._time = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._time.setRange(0, 1000)
        self._time.setAccessibleName(i18n.tr('Replay position'))
        self._clock = QtCore.QVariantAnimation(self)
        self._clock.setStartValue(0)
        self._clock.setEndValue(self._time.maximum())
        self._clock.setDuration(3050)
        self._clock.valueChanged.connect(self._time.setValue)
        self._time.sliderPressed.connect(self._clock.stop)
        self._time.actionTriggered.connect(self._clock.stop)
        self._time.valueChanged.connect(self._canvas.seek)
        self._view.currentIndexChanged.connect(self._select)
        self._arrows.hide()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._canvas)
        layout.addWidget(self._arrows)
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self._time, 1)
        controls.addStretch(1)
        controls.addWidget(self._view)
        layout.addLayout(controls)

    def set_order(self, bank, colors, submitted, solution) -> None:
        """Keep both views on the same verified order, with no new grading."""

        self._canvas.set_order(bank, colors, submitted, solution)
        self._arrows.set_order(bank, colors, submitted, solution)
        self.replay()

    def _select(self, idx: int) -> None:
        """Switch layouts without clearing the learner's submitted blocks."""

        self._canvas.setVisible(idx == 0)
        self._time.setVisible(idx == 0)
        self._arrows.setVisible(idx == 1)
        self.replay()

    def replay(self) -> None:
        """Return to the submitted order, then follow each actual block to its destination."""

        self._clock.stop()
        if self._view.currentIndex() == 0:
            self._time.setValue(0)
            self._clock.start()
        else:
            self._arrows.replay()


class _Canvas(QtWidgets.QWidget):
    """Lay out real wrapped chips and let them travel along staggered horizontal paths."""

    def __init__(self) -> None:
        """Use the same type size and outline palette as the editable block bank."""

        super().__init__()
        self._bank, self._colors, self._before, self._after = [], {}, [], []
        self._start, self._end = {}, {}
        self._bin_y, self._t = 0, 0.0
        size = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        size.setHeightForWidth(True)
        self.setSizePolicy(size)
        font = self.font()
        font.setPointSize(12)
        self.setFont(font)

    def set_order(self, bank, colors, submitted, solution) -> None:
        """Remember exact submitted and verified sequences, including unused and omitted blocks."""

        self._bank, self._colors, self._before, self._after = bank, colors, submitted, solution
        descriptions = (" ".join(bank[idx] for idx in row) for row in (submitted, solution))
        self.setAccessibleDescription(" → ".join(descriptions))
        self.updateGeometry()
        self._layout()

    def seek(self, value: int) -> None:
        """Update the moving blocks and leave brief holds at both ends of playback."""

        self._t = min(1.0, max(0.0, (value / 1000 - .12) / .76))
        self.update()

    def resizeEvent(self, event) -> None:
        """Rewrap long units whenever the correction pane is resized."""

        super().resizeEvent(event)
        self._layout()

    def sizeHint(self) -> QtCore.QSize:
        """Provide a useful starting size before the parent assigns a width."""

        return QtCore.QSize(600, self.heightForWidth(600))

    def heightForWidth(self, width: int) -> int:
        """Reserve enough height for wrapping before the controls below are positioned."""

        return self._geometry(width)[-1]

    def _wrap(self, ids, x: int, y: int, width: int) -> tuple[dict[int, QtCore.QRectF], int]:
        """Place chips left to right and wrap only at a block boundary."""

        row_height, left, slots = 0, x, {}
        for idx in ids:
            size = blocks.chip_size(self.fontMetrics(), self._bank[idx], width)
            if left != x and left + size.width() > x + width:
                left, y = x, y + row_height + 5
                row_height = 0
            slots[idx] = QtCore.QRectF(left, y, size.width(), size.height())
            left += size.width() + 5
            row_height = max(row_height, size.height())
        return slots, y + row_height

    def _layout(self) -> None:
        """Update the chip positions after normal Qt layout."""

        self._start, self._end, self._bin_y, _ = self._geometry(self.width())
        self.update()

    def _geometry(self, total_width: int) -> tuple[dict, dict, int, int]:
        """Map common blocks to both sentences and missing or extra blocks to their bins."""

        width = total_width - 18
        before, before_bottom = self._wrap(self._before, 9, 47, width)
        after, after_bottom = self._wrap(self._after, 9, 47, width)
        stage = max(before_bottom, after_bottom) + 24
        bin_y = stage + 27
        excluded = [idx for idx in self._before if idx not in after]
        missing = [idx for idx in self._after if idx not in before]
        half = width // 2 - 7
        removed, removed_bottom = self._wrap(excluded, 9, bin_y, half)
        added, added_bottom = self._wrap(missing, total_width // 2 + 6, bin_y, half)
        height = max(removed_bottom, added_bottom) + 10 if excluded or missing else stage
        return before | added, after | removed, bin_y, height

    def paintEvent(self, _event) -> None:
        """Animate actual Dutch text blocks, with destination outlines as guides."""

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QColor("#40516a"))
        painter.setBrush(QtGui.QColor("#171f2d"))
        painter.drawRoundedRect(QtCore.QRectF(self.rect().adjusted(1, 1, -2, -2)), 8, 8)
        painter.setPen(QtGui.QColor("#c8d8ea"))
        phase = i18n.tr('Submitted order') if self._t == 0 else i18n.tr('Correct order')
        if 0 < self._t < 1:
            phase = i18n.tr('Rearranging blocks…')
        painter.drawText(10, 22, phase)
        if self._t < 1 and len(self._before) < len(self._start):
            painter.drawText(self.width() // 2 + 6, self._bin_y - 7, i18n.tr('Add to sentence'))
        if len(self._after) < len(self._end):
            painter.drawText(9, self._bin_y - 7, i18n.tr('Leave out'))
        if 0 < self._t < 1:
            painter.setOpacity(.34)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            for idx in self._after:
                painter.setPen(QtGui.QPen(QtGui.QColor(self._colors[idx]), 1, QtCore.Qt.PenStyle.DashLine))
                painter.drawRoundedRect(self._end[idx], 7, 7)
            painter.setOpacity(1)
        remove_first = .24 if len(self._after) < len(self._end) else 0.0
        add_last = .76 if len(self._before) < len(self._start) else 1.0
        for pos, (idx, start) in enumerate(self._start.items()):
            end = self._end[idx]
            if idx not in self._after:
                lo, hi = 0.0, remove_first
            elif idx not in self._before:
                lo, hi = add_last, 1.0
            else:
                span = add_last - remove_first
                delay = span * .10 * pos / max(1, len(self._start) - 1)
                lo, hi = remove_first + delay, add_last - span * .10 + delay
            t = min(1.0, max(0.0, (self._t - lo) / (hi - lo)))
            rect = self._at(start, end, t)
            blocks.paint_chip(painter, rect, self._bank[idx], self._colors[idx])

    @staticmethod
    def _at(start: QtCore.QRectF, end: QtCore.QRectF, t: float) -> QtCore.QRectF:
        """Ease chips between slots and arc opposite-direction moves away from one another."""

        ease = t * t * (3 - 2 * t)
        dx, dy = end.x() - start.x(), end.y() - start.y()
        arc = min(27.0, abs(dx) * .14) * sin(pi * ease) * (-1 if dx < 0 else 1)
        x = start.x() + dx * ease
        y = start.y() + dy * ease + arc
        width = start.width() + (end.width() - start.width()) * ease
        height = start.height() + (end.height() - start.height()) * ease
        return QtCore.QRectF(x, y, width, height)
