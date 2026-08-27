"""Shared machinery for every price feed: the canonical frame, resampling,
lookback arithmetic, the `as_of` cut and an on-disk cache.

Providers differ in what they can serve natively. Yahoo has no 4-hour bars;
MetaTrader has them but only for symbols the terminal carries. Rather than let
that leak into the agents, every provider returns the same thing — a UTC-indexed
frame with `open, high, low, close, volume`, oldest first — and this module owns
the conversions that make that possible.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ...logging_utils import get_logger

log = get_logger("data")

CANONICAL_COLUMNS = ["open", "high", "low", "close", "volume"]

_DURATION = re.compile(r"^(\d+)\s*([mhdwy])$", re.IGNORECASE)
_INTERVAL = re.compile(r"^(\d+)\s*(m|min|h|d|wk|w|mo)$", re.IGNORECASE)

_UNIT_SECONDS = {
    "m": 60,
    "min": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "wk": 604800,
    "y": 31_536_000,
    "mo": 2_592_000,
}


def parse_duration(value: str) -> timedelta:
    """`"180d"` → 180 days. Used for lookbacks, which are always coarse."""
    match = _DURATION.match(value.strip())
    if not match:
        raise ValueError(f"cannot read lookback {value!r}; expected forms like '60d' or '12h'")
    amount, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])


def interval_seconds(interval: str) -> int:
    """`"4h"` → 14400. The one place bar length is defined."""
    match = _INTERVAL.match(interval.strip())
    if not match:
        raise ValueError(f"cannot read interval {interval!r}; expected forms like '15m' or '4h'")
    amount, unit = int(match.group(1)), match.group(2).lower()
    return amount * _UNIT_SECONDS[unit]


def to_utc_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Force a tz-aware UTC DatetimeIndex, sorted, without duplicates."""
    out = frame.copy()
    index = pd.DatetimeIndex(out.index)
    out.index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


def canonicalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Lower-cased OHLCV columns, numeric, with unusable rows dropped."""
    out = frame.copy()
    out.columns = [str(column).split(" ")[0].strip().lower() for column in out.columns]
    if "adj close" in out.columns and "close" not in out.columns:
        out = out.rename(columns={"adj close": "close"})
    missing = [column for column in ("open", "high", "low", "close") if column not in out.columns]
    if missing:
        raise ValueError(f"feed returned no {missing} column; got {list(out.columns)}")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out = out[CANONICAL_COLUMNS].apply(pd.to_numeric, errors="coerce")
    return to_utc_index(out).dropna(subset=["open", "high", "low", "close"])


def resample(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Aggregate to a longer bar — how 4-hour candles exist on a feed without them.

    Right-labelled, right-closed: the bar stamped 12:00 covers 08:00–12:00 and
    is only complete once 12:00 has passed. Left-labelling it would place
    information a few hours before it existed, which is the quiet way a replay
    starts leaking the future.
    """
    rule = f"{interval_seconds(interval)}s"
    aggregated = frame.resample(rule, label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return aggregated.dropna(subset=["open", "high", "low", "close"])


def apply_as_of(frame: pd.DataFrame, as_of: datetime | None, interval: str) -> pd.DataFrame:
    """Drop everything after *as_of*, and the bar *as_of* falls inside.

    A bar that closes after the cut-off contains prices from after the cut-off.
    Keeping it would hand the analyst a few hours of the future, and every
    replay result built on it would be worthless. This is the single most
    important line in the data layer.
    """
    if as_of is None:
        return frame
    cutoff = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    cutoff = pd.Timestamp(cutoff).tz_convert("UTC")
    seconds = interval_seconds(interval)
    # Bars are right-stamped, so a bar stamped exactly at the cut-off closed then
    # and is legal; anything later is not.
    kept = frame[frame.index <= cutoff]
    if not kept.empty and (kept.index[-1] - cutoff).total_seconds() > 0:
        kept = kept.iloc[:-1]
    log.debug("as_of %s kept %d of %d bars (%ds)", cutoff, len(kept), len(frame), seconds)
    return kept


class FrameCache:
    """A dull CSV cache. Free feeds rate-limit, and a chart is redrawn often."""

    def __init__(self, directory: Path, ttl_s: int = 900) -> None:
        self.directory = directory
        self.ttl_s = ttl_s
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
        return self.directory / f"{safe}.csv"

    def get(self, key: str) -> pd.DataFrame | None:
        path = self._path(key)
        if not path.exists() or (time.time() - path.stat().st_mtime) > self.ttl_s:
            return None
        try:
            frame = pd.read_csv(path, index_col=0)
            # Parsed explicitly rather than with `parse_dates`: a CSV written
            # from a tz-aware index comes back as offset strings, which pandas
            # will not convert without being told they are all UTC.
            frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")
            frame = frame[frame.index.notna()]
        except (OSError, ValueError):  # a half-written file is not worth a crash
            return None
        return to_utc_index(frame)

    def put(self, key: str, frame: pd.DataFrame) -> None:
        try:
            frame.to_csv(self._path(key))
        except OSError as exc:
            log.warning("could not cache %s: %s", key, exc)


class DataUnavailable(RuntimeError):
    """The feed answered, and the answer was 'nothing'. Callers report it as-is."""
