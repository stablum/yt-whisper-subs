"""Overlapped Windows named-pipe I/O for simultaneous mpv reads and commands.

Example: `Pipe(address, stop).write(command)` never waits behind a pending read.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes


class _Overlapped(ctypes.Structure):
    """Match the Win32 OVERLAPPED layout; e.g. one event for each concurrent operation."""

    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


_api = ctypes.WinDLL("kernel32", use_last_error=True)
_ov_ptr = ctypes.POINTER(_Overlapped)
_dword_ptr = ctypes.POINTER(wintypes.DWORD)
_signatures = {
    "CreateFileW": (wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]),
    "CreateEventW": (wintypes.HANDLE, [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]),
    "ReadFile": (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, _dword_ptr, _ov_ptr]),
    "WriteFile": (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, _dword_ptr, _ov_ptr]),
    "GetOverlappedResult": (wintypes.BOOL, [wintypes.HANDLE, _ov_ptr, _dword_ptr, wintypes.BOOL]),
    "CancelIoEx": (wintypes.BOOL, [wintypes.HANDLE, _ov_ptr]),
    "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
    "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
}
for _name, (_result, _args) in _signatures.items():
    getattr(_api, _name).restype = _result
    getattr(_api, _name).argtypes = _args


class Pipe:
    """Buffer complete JSON lines while allowing independent writes; e.g. a paused player."""

    def __init__(self, address: str, stop: threading.Event) -> None:
        """Open one cancellable duplex handle; e.g. mpv's unique per-launch pipe."""

        self._stop = stop
        self._buffer = bytearray()
        self._state = threading.Condition()
        self._active = 0
        self._closing = False
        # GENERIC_READ | GENERIC_WRITE, OPEN_EXISTING, FILE_FLAG_OVERLAPPED.
        self._handle = _api.CreateFileW(address, 0xC0000000, 0, None, 3, 0x40000000, None)
        if self._handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self) -> Pipe:
        """Own the handle for one listener lifetime; e.g. `with pipe: ...`."""

        return self

    def __exit__(self, *_args: object) -> None:
        """Cancel and drain outstanding I/O before freeing handles; e.g. mpv Quit."""

        with self._state:
            self._closing = True
            _api.CancelIoEx(self._handle, None)
            self._state.wait_for(lambda: self._active == 0)
            _api.CloseHandle(self._handle)

    def _transfer(self, fn, buffer, size: int) -> int:
        """Use a private completion event per operation; e.g. read and reply concurrently."""

        with self._state:
            if self._closing:
                raise OSError("mpv pipe is closed")
            self._active += 1
        event = _api.CreateEventW(None, True, False, None)
        op = _Overlapped(hEvent=event)
        count = wintypes.DWORD()
        try:
            if not event:
                raise ctypes.WinError(ctypes.get_last_error())
            success = fn(self._handle, buffer, size, ctypes.byref(count), ctypes.byref(op))
            error = ctypes.get_last_error() if not success else 0
            if not success and error != 997:  # ERROR_IO_PENDING is normal asynchronous progress.
                if error in {109, 232, 233} and fn == _api.ReadFile:
                    return 0
                raise ctypes.WinError(error)
            if not success:
                while _api.WaitForSingleObject(event, 50) == 258:  # WAIT_TIMEOUT.
                    if self._stop.is_set():
                        _api.CancelIoEx(self._handle, ctypes.byref(op))
                        break
                # Wait for completion even after cancellation; the kernel still owns op/buffer.
                if not _api.GetOverlappedResult(self._handle, ctypes.byref(op), ctypes.byref(count), True):
                    error = ctypes.get_last_error()
                    if fn == _api.ReadFile and error in {109, 232, 233, 995}:
                        return 0
                    raise ctypes.WinError(error)
            return count.value
        finally:
            if event:
                _api.CloseHandle(event)
            with self._state:
                self._active -= 1
                self._state.notify_all()

    def readline(self) -> bytes:
        """Read buffered chunks rather than issuing one syscall per byte; e.g. long cue payloads."""

        while b"\n" not in self._buffer:
            buffer = ctypes.create_string_buffer(8192)
            count = self._transfer(_api.ReadFile, buffer, len(buffer))
            if count == 0:
                result = bytes(self._buffer)
                self._buffer.clear()
                return result
            self._buffer.extend(buffer.raw[:count])
            if len(self._buffer) > 16 * 1024 * 1024:
                raise OSError("mpv IPC message exceeds 16 MiB")
        end = self._buffer.index(b"\n") + 1
        result = bytes(self._buffer[:end])
        del self._buffer[:end]
        return result

    def write(self, data: bytes) -> int:
        """Finish a complete command independently of the reader; e.g. acknowledge SQLite commit."""

        written = 0
        while written < len(data):
            buffer = ctypes.create_string_buffer(data[written:])
            count = self._transfer(_api.WriteFile, buffer, len(data) - written)
            if count == 0:
                raise BrokenPipeError("mpv pipe stopped accepting commands")
            written += count
        return written

    def flush(self) -> None:
        """Expose the binary stream contract without an extra kernel flush; e.g. unbuffered writes."""
