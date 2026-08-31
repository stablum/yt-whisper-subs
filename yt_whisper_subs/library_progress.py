"""Native table rendering for segmented per-video pipeline progress.

Example: `table.setItemDelegateForColumn(0, PipelineProgressDelegate(table))`.
"""

from __future__ import annotations

from PySide6 import QtCore
from PySide6 import QtGui
from PySide6 import QtWidgets

from yt_whisper_subs import library_model
from yt_whisper_subs import pipeline_progress as progress


class PipelineProgressDelegate(QtWidgets.QStyledItemDelegate):
    """Paint a compact multi-stage bar with an honest live phase label.

    Example: the catalog's Pipeline column installs this delegate.
    """

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> None:
        """Draw selection chrome, phase wording, and weighted bar segments.

        Example: Qt invokes `paint(...)` for every visible pipeline cell.
        """

        cell = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(cell, index)
        cell.text = ""
        style = cell.widget.style() if cell.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, cell, painter, cell.widget)

        update = index.data(library_model.PROGRESS_ROLE)
        if not isinstance(update, progress.Update):
            return
        rect = option.rect.adjusted(9, 4, -9, -4)
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        text_color = option.palette.highlightedText().color() if selected else QtGui.QColor("#e8edf4")
        muted_color = QtGui.QColor("#a3adba") if not selected else text_color
        state_colors = {
            progress.Stage.AVAILABLE: "#8fc7ff",
            progress.Stage.READY: "#71d99b",
            progress.Stage.FAILED: "#ff8b8b",
            progress.Stage.LIVE: "#ff8b8b",
        }
        if not selected and update.stage in state_colors:
            muted_color = QtGui.QColor(state_colors[update.stage])

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        label_font = QtGui.QFont(option.font)
        label_font.setPointSizeF(max(8.0, label_font.pointSizeF() - 0.5))
        painter.setFont(label_font)
        painter.setPen(text_color if progress.active(update) else muted_color)
        label_rect = QtCore.QRect(rect.left(), rect.top(), rect.width() - 44, 19)
        label = painter.fontMetrics().elidedText(
            update.label,
            QtCore.Qt.TextElideMode.ElideRight,
            label_rect.width(),
        )
        painter.drawText(label_rect, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, label)

        overall = progress.overall_fraction(update)
        show_percent = update.fraction is not None or update.stage in {progress.Stage.READY, progress.Stage.FAILED}
        if show_percent:
            painter.setPen(muted_color)
            percent_rect = QtCore.QRect(rect.right() - 42, rect.top(), 42, 19)
            painter.drawText(
                percent_rect,
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                f"{overall:.0%}",
            )

        track = QtCore.QRectF(rect.left(), rect.top() + 23, rect.width(), 8)
        self._paint_segments(painter, track, update, overall)
        painter.restore()

    def sizeHint(
        self,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> QtCore.QSize:
        """Reserve enough height for a label above the segmented bar.

        Example: table rows use this 48-pixel visual rhythm.
        """

        del option, index
        return QtCore.QSize(230, 48)

    @staticmethod
    def _paint_segments(
        painter: QtGui.QPainter,
        track: QtCore.QRectF,
        update: progress.Update,
        overall: float,
    ) -> None:
        """Fill weighted phase blocks up to the reached overall position.

        Example: 50% overall paints across multiple colored stage blocks.
        """

        gap = 2.0
        usable_width = track.width() - gap * (len(progress.STAGE_SPECS) - 1)
        x = track.left()
        start = 0.0
        failed = update.stage is progress.Stage.FAILED
        for idx, spec in enumerate(progress.STAGE_SPECS):
            width = usable_width * spec.weight
            if idx == len(progress.STAGE_SPECS) - 1:
                width = track.right() - x
            segment = QtCore.QRectF(x, track.top(), max(1.0, width), track.height())
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#343b47"))
            painter.drawRoundedRect(segment, 2.5, 2.5)

            fill_ratio = min(1.0, max(0.0, (overall - start) / spec.weight))
            if fill_ratio:
                fill = QtCore.QRectF(segment.left(), segment.top(), segment.width() * fill_ratio, segment.height())
                color = QtGui.QColor("#ff7675") if failed else QtGui.QColor(spec.color)
                painter.setBrush(color)
                painter.drawRoundedRect(fill, 2.5, 2.5)
            if update.stage is spec.stage:
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.setPen(QtGui.QPen(QtGui.QColor(spec.color).lighter(135), 1.0))
                painter.drawRoundedRect(segment.adjusted(0.5, 0.5, -0.5, -0.5), 2.5, 2.5)

            x += width + gap
            start += spec.weight
