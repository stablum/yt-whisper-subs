"""Compose top-level direction-specific practice and separate progress pages.

Example: Word quiz and Sentences each expose Dutch-to-English and English-to-Dutch directly.
"""

from __future__ import annotations

import enum
from functools import partial

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_practice_activity
from yt_whisper_subs import library_practice_nav
from yt_whisper_subs import library_practice_progress
from yt_whisper_subs import library_practice_sequence
from yt_whisper_subs import library_pronunciation
from yt_whisper_subs import library_sentence_progress
from yt_whisper_subs import library_sentences
from yt_whisper_subs import library_verb_progress
from yt_whisper_subs import library_verbs
from yt_whisper_subs import library_word_quiz
from yt_whisper_subs import practice
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import sentence_practice
from yt_whisper_subs import comprehension
from yt_whisper_subs import library_comprehension
from yt_whisper_subs import library_comprehension_progress


_STYLE = """
QLabel#practiceTitle { font-size: 24pt; font-weight: 700; color: #ffffff; }
QLabel#practicePrompt { font-size: 30pt; font-weight: 600; color: #ffd166; padding: 10px; }
QLabel#practiceContext { font-size: 13pt; color: #b8c7d9; padding: 6px; }
QLabel#practiceResult { font-size: 15pt; font-weight: 600; color: #dce6f2; padding: 8px 0; }
QLabel#practiceStats { font-size: 12pt; color: #c4d5e9; padding: 12px 0; }
QPushButton#practiceChoice { font-size: 17pt; text-align: left; padding: 12px 22px; border: 2px solid #3a424f; border-radius: 10px; }
QPushButton#practiceChoice[answerState=""]:pressed,
QPushButton#practiceChoice[answerState="pending"] { background: #243f60; border-color: #83b6ef; color: #eaf4ff; }
QPushButton#practiceChoice[answerState="correct"] { background: #214b3d; border: 2px solid #75dba4; color: #d2ffe5; }
QPushButton#practiceChoice[answerState="wrong"] { background: #552f39; border: 2px solid #ed8594; color: #ffe2e6; }
QToolButton#practiceOptions, QToolButton#practiceDetails { background: #2b313c; border: 1px solid #3a424f; padding: 5px 10px; border-radius: 6px; }
"""


class Page(enum.IntEnum):
    """Name visible page positions centrally; e.g. shortcuts and reminders select the same direct destination as its button."""

    WORD_NL_EN = 0
    WORD_EN_NL = 1
    VERBS = 2
    SENTENCE_EN_NL = 3
    SENTENCE_NL_EN = 4
    LISTENING = 5
    READ_COMPREHENSION = 6
    LISTEN_COMPREHENSION = 7
    PROGRESS = 8
    ACTIVITY = 9

    @classmethod
    def for_track(cls, track: practice_tracks.Track) -> Page:
        """Map the shared destination order to its page; e.g. native Ctrl+Tab includes both translation directions."""

        return cls(list(practice_tracks.Track).index(track))


class PracticeDialog(QtWidgets.QDialog):
    """Compose independent direction-bound panels without owning their rounds; e.g. navigating to tracking cannot start hidden practice."""

    activity_changed = QtCore.Signal()

    def __init__(self, service, parent=None, *, engine=None) -> None:
        """Construct direct practice destinations with one native page state; e.g. each direction keeps its draft when another is selected."""

        super().__init__(parent)
        self.setWindowTitle(i18n.tr('Practice · vocabulary, verbs, sentences and comprehension'))
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1160, 900)
        self._db = service.db
        self._quizzes = {direction: library_word_quiz.WordQuiz(self._db, direction, engine=engine) for direction in practice.Direction}
        self._sentences = {direction: library_sentences.SentencePanel(self._db, direction=direction) for direction in practice.Direction}
        self._verbs = library_verbs.VerbPanel(self._db)
        self._listening = library_sentences.SentencePanel(self._db, direction=sentence_practice.Direction.LISTEN)
        self._comprehension = {direction: library_comprehension.Panel(self._db, direction) for direction in comprehension.Direction}
        self._panels = {Page.WORD_NL_EN: self._quizzes[practice.Direction.NL_EN],
                        Page.WORD_EN_NL: self._quizzes[practice.Direction.EN_NL], Page.VERBS: self._verbs,
                        Page.SENTENCE_EN_NL: self._sentences[practice.Direction.EN_NL],
                        Page.SENTENCE_NL_EN: self._sentences[practice.Direction.NL_EN], Page.LISTENING: self._listening,
                        Page.READ_COMPREHENSION: self._comprehension[comprehension.Direction.READ],
                        Page.LISTEN_COMPREHENSION: self._comprehension[comprehension.Direction.LISTEN]}
        self._tabs = QtWidgets.QTabWidget()
        self._pronunciation = library_pronunciation.Controls(self._db)
        self._spoken = {page: (0, '') for page in self._panels
                        if page not in {Page.LISTENING, Page.LISTEN_COMPREHENSION}}
        self._comprehension[comprehension.Direction.READ].passage_requested.connect(self._pronunciation.read)
        self._sequence = library_practice_sequence.SequenceBar(self._db)
        self._sequence.requested.connect(self._show_sequence_track)
        for track in practice_tracks.Track:
            page = Page.for_track(track)
            panel = self._panels[page]
            panel.activity_changed.connect(partial(self._record_activity, track))
            panel.round_reviewed.connect(partial(self._sequence.reviewed, track))
            if page in self._spoken:
                panel.speech_changed.connect(partial(self._offer_pronunciation, page))
            self._tabs.addTab(panel, track.label)
        self._arrange_tracking()
        self._nav = library_practice_nav.PracticeNav(self._tabs, (
            (i18n.tr('Practice'), tuple(self._panels)), (i18n.tr('Tracking'), (Page.PROGRESS, Page.ACTIVITY)),
        ))
        self._tabs.currentChanged.connect(self._tab_changed)
        title = QtWidgets.QLabel(i18n.tr('Practice'))
        title.setObjectName("practiceTitle")
        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(title)
        heading.addWidget(self._sequence, 1)
        navigation = QtWidgets.QHBoxLayout()
        navigation.addWidget(self._nav, 1)
        navigation.addWidget(self._pronunciation, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(heading)
        layout.addLayout(navigation)
        layout.addWidget(self._tabs, 1)
        self.setStyleSheet(_STYLE)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.reject)

    def _arrange_tracking(self) -> None:
        """Keep analytics separate from exercise controllers; e.g. progress reads all directions without generating questions."""

        self._progress = library_practice_progress.ProgressPanel(self._db)
        self._verb_progress = library_verb_progress.VerbProgress(self._db)
        self._sentence_progress = library_sentence_progress.SentenceProgress(self._db)
        self._listening_progress = library_sentence_progress.SentenceProgress(self._db, listening=True)
        self._comprehension_progress = {direction: library_comprehension_progress.Progress(self._db, direction) for direction in comprehension.Direction}
        self._progress_tabs = QtWidgets.QTabWidget()
        for panel, label in ((self._progress, i18n.tr('Vocabulary')), (self._verb_progress, i18n.tr('Verbs')), (self._sentence_progress, i18n.tr('Sentences')),
                             (self._listening_progress, i18n.tr('Listening')),
                             (self._comprehension_progress[comprehension.Direction.READ], i18n.tr('Reading comprehension')),
                             (self._comprehension_progress[comprehension.Direction.LISTEN], i18n.tr('Listening comprehension'))):
            self._progress_tabs.addTab(panel, label)
        self._progress_tabs.currentChanged.connect(self._refresh_progress)
        self._activity = library_practice_activity.ActivityPanel(self._db)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._activity)
        self._tabs.addTab(self._progress_tabs, i18n.tr('Progress'))
        self._tabs.addTab(scroll, i18n.tr('Activity'))

    def _tab_changed(self, idx: int) -> None:
        """Activate only the selected destination; e.g. opening a reverse quiz cannot expose its sibling's next question."""

        self._pronunciation.setVisible(idx in self._spoken)
        ident, text = self._spoken.get(idx, (0, ''))
        self._pronunciation.prompt((idx, ident), text)
        guided = self._sequence.current
        if guided is not None and idx != Page.for_track(guided):
            self._sequence.pause()
        if idx in self._panels:
            track = list(practice_tracks.Track)[idx]
            self._db.set_setting("practice_last_track", track.value)
            self._panels[idx].activate()
        elif idx == Page.PROGRESS:
            track = self._last_track()
            mode = {"practice": 0, "verb": 1, "sentence": 2, 'comprehension': 4}[track.kind]
            if track == practice_tracks.Track.LISTENING:
                mode = 3
            elif track == practice_tracks.Track.LISTEN_COMPREHENSION:
                mode = 5
            if self._progress_tabs.currentIndex() == mode:
                self._refresh_progress()
            else:
                self._progress_tabs.setCurrentIndex(mode)
        else:
            self._activity.refresh()

    def _offer_pronunciation(self, page: Page, ident: int, text: str) -> None:
        """Keep playback on the visible page, e.g. background grading cannot interrupt another exercise."""

        self._spoken[page] = ident, text
        if self._tabs.currentIndex() == page:
            self._pronunciation.prompt((page, ident), text)

    def _last_track(self) -> practice_tracks.Track:
        """Restore the last explicit direction; e.g. a practice reminder resumes Dutch writing rather than a generic category."""

        try:
            return practice_tracks.Track(self._db.setting("practice_last_track", practice_tracks.Track.WORD_NL_EN.value))
        except ValueError:
            return practice_tracks.Track.WORD_NL_EN

    def _record_activity(self, track: practice_tracks.Track) -> None:
        """Checkpoint the guided mode alongside the normal answer notification."""

        self._sequence.bind(track, self._panels[Page.for_track(track)].round_id)
        self.activity_changed.emit()

    def _show_sequence_track(self, track: practice_tracks.Track) -> None:
        """Select the same practice page used by manual navigation before opening the mode."""

        self._tabs.setCurrentIndex(Page.for_track(track))
        QtCore.QTimer.singleShot(0, self, self._enter_sequence_track)

    def _enter_sequence_track(self) -> None:
        """Only start the guided mode if it is still visible, e.g. a later tab selection paused it."""

        if (track := self._sequence.current) is not None and self.isVisible():
            page = Page.for_track(track)
            if self._tabs.currentIndex() == page:
                self._panels[page].enter_sequence()

    def showEvent(self, event) -> None:
        """Open the guided mode after the dialog becomes visible from the tray."""

        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self, self._enter_sequence_track)

    def show_page(self, activity: bool = False, *, sequence: bool = False) -> None:
        """Route menu and reminder entry points directly; e.g. Activity opens without starting any practice destination."""

        if sequence and not activity:
            self._sequence.start()
            return
        page = Page.ACTIVITY if activity else Page.for_track(self._last_track())
        self._tabs.setCurrentIndex(page)
        if page == Page.ACTIVITY:
            self._activity.refresh()
        else:
            self._panels[page].activate()

    def _refresh_progress(self, *_args) -> None:
        """Refresh only visible analytics; e.g. sentence writing history remains independent of word-choice state."""

        panels = (self._progress, self._verb_progress, self._sentence_progress, self._listening_progress,
                  *self._comprehension_progress.values())
        panels[self._progress_tabs.currentIndex()].refresh()

    def reject(self) -> None:
        """Pause all destinations without losing drafts or queued corrections; e.g. closing one dialog does not discard the reverse direction."""

        self._pronunciation.stop()
        for panel in self._panels.values():
            panel.shutdown()
        super().reject()

    def changeEvent(self, event) -> None:
        """Silence the current clip when minimizing via the title bar."""

        if event.type() == QtCore.QEvent.Type.WindowStateChange and self.isMinimized():
            self._pronunciation.stop()
        super().changeEvent(event)
