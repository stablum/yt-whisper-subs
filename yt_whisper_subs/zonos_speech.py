"""Render configurable Zonos voices through the installed open-source native runtime.

Example: two named presets share one model while keeping independent cached normal and slow recordings.
"""

import uuid
from pathlib import Path

from yt_whisper_subs import listening_tts
from yt_whisper_subs import listening_voices
from yt_whisper_subs import proc
from yt_whisper_subs import zonos_install


class ZonosSpeech:
    """Own one audible preset without a resident model server; e.g. closed practice cannot leave an idle GPU allocation behind."""

    def __init__(self, config: listening_voices.Config) -> None:
        """Bind reproducible settings to a voice identity; e.g. labels can change while audio is reused."""

        self.config = config
        self.profile = f"zonos3-mini:{zonos_install.REVISION}:{zonos_install.SOURCE}:" + config.identity()

    def _command(self, install: dict, text: str, output: Path, speed: str) -> list:
        """Translate friendly control strengths into calibrated native doses; e.g. the slow button retains the selected voice and emotion."""

        files, cfg = install["files"], self.config
        cmd = [install["binary"], files["gguf/backbone-ptq.gguf"], "--cpu", "--tts", text, output,
               "--zac", files["gguf/zac-keep2.gguf"], "--steer", files["gguf/steer.gguf"],
               "--seed", str(cfg.seed), "--max", "450", "--cfg-scale", str(cfg.guidance), "--headroom-db", str(cfg.headroom)]
        if cfg.reference:
            cmd.extend(["--clone", str(Path(cfg.reference).expanduser().resolve()), "--spk-encoder", files["gguf/spk-encoder.gguf"]])
        else:
            voice = next(row for row in listening_voices.catalog()["voices"] if row["name"] == cfg.voice)
            cmd.extend(["--speaker-embedding", files[voice["file"]]])
        controls = cfg.steering()
        # Tempo is a measured style axis: slowing speech keeps its pitch and character.
        adjustment = -0.6 if speed == "slow" else 0
        if cfg.pace != 1 or adjustment:
            controls["delivery.tempo"] = max(-1, min(1, controls.get("delivery.tempo", 0) + (cfg.pace - 1) + adjustment))
        for row in listening_voices.catalog()["controls"]:
            if strength := controls.get(row["id"], 0):
                cmd.extend(["--control", f"{row['id']}={strength * row['scale']:.6g}"])
        return cmd

    def render_many(self, jobs: list, report) -> None:
        """Publish verified WAVs atomically and keep source text out of progress logs; e.g. preparing a hidden question cannot display its transcript."""

        install = zonos_install.installation()
        with listening_tts.synthesis():
            for idx, (text, paths) in enumerate(jobs, 1):
                for speed, path in paths.items():
                    report(f"Preparing {self.config.voice} · {idx}/{len(jobs)} · {speed}")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.wav")
                    try:
                        command = self._command(install, text, temp, speed)
                        result = proc.run(command, capture_stdout=True, stream_stdout=True, check=False, quiet=True)
                        if result.returncode or "eos_frame=-1" in (result.stdout or ""):
                            log = zonos_install.root() / "last-error.log"
                            log.write_text(result.stdout or "Native speech worker failed.", encoding="utf-8")
                            if "eos_frame=-1" in (result.stdout or ""):
                                raise RuntimeError(f"Zonos could not finish the {speed} recording with this expression mix. "
                                                   "Lower Overall expression in Voices (try 25–50%) and preview again. "
                                                   "Your answer and preset are saved.")
                            raise RuntimeError("Zonos speech synthesis failed. Check the voice preview; diagnostics are in the shared model folder's last-error.log.")
                        listening_tts.duration(temp)
                        self._gain(temp)
                        temp.replace(path)
                    finally:
                        temp.unlink(missing_ok=True)

    def _gain(self, path: Path) -> None:
        """Apply the preset's output level without changing pitch or speed; e.g. a quiet character can remain comfortable to hear."""

        if self.config.volume == 1:
            return
        import wave
        import numpy as np

        with wave.open(str(path), "rb") as stream:
            params = stream.getparams()
            pcm = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32)
        pcm = np.clip(pcm * self.config.volume, -32768, 32767).astype("<i2")
        with wave.open(str(path), "wb") as stream:
            stream.setparams(params)
            stream.writeframes(pcm.tobytes())
