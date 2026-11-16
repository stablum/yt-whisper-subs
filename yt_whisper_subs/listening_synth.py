"""Isolate Piper's native runtime from the GUI and render one bounded audio batch.

Example: one worker loads the voice once for all normal and slow recordings in a prepared round.
"""

import json
import uuid
import wave
from pathlib import Path

from piper import config
from piper import phonemize_espeak
from piper import voice

from yt_whisper_subs import listening_tts


def main(manifest: str) -> None:
    """Consume a local manifest, publishing valid WAVs atomically; e.g. a failed native decode leaves earlier complete speech reusable."""

    document = json.loads(Path(manifest).read_text(encoding="utf-8"))
    # Windows eSpeak's C stat() can see uv's file symlinks as zero bytes. Resolving
    # the directory alone does not help: uv links each data file individually.
    data_dir = (phonemize_espeak.ESPEAK_DATA_DIR / "phondata").resolve(strict=True).parent
    speaker = voice.PiperVoice.load(document["model"], use_cuda=False, espeak_data_dir=data_dir)
    for idx, job in enumerate(document["jobs"], 1):
        print(f"Preparing Dutch speech {idx}/{len(document['jobs'])}", flush=True)
        for speed, value in job["paths"].items():
            path = Path(value)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + f".{uuid.uuid4().hex}.part")
            try:
                cfg = config.SynthesisConfig(length_scale=1.3 if speed == "slow" else 1.0)
                with wave.open(str(temp), "wb") as output:
                    speaker.synthesize_wav(job["text"], output, syn_config=cfg)
                listening_tts.duration(temp)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
