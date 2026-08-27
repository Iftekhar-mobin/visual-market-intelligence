# From one command to one answer

### A complete walkthrough of how this project runs

You type:

```bash
vmi analyze EURUSD
```

Ninety seconds later a report appears. This document is everything that happens
in between, in order, with the name of the file doing it. Nothing is skipped and
nothing is hand-waved.

## How to read this book

Each chapter is one stage. The heading names the file. If you only want the shape
of the thing, read Chapter 1 and Chapter 18. If you are about to change
something, read the chapter that owns it.

---

## Chapter 1. The shape of the whole thing

Six stages:

```
1. read your instructions          cli/main.py
2. resolve the configuration       config.py
3. get the bars                    infrastructure/market_data/
4. draw the charts                 infrastructure/charts/
5. read the charts                 application/agents/timeframe_analyst.py  ×3
6. reason about the readings       structure -> opportunity -> risk -> report
   then store and print            persistence/run_store.py, cli/main.py
```

Stage 5 is the only one that involves a model. Everything after it is
arithmetic and rules, which is why the same three readings always produce the
same report.

---

## Chapter 2. `vmi` — how a word becomes a program

`pyproject.toml` declares:

```toml
[project.scripts]
vmi = "vmi.interfaces.cli.main:main"
```

At install time a small launcher lands in your environment's `Scripts/` (or
`bin/`) directory. Typing `vmi` runs it; it imports `vmi.interfaces.cli.main` and
calls `main()`. `python -m vmi.interfaces.cli.main` does the same thing without
the launcher.

---

## Chapter 3. `cli/main.py` — reading your instructions

### 3.1 Working out what you asked for

`build_parser()` constructs an `argparse` parser with global flags (`--provider`,
`--model`, `--data`, `--config`, `--json`, `-v`) and one sub-parser per command.
`analyze` adds `symbol`, `--as-of`, `--timeframes` and `--no-store`.

`main()` first widens the output pipe:

```python
for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        stream.reconfigure(encoding="utf-8")
```

A Windows console defaults to cp1252 and raises on the first non-Latin-1
character in a report. The reports are ours, so the pipe is widened rather than
the vocabulary narrowed.

Then it dispatches through the `HANDLERS` dict to `cmd_analyze`.

### 3.2 Turning the logging on

`_config(args)` builds the configuration (next chapter) and calls
`configure_logging`, which attaches one stderr handler to the `vmi` logger. Every
`log.info` you see during a run comes from there. The Streamlit console attaches
a second handler to the same logger to mirror those lines into the page.

### 3.3 Handing over

```python
pipeline = VisionPipeline(config)
report, bundles = pipeline.analyze(symbol, as_of=..., timeframes=...)
```

Two objects come back: the report, and the chart bundles. The bundles come back
because the caller is usually about to store or display the images, and
re-rendering them would be slow and — across a renderer version bump — wrong.

---

## Chapter 4. `config.py` — one place for every setting

### 4.1 What is on disk

`configs/default.yaml` holds the whole configuration in one screen: the vision
backend, the data feed, the API, storage, the timeframe ladder, the chart style,
the opportunity weights, the evaluation horizons.

### 4.2 What it becomes

`load_config()`:

1. reads `.env` if present (never overriding a real environment variable),
2. parses the YAML,
3. overlays every `VMI_<SECTION>__<FIELD>` variable,
4. applies any per-call overrides (`load_config(vision={"provider": "stub"})`),
5. validates the whole tree with pydantic.

Every model in that tree is `extra="forbid"`, so a typo in the YAML is an error
at startup rather than a setting that silently does nothing.

### 4.3 The digest

`config_digest(config)` hashes the validated tree — minus the API key, which
changes nothing about the output — into twelve hex characters. It is stored with
every run, and two runs with different digests are not directly comparable.

---

## Chapter 5. `orchestration/pipeline.py` — the conductor takes over

`VisionPipeline.__init__` builds everything once:

```python
self.vision  = build_vision_model(config)     # the backend named in the config
self.data    = build_provider(config)         # the feed named in the config
self.renderer = ChartRendererImpl(config.chart, config.data.max_bars)

self.preprocess  = ChartPreprocessingAgent(config, self.data, self.renderer)
self.analysts    = {frame.name: TimeframeAnalyst(frame.role, self.vision, config) …}
self.structure   = StructureAgent()
self.opportunity = OpportunityAgent(config)
self.risk        = RiskAgent(config)
self.reporter    = ReportAgent()
```

`analyze()` then mints a run id — `EURUSD-20260827T161203-878b14`, symbol,
timestamp, six random hex — and works down the ladder.

---

## Chapter 6. Stage one — getting the bars

### 6.1 The request

`ChartPreprocessingAgent.run` asks the feed for each rung:

```python
bars = provider.fetch(symbol, frame.interval, frame.lookback, as_of)
```

For H4 that is `("EURUSD", "4h", "180d", None)`.

### 6.2 What Yahoo can and cannot do

`YahooProvider.fetch` translates `EURUSD` to `EURUSD=X` (`BTCUSD` → `BTC-USD`,
`XAUUSD` → `GC=F`; anything already carrying Yahoo punctuation passes through
untouched). Then it hits two facts:

- Yahoo has **no 4-hour bars**. `_base_interval("4h")` returns `"1h"`, and the
  hourly series is aggregated afterwards.
- Yahoo keeps only ~729 days of hourly data, measured from *today*. The requested
  window is clamped to that, and a cursor older than the limit raises a readable
  `DataUnavailable` rather than returning an empty frame that looks like a bad
  symbol.

The response is cached to `data/cache/` as CSV with a 15-minute TTL, because a
chart is redrawn often and free feeds rate-limit.

### 6.3 Cleaning it

`canonicalise()` lower-cases the columns, coerces them to numbers, adds a zero
`volume` column if the feed has none, and forces a sorted, de-duplicated,
tz-aware UTC index. Every provider returns exactly this shape, which is what
keeps the rest of the system feed-agnostic.

### 6.4 Aggregating

`resample(frame, "4h")` is right-labelled and right-closed:

```python
frame.resample("14400s", label="right", closed="right").agg(
    {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
```

The bar stamped 12:00 covers 08:00–12:00 and is complete only once 12:00 has
passed. Left-labelling would place that information four hours before it existed
— the quiet way a replay starts leaking the future.

### 6.5 The cut

```python
kept = frame[frame.index <= cutoff]
```

`apply_as_of` is the single most important function in the data layer. With
`as_of=None` (a live run) it returns the frame untouched. With a cursor it drops
every bar after it, and — because bars are right-stamped — a bar the cursor falls
inside closes *after* the cursor and is dropped too. Every provider routes
through this one function, so there is exactly one place to audit.

---

## Chapter 7. Stage two — five columns become twenty

`indicators.enrich(frame, wanted)` adds only what this rung asked for:

| Name | Columns | Formula |
|---|---|---|
| `ema9/20/50/200` | `ema9`… | `ewm(span, adjust=False)` |
| `bbands` | `bb_upper/middle/lower` | 20-period SMA ± 2 population σ |
| `rsi` | `rsi` | Wilder's, `ewm(alpha=1/14)` |
| `macd` | `macd`, `macd_signal`, `macd_hist` | 12/26 EMA difference, 9 EMA signal |
| `atr` | `atr` | Wilder-smoothed true range |
| `volume` | `volume_sma` | 20-period SMA |

Every function is causal: the value at bar *i* uses bars ≤ *i*. There is no TA
library, because each formula is four lines and a dependency that changes a
smoothing convention between minor versions would silently invalidate every
stored run.

**Indicators are computed on the full history, then the window is cut.** An
EMA200 on a 240-bar chart needs the 200 bars before the first one drawn;
computing after the cut would leave it empty.

---

## Chapter 8. Stage three — finding the levels

`levels.detect_levels(data, k=5, atr)`:

1. `swing_points` finds pivots — a bar whose high is the highest of the five bars
   either side, or whose low the lowest. The last five bars can never be a pivot;
   that lag is honest, since an unconfirmed pivot is a guess about the future.
2. Pivots within `0.75 × ATR` of each other are merged into one zone.
3. Zones are scored by how many pivots formed them.
4. Support is taken from lows **below** the last close, resistance from highs
   **above** it — a "support" above the market is a broken level, and calling it
   support would mislead.
5. At most three per side: more and their labels overlap, and a level a model
   cannot read is worse than no level.

---

## Chapter 9. Stage four — drawing the picture

`renderer.ChartRendererImpl._draw` builds four stacked panels in a fixed ratio —
price 5.0, volume 1.1, RSI 1.5, MACD 1.5 — on a frozen dark palette.

Worth knowing:

- **Candles** are a `LineCollection` of wicks plus one `Rectangle` per body, with
  a minimum body height so a doji does not vanish.
- **The price grid** is eight `linspace` ticks across the y-limits, each labelled
  with the price to the instrument's own precision (five decimals for FX, two for
  an index). This is the single most important design decision in the file: it
  turns "read the support level" into "read the nearest printed number".
- **Levels** are dashed lines with their prices annotated inside the plot.
- **The last close** gets a coloured tag on the right edge.
- **The x-axis** is integer positions with date labels underneath, so weekend gaps
  do not appear as structure.
- **Saving** passes `metadata={"Software": "vmi chart-v1"}` and nothing else — no
  creation timestamp, so identical inputs give identical bytes.

What comes back is a `ChartBundle`: the PNG (base64), a `PriceWindow` (symbol,
timeframe, bar count, first and last timestamp, min and max price, last close,
decimal precision), the computed levels, and an `IndicatorSnapshot` of the last
values. The window is the ground truth every later price claim is checked
against.

---

## Chapter 10. Stage five — one analyst, one chart

Repeated three times, independently.

### 10.1 The prompt

`prompts/analyst.py::build_prompt(bundle, role, grounding)` assembles:

1. the task line — *"Analyse this EURUSD H4 chart (role: context)"*,
2. the role brief — context, setup or entry, with the context brief stating
   explicitly that `setup` must be `NO_SETUP`,
3. the chart legend — what the colours, panels and dashed lines mean,
4. the `CHART FACTS` block, whose depth depends on `grounding`,
5. the price-range constraint — *"every price you report must lie between … and
   …; if you cannot read a level, return an empty list rather than a guess"*,
6. the JSON schema, field by field, with the allowed values spelled out.

The system prompt is four rules: report what you see, never invent a price, a
pattern is evidence not a guarantee, answer with one JSON object.

`PROMPT_VERSION` is `analyst-v1` and is stored with every observation. Changing
the wording changes the answers, so a v1 report and a v2 report are different
experiments.

### 10.2 The call

`vision.analyze(image_b64, prompt, system)` goes to whichever backend is
configured:

- **Ollama** — `POST /api/generate` with `images: [b64]`, `format: "json"`,
  `num_ctx: 8192` (the default 2048 truncates the prompt before the model sees
  the instructions).
- **OpenAI-compatible / OpenRouter** — `POST /v1/chat/completions` with a content
  part `{"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}`
  and `response_format: json_object`.
- **Stub** — no call at all; it parses the `CHART FACTS` block and applies rules.

`BaseVisionModel.analyze` wraps this in retries (default 2) and timing. A cold
local model routinely times out on its first request while the weights page in —
that is a slow model, not a broken one.

### 10.3 Getting JSON out of prose

`extract_json` tries three things: parse the whole string; find a fenced
```` ```json ```` block; scan for a balanced `{…}` respecting strings and
escapes. Small models wrap JSON in "Sure! Here's my analysis:" no matter how
firmly the prompt says not to, and failing a ninety-second run over that would be
absurd.

### 10.4 Believing none of it

`agents/parsing.py` turns the payload into a `TimeframeObservation` without
trusting a single field:

- `"Bullish trend"` → `Trend.BULLISH` (exact match, then substring)
- `"75%"`, `0.75`, `"high"` → `0.75`
- `"1.1550"`, `1.155`, `{"price": 1.155}` → `1.155`
- prices outside the drawn window → **rejected**, recorded in `rejected_levels`,
  and noted in `uncertainties`
- a `context` analyst that proposed a trade anyway → forced back to `NO_SETUP`
- levels the model did not report at all → filled from the swing-point levels
  that were drawn on the chart it was shown, with a note saying so

A price outside the window is not a formatting slip. It was not read, it was
remembered or invented, and the report says which.

### 10.5 When the model fails

`degraded_observation` returns a null reading with `degraded: true`, zero
confidence and the error text. The run continues. Two good readings and one blank
is a weaker report; one blank producing no report at all would be worse.

---

## Chapter 11. Stage six — reconciling three readings

`agents/structure.py`, deterministic.

Each observation is weighted by its **role** (context 0.50, setup 0.32, entry
0.18) times its own confidence. The weighted direction sum normalises to a bias.

Alignment is classified:

- all decisive readings agree, none missing → `ALIGNED_BULLISH` / `ALIGNED_BEARISH`
- context and setup point in opposite directions → `CONFLICTING`
- some agree, some are sideways or unclear → `PARTIALLY_ALIGNED`
- nothing decisive → `NEUTRAL`

Note the asymmetry: the *entry* timeframe leaning the other way is not a conflict,
it is what a pullback looks like — and the reason to wait rather than abandon the
idea. Context against setup is a real conflict.

Levels from all three timeframes are clustered within 15 basis points, and
clusters seen on more than one timeframe are ranked first. A level two analysts
found independently is worth more than one that only the fastest chart saw.

Confidence is the weighted mean of the analysts' confidences, multiplied by an
alignment factor (1.0 aligned, 0.8 partial, 0.6 neutral, 0.45 conflicting) and
reduced 20% per missing timeframe.

---

## Chapter 12. Stage seven — both sides of the market

`agents/opportunity.py`, deterministic, and the most opinionated file here.

For each direction, four components in [0, 1]:

```
alignment          0.35   does the ladder agree with this direction
setup_confidence   0.30   does the setup timeframe see this setup, how surely
entry_confirmation 0.20   is the entry timeframe confirming right now
structure_quality  0.15   context trend strength × regime suitability
```

summed with those weights, minus 0.25 if the timeframes conflict.

Then the scenario is **priced against the bars**, not against prose. For a long:

- anchor = nearest support below price
- entry zone = anchor to anchor + 0.6 ATR
- invalidation = anchor − 0.5 ATR, phrased as *"H1 closes below X"*
- targets = the next agreed resistance zones, or 1.5 and 2.5 ATR projections
  explicitly labelled as inferred
- reward/risk = first target against invalidation, from the middle of the zone

A model asked to do this arithmetic will occasionally put the stop above the
entry on a long and be completely confident about it.

Finally the state:

```
best < 0.45                                        -> NO_TRADE
both ≥ 0.45 and within 0.08 of each other          -> WAIT
best ≥ 0.65, entry confirms, price at entry zone   -> LONG/SHORT_TRIGGERED
otherwise                                          -> WATCH_LONG / WATCH_SHORT
```

That last condition matters: a scenario can only be *triggered* where it can
actually be entered. Price three percent above its pullback zone is a strong idea
nobody can act on, and calling it `LONG_TRIGGERED` would hand a consuming system
a fill it will never get.

---

## Chapter 13. Stage eight — the agent allowed to say no

`agents/risk.py` asks whether the idea is *actionable and falsifiable*, not
whether it will work.

It vetoes — forcing `NO_TRADE` and recording the reason — when:

- there is no invalidation price (an idea that cannot be proved wrong at a price
  is an opinion),
- reward/risk is below 0.8 (the first target is barely further than the stop),
- two or more timeframes produced no reading.

It also collects risks that do not veto: elevated volatility as a percentage of
price, a first target inside one ATR, ATR-projected targets, timeframe conflicts,
and any level a model reported that was never on the chart.

---

## Chapter 14. Stage nine — writing it down

`agents/report.py` assembles the `VisionReport`, computes the headline confidence
(`0.65 × structure + 0.35 × best scenario`, reduced per degraded timeframe,
capped at 0.35 if vetoed), sorts the key levels by distance from the market, and
composes the summary paragraph **from the structured fields** — so it can never
say something the data does not.

---

## Chapter 15. Storing it

`persistence/run_store.py` writes:

```
data/runs/EURUSD-20260827T161203-878b14/
  report.json          the whole report, minus the base64 images
  charts/H4.png H1.png M15.png    exactly what each analyst was shown
  agents/H4.raw.txt …             exactly what each model replied
```

and one row in `data/runs/index.db` so the console can list a thousand runs
without opening a thousand files. The index is derived and disposable
(`vmi reindex` rebuilds it); the directories are the truth.

---

## Chapter 16. Printing it

`cli/main.py::print_report` writes the state, the provenance, one line per
timeframe, both scenarios with their conditions and levels, the risks, and the
summary. `--json` prints the whole report instead, for anything downstream.

---

## Chapter 17. The other two doors read the same objects

**The API** (`interfaces/api/app.py`) calls the same `VisionPipeline`, saves
through the same store, and returns `report.model_dump()` plus a flattened `api`
block. Nothing it can do is unavailable from the CLI.

**The console** (`ui/streamlit_app/`) talks to the API over HTTP — it never
imports the pipeline. It renders the state banner, both scenario cards, the
charts beside their readings, the run history, the replay table and the raw HTTP
traffic. If something is awkward to render there, the API is missing something.

---

## Chapter 18. The whole journey on one page

```
vmi analyze EURUSD
   |
   +-- argparse                                       cli/main.py
   +-- load_config  yaml + .env + VMI_*               config.py
   |
   +-- VisionPipeline.analyze()                       orchestration/pipeline.py
        |
        +-- ChartPreprocessingAgent                   agents/preprocess.py
        |     +-- provider.fetch  (as_of cut)         market_data/{yahoo,mt5,csv}.py
        |     +-- enrich          (EMA/BB/RSI/…)      charts/indicators.py
        |     +-- detect_levels   (swing pivots)      charts/levels.py
        |     +-- render          (PNG, versioned)    charts/renderer.py
        |
        +-- TimeframeAnalyst × 3   <-- the only model calls
        |     +-- build_prompt                        prompts/analyst.py
        |     +-- vision.analyze                      vision/{ollama,openai_compatible,stub}.py
        |     +-- extract_json + parse_observation    agents/parsing.py
        |
        +-- StructureAgent      alignment, bias, zones      agents/structure.py
        +-- OpportunityAgent    two scenarios + a state     agents/opportunity.py
        +-- RiskAgent           risks, and the veto         agents/risk.py
        +-- ReportAgent         confidence, levels, prose   agents/report.py
        |
        +-- RunStoreImpl.save   report.json + PNGs + raw    persistence/run_store.py
   |
   +-- print_report                                   cli/main.py
```

---

## Chapter 19. Every file, one line each

| File | What it does |
|---|---|
| `config.py` | the typed configuration tree, YAML + `.env` + env overlay |
| `paths.py` | where things live on disk |
| `logging_utils.py` | logging, and the sink the console subscribes to |
| `domain/models/market.py` | `ChartBundle`, `PriceWindow`, `Level`, `IndicatorSnapshot` |
| `domain/models/analysis.py` | `TimeframeObservation`, `StructureSynthesis`, `Evidence` |
| `domain/models/opportunity.py` | `Scenario`, `RiskAssessment`, `MarketState` |
| `domain/models/report.py` | `VisionReport`, `RunMetadata`, `AgentTrace` |
| `domain/ports.py` | the four protocols that make everything replaceable |
| `market_data/base.py` | canonical frame, resampling, the `as_of` cut, the cache |
| `market_data/yahoo.py` | the free default feed, and its two limitations |
| `market_data/metatrader.py` | real H4 bars and tick volume, Windows only |
| `market_data/csv_files.py` | reproducible history from disk |
| `charts/indicators.py` | EMA, Bollinger, RSI, MACD, ATR, in plain pandas |
| `charts/levels.py` | swing pivots, clustered into support and resistance |
| `charts/renderer.py` | the deterministic, versioned chart |
| `vision/base.py` | retries, timing, and getting JSON out of prose |
| `vision/ollama.py` | the local backend |
| `vision/openai_compatible.py` | every other backend, including OpenRouter |
| `vision/stub.py` | no model at all — plumbing, regression, and a floor |
| `prompts/analyst.py` | every word the model is shown, versioned |
| `agents/preprocess.py` | bars in, charts out |
| `agents/timeframe_analyst.py` | one chart, one reading, failure included |
| `agents/parsing.py` | trusting nothing the model said about numbers |
| `agents/structure.py` | three readings into one view |
| `agents/opportunity.py` | both directions, conditioned and priced |
| `agents/risk.py` | the veto |
| `agents/report.py` | assembly, confidence, the paragraph |
| `orchestration/pipeline.py` | the order, and the provenance |
| `persistence/run_store.py` | files are the truth, SQLite is the index |
| `evaluation/outcomes.py` | what happened next |
| `evaluation/replay.py` | walking history without seeing it |
| `interfaces/api/app.py` | the HTTP surface |
| `interfaces/cli/main.py` | the command line |
| `ui/streamlit_app/*` | the operator console |

---

## Chapter 20. Watch it yourself

```bash
vmi analyze EURUSD --provider stub -v      # every stage, logged, in about four seconds
vmi charts EURUSD --out /tmp/charts        # stop after stage four and look at the pictures
vmi show <run_id> --json | jq .observations[0].raw_text   # what the model literally said
```

The third one is the most useful when something looks wrong. The raw reply is
stored for every timeframe of every run, so "why did it say that" is always
answerable.
