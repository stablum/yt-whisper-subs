"""Minimal OpenAI Responses API client with dotenv loading and retries.

Example: `openai_client.responses_api_request(args, payload)`.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from yt_whisper_subs import cfg

TRANSIENT_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
TRANSIENT_NETWORK_ERRORS = (
    urlerror.URLError,
    http.client.HTTPException,
    ConnectionError,
    TimeoutError,
    OSError,
)


def strip_env_quotes(value: str) -> str:
    """Remove simple dotenv quote wrappers from a value.

    Example: `strip_env_quotes(value)`.
    """

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: str | os.PathLike[str] | None) -> None:
    """Load a tiny dotenv subset without adding another runtime dependency.

    Example: `load_env_file(".env")`.
    """

    if not path:
        return

    env_path = Path(path).expanduser()
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, strip_env_quotes(value))


def api_key(args: argparse.Namespace) -> str:
    """Resolve the OpenAI API key from env or the configured env file.

    Example: `api_key(args)`.
    """

    load_env_file(getattr(args, "openai_env_file", cfg.DEFAULT_OPENAI_ENV_FILE))
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to the .env file next to this script "
            "or set it in the environment."
        )
    return key


def retry_after_seconds(exc: urlerror.HTTPError) -> float | None:
    """Parse HTTP Retry-After seconds for transient OpenAI responses.

    Example: `retry_after_seconds(http_error)`.
    """

    retry_after = exc.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        seconds = float(retry_after)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Choose exponential backoff unless the server supplies Retry-After.

    Example: `retry_delay(2)`.
    """

    if retry_after is not None:
        return retry_after
    return cfg.DEFAULT_OPENAI_RETRY_INITIAL_DELAY * (2 ** max(0, attempt - 1))


def responses_api_request(args: argparse.Namespace, payload: dict[str, object]) -> dict[str, object]:
    """Call the Responses API with retry handling for transient failures.

    Example: `responses_api_request(args, payload)`.
    """

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    max_retries = max(0, int(getattr(args, "openai_max_retries", cfg.DEFAULT_OPENAI_MAX_RETRIES)))
    total_attempts = max_retries + 1

    for attempt in range(1, total_attempts + 1):
        request = urlrequest.Request(
            "https://api.openai.com/v1/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key(args)}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlrequest.urlopen(
                request,
                timeout=getattr(args, "openai_timeout", cfg.DEFAULT_OPENAI_TIMEOUT),
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            details = details[:2000] if details else exc.reason
            if exc.code not in TRANSIENT_HTTP_STATUS_CODES or attempt >= total_attempts:
                raise RuntimeError(f"OpenAI request failed with HTTP {exc.code}: {details}") from exc
            delay = retry_delay(attempt, retry_after_seconds(exc))
            print(
                f"OpenAI request failed with HTTP {exc.code}; retrying "
                f"{attempt}/{max_retries} in {delay:g}s..."
            )
            time.sleep(delay)
        except TRANSIENT_NETWORK_ERRORS as exc:
            if attempt >= total_attempts:
                reason = getattr(exc, "reason", exc)
                raise RuntimeError(f"OpenAI request failed: {reason}") from exc
            delay = retry_delay(attempt)
            reason = getattr(exc, "reason", exc)
            print(f"OpenAI request failed: {reason}; retrying {attempt}/{max_retries} in {delay:g}s...")
            time.sleep(delay)

    raise RuntimeError("OpenAI request failed after retries.")


def response_output_text(data: dict[str, object]) -> str:
    """Extract text from both current and nested Responses API response shapes.

    Example: `response_output_text(response)`.
    """

    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    parts: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for entry in content:
                if not isinstance(entry, dict):
                    continue
                text = entry.get("text")
                if isinstance(text, str):
                    parts.append(text)

    text = "".join(parts).strip()
    if not text:
        status = data.get("status")
        raise RuntimeError(f"OpenAI response did not include output text; status={status!r}")
    return text
