"""Save guided progress through the existing independent rounds.

Example: the reminder can resume Dutch dictation after the earlier modes were completed.
"""

import pydantic as pd
from PySide6 import QtCore
from PySide6 import QtWidgets

from yt_whisper_subs import i18n
from yt_whisper_subs import practice_tracks


_TRACKS = tuple(practice_tracks.Track)
_KEY = 'practice_sequence'


class Cursor(pd.BaseModel):
    """Store the number of modes visited and the round currently being practised."""

    completed: int = pd.Field(default=0, ge=0)
    skipped: int = pd.Field(default=0, ge=0)
    round_id: str | None = None
    order: tuple[practice_tracks.Track, ...] = _TRACKS

    @property
    def track(self) -> practice_tracks.Track | None:
        """Follow the same order as the practice buttons, from vocabulary to comprehension."""

        return self.track_at_offset(0)

    def track_at_offset(self, offset: int) -> practice_tracks.Track | None:
        """Look ahead from the cursor, e.g. show where a recovered completion will resume."""

        idx = self.completed + self.skipped + offset
        return self.order[idx] if idx < len(self.order) else None

    def round_reviewed(self, db) -> bool:
        """Recover a completed round if the window closed before the handoff to the next mode."""

        track = self.track
        if not track or not self.round_id:
            return False
        if track.kind == 'sentence':
            rows = db.sentence_history(self.round_id)
            return bool(rows) and all(r['outcome'] == 'invalid' or (r['outcome'] and r['viewed_at'] is not None) for r in rows)
        summary = getattr(db, f'{track.kind}_summary')(self.round_id)
        return (summary['planned'] > 0 and summary['planned'] == summary['correct'] + summary['wrong'] + summary['invalid']
                and not summary.get('unseen'))


class SequenceBar(QtWidgets.QWidget):
    """Show the current mode and keep sequence navigation separate from answering exercises."""

    requested = QtCore.Signal(object)
    position_changed = QtCore.Signal()

    def __init__(self, db) -> None:
        """Restore a saved cursor without opening a round until Full practice is selected."""

        super().__init__()
        self._db = db
        try:
            self._cursor = Cursor.model_validate_json(db.setting(_KEY, 'null'))
        except ValueError:
            self._cursor = None
        self._following = False
        self._status = QtWidgets.QLabel()
        self._status.setWordWrap(True)
        self._start = QtWidgets.QPushButton()
        self._start.setObjectName('primaryButton')
        self._skip = QtWidgets.QPushButton(i18n.tr('Skip mode'))
        self._pause = QtWidgets.QPushButton(i18n.tr('Pause full practice'))
        self._start.clicked.connect(self.start)
        self._skip.clicked.connect(lambda: self._step(skipped=True))
        self._pause.clicked.connect(self.pause)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._status, 1)
        for button in (self._start, self._skip, self._pause):
            button.setAutoDefault(False)
            layout.addWidget(button)
        self.refresh_position()

    @property
    def current(self) -> practice_tracks.Track | None:
        """Return a track only while following the sequence in the dialog."""

        return self._cursor.track if self._following and self._cursor else None

    def start(self) -> None:
        """Resume a saved mode, including a round completed immediately before closing."""

        if not self._cursor or not self._cursor.track:
            self._cursor = Cursor()
        self._following = True
        if self._cursor.round_reviewed(self._db):
            self._step()
        else:
            self._publish()

    def pause(self) -> None:
        """Let the learner switch tabs and return to the same sequence position later."""

        if self._following:
            self._following = False
            self.refresh_position()
            self.position_changed.emit()

    def bind(self, track, round_id: str | None) -> None:
        """Remember the real round behind a submission so reopening avoids an extra round."""

        if self.current == track and round_id and self._cursor.round_id != round_id:
            self._cursor.round_id = round_id
            self._save()

    def reviewed(self, track) -> None:
        """Advance only when feedback for the active mode has been reviewed."""

        if self.current == track:
            self._step()

    def _step(self, *, skipped=False) -> None:
        """Close one mode without changing any underlying round or its answers."""

        if self.current is None:
            return
        self._cursor.skipped += int(skipped)
        self._cursor.completed += int(not skipped)
        self._cursor.round_id = None
        self._publish()

    def _save(self) -> None:
        """Keep the sequence cursor durable in the same catalog as its rounds."""

        self._db.set_setting(_KEY, self._cursor.model_dump_json())

    def _publish(self) -> None:
        """Save a transition before requesting the next page, including when the final mode ends."""

        self._save()
        self._following = self._cursor.track is not None
        self.refresh_position()
        self.position_changed.emit()
        if self.current is not None:
            self.requested.emit(self.current)

    def refresh_position(self) -> practice_tracks.Track | None:
        """Use the same resume destination in the header and sidebar, including after a recovered handoff."""

        track = self._cursor.track if self._cursor else None
        self._start.setText(i18n.tr('Resume full practice') if track else i18n.tr('Full practice'))
        self._start.setVisible(not self._following)
        self._skip.setVisible(self._following)
        self._pause.setVisible(self._following)
        resume = track
        if track:
            recovered = not self._following and self._cursor.round_reviewed(self._db)
            resume = self._cursor.track_at_offset(int(recovered))
            number = self._cursor.completed + self._cursor.skipped + 1 + int(recovered and resume is not None)
            mode = (resume or track).label.replace('\n', ' · ')
            self._status.setText(i18n.tr('Full practice {number}/{total} · {mode}', number=number, total=len(self._cursor.order), mode=mode))
        elif self._cursor:
            self._status.setText(i18n.tr('Full practice finished · {completed} completed · {skipped} skipped',
                                         completed=self._cursor.completed, skipped=self._cursor.skipped))
        else:
            self._status.clear()
        return resume
