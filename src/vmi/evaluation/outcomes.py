"""What actually happened next.

A report says `WATCH_LONG` at a moment in history. This module goes and looks at
the bars after that moment and writes down what the price did — forward return at
several horizons, the furthest it ran in favour, the furthest it ran against, and
whether the scenario's own target or its own invalidation was touched first.

That last one is the honest test, because it uses the levels the system itself
published rather than a horizon someone chose afterwards to make the numbers look
better.

Nothing here is a backtest. There is no position sizing, no cost model and no
compounding — a forward return is not a P&L. What it can tell you is whether the
states mean anything: if `WATCH_LONG` and `WATCH_SHORT` have the same forward
return distribution, the system is not seeing what it claims to see.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd

from ..domain.models import Direction, MarketState, VisionReport

DIRECTIONAL = {
    MarketState.WATCH_LONG: 1,
    MarketState.LONG_TRIGGERED: 1,
    MarketState.WATCH_SHORT: -1,
    MarketState.SHORT_TRIGGERED: -1,
    MarketState.WAIT: 0,
    MarketState.NO_TRADE: 0,
}
"""The sign a state asserts. Used to turn a forward return into a signed one, so
a short that made money and a long that made money both count as correct."""


@dataclass
class Outcome:
    """One report, scored against the bars that followed it."""

    run_id: str
    symbol: str
    as_of: datetime
    state: str
    direction: int
    confidence: float
    long_score: float
    short_score: float
    entry_price: float
    bars_available: int
    forward_return: dict[int, float] = field(default_factory=dict)
    signed_return: dict[int, float] = field(default_factory=dict)
    max_favourable: float | None = None
    max_adverse: float | None = None
    target_hit: bool | None = None
    invalidation_hit: bool | None = None
    target_first: bool | None = None
    notes: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        row = asdict(self)
        row["as_of"] = self.as_of.isoformat()
        for horizon, value in self.forward_return.items():
            row[f"fwd_{horizon}"] = value
        for horizon, value in self.signed_return.items():
            row[f"signed_{horizon}"] = value
        row.pop("forward_return")
        row.pop("signed_return")
        row["notes"] = "; ".join(self.notes)
        return row


def score(
    report: VisionReport,
    future: pd.DataFrame,
    horizons: list[int] | None = None,
) -> Outcome:
    """Score one report against the bars that came after its `as_of`.

    *future* must be the entry-timeframe series **including** the bars after the
    cut-off — that is the one place in the system where looking ahead is the
    point rather than a bug.
    """
    horizons = horizons or [10, 20, 50]
    as_of = report.metadata.as_of or report.metadata.created_at
    cutoff = (
        pd.Timestamp(as_of).tz_convert("UTC") if as_of.tzinfo else pd.Timestamp(as_of, tz="UTC")
    )

    after = future[future.index > cutoff]
    entry_price = report.last_price or (
        float(future[future.index <= cutoff]["close"].iloc[-1]) if len(future) else 0.0
    )
    direction = DIRECTIONAL.get(report.current_state, 0)

    outcome = Outcome(
        run_id=report.metadata.run_id,
        symbol=report.symbol,
        as_of=as_of,
        state=report.current_state.value,
        direction=direction,
        confidence=report.vision_confidence,
        long_score=report.long.score if report.long else 0.0,
        short_score=report.short.score if report.short else 0.0,
        entry_price=entry_price,
        bars_available=len(after),
    )
    if after.empty or not entry_price:
        outcome.notes.append("no bars after the cut-off; nothing to score")
        return outcome

    for horizon in horizons:
        window = after.iloc[: min(horizon, len(after))]
        if len(window) < horizon:
            outcome.notes.append(f"only {len(window)} of {horizon} bars available")
        change = (float(window["close"].iloc[-1]) - entry_price) / entry_price
        outcome.forward_return[horizon] = round(change * 100, 4)
        outcome.signed_return[horizon] = round(change * 100 * (direction or 0), 4)

    longest = after.iloc[: max(horizons)]
    high = float(longest["high"].max())
    low = float(longest["low"].min())
    if direction >= 0:
        outcome.max_favourable = round((high - entry_price) / entry_price * 100, 4)
        outcome.max_adverse = round((low - entry_price) / entry_price * 100, 4)
    else:
        outcome.max_favourable = round((entry_price - low) / entry_price * 100, 4)
        outcome.max_adverse = round((entry_price - high) / entry_price * 100, 4)

    scenario = _acted_scenario(report)
    if scenario is not None and scenario.targets.zones and scenario.invalidation_price is not None:
        _touch_test(outcome, longest, scenario)
    else:
        outcome.notes.append("scenario had no priced target and stop, so no touch test")
    return outcome


def _acted_scenario(report: VisionReport):
    if report.current_state in (MarketState.WATCH_LONG, MarketState.LONG_TRIGGERED):
        return report.long
    if report.current_state in (MarketState.WATCH_SHORT, MarketState.SHORT_TRIGGERED):
        return report.short
    return None


def _touch_test(outcome: Outcome, bars: pd.DataFrame, scenario) -> None:
    """Which came first: the published target, or the published invalidation?

    Bar by bar, and when a single bar touches both, the invalidation wins. That
    is the pessimistic reading, and the right one — from an OHLC bar you cannot
    tell which side was hit first, and a scorer that guesses in its own favour
    is how a system talks itself into believing it works.
    """
    target = scenario.targets.zones[0]
    stop = scenario.invalidation_price
    is_long = scenario.direction is Direction.LONG

    for timestamp, bar in bars.iterrows():
        hit_target = bar["high"] >= target if is_long else bar["low"] <= target
        hit_stop = bar["low"] <= stop if is_long else bar["high"] >= stop
        if hit_stop:
            outcome.invalidation_hit = True
            outcome.target_hit = bool(hit_target)
            outcome.target_first = False
            outcome.notes.append(f"invalidation touched at {timestamp:%Y-%m-%d %H:%M}")
            return
        if hit_target:
            outcome.target_hit = True
            outcome.invalidation_hit = False
            outcome.target_first = True
            outcome.notes.append(f"first target touched at {timestamp:%Y-%m-%d %H:%M}")
            return
    outcome.target_hit = False
    outcome.invalidation_hit = False
    outcome.target_first = None
    outcome.notes.append("neither target nor invalidation was touched in the window")


def summarise(outcomes: list[Outcome], horizons: list[int] | None = None) -> pd.DataFrame:
    """One row per state: how many, and what happened after each.

    `signed_return` is the column that matters. A directional state whose mean
    signed return is negative is not merely useless, it is wrong in a way that a
    consumer could exploit by inverting it — which is worth knowing.
    """
    if not outcomes:
        return pd.DataFrame()
    horizons = horizons or sorted({h for outcome in outcomes for h in outcome.forward_return})
    frame = pd.DataFrame([outcome.as_row() for outcome in outcomes])

    aggregation: dict[str, tuple[str, str]] = {"runs": ("run_id", "count")}
    for horizon in horizons:
        if f"signed_{horizon}" in frame.columns:
            aggregation[f"mean_signed_{horizon}"] = (f"signed_{horizon}", "mean")
            aggregation[f"median_signed_{horizon}"] = (f"signed_{horizon}", "median")
    if "target_first" in frame.columns:
        aggregation["target_first_rate"] = ("target_first", "mean")
    if "confidence" in frame.columns:
        aggregation["mean_confidence"] = ("confidence", "mean")

    summary = frame.groupby("state").agg(**aggregation).round(4)
    summary["share"] = (summary["runs"] / summary["runs"].sum()).round(3)
    return summary.sort_values("runs", ascending=False)
