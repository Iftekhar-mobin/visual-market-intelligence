"""What every vision backend shares: retries, timing, and getting JSON out of
a model that was asked for JSON and answered with a paragraph.

The port is one method — `analyze(image_b64, prompt, system) -> str` — and it
returns raw text on purpose. Parsing belongs to the agent that knows what shape
it asked for, and a backend that pre-parsed would have to guess.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ...logging_utils import get_logger

log = get_logger("vision")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class VisionError(RuntimeError):
    """The backend could not answer. Carries the reason a user can act on."""


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model's reply.

    Three passes, cheapest first: the whole string, a fenced block, then a brace
    scan that respects strings and escapes. Small local models routinely wrap
    JSON in prose or a fence no matter how firmly the prompt says not to, and
    failing the whole run over a stray "Here you go:" would be absurd.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    for match in _FENCE.finditer(text):
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue

    candidate = _first_object(text)
    if candidate is not None:
        return candidate
    raise ValueError(f"no JSON object in response: {text[:200]!r}")


def _first_object(text: str) -> dict[str, Any] | None:
    """Scan for a balanced `{...}`, ignoring braces inside strings."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : index + 1])
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


class BaseVisionModel:
    """Retry and timing, shared by every HTTP-backed implementation.

    Subclasses implement `_call`. Retries exist because a cold local model
    routinely times out on its first request while the weights page in — that is
    a slow model, not a broken one, and it should not fail the run.
    """

    provider = "base"

    def __init__(
        self,
        model: str,
        base_url: str = "",
        api_key: str = "",
        timeout_s: float = 300.0,
        temperature: float = 0.0,
        max_tokens: int = 1600,
        retries: int = 2,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retries = max(retries, 0)
        self.last_duration_ms = 0.0

    # ------------------------------------------------------------- public API

    def analyze(self, image_b64: str, prompt: str, system: str | None = None) -> str:
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                text = self._call(image_b64, prompt, system)
            except Exception as exc:  # every backend failure looks the same here
                last_error = exc
                log.warning(
                    "%s attempt %d/%d failed: %s",
                    self.provider,
                    attempt + 1,
                    self.retries + 1,
                    exc,
                )
                continue
            self.last_duration_ms = (time.perf_counter() - started) * 1000
            if text.strip():
                return text
            last_error = VisionError("the model returned an empty response")
        self.last_duration_ms = (time.perf_counter() - started) * 1000
        raise VisionError(f"{self.provider}/{self.model}: {last_error}") from last_error

    def available(self) -> tuple[bool, str]:
        return True, ""

    def list_models(self) -> list[dict[str, Any]]:
        return []

    def describe(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url}

    # --------------------------------------------------------------- subclass

    def _call(self, image_b64: str, prompt: str, system: str | None) -> str:
        raise NotImplementedError
