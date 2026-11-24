"""Compose top-level direction-specific practice and separate progress pages.

Example: Word quiz and Sentences each expose Dutch-to-English and English-to-Dutch directly.
"""

from __future__ import annotations

import enum
from datetime import datetime, time, timedelta
from functools import partial

from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import library_checks
from yt_whisper_subs import library_practice_activity
from yt_whisper_subs import library_practice_nav
from yt_whisper_subs import library_practice_progress
from yt_whisper_subs import library_practice_sequence
from yt_whisper_subs import library_practice_voices
from yt_whisper_subs import library_pronunciation
from yt_whisper_subs import library_sentence_progress
from yt_whisper_subs import library_sentences
from yt_whisper_subs import library_verb_progress
from yt_whisper_subs import library_verb_forms
from yt_whisper_subs import library_verbs
from yt_whisper_subs import library_word_quiz
from yt_whisper_subs import library_word_order
from yt_whisper_subs import library_order_progress
from yt_whisper_subs import practice
from yt_whisper_subs import practice_activity
from yt_whisper_subs import practice_tracks
from yt_whisper_subs import sentence_practice
from yt_whisper_subs import word_order
from yt_whisper_subs import comprehension
from yt_whisper_subs import library_comprehension
from yt_whisper_subs import library_comprehension_progress


_STYLE = """
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
    SOUND = 2
    VERBS = 3
    VERB_FORMS = 4
    SENTENCE_NL_EN = 5
    SENTENCE_EN_NL = 6
    WORD_ORDER = 7
    PHRASE_ORDER = 8
    LISTENING = 9
    READ_COMPREHENSION = 10
    LISTEN_COMPREHENSION = 11
    PROGRESS = 12
    ACTIVITY = 13

    @classmethod
    def for_track(cls, track: practice_tracks.Track) -> Page:
        """Map the shared destination order to its page; e.g. native Ctrl+Tab includes both translation directions."""

        return cls(list(practice_tracks.Track).index(track))


class PracticeView(QtWidgets.QWidget):
    """Compose the library's embedded practice page; e.g. leaving it preserves the current exercise."""

    activity_changed = QtCore.Signal()
    voice_changed = QtCore.Signal()
    audio_preparation_requested = QtCore.Signal()

    def __init__(self, service, parent=None, *, engine=None) -> None:
        """Construct direct practice destinations with one native page state; e.g. each direction keeps its draft when another is selected."""

        super().__init__(parent)
        self._db = service.db
        self._quizzes = {direction: library_word_quiz.WordQuiz(self._db, direction, engine=engine) for direction in practice.Direction}
        self._sentences = {direction: library_sentences.SentencePanel(self._db, direction=direction)
                           for direction in (practice.Direction.NL_EN, practice.Direction.EN_NL)}
        self._verbs = library_verbs.VerbPanel(self._db)
        self._orders = {variant: library_word_order.Panel(self._db, variant=variant) for variant in word_order.Variant}
        self._listening = library_sentences.SentencePanel(self._db, direction=sentence_practice.Direction.LISTEN)
        self._comprehension = {direction: library_comprehension.Panel(self._db, direction) for direction in comprehension.Direction}
        self._panels = {Page.WORD_NL_EN: self._quizzes[practice.Direction.NL_EN],
                        Page.SOUND: self._quizzes[practice.Direction.SOUND],
                        Page.WORD_EN_NL: self._quizzes[practice.Direction.EN_NL], Page.VERBS: self._verbs,
                        Page.VERB_FORMS: library_verb_forms.FormPanel(self._db),
                        Page.SENTENCE_EN_NL: self._sentences[practice.Direction.EN_NL],
                        Page.SENTENCE_NL_EN: self._sentences[practice.Direction.NL_EN],
                        Page.WORD_ORDER: self._orders[word_order.Variant.WORDS],
                        Page.PHRASE_ORDER: self._orders[word_order.Variant.PHRASES],
                        Page.LISTENING: self._listening,
                        Page.READ_COMPREHENSION: self._comprehension[comprehension.Direction.READ],
                        Page.LISTEN_COMPREHENSION: self._comprehension[comprehension.Direction.LISTEN]}
        self._tabs = QtWidgets.QTabWidget()
        self._pronunciation = library_pronunciation.Controls(self._db)
        self._pronunciation.playback_started.connect(self._heard)
        self._pronunciation.round_opened.connect(self.audio_preparation_requested)
        self._voice_bar = library_practice_voices.VoiceBar(self._db)
        self._listeners = (self._listening._view.card,
                           self._comprehension[comprehension.Direction.LISTEN]._view.source.audio)
        self._voice_bar.changed.connect(self._change_voice)
        self._voice_bar.configuring.connect(self._stop_audio)
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
            panel.round_reviewed.connect(self._schedule_nav_refresh)
            if page in self._spoken:
                panel.before_question = partial(self._pronunciation.before_question, track)
                panel.speech_changed.connect(partial(self._offer_pronunciation, page))
            self._tabs.addTab(panel, track.label)
        self._arrange_tracking()
        short_labels = {Page.for_track(track): track.button_label for track in practice_tracks.Track}
        self._nav = library_practice_nav.PracticeNav(self._tabs, (
            (i18n.tr('Words'), (Page.WORD_NL_EN, Page.WORD_EN_NL, Page.SOUND)),
            (i18n.tr('Verbs'), (Page.VERBS, Page.VERB_FORMS)),
            (i18n.tr('Sentences'), (Page.SENTENCE_NL_EN, Page.SENTENCE_EN_NL,
                                    Page.WORD_ORDER, Page.PHRASE_ORDER, Page.LISTENING)),
            (i18n.tr('Passages'), (Page.READ_COMPREHENSION, Page.LISTEN_COMPREHENSION)),
            (i18n.tr('Tracking'), (Page.PROGRESS, Page.ACTIVITY)),
        ), short_labels)
        self._nav_timer = QtCore.QTimer(self)
        self._nav_timer.setSingleShot(True)
        self._nav_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._nav_timer.timeout.connect(self._refresh_nav)
        self._sequence.position_changed.connect(self._schedule_nav_refresh)
        # Corrections can arrive while Progress is open and the exercise page is inactive.
        for checks in library_checks.Checks.for_catalog(self._db):
            checks.changed.connect(self._schedule_nav_refresh)
        self._tabs.currentChanged.connect(self._tab_changed)
        audio_controls = QtWidgets.QHBoxLayout()
        audio_controls.setSpacing(16)
        audio_controls.addWidget(self._voice_bar)
        audio_controls.addWidget(self._pronunciation)
        audio_controls.addStretch()
        content = QtWidgets.QVBoxLayout()
        content.setContentsMargins(22, 12, 22, 12)
        content.addWidget(self._sequence)
        content.addLayout(audio_controls)
        content.addWidget(self._tabs, 1)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._nav)
        layout.addLayout(content, 1)
        self.setStyleSheet(_STYLE)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self._shutdown)

    def _arrange_tracking(self) -> None:
        """Keep analytics separate from exercise controllers; e.g. progress reads all directions without generating questions."""

        self._overall = library_practice_progress.OverallProgress(self._db)
        self._progress = library_practice_progress.ProgressPanel(self._db)
        self._verb_progress = library_verb_progress.VerbProgress(self._db)
        self._sentence_progress = library_sentence_progress.SentenceProgress(self._db)
        self._listening_progress = library_sentence_progress.SentenceProgress(self._db, listening=True)
        self._order_progress = {variant: library_order_progress.Progress(self._db, variant) for variant in word_order.Variant}
        self._comprehension_progress = {direction: library_comprehension_progress.Progress(self._db, direction) for direction in comprehension.Direction}
        self._progress_tabs = QtWidgets.QTabWidget()
        for panel, label in ((self._overall, i18n.tr('Overall')), (self._progress, i18n.tr('Vocabulary')),
                             (self._verb_progress, i18n.tr('Verbs')), (self._sentence_progress, i18n.tr('Sentences')),
                             (self._listening_progress, i18n.tr('Listening')),
                             (self._comprehension_progress[comprehension.Direction.READ], i18n.tr('Reading comprehension')),
                             (self._comprehension_progress[comprehension.Direction.LISTEN], i18n.tr('Listening comprehension')),
                             *((panel, variant.label) for variant, panel in self._order_progress.items())):
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

        self._pronunciation.stop()
        self._pronunciation.listening_question(idx == Page.SOUND)
        self._voice_bar.setVisible(idx in self._panels)
        self._pronunciation.setVisible(idx in self._spoken)
        ident, text = self._spoken.get(idx, (0, ''))
        self._pronunciation.prompt((idx, ident), text)
        guided = self._sequence.current
        if guided is not None and idx != Page.for_track(guided):
            self._sequence.pause()
        if idx in self._panels:
            track = list(practice_tracks.Track)[idx]
            self._db.set_setting("practice_last_track", track.value)
            if idx in self._spoken:
                self._pronunciation.bind_round(track, self._panels[idx].round_id)
            self._panels[idx].activate()
        elif idx == Page.PROGRESS:
            self._open_progress()
        else:
            self._activity.refresh()

    def _open_progress(self) -> None:
        """Keep Overall selected, or follow the last exercise when viewing detailed progress."""

        current = self._progress_tabs.currentWidget()
        if current is self._overall:
            self._overall.refresh()
            return
        track = self._last_track()
        if track.kind == "comprehension":
            target = self._comprehension_progress[track.direction]
        elif track.variant:
            target = self._order_progress[track.variant]
        elif track == practice_tracks.Track.LISTENING:
            target = self._listening_progress
        else:
            target = {"practice": self._progress, "verb": self._verb_progress,
                      "sentence": self._sentence_progress}[track.kind]
        if current is target:
            target.refresh()
        else:
            self._progress_tabs.setCurrentWidget(target)

    def _offer_pronunciation(self, page: Page, ident: int, text: str) -> None:
        """Keep playback on the visible page, e.g. background grading cannot interrupt another exercise."""

        self._spoken[page] = ident, text
        if self._tabs.currentIndex() == page:
            self._pronunciation.prompt((page, ident), text)

    def _heard(self, cue: tuple) -> None:
        """Unlock only the audible question whose recording started, including after a replay."""

        page, ident = cue
        if page == Page.SOUND and self._tabs.currentIndex() == page:
            self._panels[page].heard(ident)

    def _stop_audio(self) -> None:
        """Silence every player before opening a voice preview or minimizing Practice."""

        self._pronunciation.stop()
        for listener in self._listeners:
            listener.shutdown()

    def _change_voice(self) -> None:
        """Apply the saved voice to playback and wake background preparation."""

        for listener in self._listeners:
            listener.voice_changed()
        self._pronunciation.voice_changed()
        self.voice_changed.emit()

    def _last_track(self) -> practice_tracks.Track:
        """Restore the last explicit direction; e.g. a practice reminder resumes Dutch writing rather than a generic category."""

        try:
            return practice_tracks.Track(self._db.setting("practice_last_track", practice_tracks.Track.WORD_NL_EN.value))
        except ValueError:
            return practice_tracks.Track.WORD_NL_EN

    def _record_activity(self, track: practice_tracks.Track) -> None:
        """Checkpoint the guided mode alongside the normal answer notification."""

        self._sequence.bind(track, self._panels[Page.for_track(track)].round_id)
        self._schedule_nav_refresh()
        self.activity_changed.emit()

    def _schedule_nav_refresh(self) -> None:
        """Coalesce answer and guided-sequence signals; e.g. grade and review can arrive together."""

        if self.isVisible():
            self._nav_timer.start(0)

    def _refresh_nav(self) -> None:
        """Sync markers with saved history and the resume position, then refresh again at local midnight."""

        now = datetime.now()
        statuses = {Page.for_track(track): status for track, status in practice_activity.day_states(self._db, now).items()}
        resume = self._sequence.refresh_position()
        self._nav.mark(statuses, Page.for_track(resume) if resume else None)
        if self._tabs.currentIndex() == Page.PROGRESS and self._progress_tabs.currentWidget() is self._overall:
            self._overall.refresh()
        midnight = datetime.combine(now.date() + timedelta(days=1), time.min)
        delay_ms = int((midnight.timestamp() - now.timestamp()) * 1000) + 1
        self._nav_timer.start(delay_ms)

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
        """Refresh the returned page; e.g. Activity includes answers saved while browsing the Library."""

        super().showEvent(event)
        self._schedule_nav_refresh()
        idx = self._tabs.currentIndex()
        if idx == Page.ACTIVITY:
            self._activity.refresh()
        elif idx == Page.PROGRESS:
            self._refresh_progress()
        elif idx in self._spoken:
            track = list(practice_tracks.Track)[idx]
            self._pronunciation.bind_round(track, self._panels[idx].round_id)
        QtCore.QTimer.singleShot(0, self, self._enter_sequence_track)

    def hideEvent(self, event) -> None:
        """Stop playback when leaving Practice or minimizing the main window."""

        self._nav_timer.stop()
        self._stop_audio()
        super().hideEvent(event)

    def show_page(self, activity: bool = False, *, sequence: bool = False) -> None:
        """Route initial and reminder entry points directly; e.g. Activity opens without starting a round."""

        if sequence and not activity:
            self._sequence.start()
            return
        page = Page.ACTIVITY if activity else Page.for_track(self._last_track())
        if self._tabs.currentIndex() == page:
            self._tab_changed(page)
        else:
            self._tabs.setCurrentIndex(page)

    def _refresh_progress(self, *_args) -> None:
        """Refresh only visible analytics; e.g. sentence writing history remains independent of word-choice state."""

        panel = self._progress_tabs.currentWidget()
        if panel is not None:
            panel.refresh()

    def _shutdown(self) -> None:
        """Release view-owned work at exit while retaining drafts and queued corrections."""

        self._stop_audio()
        for panel in self._panels.values():
            panel.shutdown()

    def closeEvent(self, event) -> None:
        """Release the page when its owner closes it; e.g. cancel a pending preparation."""

        self._shutdown()
        super().closeEvent(event)
