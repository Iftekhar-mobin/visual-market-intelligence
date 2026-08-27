"""Price feeds. One factory, three implementations, one canonical frame.

Adding a fourth means writing a class with `name` and `fetch`, and adding a line
to `build_provider`. Nothing else in the system needs to know it exists.
"""

from __future__ import annotations

from ...config import Config
from ...paths import project_path
from .base import (
    CANONICAL_COLUMNS,
    DataUnavailable,
    FrameCache,
    apply_as_of,
    canonicalise,
    interval_seconds,
    parse_duration,
    resample,
)
from .csv_files import CsvProvider
from .metatrader import MetaTraderProvider
from .yahoo import YahooProvider, to_yahoo_symbol

__all__ = [
    "CANONICAL_COLUMNS",
    "CsvProvider",
    "DataUnavailable",
    "FrameCache",
    "MetaTraderProvider",
    "YahooProvider",
    "apply_as_of",
    "build_provider",
    "canonicalise",
    "interval_seconds",
    "parse_duration",
    "resample",
    "to_yahoo_symbol",
]


def build_provider(config: Config, name: str | None = None):
    """The feed named in the config, or *name* when a caller overrides it."""
    provider = (name or config.data.provider).lower()
    if provider == "yahoo":
        return YahooProvider(FrameCache(config.cache_path, config.data.cache_ttl_s))
    if provider == "metatrader":
        return MetaTraderProvider()
    if provider == "csv":
        return CsvProvider(project_path("data/samples"))
    raise ValueError(f"unknown market data provider {provider!r}")
