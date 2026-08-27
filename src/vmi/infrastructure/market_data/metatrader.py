"""MetaTrader 5 — the feed to use when there is a terminal on the machine.

It is better than Yahoo for FX in every way that matters here: real 4-hour
bars, tick volume, broker-accurate sessions, and years of intraday history. It
is also Windows-only, needs a running terminal that is logged in, and is
therefore never a hard dependency. The import happens inside `_connect` so a
Linux deployment of this service loads normally and simply reports the provider
as unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from ...logging_utils import get_logger
from .base import DataUnavailable, apply_as_of, canonicalise, interval_seconds, parse_duration

log = get_logger("data.mt5")

# MetaTrader's timeframe constants, by seconds per bar. Resolved lazily because
# the values live in the MetaTrader5 module, which may not be installed.
_TIMEFRAME_NAMES = {
    60: "TIMEFRAME_M1",
    300: "TIMEFRAME_M5",
    900: "TIMEFRAME_M15",
    1800: "TIMEFRAME_M30",
    3600: "TIMEFRAME_H1",
    14400: "TIMEFRAME_H4",
    86400: "TIMEFRAME_D1",
    604800: "TIMEFRAME_W1",
}


class MetaTraderProvider:
    """The `MarketDataProvider` port, over the MetaTrader 5 Python bridge."""

    name = "metatrader"

    def __init__(self, terminal_path: str | None = None) -> None:
        self._terminal_path = terminal_path
        self._module = None

    # ------------------------------------------------------------- connection

    def _connect(self):
        if self._module is not None:
            return self._module
        try:
            import MetaTrader5 as mt5  # noqa: N813 - the vendor spells it this way
        except ImportError as exc:
            raise DataUnavailable(
                "MetaTrader5 is not installed. `uv sync --extra mt5` on a Windows "
                "machine with the terminal installed, then try again."
            ) from exc

        initialised = (
            mt5.initialize(self._terminal_path) if self._terminal_path else mt5.initialize()
        )
        if not initialised:
            raise DataUnavailable(
                f"MetaTrader 5 would not start: {mt5.last_error()}. "
                "Open the terminal, log in to an account, and leave it running."
            )
        self._module = mt5
        return mt5

    def available(self) -> tuple[bool, str]:
        try:
            self._connect()
        except DataUnavailable as exc:
            return False, str(exc)
        return True, "terminal connected"

    def symbols(self, pattern: str = "") -> list[str]:
        mt5 = self._connect()
        found = mt5.symbols_get(pattern) if pattern else mt5.symbols_get()
        return sorted(item.name for item in (found or []))

    # ------------------------------------------------------------------ fetch

    def fetch(
        self,
        symbol: str,
        interval: str,
        lookback: str,
        as_of: datetime | None = None,
    ) -> pd.DataFrame:
        mt5 = self._connect()
        seconds = interval_seconds(interval)
        name = _TIMEFRAME_NAMES.get(seconds)
        if name is None:
            raise DataUnavailable(
                f"MetaTrader has no {interval} timeframe; use one of "
                f"{sorted(_TIMEFRAME_NAMES)} seconds per bar"
            )
        timeframe = getattr(mt5, name)

        if not mt5.symbol_select(symbol, True):
            raise DataUnavailable(f"{symbol} is not in this terminal's Market Watch")

        end = as_of or datetime.now(timezone.utc)
        start = end - parse_duration(lookback) - timedelta(days=3)
        rates = mt5.copy_rates_range(symbol, timeframe, start, end)
        if rates is None or len(rates) == 0:
            raise DataUnavailable(f"MetaTrader returned no {interval} bars for {symbol}")

        frame = pd.DataFrame(rates)
        # MT5 stamps bars with the *open* time in the server's timezone; the
        # rest of the system is right-stamped UTC, so shift by one bar.
        frame.index = pd.to_datetime(frame["time"], unit="s", utc=True) + pd.Timedelta(
            seconds=seconds
        )
        frame = frame.rename(columns={"tick_volume": "volume", "real_volume": "real_volume"})
        frame = canonicalise(frame)
        frame = apply_as_of(frame, as_of, interval)
        if frame.empty:
            raise DataUnavailable(f"no {interval} bars for {symbol} at or before {as_of}")
        log.info("mt5 %s %s: %d bars to %s", symbol, interval, len(frame), frame.index[-1])
        return frame

    def close(self) -> None:
        if self._module is not None:
            self._module.shutdown()
            self._module = None
