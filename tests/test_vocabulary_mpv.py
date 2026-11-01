"""Opt-in end-to-end tests against the installed mpv, with a fake translation provider.

Example: set YTW_TEST_MPV=1, then run `python -m unittest tests.test_vocabulary_mpv`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from yt_whisper_subs import library_db
from yt_whisper_subs import mpv_ipc
from yt_whisper_subs import playback
from yt_whisper_subs import vocabulary as words
from yt_whisper_subs import vocabulary_playback
from yt_whisper_subs import vocabulary_translate
from yt_whisper_subs import vocabulary_worker


class FakeTranslator(vocabulary_translate.WordTranslator):
    """Return deterministic meanings without network; e.g. validate the player and database together."""

    def translate(self, document: dict) -> words.Meaning:
        """Preserve the selected token in the result; e.g. verify no cue-index shift."""

        return words.Meaning(f"Meaning of {document['word']}", document["word"], "test gloss", "test", "", "", False)


class ProbeClient:
    """Capture test outcomes while using the real application message client."""

    def __init__(self, capture: vocabulary_playback.WordCapture) -> None:
        self.capture = capture
        self.results = []
        self.clicks = []

    def connected(self, send) -> None:
        """Forward the actual handshake; e.g. configure track data after pipe connection."""

        self.capture.connected(send)

    def disconnected(self) -> None:
        """Release the player callback; e.g. accepted saves still drain after Quit."""

        self.capture.disconnected()

    def receive(self, args: list[str]) -> None:
        """Observe real mouse-generated messages; e.g. no direct mock of capture submission."""

        if args and args[0] == "vocab-test-result":
            self.results.append(json.loads(args[1]))
        else:
            if args and args[0] == "vocab-click":
                self.clicks.append(json.loads(args[1]))
            self.capture.receive(args)


@unittest.skipUnless(os.environ.get("YTW_TEST_MPV") == "1" and shutil.which("mpv") and shutil.which("ffmpeg"),
                     "set YTW_TEST_MPV=1 with mpv and ffmpeg installed")
class MpvVocabularyTests(unittest.TestCase):
    """Validate actual Lua rendering, native input, IPC, and SQLite at multiple cue times."""

    def test_click_resize_seek_and_restore_native_subtitles(self) -> None:
        """Persist repeated Unicode words through the real player; e.g. click then seek."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "sample.mkv"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "color=c=0x1c2430:s=960x540:r=5:d=12", "-c:v", "libx264",
                            "-preset", "ultrafast", str(video)], check=True, creationflags=flags, timeout=15)
            nl, en, ass = root / "sample.srt", root / "sample.en.srt", root / "sample.ass"
            nl.write_text("1\n00:00:01,000 --> 00:00:06,000\nIk ben geïnteresseerd in de Nederlandse taal.\n\n"
                          "2\n00:00:06,000 --> 00:00:11,000\nIk liep naar huis.\n", encoding="utf-8")
            en.write_text("1\n00:00:01,000 --> 00:00:06,000\nI am interested in the Dutch language.\n\n"
                          "2\n00:00:06,000 --> 00:00:11,000\nI walked home.\n", encoding="utf-8")
            prefs = playback.PlaybackPrefs.defaults()
            loaded = playback.dual_subtitle_playback_paths([en, nl], prefs, root)
            db = library_db.LibraryDb(root / "catalog.sqlite3")
            db.initialize()
            service = vocabulary_worker.VocabularyService(db, FakeTranslator())
            capture = vocabulary_playback.WordCapture(service, words.Source.local(video))
            script = capture.prepare([en, nl], loaded, prefs)
            client = ProbeClient(capture)
            log = root / "mpv.log"
            driver = Path(__file__).parent / "fixtures" / "vocabulary_driver.lua"
            try:
                with mpv_ipc.MpvMonitor(client=client) as monitor:
                    cmd = ["mpv", "--no-config", "--load-scripts=no", "--osc=no", "--audio=no",
                           "--pause=yes", "--start=2", "--keep-open=yes", "--window-minimized=yes",
                           "--geometry=960x540", "--sub-auto=no", "--sid=1", "--secondary-sid=2",
                           f"--sub-file={en}", f"--sub-file={loaded[1]}", f"--script={script}",
                           f"--script={driver}", f"--log-file={log}", monitor.mpv_option, str(video)]
                    result = subprocess.run(cmd, capture_output=True, timeout=20, creationflags=flags)
                debug = "\n".join(line for line in log.read_text(encoding="utf-8").splitlines()
                                  if "[vocabulary]" in line or "vocab-click" in line or "script-binding" in line)
                self.assertEqual(result.returncode, 0, f"{client.results}\nclicks={client.clicks}\n{debug[-7000:]}")
                self.assertTrue(client.results[-1]["passed"])
                self.assertEqual(len(client.clicks), 5)
                service.close()
                rows = {row["surface"]: row for row in db.word_summary()}
                self.assertEqual(rows["geïnteresseerd"]["clicks"], 2)
                self.assertEqual(rows["geïnteresseerd"]["videos"], 1)
                history = db.word_history("nl", "liep")
                self.assertEqual(len(history), 2)
                self.assertEqual(history[0]["cue_start_ms"], 6000)
                self.assertAlmostEqual(history[0]["position_seconds"], 7, delta=0.25)
            finally:
                service.close()
                if service._threads:
                    service._threads[1].join(3)
