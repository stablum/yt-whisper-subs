"""Render tiny offline recordings for learning and native UI checks.

Example: real WAV headers exercise cache integrity without model downloads or audible playback.
"""

import wave

from PySide6 import QtCore


class Speech:
    """Count only missing speech jobs; e.g. a cached round cannot silently synthesize again."""

    profile = "test-dutch-voice-1"

    def __init__(self) -> None:
        """Allow a controlled speech failure; e.g. approved question text remains reusable after an outage."""

        self.calls = []
        self.available = True

    def render_many(self, jobs, report) -> None:
        """Publish valid mono PCM fixtures; e.g. slow speech has a longer measurable duration."""

        self.calls.extend(jobs)
        if not self.available:
            raise OSError("Speech unavailable")
        for _, paths in jobs:
            for speed, path in paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(path), "wb") as output:
                    output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                    output.writeframes(b"\0\0" * (4410 if speed == "slow" else 2205))


class Player(QtCore.QObject):
    """Observe replay without opening an audio device; e.g. native navigation tests remain silent."""

    finished = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        """Keep native ownership semantics; e.g. deleting the card deletes its fake player too."""

        super().__init__(parent)
        self.calls = []
        self.stops = 0

    def play(self, path, seconds) -> None:
        """Record successful playback starts; e.g. slow listening can be asserted independently from answers."""

        self.calls.append((path, seconds))

    def stop(self) -> None:
        """Observe navigation stopping speech; e.g. hiding the card cannot leave audio running."""

        self.stops += 1
