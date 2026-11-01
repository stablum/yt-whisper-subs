"""Connect trusted subtitle indices to launch-scoped mpv script messages.

Example: `WordCapture(service, source).prepare(srts, loaded, prefs)` enables clicking.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from yt_whisper_subs import vocabulary as words
from yt_whisper_subs import vocabulary_worker

if TYPE_CHECKING:
    from yt_whisper_subs import mpv_ipc
    from yt_whisper_subs import playback


class WordCapture:
    """Bind each player to one immutable video source; e.g. never use GUI selection state."""

    def __init__(self, service: vocabulary_worker.VocabularyService, source: words.Source, language: str = "nl") -> None:
        """Give this player's events a unique replay identity; e.g. two simultaneous videos."""

        self._service = service
        self._source = source
        self._language = language
        self._nonce = uuid.uuid4().hex
        self._tracks: list[words.Transcript] = []
        self._document: dict = {}
        self._send: mpv_ipc.MessageSender | None = None

    @property
    def source_key(self) -> str:
        """Identify local players for ownership and seeking; e.g. replay a CLI-only source."""

        return self._source.key

    def prepare(self, srts: list[Path], loaded: list[Path], prefs: playback.PlaybackPrefs) -> Path:
        """Share exact token indices and previous marks with Lua; e.g. SRT versus temporary ASS."""

        marks = self._service.marks(self._source.key)
        tracks = []
        self._tracks = []
        for idx, (original, actual) in enumerate(zip(srts, loaded)):
            language = "en" if original.stem.endswith(".en") else self._language
            transcript = words.Transcript.load(original, language)
            self._tracks.append(transcript)
            cues = transcript.payload()
            for cue, payload in zip(transcript.cues, cues):
                payload["saved"] = [i + 1 for i, token in enumerate(cue.tokens)
                                    if (language, cue.start_ms, cue.text, token.start) in marks]
            style = prefs.primary if idx == 0 else prefs.secondary
            tracks.append({"path": str(actual.resolve()), "cues": cues, "language": language,
                           "style": {"color": style.color, "position": style.position,
                                     "font_size": style.font_size, "res_y": 720 if idx == 0 else 1080}})
        self._document = {"tracks": tracks, "dual": prefs.dual_subs}
        return Path(__file__).parent / "mpv_scripts" / "vocabulary.lua"

    def connected(self, send: mpv_ipc.MessageSender) -> None:
        """Send configuration after the IPC pipe connects; e.g. no path quoting in script options."""

        self._send = send
        send("vocabulary", "vocab-config", self._document)

    def disconnected(self) -> None:
        """Drop player replies while accepted saves finish; e.g. quit immediately after clicking."""

        self._send = None

    def receive(self, args: list[str]) -> None:
        """Validate player indices before queuing persistence; e.g. ignore unrelated script messages."""

        if args == ["vocab-ready"]:
            if self._send:
                self._send("vocabulary", "vocab-config", self._document)
            return
        if len(args) != 2 or args[0] != "vocab-click":
            return
        request = None
        try:
            data = json.loads(args[1])
            request = data["request"]
            indices = [request, data["track"], data["cue"], data["token"]]
            if any(type(value) is not int or not 0 < value < 1_000_000_000 for value in indices):
                raise ValueError("invalid word selection indices")
            transcript = self._tracks[data["track"] - 1]
            english = next((track for track in self._tracks if track.language == "en"), None)
            ctx = transcript.context(data["cue"] - 1, data["token"] - 1, english)
            position = data["position"]
            if type(position) not in {float, int} or not math.isfinite(position) or position < 0:
                raise ValueError("invalid playback position")
            selection = words.Selection(self._source, ctx, position, time.time(), f"{self._nonce}:{request}")
            self._service.submit(selection, lambda result: self._reply(request, result))
        except (ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
            self._reply(request, {"saved": False, "error": str(exc)})

    def _reply(self, request: int | None, result: dict) -> None:
        """Send only display-safe fields; e.g. never send an API response body into ASS tags."""

        sender = self._send
        if sender:
            fields = {key: result.get(key) for key in ("saved", "state", "meaning", "note", "error", "clicks", "videos")}
            sender("vocabulary", "vocab-result", {"request": request, **fields})
