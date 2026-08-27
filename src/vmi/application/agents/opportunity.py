"""The Opportunity agent: where the intelligence of the system lives.

It never says "buy". It describes both sides of the market as conditional
scenarios — *what must remain true*, *what would make it live*, *what would prove
it wrong*, *where it is going* — and then names the one state a consumer should
be in right now. `NO_TRADE` and `WAIT` are ordinary outputs, not failures.

Prices come from the bars, not from prose. The vision agents supply the reading
(trend, structure, whether a pullback is happening); the arithmetic of an entry
zone, an invalidation and a reward/risk ratio is done here against the actual
price series, because a model asked to do that arithmetic will occasionally
place a stop above the entry on a long and be entirely confident about it.

Scoring is a weighted sum of four components, each in [0, 1] and each traceable
to a specific reading:

    alignment          do the timeframes agree with this direction?
    setup_confidence   does the setup timeframe see this setup, and how surely?
    entry_confirmation is the entry timeframe confirming right now?
    structure_quality  is the context strong and the regime the right sort?

The weights are in `configs/default.yaml`, so tuning them is a config change and
a replay, not a code change.
"""

from __future__ import annotations

from ...config import Config
from ...domain.models import (
    Alignment,
    ChartBundle,
    Direction,
    Evidence,
    MarketState,
    Regime,
    Scenario,
    SetupClass,
    StructureSynthesis,
    Targets,
    TimeframeObservation,
    Trend,
)
from ...domain.models.analysis import EvidenceKind
from .base import Agent, AgentResult

ALIGNMENT_SCORE = {
    Alignment.ALIGNED_BULLISH: {Direction.LONG: 1.00, Direction.SHORT: 0.10},
    Alignment.ALIGNED_BEARISH: {Direction.LONG: 0.10, Direction.SHORT: 1.00},
    Alignment.PARTIALLY_ALIGNED: {Direction.LONG: 0.60, Direction.SHORT: 0.60},
    Alignment.NEUTRAL: {Direction.LONG: 0.35, Direction.SHORT: 0.35},
    Alignment.CONFLICTING: {Direction.LONG: 0.20, Direction.SHORT: 0.20},
}

TREND_FOR = {Direction.LONG: Trend.BULLISH, Direction.SHORT: Trend.BEARISH}
SETUP_FOR = {Direction.LONG: SetupClass.LONG_SETUP, Direction.SHORT: SetupClass.SHORT_SETUP}

STRENGTH_SCORE = {"strong": 1.0, "moderate": 0.7, "weak": 0.4, "none": 0.15}
REGIME_SCORE = {
    Regime.TRENDING: 1.0,
    Regime.BREAKOUT: 0.85,
    Regime.CONSOLIDATION: 0.6,
    Regime.REVERSAL: 0.55,
    Regime.RANGING: 0.5,
    Regime.VOLATILE: 0.35,
    Regime.UNCLEAR: 0.3,
}

AMBIGUITY_BAND = 0.08
"""Two scenarios closer than this are not a choice, they are a coin toss. The
state becomes WAIT rather than picking the marginally higher one."""


class OpportunityAgent(Agent):
    """Both directions, always. One state, sometimes."""

    name = "opportunity_detection"

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.weights = config.opportunity

    def run(
        self,
        observations: list[TimeframeObservation],
        structure: StructureSynthesis,
        bundles: dict[str, ChartBundle],
    ) -> AgentResult[tuple[Scenario, Scenario, MarketState]]:
        with self._traced() as trace:
            by_role = {o.role: o for o in observations}
            reference = _reference_bundle(bundles, observations)

            long = self._scenario(Direction.LONG, by_role, structure, reference)
            short = self._scenario(Direction.SHORT, by_role, structure, reference)
            state = self._state(long, short, by_role.get("entry"), reference)

            self.log.info(
                "long=%.2f short=%.2f state=%s", long.score, short.score, state.value
            )
        return AgentResult(value=(long, short, state), trace=trace)

    # ------------------------------------------------------------------ score

    def _scenario(
        self,
        direction: Direction,
        by_role: dict[str, TimeframeObservation],
        structure: StructureSynthesis,
        bundle: ChartBundle | None,
    ) -> Scenario:
        context = by_role.get("context")
        setup = by_role.get("setup")
        entry = by_role.get("entry")
        wanted_trend = TREND_FOR[direction]

        alignment_component = ALIGNMENT_SCORE[structure.alignment][direction]

        if setup is None or setup.degraded:
            setup_component = 0.2
        elif setup.setup is SETUP_FOR[direction]:
            setup_component = 0.5 + 0.5 * setup.confidence
        elif setup.setup is SetupClass.NO_SETUP:
            setup_component = 0.35 if setup.trend is wanted_trend else 0.15
        else:
            setup_component = 0.05  # the setup timeframe sees the opposite trade

        entry_component = _entry_component(entry, direction)
        structure_component = _structure_component(context, direction)

        score = (
            self.weights.weight_alignment * alignment_component
            + self.weights.weight_setup_confidence * setup_component
            + self.weights.weight_entry_confirmation * entry_component
            + self.weights.weight_structure_quality * structure_component
        )
        if structure.alignment is Alignment.CONFLICTING:
            score -= self.weights.conflict_penalty
        score = round(min(max(score, 0.0), 1.0), 3)

        scenario = Scenario(
            direction=direction,
            setup_type=_setup_type(setup, entry, direction),
            score=score,
            quality=_quality(score, self.weights.min_score_to_watch),
            confidence=round(min(score * (0.5 + 0.5 * structure.confidence), 1.0), 3),
            supporting=_supporting(by_role, direction),
            conflicting=_conflicting(by_role, structure, direction),
        )
        _price_the_scenario(scenario, direction, structure, bundle, by_role)
        return scenario

    def _state(
        self,
        long: Scenario,
        short: Scenario,
        entry: TimeframeObservation | None,
        bundle: ChartBundle | None,
    ) -> MarketState:
        watch, trigger = self.weights.min_score_to_watch, self.weights.min_score_to_trigger
        best, other = (long, short) if long.score >= short.score else (short, long)

        if best.score < watch:
            return MarketState.NO_TRADE
        if other.score >= watch and abs(best.score - other.score) < AMBIGUITY_BAND:
            # Both sides are arguable. That is a market to watch, not to pick.
            return MarketState.WAIT

        confirmed = (
            entry is not None
            and not entry.degraded
            and entry.setup is SETUP_FOR[best.direction]
            and bool(entry.entry_confirmation)
            and not entry.entry_warning
        )
        # A scenario can only be *triggered* where it can actually be entered.
        # Price three percent above the pullback zone is a strong idea nobody can
        # act on yet, and calling that LONG_TRIGGERED would hand a consuming
        # system a fill it will never get.
        if best.score >= trigger and confirmed and _price_at_entry(best, bundle):
            return (
                MarketState.LONG_TRIGGERED
                if best.direction is Direction.LONG
                else MarketState.SHORT_TRIGGERED
            )
        return (
            MarketState.WATCH_LONG
            if best.direction is Direction.LONG
            else MarketState.WATCH_SHORT
        )


# --------------------------------------------------------------------- helpers


def _price_at_entry(scenario: Scenario, bundle: ChartBundle | None) -> bool:
    """Is the last price inside the entry zone, or within half an ATR of it?"""
    if bundle is None or not scenario.entry_zone:
        return False
    price = bundle.window.last_close
    low, high = min(scenario.entry_zone), max(scenario.entry_zone)
    atr = bundle.indicators.atr or (high - low) or 1e-9
    return (low - 0.5 * atr) <= price <= (high + 0.5 * atr)


def _reference_bundle(
    bundles: dict[str, ChartBundle], observations: list[TimeframeObservation]
) -> ChartBundle | None:
    """The chart every price in the report is measured against.

    The setup timeframe, because that is where the trade is framed; the entry
    chart moves too fast for an invalidation level and the context chart too
    slowly.
    """
    setup = next((o for o in observations if o.role == "setup"), None)
    if setup and setup.timeframe in bundles:
        return bundles[setup.timeframe]
    return next(iter(bundles.values()), None)


def _entry_component(entry: TimeframeObservation | None, direction: Direction) -> float:
    if entry is None or entry.degraded:
        return 0.3  # unknown, not bad: the trade is simply untimed
    if entry.setup is SETUP_FOR[direction]:
        base = 0.6 + 0.4 * entry.confidence
        return base * (0.6 if entry.entry_warning else 1.0)
    if entry.trend is TREND_FOR[direction]:
        return 0.5
    if entry.trend is Trend.SIDEWAYS or entry.trend is Trend.UNCLEAR:
        return 0.35
    return 0.2  # the entry chart is going the other way right now


def _structure_component(context: TimeframeObservation | None, direction: Direction) -> float:
    if context is None or context.degraded:
        return 0.3
    strength = STRENGTH_SCORE.get(context.trend_strength.value, 0.4)
    regime = REGIME_SCORE.get(context.regime, 0.4)
    if context.trend is TREND_FOR[direction]:
        return round(0.5 * strength + 0.5 * regime, 3)
    if context.trend in (Trend.SIDEWAYS, Trend.UNCLEAR):
        return round(0.35 * regime + 0.15, 3)
    # Trading against the context timeframe. Possible, and it starts from a hole.
    return round(0.2 * (1 - strength) + 0.1, 3)


def _quality(score: float, watch: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.58:
        return "medium"
    if score >= watch:
        return "low"
    return "none"


def _setup_type(
    setup: TimeframeObservation | None, entry: TimeframeObservation | None, direction: Direction
) -> str:
    if setup is None or setup.degraded:
        return "unread"
    if setup.regime is Regime.BREAKOUT:
        return "breakout"
    if setup.regime is Regime.RANGING:
        return "range rejection"
    if setup.regime is Regime.REVERSAL:
        return "reversal"
    counter_entry = entry is not None and entry.trend not in (TREND_FOR[direction], Trend.UNCLEAR)
    if setup.trend is TREND_FOR[direction]:
        return "pullback" if counter_entry else "trend continuation"
    return "counter-trend"


def _supporting(by_role: dict[str, TimeframeObservation], direction: Direction) -> list[Evidence]:
    wanted = TREND_FOR[direction]
    out: list[Evidence] = []
    for role in ("context", "setup", "entry"):
        observation = by_role.get(role)
        if observation is None or observation.degraded:
            continue
        if observation.trend is wanted:
            out.append(
                Evidence(
                    kind=EvidenceKind.OBSERVED,
                    statement=(
                        f"{observation.timeframe} reads {observation.trend.value} "
                        f"({observation.trend_strength.value})"
                    ),
                    source=f"{observation.timeframe} analyst",
                )
            )
        if observation.setup is SETUP_FOR[direction]:
            out.append(
                Evidence(
                    kind=EvidenceKind.INFERRED,
                    statement=(
                        f"{observation.timeframe} classifies the chart as "
                        f"{observation.setup.value} at confidence {observation.confidence:.2f}"
                    ),
                    source=f"{observation.timeframe} analyst",
                )
            )
        out.extend(
            item
            for item in observation.evidence[:2]
            if item.kind is EvidenceKind.OBSERVED and observation.trend is wanted
        )
    return out[:8]


def _conflicting(
    by_role: dict[str, TimeframeObservation], structure: StructureSynthesis, direction: Direction
) -> list[Evidence]:
    out = [
        Evidence(kind=EvidenceKind.OBSERVED, statement=note, source="structure synthesis")
        for note in structure.conflicts
    ]
    opposite = Trend.BEARISH if direction is Direction.LONG else Trend.BULLISH
    for observation in by_role.values():
        if observation.degraded:
            continue
        if observation.trend is opposite:
            out.append(
                Evidence(
                    kind=EvidenceKind.OBSERVED,
                    statement=f"{observation.timeframe} is {observation.trend.value}",
                    source=f"{observation.timeframe} analyst",
                )
            )
        if observation.entry_warning:
            out.append(
                Evidence(
                    kind=EvidenceKind.UNCERTAIN,
                    statement=observation.entry_warning,
                    source=f"{observation.timeframe} analyst",
                )
            )
    return out[:8]


def _price_the_scenario(
    scenario: Scenario,
    direction: Direction,
    structure: StructureSynthesis,
    bundle: ChartBundle | None,
    by_role: dict[str, TimeframeObservation],
) -> None:
    """Attach entry zone, trigger, invalidation and targets — in real prices.

    Everything here is anchored to a level that exists in the price series and
    sized in ATR, so the numbers mean the same thing on EURUSD and on Bitcoin.
    When there is no usable level the field stays empty and the text says why;
    that is the whole reason the fields are optional.
    """
    if bundle is None:
        scenario.condition = "no price series was available to frame this scenario"
        return

    price = bundle.window.last_close
    digits = bundle.window.digits
    atr = bundle.indicators.atr or (bundle.window.price_max - bundle.window.price_min) * 0.02
    timeframe = bundle.window.timeframe

    below = sorted([level for level in structure.support_zones if level < price], reverse=True)
    above = sorted([level for level in structure.resistance_zones if level > price])
    # Fall back to the chart's own levels when the synthesis found none.
    if not below:
        below = sorted(
            [level.price for level in bundle.levels if level.price < price], reverse=True
        )
    if not above:
        above = sorted([level.price for level in bundle.levels if level.price > price])

    fmt = lambda value: format(round(value, digits), f".{digits}f")  # noqa: E731

    if direction is Direction.LONG:
        anchor = below[0] if below else None
        target_levels = above[:2]
        if anchor is not None:
            scenario.entry_zone = [round(anchor, digits), round(anchor + 0.6 * atr, digits)]
            scenario.condition = f"{timeframe} holds above {fmt(anchor)}"
            scenario.invalidation_price = round(anchor - 0.5 * atr, digits)
            scenario.invalidation = (
                f"{timeframe} closes below {fmt(scenario.invalidation_price)} "
                f"(half an ATR under the support that defines the idea)"
            )
        scenario.trigger = (
            f"a lower-timeframe rejection of {fmt(anchor)} followed by a close back above "
            f"{fmt(anchor + 0.3 * atr)} with momentum turning up"
            if anchor is not None
            else "no support level could be read, so no trigger can be defined"
        )
        if not target_levels:
            target_levels = [price + 1.5 * atr, price + 2.5 * atr]
            scenario.targets = Targets(
                zones=[round(level, digits) for level in target_levels],
                rationale="no resistance was read from the charts; these are 1.5 and 2.5 ATR "
                "projections and should be treated as inferred, not observed",
            )
        else:
            scenario.targets = Targets(
                zones=[round(level, digits) for level in target_levels],
                rationale="the nearest resistance zones agreed on by the timeframes",
            )
    else:
        anchor = above[0] if above else None
        target_levels = below[:2]
        if anchor is not None:
            scenario.entry_zone = [round(anchor - 0.6 * atr, digits), round(anchor, digits)]
            scenario.condition = f"{timeframe} stays below {fmt(anchor)}"
            scenario.invalidation_price = round(anchor + 0.5 * atr, digits)
            scenario.invalidation = (
                f"{timeframe} closes above {fmt(scenario.invalidation_price)} "
                f"(half an ATR over the resistance that defines the idea)"
            )
        scenario.trigger = (
            f"a lower-timeframe rejection of {fmt(anchor)} followed by a close back below "
            f"{fmt(anchor - 0.3 * atr)} with momentum turning down"
            if anchor is not None
            else "no resistance level could be read, so no trigger can be defined"
        )
        if not target_levels:
            target_levels = [price - 1.5 * atr, price - 2.5 * atr]
            scenario.targets = Targets(
                zones=[round(level, digits) for level in target_levels],
                rationale="no support was read from the charts; these are 1.5 and 2.5 ATR "
                "projections and should be treated as inferred, not observed",
            )
        else:
            scenario.targets = Targets(
                zones=[round(level, digits) for level in target_levels],
                rationale="the nearest support zones agreed on by the timeframes",
            )

    if scenario.entry_zone and scenario.invalidation_price and scenario.targets.zones:
        mid = sum(scenario.entry_zone) / 2
        risk = abs(mid - scenario.invalidation_price)
        reward = abs(scenario.targets.zones[0] - mid)
        scenario.reward_risk = round(reward / risk, 2) if risk > 0 else None
