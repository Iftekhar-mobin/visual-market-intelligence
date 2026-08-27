# Methodology

Why this system is built the way it is, and what would have to be true for its
output to be worth anything.

---

## 1. The question

> Can a vision-language model read a price chart well enough to be a useful
> *specialist opinion* inside a quantitative system — and can that opinion be
> measured?

Two halves, and the second is the harder one. Plenty of demos hand a chart to a
model and print whatever comes back. This project is arranged so that the answer
can be scored against what the market actually did, and so that the answer can
be **no**.

The design commitments that follow all serve that: determinism, versioning,
`as_of` discipline, structured output, and a rule-based floor to measure against.

---

## 2. Why charts at all

A numerical agent already has the OHLCV series, and any indicator it wants. So
what does a picture add?

**Spatial relationships that are awkward as features.** "Price is compressing
into the apex of a triangle whose upper boundary has been touched four times" is
a sentence about geometry. It is expressible numerically — everything is — but
each such pattern needs its own detector, its own parameters and its own
maintenance. A vision model reads the geometry directly.

**Context a feature vector loses.** The same RSI value means different things
below a two-month resistance shelf and in open air above it. A chart carries the
neighbourhood; a row of features carries the point.

**The honest counterweight:** a vision model also *hallucinates* geometry, reads
axes badly, and is confidently wrong about numbers. Every one of the guards in
section 4 exists because of that, and the evaluation in section 8 exists to find
out whether what is left is worth the runtime.

---

## 3. Why three timeframes, and why they are independent

One chart cannot answer "should I be in this market" and "should I enter now".
Those are different questions with different horizons, and a single analyst asked
both will collapse them.

| Rung | Role | Question | Allowed to propose a trade? |
|---|---|---|---|
| H4 | context | What is the structure? | **No** — schema forces `NO_SETUP` |
| H1 | setup | Is there a trade here? | Yes |
| M15 | entry | Is it confirmed now? | Refinement only |

Each analyst sees exactly one chart and nothing else. It is not told what the
higher timeframe concluded.

That independence is not an implementation convenience, it is the point. An
analyst told "the H4 is bullish" will find bullishness in the H1 — models are
agreeable. Three agreeing agents that were never independent are one agent
wearing three hats, and their agreement carries no information. Independent
readings that agree carry a great deal.

The consequence is that **conflict is measurable**. When H4 says bullish and H1
says bearish, that is a genuine disagreement between two separate observations,
and the pipeline can price it (a conflict penalty, a lower confidence, sometimes
`NO_TRADE`) rather than hide it.

---

## 4. Making a chart legible to a model

The renderer is the least glamorous and most consequential module in the project.

**Determinism.** Frozen palette, fixed layout, no randomness, no creation
timestamp in the PNG metadata. Identical bars give identical bytes. The version
string `chart-v1` is stored with every run; any visual change bumps it, because a
report produced against a different picture is not comparable with earlier ones.

**Prices printed inside the plot.** Eight labelled horizontal rules across the
price panel. A model asked to read a level off an axis has to interpolate
between ticks, which it does badly; a model asked to read the nearest printed
number does well. This single choice does more for level accuracy than any
prompt wording tried.

**Index-positioned bars.** Weekends in FX and overnight gaps in equities leave
holes on a time axis, and models read holes as structure. Bars are drawn at
integer positions and the date labels are placed underneath.

**Levels drawn, not just described.** Swing-pivot support and resistance are
computed from the bars, clustered within a fraction of ATR, and drawn with their
prices labelled. They give the model something to anchor to — and give the
pipeline a second opinion to fall back on.

**A minimum candle body.** A doji has zero height and would vanish. It is drawn
at a visible floor instead.

**Volume that says when it is absent.** Free FX feeds have no volume. An empty
panel would read as "volume collapsed"; the panel says "volume not provided by
this feed".

---

## 5. Grounding: how much do you tell the model?

`vision.grounding` has three settings, and the choice is methodological rather
than cosmetic.

| Setting | The prompt carries | Use it for |
|---|---|---|
| `none` | nothing but the image | the purest test of chart reading |
| `window` (default) | symbol, timeframe, and the exact price band drawn | ordinary use |
| `full` | plus computed levels and the last indicator values | a grounded ceiling, and the `stub` backend |

`window` is the default because everything it states is *already printed on the
chart* — it is a legibility aid, not a hint, and it lets the parser reject a
price the model could not have seen.

`full` raises level accuracy and lowers the value of the exercise: a model given
the indicator values can produce a plausible report without looking at the
picture at all. That is exactly what the `stub` backend does, on purpose. **Any
write-up of results at `full` grounding must say so**, or it is claiming visual
skill it has not demonstrated.

---

## 6. Why only three agents use a model

Perception needs a model. Arbitration does not.

"The context and setup timeframes disagree, so subtract 0.25 from the score" is
a rule. Written in Python it can be read, argued with, changed in one place, and
replayed identically a year from now. Asked of a language model on every run it
becomes an opinion that drifts with temperature, model version and phrasing —
and when the score changes you cannot tell whether the market changed or the
model did.

So the split is:

```
perception   ->  vision model     what does this chart show
arbitration  ->  deterministic    what follows from three such readings
```

The practical benefit shows up in replay: two runs with the same three
observations produce byte-identical opportunities, so any difference between two
replays is attributable to the *perception* layer, which is the layer under
study.

---

## 7. Scoring an opportunity

Each direction gets four components in [0, 1], each traceable to a specific
reading:

| Component | Default weight | Source |
|---|---|---|
| alignment | 0.35 | do the timeframes agree with this direction |
| setup confidence | 0.30 | does the setup timeframe see this setup, how surely |
| entry confirmation | 0.20 | is the entry timeframe confirming right now |
| structure quality | 0.15 | context trend strength × regime suitability |

`score = Σ wᵢ · componentᵢ − conflict_penalty (if CONFLICTING)`

Then:

- both scores below `min_score_to_watch` (0.45) → **`NO_TRADE`**
- both above it and within 0.08 of each other → **`WAIT`** (a coin toss is not a
  choice)
- otherwise the better side becomes **`WATCH_*`**
- and **`*_TRIGGERED`** only if the score clears `min_score_to_trigger` (0.65),
  the entry timeframe confirms, **and price is actually at the entry zone** —
  a strong idea three percent above its pullback zone is not a fill anyone can get

The weights live in `configs/default.yaml`. Changing them is a config edit and a
replay, which is how a tuning decision should be made.

### Prices are computed, not quoted

The model supplies the reading; the arithmetic of an entry zone, an invalidation
and a reward/risk ratio is done here against the actual bars, anchored to a level
that exists in the series and sized in ATR:

- **entry zone** — from the nearest level to half an ATR past it
- **invalidation** — half an ATR beyond that level, on the wrong side
- **targets** — the next agreed zones; if there are none, 1.5 and 2.5 ATR
  projections, explicitly labelled as inferred
- **reward/risk** — first target against invalidation, from the middle of the zone

A model asked to do this arithmetic will occasionally place a stop above the
entry on a long and be entirely confident about it.

### The risk agent's veto

An opportunity is rejected outright — state forced to `NO_TRADE`, reason
recorded — when it has no invalidation price, when reward/risk is under 0.8, or
when two or more timeframes produced no reading. An idea that cannot be proved
wrong at a price is an opinion, not a trade.

---

## 8. Not fooling yourself

**The cut-off.** `as_of` truncation happens in exactly one function,
`market_data.base.apply_as_of`, and every provider routes through it. Bars are
right-stamped everywhere (the bar labelled 12:00 covers 08:00–12:00 and is
complete only once 12:00 has passed), so "drop everything after the cut" is
unambiguous, and a bar the cut falls inside is dropped rather than half-used.
Resampling is right-closed and right-labelled for the same reason — left-labelling
a 4-hour bar would place its information four hours before it existed.

**The touch test.** Scoring uses the report's *own* published target and
invalidation. When one bar touches both, the invalidation wins: from an OHLC bar
you cannot tell which came first, and a scorer that guesses in its own favour is
how a system talks itself into believing it works.

**Both directions, always.** The report always describes the long and the short
case. A system that only writes down the side it likes cannot be caught being
wrong.

**The floor.** The `stub` backend applies moving-average and oscillator rules to
the same data and produces the same report shape. A vision model that cannot beat
it on replayed history is not earning its runtime, and `vmi replay --provider
stub` gives you that comparison directly.

**Provenance on everything.** Model, provider, prompt version, chart version,
config digest and `as_of` are stored with every run. Results from different
prompt or chart versions are different experiments and are not pooled.

---

## 9. Known limitations

- **Confidence is not a probability.** It is a weighted average of agent
  agreement. Calibrating it against outcomes on your own instrument is work this
  project makes possible and does not do for you.
- **Small models read numbers badly.** Mitigated by printed prices, level
  validation and swing-point fallbacks; not eliminated. `rejected_levels` in
  every report tells you how often it happened.
- **Free intraday history is short.** Yahoo keeps ~60 days of 15-minute bars and
  ~730 of hourly, which caps how far back a replay can reach. MetaTrader or CSV
  exports remove the limit.
- **No news, no calendar, no positioning.** The system sees a picture of prices.
  A central bank meeting is invisible to it, and the report says so.
- **One symbol at a time.** No cross-asset context, no correlation, no regime
  spillover. Those belong to the numerical system this one plugs into.
- **The evaluation is not a backtest.** Forward returns are not P&L. There is no
  sizing, no cost model and no compounding, and a positive `mean_signed_20` is
  evidence the states mean something, not evidence anyone would have made money.
