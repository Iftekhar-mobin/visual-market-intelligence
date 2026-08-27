"""The Final Report agent: assemble, score, and say it in one paragraph.

Nothing here decides anything. It gathers what the other agents concluded, works
out the headline confidence, promotes the agreed levels to `key_levels`, and
writes a summary a person can read in ten seconds without scrolling through the
JSON. The summary is composed from the structured fields rather than generated,
so it can never say something the data does not.
"""

from __future__ import annotations

from datetime import datetime

from ...domain.models import (
    ChartBundle,
    Direction,
    KeyLevels,
    Level,
    LevelKind,
    MarketState,
    RiskAssessment,
    RunMetadata,
    Scenario,
    StructureSynthesis,
    TimeframeObservation,
    VisionReport,
)
from .base import Agent, AgentResult

STATE_SENTENCE = {
    MarketState.NO_TRADE: "There is no opportunity worth acting on here.",
    MarketState.WAIT: "Both directions are arguable, so the honest answer is to wait.",
    MarketState.WATCH_LONG: "Watch for a long; it is not live yet.",
    MarketState.WATCH_SHORT: "Watch for a short; it is not live yet.",
    MarketState.LONG_TRIGGERED: "A long is triggered on the conditions below.",
    MarketState.SHORT_TRIGGERED: "A short is triggered on the conditions below.",
}


class ReportAgent(Agent):
    """One report, one confidence, one paragraph."""

    name = "final_report"

    def run(
        self,
        *,
        metadata: RunMetadata,
        observations: list[TimeframeObservation],
        structure: StructureSynthesis,
        long: Scenario,
        short: Scenario,
        state: MarketState,
        risk: RiskAssessment,
        bundles: dict[str, ChartBundle],
    ) -> AgentResult[VisionReport]:
        with self._traced() as trace:
            best = long if long.score >= short.score else short
            reference = next(iter(bundles.values()), None)
            confidence = _confidence(structure, best, risk, observations)

            report = VisionReport(
                metadata=metadata,
                symbol=metadata.symbol,
                last_price=reference.window.last_close if reference else None,
                current_state=state,
                market_regime=_regime_label(structure),
                observations=observations,
                structure=structure,
                long=long,
                short=short,
                risk=risk,
                key_levels=_key_levels(structure, bundles),
                risks=risk.risks,
                vision_confidence=confidence,
                charts=dict.fromkeys(bundles, ""),
            )
            report.summary = _summary(report, best)
            self.log.info("report: state=%s confidence=%.2f", state.value, confidence)
        return AgentResult(value=report, trace=trace)


def _confidence(
    structure: StructureSynthesis,
    best: Scenario,
    risk: RiskAssessment,
    observations: list[TimeframeObservation],
) -> float:
    """How much the system believes its own picture.

    Two thirds of it is whether the timeframes agreed and how sure each analyst
    was; one third is the quality of the best scenario. A veto caps it, because
    an idea the risk agent rejected should never be reported at high confidence
    whatever the charts looked like.
    """
    base = 0.65 * structure.confidence + 0.35 * best.confidence
    degraded = sum(1 for observation in observations if observation.degraded)
    base *= 1 - 0.25 * degraded
    if risk.veto:
        base = min(base, 0.35)
    if risk.uncertainty == "high":
        base *= 0.8
    return round(min(max(base, 0.0), 1.0), 3)


def _regime_label(structure: StructureSynthesis) -> str:
    bias = structure.bias.value
    regime = structure.regime.value
    if regime in ("ranging", "consolidation"):
        return regime.upper()
    if bias in ("bullish", "bearish"):
        return f"{bias.upper()}_{regime.upper()}"
    return regime.upper()


def _key_levels(structure: StructureSynthesis, bundles: dict[str, ChartBundle]) -> KeyLevels:
    """Zones the timeframes agreed on, falling back to the computed ones.

    Ordered by distance from the market rather than by how many timeframes saw
    them: the nearest support is the one that matters next, whoever spotted it.
    """
    support = [Level(price=price, kind=LevelKind.SUPPORT) for price in structure.support_zones]
    resistance = [
        Level(price=price, kind=LevelKind.RESISTANCE) for price in structure.resistance_zones
    ]
    if not support or not resistance:
        for bundle in bundles.values():
            for level in bundle.levels:
                if level.kind is LevelKind.SUPPORT and not support:
                    support.append(level)
                elif level.kind is LevelKind.RESISTANCE and not resistance:
                    resistance.append(level)
    support.sort(key=lambda level: -level.price)
    resistance.sort(key=lambda level: level.price)
    return KeyLevels(support=support[:4], resistance=resistance[:4])


def _summary(report: VisionReport, best: Scenario) -> str:
    structure = report.structure
    parts = [
        f"{report.symbol}: {structure.alignment.value.replace('_', ' ').lower()} across "
        f"{len(report.observations)} timeframes, dominant read from "
        f"{structure.dominant_timeframe or 'no timeframe'}.",
        STATE_SENTENCE.get(report.current_state, ""),
    ]

    if best.quality != "none" and not report.risk.veto:
        direction = "long" if best.direction is Direction.LONG else "short"
        parts.append(
            f"The {direction} case scores {best.score:.2f} ({best.quality} quality, "
            f"{best.setup_type})."
        )
        if best.condition:
            parts.append(f"It holds while {best.condition}.")
        if best.trigger:
            parts.append(f"It becomes live on {best.trigger}.")
        if best.invalidation:
            parts.append(f"It is wrong if {best.invalidation}.")
        if best.reward_risk:
            parts.append(f"First target is {best.reward_risk:.2f}x the risk.")
    elif report.risk.veto_reason:
        parts.append(f"The risk agent vetoed it: {report.risk.veto_reason}.")

    if report.risks:
        parts.append(f"Main risk: {report.risks[0]}.")
    parts.append(
        f"Visual confidence {report.vision_confidence:.2f}. "
        "This is a reading of charts, not a forecast."
    )
    return " ".join(part for part in parts if part)


def build_metadata(
    *,
    run_id: str,
    symbol: str,
    as_of: datetime | None,
    provider: str,
    model: str,
    chart_version: str,
    timeframes: list[str],
    data_provider: str,
    config_digest: str,
) -> RunMetadata:
    return RunMetadata(
        run_id=run_id,
        symbol=symbol,
        as_of=as_of,
        provider=provider,
        model=model,
        chart_version=chart_version,
        timeframes=timeframes,
        data_provider=data_provider,
        config_digest=config_digest,
    )
