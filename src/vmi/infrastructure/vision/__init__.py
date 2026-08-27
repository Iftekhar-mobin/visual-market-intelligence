"""Vision backends. Four implementations, one factory, no vendor in the agents.

The whole point of this package is that `application/agents` never imports any
of it: it is handed something satisfying `VisionModel` and cannot tell whether
the answer came from a 7B model on a laptop, a free hosted one, or arithmetic.
Swapping backends is a config line, and — should a paid model ever be worth it —
adding one is a subclass of `OpenAICompatibleVisionModel` with a different host.
"""

from __future__ import annotations

from typing import Any

from ...config import Config, VisionConfig
from .base import BaseVisionModel, VisionError, extract_json
from .ollama import DEFAULT_BASE_URL as OLLAMA_URL
from .ollama import OllamaVisionModel
from .openai_compatible import (
    FREE_VISION_MODELS,
    OPENROUTER_URL,
    OpenAICompatibleVisionModel,
    OpenRouterVisionModel,
)
from .stub import StubVisionModel

__all__ = [
    "FREE_VISION_MODELS",
    "OLLAMA_URL",
    "OPENROUTER_URL",
    "BaseVisionModel",
    "OllamaVisionModel",
    "OpenAICompatibleVisionModel",
    "OpenRouterVisionModel",
    "StubVisionModel",
    "VisionError",
    "build_vision_model",
    "extract_json",
    "recommended_models",
]

PROVIDERS = {
    "ollama": OllamaVisionModel,
    "openai_compatible": OpenAICompatibleVisionModel,
    "openrouter": OpenRouterVisionModel,
    "stub": StubVisionModel,
}

RECOMMENDED: list[dict[str, Any]] = [
    {
        "id": "qwen2.5vl:7b",
        "provider": "ollama",
        "size_gb": 6.0,
        "note": "Best free local reader of a chart. Needs ~8 GB RAM; slow but fine on CPU.",
    },
    {
        "id": "qwen2.5vl:3b",
        "provider": "ollama",
        "size_gb": 3.2,
        "note": "Half the memory, most of the ability. The one to start with on a laptop.",
    },
    {
        "id": "llama3.2-vision:11b",
        "provider": "ollama",
        "size_gb": 7.9,
        "note": "Strong general vision; weaker at reading small numbers off an axis.",
    },
    {
        "id": "moondream:latest",
        "provider": "ollama",
        "size_gb": 1.7,
        "note": "Tiny and quick. Use it to prove the plumbing works, not to trade.",
    },
    {
        "id": "qwen/qwen2.5-vl-72b-instruct:free",
        "provider": "openrouter",
        "size_gb": 0.0,
        "note": "Free tier, no download, far stronger than anything local. Rate limited.",
    },
]
"""What to install, in the order worth trying. Every entry costs nothing."""


def recommended_models(provider: str | None = None) -> list[dict[str, Any]]:
    if provider is None:
        return list(RECOMMENDED)
    return [model for model in RECOMMENDED if model["provider"] == provider]


def build_vision_model(config: Config | VisionConfig, **overrides: Any) -> BaseVisionModel:
    """Instantiate the configured backend.

    Accepts either the whole config or just its `vision` section, because the
    API's per-request override path only carries the latter.
    """
    settings = config.vision if isinstance(config, Config) else config
    merged = settings.model_copy(update=overrides) if overrides else settings
    factory = PROVIDERS.get(merged.provider)
    if factory is None:
        raise ValueError(f"unknown vision provider {merged.provider!r}; have {sorted(PROVIDERS)}")

    kwargs: dict[str, Any] = {
        "model": merged.model,
        "timeout_s": merged.timeout_s,
        "temperature": merged.temperature,
        "max_tokens": merged.max_tokens,
        "retries": merged.retries,
    }
    if factory is StubVisionModel:
        # The configured model id names a vision model the stub does not have and
        # would misreport as the author of its arithmetic.
        kwargs["model"] = "rules-v1"
    else:
        kwargs["base_url"] = merged.base_url
        kwargs["api_key"] = merged.api_key
    if factory is OllamaVisionModel:
        kwargs.pop("api_key", None)
    return factory(**kwargs)
