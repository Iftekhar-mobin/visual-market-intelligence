# The HTTP API

```bash
vmi serve                       # http://127.0.0.1:8100
vmi serve --host 0.0.0.0 --port 9000
```

Interactive documentation is generated from the schemas at `/docs`, and the
OpenAPI JSON at `/openapi.json`. This page is the human version, with the notes
that matter to a consumer.

## Authentication

If `api.keys` is empty (the default) the API is **open** — bind it to
`127.0.0.1` and leave it there. To require a token:

```bash
export VMI_API__KEYS=your-token,another-token
```

Then send `Authorization: Bearer your-token`. `/health` is always open so a load
balancer can reach it.

Every response carries `X-Request-ID` and `X-Duration-MS`. Quote the request id
when reporting anything odd; it is in the server log next to the same call.

---

## `GET /health`

```json
{
  "status": "ok",
  "version": "0.1.0",
  "vision": {
    "provider": "ollama",
    "model": "qwen2.5vl:7b",
    "reachable": true,
    "detail": "4 models installed"
  },
  "data": { "provider": "yahoo" },
  "chart_version": "chart-v1",
  "config_digest": "51660942bde9",
  "timeframes": ["H4", "H1", "M15"],
  "runs_stored": 128
}
```

`status` is `degraded` rather than an error when the vision backend is
unreachable — the service is up, it just cannot see. `config_digest` is a hash of
everything that could change an answer; two runs with different digests are not
directly comparable.

---

## `POST /analyze`

The main entry point.

```json
{
  "symbol": "EURUSD",
  "timeframes": ["H4", "H1", "M15"],
  "as_of": null,
  "provider": null,
  "model": null,
  "data_provider": null,
  "store": true,
  "include_charts": false
}
```

| Field | Meaning |
|---|---|
| `symbol` | `EURUSD`, `AAPL`, `BTCUSD`, `XAUUSD`, `^GSPC` — the plain form, translated per feed |
| `timeframes` | subset of the configured ladder; omit for all of it |
| `as_of` | ISO timestamp. Every bar after it is dropped before charts are drawn |
| `provider` / `model` | override the vision backend for this request only |
| `data_provider` | `yahoo` \| `metatrader` \| `csv` |
| `store` | persist the run and its charts (default true) |
| `include_charts` | return the PNGs base64-encoded in the response |

The response is the full `VisionReport` plus a flattened `api` block for
consumers that want the short version:

```json
{
  "api": {
    "symbol": "EURUSD",
    "run_id": "EURUSD-20260827T161203-878b14",
    "as_of": "2026-08-27T16:12:03+00:00",
    "current_state": "WATCH_LONG",
    "market_regime": "BULLISH_TRENDING",
    "last_price": 1.16564,
    "opportunities": { "long": { … }, "short": { … } },
    "key_levels": { "support": [1.16453], "resistance": [1.16779, 1.17144] },
    "alignment": "ALIGNED_BULLISH",
    "confidence": 0.71,
    "risks": ["…"]
  },
  "metadata": { "run_id": "…", "provider": "ollama", "model": "qwen2.5vl:7b",
                "chart_version": "chart-v1", "prompt versions live on each observation",
                "config_digest": "…", "duration_ms": 74213.5 },
  "observations": [ { "timeframe": "H4", "role": "context", "trend": "bullish", … } ],
  "structure":   { "alignment": "ALIGNED_BULLISH", "conflicts": [], … },
  "long":  { "score": 0.78, "entry_zone": [...], "invalidation_price": 1.16421, … },
  "short": { … },
  "risk":  { "has_clear_invalidation": true, "veto": false, … },
  "traces": [ { "agent": "setup_analyst", "duration_ms": 24118.4, "status": "ok" } ],
  "summary": "EURUSD: aligned bullish across 3 timeframes …"
}
```

### States

| State | Meaning for a consumer |
|---|---|
| `NO_TRADE` | neither direction scored well enough. A normal, frequent answer |
| `WAIT` | both sides arguable and within 0.08 of each other |
| `WATCH_LONG` / `WATCH_SHORT` | one side is the better case; the trigger has not fired |
| `LONG_TRIGGERED` / `SHORT_TRIGGERED` | score cleared the threshold, the entry timeframe confirms, **and** price is at the entry zone |

A `*_TRIGGERED` state is the strongest claim the system makes and is still not an
instruction. `risk.veto` forces `NO_TRADE` regardless of score; the reason is in
`risk.veto_reason`.

### Timing and errors

Three vision calls. A hosted model answers in 10–40 seconds; a 7B model on CPU
takes one to three minutes per chart. Set your client timeout accordingly — the
console uses 900 seconds.

| Status | When |
|---|---|
| `400` | no configured timeframe matched, or a malformed request |
| `401` | keys are configured and the token is missing or wrong |
| `422` | the feed had no bars (bad symbol, market closed, history too old) |
| `502` | a model pull failed |

A vision model that fails does **not** produce an error. It produces a degraded
observation with `degraded: true`, zero confidence and the error text, and the
run continues on the remaining timeframes.

---

## `POST /analyze/charts`

For callers whose price data cannot leave their network.

```json
{
  "symbol": "EURUSD",
  "charts": { "H4": "<base64 png>", "H1": "<base64 png>", "M15": "<base64 png>" },
  "roles":  { "H4": "context", "H1": "setup", "M15": "entry" }
}
```

`roles` is optional — the first key becomes `context`, the last `entry`. Note the
trade-off: with no price series behind the images, **nothing can be grounded**.
No level is validated, no entry zone, stop or target is priced, and the report
appends a risk line saying so.

---

## Models

```http
GET  /models?provider=ollama&free_only=true
POST /models/select   { "provider": "ollama", "model": "qwen2.5vl:7b" }
POST /models/pull     { "provider": "ollama", "model": "qwen2.5vl:3b" }
```

`/models` returns what the backend can serve now, what is currently active, and a
`recommended` list of free models with sizes and notes. `select` changes the
process default until restart. `pull` is Ollama-only and takes as long as the
download does.

---

## Runs

```http
GET    /runs?limit=50&symbol=EURUSD&state=WATCH_LONG
GET    /runs/{run_id}
GET    /runs/{run_id}/chart/{timeframe}      -> image/png
DELETE /runs/{run_id}
```

`GET /runs/{id}/chart/{tf}` returns **the exact PNG that model was shown**, not a
re-render. That is the point of storing them: a re-render after a renderer change
would be a different picture, and the argument you are trying to settle is about
the original.

---

## `POST /replay`

```json
{ "symbol": "EURUSD", "start": "2026-08-01T00:00:00Z",
  "end": "2026-08-25T00:00:00Z", "step": "24h", "store": true }
```

Runs one analysis per cursor, then scores every report against the bars that
followed. Synchronous and slow by nature — 25 cursors is 75 vision calls. Use the
CLI (`vmi replay`) for anything large; this endpoint exists so the console can run
a short one.

```json
{
  "symbol": "EURUSD",
  "reports": 25,
  "failures": [],
  "outcomes": [ { "run_id": "…", "state": "WATCH_LONG", "signed_20": 0.0347,
                  "target_first": true, … } ],
  "summary":  [ { "state": "WATCH_LONG", "runs": 8, "mean_signed_20": 0.0391, … } ]
}
```

---

## `GET /symbols/search?q=euro`

Best-effort lookup against Yahoo's search endpoint, for the console's symbol box.
Returns `[]` rather than an error when the endpoint is unreachable — it is
undocumented and moves.

---

## Consuming this from another system

```python
import httpx

report = httpx.post(
    "http://vmi.internal:8100/analyze",
    json={"symbol": "EURUSD"},
    headers={"Authorization": "Bearer …"},
    timeout=900,
).json()["api"]

if report["current_state"] in ("WATCH_LONG", "LONG_TRIGGERED"):
    long_case = report["opportunities"]["long"]
    ...  # your decision agent takes it from here
```

Two habits worth adopting:

1. **Store the `run_id`** with whatever you decide. Six months later you can pull
   the report and the actual charts and see what the market looked like when you
   decided it.
2. **Read `risks` and `confidence`, not just `current_state`.** The state is a
   summary of a structured judgement; the caveats are where the judgement admits
   what it could not see.
