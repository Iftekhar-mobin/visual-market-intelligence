"""The wire contract. What a caller sends, and what comes back.

Deliberately narrow. The domain models are rich because the agents need them to
be; the API surface is the subset another system should build against, and it is
versioned separately from the internals so refactoring an agent does not break a
consumer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AnalyzeRequest(BaseModel):
    """`POST /analyze` — the main entry point."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="EURUSD, AAPL, BTCUSD, GC=F …")
    timeframes: list[str] | None = Field(
        default=None, description="Subset of the configured ladder. None means all of it."
    )
    as_of: datetime | None = Field(
        default=None,
        description="Analyse the market as it looked at this moment. Bars after it are dropped.",
    )
    provider: str | None = Field(default=None, description="Override the vision backend.")
    model: str | None = Field(default=None, description="Override the vision model.")
    data_provider: str | None = Field(default=None, description="yahoo | metatrader | csv")
    store: bool = Field(default=True, description="Persist the run and its charts.")
    include_charts: bool = Field(
        default=False, description="Return the PNGs base64-encoded in the response."
    )


class ChartAnalyzeRequest(BaseModel):
    """`POST /analyze/charts` — charts you already have, no market data needed.

    The escape hatch for anyone whose price data lives somewhere this service
    cannot reach. Levels and indicator grounding are unavailable on this path,
    so the report says so.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    charts: dict[str, str] = Field(description="Timeframe name -> base64 PNG.")
    roles: dict[str, str] | None = Field(
        default=None, description="Timeframe name -> context | setup | entry."
    )


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str = "ollama"


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    start: datetime
    end: datetime
    step: str = "24h"
    store: bool = True


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    version: str
    vision: dict[str, Any]
    data: dict[str, Any]
    chart_version: str
    config_digest: str
    timeframes: list[str]
    runs_stored: int


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    kind: str = "error"
    request_id: str = ""
