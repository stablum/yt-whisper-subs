"""Render verified Dutch speech locally and reuse content-addressed WAVs.

Example: preparing a listening round writes both ordinary and slower speech before it becomes ready.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import urllib.request
import uuid
import wave
import contextlib
from pathlib import Path

from yt_whisper_subs import i18n
from yt_whisper_subs import task_cancel
from yt_whisper_subs import cfg
from yt_whisper_subs import proc
from yt_whisper_subs import listening_voices


VOICE = "nl_NL-ronnie-medium"
REVISION = "e9ab671f2a0e50a701b34361136ba3e6a3a9eb0c"
PROFILE = f"piper-{cfg.PIPER_VERSION}:{VOICE}:{REVISION}:1.0,1.3"
_BASE = f"https://huggingface.co/rhasspy/piper-voices/resolve/{REVISION}/nl/nl_NL/ronnie/medium"
# LFS SHA-256 for the network weights, and published Git blob IDs for the small files.
_FILES = {f"{VOICE}.onnx": ("sha256", "c6b69203247c9ff9aaadf59ec51147fffb16bb2c2892eb92d5b75d2f9d096622"),
          f"{VOICE}.onnx.json": ("git", "0b7184d816827c955ec15e47f366d65effa7b1a3"),
          "MODEL_CARD": ("git", "fad44433d0cb18ae198c43ae4ba29fb960272ad4")}
_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()


def _checkpoint() -> None:
    """Cooperate at download and synthesis boundaries; e.g. cancelled preparation leaves completed files reusable."""

    if control := task_cancel.current():
        control.checkpoint()


@contextlib.contextmanager
def synthesis():
    """Share bounded CPU synthesis across engines; e.g. a preview waits cooperatively for a preparing round."""

    while not _LOCK.acquire(timeout=0.25):
        _checkpoint()
    try:
        _checkpoint()
        yield
    finally:
        _LOCK.release()


def digest(path: Path, algorithm: str = "sha256") -> str:
    """Validate a file against its published identity; e.g. Git blobs include the length header in their SHA-1."""

    value = hashlib.sha1() if algorithm == "git" else hashlib.sha256()
    if algorithm == "git":
        value.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def duration(path: Path) -> float:
    """Reject empty or truncated speech before publishing it; e.g. a WAV header alone is not playable stock."""

    with wave.open(str(path), "rb") as stream:
        frames = stream.getnframes()
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2 or not frames or stream.getframerate() <= 0:
            raise ValueError("Invalid listening audio format")
        expected = frames * stream.getsampwidth()
        if len(stream.readframes(frames)) != expected:
            raise ValueError("Truncated listening audio")
        return frames / stream.getframerate()


class PiperSpeech:
    """Keep one CPU voice per preparation service without competing for Whisper's GPU; e.g. no sentence is sent to a speech API."""

    profile = PROFILE

    def __init__(self, root: Path, *, config=None) -> None:
        """Store assets beside the catalog; e.g. a custom output root has its own durable speech cache."""

        self._root = root
        self._model = None
        self.config = config or listening_voices.Config()
        self.profile = PROFILE if (self.config.pace, self.config.volume) == (1, 1) else PROFILE + ":" + self.config.identity()

    def _load(self, report) -> None:
        """Download only missing or corrupt pinned assets, then load offline; e.g. later app starts need no network for speech."""

        if self._model is not None:
            return

        directory = self._root / "voices" / VOICE
        directory.mkdir(parents=True, exist_ok=True)
        for name, (algorithm, expected) in _FILES.items():
            target = directory / name
            if target.is_file() and digest(target, algorithm) == expected:
                continue
            _checkpoint()
            report(i18n.tr('Downloading verified Dutch speech voice (first use only)…'))
            temp = target.with_name(name + f".{uuid.uuid4().hex}.part")
            try:
                with urllib.request.urlopen(f"{_BASE}/{name}", timeout=60) as response, temp.open("wb") as output:
                    while block := response.read(1024 * 1024):
                        _checkpoint()
                        output.write(block)
                if digest(temp, algorithm) != expected:
                    raise ValueError(f"Dutch voice integrity check failed: {name}")
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
        self._model = directory / f"{VOICE}.onnx"

    def render(self, text: str, paths: dict[str, Path], report) -> None:
        """Serialize CPU synthesis and atomically publish each speed; e.g. cancelling slow speech never damages the normal recording."""

        self.render_many([(text, paths)], report)

    def render_many(self, jobs: list[tuple[str, dict[str, Path]]], report) -> None:
        """Load one voice in an isolated native worker per batch; e.g. a phonemizer exit cannot close the library application."""

        manifest = self._root / f"synthesis-{uuid.uuid4().hex}.json"
        with synthesis():
            self._render_worker(jobs, report, manifest)

    def _render_worker(self, jobs, report, manifest: Path) -> None:
        """Isolate native phonemization and always remove its manifest; e.g. a cancelled worker leaves no sentence text behind."""

        try:
            self._load(report)
            document = {"model": str(self._model.resolve()), "pace": self.config.pace, "volume": self.config.volume, "jobs": [
                {"text": text, "paths": {speed: str(path.resolve()) for speed, path in paths.items()}} for text, paths in jobs]}
            manifest.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            python = Path(sys.executable)
            if python.name.lower() == "pythonw.exe":
                python = python.with_name("python.exe")
            boot = "import sys; sys.path.insert(0,sys.argv[1]); from yt_whisper_subs.listening_synth import main; main(sys.argv[2])"
            report(i18n.tr('Preparing Dutch listening audio…'))
            result = proc.run([python, "-c", boot, str(cfg.PROJECT_DIR), str(manifest.resolve())],
                              capture_stdout=True, stream_stdout=True, check=False)
            if result.returncode:
                raise RuntimeError("Dutch speech synthesis failed: " + (result.stdout or "native worker exited")[-1400:])
        finally:
            manifest.unlink(missing_ok=True)


class AudioCache:
    """Materialize approved sentence audio before sessions or stock are published; e.g. failed TTS leaves paid text reusable."""

    def __init__(self, db, speech=None) -> None:
        """Allow deterministic speech fixtures; e.g. tests verify readiness without a model download."""

        self._db = db
        self._speech = speech

    def prepare(self, questions: list[dict], report, *, preset=None) -> None:
        """Repair absent audio by content identity without regenerating text; e.g. two rounds reusing a sentence reuse its recordings."""

        presets = [preset] if preset else listening_voices.read(self._db).enabled
        for row in presets:
            _checkpoint()
            report(i18n.tr('Preparing voice: ') + row.name)
            try:
                self._prepare_voice(self.engine(row.config), questions, report)
            except RuntimeError as exc:
                raise RuntimeError(f"{row.name}: {exc}") from exc

    def engine(self, config):
        """Resolve a preset through the cache's speech strategy; e.g. offline fixtures exercise the same UI and readiness paths."""

        return self._speech or listening_voices.speech(self._db, config)

    def ready(self, questions: list[dict]) -> bool:
        """Require both speeds for every enabled preset; e.g. a new character warms existing text instead of spending generation tokens."""

        try:
            profiles = {self.engine(row.config).profile for row in listening_voices.read(self._db).enabled}
            return all((audio := self._db.listening_audio(row['id'], profile)) and self._db.listening_audio_valid(audio)
                       for row in questions for profile in profiles)
        except (ValueError, OSError):
            return False

    def _prepare_voice(self, engine, questions: list[dict], report) -> None:
        """Cache each enabled sound independently; e.g. adding a conversational voice reuses Piper and already approved sentence text."""

        texts = {row['id']: json.loads(row['body_json'])['dutch'] for row in questions}
        assets = Recordings(self._db).prepare(engine, list(texts.values()), report)
        with self._db._connect() as conn:
            conn.executemany('INSERT OR IGNORE INTO listening_audio VALUES(?,?)', [(qid, assets[text]) for qid, text in texts.items()])


class Recordings:
    """Own content-addressed speech independently from question persistence; e.g. dictation and long narration share integrity checks and synthesis ownership."""

    def __init__(self, db) -> None:
        """Bind one catalog's asset paths; e.g. changing the output root retains independent recording metadata."""

        self._db = db

    @staticmethod
    def key(profile: str, text: str) -> str:
        """Identify exact text and audible settings; e.g. renaming a narrator does not synthesize another clip."""

        return hashlib.sha256((profile + '\0' + text).encode('utf-8')).hexdigest()

    def prepare(self, engine, texts: list[str], report) -> dict[str, str]:
        """Coalesce concurrent repair and narration work; e.g. a foreground retry rechecks stock after waiting for background synthesis."""

        while not _CACHE_LOCK.acquire(timeout=.25):
            _checkpoint()
        try:
            return self._prepare(engine, texts, report)
        finally:
            _CACHE_LOCK.release()

    def _prepare(self, engine, texts, report) -> dict[str, str]:
        """Batch only missing files and publish after validation; e.g. one broken speed reuses its intact counterpart."""

        jobs, prepared = {}, []
        result = {text: self.key(engine.profile, text) for text in texts}
        for text, key in result.items():
            cached = self._db.audio_asset(key)
            if cached and cached["cache_key"] == key and self._db.listening_audio_valid(cached):
                continue
            paths = self._db.listening_paths(key)
            missing = {}
            for speed, path in paths.items():
                try:
                    duration(path)
                    # Previously published hashes also detect plausible but changed WAVs.
                    if cached and cached["cache_key"] == key and digest(path) != cached[f"{speed}_sha256"]:
                        missing[speed] = path
                except (OSError, EOFError, ValueError, wave.Error):
                    missing[speed] = path
            if missing:
                jobs[key] = (text, missing)
            prepared.append((key, paths))
        if jobs:
            engine.render_many(list(jobs.values()), report)
        for key, paths in prepared:
            settings = engine.config.model_dump(mode="json") if hasattr(engine, "config") else {}
            self._db.audio_store(key, engine.profile, paths, settings=settings)
        return result
