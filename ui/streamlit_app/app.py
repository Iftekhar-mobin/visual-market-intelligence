"""The Visual Market Intelligence console.

An operator console, not a trading terminal. What it is built to show is *why*:
every state comes with the three timeframe readings behind it, the evidence each
analyst gave, the levels it read (and the ones it invented and had thrown away),
and the exact picture the model was looking at when it said so. A conclusion you
cannot interrogate is one you should not act on.

One command is enough — the sidebar starts the API itself when nothing is
answering:

    uv run streamlit run ui/streamlit_app/app.py

Running `vmi serve` separately still works, and is what you want when you need
the server log in front of you.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

import theme
from client import DEFAULT_BASE_URL, ApiError, VmiClient
from server import ApiServer, is_answering, parse_target

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FILL = theme.fill_width()

PROVIDERS = ["ollama", "openrouter", "openai_compatible", "stub"]
PROVIDER_LABEL = {
    "ollama": "Ollama (local, free)",
    "openrouter": "OpenRouter (hosted free tier)",
    "openai_compatible": "OpenAI-compatible server",
    "stub": "Rule-based stub (no model)",
}
DATA_PROVIDERS = ["yahoo", "metatrader", "csv"]
QUICK_SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "ETHUSD", "AAPL", "NVDA", "SPX500",
]

st.set_page_config(page_title="VMI Console", page_icon="👁", layout="wide")
theme.inject()


# --------------------------------------------------------------------- plumbing


@st.cache_resource
def api_server() -> ApiServer:
    return ApiServer(PROJECT_ROOT, PROJECT_ROOT / "logs" / "api.log")


def client() -> VmiClient:
    return VmiClient(
        st.session_state.get("base_url", DEFAULT_BASE_URL),
        st.session_state.get("api_key", ""),
    )


def remember(exchanges: list[dict[str, Any]]) -> None:
    """Keep the traffic for the Processing tab, newest run last."""
    history = st.session_state.setdefault("exchanges", [])
    history.extend(exchanges)
    del history[:-60]


def show_error(exc: ApiError) -> None:
    st.error(str(exc))
    if exc.status_code == 0:
        st.info("Start the API from the sidebar, or run `vmi serve` in a terminal.")
    elif exc.status_code == 401:
        st.info("Set the API key in the sidebar (`VMI_API__KEYS` on the server).")
    elif exc.status_code == 422:
        st.info("The feed had no bars for that symbol. Try another symbol or provider.")


def report_state() -> dict[str, Any] | None:
    return st.session_state.get("report")


# ---------------------------------------------------------------------- sidebar


def render_sidebar() -> None:
    st.sidebar.markdown("### 👁 Visual Market Intelligence")
    st.sidebar.caption("Charts in, conditional opportunities out.")

    with st.sidebar.expander("Connection", expanded=False):
        st.text_input("API base URL", DEFAULT_BASE_URL, key="base_url")
        st.text_input("API key", "", key="api_key", type="password")

    base_url = st.session_state.get("base_url", DEFAULT_BASE_URL)
    target = parse_target(base_url)
    server = api_server()
    online = is_answering(base_url)

    status = "🟢 API online" if online else "🔴 API offline"
    st.sidebar.markdown(f"**{status}**  \n<span class='vmi-muted'>{base_url}</span>",
                        unsafe_allow_html=True)

    start_clicked = target.is_local and not online and st.sidebar.button(
        "Start the API", type="primary", **FILL
    )
    if start_clicked:
        server.start(target)
        st.sidebar.info("Starting - give it a couple of seconds, then rerun.")
    elif online and server.managed:
        if st.sidebar.button(f"Stop the API (pid {server.pid})", **FILL):
            server.stop()
            st.rerun()

    health: dict[str, Any] = {}
    if online:
        try:
            connection = client()
            health = connection.health()
            remember(connection.exchanges)
        except ApiError:
            health = {}

    if health:
        vision = health.get("vision", {})
        mark = "🟢" if vision.get("reachable") else "🟠"
        st.sidebar.markdown(
            f"{mark} **{vision.get('provider', '?')}** · `{vision.get('model', '?')}`  \n"
            f"<span class='vmi-muted'>{vision.get('detail', '')}</span>",
            unsafe_allow_html=True,
        )
        st.sidebar.caption(
            f"charts {health.get('chart_version', '?')} · "
            f"config {health.get('config_digest', '?')} · "
            f"{health.get('runs_stored', 0)} runs stored"
        )

    st.sidebar.divider()
    st.sidebar.markdown("#### Analysis")

    symbol = st.sidebar.text_input("Symbol", value=st.session_state.get("symbol", "EURUSD"))
    st.session_state["symbol"] = symbol.strip().upper()
    picked = st.sidebar.selectbox("Quick pick", ["—", *QUICK_SYMBOLS], index=0)
    if picked != "—":
        st.session_state["symbol"] = picked

    ladder = health.get("timeframes") or ["H4", "H1", "M15"]
    st.sidebar.multiselect("Timeframes", ladder, default=ladder, key="timeframes")

    st.sidebar.selectbox(
        "Vision provider",
        PROVIDERS,
        index=PROVIDERS.index(health.get("vision", {}).get("provider", "ollama"))
        if health.get("vision", {}).get("provider") in PROVIDERS
        else 0,
        format_func=lambda name: PROVIDER_LABEL[name],
        key="provider",
    )
    st.sidebar.text_input(
        "Model",
        value=health.get("vision", {}).get("model", "qwen2.5vl:7b"),
        key="model",
        help="Anything the provider serves. The Models tab lists what is installed.",
    )
    st.sidebar.selectbox("Market data", DATA_PROVIDERS, key="data_provider")

    with st.sidebar.expander("Historical replay point", expanded=False):
        use_as_of = st.checkbox("Analyse the market as it looked at a past moment")
        as_of_date = st.date_input("Date", value=date.today() - timedelta(days=1))
        as_of_time = st.time_input("Time (UTC)", value=clock(12, 0))
        st.caption("Every bar after this moment is dropped before the charts are drawn.")

    as_of = (
        datetime.combine(as_of_date, as_of_time, tzinfo=timezone.utc).isoformat()
        if use_as_of
        else None
    )

    if st.sidebar.button("Analyse", type="primary", **FILL, disabled=not online):
        run_analysis(as_of)

    st.sidebar.divider()
    st.sidebar.caption(
        "Free by design: a local Ollama model, or OpenRouter's free tier. "
        "The stub provider runs the whole pipeline with no model at all."
    )


def run_analysis(as_of: str | None) -> None:
    connection = client()
    symbol = st.session_state.get("symbol", "EURUSD")
    with st.spinner(f"Reading {symbol} charts… a local model takes a minute or two."):
        try:
            report = connection.analyze(
                symbol,
                timeframes=st.session_state.get("timeframes") or None,
                as_of=as_of,
                provider=st.session_state.get("provider"),
                model=st.session_state.get("model"),
                data_provider=st.session_state.get("data_provider"),
                include_charts=True,
            )
        except ApiError as exc:
            remember(connection.exchanges)
            show_error(exc)
            return
    remember(connection.exchanges)
    st.session_state["report"] = report
    st.session_state["charts"] = report.get("chart_images", {})


# ----------------------------------------------------------------- report views


def render_report_tab() -> None:
    report = report_state()
    if not report:
        st.info(
            "Pick a symbol in the sidebar and press **Analyse**. "
            "With no vision model installed, choose the `stub` provider to see the "
            "pipeline run end to end."
        )
        render_how_it_works()
        return

    metadata = report["metadata"]
    as_of = metadata.get("as_of") or metadata.get("created_at", "")
    theme.banner(
        report["current_state"],
        report["symbol"],
        f"{report['market_regime']} · {metadata['provider']} / {metadata['model']} · "
        f"as of {as_of[:19].replace('T', ' ')} UTC",
    )

    columns = st.columns(5)
    columns[0].metric("Last price", _format_price(report.get("last_price")))
    columns[1].metric("Alignment", report["structure"]["alignment"].replace("_", " ").title())
    columns[2].metric("Confidence", f"{report['vision_confidence']:.0%}")
    columns[3].metric("Long score", f"{(report.get('long') or {}).get('score', 0):.2f}")
    columns[4].metric("Short score", f"{(report.get('short') or {}).get('score', 0):.2f}")

    st.markdown(f"<div class='vmi-card'>{report['summary']}</div>", unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        render_scenario(report.get("long"), theme.UP)
    with right:
        render_scenario(report.get("short"), theme.DOWN)

    levels, risks = st.columns([1, 1.4])
    with levels:
        support = report["key_levels"]["support"]
        resistance = report["key_levels"]["resistance"]
        body = "".join(
            theme.kv("resistance", _format_price(item["price"]))
            for item in reversed(resistance)
        ) + "".join(theme.kv("support", _format_price(item["price"])) for item in support)
        theme.card("Key levels", body or "<span class='vmi-muted'>none read</span>", theme.INFO)
    with risks:
        items = "".join(f"<li>{risk}</li>" for risk in report.get("risks", []))
        theme.card(
            "Risks and caveats",
            f"<ul style='margin:0;padding-left:18px'>{items}</ul>"
            if items
            else "<span class='vmi-muted'>none recorded</span>",
            theme.WARN,
        )

    risk = report.get("risk", {})
    if risk.get("veto"):
        st.warning(f"**Risk agent veto** — {risk.get('veto_reason')}")

    with st.expander("Structure synthesis — how the timeframes were reconciled"):
        structure = report["structure"]
        st.markdown(
            f"**Bias** {structure['bias']} · **dominant** {structure['dominant_timeframe']} · "
            f"**regime** {structure['regime']} · **confidence** {structure['confidence']:.2f}"
        )
        if structure["agreements"]:
            st.markdown("**Agreements**")
            for note in structure["agreements"]:
                st.markdown(f"- {note}")
        if structure["conflicts"]:
            st.markdown("**Conflicts**")
            for note in structure["conflicts"]:
                st.markdown(f"- {note}")


def render_scenario(scenario: dict[str, Any] | None, colour: str) -> None:
    if not scenario:
        return
    quality = scenario["quality"]
    body = (
        theme.badge(f"score {scenario['score']:.2f}", colour)
        + theme.badge(quality, colour if quality in ("high", "medium") else theme.MUTED)
        + theme.badge(scenario["setup_type"], theme.MUTED)
        + "<div style='height:8px'></div>"
    )
    body += theme.kv("holds while", scenario.get("condition") or "—")
    body += theme.kv(
        "entry zone",
        " – ".join(_format_price(value) for value in scenario["entry_zone"])
        if scenario["entry_zone"]
        else "—",
    )
    body += theme.kv("trigger", scenario.get("trigger") or "—")
    body += theme.kv("invalidation", scenario.get("invalidation") or "—")
    body += theme.kv(
        "targets",
        ", ".join(_format_price(value) for value in scenario["targets"]["zones"]) or "—",
    )
    body += theme.kv(
        "reward / risk",
        f"{scenario['reward_risk']:.2f}x" if scenario.get("reward_risk") else "—",
    )
    theme.card(f"{scenario['direction']} scenario", body, colour)

    with st.expander(f"{scenario['direction']} — evidence"):
        groups = (("supporting", scenario["supporting"]), ("against", scenario["conflicting"]))
        for group, items in groups:
            st.markdown(f"**{group.title()}**")
            if not items:
                st.caption("nothing recorded")
            for item in items:
                icon = theme.EVIDENCE_ICON.get(item["kind"], "·")
                st.markdown(
                    f"- {icon} {item['statement']}  "
                    f"<span class='vmi-muted'>({item['source']})</span>",
                    unsafe_allow_html=True,
                )


def render_charts_tab() -> None:
    report = report_state()
    if not report:
        st.info("Run an analysis to see the charts the model was shown.")
        return
    charts = st.session_state.get("charts") or {}
    observations = {item["timeframe"]: item for item in report["observations"]}

    for timeframe in report["metadata"]["timeframes"]:
        observation = observations.get(timeframe)
        st.markdown(f"#### {timeframe}")
        image, panel = st.columns([2.1, 1])
        with image:
            payload = charts.get(timeframe)
            if payload:
                st.image(base64.b64decode(payload), **FILL)
            else:
                blob = _fetch_chart(report["metadata"]["run_id"], timeframe)
                if blob:
                    st.image(blob, **FILL)
                else:
                    st.caption("no chart stored for this timeframe")
        with panel:
            if observation is None:
                st.caption("no reading")
                continue
            render_observation(observation)
        st.divider()


def render_observation(observation: dict[str, Any]) -> None:
    if observation["degraded"]:
        st.error(f"No reading: {observation.get('error')}")
        return
    icon = theme.TREND_ICON.get(observation["trend"], "·")
    body = (
        theme.badge(f"{icon} {observation['trend']}", theme.UP if observation["trend"] == "bullish"
                    else theme.DOWN if observation["trend"] == "bearish" else theme.MUTED)
        + theme.badge(observation["setup"], theme.INFO)
        + "<div style='height:6px'></div>"
        + theme.kv("strength", observation["trend_strength"])
        + theme.kv("regime", observation["regime"])
        + theme.kv("momentum", observation["momentum"])
        + theme.kv("volatility", observation["volatility"])
        + theme.kv("structure", observation.get("structure") or "—")
        + theme.kv("confidence", f"{observation['confidence']:.2f}")
        + theme.kv("read in", f"{observation['duration_ms'] / 1000:.1f}s")
    )
    theme.card(f"{observation['timeframe']} · {observation['role']}", body)

    if observation["evidence"]:
        with st.expander("evidence", expanded=False):
            for item in observation["evidence"]:
                st.markdown(
                    f"{theme.EVIDENCE_ICON.get(item['kind'], '·')} {item['statement']}"
                    f" <span class='vmi-muted'>({item['source']})</span>",
                    unsafe_allow_html=True,
                )
    if observation["uncertainties"]:
        with st.expander("what it could not read", expanded=False):
            for note in observation["uncertainties"]:
                st.markdown(f"- {note}")
    if observation.get("raw_text"):
        with st.expander("raw model output", expanded=False):
            st.code(observation["raw_text"][:4000], language="json")


def render_timeframes_tab() -> None:
    report = report_state()
    if not report:
        st.info("Run an analysis first.")
        return
    rows = [
        {
            "timeframe": item["timeframe"],
            "role": item["role"],
            "trend": item["trend"],
            "strength": item["trend_strength"],
            "regime": item["regime"],
            "momentum": item["momentum"],
            "volatility": item["volatility"],
            "setup": item["setup"],
            "confidence": item["confidence"],
            "support": ", ".join(_format_price(value) for value in item["support"]),
            "resistance": ", ".join(_format_price(value) for value in item["resistance"]),
            "read (s)": round(item["duration_ms"] / 1000, 1),
            "degraded": item["degraded"],
        }
        for item in report["observations"]
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, **FILL)

    rejected = [
        (item["timeframe"], item["rejected_levels"])
        for item in report["observations"]
        if item["rejected_levels"]
    ]
    if rejected:
        st.warning(
            "Levels reported outside the price range the chart covered, and discarded: "
            + "; ".join(f"{timeframe}: {values}" for timeframe, values in rejected)
        )


def render_runs_tab() -> None:
    connection = client()
    try:
        payload = connection.runs(limit=200)
    except ApiError as exc:
        show_error(exc)
        return
    remember(connection.exchanges)

    runs = payload["runs"]
    if not runs:
        st.info("No runs stored yet. Every analysis is saved with its charts.")
        return

    frame = pd.DataFrame(runs)
    left, right = st.columns([1, 3])
    with left:
        symbols = ["all", *payload.get("symbols", [])]
        chosen = st.selectbox("Symbol", symbols)
        states = ["all", *sorted(frame["state"].unique())]
        state = st.selectbox("State", states)
    filtered = frame
    if chosen != "all":
        filtered = filtered[filtered["symbol"] == chosen]
    if state != "all":
        filtered = filtered[filtered["state"] == state]

    with right:
        st.dataframe(
            filtered[
                ["created_at", "symbol", "state", "confidence", "long_score", "short_score",
                 "provider", "model", "run_id"]
            ],
            hide_index=True,
            **FILL,
        )

    run_id = st.selectbox("Open a run", filtered["run_id"].tolist())
    if st.button("Load this run into the Report tab"):
        try:
            report = connection.run(run_id)
        except ApiError as exc:
            show_error(exc)
            return
        st.session_state["report"] = report
        st.session_state["charts"] = {}
        st.success(f"Loaded {run_id}. Open the Report tab.")

    if len(filtered) > 2:
        counts = filtered["state"].value_counts()
        st.bar_chart(counts)


def render_replay_tab() -> None:
    st.markdown(
        "Walk the system through history, one cursor at a time, and score what it said "
        "against what the price did next. Bars after each cursor are dropped before the "
        "charts are drawn, so nothing here can see its own future."
    )
    columns = st.columns(4)
    symbol = columns[0].text_input("Symbol", st.session_state.get("symbol", "EURUSD"))
    start = columns[1].date_input("Start", value=date.today() - timedelta(days=14))
    end = columns[2].date_input("End", value=date.today() - timedelta(days=1))
    step = columns[3].selectbox("Step", ["4h", "12h", "24h", "48h", "7d"], index=2)

    st.caption(
        "One analysis per cursor. Fourteen days at a daily step is 14 model calls - "
        "minutes on a hosted model, longer on a laptop. Use `vmi replay` for anything large."
    )

    if st.button("Run replay", type="primary"):
        connection = client()
        with st.spinner("Replaying…"):
            try:
                result = connection.replay(
                    symbol.upper(),
                    datetime.combine(start, clock(0, 0), tzinfo=timezone.utc).isoformat(),
                    datetime.combine(end, clock(0, 0), tzinfo=timezone.utc).isoformat(),
                    step,
                )
            except ApiError as exc:
                remember(connection.exchanges)
                show_error(exc)
                return
        remember(connection.exchanges)
        st.session_state["replay"] = result

    result = st.session_state.get("replay")
    if not result:
        return

    st.success(f"{result['reports']} reports, {len(result['failures'])} failures")
    if result["summary"]:
        st.markdown("#### By state — what happened next")
        st.dataframe(pd.DataFrame(result["summary"]), hide_index=True, **FILL)
        st.caption(
            "`mean_signed_N` is the forward return over N bars multiplied by the direction "
            "the state asserted. Positive means the call was right on average. "
            "`target_first_rate` is how often the published target was touched before the "
            "published invalidation."
        )
    if result["outcomes"]:
        outcomes = pd.DataFrame(result["outcomes"])
        st.markdown("#### Every call")
        st.dataframe(outcomes, hide_index=True, **FILL)
        if "signed_20" in outcomes.columns:
            chart = outcomes.set_index("as_of")[["signed_20"]]
            st.line_chart(chart)
    for failure in result["failures"][:10]:
        st.caption(f"failed: {failure}")


def render_models_tab() -> None:
    connection = client()
    provider = st.selectbox(
        "Provider", PROVIDERS, format_func=lambda name: PROVIDER_LABEL[name], key="models_provider"
    )
    try:
        payload = connection.models(provider=provider)
    except ApiError as exc:
        show_error(exc)
        return
    remember(connection.exchanges)

    mark = "🟢" if payload["reachable"] else "🔴"
    st.markdown(f"{mark} **{provider}** — {payload['detail']}")
    st.caption(
        f"active: {payload['active']['provider']} / {payload['active']['model']}"
    )

    if payload["models"]:
        frame = pd.DataFrame(payload["models"])
        st.dataframe(frame, hide_index=True, **FILL)
        chosen = st.selectbox("Use this model", [item["id"] for item in payload["models"]])
        if st.button("Make it the default", type="primary"):
            try:
                result = connection.select_model(provider, chosen)
            except ApiError as exc:
                show_error(exc)
                return
            st.success(f"now serving {result['model']} — {result['detail']}")
    else:
        st.info("Nothing installed for this provider yet.")

    st.markdown("#### Free models worth having")
    st.dataframe(pd.DataFrame(payload["recommended"]), hide_index=True, **FILL)

    if provider == "ollama":
        to_pull = st.text_input("Pull a model", "qwen2.5vl:7b")
        if st.button("Download it"):
            with st.spinner(f"Pulling {to_pull} — gigabytes, this takes a while…"):
                try:
                    connection.pull_model(to_pull)
                except ApiError as exc:
                    show_error(exc)
                    return
            st.success(f"{to_pull} is installed.")
        st.caption(
            "Ollama must be installed and running: get it from ollama.com, then `ollama serve`."
        )


def render_processing_tab() -> None:
    report = report_state()
    if report:
        st.markdown("#### Agent trace")
        traces = pd.DataFrame(report["traces"])
        st.dataframe(traces, hide_index=True, **FILL)
        failed = traces[traces["status"] != "ok"] if "status" in traces else pd.DataFrame()
        if not failed.empty:
            st.warning("Some agents did not complete cleanly — see the `error` column.")

        st.markdown("#### Provenance")
        metadata = report["metadata"]
        body = "".join(
            theme.kv(key, str(value))
            for key, value in metadata.items()
            if key not in ("timeframes",)
        )
        theme.card("This run", body)

    st.markdown("#### HTTP exchanges")
    exchanges = st.session_state.get("exchanges", [])
    if not exchanges:
        st.caption("nothing yet")
        return
    for exchange in reversed(exchanges[-15:]):
        status = exchange.get("status", 0)
        icon = "🟢" if 200 <= status < 300 else "🔴"
        with st.expander(
            f"{icon} {exchange['method']} {exchange['url']} · {status} · "
            f"{exchange.get('duration_ms', 0):.0f}ms"
        ):
            st.code(json.dumps(exchange.get("request", {}), indent=2)[:2000], language="json")
            if "error" in exchange:
                st.error(exchange["error"])
            elif "response" in exchange:
                st.code(json.dumps(exchange["response"], indent=2)[:4000], language="json")


def render_how_it_works() -> None:
    st.markdown("#### How a run works")
    st.code(
        """  bars ──> charts ──┬─ H4 analyst  (context: trend, regime, major levels)
                    ├─ H1 analyst  (setup:   is there a trade here at all)
                    └─ M15 analyst (entry:   is it confirmed right now)
                          │
                          ├─ structure synthesis   do the timeframes agree
                          ├─ opportunity detection both directions, conditioned
                          ├─ risk / invalidation   can this be proved wrong
                          └─ final report          one state, one confidence""",
        language="text",
    )
    st.caption(
        "Only the three analysts see a picture. Everything after them is deterministic code, "
        "so the same readings always produce the same opportunity — which is what makes a "
        "replay worth running."
    )


# ------------------------------------------------------------------- formatting


def _format_price(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 100:
        return f"{number:,.2f}"
    if abs(number) >= 1:
        return f"{number:.5f}".rstrip("0").rstrip(".")
    return f"{number:.6f}"


def _fetch_chart(run_id: str, timeframe: str) -> bytes | None:
    connection = client()
    try:
        return connection.chart(run_id, timeframe)
    except ApiError:
        return None


# -------------------------------------------------------------------------- app


def main() -> None:
    render_sidebar()
    tabs = st.tabs(
        ["Report", "Charts", "Timeframes", "Runs", "Replay", "Models", "Processing"]
    )
    with tabs[0]:
        render_report_tab()
    with tabs[1]:
        render_charts_tab()
    with tabs[2]:
        render_timeframes_tab()
    with tabs[3]:
        render_runs_tab()
    with tabs[4]:
        render_replay_tab()
    with tabs[5]:
        render_models_tab()
    with tabs[6]:
        render_processing_tab()


main()
