"""The prompts, versioned.

`PROMPT_VERSION` is stored with every run. Changing wording here changes the
answers, so a report written under `analyst-v1` and one written under `v2` are
different experiments and the store keeps them apart.

Three things the prompts are built to prevent, in order of how often they go
wrong with small local models:

1. **Inventing prices.** The model is told the exact price band the chart
   covers and instructed to return `null` rather than a number it cannot read.
   Anything outside the band is rejected downstream and recorded.
2. **Trading on the context chart.** The H4 analyst is given a schema in which
   `setup` may only be `NO_SETUP`. A model that cannot answer "should I buy"
   cannot bias the rest of the chain.
3. **Prose.** Every prompt ends with the schema and an instruction to emit that
   object and nothing else.
"""

from __future__ import annotations

import json
from typing import Any

from ...domain.models import ChartBundle

PROMPT_VERSION = "analyst-v1"

SYSTEM = (
    "You are a disciplined technical chart analyst. You are shown one candlestick "
    "chart and you report only what is visible in it.\n"
    "Rules you never break:\n"
    "- Report what you SEE. Separate observation from interpretation.\n"
    "- Never invent a price. If a level cannot be read from the chart, use null.\n"
    "- A chart pattern is evidence, never a guarantee. Say so when you are unsure.\n"
    "- Answer with one JSON object and nothing else. No prose, no code fence."
)

ROLE_BRIEF = {
    "context": (
        "This is the HIGHER timeframe. Your job is market context only: the trend, "
        "its strength, the regime, the major levels and the two scenarios that could "
        "follow. You must NOT propose a trade. Set \"setup\" to \"NO_SETUP\"."
    ),
    "setup": (
        "This is the PRIMARY trading timeframe. Decide whether a setup is visible: "
        "a pullback, a breakout, a rejection, a continuation or nothing at all. "
        "Classify it as LONG_SETUP, SHORT_SETUP or NO_SETUP, and say which visible "
        "evidence supports the classification. NO_SETUP is a good answer when the "
        "chart is unclear."
    ),
    "entry": (
        "This is the LOWER timeframe. Your job is entry refinement only: short-term "
        "structure, momentum, local levels, and whether an entry in the direction of "
        "the higher timeframe is confirmed or warned against right now. Do not "
        "attempt to overturn the higher-timeframe picture."
    ),
}

SCHEMA: dict[str, Any] = {
    "trend": "bullish | bearish | sideways | unclear",
    "trend_strength": "strong | moderate | weak | none",
    "regime": "trending | ranging | breakout | reversal | consolidation | volatile | unclear",
    "momentum": "accelerating | steady | fading | diverging | unclear",
    "volatility": "expanding | stable | contracting | unclear",
    "structure": "one short phrase, e.g. 'higher highs and higher lows' or null",
    "support": ["numbers read from the chart, nearest first, or []"],
    "resistance": ["numbers read from the chart, nearest first, or []"],
    "patterns": ["short names of visible formations, or []"],
    "setup": "LONG_SETUP | SHORT_SETUP | NO_SETUP",
    "confidence": "0.0 to 1.0",
    "bullish_scenario": "one sentence, or null",
    "bearish_scenario": "one sentence, or null",
    "entry_confirmation": "one sentence, or null",
    "entry_warning": "one sentence, or null",
    "evidence": [
        {
            "kind": "OBSERVED | INFERRED | UNCERTAIN",
            "statement": "what you saw",
            "source": "which part of the chart",
        }
    ],
    "uncertainties": ["anything you could not read clearly, or []"],
}

CHART_LEGEND = (
    "How to read this chart:\n"
    "- Candles are green when the close is above the open, red when below.\n"
    "- Coloured lines are exponential moving averages, named in the legend.\n"
    "- The shaded band is Bollinger(20, 2).\n"
    "- Dashed horizontal lines are support (green, labelled S) and resistance "
    "(red, labelled R) computed from swing points.\n"
    "- The numbers down the left edge of the price panel are exact prices at that "
    "height. Read levels off them.\n"
    "- The coloured tag on the right edge is the last close.\n"
    "- Lower panels are volume, RSI(14) with 30/50/70 rules, and MACD(12,26,9)."
)


def facts_block(bundle: ChartBundle, grounding: str) -> str:
    """The `CHART FACTS` section — the model's anchor, at one of three depths.

    * `none` — nothing. The purest test of chart reading, and the one where a
      weak model most often invents a price.
    * `window` — the symbol, the timeframe and the exact price band drawn. All
      of it is printed on the chart already, so this is a legibility aid rather
      than a hint, and it is the default.
    * `full` — adds the computed levels and the last indicator values. Use it to
      compare a model against its own grounded ceiling, and note in any write-up
      that the numbers were supplied. The `stub` backend requires this level,
      since arithmetic is all it has.
    """
    window = bundle.window
    if grounding == "none":
        return ""

    facts: dict[str, Any] = {
        "symbol": window.symbol,
        "timeframe": window.timeframe,
        "interval": window.interval,
        "bars": window.bars,
        "price_low": round(window.price_min, window.digits),
        "price_high": round(window.price_max, window.digits),
        "last_close": round(window.last_close, window.digits),
        "decimals": window.digits,
    }
    if grounding == "full":
        facts["support"] = [
            round(level.price, window.digits) for level in bundle.levels if level.kind == "support"
        ]
        facts["resistance"] = [
            round(level.price, window.digits)
            for level in bundle.levels
            if level.kind == "resistance"
        ]
        facts["indicators"] = {
            key: (round(value, window.digits) if isinstance(value, float) else value)
            for key, value in bundle.indicators.model_dump().items()
            if value is not None and key not in ("swing_highs", "swing_lows")
        }
    return "CHART FACTS (json):\n" + json.dumps(facts, indent=2) + "\n\n"


def build_prompt(bundle: ChartBundle, role: str, grounding: str = "window") -> str:
    """The full user prompt for one analyst on one chart."""
    window = bundle.window
    brief = ROLE_BRIEF.get(role, ROLE_BRIEF["setup"])
    schema = json.dumps(SCHEMA, indent=2)
    setup_rule = (
        '\nBecause this is the context timeframe, "setup" MUST be "NO_SETUP".'
        if role == "context"
        else ""
    )
    return (
        f"Analyse this {window.symbol} {window.timeframe} chart (role: {role}).\n\n"
        f"{brief}{setup_rule}\n\n"
        f"{CHART_LEGEND}\n\n"
        f"{facts_block(bundle, grounding)}"
        f"Every price you report must lie between {window.price_min:.{window.digits}f} and "
        f"{window.price_max:.{window.digits}f}, which is the range this chart covers. "
        f"If you cannot read a level from the chart, return an empty list rather than a guess.\n\n"
        f"Reply with exactly this JSON object:\n{schema}\n"
    )
