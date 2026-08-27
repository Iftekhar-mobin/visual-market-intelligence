"""A backend that answers without a model — and does not pretend otherwise.

It exists for three jobs:

* **First run.** Someone clones the repository, has no GPU and no Ollama, and
  wants to see the pipeline work end to end before deciding whether to install
  four gigabytes of weights.
* **Regression.** The orchestration, the JSON contracts, the store, the API and
  the console can all be exercised deterministically, with no network and no
  sampling noise.
* **A floor to measure against.** A vision model that cannot beat these rules on
  replayed history is not earning its runtime, and `vmi replay` can say so.

It does **not** look at the image. It reads the `CHART FACTS` block the prompt
builder attaches when `grounding` is `full`, and applies moving-average and
oscillator rules to it. Every observation it produces is tagged `INFERRED`, and
the report records the provider as `stub`, so nothing downstream can mistake
this for chart perception.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import BaseVisionModel

FACTS = re.compile(r"CHART FACTS \(json\)\s*:?\s*(\{.*?\})\s*(?:\n\s*\n|$)", re.DOTALL)

STUB_NOTE = "rule-based stub: derived from indicator values, not from the image"


class StubVisionModel(BaseVisionModel):
    """The `VisionModel` port, implemented with arithmetic instead of a model."""

    provider = "stub"

    def __init__(self, model: str = "rules-v1", **kwargs: Any) -> None:
        kwargs.pop("base_url", None)
        kwargs.pop("api_key", None)
        super().__init__(model=model, base_url="", **kwargs)

    def available(self) -> tuple[bool, str]:
        return True, "always available; reads indicators, never the chart"

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "rules-v1",
                "provider": self.provider,
                "vision": False,
                "free": True,
                "installed": True,
            }
        ]

    def _call(self, image_b64: str, prompt: str, system: str | None) -> str:
        facts = _read_facts(prompt)
        role = _read_role(prompt)
        return json.dumps(_analyse(facts, role))


def _read_facts(prompt: str) -> dict[str, Any]:
    match = FACTS.search(prompt)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except ValueError:
        return {}


def _read_role(prompt: str) -> str:
    for role in ("context", "setup", "entry"):
        if f"role: {role}" in prompt:
            return role
    return "setup"


def _analyse(facts: dict[str, Any], role: str) -> dict[str, Any]:
    indicators = facts.get("indicators") or {}
    close = _number(indicators.get("close"))
    ema20 = _number(indicators.get("ema20"))
    ema50 = _number(indicators.get("ema50"))
    ema200 = _number(indicators.get("ema200"))
    rsi = _number(indicators.get("rsi"))
    hist = _number(indicators.get("macd_hist"))
    atr = _number(indicators.get("atr"))
    atr_pct = _number(indicators.get("atr_pct"))

    fast = ema20 if ema20 is not None else _number(indicators.get("ema9"))
    slow = ema50 if ema50 is not None else ema200

    evidence: list[dict[str, str]] = []
    bullish = bearish = 0

    if close is not None and slow is not None:
        if close > slow:
            bullish += 1
            evidence.append(_note(f"close {close:g} is above the mid moving average {slow:g}"))
        else:
            bearish += 1
            evidence.append(_note(f"close {close:g} is below the mid moving average {slow:g}"))
    if fast is not None and slow is not None:
        if fast > slow:
            bullish += 1
            evidence.append(_note("the fast moving average is above the slow one"))
        else:
            bearish += 1
            evidence.append(_note("the fast moving average is below the slow one"))
    if hist is not None:
        if hist > 0:
            bullish += 1
            evidence.append(_note("the MACD histogram is positive"))
        else:
            bearish += 1
            evidence.append(_note("the MACD histogram is negative"))
    if rsi is not None:
        if rsi >= 55:
            bullish += 1
        elif rsi <= 45:
            bearish += 1
        evidence.append(_note(f"RSI is {rsi:.1f}"))

    total = max(bullish + bearish, 1)
    if bullish > bearish:
        trend, strength_score = "bullish", bullish / total
    elif bearish > bullish:
        trend, strength_score = "bearish", bearish / total
    else:
        trend, strength_score = "sideways", 0.5

    strength = (
        "strong" if strength_score >= 0.85 else "moderate" if strength_score >= 0.6 else "weak"
    )

    separation = abs((fast - slow) / atr) if (fast and slow and atr) else 0.0
    regime = "trending" if separation >= 0.5 else "ranging"
    volatility = (
        "expanding"
        if (atr_pct or 0) > 1.2
        else "contracting"
        if (atr_pct or 0) < 0.35
        else "stable"
    )
    momentum = "accelerating" if abs(hist or 0) > 0 and strength_score > 0.7 else "steady"

    support = [level for level in facts.get("support", []) if isinstance(level, (int, float))]
    resistance = [level for level in facts.get("resistance", []) if isinstance(level, (int, float))]

    setup = "NO_SETUP"
    if role != "context" and strength_score >= 0.7:
        if trend == "bullish" and (rsi is None or rsi < 72):
            setup = "LONG_SETUP"
        elif trend == "bearish" and (rsi is None or rsi > 28):
            setup = "SHORT_SETUP"

    confidence = round(min(0.30 + 0.45 * strength_score, 0.75), 2)

    payload: dict[str, Any] = {
        "trend": trend,
        "trend_strength": strength,
        "regime": regime,
        "momentum": momentum,
        "volatility": volatility,
        "structure": "higher highs and higher lows" if trend == "bullish" else
                     "lower highs and lower lows" if trend == "bearish" else "range",
        "support": support[:3],
        "resistance": resistance[:3],
        "patterns": [],
        "setup": setup,
        "confidence": confidence,
        "evidence": evidence,
        "uncertainties": [STUB_NOTE],
    }
    if role == "context":
        payload["setup"] = "NO_SETUP"
        payload["bullish_scenario"] = (
            f"continuation while price holds above {support[0]:g}" if support else
            "continuation while the trend structure holds"
        )
        payload["bearish_scenario"] = (
            f"failure and rotation lower below {support[0]:g}" if support else
            "structure break and rotation lower"
        )
    if role == "entry":
        payload["entry_confirmation"] = (
            "momentum turning back in the direction of the higher-timeframe trend"
            if setup != "NO_SETUP"
            else None
        )
        payload["entry_warning"] = (
            "no confirmation while momentum is flat" if setup == "NO_SETUP" else None
        )
    return payload


def _note(statement: str) -> dict[str, str]:
    return {"kind": "INFERRED", "statement": statement, "source": "indicator values"}


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None
