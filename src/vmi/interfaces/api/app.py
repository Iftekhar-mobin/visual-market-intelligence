"""The HTTP surface — the only way another system should talk to this one.

The console, the CLI and any consuming quant platform all go through here. That
is a deliberate constraint: if something is awkward over HTTP, the API is
missing something, and hiding that by importing the pipeline directly from the
UI would let the gap survive.

Endpoints:

    GET  /health                      is everything reachable
    GET  /config                      the resolved configuration, key redacted
    POST /analyze                     symbol in, report out
    POST /analyze/charts              your own PNGs in, report out
    GET  /models                      what this backend can serve
    POST /models/select               switch backend for this process
    POST /models/pull                 download a local model
    GET  /runs                        the run index
    GET  /runs/{id}                   one stored report
    GET  /runs/{id}/chart/{tf}        the exact PNG the model was shown
    POST /replay                      walk history and score it
    GET  /symbols/search              symbol lookup for the console
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ... import __version__
from ...application.orchestration import VisionPipeline
from ...config import Config, default_config, load_config
from ...domain.models import ChartBundle, ChartImage, IndicatorSnapshot, PriceWindow
from ...infrastructure.market_data import DataUnavailable, build_provider
from ...infrastructure.observability import RequestIdMiddleware
from ...infrastructure.persistence import RunStoreImpl
from ...infrastructure.vision import build_vision_model, recommended_models
from ...logging_utils import configure_logging, get_logger
from .schemas import (
    AnalyzeRequest,
    ChartAnalyzeRequest,
    HealthResponse,
    ModelSelection,
    PullRequest,
    ReplayRequest,
)

log = get_logger("api")
security = HTTPBearer(auto_error=False)


class Container:
    """Everything the API needs, built once and shared.

    Pipelines are cached by (vision provider, model, data provider) because
    constructing one opens no connections but does build eight agents, and a
    console that flips between two models should not rebuild the world on every
    request.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or default_config()
        configure_logging(self.config.logging.level, self.config.logging.as_json)
        self.store = RunStoreImpl(self.config.runs_path, self.config.index_db_path)
        self._pipelines: dict[tuple[str, str, str], VisionPipeline] = {}

    def pipeline(
        self,
        provider: str | None = None,
        model: str | None = None,
        data_provider: str | None = None,
    ) -> VisionPipeline:
        key = (
            provider or self.config.vision.provider,
            model or self.config.vision.model,
            data_provider or self.config.data.provider,
        )
        if key not in self._pipelines:
            config = load_config(
                vision={"provider": key[0], "model": key[1]}, data={"provider": key[2]}
            )
            self._pipelines[key] = VisionPipeline(
                config,
                vision_model=build_vision_model(config),
                data_provider=build_provider(config, key[2]),
            )
        return self._pipelines[key]

    def select(self, provider: str, model: str) -> None:
        """Make a backend the process default. Survives until restart."""
        self.config = load_config(vision={"provider": provider, "model": model})
        self._pipelines.clear()


def create_app(config: Config | None = None) -> FastAPI:
    container = Container(config)

    app = FastAPI(
        title="Visual Market Intelligence",
        version=__version__,
        description=(
            "Multi-timeframe chart analysis by a vision model. Returns conditional "
            "opportunities, never a bare BUY or SELL."
        ),
    )
    app.add_middleware(RequestIdMiddleware)
    app.state.container = container

    def authorise(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> None:
        """No keys configured means an open API. Bind it to localhost if so."""
        keys = container.config.api.keys
        if not keys:
            return
        token = credentials.credentials if credentials else ""
        if token not in keys:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    # ------------------------------------------------------------------ health

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        pipeline = container.pipeline()
        state = pipeline.health()
        return HealthResponse(
            status="ok" if state["vision"]["reachable"] else "degraded",
            version=__version__,
            vision=state["vision"],
            data=state["data"],
            chart_version=str(state["chart_version"]),
            config_digest=str(state["config_digest"]),
            timeframes=list(state["timeframes"]),
            runs_stored=len(container.store.list_runs(limit=100000)),
        )

    @app.get("/config")
    def read_config(_: None = Depends(authorise)) -> dict[str, Any]:
        payload = container.config.model_dump(mode="json")
        payload["vision"]["api_key"] = "set" if container.config.vision.api_key else ""
        return payload

    # ----------------------------------------------------------------- analyse

    @app.post("/analyze")
    def analyze(request: AnalyzeRequest, _: None = Depends(authorise)) -> dict[str, Any]:
        pipeline = container.pipeline(request.provider, request.model, request.data_provider)
        try:
            report, bundles = pipeline.analyze(
                request.symbol.upper(), as_of=request.as_of, timeframes=request.timeframes
            )
        except DataUnavailable as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.store:
            container.store.save(report, bundles)

        payload = report.model_dump(mode="json")
        payload["api"] = report.to_api()
        if request.include_charts:
            payload["chart_images"] = {
                name: bundle.image.data_b64 for name, bundle in bundles.items()
            }
        return payload

    @app.post("/analyze/charts")
    def analyze_charts(
        request: ChartAnalyzeRequest, _: None = Depends(authorise)
    ) -> dict[str, Any]:
        """Analyse charts the caller drew themselves.

        No price series is available on this path, so nothing can be grounded:
        every level the model reports is accepted as given, and the report says
        as much. It exists for callers whose data cannot leave their network.
        """
        pipeline = container.pipeline()
        roles = request.roles or _default_roles(list(request.charts))
        bundles = {
            name: _bundle_from_image(request.symbol.upper(), name, image)
            for name, image in request.charts.items()
        }
        report = pipeline.analyze_bundles(request.symbol.upper(), bundles, roles)
        report.risks.append(
            "charts were supplied by the caller: no price series was available, so no level "
            "could be checked and no entry, stop or target was priced"
        )
        container.store.save(report, bundles)
        payload = report.model_dump(mode="json")
        payload["api"] = report.to_api()
        return payload

    # ------------------------------------------------------------------ models

    @app.get("/models")
    def models(
        provider: str | None = None,
        free_only: bool = False,
        _: None = Depends(authorise),
    ) -> dict[str, Any]:
        name = provider or container.config.vision.provider
        backend = build_vision_model(load_config(vision={"provider": name}))
        reachable, detail = backend.available()
        listed = backend.list_models()
        if free_only:
            listed = [model for model in listed if model.get("free")]
        return {
            "provider": name,
            "reachable": reachable,
            "detail": detail,
            "active": {
                "provider": container.config.vision.provider,
                "model": container.config.vision.model,
            },
            "models": listed,
            "recommended": recommended_models(name),
        }

    @app.post("/models/select")
    def select_model(request: ModelSelection, _: None = Depends(authorise)) -> dict[str, Any]:
        container.select(request.provider, request.model)
        backend = container.pipeline().vision
        reachable, detail = backend.available()
        return {
            "provider": request.provider,
            "model": request.model,
            "reachable": reachable,
            "detail": detail,
        }

    @app.post("/models/pull")
    def pull_model(request: PullRequest, _: None = Depends(authorise)) -> dict[str, Any]:
        if request.provider != "ollama":
            raise HTTPException(status_code=400, detail="only Ollama models can be pulled")
        backend = build_vision_model(load_config(vision={"provider": "ollama"}))
        try:
            result = backend.pull(request.model)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"pull failed: {exc}") from exc
        return {"model": request.model, "result": result}

    # -------------------------------------------------------------------- runs

    @app.get("/runs")
    def list_runs(
        limit: int = Query(default=50, le=1000),
        symbol: str | None = None,
        state: str | None = None,
        _: None = Depends(authorise),
    ) -> dict[str, Any]:
        return {
            "runs": container.store.list_runs(limit=limit, symbol=symbol, state=state),
            "symbols": container.store.symbols(),
        }

    @app.get("/runs/{run_id}")
    def read_run(run_id: str, _: None = Depends(authorise)) -> dict[str, Any]:
        report = container.store.load(run_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        payload = report.model_dump(mode="json")
        payload["api"] = report.to_api()
        return payload

    @app.get("/runs/{run_id}/chart/{timeframe}")
    def read_chart(run_id: str, timeframe: str, _: None = Depends(authorise)) -> FileResponse:
        path = container.store.chart_path(run_id, timeframe)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no {timeframe} chart for {run_id}")
        return FileResponse(path, media_type="image/png")

    @app.delete("/runs/{run_id}")
    def delete_run(run_id: str, _: None = Depends(authorise)) -> dict[str, Any]:
        return {"deleted": container.store.delete(run_id)}

    # ------------------------------------------------------------------ replay

    @app.post("/replay")
    def run_replay(request: ReplayRequest, _: None = Depends(authorise)) -> dict[str, Any]:
        """Walk history and score it. Synchronous, and slow by nature.

        A six-month daily replay is ~180 model calls. Against a local 7B model
        that is hours, so the CLI is the better door for anything large; this
        endpoint exists so the console can run a short one.
        """
        from ...evaluation import replay

        result = replay(
            request.symbol.upper(),
            request.start,
            request.end,
            request.step,
            config=container.config,
            pipeline=container.pipeline(),
            store=container.store,
            save=request.store,
        )
        summary = result.summary
        return {
            "symbol": result.symbol,
            "reports": len(result.reports),
            "failures": result.failures,
            "outcomes": [outcome.as_row() for outcome in result.outcomes],
            "summary": summary.reset_index().to_dict(orient="records") if not summary.empty else [],
        }

    # ----------------------------------------------------------------- symbols

    @app.get("/symbols/search")
    def search_symbols(q: str, _: None = Depends(authorise)) -> dict[str, Any]:
        provider = build_provider(container.config, "yahoo")
        return {"query": q, "results": provider.search(q)}

    # ------------------------------------------------------------------ errors

    @app.exception_handler(DataUnavailable)
    def _data_unavailable(request: Request, exc: DataUnavailable) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": {"message": str(exc)}})

    return app


def _default_roles(names: list[str]) -> dict[str, str]:
    """Highest timeframe is context, lowest is entry, the rest is the setup."""
    if len(names) == 1:
        return {names[0]: "setup"}
    roles = dict.fromkeys(names, "setup")
    roles[names[0]] = "context"
    roles[names[-1]] = "entry"
    return roles


def _bundle_from_image(symbol: str, timeframe: str, image_b64: str) -> ChartBundle:
    """Wrap a caller's PNG in the same envelope a rendered chart travels in.

    The price window is a placeholder: with no series behind the picture there
    is nothing to check a level against, so the range is opened up rather than
    faked, and `bars=0` marks it as unknown wherever it is read.
    """
    payload = image_b64.split(",", 1)[-1]  # tolerate a data: URI
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{timeframe}: not valid base64 PNG") from exc

    return ChartBundle(
        window=PriceWindow(
            symbol=symbol,
            timeframe=timeframe,
            interval="unknown",
            bars=0,
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc),
            price_min=0.0,
            price_max=1e12,
            last_close=0.0,
            digits=5,
        ),
        image=ChartImage(
            data_b64=payload,
            width=0,
            height=0,
            sha256=hashlib.sha256(raw).hexdigest(),
            chart_version="caller-supplied",
        ),
        levels=[],
        indicators=IndicatorSnapshot(close=0.0),
        indicators_drawn=[],
    )


app = create_app()
"""The ASGI application `uvicorn vmi.interfaces.api.app:app` serves."""
