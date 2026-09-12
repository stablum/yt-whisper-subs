"""Persist adjustable catalog geometry independently from window construction.

Example: `LayoutMixin._store_ui_layout(window)` saves columns and pane sizes.
"""

from __future__ import annotations

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import library_model


_TABLE_LAYOUT_SETTING = "video_table_header_v1"
_CATALOG_LAYOUT_SETTING = "video_catalog_splitter_v1"
LAYOUT_SAVE_DELAY_MS = 250


def _decode_state(encoded: str) -> QtCore.QByteArray | None:
    """Decode persisted Qt state without trusting a corrupt setting.

    Example: `_decode_state(db.setting(key, ""))` feeds `restoreState()`.
    """

    if not encoded:
        return None
    try:
        state = QtCore.QByteArray.fromBase64(encoded.encode("ascii"))
    except UnicodeEncodeError:
        return None
    return state if not state.isEmpty() else None


def _encode_state(widget: QtWidgets.QHeaderView | QtWidgets.QSplitter) -> str:
    """Encode a native header or splitter state for the settings table.

    Example: `_encode_state(splitter)` preserves its manual sizes.
    """

    return bytes(widget.saveState().toBase64()).decode("ascii")


class LayoutMixin:
    """Own default, persisted, and resettable catalog geometry behavior.

    Example: `LibraryWindow` mixes this beside its UI-construction concern.
    """

    @staticmethod
    def _apply_default_catalog_layout(splitter: QtWidgets.QSplitter) -> None:
        """Give the table most space while retaining a useful inspector.

        Example: View -> Reset video inspector size restores this proportion.
        """

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([600, 240])

    @staticmethod
    def _apply_default_table_layout(
        table: QtWidgets.QTableView,
        model: library_model.VideoTableModel,
    ) -> None:
        """Enable interactive columns and apply readable shipped defaults.

        Example: View -> Reset column layout restores widths and order.
        """

        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        for visual_idx, column in enumerate(range(model.columnCount())):
            header.moveSection(header.visualIndex(column), visual_idx)
            size = model.headerData(
                column,
                QtCore.Qt.Orientation.Horizontal,
                QtCore.Qt.ItemDataRole.SizeHintRole,
            )
            if isinstance(size, QtCore.QSize):
                table.setColumnWidth(column, size.width())

    def _restore_table_layout(self) -> None:
        """Restore Qt's versioned header state after safe defaults.

        Example: reopening restores column widths and positions.
        """

        encoded = self._service.db.setting(_TABLE_LAYOUT_SETTING, "")
        state = _decode_state(encoded)
        if state is not None:
            self._ui.catalog.table.horizontalHeader().restoreState(state)

    def _restore_catalog_layout(self) -> None:
        """Restore the user-selected table-to-inspector height proportion.

        Example: reopening keeps the detail pane where it was dragged.
        """

        encoded = self._service.db.setting(_CATALOG_LAYOUT_SETTING, "")
        state = _decode_state(encoded)
        if state is not None:
            self._ui.catalog.splitter.restoreState(state)

    def _schedule_layout_store(self, *_change: int) -> None:
        """Debounce repeated header and splitter drags into one settings write.

        Example: resizing the inspector restarts the short save timer.
        """

        self._layout_timer.start(LAYOUT_SAVE_DELAY_MS)

    def _store_ui_layout(self) -> None:
        """Flush both independently restorable catalog layout states.

        Example: shutdown preserves columns and inspector height.
        """

        self._store_table_layout()
        self._store_catalog_layout()

    def _store_table_layout(self) -> None:
        """Persist the native header state containing widths and order.

        Example: shutdown flushes the latest table arrangement.
        """

        header = self._ui.catalog.table.horizontalHeader()
        self._service.db.set_setting(_TABLE_LAYOUT_SETTING, _encode_state(header))

    def _store_catalog_layout(self) -> None:
        """Persist the adjustable table-to-inspector splitter position.

        Example: dragging stores `video_catalog_splitter_v1`.
        """

        splitter = self._ui.catalog.splitter
        self._service.db.set_setting(_CATALOG_LAYOUT_SETTING, _encode_state(splitter))

    def _reset_table_layout(self) -> None:
        """Return every video column to its shipped order and width.

        Example: View -> Reset column layout repairs an awkward arrangement.
        """

        catalog = self._ui.catalog
        self._apply_default_table_layout(catalog.table, catalog.model)
        self._layout_timer.stop()
        self._store_table_layout()

    def _reset_catalog_layout(self) -> None:
        """Return the resizable video inspector to its shipped height.

        Example: View -> Reset video inspector size recovers an awkward split.
        """

        self._apply_default_catalog_layout(self._ui.catalog.splitter)
        self._layout_timer.stop()
        self._store_catalog_layout()
