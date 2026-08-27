"""The Risk / Invalidation agent: the one that is allowed to say no.

Its question is not "will this work" — nothing here can answer that — but "is
this idea *actionable and falsifiable*". An opportunity with no invalidation
level is not a trade, it is an opinion; an opportunity whose first target is
nearer than its stop is a losing proposition however good the chart looks.

It holds a veto. When it uses one, the state becomes `NO_TRADE` and the reason
is recorded, so the report says which rule fired rather than quietly returning a
different answer.
"""

from __future__ import annotations

from ...config import Config
from ...domain.models import (
    Alignment,
    ChartBundle,
    Direction,
    MarketState,
    RiskAssessment,
    Scenario,
    StructureSynthesis,
    TimeframeObservation,
)
from .base import Agent, AgentResult

MIN_REWARD_RISK = 0.8
"""Below this the first target is barely further than the stop. Not a rule about
what wins — a rule about what is worth the spread."""

VOLATILITY_BANDS = ((1.5, "high"), (0.8, "elevated"), (0.0, "normal"))
"""ATR as a percentage of price, on the entry timeframe."""


class RiskAgent(Agent):
    """Judge the opportunity's structure, not its direction."""

    name = "risk_invalidation"

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def run(
        self,
        long: Scenario,
        short: Scenario,
        state: MarketState,
        structure: StructureSynthesis,
        observations: list[TimeframeObservation],
        bundles: dict[str, ChartBundle],
    ) -> AgentResult[tuple[RiskAssessment, MarketState]]:
        with self._traced() as trace:
            best = long if long.score >= short.score else short
            entry_bundle = _entry_bundle(bundles, observations)

            assessment = RiskAssessment(
                has_clear_invalidation=best.invalidation_price is not None,
                volatility_risk=_volatility(entry_bundle),
                structural_risks=_structural(best, structure, entry_bundle),
                conflicting_signals=list(structure.conflicts),
                uncertainty=_uncertainty(observations, structure),
                reward_risk_note=_reward_risk_note(best),
            )

            veto_reason = self._veto(best, assessment, observations, state)
            if veto_reason:
                assessment.veto = True
                assessment.veto_reason = veto_reason
                state = MarketState.NO_TRADE

            assessment.risks = _risk_lines(assessment, best, observations)
            self.log.info(
                "risk: invalidation=%s volatility=%s veto=%s",
                assessment.has_clear_invalidation,
                assessment.volatility_risk,
                assessment.veto,
            )
        return AgentResult(value=(assessment, state), trace=trace)

    def _veto(
        self,
        best: Scenario,
        assessment: RiskAssessment,
        observations: list[TimeframeObservation],
        state: MarketState,
    ) -> str | None:
        if state is MarketState.NO_TRADE:
            return None  # nothing to veto; the opportunity agent already declined
        if not assessment.has_clear_invalidation:
            return (
                "no invalidation level could be read from the charts, so the idea "
                "cannot be proved wrong at a price"
            )
        if best.reward_risk is not None and best.reward_risk < MIN_REWARD_RISK:
            return (
                f"the first target is {best.reward_risk:.2f}x the risk, below the "
                f"{MIN_REWARD_RISK:.2f}x floor"
            )
        degraded = sum(1 for observation in observations if observation.degraded)
        if degraded >= 2:
            return f"{degraded} of {len(observations)} timeframes produced no reading"
        return None


def _entry_bundle(
    bundles: dict[str, ChartBundle], observations: list[TimeframeObservation]
) -> ChartBundle | None:
    entry = next((o for o in observations if o.role == "entry"), None)
    if entry and entry.timeframe in bundles:
        return bundles[entry.timeframe]
    return next(iter(bundles.values()), None)


def _volatility(bundle: ChartBundle | None) -> str:
    if bundle is None or bundle.indicators.atr_pct is None:
        return "unknown"
    atr_pct = bundle.indicators.atr_pct
    for threshold, label in VOLATILITY_BANDS:
        if atr_pct >= threshold:
            return f"{label} (ATR is {atr_pct:.2f}% of price)"
    return "unknown"


def _structural(
    best: Scenario, structure: StructureSynthesis, bundle: ChartBundle | None
) -> list[str]:
    risks: list[str] = []
    if structure.alignment is Alignment.CONFLICTING:
        risks.append("the context and setup timeframes disagree on direction")
    if best.quality == "none":
        risks.append("neither direction scored well enough to be worth watching")
    if not best.entry_zone:
        risks.append("no entry zone could be framed from the levels read")

    if bundle is not None and best.entry_zone and best.targets.zones:
        atr = bundle.indicators.atr or 0.0
        distance = abs(best.targets.zones[0] - sum(best.entry_zone) / 2)
        if atr and distance < atr:
            risks.append(
                f"the first target is under one ATR away ({distance:.5g} against "
                f"an ATR of {atr:.5g}); noise alone covers that distance"
            )
    if "projections" in (best.targets.rationale or ""):
        risks.append("targets are ATR projections, not levels anyone can see on the chart")
    return risks


def _uncertainty(
    observations: list[TimeframeObservation], structure: StructureSynthesis
) -> str:
    degraded = sum(1 for observation in observations if observation.degraded)
    unread = sum(len(observation.uncertainties) for observation in observations)
    if degraded or structure.confidence < 0.35:
        return "high"
    if unread > 3 or structure.confidence < 0.55:
        return "moderate"
    return "low"


def _reward_risk_note(best: Scenario) -> str | None:
    if best.reward_risk is None:
        return "reward/risk could not be computed: the scenario has no priced entry or stop"
    return (
        f"first target is {best.reward_risk:.2f}x the distance to invalidation, "
        f"measured from the middle of the entry zone"
    )


def _risk_lines(
    assessment: RiskAssessment, best: Scenario, observations: list[TimeframeObservation]
) -> list[str]:
    """The flat list the API returns under `risks` — everything worth saying once."""
    lines = list(assessment.structural_risks)
    lines.extend(assessment.conflicting_signals)
    if assessment.volatility_risk.startswith(("high", "elevated")):
        lines.append(f"volatility is {assessment.volatility_risk}; stops sized for calm will fail")
    if assessment.uncertainty == "high":
        lines.append("the visual reading is weak or incomplete; treat every level as provisional")
    for observation in observations:
        if observation.rejected_levels:
            lines.append(
                f"{observation.timeframe} reported prices outside the chart it was shown "
                f"({', '.join(str(price) for price in observation.rejected_levels)}); "
                "those were discarded"
            )
    if assessment.veto and assessment.veto_reason:
        lines.append(f"state forced to NO_TRADE: {assessment.veto_reason}")
    if best.direction is Direction.LONG and best.quality in ("high", "medium"):
        lines.append(
            "this is a visual reading only; nothing here accounts for news or the calendar"
        )
    # Preserve order, drop repeats: several agents legitimately raise the same point.
    seen: set[str] = set()
    return [line for line in lines if not (line in seen or seen.add(line))]
