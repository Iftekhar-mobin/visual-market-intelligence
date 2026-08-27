"""Yahoo Finance — the default feed, because it is free and needs no account.

What it can and cannot do shapes the timeframe ladder:

* intraday history is capped (roughly 60 days of 15-minute bars, 730 of hourly),
  so the M15 chart looks back days and the H4 chart looks back months;
* there are no 4-hour bars at all, so H4 is resampled from H1 here rather than
  being quietly dropped.

Symbols are given in the plain form a person would type — `EURUSD`, `BTCUSD`,
`AAPL` — and translated to Yahoo's spelling on the way out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from ...logging_utils import get_logger
from .base import (
    DataUnavailable,
    FrameCache,
    apply_as_of,
    canonicalise,
    interval_seconds,
    parse_duration,
    resample,
)

log = get_logger("data.yahoo")

NATIVE_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo"}
"""What yfinance accepts. Anything else is built by resampling."""

MAX_INTRADAY_DAYS = {
    "1m": 7,
    "2m": 59,
    "5m": 59,
    "15m": 59,
    "30m": 59,
    "60m": 729,
    "90m": 59,
    "1h": 729,
}
"""Yahoo's history limits. Asking for more returns an empty frame with a
warning, which looks exactly like a bad symbol — so the request is clamped."""

FX_MAJORS = {
    "EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "SEK", "NOK", "MXN", "ZAR", "TRY",
}
CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "LTC", "AVAX", "DOT", "MATIC"}

EXPLICIT = {
    "XAUUSD": "GC=F",   # spot gold has no free feed; the front future is the usual stand-in
    "XAGUSD": "SI=F",
    "GOLD": "GC=F",
    "WTI": "CL=F",
    "BRENT": "BZ=F",
    "US30": "^DJI",
    "SPX500": "^GSPC",
    "SPX": "^GSPC",
    "NAS100": "^NDX",
    "GER40": "^GDAXI",
    "UK100": "^FTSE",
    "VIX": "^VIX",
}


def to_yahoo_symbol(symbol: str) -> str:
    """`EURUSD` → `EURUSD=X`, `BTCUSD` → `BTC-USD`, `AAPL` → `AAPL`.

    Anything already carrying Yahoo punctuation (`=X`, `-USD`, `^`) is passed
    through untouched, so an exact ticker always wins over the guess.
    """
    raw = symbol.strip().upper()
    if not raw:
        raise ValueError("empty symbol")
    if raw in EXPLICIT:
        return EXPLICIT[raw]
    if any(token in raw for token in ("=", "-", "^", ".")):
        return raw
    if len(raw) == 6 and raw[:3] in FX_MAJORS and raw[3:] in FX_MAJORS:
        return raw + "=X"
    for base in CRYPTO_BASES:
        if raw.startswith(base) and raw[len(base) :] in {"USD", "USDT", "EUR"}:
            return f"{base}-{raw[len(base):].replace('USDT', 'USD')}"
    return raw


def _base_interval(interval: str) -> str:
    """The native interval a non-native one is aggregated from."""
    if interval in NATIVE_INTERVALS:
        return interval
    seconds = interval_seconds(interval)
    if seconds % 3600 == 0 and seconds > 3600:
        return "1h"
    if seconds % 60 == 0 and seconds < 3600:
        return "5m"
    return "1d"


class YahooProvider:
    """The `MarketDataProvider` port, over yfinance."""

    name = "yahoo"

    def __init__(self, cache: FrameCache | None = None) -> None:
        self._cache = cache

    def fetch(
        self,
        symbol: str,
        interval: str,
        lookback: str,
        as_of: datetime | None = None,
    ) -> pd.DataFrame:
        ticker = to_yahoo_symbol(symbol)
        base = _base_interval(interval)
        span = parse_duration(lookback)
        # Aggregating to a longer bar throws bars away, and an EMA200 on the
        # resampled series needs 200 of them — so pull generously and cut later.
        if base != interval:
            span *= max(interval_seconds(interval) // interval_seconds(base), 1)
        span += timedelta(days=3)  # weekends and holidays are not trading days
        days = max(int(span.total_seconds() // 86400) + 1, 2)
        days = min(days, MAX_INTRADAY_DAYS.get(base, 3650))

        end = as_of or datetime.now(timezone.utc)
        start = end - timedelta(days=days)

        # Yahoo's intraday limit is measured from *today*, not from the end of
        # the requested window, so a replay cursor six months back still cannot
        # reach further than 729 days from now. Clamp rather than send a request
        # that comes back empty and looks like a bad symbol.
        horizon = MAX_INTRADAY_DAYS.get(base)
        if horizon is not None:
            earliest = datetime.now(timezone.utc) - timedelta(days=horizon - 1)
            if end < earliest:
                raise DataUnavailable(
                    f"Yahoo keeps only {horizon} days of {base} bars, so {ticker} at "
                    f"{end:%Y-%m-%d} is out of reach. Use the MetaTrader or CSV provider "
                    f"for history this old, or analyse a higher timeframe."
                )
            start = max(start, earliest)

        key = f"yahoo_{ticker}_{base}_{days}_{end:%Y%m%d%H}"

        frame = self._cache.get(key) if self._cache else None
        if frame is None:
            frame = self._download(ticker, base, start, end)
            if self._cache is not None and not frame.empty:
                self._cache.put(key, frame)

        if frame.empty:
            raise DataUnavailable(
                f"Yahoo returned no {base} bars for {ticker} "
                f"({days}d window). Check the symbol, or try a longer interval."
            )

        frame = canonicalise(frame)
        if base != interval:
            frame = resample(frame, interval)
        frame = apply_as_of(frame, as_of, interval)
        if frame.empty:
            raise DataUnavailable(f"no {interval} bars for {symbol} at or before {as_of}")
        log.info("yahoo %s %s: %d bars to %s", ticker, interval, len(frame), frame.index[-1])
        return frame

    def _download(self, ticker: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        import yfinance  # imported here so the package loads without a network stack

        frame = yfinance.download(
            ticker,
            start=start.date(),
            end=(end + timedelta(days=1)).date(),
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if isinstance(frame.columns, pd.MultiIndex):
            # A single-ticker download still comes back with (field, ticker)
            # columns on newer yfinance; flatten to the field.
            frame.columns = [str(level[0]) for level in frame.columns]
        return frame

    def search(self, query: str) -> list[dict[str, str]]:
        """Best-effort symbol lookup, for the console's symbol box."""
        try:
            import yfinance

            results = yfinance.Search(query, max_results=8).quotes
        except Exception as exc:  # the endpoint is undocumented and moves
            log.debug("symbol search failed: %s", exc)
            return []
        return [
            {
                "symbol": str(item.get("symbol", "")),
                "name": str(item.get("shortname") or item.get("longname") or ""),
                "type": str(item.get("quoteType", "")),
            }
            for item in results
            if item.get("symbol")
        ]
