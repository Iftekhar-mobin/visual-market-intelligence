"""Scoring what the system said against what the market did.

Kept separate from the pipeline on purpose: the pipeline must never be able to
see the data this package reads.
"""

from .outcomes import Outcome, score, summarise
from .replay import ReplayResult, cursors, replay, score_reports

__all__ = [
    "Outcome",
    "ReplayResult",
    "cursors",
    "replay",
    "score",
    "score_reports",
    "summarise",
]
