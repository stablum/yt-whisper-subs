"""Cover native speech loading from uv's individual dictionary-file symlinks.

Example: `python -m unittest tests.test_listening_synth` needs no voice download.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_whisper_subs import listening_synth
from yt_whisper_subs import listening_tts


class SynthesisTests(unittest.TestCase):
    """Exercise the full batch entry point against a linked data install; e.g. Windows must receive the real dictionary directory."""

    def test_file_symlink_install_uses_complete_real_data_directory(self) -> None:
        """Keep matching data files together instead of resolving only the containing folder; e.g. uv's phondata link must not reach native stat()."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed, cache = root / "site-package", root / "wheel-cache"
            installed.mkdir()
            cache.mkdir()
            for name in ("phondata", "phontab", "phonindex", "intonations", "nl_dict"):
                target = cache / name
                target.write_bytes(b"\x01\x48\x01\0" + b"fixture dictionary")
                try:
                    (installed / name).symlink_to(target)
                except OSError as exc:
                    self.skipTest(f"Creating file symlinks requires Windows developer mode: {exc}")
            self.assertEqual(installed.resolve(), installed)
            output = root / "speech.wav"
            manifest = root / "batch.json"
            manifest.write_text(json.dumps({"model": "fixture.onnx", "jobs": [
                {"text": "Morgen fiets ik naar de winkel.", "paths": {"normal": str(output)}}]}), encoding="utf-8")

            def load(_model, *, use_cuda, espeak_data_dir):
                """Reject linked files like Windows eSpeak's native metadata check; e.g. every dictionary must come from the resolved wheel tree."""

                self.assertFalse(use_cuda)
                self.assertEqual(espeak_data_dir, cache)
                for path in espeak_data_dir.iterdir():
                    self.assertFalse(path.is_symlink())
                    self.assertGreater(path.stat().st_size, 8)
                return speaker

            def synthesize(_text, stream, **_kwargs):
                """Publish a tiny valid PCM stream after native prerequisites pass; e.g. the ordinary batch publication path still runs."""

                stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                stream.writeframes(b"\0\0" * 2205)

            speaker = mock.Mock(synthesize_wav=synthesize)
            with (
                mock.patch.object(listening_synth.phonemize_espeak, "ESPEAK_DATA_DIR", installed),
                mock.patch.object(listening_synth.voice.PiperVoice, "load", side_effect=load),
            ):
                listening_synth.main(str(manifest))
            self.assertAlmostEqual(listening_tts.duration(output), 0.1)
            self.assertFalse(list(root.glob("*.part")))
