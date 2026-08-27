"""The typed configuration tree.

Three layers, lowest priority first:

1. `configs/default.yaml` — the resolved defaults, readable in one screen.
2. `.env` in the repository root, if present.
3. Real environment variables.

Both env layers use the same spelling: ``VMI_<SECTION>__<FIELD>``, so
``VMI_VISION__MODEL=llava:13b`` sets ``config.vision.model``. Unknown keys in
the YAML are an error rather than a silently ignored typo — a config file that
lies about what it configures is worse than no config file.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .paths import DEFAULT_CONFIG, PROJECT_ROOT, project_path

ENV_PREFIX = "VMI_"
ENV_DELIMITER = "__"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VisionConfig(_Strict):
    """Which vision backend answers, and how patiently we wait for it."""

    provider: Literal["ollama", "openai_compatible", "openrouter", "stub"] = "ollama"
    model: str = "qwen2.5vl:7b"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    timeout_s: float = 300.0
    temperature: float = 0.0
    max_tokens: int = 1600
    retries: int = 2
    grounding: Literal["none", "window", "full"] = "window"
    """How much numeric context the prompt carries. See `prompts.analyst.facts_block`.
    The `stub` provider forces `full`, because arithmetic is all it has."""


class DataConfig(_Strict):
    provider: Literal["yahoo", "metatrader", "csv"] = "yahoo"
    cache_dir: str = "data/cache"
    cache_ttl_s: int = 900
    max_bars: int = 240


class ApiConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8100
    keys: list[str] = Field(default_factory=list)


class StorageConfig(_Strict):
    runs_dir: str = "data/runs"
    index_db: str = "data/runs/index.db"
    keep_charts: bool = True


class TimeframeConfig(_Strict):
    """One rung of the ladder: one chart, one visual analyst."""

    name: str
    interval: str
    lookback: str
    role: Literal["context", "setup", "entry"]
    indicators: list[str] = Field(default_factory=list)


class ChartConfig(_Strict):
    width_px: int = 1280
    height_px: int = 900
    dpi: int = 100
    style: Literal["dark", "light"] = "dark"
    price_gridlines: int = 8
    show_last_price_tag: bool = True
    candle_body_min_px: float = 1.0
    level_lookback_pivots: int = 5


class OpportunityConfig(_Strict):
    weight_alignment: float = 0.35
    weight_setup_confidence: float = 0.30
    weight_entry_confirmation: float = 0.20
    weight_structure_quality: float = 0.15
    min_score_to_watch: float = 0.45
    min_score_to_trigger: float = 0.65
    conflict_penalty: float = 0.25


class EvaluationConfig(_Strict):
    horizons: list[int] = Field(default_factory=lambda: [10, 20, 50])
    atr_multiple_hit: float = 1.0


class LoggingConfig(_Strict):
    level: str = "INFO"
    # `json` on a pydantic model shadows an inherited attribute, so the field is
    # named for what it does and the config file keeps the obvious spelling.
    as_json: bool = Field(default=False, alias="json")


class Config(_Strict):
    """Everything the system needs to run, validated at load."""

    vision: VisionConfig = Field(default_factory=VisionConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    timeframes: list[TimeframeConfig] = Field(default_factory=list)
    chart: ChartConfig = Field(default_factory=ChartConfig)
    opportunity: OpportunityConfig = Field(default_factory=OpportunityConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def runs_path(self) -> Path:
        return project_path(self.storage.runs_dir)

    @property
    def index_db_path(self) -> Path:
        return project_path(self.storage.index_db)

    @property
    def cache_path(self) -> Path:
        return project_path(self.data.cache_dir)

    def timeframe(self, name: str) -> TimeframeConfig:
        for frame in self.timeframes:
            if frame.name.upper() == name.upper():
                return frame
        raise KeyError(f"no timeframe named {name!r}; have {[f.name for f in self.timeframes]}")

    @property
    def entry_timeframe(self) -> TimeframeConfig:
        """The lowest rung — the one entries and evaluation are measured on."""
        for frame in self.timeframes:
            if frame.role == "entry":
                return frame
        return self.timeframes[-1]


def load_dotenv(path: Path | None = None) -> None:
    """Read `.env` into the process environment without overwriting real vars.

    Deliberately minimal — `KEY=value`, `#` comments, optional quotes. Anything
    fancier belongs in the shell that starts the process.
    """
    env_file = path or (PROJECT_ROOT / ".env")
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _coerce(raw: str) -> Any:
    """Turn an environment string into the JSON-ish value it looks like."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _apply_env(tree: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    """Overlay ``VMI_SECTION__FIELD`` variables onto the parsed YAML tree."""
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX) or ENV_DELIMITER not in name:
            continue
        section, _, field = name[len(ENV_PREFIX) :].partition(ENV_DELIMITER)
        section, field = section.lower(), field.lower()
        if not section or not field:
            continue
        value: Any = _coerce(raw)
        # `VMI_API__KEYS=a,b` is the shape people actually type for a list.
        if field == "keys" and isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        tree.setdefault(section, {})
        if isinstance(tree[section], dict):
            tree[section][field] = value
    return tree


def load_config(path: str | Path | None = None, **overrides: Any) -> Config:
    """Load, merge and validate the configuration.

    *overrides* are applied last and are section-scoped dicts, e.g.
    ``load_config(vision={"provider": "stub"})`` — the shape the API uses when a
    caller picks a model for a single request.
    """
    load_dotenv()
    config_path = Path(path) if path else DEFAULT_CONFIG
    tree: dict[str, Any] = {}
    if config_path.exists():
        tree = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    tree = _apply_env(tree, dict(os.environ))
    for section, value in overrides.items():
        if isinstance(value, dict) and isinstance(tree.get(section), dict):
            tree[section] = {**tree[section], **value}
        else:
            tree[section] = value
    return Config.model_validate(tree)


@lru_cache(maxsize=1)
def default_config() -> Config:
    """The process-wide configuration, loaded once.

    Anything that needs a modified copy should call `load_config` instead of
    mutating this — a shared mutable config is how two requests end up
    disagreeing about which model produced a report.
    """
    return load_config()
