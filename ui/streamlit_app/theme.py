"""The console's look: one palette, one set of components, no inline styling.

Streamlit's defaults are fine for a notebook and wrong for an operator console —
a dashboard someone stares at for an hour needs a dark ground, a single accent
per meaning, and state that is legible at a glance from across a desk. Everything
visual lives here so the tabs stay about content.

The palette is the chart renderer's palette. A green candle and a green "long"
badge should be the same green, or the eye learns two vocabularies.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

BG = "#0e1117"
PANEL = "#161a23"
PANEL_2 = "#1c2130"
BORDER = "#252b3a"
TEXT = "#d7dce5"
MUTED = "#8a93a6"
UP = "#26a69a"
DOWN = "#ef5350"
WARN = "#ffb74d"
INFO = "#4fc3f7"
ACCENT = "#7c6cf0"

STATE_COLOUR = {
    "LONG_TRIGGERED": UP,
    "WATCH_LONG": "#4db6ac",
    "SHORT_TRIGGERED": DOWN,
    "WATCH_SHORT": "#e57373",
    "WAIT": WARN,
    "NO_TRADE": MUTED,
}

TREND_ICON = {
    "bullish": "▲",
    "bearish": "▼",
    "sideways": "◆",
    "unclear": "?",
}

EVIDENCE_ICON = {"OBSERVED": "👁", "INFERRED": "⇒", "UNCERTAIN": "?"}

CSS = f"""
<style>
  .stApp {{ background: {BG}; }}
  section[data-testid="stSidebar"] {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
  h1, h2, h3, h4 {{ color: {TEXT}; font-weight: 650; letter-spacing: -0.01em; }}
  .vmi-card {{
      background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px;
      padding: 14px 16px; margin-bottom: 12px;
  }}
  .vmi-card h4 {{ margin: 0 0 8px 0; font-size: 0.95rem; }}
  .vmi-banner {{
      border-radius: 12px; padding: 18px 22px; margin-bottom: 14px;
      background: linear-gradient(90deg, {PANEL_2} 0%, {PANEL} 100%);
      border: 1px solid {BORDER}; border-left-width: 6px;
  }}
  .vmi-banner .state {{ font-size: 1.9rem; font-weight: 700; letter-spacing: -0.02em; }}
  .vmi-banner .sub {{ color: {MUTED}; font-size: 0.9rem; margin-top: 2px; }}
  .vmi-badge {{
      display: inline-block; padding: 2px 9px; border-radius: 999px;
      font-size: 0.74rem; font-weight: 600; margin-right: 6px; border: 1px solid transparent;
  }}
  .vmi-kv {{ display: flex; justify-content: space-between; gap: 18px; padding: 4px 0;
             border-bottom: 1px dashed {BORDER}; font-size: 0.86rem; align-items: baseline; }}
  .vmi-kv span:first-child {{ color: {MUTED}; white-space: nowrap; flex: 0 0 auto; }}
  .vmi-kv span:last-child {{ color: {TEXT}; font-variant-numeric: tabular-nums;
                             text-align: right; }}
  .vmi-muted {{ color: {MUTED}; font-size: 0.84rem; }}
  .vmi-mono {{ font-family: ui-monospace, Consolas, monospace; font-size: 0.8rem; }}
  div[data-testid="stMetricValue"] {{ font-size: 1.35rem; }}
  .stTabs [data-baseweb="tab-list"] {{ gap: 2px; border-bottom: 1px solid {BORDER}; }}
  .stTabs [data-baseweb="tab"] {{ padding: 8px 14px; font-size: 0.9rem; }}
  .stDataFrame {{ border: 1px solid {BORDER}; border-radius: 8px; }}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def fill_width() -> dict[str, Any]:
    """Kwargs telling a table to fill its container, across Streamlit versions.

    1.51 replaced `use_container_width=True` with `width="stretch"`, and passing
    the new form to an older build raises at render time. The console is
    routinely launched with whatever Streamlit is on the path, so it supports
    both.
    """
    try:
        major, minor = (int(part) for part in st.__version__.split(".")[:2])
    except ValueError:  # a dev build like "1.60.0.dev0"
        return {"width": "stretch"}
    return {"width": "stretch"} if (major, minor) >= (1, 51) else {"use_container_width": True}


def badge(text: str, colour: str = INFO) -> str:
    return (
        f'<span class="vmi-badge" style="background:{colour}22;color:{colour};'
        f'border-color:{colour}55">{text}</span>'
    )


def banner(state: str, symbol: str, subtitle: str) -> None:
    colour = STATE_COLOUR.get(state, MUTED)
    st.markdown(
        f'<div class="vmi-banner" style="border-left-color:{colour}">'
        f'<div class="state" style="color:{colour}">{state.replace("_", " ")}</div>'
        f'<div class="sub"><b style="color:{TEXT}">{symbol}</b> &nbsp;·&nbsp; {subtitle}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def kv(label: str, value: str) -> str:
    return f'<div class="vmi-kv"><span>{label}</span><span>{value}</span></div>'


def card(title: str, body_html: str, accent: str = BORDER) -> None:
    st.markdown(
        f'<div class="vmi-card" style="border-left:3px solid {accent}">'
        f"<h4>{title}</h4>{body_html}</div>",
        unsafe_allow_html=True,
    )


def confidence_bar(label: str, value: float) -> None:
    st.progress(min(max(value, 0.0), 1.0), text=f"{label} {value:.0%}")
