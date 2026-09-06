"""Observe mpv playback through an ephemeral launch-only JSON IPC endpoint.

Example: `MpvMonitor(observer)` adds no persistent mpv configuration.
"""

from __future__ import annotations

import json
import math
import os
import socket
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import BinaryIO, NamedTuple

from yt_whisper_subs import cfg
from yt_whisper_subs import playback_progress as progress


class IpcEndpoint(NamedTuple):
    """Describe mpv's launch value and the matching client address.

    Example: Windows uses a unique local named-pipe name for both sides.
    """

    option_value: str
    client_address: str
    socket_path: Path | None


def _new_endpoint() -> IpcEndpoint:
    """Create a collision-resistant platform IPC address without opening it.

    Example: `_new_endpoint().option_value` is passed only to this mpv run.
    """

    name = f"yt-whisper-subs-{uuid.uuid4().hex}"
    if os.name == "nt":
        return IpcEndpoint(name, rf"\\.\pipe\{name}", None)
    path = Path(tempfile.gettempdir()) / f"{name}.sock"
    return IpcEndpoint(str(path), str(path), path)


class MpvEventTracker:
    """Turn noisy property changes and terminal events into paced updates.

    Example: `tracker.ingest({"event": "end-file", "reason": "eof"})` completes playback.
    """

    def __init__(
        self,
        observer: progress.Observer,
        emit_interval: float = cfg.DEFAULT_PLAYBACK_PROGRESS_SECONDS,
    ) -> None:
        self._observer = observer
        self._emit_interval = emit_interval
        self._position: float | None = None
        self._duration: float | None = None
        self._last_emit = float("-inf")
        self._finished = False

    @property
    def finished(self) -> bool:
        """Reveal when the tracked file has emitted its terminal event.

        Example: the IPC reader stops before a replacement file can be misattributed.
        """

        return self._finished

    def ingest(self, payload: dict[str, object], now: float | None = None) -> bool:
        """Consume one JSON IPC object and publish when progress is due.

        Example: a `time-pos` change publishes at most once per interval.
        """

        event = payload.get("event")
        timestamp = time.monotonic() if now is None else now
        if event == "property-change":
            name = payload.get("name")
            value = payload.get("data")
            if name == "time-pos" and _is_number(value):
                position = max(0.0, float(value))
                self._position = max(self._position or 0.0, position)
            elif name == "duration" and _is_number(value):
                self._duration = max(0.0, float(value))
            else:
                return False
            return self._publish(timestamp, force=False, completed=False)

        if event != "end-file":
            return False
        completed = payload.get("reason") == "eof"
        if completed and self._duration is not None:
            self._position = max(self._position or 0.0, self._duration)
        self._finished = True
        return self._publish(timestamp, force=True, completed=completed)

    def finish(self) -> bool:
        """Flush the last known position when mpv exits without `end-file`.

        Example: monitor shutdown invokes `finish()` after the pipe closes.
        """

        if self._finished:
            return False
        self._finished = True
        return self._publish(time.monotonic(), force=True, completed=False)

    def _publish(self, now: float, *, force: bool, completed: bool) -> bool:
        """Emit one normalized update when time or terminal state requires it.

        Example: EOF forces an update even within the normal five-second gap.
        """

        if self._position is None and not completed:
            return False
        if not force and now - self._last_emit < self._emit_interval:
            return False
        self._last_emit = now
        self._observer.publish(
            self._position or 0.0,
            self._duration,
            completed=completed,
        )
        return True


class MpvMonitor:
    """Own one short-lived IPC listener thread for a single mpv process.

    Example: `with MpvMonitor(observer) as monitor: run(monitor.mpv_option)`.
    """

    def __init__(self, observer: progress.Observer) -> None:
        self._observer = observer
        self._endpoint = _new_endpoint()
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._write_lock = threading.Lock()
        self._stream: BinaryIO | None = None
        self._pending: deque[bytes] = deque()
        self._thread = threading.Thread(
            target=self._listen,
            daemon=True,
            name=f"mpv-progress-{observer.video_id}",
        )

    @property
    def mpv_option(self) -> str:
        """Return the command-line-only IPC option for this playback run.

        Example: `cmd.append(monitor.mpv_option)` leaves `mpv.conf` untouched.
        """

        return f"--input-ipc-server={self._endpoint.option_value}"

    def __enter__(self) -> MpvMonitor:
        """Start connecting just before mpv is launched.

        Example: a playback context enters before calling `proc.run`.
        """

        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Stop and join after mpv has closed its endpoint.

        Example: normal player exit cleans up the monitor automatically.
        """

        del exc_type, exc, traceback
        # Refuse new commands once the owning mpv process has returned, while
        # still allowing the reader a moment to persist its final EOF event.
        self._closed.set()
        # Give mpv's closed pipe time to deliver its final EOF event before cancellation.
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=1)
        if path := self._endpoint.socket_path:
            path.unlink(missing_ok=True)

    def seek(self, seconds: float) -> bool:
        """Seek this mpv session absolutely, queueing while its pipe connects.

        Example: `monitor.seek(750)` jumps the active player to 12:30.
        """

        if not _is_number(seconds) or not math.isfinite(float(seconds)):
            return False
        command = {"command": ["seek", max(0.0, float(seconds)), "absolute+exact"]}
        return self._send(command)

    def _send(self, command: dict[str, object]) -> bool:
        """Write one JSON command safely or retain it until connection startup.

        Example: `_send({"command": [...]})` shares the listener's duplex pipe.
        """

        encoded = json.dumps(command, separators=(",", ":")).encode() + b"\n"
        with self._write_lock:
            if self._closed.is_set():
                return False
            if self._stream is None:
                self._pending.append(encoded)
                return True
            try:
                self._stream.write(encoded)
                self._stream.flush()
            except (OSError, ValueError):
                return False
        return True

    def _listen(self) -> None:
        """Connect, request time properties, and consume newline JSON events.

        Example: this method runs only in the monitor's daemon thread.
        """

        tracker = MpvEventTracker(self._observer)
        stream = self._connect()
        if stream is None:
            self._closed.set()
            return
        try:
            with stream:
                self._initialize_stream(stream)
                while not self._stop.is_set() and (line := stream.readline()):
                    try:
                        payload = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, dict):
                        tracker.ingest(payload)
                        if tracker.finished:
                            break
        except OSError as exc:
            if not self._stop.is_set():
                print(f"Warning: mpv progress IPC stopped: {exc}")
        finally:
            with self._write_lock:
                self._stream = None
            self._closed.set()
            tracker.finish()

    def _initialize_stream(self, stream: BinaryIO) -> None:
        """Publish observed properties, then flush commands queued at launch.

        Example: `_initialize_stream(pipe)` makes early chapter clicks reliable.
        """

        commands = [
            {"command": ["observe_property", request_id, name]}
            for request_id, name in enumerate(("time-pos", "duration"), start=1)
        ]
        encoded = [
            json.dumps(command, separators=(",", ":")).encode() + b"\n"
            for command in commands
        ]
        with self._write_lock:
            self._stream = stream
            for message in (*encoded, *self._pending):
                stream.write(message)
            self._pending.clear()
            stream.flush()

    def _connect(self) -> BinaryIO | None:
        """Retry briefly while mpv creates its local pipe or socket.

        Example: the monitor starts before the child process exists.
        """

        deadline = time.monotonic() + cfg.DEFAULT_PLAYBACK_IPC_TIMEOUT_SECONDS
        last_error: OSError | None = None
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                if os.name == "nt":
                    return open(self._endpoint.client_address, "r+b", buffering=0)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    client.connect(self._endpoint.client_address)
                except OSError:
                    client.close()
                    raise
                return client.makefile("rwb", buffering=0)
            except OSError as exc:
                last_error = exc
                self._stop.wait(0.05)
        if not self._stop.is_set() and last_error:
            print(f"Warning: could not connect to mpv progress IPC: {last_error}")
        return None


def _is_number(value: object) -> bool:
    """Accept JSON numeric times while excluding booleans.

    Example: `_is_number(12.5)` is true and `_is_number(True)` is false.
    """

    return isinstance(value, (int, float)) and not isinstance(value, bool)
