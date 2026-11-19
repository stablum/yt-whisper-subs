"""Cache normal-speed Dutch pronunciation using the existing local Piper engine."""

import wave
from pathlib import Path

from yt_whisper_subs import comprehension_audio
from yt_whisper_subs import listening_tts


type Clip = tuple[Path, float]


class Audio:
    """Reuse listening audio where possible, e.g. the same Dutch sentence needs no extra render."""

    def __init__(self, db, *, speech=None) -> None:
        """Keep optional clips beside scored practice assets in their own normal-speed cache."""

        root = db.path.parent / 'practice-audio'
        self._db, self._root = db, root / 'pronunciation'
        self._speech = speech or listening_tts.PiperSpeech(root)

    def prepare(self, paragraphs: list[str], report) -> list[Clip]:
        """Return verified clips in spoken order, e.g. a long article can play paragraph by paragraph."""

        texts = [part['text'] for part in comprehension_audio.segments(paragraphs)]
        keys = {text: listening_tts.Recordings.key(self._speech.profile, text) for text in texts}
        clips = {text: self._cached(key) for text, key in keys.items()}
        if missing := [text for text in keys if clips[text] is None]:
            with listening_tts.cache_writes():
                jobs = []
                for text in missing:
                    clips[text] = self._cached(keys[text])
                    if clips[text] is None:
                        jobs.append((text, {'normal': self._root / f'{keys[text]}.wav'}))
                if jobs:
                    self._speech.render_many(jobs, report)
                    for text, paths in jobs:
                        path = paths['normal']
                        clips[text] = path, listening_tts.duration(path)
        return [clips[text] for text in texts]

    def _cached(self, key: str) -> Clip | None:
        """Check cache integrity before replay, e.g. replace a truncated optional clip on demand."""

        path = self._root / f'{key}.wav'
        try:
            return path, listening_tts.duration(path)
        except (OSError, ValueError, EOFError, wave.Error):
            pass
        normal = self._db.listening_paths(key)['normal']
        try:
            asset = self._db.audio_asset(key)
            if asset and listening_tts.digest(normal) == asset['normal_sha256']:
                return normal, asset['normal_seconds']
        except OSError:
            pass
        return None
