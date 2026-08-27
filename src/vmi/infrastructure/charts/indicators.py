"""Indicator primitives, in plain pandas.

No TA library. Every formula here is four lines long, and a dependency that
silently changes a smoothing convention between minor versions is not worth the
import — the charts have to look the same next year for the replay to mean
anything.

All functions are causal: the value at bar *i* uses bars ≤ *i* only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ...domain.models import IndicatorSnapshot

REQUIRED_COLUMNS = ("open", "high", "low", "close")


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    middle = sma(series, window)
    # ddof=0: the population deviation, which is what every charting package draws.
    spread = series.rolling(window, min_periods=window).std(ddof=0) * num_std
    return pd.DataFrame(
        {"bb_middle": middle, "bb_upper": middle + spread, "bb_lower": middle - spread}
    )


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's RSI. The `ewm(alpha=1/window)` form is Wilder's smoothing."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # A window with no losses is RSI 100 by definition, not NaN.
    return out.where(avg_loss.notna() & (avg_loss != 0), other=100.0).where(avg_gain.notna())


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line, "macd_hist": macd_line - signal_line}
    )


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(frame: pd.DataFrame, window: int = 14) -> pd.Series:
    return true_range(frame).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


# Which column each indicator name in the config produces.
INDICATOR_COLUMNS: dict[str, tuple[str, ...]] = {
    "ema9": ("ema9",),
    "ema20": ("ema20",),
    "ema50": ("ema50",),
    "ema200": ("ema200",),
    "bbands": ("bb_upper", "bb_middle", "bb_lower"),
    "rsi": ("rsi",),
    "macd": ("macd", "macd_signal", "macd_hist"),
    "atr": ("atr",),
    "volume": ("volume_sma",),
}


def enrich(frame: pd.DataFrame, indicators: list[str] | None = None) -> pd.DataFrame:
    """Add every requested indicator column to a copy of *frame*.

    Unknown names are ignored rather than raised: `levels` is a legal entry in
    the config's indicator list and is handled by `levels.detect_levels`, not
    here, and a config should not have to know which module serves it.
    """
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"OHLCV frame is missing {missing}")

    wanted = set(indicators or list(INDICATOR_COLUMNS))
    out = frame.copy()
    close = out["close"]

    for span, name in ((9, "ema9"), (20, "ema20"), (50, "ema50"), (200, "ema200")):
        if name in wanted:
            out[name] = ema(close, span)
    if "bbands" in wanted:
        out = out.join(bollinger(close))
    if "rsi" in wanted:
        out["rsi"] = rsi(close)
    if "macd" in wanted:
        out = out.join(macd(close))
    if "atr" in wanted:
        out["atr"] = atr(out)
    if "volume" in wanted and "volume" in out.columns:
        out["volume_sma"] = sma(out["volume"], 20)
    return out


def _last(frame: pd.DataFrame, column: str) -> float | None:
    """The final value of *column*, or None when it was never computable.

    A NaN tail is the normal state of an EMA200 on a 120-bar window; returning
    None here is how that reaches the report as "not enough history" rather than
    as a number nobody should trust.
    """
    if column not in frame.columns:
        return None
    series = frame[column].dropna()
    if series.empty:
        return None
    value = float(series.iloc[-1])
    return None if not np.isfinite(value) else value


def snapshot(
    frame: pd.DataFrame, swings: tuple[list[float], list[float]] | None = None
) -> IndicatorSnapshot:
    """The numeric state of the last bar — provenance, not model input."""
    close = float(frame["close"].iloc[-1])
    atr_value = _last(frame, "atr")
    highs, lows = swings or ([], [])
    return IndicatorSnapshot(
        close=close,
        ema9=_last(frame, "ema9"),
        ema20=_last(frame, "ema20"),
        ema50=_last(frame, "ema50"),
        ema200=_last(frame, "ema200"),
        bb_upper=_last(frame, "bb_upper"),
        bb_middle=_last(frame, "bb_middle"),
        bb_lower=_last(frame, "bb_lower"),
        rsi=_last(frame, "rsi"),
        macd=_last(frame, "macd"),
        macd_signal=_last(frame, "macd_signal"),
        macd_hist=_last(frame, "macd_hist"),
        atr=atr_value,
        atr_pct=(atr_value / close * 100.0) if atr_value and close else None,
        volume=_last(frame, "volume"),
        volume_sma=_last(frame, "volume_sma"),
        swing_highs=highs,
        swing_lows=lows,
    )
