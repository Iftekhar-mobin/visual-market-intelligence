"""CSV files on disk — the reproducible feed.

A study that has to be defended a year from now cannot depend on a free web API
still answering the same way. Drop the exported bars in `data/samples` (or point
`VMI_DATA__CACHE_DIR` elsewhere) named

    SYMBOL_TIMEFRAME.csv          e.g. EURUSD_H1.csv
    SYMBOL_TIMEFRAME_START_END.csv

and this provider serves them with the same `as_of` guarantees as any live feed.
Any column layout with a parseable timestamp and OHLC works; the MetaTrader
exporter's `<DATE> <TIME> <OPEN>…` header is handled specially because it is the
one most people already have.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from ...logging_utils import get_logger
from .base import DataUnavailable, apply_as_of, canonicalise, interval_seconds, resample

log = get_logger("data.csv")

SECONDS_TO_MT_NAME = {
    60: "M1",
    300: "M5",
    900: "M15",
    1800: "M30",
    3600: "H1",
    14400: "H4",
    86400: "D1",
}


class CsvProvider:
    """The `MarketDataProvider` port, over a directory of exports."""

    name = "csv"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def catalogue(self) -> list[dict[str, str]]:
        """Every file this provider can serve, for the console's picker."""
        entries = []
        for path in sorted(self.directory.glob("*.csv")):
            parts = path.stem.split("_")
            entries.append(
                {
                    "symbol": parts[0] if parts else path.stem,
                    "timeframe": parts[1] if len(parts) > 1 else "",
                    "path": str(path),
                }
            )
        return entries

    def _find(self, symbol: str, interval: str) -> Path:
        name = SECONDS_TO_MT_NAME.get(interval_seconds(interval), interval)
        patterns = [
            f"{symbol}_{name}*.csv",
            f"{symbol}_{interval}*.csv",
            f"{symbol.lower()}_{name.lower()}*.csv",
            f"{symbol}*.csv",
        ]
        for pattern in patterns:
            matches = sorted(self.directory.glob(pattern))
            if matches:
                return matches[-1]
        raise DataUnavailable(
            f"no CSV for {symbol} {interval} in {self.directory}. "
            f"Expected a file named like {symbol}_{name}.csv"
        )

    def fetch(
        self,
        symbol: str,
        interval: str,
        lookback: str,
        as_of: datetime | None = None,
    ) -> pd.DataFrame:
        path = self._find(symbol, interval)
        frame = _read(path)
        native = _infer_interval(frame)
        wanted = interval_seconds(interval)
        if native and wanted > native and wanted % native == 0:
            frame = resample(frame, interval)
        elif native and wanted < native:
            raise DataUnavailable(
                f"{path.name} holds {native}s bars; {interval} cannot be built from it"
            )
        frame = apply_as_of(frame, as_of, interval)
        if frame.empty:
            raise DataUnavailable(f"{path.name} has no bars at or before {as_of}")
        log.info("csv %s %s: %d bars from %s", symbol, interval, len(frame), path.name)
        return frame


def _read(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, sep=None, engine="python")
    frame.columns = [re.sub(r"[<>]", "", str(column)).strip().lower() for column in frame.columns]

    if "date" in frame.columns and "time" in frame.columns:
        stamp = pd.to_datetime(
            frame["date"].astype(str) + " " + frame["time"].astype(str), utc=True, errors="coerce"
        )
        frame = frame.drop(columns=["date", "time"])
    else:
        candidates = [c for c in ("datetime", "date", "time", "timestamp") if c in frame.columns]
        if not candidates:
            raise DataUnavailable(f"{path.name} has no timestamp column")
        stamp = pd.to_datetime(frame[candidates[0]], utc=True, errors="coerce")
        frame = frame.drop(columns=[candidates[0]])

    frame.index = stamp
    frame = frame[frame.index.notna()]
    if "tickvol" in frame.columns and "volume" not in frame.columns:
        frame = frame.rename(columns={"tickvol": "volume"})
    return canonicalise(frame)


def _infer_interval(frame: pd.DataFrame) -> int | None:
    """Bar length in seconds, from the most common gap between rows."""
    if len(frame) < 3:
        return None
    gaps = frame.index.to_series().diff().dropna().dt.total_seconds()
    return int(gaps.mode().iloc[0]) if not gaps.empty else None
