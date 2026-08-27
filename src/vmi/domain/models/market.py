"""Price, levels and the chart that is actually shown to the model.

A `ChartBundle` is the unit the vision layer consumes: one image, plus every
fact about how that image was made. The numeric snapshot travelling beside the
PNG is *not* fed to the vision model as text — it exists so that a level the
model claims to read can be checked against the range the chart really covers,
and so a run can be reproduced byte for byte later.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class LevelKind(str, Enum):
    SUPPORT = "support"
    RESISTANCE = "resistance"


class Level(BaseModel):
    """A horizontal price zone found in the data, not read off the picture."""

    model_config = ConfigDict(extra="forbid")

    price: float
    kind: LevelKind
    touches: int = Field(default=1, description="Swing points that formed this zone.")
    strength: float = Field(default=0.0, ge=0.0, le=1.0)
    last_touch: datetime | None = None

    def label(self, digits: int) -> str:
        return f"{self.kind.value[:1].upper()} {self.price:.{digits}f}"


class IndicatorSnapshot(BaseModel):
    """The last value of every indicator drawn on the chart.

    Used for grounding checks and by the stub vision provider. Optional
    throughout: a 15-minute chart has no EMA200 worth quoting if the window is
    shorter than 200 bars, and inventing one would be worse than a null.
    """

    model_config = ConfigDict(extra="forbid")

    close: float
    ema9: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    volume: float | None = None
    volume_sma: float | None = None
    swing_highs: list[float] = Field(default_factory=list)
    swing_lows: list[float] = Field(default_factory=list)


class PriceWindow(BaseModel):
    """What the chart covers, in numbers — the ground truth for any price claim."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: str
    interval: str
    bars: int
    start: datetime
    end: datetime
    price_min: float
    price_max: float
    last_close: float
    digits: int = 5

    def contains(self, price: float, tolerance: float = 0.02) -> bool:
        """Whether *price* falls inside the drawn range, with a little slack.

        A level a model reports outside this band was not read from the chart;
        it was remembered or invented, and the report says so.
        """
        span = max(self.price_max - self.price_min, 1e-12)
        return (self.price_min - tolerance * span) <= price <= (self.price_max + tolerance * span)


class ChartImage(BaseModel):
    """The PNG itself, addressed either by path or by bytes."""

    model_config = ConfigDict(extra="forbid")

    path: Path | None = None
    data_b64: str | None = None
    width: int
    height: int
    sha256: str
    chart_version: str = Field(description="Renderer version — charts differ, so runs differ.")


class ChartBundle(BaseModel):
    """One timeframe, ready for an analyst: picture, window, levels, numbers."""

    model_config = ConfigDict(extra="forbid")

    window: PriceWindow
    image: ChartImage
    levels: list[Level] = Field(default_factory=list)
    indicators: IndicatorSnapshot
    indicators_drawn: list[str] = Field(default_factory=list)

    @property
    def timeframe(self) -> str:
        return self.window.timeframe
