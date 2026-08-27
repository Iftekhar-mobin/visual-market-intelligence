"""Conditional opportunities — the output the rest of the world actually uses.

A scenario is not a recommendation. It is a sentence of the form *"if this
condition holds and that trigger fires, a long is live; it is wrong below
here"*. Both directions are always described, and both may be poor.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .analysis import Evidence


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class MarketState(str, Enum):
    """What a consumer should do right now. NO_TRADE is a first-class answer."""

    WAIT = "WAIT"
    WATCH_LONG = "WATCH_LONG"
    WATCH_SHORT = "WATCH_SHORT"
    LONG_TRIGGERED = "LONG_TRIGGERED"
    SHORT_TRIGGERED = "SHORT_TRIGGERED"
    NO_TRADE = "NO_TRADE"


class Targets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zones: list[float] = Field(default_factory=list)
    rationale: str | None = None


class Scenario(BaseModel):
    """One side of the market, fully conditioned."""

    model_config = ConfigDict(extra="forbid")

    direction: Direction
    setup_type: str = "none"
    quality: str = Field(default="low", description="high | medium | low | none")
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    condition: str | None = Field(default=None, description="What must remain true.")
    entry_zone: list[float] = Field(
        default_factory=list, description="[low, high], or [] when no level could be read."
    )
    trigger: str | None = Field(default=None, description="The event that makes it live.")
    invalidation: str | None = None
    invalidation_price: float | None = None
    targets: Targets = Field(default_factory=Targets)

    supporting: list[Evidence] = Field(default_factory=list)
    conflicting: list[Evidence] = Field(default_factory=list)
    reward_risk: float | None = None


class RiskAssessment(BaseModel):
    """Whether the opportunity can be wrong in a way you could act on."""

    model_config = ConfigDict(extra="forbid")

    has_clear_invalidation: bool = False
    volatility_risk: str = "unknown"
    structural_risks: list[str] = Field(default_factory=list)
    conflicting_signals: list[str] = Field(default_factory=list)
    uncertainty: str = "unknown"
    reward_risk_note: str | None = None
    risks: list[str] = Field(default_factory=list)
    veto: bool = Field(default=False, description="Risk agent forced the state to NO_TRADE.")
    veto_reason: str | None = None
