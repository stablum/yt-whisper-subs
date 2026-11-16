"""Name reusable voice presets independently from sentence selection and grading.

Example: the same question can be heard with Piper and two differently styled Zonos voices.
"""

import hashlib
import json
import uuid
import functools
from pathlib import Path
from typing import Literal

import pydantic as pd


SETTING = "listening_voices"
CATALOG = Path(__file__).with_name("zonos_catalog.json")


@functools.cache
def catalog() -> dict:
    """Read the pinned public voice/control catalogue; e.g. opening configuration never downloads or loads a model."""

    return json.loads(CATALOG.read_text(encoding="utf-8"))


class Config(pd.BaseModel):
    """Validate audible settings with neutral defaults; e.g. changing a display name does not invalidate recorded speech."""

    model_config = pd.ConfigDict(extra="forbid")
    engine: Literal["piper", "zonos"] = "piper"
    voice: str = "Savannah"
    reference: str = ""
    controls: dict[str, float] = pd.Field(default_factory=dict)
    pace: float = pd.Field(default=1, ge=0.6, le=1.5)
    volume: float = pd.Field(default=1, ge=0.2, le=1.5)
    seed: int = pd.Field(default=1, ge=0, le=2147483647)
    guidance: float = pd.Field(default=1, ge=1, le=3)
    headroom: float = pd.Field(default=3, ge=0, le=24)

    @pd.field_validator("controls")
    @classmethod
    def checked_controls(cls, value: dict) -> dict:
        """Keep settings within the published direction catalogue; e.g. unknown slider IDs cannot become CLI arguments."""

        definitions = {row["id"]: row for row in catalog()["controls"]}
        for key, strength in value.items():
            if key not in definitions or not -1 <= strength <= 1:
                raise ValueError("Unknown voice control or strength outside its recommended range")
            if strength < 0 and not definitions[key]["axis"]:
                raise ValueError("This voice style has no negative direction")
        return {key: strength for key, strength in value.items() if strength}

    @pd.field_validator("voice")
    @classmethod
    def checked_voice(cls, value: str) -> str:
        """Choose a published speaker identity; e.g. custom recordings use the separate local reference field."""

        if value not in {row["name"] for row in catalog()["voices"]}:
            raise ValueError("Unknown Zonos voice")
        return value

    def identity(self) -> str:
        """Fingerprint only audio-producing choices; e.g. each custom recording's content participates in cache identity."""

        data = self.model_dump(mode="json")
        if self.engine == "piper":
            data = {key: data[key] for key in ("engine", "pace", "volume")}
        elif self.reference:
            path = Path(self.reference).expanduser()
            with path.open("rb") as stream:
                data["reference"] = hashlib.file_digest(stream, "sha256").hexdigest()
        text = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Preset(pd.BaseModel):
    """Keep editable labels separate from stable preset IDs; e.g. renaming Clear Dutch preserves its cached recordings."""

    model_config = pd.ConfigDict(extra="forbid")
    id: str = pd.Field(default_factory=lambda: uuid.uuid4().hex)
    name: str = pd.Field(min_length=1, max_length=60)
    enabled: bool = True
    config: Config = pd.Field(default_factory=Config)


class Voices(pd.BaseModel):
    """Store an ordered, enabled set for one-click listening comparisons; e.g. only enabled presets are pre-rendered."""

    model_config = pd.ConfigDict(extra="forbid")
    presets: list[Preset] = pd.Field(default_factory=lambda: [
        Preset(id="piper", name="Piper"),
        Preset(id="zonos-clear", name="Zonos · clear", enabled=False,
               config=Config(engine="zonos", controls={"mood.clarity": 0.35, "mood.articulation": 0.2})),
        Preset(id="zonos-conversation", name="Zonos · conversational", enabled=False,
               config=Config(engine="zonos", voice="Imogen", controls={"delivery.conversational": 0.5})),
    ], min_length=1, max_length=50)

    @pd.model_validator(mode="after")
    def valid_set(self):
        """Require distinct identities and a usable playback choice; e.g. disabling every voice cannot strand practice."""

        if not any(row.enabled for row in self.presets):
            raise ValueError("Enable at least one voice")
        if len({row.id for row in self.presets}) != len(self.presets):
            raise ValueError("Duplicate voice preset identity")
        return self

    @property
    def enabled(self) -> list[Preset]:
        """Preserve the chosen playback order; e.g. the first enabled voice supplies automatic first playback."""

        return [row for row in self.presets if row.enabled]


def read(db) -> Voices:
    """Load the library's shared voice choices; e.g. new rounds and current-question comparisons see the same presets."""

    saved = db.setting(SETTING, "")
    return Voices.model_validate_json(saved) if saved else Voices()


def speech(db, cfg: Config):
    """Choose the synthesis strategy at the domain boundary; e.g. tests can still inject a deterministic speech provider."""

    if cfg.engine == "zonos":
        from yt_whisper_subs import zonos_speech

        return zonos_speech.ZonosSpeech(cfg)
    from yt_whisper_subs import listening_tts

    return listening_tts.PiperSpeech(db.path.parent / "practice-audio", config=cfg)
