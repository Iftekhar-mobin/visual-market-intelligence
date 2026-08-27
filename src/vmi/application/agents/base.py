"""What every agent has in common.

An agent here is a small, named, single-responsibility step that takes typed
input and returns typed output plus a trace of what it did. Three of them call a
vision model; four do not. That split is deliberate and is the main architectural
opinion in this project:

    perception  -> a vision model, because reading a picture needs one
    arbitration -> deterministic code, because "the timeframes disagree, so
                   halve the score" is a rule, and a rule written in Python can
                   be read, changed, unit-priced and replayed identically a year
                   later. Asking a language model to re-derive it on every run
                   buys nothing and costs reproducibility.

`AgentResult` keeps the trace beside the value so the pipeline can assemble the
run's provenance without every agent remembering to report itself.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from ...domain.models import AgentTrace
from ...logging_utils import get_logger

T = TypeVar("T")
log = get_logger("agent")


@dataclass
class AgentResult(Generic[T]):
    """A value, and the record of how it was produced."""

    value: T
    trace: AgentTrace


class Agent:
    """Base class: a name, a logger, and a timed trace helper."""

    name = "agent"

    def __init__(self) -> None:
        self.log = get_logger(f"agent.{self.name}")

    @contextmanager
    def _traced(self, **fields: Any) -> Iterator[AgentTrace]:
        """Time the block and record failure without swallowing the value.

        The trace is yielded so the body can attach the model and prompt version
        it actually used, which is not always the one it was constructed with.
        """
        trace = AgentTrace(agent=self.name, **fields)
        started = time.perf_counter()
        try:
            yield trace
        except Exception as exc:
            trace.status = "failed"
            trace.error = f"{type(exc).__name__}: {exc}"
            self.log.warning("%s failed: %s", self.name, exc)
            raise
        finally:
            trace.duration_ms = round((time.perf_counter() - started) * 1000, 1)
