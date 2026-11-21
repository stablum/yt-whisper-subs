"""Native sentence blocks with drag placement and a diagram of corrected movement."""

import math

from PySide6 import QtCore, QtGui, QtWidgets

from yt_whisper_subs import i18n


_ROLE = QtCore.Qt.ItemDataRole.UserRole
_MIME = "application/x-ytw-sentence-block"
_COLORS = (
    "#90c7f3", "#c6b0f5", "#8ed6b2", "#f5bd8b", "#a5dce6", "#e6abcc",
    "#e5d189", "#b6c6fc", "#a9d2a8", "#f4abaa", "#9bd4d2", "#d6c1ae",
    "#ead59e", "#b6b5f4",
)


def chip_size(metrics, label: str, available: int) -> QtCore.QSize:
    """Measure the complete label, wrapping long phrases instead of clipping the answer."""

    width = min(max(72, available), metrics.horizontalAdvance(label) + 32)
    box = metrics.boundingRect(QtCore.QRect(0, 0, width - 24, 400), QtCore.Qt.TextFlag.TextWordWrap, label)
    return QtCore.QSize(width, max(36, box.height() + 16))


def paint_chip(painter, rect, label, color, *, focused=False) -> None:
    """Keep bright text on dark tiles with a shared accent outline for each block."""

    accent = QtGui.QColor(color)
    base = QtGui.QColor("#192230")
    # A subtle tint keeps each fill dark while the outline carries the identity.
    fill = QtGui.QColor.fromRgbF(*(dark * .88 + bright * .12 for dark, bright in zip(base.getRgbF(), accent.getRgbF(), strict=True)))
    painter.setPen(QtGui.QPen(accent, 2 if focused else 1.4))
    painter.setBrush(fill)
    painter.drawRoundedRect(rect, 7, 7)
    if focused:
        painter.setPen(QtGui.QPen(QtGui.QColor("#edf5ff"), 1))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 4, 4)
    painter.setPen(QtGui.QColor("#f1f5fb"))
    inner = rect.adjusted(10, 0, -10, 0)
    flags = int(QtCore.Qt.AlignmentFlag.AlignCenter) | int(QtCore.Qt.TextFlag.TextWordWrap)
    painter.drawText(inner, flags, label)


class _Delegate(QtWidgets.QStyledItemDelegate):
    """Paint a large readable chip while retaining native keyboard and accessibility support."""

    def paint(self, painter, option, index) -> None:
        """Draw the chip and focus ring independently of the system highlight color."""

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setFont(option.font)
        selected = bool(option.state & (QtWidgets.QStyle.StateFlag.State_Selected | QtWidgets.QStyle.StateFlag.State_HasFocus))
        paint_chip(painter, QtCore.QRectF(option.rect.adjusted(2, 2, -2, -2)), index.data(),
                   index.data(_ROLE + 1), focused=selected)
        painter.restore()


class _Tiles(QtWidgets.QListWidget):
    """Provide a wrapping, accessible tile list, e.g. bank or assembled answer."""

    moved = QtCore.Signal(int, bool, int)

    def __init__(self, board, *, answer: bool) -> None:
        """Enable native drag motion while letting the board own the one true ordering."""

        super().__init__(board)
        self.answer = answer
        self.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.setMovement(QtWidgets.QListView.Movement.Static)
        self.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.setFlow(QtWidgets.QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setSpacing(3)
        self.setWordWrap(False)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMaximumHeight(117)
        self.setMinimumHeight(82)
        self.setItemDelegate(_Delegate(self))
        self.setStyleSheet("QListWidget {background:#171f2d;border:1px solid #40516a;border-radius:8px;padding:5px;}")
        font = self.font()
        font.setPointSize(12)
        self.setFont(font)
        self.itemClicked.connect(self._toggle)

    def _toggle(self, item) -> None:
        """A click appends to the answer or returns a block to its bank."""

        self.moved.emit(item.data(_ROLE), not self.answer, -1)

    def resizeEvent(self, event) -> None:
        """Reflow long blocks on narrow windows, including after a scrollbar appears."""

        super().resizeEvent(event)
        available = self.viewport().width() - 12
        for idx in range(self.count()):
            item = self.item(idx)
            size = chip_size(self.fontMetrics(), item.text(), available)
            if item.sizeHint() != size:
                item.setSizeHint(size)

    def startDrag(self, _actions) -> None:
        """Move the block visually without allowing Qt to duplicate or delete a list item."""

        if (item := self.currentItem()) is None:
            return
        mime = QtCore.QMimeData()
        mime.setData(_MIME, str(item.data(_ROLE)).encode())
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        rect = self.visualItemRect(item)
        drag.setPixmap(self.viewport().grab(rect))
        drag.setHotSpot(QtCore.QPoint(rect.width() // 2, rect.height() // 2))
        drag.exec(QtCore.Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event) -> None:
        """Accept drags between the two lists on this board only."""

        source = event.source()
        if isinstance(source, _Tiles) and source.parent() == self.parent() and event.mimeData().hasFormat(_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        """Accept movement within the board, including across wrapped lines."""

        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        """Insert before or after the tile under the pointer, or append in the empty area."""

        self.dragEnterEvent(event)
        if not event.isAccepted():
            return
        block_id = int(bytes(event.mimeData().data(_MIME)))
        point = event.position().toPoint()
        target = self.itemAt(point)
        index = self.count()
        if target is not None:
            index = self.row(target) + int(point.x() >= self.visualItemRect(target).center().x())
        self.moved.emit(block_id, self.answer, index)

    def keyPressEvent(self, event) -> None:
        """Let Space move a block and Ctrl+Left/Right adjust its position in the answer."""

        current = self.currentItem()
        if current is not None:
            if event.key() in (QtCore.Qt.Key.Key_Space, QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                self._toggle(current)
                return
            arrows = (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right)
            if self.answer and event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier and event.key() in arrows:
                offset = -1 if event.key() == arrows[0] else 2
                self.moved.emit(current.data(_ROLE), True, max(0, self.currentRow() + offset))
                return
        super().keyPressEvent(event)


class Board(QtWidgets.QWidget):
    """Coordinate the available blocks and the draft, with the same order for clicking and dragging."""

    changed = QtCore.Signal(list)

    def __init__(self, correction) -> None:
        """Show both destinations at once, so returning an unwanted block is always possible."""

        super().__init__()
        self._blocks, self._tray, self._selected = [], [], []
        self._colors = {}
        self._correction = correction
        self._bank, self._answer = _Tiles(self, answer=False), _Tiles(self, answer=True)
        self._bank_label = QtWidgets.QLabel(i18n.tr('Available blocks'))
        answer_label = QtWidgets.QLabel(i18n.tr('Your sentence'))
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for label, view in ((self._bank_label, self._bank), (answer_label, self._answer)):
            view.setAccessibleName(label.text())
            layout.addWidget(label)
            layout.addWidget(view)
            view.moved.connect(self._move)
        layout.addWidget(self._correction)
        self._correction.hide()

    @property
    def selected(self) -> list[int]:
        """Read a copy so callers cannot change the board without repainting."""

        return self._selected.copy()

    @property
    def colors(self) -> dict[int, str]:
        """Reuse shuffled, answer-neutral colors for the correction arrows."""

        return self._colors

    def open(self, blocks: list[str], tray: list[int], selected: list[int]) -> None:
        """Open a saved bank in its original random order and restore any unsent arrangement."""

        self._blocks, self._tray, self._selected = blocks, tray, selected.copy()
        self._colors = {block: _COLORS[idx] for idx, block in enumerate(tray)}
        self._correction.hide()
        self._bank_label.show()
        self._bank.show()
        self._bank.setEnabled(True)
        self._answer.setEnabled(True)
        self._answer.show()
        self._render()

    def reveal(self, solution: list[int] | None) -> None:
        """Keep the answer area in place while switching from editing to feedback."""

        self._bank_label.hide()
        self._bank.hide()
        self._answer.setEnabled(False)
        if solution is not None:
            self._correction.set_order(self._blocks, self._colors, self._selected, solution)
            self._answer.hide()
            self._correction.show()

    def clear_answer(self) -> None:
        """Send all selected blocks back to the bank in its original order."""

        self._selected.clear()
        self._render()
        self.changed.emit([])

    def _move(self, block: int, to_answer: bool, index: int) -> None:
        """Apply one move, including adjusting a drop index measured before removal."""

        if block in self._selected:
            previous = self._selected.index(block)
            self._selected.remove(block)
            index -= int(index > previous)
        if to_answer:
            self._selected.insert(len(self._selected) if index < 0 else index, block)
        self._render(block, to_answer)
        self.changed.emit(self.selected)

    def _render(self, focus: int | None = None, answer=False) -> None:
        """Keep available blocks in their original order and retain the selected item's keyboard focus."""

        available = [block for block in self._tray if block not in self._selected]
        for view, blocks in ((self._bank, available), (self._answer, self._selected)):
            view.clear()
            for block in blocks:
                label = self._blocks[block]
                item = QtWidgets.QListWidgetItem(label, view)
                item.setData(_ROLE, block)
                item.setData(_ROLE + 1, self._colors[block])
                item.setToolTip(label)
                item.setSizeHint(chip_size(view.fontMetrics(), label, view.viewport().width() - 12))
                if view.answer == answer and block == focus:
                    view.setCurrentItem(item)
                    view.scrollToItem(item)
                    view.setFocus()


class Moves(QtWidgets.QWidget):
    """Draw how each submitted block moves to the correction, including additions and exclusions."""

    def __init__(self) -> None:
        """Animate movement once when a correction appears, with replay on demand."""

        super().__init__()
        self._blocks, self._colors, self._before, self._after = [], {}, [], []
        self._t = 0.0
        self._animation = QtCore.QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(1150)
        self._animation.setEasingCurve(QtCore.QEasingCurve.Type.InOutCubic)
        self._animation.valueChanged.connect(self._phase)

    def set_order(self, blocks, colors, before, after) -> None:
        """Show the actual and closest valid order, e.g. an omitted finite verb enters from the center."""

        self._blocks, self._colors, self._before, self._after = blocks, colors, before, after
        self._fit_height()
        self.setAccessibleDescription(" → ".join((" ".join(blocks[idx] for idx in before), " ".join(blocks[idx] for idx in after))))
        self.replay()

    def replay(self) -> None:
        """Run the same transition again without changing the saved answer."""

        self._animation.stop()
        self._animation.start()

    def _phase(self, value) -> None:
        """Update only the painted progress, leaving both labelled columns stable."""

        self._t = value
        self.update()

    def resizeEvent(self, event) -> None:
        """Give both columns room for wrapped phrase labels."""

        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        """Fit the longest side of the before/after diagram."""

        col, right = self._columns()
        _, left_height = self._slots(self._before, 5, col)
        _, right_height = self._slots(self._after, right, col)
        self.setMinimumHeight(max(left_height, right_height) + 6)

    def _columns(self) -> tuple[float, float]:
        """Leave a center lane wide enough to see crossing arrows."""

        lane = min(150, self.width() * .26)
        col = (self.width() - lane - 10) / 2
        return col, col + lane + 5

    def _slots(self, ids, x, width) -> tuple[dict[int, QtCore.QRectF], int]:
        """Lay out a legible chip per row, allowing long blocks to wrap."""

        slots, y = {}, 28
        for block in ids:
            height = chip_size(self.fontMetrics(), self._blocks[block], int(width)).height()
            slots[block] = QtCore.QRectF(x, y, width, height - 4)
            y += height + 2
        return slots, y

    def paintEvent(self, _event) -> None:
        """Link color-matched blocks across two columns and animate the moved blocks along the arrows."""

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QColor("#d7e2ee"))
        width = self.width()
        col, right = self._columns()
        painter.drawText(5, 18, i18n.tr('Your sentence'))
        painter.drawText(int(right), 18, i18n.tr('Correct order'))
        lhs, _ = self._slots(self._before, 5, col)
        rhs, _ = self._slots(self._after, right, col)
        for block in (*self._before, *[idx for idx in self._after if idx not in lhs]):
            left, dest = lhs.get(block), rhs.get(block)
            color = self._colors[block]
            if left is not None:
                paint_chip(painter, left, self._blocks[block], color)
            if dest is not None:
                paint_chip(painter, dest, self._blocks[block], color)
            start = QtCore.QPointF(col + 11, left.center().y()) if left else QtCore.QPointF(width / 2, dest.center().y())
            end = QtCore.QPointF(right - 6, dest.center().y()) if dest else QtCore.QPointF(width / 2, left.center().y())
            self._arrow(painter, start, end, color, left is None or dest is None)

    def _arrow(self, painter, start, end, color, dashed: bool) -> None:
        """Indicate the destination with an arrowhead and animate a colored block marker along the path."""

        path = QtGui.QPainterPath(start)
        x = (start.x() + end.x()) / 2
        path.cubicTo(QtCore.QPointF(x, start.y()), QtCore.QPointF(x, end.y()), end)
        pen = QtGui.QPen(QtGui.QColor(color), 1.7)
        if dashed:
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.drawLine(end, end + QtCore.QPointF(-6, -4))
        painter.drawLine(end, end + QtCore.QPointF(-6, 4))
        if 0 < self._t < 1:
            pos = path.pointAtPercent(self._t)
            painter.setBrush(QtGui.QColor(color))
            radius = 4 + 3 * math.sin(math.pi * self._t)
            painter.drawEllipse(pos, radius, radius)
