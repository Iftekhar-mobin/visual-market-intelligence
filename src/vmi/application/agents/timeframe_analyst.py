"""The visual analysts — the only agents that look at a picture.

One instance per rung of the ladder. They differ in exactly two ways: the prompt
brief they are given (`context`, `setup`, `entry`) and the chart they are handed.
Everything else — the schema, the parsing, the grounding rules, the failure
behaviour — is shared, because three subtly different parsers is how a project
ends up with three subtly different definitions of "bullish".

Failure is a first-class path. If the model times out, refuses, or answers with
prose that contains no JSON, the analyst returns a *degraded* observation with
zero confidence rather than raising. Two good readings and one blank produce a
weaker report; one blank producing no report at all would be worse.
"""

from __future__ import annotations

from ...config import Config
from ...domain.models import ChartBundle, TimeframeObservation
from ...infrastructure.vision import VisionError, extract_json
from ..prompts import PROMPT_VERSION, SYSTEM, build_prompt
from .base import Agent, AgentResult
from .parsing import degraded_observation, parse_observation


class TimeframeAnalyst(Agent):
    """One chart, one reading."""

    def __init__(self, role: str, vision_model, config: Config) -> None:
        self.name = f"{role}_analyst"
        super().__init__()
        self.role = role
        self.vision = vision_model
        self.config = config

    @property
    def grounding(self) -> str:
        # The stub has no eyes; without the numeric block it has nothing at all.
        if getattr(self.vision, "provider", "") == "stub":
            return "full"
        return self.config.vision.grounding

    def run(self, bundle: ChartBundle) -> AgentResult[TimeframeObservation]:
        prompt = build_prompt(bundle, self.role, self.grounding)
        provider = getattr(self.vision, "provider", "unknown")
        model = getattr(self.vision, "model", "unknown")

        with self._traced(provider=provider, model=model, prompt_version=PROMPT_VERSION) as trace:
            image = bundle.image.data_b64 or ""
            try:
                raw = self.vision.analyze(image, prompt, SYSTEM)
                payload = extract_json(raw)
            except (VisionError, ValueError) as exc:
                trace.status = "failed"
                trace.error = str(exc)
                observation = degraded_observation(
                    timeframe=bundle.timeframe,
                    role=self.role,
                    provider=provider,
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    error=str(exc),
                )
                return AgentResult(value=observation, trace=trace)

            observation = parse_observation(
                payload,
                timeframe=bundle.timeframe,
                role=self.role,
                window=bundle.window,
                provider=provider,
                model=model,
                prompt_version=PROMPT_VERSION,
                duration_ms=getattr(self.vision, "last_duration_ms", 0.0),
                raw_text=raw,
            )
            observation = _backfill_levels(observation, bundle)
            self.log.info(
                "%s %s: trend=%s setup=%s confidence=%.2f",
                bundle.window.symbol,
                bundle.timeframe,
                observation.trend.value,
                observation.setup.value,
                observation.confidence,
            )
        return AgentResult(value=observation, trace=trace)


def _backfill_levels(
    observation: TimeframeObservation, bundle: ChartBundle
) -> TimeframeObservation:
    """Fill empty level lists from the computed ones, and mark that we did.

    A model that reads the trend correctly but returns no levels is common and
    still useful. The swing-point levels are already drawn on the chart it was
    shown, so using them is not inventing data — but the report has to say which
    numbers the model read and which the system supplied, so this leaves a note
    in `uncertainties` either way.
    """
    if observation.support and observation.resistance:
        return observation

    computed_support = [level.price for level in bundle.levels if level.kind == "support"]
    computed_resistance = [level.price for level in bundle.levels if level.kind == "resistance"]
    filled: list[str] = []

    if not observation.support and computed_support:
        observation.support = computed_support[:3]
        filled.append("support")
    if not observation.resistance and computed_resistance:
        observation.resistance = computed_resistance[:3]
        filled.append("resistance")
    if filled:
        observation.uncertainties.append(
            f"{' and '.join(filled)} levels were not read by the model; "
            "the swing-point levels drawn on the chart were used instead"
        )
    return observation
