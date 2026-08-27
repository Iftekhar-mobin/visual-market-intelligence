"""What a visual analyst saw, and what the synthesis agent made of three of them.

Two rules shape every model here:

* **Observation, inference and uncertainty are different things.** `Evidence`
  carries a `kind` so a downstream reader can weigh "the candles made a lower
  high" differently from "this looks like a bull flag".
* **A missing answer is a legal answer.** Every scalar the model might not be
  able to read is optional. Nothing in the pipeline fills a null with a guess.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .market import Level


class Trend(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    SIDEWAYS = "sideways"
    UNCLEAR = "unclear"


class TrendStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


class Regime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    BREAKOUT = "breakout"
    REVERSAL = "reversal"
    CONSOLIDATION = "consolidation"
    VOLATILE = "volatile"
    UNCLEAR = "unclear"


class Momentum(str, Enum):
    ACCELERATING = "accelerating"
    STEADY = "steady"
    FADING = "fading"
    DIVERGING = "diverging"
    UNCLEAR = "unclear"


class Volatility(str, Enum):
    EXPANDING = "expanding"
    STABLE = "stable"
    CONTRACTING = "contracting"
    UNCLEAR = "unclear"


class SetupClass(str, Enum):
    LONG_SETUP = "LONG_SETUP"
    SHORT_SETUP = "SHORT_SETUP"
    NO_SETUP = "NO_SETUP"


class Alignment(str, Enum):
    ALIGNED_BULLISH = "ALIGNED_BULLISH"
    ALIGNED_BEARISH = "ALIGNED_BEARISH"
    PARTIALLY_ALIGNED = "PARTIALLY_ALIGNED"
    CONFLICTING = "CONFLICTING"
    NEUTRAL = "NEUTRAL"


class EvidenceKind(str, Enum):
    """The epistemic status of a statement. Never collapsed downstream."""

    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    UNCERTAIN = "UNCERTAIN"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind = EvidenceKind.OBSERVED
    statement: str
    source: str = Field(default="", description="Which chart element supports it.")


class TimeframeObservation(BaseModel):
    """One analyst's reading of one chart.

    The `context` analyst leaves `setup` at NO_SETUP by construction: deciding
    whether to trade is not its job, and a prompt that lets it try is a prompt
    that produces a trade on every chart.
    """

    model_config = ConfigDict(extra="forbid")

    timeframe: str
    role: str
    trend: Trend = Trend.UNCLEAR
    trend_strength: TrendStrength = TrendStrength.NONE
    regime: Regime = Regime.UNCLEAR
    momentum: Momentum = Momentum.UNCLEAR
    volatility: Volatility = Volatility.UNCLEAR
    structure: str | None = Field(default=None, description="HH/HL, LH/LL, range, …")

    support: list[float] = Field(default_factory=list)
    resistance: list[float] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)

    setup: SetupClass = SetupClass.NO_SETUP
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    bullish_scenario: str | None = None
    bearish_scenario: str | None = None
    entry_confirmation: str | None = None
    entry_warning: str | None = None

    evidence: list[Evidence] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    rejected_levels: list[float] = Field(
        default_factory=list,
        description="Prices the analyst reported that fall outside the drawn range.",
    )
    model_name: str = ""
    provider: str = ""
    prompt_version: str = ""
    duration_ms: float = 0.0
    degraded: bool = Field(
        default=False, description="True when the model failed and this is a null reading."
    )
    error: str | None = None
    raw_text: str = ""

    def levels(self) -> list[Level]:
        from .market import LevelKind  # local import keeps the module import-cycle free

        return [Level(price=p, kind=LevelKind.SUPPORT) for p in self.support] + [
            Level(price=p, kind=LevelKind.RESISTANCE) for p in self.resistance
        ]


class StructureSynthesis(BaseModel):
    """Three readings reconciled into one view of the market."""

    model_config = ConfigDict(extra="forbid")

    alignment: Alignment = Alignment.NEUTRAL
    dominant_timeframe: str = ""
    bias: Trend = Trend.UNCLEAR
    regime: Regime = Regime.UNCLEAR
    momentum_agreement: bool = False
    volatility_agreement: bool = False
    agreements: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    support_zones: list[float] = Field(default_factory=list)
    resistance_zones: list[float] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
