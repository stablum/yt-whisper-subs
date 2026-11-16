"""Compose a source pane, narrow history rail and mixed comprehension question.

Example: reading keeps the article visible while listening reveals no transcript before every answer is submitted.
"""

import html

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import comprehension as comp
from yt_whisper_subs import library_comprehension_audio
from yt_whisper_subs import library_comprehension_controls
from yt_whisper_subs import library_practice_controls
from yt_whisper_subs import library_sentence_review


class PassagePane(QtWidgets.QWidget):
    """Own display-safe source presentation; e.g. transcript text never enters a hidden browser before explicit post-submission reveal."""

    def __init__(self, db, direction, audio) -> None:
        """Keep audio separate from the visible transcript; e.g. source playback can proceed while an open answer is typed."""

        super().__init__()
        self._db, self._direction = db, comp.Direction(direction)
        self._passage_id, self._content = None, None
        self._revealed, self._allowed = False, False
        self.title = QtWidgets.QLabel()
        self.title.setWordWrap(True)
        self.title.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.title.setStyleSheet('font-size:18pt;font-weight:600;color:#ffd166;')
        self.audio = library_comprehension_audio.Narration(db, audio)
        self.browser = QtWidgets.QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setAccessibleName(i18n.tr('Dutch text'))
        self.browser.setStyleSheet('font-size:15pt;padding:10px;background:#1b202a;border:1px solid #384356;border-radius:8px;')
        self.reveal = QtWidgets.QPushButton(i18n.tr('View transcript'))
        self.reveal.setAutoDefault(False)
        self.reveal.clicked.connect(self._reveal)
        self.note = QtWidgets.QLabel(i18n.tr('The text stays hidden until all answers are submitted.'))
        self.note.setWordWrap(True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.title)
        layout.addWidget(self.audio)
        layout.addWidget(self.note)
        layout.addWidget(self.reveal)
        layout.addWidget(self.browser, 1)
        layout.addStretch(0)
        self.clear()

    def clear(self) -> None:
        """Remove an earlier article before preparing another; e.g. an old transcript never appears as the next listening source."""

        self.audio.stop()
        self._passage_id, self._content = None, None
        self.title.clear()
        self.browser.clear()
        self.browser.hide()
        self.audio.hide()
        self.note.hide()
        self.reveal.hide()

    def load(self, session: dict, summary: dict) -> bool:
        """Reveal reading text immediately and listening text only on request; e.g. arriving feedback cannot bypass the transcript gate."""

        changed = self._passage_id != session['passage_id']
        if changed:
            self._passage_id = session['passage_id']
            self._content = self._db.comprehension_passage(self._passage_id)['content']
            self._revealed = self._direction == comp.Direction.READ
            self.browser.clear()
        self._allowed = summary['submitted'] == summary['planned']
        listen = self._direction == comp.Direction.LISTEN
        self.audio.setVisible(listen)
        if listen:
            self.audio.load(session)
        self.reveal.setVisible(listen and not self._revealed)
        self.reveal.setEnabled(self._allowed)
        self.note.setVisible(listen and not self._revealed)
        self.layout().setStretch(5, int(listen and not self._revealed))
        self.title.setText(self._content['title'] if self._revealed else i18n.tr('Listen to the text'))
        if self._revealed:
            if changed or not self.browser.toPlainText():
                self._show_text()
            self.browser.show()
        else:
            self.browser.hide()
        return changed

    def _show_text(self) -> None:
        """Escape every model-supplied string before rich rendering; e.g. an angle bracket in the story cannot become an interactive link."""

        genre = html.escape(i18n.tr(comp.GENRES[self._content['genre']]))
        paragraphs = ''.join(f"<p><span style='color:#8fa2b9;font-size:10pt'>{idx}</span> {html.escape(text)}</p>"
                             for idx, text in enumerate(self._content['paragraphs'], 1))
        self.browser.setHtml(f'<p style="color:#a9b9cc">{genre}</p>{paragraphs}')

    def _reveal(self) -> None:
        """Require all immutable submissions before showing the transcript; e.g. a premature button signal reveals nothing."""

        if self._allowed and self._content:
            self._revealed = True
            self._show_text()
            self.browser.show()
            self.reveal.hide()
            self.note.hide()
            self.layout().setStretch(5, 0)
            self.title.setText(self._content['title'])


class View(QtWidgets.QWidget):
    """Keep primary actions outside independent source and feedback scroll areas; e.g. a long essay never pushes Submit below the window."""

    def __init__(self, db, direction, audio) -> None:
        """Compose existing native controls around one passage; e.g. resizing the split keeps question history immediately accessible."""

        super().__init__()
        self.options = library_comprehension_controls.Options(db, direction)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(self.status, 1)
        header.addWidget(self.options)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)
        self.source = PassagePane(db, direction, audio)
        self.card = library_comprehension_controls.Card()
        self.rail = library_sentence_review.QuestionRail(comprehension=True)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.card)
        question = QtWidgets.QWidget()
        question_layout = QtWidgets.QHBoxLayout(question)
        question_layout.setContentsMargins(0, 0, 0, 0)
        question_layout.addWidget(self.rail)
        question_layout.addWidget(scroll, 1)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.addWidget(self.source)
        split.addWidget(question)
        split.setSizes([480, 650])
        split.setChildrenCollapsible(False)
        self.action = library_practice_controls.ContinueButton(i18n.tr('Submit answer'))
        self.action.setObjectName('primaryButton')
        self.action.setMinimumSize(200, 44)
        self.action.setAutoDefault(False)
        self.retry = QtWidgets.QPushButton(i18n.tr('Retry correction'))
        self.flag = QtWidgets.QPushButton(i18n.tr('Report text or question'))
        self.cancel = QtWidgets.QPushButton(i18n.tr('Cancel preparation'))
        footer = QtWidgets.QHBoxLayout()
        for button in (self.flag, self.retry, self.cancel):
            button.setAutoDefault(False)
            footer.addWidget(button)
        footer.addStretch()
        footer.addWidget(self.action)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.bar)
        layout.addWidget(split, 1)
        layout.addLayout(footer)
