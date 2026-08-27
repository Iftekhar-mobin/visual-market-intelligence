"""The command line.

    vmi health                              is the backend reachable
    vmi models [--provider ollama]          what can serve a chart
    vmi charts EURUSD [--out charts/]       draw only, no model
    vmi analyze EURUSD [--as-of ...]        the whole pipeline
    vmi replay EURUSD --start ... --end ... walk history and score it
    vmi runs [--symbol EURUSD]              the run index
    vmi show RUN_ID                         one stored report
    vmi serve [--port 8100]                 the API
    vmi console                             the Streamlit console

Every command prints a human-readable summary and takes `--json` when something
downstream wants to read it instead. Nothing the console can do is unavailable
here — that is what makes a result reproducible rather than a screenshot.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ... import __version__
from ...application.orchestration import VisionPipeline
from ...config import Config, load_config
from ...domain.models import VisionReport
from ...infrastructure.charts import ChartRendererImpl
from ...infrastructure.market_data import DataUnavailable, build_provider
from ...infrastructure.persistence import RunStoreImpl
from ...infrastructure.vision import build_vision_model, recommended_models
from ...logging_utils import configure_logging
from ...paths import PROJECT_ROOT


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _config(args: argparse.Namespace) -> Config:
    overrides: dict[str, dict[str, object]] = {}
    if getattr(args, "provider", None) or getattr(args, "model", None):
        overrides["vision"] = {}
        if args.provider:
            overrides["vision"]["provider"] = args.provider
        if args.model:
            overrides["vision"]["model"] = args.model
    if getattr(args, "data", None):
        overrides["data"] = {"provider": args.data}
    config = load_config(getattr(args, "config", None), **overrides)
    configure_logging(
        "DEBUG" if getattr(args, "verbose", False) else config.logging.level, config.logging.as_json
    )
    return config


# --------------------------------------------------------------------- output


def print_report(report: VisionReport) -> None:
    """The report as a person wants to read it: state first, then the why."""
    line = "-" * 78
    print(line)
    print(f" {report.symbol}   {report.current_state.value}   ({report.market_regime})")
    print(line)
    print(f" run          {report.metadata.run_id}")
    print(f" model        {report.metadata.provider} / {report.metadata.model}")
    print(f" as of        {(report.metadata.as_of or report.metadata.created_at).isoformat()}")
    print(f" last price   {report.last_price}")
    print(f" confidence   {report.vision_confidence:.2f}")
    print(f" alignment    {report.structure.alignment.value} "
          f"(dominant {report.structure.dominant_timeframe})")
    print(line)

    for observation in report.observations:
        flag = " [no reading]" if observation.degraded else ""
        print(
            f" {observation.timeframe:<5} {observation.trend.value:<9} "
            f"{observation.trend_strength.value:<9} {observation.regime.value:<14} "
            f"{observation.setup.value:<12} {observation.confidence:.2f}{flag}"
        )
    print(line)

    for scenario in (report.long, report.short):
        if scenario is None:
            continue
        print(f" {scenario.direction.value}  score {scenario.score:.2f}  "
              f"quality {scenario.quality}  type {scenario.setup_type}")
        if scenario.condition:
            print(f"   holds while     {scenario.condition}")
        if scenario.entry_zone:
            print(f"   entry zone      {scenario.entry_zone}")
        if scenario.trigger:
            print(f"   trigger         {scenario.trigger}")
        if scenario.invalidation:
            print(f"   invalidation    {scenario.invalidation}")
        if scenario.targets.zones:
            print(f"   targets         {scenario.targets.zones}")
        if scenario.reward_risk:
            print(f"   reward/risk     {scenario.reward_risk:.2f}x")
        print()

    if report.risks:
        print(" risks")
        for risk in report.risks:
            print(f"   - {risk}")
        print(line)
    print(f" {report.summary}")
    print(line)


# -------------------------------------------------------------------- commands


def cmd_health(args: argparse.Namespace) -> int:
    config = _config(args)
    pipeline = VisionPipeline(config)
    state = pipeline.health()
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    vision = state["vision"]
    print(f"vmi {__version__}")
    print(f"vision   {vision['provider']} / {vision['model']}")
    print(f"         {'reachable' if vision['reachable'] else 'NOT reachable'}: {vision['detail']}")
    print(f"data     {state['data']['provider']}")
    print(f"charts   {state['chart_version']}   config {state['config_digest']}")
    print(f"ladder   {', '.join(state['timeframes'])}")
    return 0 if vision["reachable"] else 1


def cmd_models(args: argparse.Namespace) -> int:
    config = _config(args)
    backend = build_vision_model(config)
    reachable, detail = backend.available()
    listed = backend.list_models()
    if args.json:
        print(json.dumps({"reachable": reachable, "detail": detail, "models": listed}, indent=2))
        return 0
    print(f"{backend.provider}: {'reachable' if reachable else 'unreachable'} — {detail}\n")
    if listed:
        for model in listed:
            mark = "vision" if model.get("vision") else "text  "
            size = f" {model['size_gb']:.1f} GB" if model.get("size_gb") else ""
            print(f"  [{mark}] {model['id']}{size}")
    else:
        print("  (nothing installed)")
    print("\nfree models worth having:")
    for model in recommended_models():
        print(f"  {model['provider']:<11} {model['id']:<42} {model['note']}")
    return 0


def cmd_charts(args: argparse.Namespace) -> int:
    """Draw the ladder and stop. The fastest way to see what the model will see."""
    import base64

    config = _config(args)
    provider = build_provider(config)
    renderer = ChartRendererImpl(config.chart, config.data.max_bars)
    out = Path(args.out or (PROJECT_ROOT / "data" / "charts"))
    out.mkdir(parents=True, exist_ok=True)
    as_of = _parse_time(args.as_of)

    for frame in config.timeframes:
        try:
            bars = provider.fetch(args.symbol.upper(), frame.interval, frame.lookback, as_of)
            bundle = renderer.render(
                bars, args.symbol.upper(), frame.name, frame.interval, frame.indicators
            )
        except (DataUnavailable, ValueError) as exc:
            print(f"{frame.name}: {exc}", file=sys.stderr)
            continue
        path = out / f"{args.symbol.upper()}_{frame.name}.png"
        path.write_bytes(base64.b64decode(bundle.image.data_b64 or ""))
        print(
            f"{frame.name:<5} {bundle.window.bars:>4} bars  "
            f"{len(bundle.levels)} levels  {path}"
        )
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    config = _config(args)
    pipeline = VisionPipeline(config)
    try:
        report, bundles = pipeline.analyze(
            args.symbol.upper(),
            as_of=_parse_time(args.as_of),
            timeframes=args.timeframes.split(",") if args.timeframes else None,
        )
    except DataUnavailable as exc:
        print(f"no data: {exc}", file=sys.stderr)
        return 2

    if not args.no_store:
        store = RunStoreImpl(config.runs_path, config.index_db_path)
        path = store.save(report, bundles)
        if not args.json:
            print(f"saved to {path}\n")
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, default=str))
    else:
        print_report(report)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from ...evaluation import replay

    config = _config(args)
    start, end = _parse_time(args.start), _parse_time(args.end)
    if start is None or end is None:
        print("--start and --end are required", file=sys.stderr)
        return 2

    result = replay(
        args.symbol.upper(), start, end, args.step, config=config, save=not args.no_store
    )
    frame = result.to_frame()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(args.out, index=False)
        print(f"wrote {len(frame)} outcomes to {args.out}")

    print(f"\n{len(result.reports)} reports, {len(result.failures)} failures\n")
    summary = result.summary
    if summary.empty:
        print("nothing to summarise")
    else:
        print(summary.to_string())
        print(
            "\nsigned return is the forward return multiplied by the direction the state "
            "asserted. Positive means the call was right on average."
        )
    for failure in result.failures[:10]:
        print(f"  failed: {failure}", file=sys.stderr)
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    config = _config(args)
    store = RunStoreImpl(config.runs_path, config.index_db_path)
    runs = store.list_runs(limit=args.limit, symbol=args.symbol, state=args.state)
    if args.json:
        print(json.dumps(runs, indent=2))
        return 0
    if not runs:
        print("no runs stored yet")
        return 0
    print(f"{'run':<34} {'symbol':<10} {'state':<16} {'conf':>5}  {'model':<24} created")
    for run in runs:
        print(
            f"{run['run_id']:<34} {run['symbol']:<10} {run['state']:<16} "
            f"{(run['confidence'] or 0):>5.2f}  {(run['model'] or ''):<24} {run['created_at'][:19]}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    config = _config(args)
    store = RunStoreImpl(config.runs_path, config.index_db_path)
    report = store.load(args.run_id)
    if report is None:
        print(f"no run {args.run_id}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, default=str))
    else:
        print_report(report)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    config = _config(args)
    host = args.host or config.api.host
    port = args.port or config.api.port
    print(f"serving on http://{host}:{port}  (docs at /docs)")
    uvicorn.run(
        "vmi.interfaces.api.app:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level=config.logging.level.lower(),
    )
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    """Launch the Streamlit console, which starts the API itself if needed."""
    import subprocess

    app_path = PROJECT_ROOT / "ui" / "streamlit_app" / "app.py"
    command = [sys.executable, "-m", "streamlit", "run", str(app_path)]
    if args.port:
        command += ["--server.port", str(args.port)]
    return subprocess.call(command)


def cmd_reindex(args: argparse.Namespace) -> int:
    config = _config(args)
    store = RunStoreImpl(config.runs_path, config.index_db_path)
    print(f"indexed {store.rebuild_index()} runs")
    return 0


# ---------------------------------------------------------------------- parser


def _common_flags(suppress: bool) -> argparse.ArgumentParser:
    """The flags every command accepts, before or after the command name.

    `vmi --provider stub analyze EURUSD` and `vmi analyze EURUSD --provider stub`
    are both things people type, and both should work. The sub-parser copies use
    `SUPPRESS` as their default, so an unspecified flag leaves whatever the
    top-level parser already set rather than overwriting it with a default.
    """
    parser = argparse.ArgumentParser(add_help=False)
    hide = {"default": argparse.SUPPRESS} if suppress else {}
    parser.add_argument("--config", help="path to a YAML config", **hide)
    parser.add_argument(
        "--provider", help="ollama | openrouter | openai_compatible | stub", **hide
    )
    parser.add_argument("--model", help="vision model id", **hide)
    parser.add_argument("--data", help="market data: yahoo | metatrader | csv", **hide)
    parser.add_argument("-v", "--verbose", action="store_true", **hide)
    parser.add_argument("--json", action="store_true", help="machine-readable output", **hide)
    return parser


def build_parser() -> argparse.ArgumentParser:
    shared = _common_flags(suppress=True)
    parser = argparse.ArgumentParser(
        prog="vmi",
        parents=[_common_flags(suppress=False)],
        description=(
            "Visual Market Intelligence - multi-timeframe chart analysis by a vision model."
        ),
    )
    parser.add_argument("--version", action="version", version=f"vmi {__version__}")

    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "health", parents=[shared], help="check the vision backend and configuration"
    )
    commands.add_parser(
        "models", parents=[shared], help="list installed and recommended models"
    )
    commands.add_parser(
        "reindex", parents=[shared], help="rebuild the run index from the run directories"
    )

    charts = commands.add_parser("charts", parents=[shared], help="render the timeframe ladder to PNGs")
    charts.add_argument("symbol")
    charts.add_argument("--out", help="output directory")
    charts.add_argument("--as-of", dest="as_of", help="draw the market as it looked then")

    analyze = commands.add_parser("analyze", parents=[shared], help="run the full pipeline")
    analyze.add_argument("symbol")
    analyze.add_argument("--as-of", dest="as_of", help="ISO timestamp; later bars are dropped")
    analyze.add_argument("--timeframes", help="comma-separated subset, e.g. H4,H1")
    analyze.add_argument("--no-store", action="store_true", help="do not persist the run")

    replay_command = commands.add_parser("replay", parents=[shared], help="walk history and score the calls")
    replay_command.add_argument("symbol")
    replay_command.add_argument("--start", required=True)
    replay_command.add_argument("--end", required=True)
    replay_command.add_argument("--step", default="24h")
    replay_command.add_argument("--out", help="write per-run outcomes to this CSV")
    replay_command.add_argument("--no-store", action="store_true")

    runs = commands.add_parser("runs", parents=[shared], help="list stored runs")
    runs.add_argument("--symbol")
    runs.add_argument("--state")
    runs.add_argument("--limit", type=int, default=25)

    show = commands.add_parser("show", parents=[shared], help="print one stored report")
    show.add_argument("run_id")

    serve = commands.add_parser("serve", parents=[shared], help="run the HTTP API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true")

    console = commands.add_parser("console", parents=[shared], help="run the Streamlit console")
    console.add_argument("--port", type=int)

    return parser


HANDLERS = {
    "health": cmd_health,
    "models": cmd_models,
    "charts": cmd_charts,
    "analyze": cmd_analyze,
    "replay": cmd_replay,
    "runs": cmd_runs,
    "show": cmd_show,
    "serve": cmd_serve,
    "console": cmd_console,
    "reindex": cmd_reindex,
}


def main(argv: list[str] | None = None) -> int:
    # A Windows console defaults to cp1252 and raises on any non-Latin-1
    # character in a report. The reports are ours, so widen the pipe instead of
    # narrowing the vocabulary.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
