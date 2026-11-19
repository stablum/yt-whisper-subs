"""Cache normal-speed Dutch pronunciation with the shared practice voice."""

import wave
from functools import lru_cache
from pathlib import Path

from yt_whisper_subs import comprehension_audio
from yt_whisper_subs import listening_tts
from yt_whisper_subs import listening_voices


type Clip = tuple[Path, float]
_WARM_BATCH = 8


@lru_cache(maxsize=2048)
def _verified_seconds(path: Path, stat_key: tuple[int, int]) -> float:
    """Validate once per file revision, e.g. a changed WAV is checked on the next scan."""

    return listening_tts.duration(path)


class Audio:
    """Reuse listening audio where possible, e.g. the same Dutch sentence needs no extra render."""

    def __init__(self, db, *, speech=None) -> None:
        """Keep optional clips beside scored practice assets in their own normal-speed cache."""

        root = db.path.parent / 'practice-audio'
        self._db, self._root = db, root / 'pronunciation'
        self._speech = speech

    def prepare(self, paragraphs: list[str], report) -> list[Clip]:
        """Return verified clips in spoken order, e.g. a long article can play paragraph by paragraph."""

        texts = [part['text'] for part in comprehension_audio.segments(paragraphs)]
        return self._prepare(texts, self._engine(), report)

    def _prepare(self, texts: list[str], engine, report) -> list[Clip]:
        """Use one sound identity per job so switching voices cannot mix recordings."""

        keys = {text: listening_tts.Recordings.key(engine.profile, text) for text in texts}
        clips = {}
        missing = {}
        for text, key in keys.items():
            if clip := self._cached(key):
                clips[text] = clip
            else:
                missing[text] = key
        if missing:
            with listening_tts.cache_writes():
                jobs = []
                for text, key in missing.items():
                    if clip := self._cached(key):
                        clips[text] = clip
                    else:
                        jobs.append((text, {'normal': self._path(key)}))
                if jobs:
                    engine.render_many(jobs, report)
                    for text, paths in jobs:
                        path = paths['normal']
                        clips[text] = path, listening_tts.duration(path)
        return [clips[text] for text in texts]

    def warm(self, texts: list[str], report) -> bool:
        """Fill one small batch ahead of use, e.g. release the CPU between standby jobs."""

        if not texts:
            return False
        engine = self._engine()
        batch = []
        for part in comprehension_audio.segments(texts):
            text = part['text']
            key = listening_tts.Recordings.key(engine.profile, text)
            if text not in batch and not self._cached(key):
                batch.append(text)
                if len(batch) == _WARM_BATCH:
                    break
        if batch:
            self._prepare(batch, engine, report)
        return bool(batch)

    def _engine(self):
        """Resolve the selected preset once per job; e.g. standby respects a new voice without restarting."""

        return self._speech or listening_voices.speech(self._db, listening_voices.read(self._db).selected.config)

    def _path(self, key: str) -> Path:
        """Locate the shared optional recording for both prewarming and replay."""

        return self._root / f'{key}.wav'

    def _cached(self, key: str) -> Clip | None:
        """Check cache integrity before replay, e.g. replace a truncated optional clip on demand."""

        path = self._path(key)
        try:
            stat = path.stat()
            return path, _verified_seconds(path, (stat.st_mtime_ns, stat.st_size))
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
