"""Ollama — the default backend, and the reason this system costs nothing to run.

`ollama pull qwen2.5vl:7b` and the whole pipeline works offline on a laptop.
The HTTP API is spoken directly with httpx rather than through the `ollama`
package: it is three endpoints, and one fewer dependency is one fewer thing that
can break a deployment.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...logging_utils import get_logger
from .base import BaseVisionModel, VisionError

log = get_logger("vision.ollama")

DEFAULT_BASE_URL = "http://127.0.0.1:11434"

VISION_FAMILIES = (
    "vl", "vision", "llava", "moondream", "minicpm", "bakllava", "gemma3", "pixtral",
)
"""Substrings that mark a locally installed model as multimodal. Ollama's tag
list does not say, and asking a text model to read a chart wastes a minute
before failing in a confusing way."""


class OllamaVisionModel(BaseVisionModel):
    """The `VisionModel` port, over a local Ollama server."""

    provider = "ollama"

    def __init__(
        self, model: str = "qwen2.5vl:7b", base_url: str = DEFAULT_BASE_URL, **kwargs: Any
    ):
        kwargs.pop("api_key", None)  # a local server has no key
        super().__init__(model=model, base_url=base_url or DEFAULT_BASE_URL, **kwargs)

    def _call(self, image_b64: str, prompt: str, system: str | None) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "images": [image_b64],
            "stream": False,
            "format": "json",  # Ollama constrains the sampler to valid JSON
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                # A chart is a large image; the default 2k context truncates the
                # prompt before the model ever sees the instructions.
                "num_ctx": 8192,
            },
        }
        if system:
            payload["system"] = system

        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(f"{self.base_url}/api/generate", json=payload)
            if response.status_code == 404:
                raise VisionError(
                    f"Ollama has no model named {self.model!r}. Run `ollama pull {self.model}`."
                )
            response.raise_for_status()
            body = response.json()
        return str(body.get("response", ""))

    def available(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                installed = {item["name"] for item in response.json().get("models", [])}
        except Exception as exc:
            return False, (
                f"Ollama is not answering at {self.base_url} ({exc}). "
                "Install it from ollama.com and run `ollama serve`."
            )
        if self.model not in installed and f"{self.model}:latest" not in installed:
            return False, f"{self.model} is not pulled. Run `ollama pull {self.model}`."
        return True, f"{len(installed)} models installed"

    def list_models(self) -> list[dict[str, Any]]:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                models = response.json().get("models", [])
        except Exception as exc:
            log.debug("cannot list Ollama models: %s", exc)
            return []
        return [
            {
                "id": item["name"],
                "provider": self.provider,
                "size_gb": round(item.get("size", 0) / 1e9, 2),
                "vision": any(token in item["name"].lower() for token in VISION_FAMILIES),
                "free": True,
                "installed": True,
            }
            for item in models
        ]

    def pull(self, model: str, timeout_s: float = 1800.0) -> dict[str, Any]:
        """Download a model. Gigabytes over the network, hence the long timeout."""
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(
                f"{self.base_url}/api/pull", json={"name": model, "stream": False}
            )
            response.raise_for_status()
            return dict(response.json())
