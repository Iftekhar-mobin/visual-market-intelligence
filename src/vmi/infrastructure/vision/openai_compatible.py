"""Any server that speaks `POST /v1/chat/completions` with image content parts.

That is llama.cpp's server, LM Studio, vLLM, Jan, text-generation-webui,
OpenRouter, and — should the user ever want to pay for one — every hosted API
worth naming. One class covers all of them because the wire format is the same;
only the base URL and whether a key is required differ.

`OpenRouterVisionModel` is the same class with a different default host and a
model list that knows which entries are free.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...logging_utils import get_logger
from .base import BaseVisionModel, VisionError

log = get_logger("vision.openai")

OPENROUTER_URL = "https://openrouter.ai/api/v1"

FREE_VISION_MODELS = [
    "qwen/qwen2.5-vl-72b-instruct:free",
    "qwen/qwen2.5-vl-32b-instruct:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
]
"""Known free multimodal models on OpenRouter, best first. The catalogue is
fetched live when a key is configured; this list is what the console offers
before one is."""


class OpenAICompatibleVisionModel(BaseVisionModel):
    """The `VisionModel` port, over the chat-completions wire format."""

    provider = "openai_compatible"

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:8080/v1", **kwargs: Any):
        super().__init__(model=model, base_url=base_url, **kwargs)

    # ------------------------------------------------------------------ wire

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _messages(self, image_b64: str, prompt: str, system: str | None) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        )
        return messages

    def _call(self, image_b64: str, prompt: str, system: str | None) -> str:
        payload = {
            "model": self.model,
            "messages": self._messages(image_b64, prompt, system),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # Not every local server honours this; the parser copes when it does not.
            "response_format": {"type": "json_object"},
        }
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
            )
            if response.status_code == 401:
                raise VisionError("the backend rejected the API key (401)")
            if response.status_code == 429:
                raise VisionError(
                    "rate limited (429). Free tiers are shared; "
                    "wait a minute, or pick another model."
                )
            if response.status_code >= 400:
                raise VisionError(f"HTTP {response.status_code}: {response.text[:300]}")
            body = response.json()

        choices = body.get("choices") or []
        if not choices:
            raise VisionError(f"no choices in response: {str(body)[:200]}")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)

    def available(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=8.0) as client:
                response = client.get(f"{self.base_url}/models", headers=self._headers())
                response.raise_for_status()
                count = len(response.json().get("data", []))
        except Exception as exc:
            return False, f"{self.base_url} is not answering ({exc})"
        return True, f"{count} models served"

    def list_models(self) -> list[dict[str, Any]]:
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(f"{self.base_url}/models", headers=self._headers())
                response.raise_for_status()
                data = response.json().get("data", [])
        except Exception as exc:
            log.debug("cannot list models at %s: %s", self.base_url, exc)
            return []
        return [
            {
                "id": item.get("id", ""),
                "provider": self.provider,
                "vision": True,
                "free": str(item.get("id", "")).endswith(":free"),
                "installed": True,
            }
            for item in data
        ]


class OpenRouterVisionModel(OpenAICompatibleVisionModel):
    """OpenRouter, with the free tier surfaced.

    The `:free` models are rate-limited and occasionally busy, which is exactly
    what you would expect for free inference. They are, however, far stronger at
    reading a chart than anything that fits on a laptop CPU — so this is the
    backend to reach for when the local model is the bottleneck and the budget
    is still zero.
    """

    provider = "openrouter"

    def __init__(
        self, model: str = FREE_VISION_MODELS[0], base_url: str = OPENROUTER_URL, **kwargs: Any
    ):
        super().__init__(model=model, base_url=base_url or OPENROUTER_URL, **kwargs)

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        # OpenRouter attributes traffic with these; they are optional but polite.
        headers["HTTP-Referer"] = "https://github.com/vmi/visual-market-intelligence"
        headers["X-Title"] = "Visual Market Intelligence"
        return headers

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, (
                "OpenRouter needs a key. Create a free one at openrouter.ai/keys and set "
                "VMI_VISION__API_KEY - free-tier models still cost nothing."
            )
        return super().available()

    def list_models(self) -> list[dict[str, Any]]:
        models = super().list_models()
        if not models:
            return [
                {
                    "id": name, "provider": self.provider,
                    "vision": True, "free": True, "installed": False,
                }
                for name in FREE_VISION_MODELS
            ]
        vision = [
            model
            for model in models
            if any(
                token in model["id"]
                for token in ("vl", "vision", "gemma-3", "pixtral", "llava")
            )
        ]
        return sorted(vision, key=lambda model: (not model["free"], model["id"]))
