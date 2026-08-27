# Evaluation

A system that produces confident prose about charts is easy. A system whose
confident prose can be checked is the point of this repository.

---

## The claim being tested

The report asserts a **state**. Each state carries a direction:

| State | Direction asserted |
|---|---|
| `WATCH_LONG`, `LONG_TRIGGERED` | +1 |
| `WATCH_SHORT`, `SHORT_TRIGGERED` | −1 |
| `WAIT`, `NO_TRADE` | 0 |

If the states mean anything, forward returns after `WATCH_LONG` should differ
from forward returns after `WATCH_SHORT` in the direction each asserted. If they
do not, the system is not seeing what it claims to see, and no amount of
well-formed JSON changes that.

---

## Running a replay

```bash
vmi replay EURUSD --start 2026-07-01 --end 2026-08-25 --step 24h --out reports/eurusd.csv
```

At each cursor the pipeline is asked for a report **as if it were then**: the
feed truncates at the cursor, the charts are drawn from truncated data, and no
agent is handed a bar that had not closed. Afterwards — and only afterwards — the
full series is fetched so outcomes can be scored.

| Flag | Meaning |
|---|---|
| `--step` | how often to ask. `24h` is one call per day of history |
| `--out` | write per-run outcomes to CSV |
| `--no-store` | do not persist the runs (they are stored by default, and you want that) |
| `--provider` | which backend to replay with — this is how you compare models |

Cost: three vision calls per cursor. Fifty cursors against a local 7B model is a
few hours; against a free hosted model, under an hour. Runs are saved as they
complete, so losing the process at hour three loses nothing but the summary.

---

## What is measured

For each report, against the entry-timeframe bars that followed:

**Forward return** at each horizon in `evaluation.horizons` (default 10, 20 and
50 bars), and the same figure multiplied by the asserted direction —
`signed_return`. Positive means the call was right on average.

**MFE and MAE** — the furthest the price ran in favour and against, over the
longest horizon, in the direction the state asserted. A state with a good mean
return and a terrible MAE was right eventually and unbearable in the meantime.

**The touch test** — the honest one. Using the report's **own** published first
target and **own** published invalidation price, walking bar by bar:

- invalidation touched first → `target_first: false`
- target touched first → `target_first: true`
- one bar touches both → **invalidation wins**

That last rule matters. From an OHLC bar you cannot tell which side was hit
first, and a scorer that resolves the ambiguity in its own favour is how a system
talks itself into believing it works.

---

## Reading the summary

```
                runs  mean_signed_20  median_signed_20  target_first_rate  mean_confidence  share
state
WATCH_LONG         8          0.0391            0.0347              0.857           0.6328  0.727
LONG_TRIGGERED     1         -0.0462           -0.0462              0.000           0.5890  0.091
NO_TRADE           1          0.0000           -0.0000                NaN           0.4160  0.091
WATCH_SHORT        1          0.0231            0.0231              1.000           0.5170  0.091
```

Questions worth asking of a table like this, in order:

1. **Is `share` sane?** A system that says `WATCH_LONG` 90% of the time is a
   permabull with extra steps. Healthy output has substantial `NO_TRADE`.
2. **Do the directional states separate?** `mean_signed_20` positive for both long
   and short states is the minimum bar. Equal-and-opposite is a directional
   forecast in disguise, not chart reading.
3. **Is `target_first_rate` above chance for the sample?** With targets typically
   further than stops, a rate below 50% can still be profitable and a rate above
   50% can still lose — read it beside the reward/risk in the reports.
4. **Does confidence sort the outcomes?** Bucket by `confidence` and check that the
   high bucket beats the low one. If it does not, the confidence number is
   decoration.
5. **How many runs?** Eleven cursors is an anecdote. Do not put a number in a
   presentation until it has a few hundred behind it.

---

## Comparing against the floor

```bash
vmi replay EURUSD --start 2026-07-01 --end 2026-08-25 --provider stub   --out reports/stub.csv
vmi replay EURUSD --start 2026-07-01 --end 2026-08-25 --provider ollama --out reports/vlm.csv
```

Same charts, same rules after the analysts, same scoring. The only difference is
whether the readings came from a picture or from arithmetic on the indicators.
If the vision model does not beat the stub, what it is adding is latency.

---

## Comparing models, prompts or charts

Change one thing at a time and keep the provenance:

| Changed | Bump | Why |
|---|---|---|
| the model | nothing | provenance already records provider and model per run |
| prompt wording | `PROMPT_VERSION` in `application/prompts/analyst.py` | different text is a different experiment |
| anything visual | `CHART_VERSION` in `infrastructure/charts/renderer.py` | a different picture is a different experiment |
| config weights | nothing (the digest changes automatically) | `config_digest` separates them |

Every run stores all four. Do not pool results across a version boundary; the
store makes it easy not to.

---

## What this is not

**It is not a backtest.** No sizing, no costs, no slippage, no compounding, no
portfolio. A positive `mean_signed_20` says the states carry information. It does
not say anyone would have made money, and the gap between those two statements
has ended more strategies than bad signals have.

To take it further you would price the published entry, stop and targets through
an execution model with spread and commission — which is exactly the job of the
quantitative system this one plugs into, and exactly why the report publishes
levels rather than opinions.

**It is not out-of-sample in the way a model study is.** The vision model was
trained on data that includes financial charts, and possibly on the very
instrument you are replaying. Replaying 2019 proves less than replaying last
month. Prefer recent windows, and prefer forward-testing over both.

---

## Doing it from Python

```python
from datetime import datetime, timezone
from vmi.config import load_config
from vmi.evaluation import replay

config = load_config(vision={"provider": "ollama", "model": "qwen2.5vl:7b"})
result = replay(
    "EURUSD",
    datetime(2026, 7, 1, tzinfo=timezone.utc),
    datetime(2026, 8, 25, tzinfo=timezone.utc),
    step="24h",
    config=config,
)

print(result.summary)                  # the table above
frame = result.to_frame()              # one row per call
frame[frame.state == "WATCH_LONG"].signed_20.describe()
```

`result.reports` holds every `VisionReport`, so anything the summary does not
answer can be asked directly of the structured output.
