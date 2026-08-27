"""The seams. Every replaceable part of the system is one of these protocols.

They are `Protocol`s rather than base classes on purpose: an implementation in
`infrastructure` never imports the port, so the dependency arrow points inwards
even though the call goes outwards.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from .models import ChartBundle, VisionReport


@runtime_checkable
class MarketDataProvider(Protocol):
    """Bars in, nothing else. Any feed that can answer this can be plugged in."""

    name: str

    def fetch(
        self,
        symbol: str,
        interval: str,
        lookback: str,
        as_of: datetime | None = None,
    ) -> pd.DataFrame:
        """Return an OHLCV frame indexed by timestamp, oldest first.

        Implementations MUST drop every bar after *as_of* before returning.
        That single rule is what makes historical replay honest.
        """
        ...


@runtime_checkable
class ChartRenderer(Protocol):
    """Deterministic OHLCV → PNG. The same frame must give the same bytes."""

    version: str

    def render(
        self, frame: pd.DataFrame, symbol: str, timeframe: str, interval: str
    ) -> ChartBundle:
        ...


@runtime_checkable
class VisionModel(Protocol):
    """A vision-language backend. One method, one contract, no vendor leakage."""

    provider: str
    model: str

    def analyze(self, image_b64: str, prompt: str, system: str | None = None) -> str:
        """Answer about the image. Returns raw model text — parsing is the caller's."""
        ...

    def available(self) -> tuple[bool, str]:
        """(reachable, human-readable reason). Never raises."""
        ...

    def list_models(self) -> list[dict[str, Any]]:
        """Models this backend can serve right now. Empty when it cannot say."""
        ...


@runtime_checkable
class RunStore(Protocol):
    """Where reports and charts are kept so a run can be read back later."""

    def save(self, report: VisionReport, charts: dict[str, bytes]) -> str:
        ...

    def load(self, run_id: str) -> VisionReport | None:
        ...

    def list_runs(self, limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
        ...
