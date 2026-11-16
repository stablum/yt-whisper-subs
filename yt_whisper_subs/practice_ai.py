"""Shared structured Responses transport for practice providers.

Example: vocabulary generation and verb correction share credentials and strict schema handling.
"""

from __future__ import annotations

import argparse
import json

import pydantic as pd

from yt_whisper_subs import cfg
from yt_whisper_subs import openai_client
from yt_whisper_subs import task_cancel


class StructuredGenerator:
    """Keep HTTP policy outside linguistic tasks; e.g. a failed correction can be retried explicitly."""

    def __init__(self, args: argparse.Namespace | None = None) -> None:
        """Use the dedicated practice model; e.g. verb exercises do not change subtitle settings."""

        self._args = argparse.Namespace(**vars(args or argparse.Namespace()))
        self._args.openai_timeout = 90.0
        self._args.openai_max_retries = 0
        self.model = getattr(self._args, "openai_practice_model", cfg.DEFAULT_PRACTICE_MODEL)
        self.reasoning = getattr(self._args, "openai_practice_reasoning", cfg.DEFAULT_PRACTICE_REASONING)

    def _request[T: pd.BaseModel](self, model: type[T], prompt: str, document: dict, *, timeout: float | None = None) -> T:
        """Validate a complete strict-schema result; e.g. refusal or truncated JSON never becomes a grade."""

        if control := task_cancel.current():
            control.checkpoint()
        payload = {"model": self.model, "instructions": prompt,
                   "input": json.dumps(document, ensure_ascii=False), "store": False,
                   "reasoning": {"effort": self.reasoning},
                   "text": {"format": {"type": "json_schema", "name": model.__name__.lower(),
                                       "strict": True, "schema": model.model_json_schema()}}}
        args = self._args
        if timeout is not None:
            args = argparse.Namespace(**vars(self._args))
            args.openai_timeout = timeout
        result = openai_client.responses_api_request(args, payload)
        if control := task_cancel.current():
            control.checkpoint()
        return model.model_validate_json(openai_client.response_output_text(result))
