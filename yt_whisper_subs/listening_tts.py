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
from pathlib import Path

from yt_whisper_subs import task_cancel
from yt_whisper_subs import cfg
from yt_whisper_subs import proc


VOICE = "nl_NL-ronnie-medium"
REVISION = "e9ab671f2a0e50a701b34361136ba3e6a3a9eb0c"
PROFILE = f"piper-{cfg.PIPER_VERSION}:{VOICE}:{REVISION}:1.0,1.3"
_BASE = f"https://huggingface.co/rhasspy/piper-voices/resolve/{REVISION}/nl/nl_NL/ronnie/medium"
# LFS SHA-256 for the network weights, and published Git blob IDs for the small files.
_FILES = {f"{VOICE}.onnx": ("sha256", "c6b69203247c9ff9aaadf59ec51147fffb16bb2c2892eb92d5b75d2f9d096622"),
          f"{VOICE}.onnx.json": ("git", "0b7184d816827c955ec15e47f366d65effa7b1a3"),
          "MODEL_CARD": ("git", "fad44433d0cb18ae198c43ae4ba29fb960272ad4")}
_LOCK = threading.Lock()


def _checkpoint() -> None:
    """Cooperate at download and synthesis boundaries; e.g. cancelled preparation leaves completed files reusable."""

    if control := task_cancel.current():
        control.checkpoint()


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

    def __init__(self, root: Path) -> None:
        """Store assets beside the catalog; e.g. a custom output root has its own durable speech cache."""

        self._root = root
        self._model = None

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
            report("Downloading verified Dutch speech voice (first use only)…")
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

        while not _LOCK.acquire(timeout=0.25):
            _checkpoint()
        manifest = self._root / f"synthesis-{uuid.uuid4().hex}.json"
        try:
            self._load(report)
            document = {"model": str(self._model.resolve()), "jobs": [
                {"text": text, "paths": {speed: str(path.resolve()) for speed, path in paths.items()}} for text, paths in jobs]}
            manifest.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            python = Path(sys.executable)
            if python.name.lower() == "pythonw.exe":
                python = python.with_name("python.exe")
            boot = "import sys; sys.path.insert(0,sys.argv[1]); from yt_whisper_subs.listening_synth import main; main(sys.argv[2])"
            report("Preparing Dutch listening audio…")
            result = proc.run([python, "-c", boot, str(cfg.PROJECT_DIR), str(manifest.resolve())],
                              capture_stdout=True, stream_stdout=True, check=False)
            if result.returncode:
                raise RuntimeError("Dutch speech synthesis failed: " + (result.stdout or "native worker exited")[-1400:])
        finally:
            manifest.unlink(missing_ok=True)
            _LOCK.release()


class AudioCache:
    """Materialize approved sentence audio before sessions or stock are published; e.g. failed TTS leaves paid text reusable."""

    def __init__(self, db, speech=None) -> None:
        """Allow deterministic speech fixtures; e.g. tests verify readiness without a model download."""

        self._db = db
        self._speech = speech or PiperSpeech(db.path.parent / "practice-audio")

    def prepare(self, questions: list[dict], report) -> None:
        """Repair absent audio by content identity without regenerating text; e.g. two rounds reusing a sentence reuse its recordings."""

        jobs, prepared = {}, []
        for row in questions:
            text = json.loads(row["body_json"])["dutch"]
            key = hashlib.sha256((self._speech.profile + "\0" + text).encode("utf-8")).hexdigest()
            cached = self._db.listening_audio(row["id"])
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
            prepared.append((row["id"], key, paths))
        if jobs:
            self._speech.render_many(list(jobs.values()), report)
        for question_id, key, paths in prepared:
            self._db.listening_store(question_id, key, self._speech.profile, paths)
