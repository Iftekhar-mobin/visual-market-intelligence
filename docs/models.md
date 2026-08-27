# Choosing a vision model

Everything documented here is **free**. A paid model is a one-line config change
if you ever decide one is worth it, and nothing in the system needs to know.

---

## The four backends

| Provider | Where it runs | Cost | Best for |
|---|---|---|---|
| `ollama` | your machine | free | the default; private, offline, no rate limits |
| `openrouter` | hosted | free tier | far stronger models, no download, rate limited |
| `openai_compatible` | anywhere | free | llama.cpp, LM Studio, vLLM, Jan, a GPU box on your LAN |
| `stub` | your machine | free | no model at all — plumbing, regression, and a floor to beat |

---

## 1. Ollama — the default

```bash
# install from ollama.com, then
ollama pull qwen2.5vl:7b
vmi models              # confirms what is installed and reachable
vmi analyze EURUSD
```

| Model | Size | Notes |
|---|---|---|
| `qwen2.5vl:7b` | ~6 GB | **Start here if you have the RAM.** The best free local chart reader tried: reads printed price labels reliably and follows the JSON schema |
| `qwen2.5vl:3b` | ~3.2 GB | Half the memory, most of the ability. The one to use on a laptop |
| `llama3.2-vision:11b` | ~7.9 GB | Strong general vision, weaker at small numbers on an axis |
| `minicpm-v:8b` | ~5.5 GB | Good alternative; occasionally more verbose than the schema allows |
| `moondream` | ~1.7 GB | Tiny and quick. Use it to prove the plumbing works, not to trade |

### What to expect on hardware

| Machine | `qwen2.5vl:3b` | `qwen2.5vl:7b` |
|---|---|---|
| CPU only, 16 GB RAM | ~40–90 s per chart | ~2–4 min per chart |
| 8 GB GPU | ~8–15 s | ~20–40 s |
| 16 GB+ GPU | ~4–8 s | ~10–20 s |

A run is three charts, so multiply by three. Nothing in the pipeline holds a
lock while it waits, but the calls are sequential by design — the analysts are
independent readings, and running them in parallel against one local model just
queues them inside Ollama.

**Tips**

- `num_ctx` is set to 8192 by the provider. A chart is a large image and the
  default 2048 truncates the prompt before the model ever sees the instructions.
- `format: json` is passed to Ollama, which constrains the sampler to valid JSON.
  Small models still wrap it in prose sometimes; the parser copes.
- First call after a pull is slow while the weights page in. `retries: 2` in the
  config exists for exactly that.

---

## 2. OpenRouter — free tier, no download

Get a key at [openrouter.ai/keys](https://openrouter.ai/keys). Models whose id
ends in `:free` cost nothing.

```bash
export VMI_VISION__PROVIDER=openrouter
export VMI_VISION__MODEL='qwen/qwen2.5-vl-72b-instruct:free'
export VMI_VISION__API_KEY=sk-or-...
vmi analyze EURUSD
```

| Model | Notes |
|---|---|
| `qwen/qwen2.5-vl-72b-instruct:free` | The strongest free chart reader available anywhere. Start here |
| `qwen/qwen2.5-vl-32b-instruct:free` | Nearly as good, usually less busy |
| `meta-llama/llama-3.2-11b-vision-instruct:free` | Reliable fallback |
| `google/gemma-3-27b-it:free` | Good general vision |

Free tiers are shared and rate-limited: expect occasional `429`s, especially on
the 72B. The provider surfaces that as a readable message rather than a stack
trace, and a rate-limited call produces a degraded observation rather than
killing the run.

Your charts are uploaded to a third party on this path. If that matters for your
data, use Ollama.

---

## 3. Any OpenAI-compatible server

One class covers llama.cpp's server, LM Studio, vLLM, Jan,
text-generation-webui, and a GPU box on your network.

```bash
export VMI_VISION__PROVIDER=openai_compatible
export VMI_VISION__BASE_URL=http://192.168.1.50:8080/v1
export VMI_VISION__MODEL=qwen2-vl-7b
export VMI_VISION__API_KEY=              # usually empty for a local server
```

The request is a standard `chat/completions` with an `image_url` content part
carrying a `data:image/png;base64,…` URI, plus `response_format: json_object`.
Servers that ignore `response_format` are fine — the parser extracts JSON from
prose, from fenced blocks, and by brace scanning.

---

## 4. The stub — no model at all

```bash
vmi analyze EURUSD --provider stub
```

It does not look at the image. It reads the `CHART FACTS` block (grounding is
forced to `full` for it) and applies moving-average and oscillator rules, tagging
every observation `INFERRED` and recording the provider as `stub` so nothing
downstream can mistake it for chart perception.

Three jobs:

1. **First run** — see the pipeline work before downloading gigabytes.
2. **Regression** — deterministic, offline, no sampling noise, so a change to the
   orchestration can be checked without a model in the loop.
3. **A floor.** A vision model that cannot beat these rules on replayed history is
   not earning its runtime. Run both and compare:

```bash
vmi replay EURUSD --start 2026-08-01 --end 2026-08-25 --provider stub --out reports/stub.csv
vmi replay EURUSD --start 2026-08-01 --end 2026-08-25 --provider ollama --out reports/vlm.csv
```

---

## Adding a paid model later

`OpenAICompatibleVisionModel` already speaks the wire format every hosted vendor
uses. A new backend is:

```python
class MyVendorVisionModel(OpenAICompatibleVisionModel):
    provider = "myvendor"

    def __init__(self, model: str, base_url: str = "https://api.myvendor.com/v1", **kwargs):
        super().__init__(model=model, base_url=base_url, **kwargs)
```

plus one line in `PROVIDERS`. Nothing in `application/` changes, because the
agents only ever see the `VisionModel` protocol.

---

## Judging a model on your own data

Prompt wording, chart design and model choice all interact. Change one at a time,
and keep the pieces that make a comparison meaningful:

1. Fix the chart version (`chart-v1`) and the prompt version (`analyst-v1`).
2. Replay the same window and step with each model.
3. Compare `mean_signed_20` and `target_first_rate` by state, and read
   `rejected_levels` — the count of prices a model reported that were never on
   the chart is the fastest single measure of how well it is actually reading.

A model with a high `rejected_levels` rate is not reading the chart; it is
remembering what EURUSD usually trades at.
