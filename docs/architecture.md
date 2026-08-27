# Architecture

## Layout

```
Vision Agent/
├── configs/default.yaml       the resolved default configuration
├── data/
│   ├── cache/                 feed responses, TTL'd (not version controlled)
│   ├── runs/                  one directory per run + the SQLite index
│   └── samples/               CSV exports the `csv` provider serves
├── docs/                      this document and its siblings
├── ui/streamlit_app/          the operator console
│   ├── app.py                 tabs, sidebar, rendering
│   ├── client.py              HTTP client that records every exchange
│   ├── server.py              starts and supervises the API process
│   └── theme.py               palette, CSS, the card/badge/banner components
└── src/vmi/
    ├── config.py              typed configuration tree, env overlay
    ├── paths.py               filesystem layout
    ├── logging_utils.py       logging, plus the sink the console reads
    ├── domain/                the vocabulary — imports nothing else
    │   ├── models/            chart, analysis, opportunity, report
    │   └── ports.py           MarketDataProvider, ChartRenderer, VisionModel, RunStore
    ├── application/
    │   ├── prompts/           versioned model-facing text (analyst-v1)
    │   ├── agents/            the eight agents
    │   └── orchestration/     the pipeline that sequences them
    ├── infrastructure/
    │   ├── market_data/       yahoo, metatrader, csv + the canonical frame
    │   ├── charts/            indicators, levels, the deterministic renderer
    │   ├── vision/            ollama, openai_compatible, openrouter, stub
    │   ├── persistence/       run store (files + SQLite index)
    │   └── observability/     request ids
    ├── evaluation/            replay and outcome scoring
    └── interfaces/
        ├── api/               FastAPI app and wire schemas
        └── cli/               the `vmi` command
```

## The dependency rule

```
interfaces ──> application ──> domain <── infrastructure
```

Arrows point inwards. `domain` imports nothing from the rest of the package;
`infrastructure` implements protocols defined in `domain/ports.py` without
importing them. That is what makes every outer component replaceable: the agents
are handed something that satisfies `VisionModel` and cannot tell whether the
answer came from a 7B model on a laptop, a free hosted one, or arithmetic.

## Data flow

```
    configs/default.yaml + VMI_* environment
              |
           Config  (typed, validated at load; unknown keys are an error)
              |
    ┌─────────┴───────────────────────────────────────────────────────────┐
    │  VisionPipeline.analyze(symbol, as_of, timeframes)                   │
    │                                                                       │
    │  ChartPreprocessingAgent                                              │
    │      provider.fetch(symbol, interval, lookback, as_of)                │
    │          -> canonical UTC OHLCV frame, bars after as_of dropped       │
    │      indicators.enrich  -> EMA / BB / RSI / MACD / ATR columns        │
    │      levels.detect      -> swing-pivot support and resistance          │
    │      renderer.render    -> ChartBundle (PNG + window + levels + snap) │
    │                                                                       │
    │  for each rung of the ladder, independently:                          │
    │      TimeframeAnalyst(role).run(bundle)                               │
    │          prompt = build_prompt(bundle, role, grounding)               │
    │          raw    = vision.analyze(png_b64, prompt, system)             │
    │          parse  -> TimeframeObservation  (levels validated, degraded  │
    │                     on failure, never raises)                          │
    │                                                                       │
    │  StructureAgent      -> StructureSynthesis  (alignment, bias, zones)  │
    │  OpportunityAgent    -> (long Scenario, short Scenario, MarketState)  │
    │  RiskAgent           -> (RiskAssessment, possibly vetoed MarketState) │
    │  ReportAgent         -> VisionReport (+ confidence, key levels, prose)│
    └──────────────────────────────┬────────────────────────────────────────┘
                                   │
                          VisionReport + bundles
                                   │
                RunStoreImpl.save  ->  data/runs/<run_id>/
                                   │
              ┌────────────────────┼────────────────────┐
          vmi CLI            HTTP API             Streamlit console
```

Both entry points call the same `VisionPipeline` and read the same artefacts.
The console talks to the API over HTTP like any other consumer — it never
imports the pipeline — so anything awkward to render is a gap in the API rather
than a gap in the UI.

## The eight agents

| Agent | Sees a chart? | In | Out |
|---|---|---|---|
| `ChartPreprocessingAgent` | — | symbol, ladder, `as_of` | `{timeframe: ChartBundle}` |
| `TimeframeAnalyst(context)` | **yes** | H4 bundle | `TimeframeObservation` (setup forced to `NO_SETUP`) |
| `TimeframeAnalyst(setup)` | **yes** | H1 bundle | `TimeframeObservation` with `LONG_SETUP`/`SHORT_SETUP`/`NO_SETUP` |
| `TimeframeAnalyst(entry)` | **yes** | M15 bundle | `TimeframeObservation` with confirmation / warning |
| `StructureAgent` | — | three observations | `StructureSynthesis` |
| `OpportunityAgent` | — | observations, synthesis, bundles | two `Scenario`s and a `MarketState` |
| `RiskAgent` | — | scenarios, state, bundles | `RiskAssessment`, possibly a vetoed state |
| `ReportAgent` | — | everything above | `VisionReport` |

Three call a model; five are deterministic. See
[methodology.md](methodology.md#why-only-three-agents-use-a-model) for why.

## Module contracts

| Module | Input | Output |
|---|---|---|
| `market_data.*` | symbol, interval, lookback, `as_of` | UTC OHLCV frame, oldest first |
| `charts.indicators` | OHLCV frame, indicator names | the same frame plus indicator columns |
| `charts.levels` | OHLCV frame, ATR | `list[Level]` split into support and resistance |
| `charts.renderer` | frame, symbol, timeframe | `ChartBundle` (PNG, window, levels, snapshot) |
| `vision.*` | base64 PNG, prompt, system | raw model text |
| `agents.parsing` | model JSON, `PriceWindow` | `TimeframeObservation`, never raising |
| `agents.structure` | observations | `StructureSynthesis` |
| `agents.opportunity` | observations, synthesis, bundles | scenarios and state |
| `agents.risk` | scenarios, state | assessment, possibly vetoed state |
| `persistence.run_store` | `VisionReport`, bundles | files on disk + an index row |
| `evaluation.outcomes` | report, future bars | `Outcome` |

Every stage consumes and returns plain pydantic models or pandas objects, so any
one of them can be driven from a notebook without the rest.

## Run artefacts

```
data/runs/
  index.db                                  derived, disposable, rebuildable
  EURUSD-20260827T161203-878b14/
    report.json                             the whole VisionReport
    charts/H4.png  H1.png  M15.png          exactly what each analyst was shown
    agents/H4.raw.txt  H1.raw.txt  …        exactly what each model replied
```

The directories are the truth; the SQLite index exists so the console can list a
thousand runs without opening a thousand files. `vmi reindex` rebuilds it from
the directories.

Each report carries its own provenance — model, provider, prompt version, chart
version, config digest, `as_of` — because a stored report whose origin is unknown
cannot be scored against what the market did next.

## Extension points

**A new vision backend.** Subclass `BaseVisionModel`, implement `_call`, add it
to `PROVIDERS` in `infrastructure/vision/__init__.py`. It immediately appears in
the CLI, the API and the console's provider list. An OpenAI-compatible vendor is
usually a subclass of `OpenAICompatibleVisionModel` with a different base URL.

**A new price feed.** Write a class with `name` and
`fetch(symbol, interval, lookback, as_of)` returning a canonical frame, and add a
branch to `build_provider`. Route the `as_of` cut through
`market_data.base.apply_as_of` — that is not optional, it is what keeps replay
honest.

**A different timeframe ladder.** Edit `timeframes` in the config. Weights come
from `role`, so D1/H4/M30 behaves exactly like H4/H1/M15.

**A new indicator.** Add the function to `charts/indicators.py`, its column names
to `INDICATOR_COLUMNS`, a branch to `enrich`, and drawing to the renderer. Bump
`CHART_VERSION`: a chart that looks different produces different answers, and
runs across a version boundary are not comparable.

**Different scoring.** The four weights, the watch and trigger thresholds and the
conflict penalty are all in `configs/default.yaml`. Tuning them is a config
change and a replay, not a code change.

**A new agent.** Add it under `application/agents/`, give it a `name` and a `run`
returning `AgentResult`, and wire it into `VisionPipeline`. Its trace is picked
up automatically and shows in the console's Processing tab.

## Design notes

**Charts are drawn outside the vision agents.** A vision agent that fetched its
own data and drew its own chart could not be replayed, and "the model saw a
different picture" would become an untestable explanation for any disagreement.

**The renderer is versioned and deterministic.** No timestamps in the PNG
metadata, no randomness, a frozen palette. Identical inputs give identical bytes,
and the hash is stored with the run.

**Failure is a path, not an exception.** A model that times out produces a
*degraded* observation with zero confidence; the run continues with two readings
instead of three, the synthesis discounts it, and the report says which timeframe
went missing and why. Only "no chart at all" aborts the run.

**Observation, inference and uncertainty stay separate.** `Evidence.kind` is
`OBSERVED`, `INFERRED` or `UNCERTAIN` from the model's own mouth through to the
report. Collapsing them would make "the candles made a lower high" and "this
looks like a bull flag" the same kind of fact, and they are not.
