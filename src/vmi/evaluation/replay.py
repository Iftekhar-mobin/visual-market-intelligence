"""Running the system over history, without letting it see the future.

The replay walks a cursor through past time. At each stop it asks the pipeline
for a report *as if it were then* — the feed truncates at the cursor, the charts
are drawn from truncated data, and no agent is ever handed a bar that had not
closed. Afterwards, and only afterwards, the same feed is asked for the whole
series so the outcome scorer can look at what followed.

That separation is the entire value of this module. It is also the easiest thing
in a project like this to get quietly wrong, which is why the cut happens in one
place — `market_data.base.apply_as_of` — and every provider routes through it.

    vmi replay EURUSD --start 2026-01-01 --end 2026-06-30 --step 24h
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..application.orchestration import VisionPipeline
from ..config import Config
from ..domain.models import VisionReport
from ..infrastructure.market_data import build_provider, parse_duration
from ..infrastructure.persistence import RunStoreImpl
from ..logging_utils import get_logger
from .outcomes import Outcome, score, summarise

log = get_logger("replay")


@dataclass
class ReplayResult:
    symbol: str
    reports: list[VisionReport]
    outcomes: list[Outcome]
    failures: list[str]

    @property
    def summary(self) -> pd.DataFrame:
        return summarise(self.outcomes)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([outcome.as_row() for outcome in self.outcomes])


def cursors(start: datetime, end: datetime, step: str) -> Iterator[datetime]:
    """Every moment the system will be asked to make a call."""
    delta = parse_duration(step)
    if delta <= timedelta(0):
        raise ValueError("replay step must be positive")
    cursor = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    stop = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    while cursor <= stop:
        yield cursor
        cursor += delta


def replay(
    symbol: str,
    start: datetime,
    end: datetime,
    step: str = "24h",
    config: Config | None = None,
    pipeline: VisionPipeline | None = None,
    store: RunStoreImpl | None = None,
    save: bool = True,
    on_progress: Callable[[int, int, VisionReport | None], None] | None = None,
) -> ReplayResult:
    """Walk history, one report per cursor, then score them all.

    *save* writes each run to the store, which is usually what you want: a
    replay of six months at a daily step is several hundred model calls and
    losing them to a crash at hour three is painful.
    """
    from ..config import default_config

    config = config or default_config()
    pipeline = pipeline or VisionPipeline(config)
    store = store or RunStoreImpl(config.runs_path, config.index_db_path)

    stops = list(cursors(start, end, step))
    reports: list[VisionReport] = []
    failures: list[str] = []
    log.info("replaying %s over %d cursors (%s steps)", symbol, len(stops), step)

    for index, cursor in enumerate(stops, start=1):
        started = time.perf_counter()
        try:
            report, bundles = pipeline.analyze(symbol, as_of=cursor)
        except Exception as exc:
            failures.append(f"{cursor.isoformat()}: {exc}")
            log.warning("cursor %s failed: %s", cursor.isoformat(), exc)
            if on_progress:
                on_progress(index, len(stops), None)
            continue
        if save:
            store.save(report, bundles)
        reports.append(report)
        log.info(
            "%d/%d %s -> %s (%.1fs)",
            index,
            len(stops),
            cursor.date(),
            report.current_state.value,
            time.perf_counter() - started,
        )
        if on_progress:
            on_progress(index, len(stops), report)

    outcomes = score_reports(reports, config, symbol)
    return ReplayResult(symbol=symbol, reports=reports, outcomes=outcomes, failures=failures)


def score_reports(
    reports: list[VisionReport], config: Config, symbol: str | None = None
) -> list[Outcome]:
    """Fetch the full series once and score every report against it."""
    if not reports:
        return []
    symbol = symbol or reports[0].symbol
    frame = config.entry_timeframe
    provider = build_provider(config)

    # A window wide enough to cover the last report's longest horizon.
    horizons = config.evaluation.horizons
    span_bars = max(horizons) if horizons else 50
    lookback = _lookback_for(reports, frame.interval, span_bars)
    series = provider.fetch(symbol, frame.interval, lookback, as_of=None)

    return [score(report, series, horizons) for report in reports]


def _lookback_for(reports: list[VisionReport], interval: str, span_bars: int) -> str:
    """A lookback string covering every report plus the scoring horizon."""
    from ..infrastructure.market_data import interval_seconds

    earliest = min(
        (report.metadata.as_of or report.metadata.created_at) for report in reports
    )
    now = datetime.now(timezone.utc)
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=timezone.utc)
    days = (now - earliest).days + 1
    days += int(span_bars * interval_seconds(interval) / 86400) + 1
    return f"{max(days, 2)}d"
