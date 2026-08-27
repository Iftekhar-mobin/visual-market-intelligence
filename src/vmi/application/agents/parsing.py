"""Turning a model's JSON into a `TimeframeObservation` without trusting it.

Small models get the shape almost right: `"trend": "Bullish trend"` instead of
`"bullish"`, `"confidence": "70%"`, a support level with a decimal point in the
wrong place, a list where a string was asked for. Every one of those is a
recoverable mistake, and a pipeline that raises on them will fail most runs on a
7B model for no good reason.

What is *not* recovered: a price outside the range the chart covers. That is not
a formatting slip, it is a fabricated number, and it is recorded in
`rejected_levels` so the report can say the model did it.
"""

from __future__ import annotations

import re
from typing import Any

from ...domain.models import (
    Evidence,
    Momentum,
    PriceWindow,
    Regime,
    SetupClass,
    TimeframeObservation,
    Trend,
    TrendStrength,
    Volatility,
)
from ...domain.models.analysis import EvidenceKind

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _enum(value: Any, enum_type: type, default: Any) -> Any:
    """Map free text onto an enum by exact match, then by substring."""
    if value is None:
        return default
    text = str(value).strip().lower().replace(" ", "_")
    for member in enum_type:
        if member.value.lower() == text:
            return member
    for member in enum_type:
        name = member.value.lower()
        if name in text or text in name:
            return member
    return default


def _confidence(value: Any) -> float:
    """`0.7`, `"70%"`, `"high"` → a number in [0, 1]."""
    if isinstance(value, (int, float)):
        number = float(value)
        return round(min(max(number / 100 if number > 1 else number, 0.0), 1.0), 3)
    text = str(value or "").strip().lower()
    words = {
        "very high": 0.9, "high": 0.75, "medium": 0.5,
        "moderate": 0.5, "low": 0.25, "none": 0.0,
    }
    for word, score in words.items():
        if word in text:
            return score
    match = _NUMBER.search(text)
    if not match:
        return 0.0
    number = float(match.group().replace(",", "."))
    return round(min(max(number / 100 if number > 1 else number, 0.0), 1.0), 3)


def _prices(value: Any, window: PriceWindow) -> tuple[list[float], list[float]]:
    """(accepted, rejected) prices, filtered against the drawn range."""
    if value is None:
        return [], []
    items = value if isinstance(value, (list, tuple)) else [value]
    accepted: list[float] = []
    rejected: list[float] = []
    for item in items:
        number = _price(item)
        if number is None:
            continue
        if window.contains(number):
            accepted.append(round(number, window.digits))
        else:
            rejected.append(round(number, window.digits))
    # Nearest the last close first: that is the order a reader wants them in.
    accepted.sort(key=lambda price: abs(price - window.last_close))
    return accepted[:5], rejected[:5]


def _price(item: Any) -> float | None:
    if isinstance(item, (int, float)):
        return float(item)
    if isinstance(item, dict):  # {"price": 1.084, "note": "..."} happens a lot
        for key in ("price", "level", "value"):
            if key in item:
                return _price(item[key])
        return None
    match = _NUMBER.search(str(item))
    if not match:
        return None
    try:
        return float(match.group().replace(",", "."))
    except ValueError:
        return None


def _strings(value: Any, limit: int = 6) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for item in items:
        text = str(item.get("name", item) if isinstance(item, dict) else item).strip()
        if text and text.lower() not in {"none", "null", "n/a", "[]"}:
            out.append(text[:200])
    return out[:limit]


def _sentence(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:400] if text and text.lower() not in {"none", "null", "n/a"} else None


def _evidence(value: Any) -> list[Evidence]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[Evidence] = []
    for item in value:
        if isinstance(item, dict):
            statement = _sentence(item.get("statement") or item.get("text") or item.get("evidence"))
            if not statement:
                continue
            out.append(
                Evidence(
                    kind=_enum(item.get("kind"), EvidenceKind, EvidenceKind.OBSERVED),
                    statement=statement,
                    source=str(item.get("source", ""))[:120],
                )
            )
        else:
            statement = _sentence(item)
            if statement:
                out.append(Evidence(statement=statement))
    return out[:10]


def parse_observation(
    payload: dict[str, Any],
    *,
    timeframe: str,
    role: str,
    window: PriceWindow,
    provider: str,
    model: str,
    prompt_version: str,
    duration_ms: float,
    raw_text: str,
) -> TimeframeObservation:
    """Build the typed observation. Never raises on a badly shaped payload."""
    support, rejected_support = _prices(payload.get("support"), window)
    resistance, rejected_resistance = _prices(payload.get("resistance"), window)

    setup = _enum(payload.get("setup"), SetupClass, SetupClass.NO_SETUP)
    if role == "context":
        # The context analyst is not allowed a trade opinion, whatever it said.
        setup = SetupClass.NO_SETUP

    uncertainties = _strings(payload.get("uncertainties"))
    if rejected_support or rejected_resistance:
        uncertainties.append(
            "levels outside the drawn price range were reported and discarded: "
            + ", ".join(str(price) for price in rejected_support + rejected_resistance)
        )

    return TimeframeObservation(
        timeframe=timeframe,
        role=role,
        trend=_enum(payload.get("trend"), Trend, Trend.UNCLEAR),
        trend_strength=_enum(payload.get("trend_strength"), TrendStrength, TrendStrength.NONE),
        regime=_enum(payload.get("regime"), Regime, Regime.UNCLEAR),
        momentum=_enum(payload.get("momentum"), Momentum, Momentum.UNCLEAR),
        volatility=_enum(payload.get("volatility"), Volatility, Volatility.UNCLEAR),
        structure=_sentence(payload.get("structure")),
        support=support,
        resistance=resistance,
        patterns=_strings(payload.get("patterns")),
        setup=setup,
        confidence=_confidence(payload.get("confidence")),
        bullish_scenario=_sentence(payload.get("bullish_scenario")),
        bearish_scenario=_sentence(payload.get("bearish_scenario")),
        entry_confirmation=_sentence(payload.get("entry_confirmation")),
        entry_warning=_sentence(payload.get("entry_warning")),
        evidence=_evidence(payload.get("evidence")),
        uncertainties=uncertainties,
        rejected_levels=rejected_support + rejected_resistance,
        model_name=model,
        provider=provider,
        prompt_version=prompt_version,
        duration_ms=round(duration_ms, 1),
        raw_text=raw_text[:8000],
    )


def degraded_observation(
    *, timeframe: str, role: str, provider: str, model: str, prompt_version: str, error: str
) -> TimeframeObservation:
    """A null reading, for when the model could not answer at all.

    The run continues: two of three timeframes is a weaker report, not no
    report, and the synthesis agent already knows how to discount missing
    evidence. What it must never do is silently look like a real reading, which
    is what `degraded` and a zero confidence are for.
    """
    return TimeframeObservation(
        timeframe=timeframe,
        role=role,
        confidence=0.0,
        degraded=True,
        error=error,
        provider=provider,
        model_name=model,
        prompt_version=prompt_version,
        uncertainties=[f"no reading for {timeframe}: {error}"],
    )
