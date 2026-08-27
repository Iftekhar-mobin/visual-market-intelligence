"""The Chart Preprocessing agent: bars in, a stack of charts out.

It is the only place market data and chart drawing meet, and it is deliberately
outside the vision agents. A vision agent that fetched its own data and drew its
own chart could not be replayed, could not be compared with yesterday's run, and
would make "the model saw a different picture" an untestable explanation for any
disagreement.

It also enforces the one rule that makes historical replay honest: `as_of` is
passed to the feed, the feed drops every bar after it, and no agent downstream
is ever given the chance to see past the cut-off.
"""

from __future__ import annotations

from datetime import datetime

from ...config import Config, TimeframeConfig
from ...domain.models import AgentTrace, ChartBundle
from ...infrastructure.charts import ChartRendererImpl
from ...infrastructure.market_data import DataUnavailable
from .base import Agent, AgentResult


class ChartPreprocessingAgent(Agent):
    """Fetch, enrich, draw. One bundle per configured timeframe."""

    name = "chart_preprocessing"

    def __init__(self, config: Config, provider, renderer: ChartRendererImpl | None = None) -> None:
        super().__init__()
        self.config = config
        self.provider = provider
        self.renderer = renderer or ChartRendererImpl(config.chart, config.data.max_bars)

    def run(
        self,
        symbol: str,
        timeframes: list[TimeframeConfig] | None = None,
        as_of: datetime | None = None,
    ) -> AgentResult[dict[str, ChartBundle]]:
        frames = timeframes or self.config.timeframes
        bundles: dict[str, ChartBundle] = {}
        failures: list[str] = []

        with self._traced(provider=getattr(self.provider, "name", "unknown")) as trace:
            for frame in frames:
                try:
                    bars = self.provider.fetch(symbol, frame.interval, frame.lookback, as_of)
                    bundles[frame.name] = self.renderer.render(
                        bars, symbol, frame.name, frame.interval, frame.indicators
                    )
                    self.log.info(
                        "%s %s: %d bars, %d levels",
                        symbol,
                        frame.name,
                        bundles[frame.name].window.bars,
                        len(bundles[frame.name].levels),
                    )
                except (DataUnavailable, ValueError) as exc:
                    # One timeframe missing is a degraded run, not a dead one:
                    # a 15-minute chart is unavailable outside market hours for
                    # plenty of instruments, and the H4 read is still worth having.
                    failures.append(f"{frame.name}: {exc}")
                    self.log.warning("no chart for %s %s: %s", symbol, frame.name, exc)

            if not bundles:
                raise DataUnavailable(
                    f"no chart could be built for {symbol}. " + " | ".join(failures)
                )
            trace.status = "ok" if not failures else "partial"
            trace.error = " | ".join(failures) or None

        return AgentResult(value=bundles, trace=trace)


def empty_trace(name: str, error: str) -> AgentTrace:
    return AgentTrace(agent=name, status="failed", error=error)
