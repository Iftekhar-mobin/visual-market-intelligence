"""The Multi-Timeframe Structure agent: three readings, one view.

Deterministic on purpose. The rules it applies are the ones a desk would state
out loud — the higher timeframe carries more weight, a lower timeframe refines
rather than overturns, disagreement costs confidence — and written as code they
can be argued with, changed in one place, and replayed identically. A language
model asked to re-derive them on every run would give a slightly different
answer each time and no way to attribute the difference.

Weights come from the role, not the interval, so a ladder of D1/H4/M30 behaves
the same as H4/H1/M15 with no code change.
"""

from __future__ import annotations

from ...domain.models import Alignment, Regime, StructureSynthesis, TimeframeObservation, Trend
from .base import Agent, AgentResult

ROLE_WEIGHT = {"context": 0.50, "setup": 0.32, "entry": 0.18}
"""Higher timeframe structure dominates. The entry timeframe gets a real but
small vote: it is there to time an entry, not to call the market."""

DIRECTION = {Trend.BULLISH: 1.0, Trend.BEARISH: -1.0, Trend.SIDEWAYS: 0.0, Trend.UNCLEAR: 0.0}

CLUSTER_TOLERANCE = 0.0015
"""Fraction of price within which two levels from different timeframes are the
same zone. 15 basis points — tight enough to keep a level meaningful, loose
enough that an H4 and an M15 read of the same shelf merge."""


class StructureAgent(Agent):
    """Reconcile the ladder into one bias, one alignment and one set of zones."""

    name = "structure_synthesis"

    def run(self, observations: list[TimeframeObservation]) -> AgentResult[StructureSynthesis]:
        with self._traced() as trace:
            usable = [o for o in observations if not o.degraded]
            if not usable:
                trace.status = "failed"
                trace.error = "every timeframe reading was degraded"
                return AgentResult(
                    value=StructureSynthesis(
                        conflicts=["no usable timeframe reading"], confidence=0.0
                    ),
                    trace=trace,
                )

            weighted = [(o, ROLE_WEIGHT.get(o.role, 0.2) * max(o.confidence, 0.05)) for o in usable]
            score = sum(DIRECTION[o.trend] * weight for o, weight in weighted)
            total_weight = sum(weight for _, weight in weighted) or 1.0
            normalised = score / total_weight

            bias = (
                Trend.BULLISH
                if normalised > 0.25
                else Trend.BEARISH
                if normalised < -0.25
                else Trend.SIDEWAYS
            )
            alignment = _alignment(usable)
            dominant = max(weighted, key=lambda item: item[1])[0].timeframe

            context = next((o for o in usable if o.role == "context"), usable[0])
            regime = context.regime if context.regime != Regime.UNCLEAR else usable[0].regime

            momentum_values = {o.momentum for o in usable if o.momentum.value != "unclear"}
            volatility_values = {o.volatility for o in usable if o.volatility.value != "unclear"}

            synthesis = StructureSynthesis(
                alignment=alignment,
                dominant_timeframe=dominant,
                bias=bias,
                regime=regime,
                momentum_agreement=len(momentum_values) <= 1,
                volatility_agreement=len(volatility_values) <= 1,
                agreements=_agreements(usable),
                conflicts=_conflicts(observations),
                support_zones=_zones(usable, "support"),
                resistance_zones=_zones(usable, "resistance"),
                confidence=_confidence(weighted, alignment, len(observations) - len(usable)),
            )
            self.log.info(
                "alignment=%s bias=%s dominant=%s confidence=%.2f",
                synthesis.alignment.value,
                synthesis.bias.value,
                synthesis.dominant_timeframe,
                synthesis.confidence,
            )
        return AgentResult(value=synthesis, trace=trace)


def _alignment(observations: list[TimeframeObservation]) -> Alignment:
    by_role = {o.role: o for o in observations}
    context = by_role.get("context")
    setup = by_role.get("setup")
    entry = by_role.get("entry")

    directions = [o.trend for o in (context, setup, entry) if o is not None]
    decisive = [trend for trend in directions if trend in (Trend.BULLISH, Trend.BEARISH)]
    if not decisive:
        return Alignment.NEUTRAL

    bullish = sum(1 for trend in decisive if trend is Trend.BULLISH)
    bearish = len(decisive) - bullish

    complete = len(decisive) == len(directions)
    if bullish and not bearish:
        return Alignment.ALIGNED_BULLISH if complete else Alignment.PARTIALLY_ALIGNED
    if bearish and not bullish:
        return Alignment.ALIGNED_BEARISH if complete else Alignment.PARTIALLY_ALIGNED

    # Both directions present. Whether that is a conflict or merely a pullback
    # depends on where the disagreement is: context against setup is a real
    # conflict; the entry timeframe leaning the other way is what a pullback
    # looks like, and it is the reason to wait rather than to abandon the idea.
    if context is not None and setup is not None and context.trend != setup.trend:
        pair = (context.trend, setup.trend)
        if Trend.BULLISH in pair and Trend.BEARISH in pair:
            return Alignment.CONFLICTING
    return Alignment.PARTIALLY_ALIGNED


def _agreements(observations: list[TimeframeObservation]) -> list[str]:
    notes: list[str] = []
    trends = {o.timeframe: o.trend for o in observations}
    shared = {trend for trend in trends.values() if trend in (Trend.BULLISH, Trend.BEARISH)}
    if len(shared) == 1:
        direction = next(iter(shared)).value
        agreeing = [tf for tf, trend in trends.items() if trend.value == direction]
        notes.append(f"{' and '.join(agreeing)} both read {direction}")
    regimes = {o.regime for o in observations if o.regime != Regime.UNCLEAR}
    if len(regimes) == 1:
        notes.append(f"every timeframe reads the regime as {next(iter(regimes)).value}")
    return notes


def _conflicts(observations: list[TimeframeObservation]) -> list[str]:
    notes: list[str] = []
    by_role = {o.role: o for o in observations if not o.degraded}
    context, setup, entry = by_role.get("context"), by_role.get("setup"), by_role.get("entry")

    if context and setup and context.trend != setup.trend:
        notes.append(
            f"{context.timeframe} reads {context.trend.value} while "
            f"{setup.timeframe} reads {setup.trend.value}"
        )
    if setup and entry and entry.trend != setup.trend and entry.trend != Trend.SIDEWAYS:
        notes.append(
            f"{entry.timeframe} is counter to {setup.timeframe} "
            f"({entry.trend.value} against {setup.trend.value}) — a pullback, or a turn"
        )
    for observation in observations:
        if observation.degraded:
            notes.append(f"{observation.timeframe} produced no reading ({observation.error})")
        if observation.rejected_levels:
            notes.append(
                f"{observation.timeframe} reported prices outside the chart: "
                + ", ".join(str(price) for price in observation.rejected_levels)
            )
    return notes


def _zones(observations: list[TimeframeObservation], side: str) -> list[float]:
    """Merge levels seen on more than one timeframe; those are the ones that matter."""
    prices: list[tuple[float, str]] = []
    for observation in observations:
        for price in getattr(observation, side):
            prices.append((price, observation.timeframe))
    if not prices:
        return []

    prices.sort(key=lambda item: item[0])
    clusters: list[list[tuple[float, str]]] = [[prices[0]]]
    for price, timeframe in prices[1:]:
        centre = sum(item[0] for item in clusters[-1]) / len(clusters[-1])
        if abs(price - centre) <= abs(centre) * CLUSTER_TOLERANCE:
            clusters[-1].append((price, timeframe))
        else:
            clusters.append([(price, timeframe)])

    scored = [
        (
            round(sum(item[0] for item in cluster) / len(cluster), 8),
            len({item[1] for item in cluster}),
        )
        for cluster in clusters
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [price for price, _ in scored[:4]]


def _confidence(
    weighted: list[tuple[TimeframeObservation, float]], alignment: Alignment, degraded: int
) -> float:
    total = sum(weight for _, weight in weighted) or 1.0
    base = sum(o.confidence * weight for o, weight in weighted) / total
    factor = {
        Alignment.ALIGNED_BULLISH: 1.0,
        Alignment.ALIGNED_BEARISH: 1.0,
        Alignment.PARTIALLY_ALIGNED: 0.8,
        Alignment.NEUTRAL: 0.6,
        Alignment.CONFLICTING: 0.45,
    }[alignment]
    # Each missing timeframe costs a fifth: the picture is genuinely less complete.
    return round(min(max(base * factor * (1 - 0.2 * degraded), 0.0), 1.0), 3)
