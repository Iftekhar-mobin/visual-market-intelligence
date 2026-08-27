"""The report, and everything needed to reproduce it.

`VisionReport` is what `POST /analyze` returns and what the evaluator replays.
It carries its own provenance — model, prompt versions, chart version, the
`as_of` timestamp — because a stored report whose origin is unknown cannot be
scored against what the market did next.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .analysis import StructureSynthesis, TimeframeObservation
from .market import Level
from .opportunity import MarketState, RiskAssessment, Scenario


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentTrace(BaseModel):
    """One agent's turn: who ran, on what, for how long, and did it work."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    duration_ms: float = 0.0
    status: str = "ok"
    error: str | None = None


class RunMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    symbol: str
    created_at: datetime = Field(default_factory=_now)
    as_of: datetime | None = Field(
        default=None,
        description="Replay cut-off. None means live: the newest bar available.",
    )
    provider: str = ""
    model: str = ""
    chart_version: str = ""
    timeframes: list[str] = Field(default_factory=list)
    data_provider: str = ""
    config_digest: str = ""
    duration_ms: float = 0.0


class KeyLevels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    support: list[Level] = Field(default_factory=list)
    resistance: list[Level] = Field(default_factory=list)


class VisionReport(BaseModel):
    """The whole answer, in one object."""

    model_config = ConfigDict(extra="forbid")

    metadata: RunMetadata
    symbol: str
    last_price: float | None = None
    current_state: MarketState = MarketState.NO_TRADE
    market_regime: str = "unclear"

    observations: list[TimeframeObservation] = Field(default_factory=list)
    structure: StructureSynthesis = Field(default_factory=StructureSynthesis)
    long: Scenario | None = None
    short: Scenario | None = None
    risk: RiskAssessment = Field(default_factory=RiskAssessment)

    key_levels: KeyLevels = Field(default_factory=KeyLevels)
    risks: list[str] = Field(default_factory=list)
    vision_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""

    charts: dict[str, str] = Field(
        default_factory=dict, description="Timeframe -> stored chart path, relative to the run."
    )
    traces: list[AgentTrace] = Field(default_factory=list)

    def observation(self, timeframe: str) -> TimeframeObservation | None:
        return next(
            (o for o in self.observations if o.timeframe.upper() == timeframe.upper()), None
        )

    def to_api(self) -> dict[str, Any]:
        """The flat shape documented in the README for external consumers."""
        return {
            "symbol": self.symbol,
            "run_id": self.metadata.run_id,
            "as_of": (self.metadata.as_of or self.metadata.created_at).isoformat(),
            "current_state": self.current_state.value,
            "market_regime": self.market_regime,
            "last_price": self.last_price,
            "opportunities": {
                "long": self.long.model_dump(mode="json") if self.long else None,
                "short": self.short.model_dump(mode="json") if self.short else None,
            },
            "key_levels": {
                "support": [level.price for level in self.key_levels.support],
                "resistance": [level.price for level in self.key_levels.resistance],
            },
            "alignment": self.structure.alignment.value,
            "confidence": self.vision_confidence,
            "risks": self.risks,
        }
