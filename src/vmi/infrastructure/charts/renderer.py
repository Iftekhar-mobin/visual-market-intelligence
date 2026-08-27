"""OHLCV in, one PNG out — the same PNG every time.

Determinism is the whole contract of this module. The renderer is versioned
(`CHART_VERSION`), the style is a frozen dictionary, nothing is randomised, and
the image hash is recorded with the run. Change anything visual and the version
goes up, because a report produced against a different picture is not comparable
with the ones before it.

The layout is fixed at four stacked panels:

    price   candles, EMAs, Bollinger band, support/resistance, labelled price grid
    volume  bars with a 20-period average
    RSI     with 30/50/70 rules
    MACD    line, signal and histogram

Two choices are worth explaining, because the reader is a vision model rather
than a person:

* **Labelled horizontal price gridlines.** A model cannot interpolate an axis
  accurately. Printing eight prices *inside* the plot area turns "read the
  support level" into "read the nearest printed number", which is a task these
  models are reliably good at.
* **Bar positions are integers, not timestamps.** Weekend gaps in FX and the
  overnight gap in equities otherwise leave holes a model reads as structure.
"""

from __future__ import annotations

import base64
import hashlib
import io

import matplotlib

matplotlib.use("Agg")  # never open a window; this runs inside a web server

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.collections import LineCollection
from matplotlib.patches import Rectangle

from ...config import ChartConfig
from ...domain.models import ChartBundle, ChartImage, Level, LevelKind, PriceWindow
from .indicators import enrich, snapshot
from .levels import detect_levels, swing_points

CHART_VERSION = "chart-v1"
"""Bump on any visual change. Stored with every run and compared during replay."""

DARK = {
    "bg": "#0e1117",
    "panel": "#161a23",
    "grid": "#232936",
    "text": "#d7dce5",
    "muted": "#7c8496",
    "up": "#26a69a",
    "down": "#ef5350",
    "ema9": "#ffd166",
    "ema20": "#4fc3f7",
    "ema50": "#ab47bc",
    "ema200": "#ff7043",
    "band": "#5c6bc0",
    "level_support": "#26a69a",
    "level_resistance": "#ef5350",
    "macd": "#4fc3f7",
    "signal": "#ffd166",
}

LIGHT = {
    "bg": "#ffffff",
    "panel": "#f6f7f9",
    "grid": "#e3e6ec",
    "text": "#1c2027",
    "muted": "#6b7280",
    "up": "#00897b",
    "down": "#d32f2f",
    "ema9": "#f9a825",
    "ema20": "#1e88e5",
    "ema50": "#8e24aa",
    "ema200": "#e64a19",
    "band": "#3949ab",
    "level_support": "#00897b",
    "level_resistance": "#d32f2f",
    "macd": "#1e88e5",
    "signal": "#f9a825",
}

PANEL_HEIGHTS = (5.0, 1.1, 1.5, 1.5)
"""price : volume : RSI : MACD. Price dominates because that is what is read."""

DEFAULT_INDICATORS = [
    "ema20", "ema50", "ema200", "bbands", "rsi", "macd", "atr", "volume", "levels",
]


def price_digits(price: float) -> int:
    """How many decimals this instrument deserves.

    1.0842 and 43,150.5 are both prices; printing five decimals on the second
    wastes the space the first needs.
    """
    price = abs(price)
    if price >= 100:
        return 2
    if price >= 10:
        return 3
    if price >= 1:
        # FX majors live here and are quoted to the pip fraction; four decimals
        # would round 1.16585 and 1.16584 to the same level.
        return 5
    return 6


class ChartRendererImpl:
    """The `ChartRenderer` port, in matplotlib."""

    version = CHART_VERSION

    def __init__(self, config: ChartConfig | None = None, max_bars: int = 240) -> None:
        self.config = config or ChartConfig()
        self.max_bars = max_bars
        self.palette = DARK if self.config.style == "dark" else LIGHT

    # ------------------------------------------------------------------ public

    def render(
        self,
        frame: pd.DataFrame,
        symbol: str,
        timeframe: str,
        interval: str,
        indicators: list[str] | None = None,
    ) -> ChartBundle:
        """Draw *frame* and return the bundle one analyst is given."""
        wanted = list(indicators or DEFAULT_INDICATORS)
        # Indicators are computed on the full history, then the window is cut:
        # an EMA200 on a 240-bar chart needs the 200 bars before the first one
        # drawn, and computing after the cut would leave it empty.
        data = enrich(frame, wanted).tail(self.max_bars)
        data = data.dropna(subset=["open", "high", "low", "close"])
        if data.empty:
            raise ValueError(f"no drawable bars for {symbol} {timeframe}")

        atr_series = data["atr"].dropna() if "atr" in data.columns else pd.Series(dtype=float)
        atr_value = float(atr_series.iloc[-1]) if not atr_series.empty else None
        levels = (
            detect_levels(data, self.config.level_lookback_pivots, atr_value)
            if "levels" in wanted
            else []
        )
        highs, lows = swing_points(data, self.config.level_lookback_pivots)

        png = self._draw(data, symbol, timeframe, interval, levels, wanted)
        digits = price_digits(float(data["close"].iloc[-1]))

        window = PriceWindow(
            symbol=symbol,
            timeframe=timeframe,
            interval=interval,
            bars=len(data),
            start=_as_utc(data.index[0]),
            end=_as_utc(data.index[-1]),
            price_min=float(data["low"].min()),
            price_max=float(data["high"].max()),
            last_close=float(data["close"].iloc[-1]),
            digits=digits,
        )
        image = ChartImage(
            data_b64=base64.b64encode(png).decode("ascii"),
            width=self.config.width_px,
            height=self.config.height_px,
            sha256=hashlib.sha256(png).hexdigest(),
            chart_version=self.version,
        )
        return ChartBundle(
            window=window,
            image=image,
            levels=levels,
            indicators=snapshot(data, (highs[-6:], lows[-6:])),
            indicators_drawn=wanted,
        )

    # ----------------------------------------------------------------- drawing

    def _draw(
        self,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str,
        interval: str,
        levels: list[Level],
        wanted: list[str],
    ) -> bytes:
        palette = self.palette
        figsize = (self.config.width_px / self.config.dpi, self.config.height_px / self.config.dpi)
        fig, axes = plt.subplots(
            4,
            1,
            figsize=figsize,
            dpi=self.config.dpi,
            sharex=True,
            gridspec_kw={"height_ratios": PANEL_HEIGHTS, "hspace": 0.06},
        )
        price_ax, volume_ax, rsi_ax, macd_ax = axes
        fig.patch.set_facecolor(palette["bg"])
        for axis in axes:
            axis.set_facecolor(palette["panel"])
            axis.tick_params(colors=palette["muted"], labelsize=8)
            for spine in axis.spines.values():
                spine.set_color(palette["grid"])
            axis.grid(True, color=palette["grid"], linewidth=0.5, alpha=0.6)

        x = np.arange(len(data), dtype=float)
        digits = price_digits(float(data["close"].iloc[-1]))

        self._candles(price_ax, x, data)
        self._overlays(price_ax, x, data, wanted)
        self._levels(price_ax, levels, digits)
        self._price_grid(price_ax, data, digits)
        self._volume(volume_ax, x, data)
        self._rsi(rsi_ax, x, data)
        self._macd(macd_ax, x, data)
        self._axis_labels(macd_ax, data)
        self._title(fig, price_ax, data, symbol, timeframe, interval, digits)

        buffer = io.BytesIO()
        fig.savefig(
            buffer,
            format="png",
            facecolor=palette["bg"],
            bbox_inches="tight",
            pad_inches=0.25,
            # No creation timestamp: identical inputs must give identical bytes.
            metadata={"Software": "vmi " + CHART_VERSION},
        )
        plt.close(fig)
        return buffer.getvalue()

    def _candles(self, ax, x: np.ndarray, data: pd.DataFrame) -> None:
        palette = self.palette
        opens = data["open"].to_numpy()
        closes = data["close"].to_numpy()
        highs = data["high"].to_numpy()
        lows = data["low"].to_numpy()
        rising = closes >= opens

        ax.add_collection(
            LineCollection(
                [[(xi, lo), (xi, hi)] for xi, lo, hi in zip(x, lows, highs, strict=True)],
                colors=[palette["up"] if up else palette["down"] for up in rising],
                linewidths=0.8,
                zorder=2,
            )
        )
        # A doji has zero body height and would vanish; give it a visible floor.
        span = float(highs.max() - lows.min()) or 1.0
        floor = span * 0.0008
        for xi, open_, close_, up in zip(x, opens, closes, rising, strict=True):
            height = max(abs(close_ - open_), floor)
            ax.add_patch(
                Rectangle(
                    (xi - 0.35, min(open_, close_)),
                    0.7,
                    height,
                    facecolor=palette["up"] if up else palette["down"],
                    edgecolor=palette["up"] if up else palette["down"],
                    linewidth=0.4,
                    zorder=3,
                )
            )
        ax.set_xlim(-1, len(data))
        pad = span * 0.06
        ax.set_ylim(float(lows.min()) - pad, float(highs.max()) + pad)

    def _overlays(self, ax, x: np.ndarray, data: pd.DataFrame, wanted: list[str]) -> None:
        palette = self.palette
        for name, width in (("ema9", 1.0), ("ema20", 1.1), ("ema50", 1.2), ("ema200", 1.4)):
            if name in wanted and name in data.columns:
                ax.plot(
                    x,
                    data[name].to_numpy(),
                    color=palette[name],
                    linewidth=width,
                    label=name.upper(),
                    zorder=4,
                )
        if "bbands" in wanted and "bb_upper" in data.columns:
            upper, lower = data["bb_upper"].to_numpy(), data["bb_lower"].to_numpy()
            ax.plot(x, upper, color=palette["band"], linewidth=0.8, alpha=0.8, label="BB(20,2)")
            ax.plot(x, lower, color=palette["band"], linewidth=0.8, alpha=0.8)
            ax.fill_between(x, lower, upper, color=palette["band"], alpha=0.07, zorder=1)
        legend = ax.legend(
            loc="upper left", fontsize=7, ncol=5, framealpha=0.0, labelcolor=palette["text"]
        )
        if legend is not None:
            legend.set_zorder(6)

    def _levels(self, ax, levels: list[Level], digits: int) -> None:
        palette = self.palette
        for level in levels:
            colour = (
                palette["level_support"]
                if level.kind is LevelKind.SUPPORT
                else palette["level_resistance"]
            )
            ax.axhline(
                level.price,
                color=colour,
                linewidth=0.9,
                linestyle="--",
                alpha=min(0.55 + 0.35 * level.strength, 1.0),
                zorder=2,
            )
            ax.annotate(
                level.kind.value[:1].upper() + " " + format(level.price, "." + str(digits) + "f"),
                xy=(0.004, level.price),
                xycoords=("axes fraction", "data"),
                fontsize=7,
                color=colour,
                va="center",
                ha="left",
                bbox={"facecolor": palette["bg"], "edgecolor": "none", "alpha": 0.65, "pad": 1.0},
                zorder=6,
            )

    def _price_grid(self, ax, data: pd.DataFrame, digits: int) -> None:
        """Print prices inside the plot so the model reads numbers, not pixels."""
        palette = self.palette
        low, high = ax.get_ylim()
        ticks = np.linspace(low, high, self.config.price_gridlines)
        ax.set_yticks(ticks)
        ax.set_yticklabels([format(tick, "." + str(digits) + "f") for tick in ticks], fontsize=8)
        ax.tick_params(axis="y", colors=palette["text"])

        if self.config.show_last_price_tag:
            last = float(data["close"].iloc[-1])
            up = last >= float(data["open"].iloc[-1])
            ax.annotate(
                " " + format(last, "." + str(digits) + "f") + " ",
                xy=(1.0, last),
                xycoords=("axes fraction", "data"),
                fontsize=8.5,
                fontweight="bold",
                color=palette["bg"],
                va="center",
                ha="left",
                bbox={
                    "facecolor": palette["up"] if up else palette["down"],
                    "edgecolor": "none",
                    "pad": 1.6,
                },
                annotation_clip=False,
                zorder=7,
            )
            ax.axhline(last, color=palette["muted"], linewidth=0.6, linestyle=":", alpha=0.7)

    def _volume(self, ax, x: np.ndarray, data: pd.DataFrame) -> None:
        palette = self.palette
        has_volume = "volume" in data.columns and float(data["volume"].fillna(0).sum()) > 0
        if not has_volume:
            # Spot FX from most free feeds has no volume. Say so rather than
            # drawing an empty panel a model might read as "volume collapsed".
            ax.text(
                0.5,
                0.5,
                "volume not provided by this feed",
                transform=ax.transAxes,
                ha="center",
                va="center",
                fontsize=8,
                color=palette["muted"],
            )
            ax.set_yticks([])
            ax.set_ylabel("VOL", color=palette["muted"], fontsize=8)
            return
        volume = data["volume"].fillna(0).to_numpy()
        rising = data["close"].to_numpy() >= data["open"].to_numpy()
        ax.bar(
            x,
            volume,
            width=0.7,
            color=[palette["up"] if up else palette["down"] for up in rising],
            alpha=0.65,
        )
        if "volume_sma" in data.columns:
            ax.plot(x, data["volume_sma"].to_numpy(), color=palette["ema20"], linewidth=1.0)
        ax.set_ylabel("VOL", color=palette["muted"], fontsize=8)
        ax.set_yticks([])

    def _rsi(self, ax, x: np.ndarray, data: pd.DataFrame) -> None:
        palette = self.palette
        if "rsi" not in data.columns:
            ax.set_visible(False)
            return
        values = data["rsi"].to_numpy()
        ax.plot(x, values, color=palette["ema20"], linewidth=1.1)
        for level, style in ((70, "--"), (50, ":"), (30, "--")):
            ax.axhline(level, color=palette["muted"], linewidth=0.6, linestyle=style, alpha=0.7)
        ax.fill_between(x, 70, values, where=values >= 70, color=palette["down"], alpha=0.25)
        ax.fill_between(x, 30, values, where=values <= 30, color=palette["up"], alpha=0.25)
        ax.set_ylim(0, 100)
        ax.set_yticks([30, 50, 70])
        ax.set_ylabel("RSI(14)", color=palette["muted"], fontsize=8)

    def _macd(self, ax, x: np.ndarray, data: pd.DataFrame) -> None:
        palette = self.palette
        if "macd" not in data.columns:
            ax.set_visible(False)
            return
        hist = np.nan_to_num(data["macd_hist"].to_numpy())
        ax.bar(
            x,
            hist,
            width=0.7,
            color=[palette["up"] if value >= 0 else palette["down"] for value in hist],
            alpha=0.6,
        )
        ax.plot(x, data["macd"].to_numpy(), color=palette["macd"], linewidth=1.1, label="MACD")
        ax.plot(
            x,
            data["macd_signal"].to_numpy(),
            color=palette["signal"],
            linewidth=1.0,
            label="signal",
        )
        ax.axhline(0, color=palette["muted"], linewidth=0.6, alpha=0.8)
        ax.set_ylabel("MACD", color=palette["muted"], fontsize=8)

    def _axis_labels(self, ax, data: pd.DataFrame) -> None:
        palette = self.palette
        count = len(data)
        step = max(count // 8, 1)
        positions = list(range(0, count, step))
        index = data.index
        fmt = "%Y-%m-%d" if (index[-1] - index[0]).days > 5 else "%m-%d %H:%M"
        ax.set_xticks(positions)
        ax.set_xticklabels(
            [pd.Timestamp(index[i]).strftime(fmt) for i in positions],
            rotation=0,
            fontsize=7.5,
            color=palette["muted"],
        )

    def _title(
        self,
        fig,
        ax,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str,
        interval: str,
        digits: int,
    ) -> None:
        palette = self.palette
        last = float(data["close"].iloc[-1])
        first = float(data["close"].iloc[0])
        change = (last - first) / first * 100 if first else 0.0
        start = pd.Timestamp(data.index[0]).strftime("%Y-%m-%d %H:%M")
        end = pd.Timestamp(data.index[-1]).strftime("%Y-%m-%d %H:%M")
        subtitle = (
            str(len(data))
            + " bars   "
            + start
            + " to "
            + end
            + " UTC   last "
            + format(last, "." + str(digits) + "f")
            + "   window "
            + format(change, "+.2f")
            + "%"
        )
        ax.set_title(
            symbol + "   " + timeframe + " (" + interval + ")",
            color=palette["text"],
            fontsize=14,
            fontweight="bold",
            loc="left",
            pad=18,
        )
        ax.annotate(
            subtitle,
            xy=(0.0, 1.008),
            xycoords="axes fraction",
            fontsize=8.5,
            color=palette["muted"],
            ha="left",
            va="bottom",
        )
        fig.text(
            0.995,
            0.005,
            "vmi - " + CHART_VERSION,
            fontsize=7,
            color=palette["muted"],
            ha="right",
            va="bottom",
        )


def _as_utc(value):
    stamp = pd.Timestamp(value)
    localised = stamp.tz_localize("UTC") if stamp.tz is None else stamp.tz_convert("UTC")
    return localised.to_pydatetime()
