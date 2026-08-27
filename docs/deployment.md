# Running it

## Locally, for one person

```bash
uv venv && uv pip install -e .
vmi console
```

The console starts the API itself if nothing is answering on the configured
address. That is convenience, not architecture: it supervises a child process
and still speaks HTTP to it exactly as any other client would.

For two terminals and a visible server log:

```bash
vmi serve                                   # terminal 1
streamlit run ui/streamlit_app/app.py       # terminal 2
```

## As a service

```bash
uvicorn vmi.interfaces.api.app:app --host 0.0.0.0 --port 8100 --workers 2
```

Before exposing it beyond localhost:

```bash
export VMI_API__KEYS=a-long-random-token,another-for-the-other-service
```

Notes that matter for a shared deployment:

- **Workers hold no shared state.** Pipelines are cached per worker; the run
  store is SQLite plus files, and SQLite is fine for this write volume (one row
  per run). Point `VMI_STORAGE__RUNS_DIR` at a persistent volume.
- **`POST /models/select` is per-process.** With several workers it changes the
  model in whichever worker answered. For a shared deployment, set the model with
  configuration and leave the endpoint to single-worker development use.
- **Requests are long.** Three vision calls, one to five minutes on local
  hardware. Raise your proxy's read timeout well past the default 60 seconds
  (`proxy_read_timeout 900s;` in nginx) or you will see 504s over a working
  backend.
- **One call at a time per model.** A local model serialises requests internally;
  more API workers than the GPU can serve just queues them with worse error
  messages.

## Docker

No image ships with the repository, deliberately — the interesting choice is
where the model lives, and that differs per deployment. The shape that works:

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        libfreetype6 libpng16-16 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs
RUN pip install --no-cache-dir -e .
ENV VMI_API__HOST=0.0.0.0 MPLBACKEND=Agg
EXPOSE 8100
CMD ["uvicorn", "vmi.interfaces.api.app:app", "--host", "0.0.0.0", "--port", "8100"]
```

Then either point it at an Ollama container on the same network —

```yaml
services:
  ollama:
    image: ollama/ollama
    volumes: [ollama:/root/.ollama]
  vmi:
    build: .
    environment:
      VMI_VISION__PROVIDER: ollama
      VMI_VISION__BASE_URL: http://ollama:11434
      VMI_VISION__MODEL: qwen2.5vl:7b
      VMI_STORAGE__RUNS_DIR: /data/runs
    volumes: [runs:/data/runs]
    ports: ["8100:8100"]
volumes: { ollama: {}, runs: {} }
```

— or set `VMI_VISION__PROVIDER=openrouter` and skip the GPU entirely, which is
the cheaper way to run this on a small VPS.

Matplotlib needs `MPLBACKEND=Agg` and a writable `MPLCONFIGDIR` in a read-only
container; the renderer already forces the Agg backend in code.

## Streamlit Community Cloud

The console deploys as-is (`ui/streamlit_app/app.py`) provided the API is
reachable from the container. The realistic combination is:

- vision provider `openrouter` (no GPU in a Streamlit container),
- the API running in the same container via the sidebar's start button, or on a
  small VPS the console points at,
- `VMI_VISION__API_KEY` and `VMI_API__KEYS` set as Streamlit secrets.

Resource limits are the constraint: ~1 GB of RAM and no persistent disk, so
`data/runs` does not survive a restart. Set `VMI_STORAGE__RUNS_DIR` to a mounted
volume if you need the history, or accept that a public demo is stateless.

## Configuration reference

Everything in `configs/default.yaml` is overridable as
`VMI_<SECTION>__<FIELD>`:

| Variable | Default | Notes |
|---|---|---|
| `VMI_VISION__PROVIDER` | `ollama` | `ollama` \| `openrouter` \| `openai_compatible` \| `stub` |
| `VMI_VISION__MODEL` | `qwen2.5vl:7b` | |
| `VMI_VISION__BASE_URL` | `http://127.0.0.1:11434` | ignored by `stub` |
| `VMI_VISION__API_KEY` | — | OpenRouter and hosted vendors |
| `VMI_VISION__GROUNDING` | `window` | `none` \| `window` \| `full` |
| `VMI_VISION__TIMEOUT_S` | `300` | raise it for a large model on CPU |
| `VMI_DATA__PROVIDER` | `yahoo` | `yahoo` \| `metatrader` \| `csv` |
| `VMI_DATA__MAX_BARS` | `240` | bars drawn per chart |
| `VMI_API__HOST` / `VMI_API__PORT` | `127.0.0.1` / `8100` | |
| `VMI_API__KEYS` | empty (open) | comma-separated bearer tokens |
| `VMI_STORAGE__RUNS_DIR` | `data/runs` | put it on a persistent volume |
| `VMI_LOGGING__LEVEL` | `INFO` | |
| `VMI_LOGGING__JSON` | `false` | structured logs for a collector |

A `.env` file in the project root is read at startup and never overrides a real
environment variable.

## Health checking

```bash
curl -fsS http://127.0.0.1:8100/health | jq .status
```

`ok` means the vision backend answered. `degraded` means the service is up and
cannot see — worth an alert, not a restart, because a restart will not pull the
model back.

## Housekeeping

Runs accumulate: a report, three PNGs and three raw replies each, roughly 500 KB.
A daily replay for a year is a few gigabytes. Delete old runs with
`DELETE /runs/{id}` or by removing the directory and running `vmi reindex` — the
directories are the truth, the index is derived.
