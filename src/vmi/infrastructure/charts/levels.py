"""Support and resistance, computed from the bars rather than eyeballed.

The vision model is asked to read levels off the picture, and it will sometimes
be wrong. These levels are the second opinion: they are drawn on the chart (so
the model has something to anchor to), and they are what the report falls back
on when a model-reported price lands outside the range the chart covers.

The method is deliberately dull:

1. Find swing pivots — a bar whose high is the highest of the `k` bars either
   side, or whose low is the lowest.
2. Cluster pivots that sit within a fraction of ATR of each other.
3. Score a cluster by how many pivots formed it and how recently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ...domain.models import Level, LevelKind

CLUSTER_ATR_FRACTION = 0.75
"""Two pivots closer than this many ATRs are the same zone. Wider merges a
support and a resistance into one meaningless average; narrower reports every
wick as its own level."""

MAX_LEVELS_PER_SIDE = 3
"""More lines than this and their labels overlap, which defeats the purpose of
drawing them: a level a model cannot read is worse than no level."""


def swing_points(frame: pd.DataFrame, k: int = 5) -> tuple[list[float], list[float]]:
    """(highs, lows) of the confirmed pivots in *frame*, oldest first.

    A pivot needs `k` bars on both sides, so the last `k` bars can never be one.
    That lag is not a defect — an unconfirmed pivot is a guess about the future.
    """
    highs, lows = [], []
    high, low = frame["high"].to_numpy(), frame["low"].to_numpy()
    for i in range(k, len(frame) - k):
        window_high = high[i - k : i + k + 1]
        window_low = low[i - k : i + k + 1]
        if high[i] == window_high.max() and (window_high.argmax() == k):
            highs.append(float(high[i]))
        if low[i] == window_low.min() and (window_low.argmin() == k):
            lows.append(float(low[i]))
    return highs, lows


def _cluster(prices: list[float], tolerance: float) -> list[tuple[float, int]]:
    """Group nearby prices into (centre, member count), strongest first."""
    if not prices:
        return []
    ordered = sorted(prices)
    groups: list[list[float]] = [[ordered[0]]]
    for price in ordered[1:]:
        if abs(price - np.mean(groups[-1])) <= tolerance:
            groups[-1].append(price)
        else:
            groups.append([price])
    clustered = [(float(np.mean(group)), len(group)) for group in groups]
    return sorted(clustered, key=lambda item: (-item[1], item[0]))


def detect_levels(frame: pd.DataFrame, k: int = 5, atr_value: float | None = None) -> list[Level]:
    """Support below the last close, resistance above it, at most four each."""
    if len(frame) < 2 * k + 2:
        return []

    highs, lows = swing_points(frame, k)
    last_close = float(frame["close"].iloc[-1])
    span = float(frame["high"].max() - frame["low"].min())
    tolerance = (atr_value or span * 0.02) * CLUSTER_ATR_FRACTION
    if not np.isfinite(tolerance) or tolerance <= 0:
        tolerance = max(span * 0.01, 1e-9)

    levels: list[Level] = []
    for prices, kind in ((lows, LevelKind.SUPPORT), (highs, LevelKind.RESISTANCE)):
        clusters = _cluster(prices, tolerance)
        # Support sits under price and resistance over it; a "support" above the
        # market is a broken level, and labelling it support would mislead.
        side = [
            (price, count)
            for price, count in clusters
            if (price <= last_close if kind is LevelKind.SUPPORT else price >= last_close)
        ]
        strongest = max((count for _, count in side), default=1)
        for price, count in side[:MAX_LEVELS_PER_SIDE]:
            levels.append(
                Level(
                    price=round(price, 8),
                    kind=kind,
                    touches=count,
                    strength=round(min(count / max(strongest, 1), 1.0), 3),
                )
            )
    return levels
