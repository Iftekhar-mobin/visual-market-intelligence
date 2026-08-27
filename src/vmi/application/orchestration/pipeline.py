"""The pipeline: eight agents, one report, one run id.

    charts ─┬─ H4 analyst ─┐
            ├─ H1 analyst ─┼─ structure ─ opportunity ─ risk ─ report
            └─ M15 analyst ┘

The order is fixed and the data flow is one-way. Each analyst sees exactly one
chart and knows nothing about the others — that independence is the point of the
design, because an analyst told what the higher timeframe concluded will agree
with it, and three agreeing agents that were never independent are one agent
wearing three hats.

Everything the run needs to be reproduced is captured on the way through: the
model, the prompt version, the chart version, the `as_of` cut-off and a digest of
the configuration.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from ...config import Config
from ...domain.models import ChartBundle, MarketState, VisionReport
from ...infrastructure.charts import ChartRendererImpl
from ...infrastructure.market_data import build_provider
from ...infrastructure.vision import build_vision_model
from ...logging_utils import get_logger
from ..agents.opportunity import OpportunityAgent
from ..agents.preprocess import ChartPreprocessingAgent
from ..agents.report import ReportAgent, build_metadata
from ..agents.risk import RiskAgent
from ..agents.structure import StructureAgent
from ..agents.timeframe_analyst import TimeframeAnalyst

log = get_logger("pipeline")


def config_digest(config: Config) -> str:
    """A short hash of everything that could change an answer.

    The API key is excluded — it changes nothing about the output and does not
    belong in a stored artefact.
    """
    payload = config.model_dump(mode="json")
    payload["vision"].pop("api_key", None)
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


class VisionPipeline:
    """One instance per configuration; safe to reuse across requests."""

    def __init__(
        self,
        config: Config,
        vision_model=None,
        data_provider=None,
        renderer: ChartRendererImpl | None = None,
    ) -> None:
        self.config = config
        self.vision = vision_model or build_vision_model(config)
        self.data = data_provider or build_provider(config)
        self.renderer = renderer or ChartRendererImpl(config.chart, config.data.max_bars)

        self.preprocess = ChartPreprocessingAgent(config, self.data, self.renderer)
        self.analysts = {
            frame.name: TimeframeAnalyst(frame.role, self.vision, config)
            for frame in config.timeframes
        }
        self.structure = StructureAgent()
        self.opportunity = OpportunityAgent(config)
        self.risk = RiskAgent(config)
        self.reporter = ReportAgent()

    # ------------------------------------------------------------------- run

    def analyze(
        self,
        symbol: str,
        as_of: datetime | None = None,
        timeframes: list[str] | None = None,
    ) -> tuple[VisionReport, dict[str, ChartBundle]]:
        """Analyse *symbol*, optionally as it looked at *as_of*.

        Returns the report and the chart bundles, because the caller — the API,
        the CLI, the console — is usually about to store or display the images
        and re-rendering them would be both slow and, over a version bump,
        wrong.
        """
        started = time.perf_counter()
        run_id = _run_id(symbol)
        frames = [
            frame
            for frame in self.config.timeframes
            if timeframes is None or frame.name.upper() in {t.upper() for t in timeframes}
        ]
        if not frames:
            raise ValueError(f"no configured timeframe matches {timeframes}")

        log.info(
            "run %s: %s over %s%s",
            run_id,
            symbol,
            [frame.name for frame in frames],
            f" as of {as_of.isoformat()}" if as_of else "",
        )

        charts = self.preprocess.run(symbol, frames, as_of)
        bundles = charts.value
        roles = {frame.name: frame.role for frame in frames}
        report = self.analyze_bundles(
            symbol, bundles, roles, as_of=as_of, run_id=run_id, extra_traces=[charts.trace]
        )
        report.metadata.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        log.info(
            "run %s finished in %.1fs: %s (confidence %.2f)",
            run_id,
            report.metadata.duration_ms / 1000,
            report.current_state.value,
            report.vision_confidence,
        )
        return report, bundles

    def analyze_bundles(
        self,
        symbol: str,
        bundles: dict[str, ChartBundle],
        roles: dict[str, str],
        as_of: datetime | None = None,
        run_id: str | None = None,
        extra_traces: list | None = None,
    ) -> VisionReport:
        """Everything after the charts exist.

        Split out so that charts produced somewhere else — uploaded by a caller
        who keeps their price data behind a firewall — travel exactly the same
        path as charts this service drew.
        """
        run_id = run_id or _run_id(symbol)
        traces = list(extra_traces or [])
        observations = []

        for name, bundle in bundles.items():
            role = roles.get(name, "setup")
            analyst = self.analysts.get(name)
            if analyst is None or analyst.role != role:
                # An uploaded chart named something the ladder does not know, or
                # given a different role than the config assigns it.
                analyst = TimeframeAnalyst(role, self.vision, self.config)
            result = analyst.run(bundle)
            observations.append(result.value)
            traces.append(result.trace)

        structure = self.structure.run(observations)
        traces.append(structure.trace)

        opportunity = self.opportunity.run(observations, structure.value, bundles)
        long_case, short_case, state = opportunity.value
        traces.append(opportunity.trace)

        risk = self.risk.run(
            long_case, short_case, state, structure.value, observations, bundles
        )
        assessment, state = risk.value
        traces.append(risk.trace)

        metadata = build_metadata(
            run_id=run_id,
            symbol=symbol,
            as_of=as_of,
            provider=getattr(self.vision, "provider", "unknown"),
            model=getattr(self.vision, "model", "unknown"),
            chart_version=self.renderer.version,
            timeframes=list(bundles),
            data_provider=getattr(self.data, "name", "unknown"),
            config_digest=config_digest(self.config),
        )
        final = self.reporter.run(
            metadata=metadata,
            observations=observations,
            structure=structure.value,
            long=long_case,
            short=short_case,
            state=state,
            risk=assessment,
            bundles=bundles,
        )
        traces.append(final.trace)

        report = final.value
        report.traces = traces
        return report

    # ------------------------------------------------------------- diagnostics

    def health(self) -> dict[str, object]:
        """Whether the two things that can be missing are actually there."""
        reachable, detail = self.vision.available()
        return {
            "vision": {
                "provider": getattr(self.vision, "provider", "unknown"),
                "model": getattr(self.vision, "model", "unknown"),
                "reachable": reachable,
                "detail": detail,
            },
            "data": {"provider": getattr(self.data, "name", "unknown")},
            "chart_version": self.renderer.version,
            "config_digest": config_digest(self.config),
            "timeframes": [frame.name for frame in self.config.timeframes],
        }


def _run_id(symbol: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{symbol.upper().replace('/', '')}-{stamp}-{uuid.uuid4().hex[:6]}"


TERMINAL_STATES = {MarketState.LONG_TRIGGERED, MarketState.SHORT_TRIGGERED}
"""States that assert a position could be opened now — the ones the evaluator
scores hardest, and the ones a consuming system should treat as actionable."""
