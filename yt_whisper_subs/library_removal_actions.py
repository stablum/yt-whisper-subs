"""Confirm selected and filtered cleanup through the same native removal flow.

Example: select Watched, then choose Remove filtered downloads to reclaim space.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import library_model
from yt_whisper_subs import library_workers
from yt_whisper_subs import library_yields


class RemovalActionsMixin:
    """Keep removal scope, confirmation, and worker dispatch in one UI concern.

    Example: `LibraryWindow` shares these slots with toolbar and menu actions.
    """

    def _remove_selected(self) -> None:
        """Confirm one selected download; e.g. Shift+Delete preserves its history."""

        record = self._selected_record()
        if not record or not record.downloaded or self._busy:
            return
        manifest = self._service.video_yields(record.meta.identity.video_id)
        selection = library_yields.YieldSelection((
            library_yields.YieldItem(record.meta.identity.title, manifest),
        ))
        if not manifest.paths or not self._confirm_yields(selection):
            return

        def remove(report: Callable[[str], None]) -> int:
            """Bind the exact approved manifest; e.g. the worker reports each unlink."""

            return self._service.remove_yields(manifest, report)

        self._run_task("Removing download…", library_workers.BackgroundTask(remove), lambda _: self.refresh())

    def _remove_filtered(self) -> None:
        """Snapshot all matching rows, independent of selection or scrolling.

        Example: Watched plus a channel and search cleans up only that intersection.
        """

        if self._busy:
            return
        proxy = self._ui.catalog.proxy
        video_ids: list[str] = []
        for row in range(proxy.rowCount()):
            record = proxy.index(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            video_ids.append(record.meta.identity.video_id)
        excluded = frozenset(self._video_queue)

        def inspect(report: Callable[[str], None]) -> library_yields.YieldSelection:
            """Inventory files outside the GUI thread; e.g. a library with 500 matches."""

            return self._service.yield_selection(video_ids, report, excluded=excluded)

        task = library_workers.BackgroundTask(inspect)
        self._run_task("Inspecting filtered downloads…", task, self._remove_inspected)

    def _remove_inspected(self, selection: library_yields.YieldSelection) -> None:
        """Review a frozen preview before starting removal; e.g. filters may have changed."""

        if not selection.items:
            message = "No removable downloads or generated files matched the captured filters."
            if selection.skipped:
                message += f" Skipped {len(selection.skipped)} active or pending video(s)."
            QtWidgets.QMessageBox.information(self, "Remove filtered downloads", message)
            return
        if not self._confirm_yields(selection, filtered=True):
            return
        excluded = frozenset(self._video_queue)

        def remove(report: Callable[[str], None]) -> library_yields.RemovalResult:
            """Recheck protected videos before executing; e.g. a newly queued repair."""

            return self._service.remove_yield_selection(selection, report, excluded=excluded)

        self._run_task("Removing filtered downloads…", library_workers.BackgroundTask(remove), self._removed_filtered)

    def _confirm_yields(
        self, selection: library_yields.YieldSelection, *, filtered: bool = False,
    ) -> bool:
        """Share exact-file confirmation and retained-history wording across removal scopes.

        Example: Cancel is the default even for a filter matching the whole library.
        """

        count = len(selection.items)
        title = "Remove filtered downloads?" if filtered else "Remove download and yields?"
        scope = f"{count} matching video(s)" if filtered else selection.items[0].title
        text = f"Remove downloads and generated files for {scope}?\n{selection.file_count} managed file(s)"
        if filtered:
            text += f" · approximately {library_model.format_size(selection.size_bytes)}"
        box = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Icon.Warning, title, text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            self,
        )
        box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        info = "Entries are marked Removed. Catalog entries, download and watch history stay, and you can download again."
        if filtered:
            info += " This uses the filter matches captured when you clicked, including rows outside the viewport."
            info += " Playing videos and pending or interrupted pipelines are skipped."
        box.setInformativeText(info)
        details = [f"{item.title}\n" + "\n".join(map(str, item.yields.paths)) for item in selection.items]
        if selection.skipped:
            details.append("Skipped active or pending videos:\n" + "\n".join(selection.skipped))
        box.setDetailedText("\n\n".join(details))
        return box.exec() == QtWidgets.QMessageBox.StandardButton.Yes

    def _removed_filtered(self, result: library_yields.RemovalResult) -> None:
        """Refresh the catalog and show the completed scope; e.g. 12 watched downloads."""

        self.refresh()
        message = f"Removed {result.videos} download(s) and {result.files} file(s)."
        if result.skipped:
            message += f" Skipped {result.skipped} active or pending video(s)."
        self.statusBar().showMessage(message, 10_000)
        self._ui.trace.append_message(message)
