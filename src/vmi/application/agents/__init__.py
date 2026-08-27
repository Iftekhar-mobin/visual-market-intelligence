"""The eight agents. Three read charts; the rest reason over what they read."""

from .base import Agent, AgentResult
from .opportunity import OpportunityAgent
from .preprocess import ChartPreprocessingAgent
from .report import ReportAgent, build_metadata
from .risk import RiskAgent
from .structure import StructureAgent
from .timeframe_analyst import TimeframeAnalyst

__all__ = [
    "Agent",
    "AgentResult",
    "ChartPreprocessingAgent",
    "OpportunityAgent",
    "ReportAgent",
    "RiskAgent",
    "StructureAgent",
    "TimeframeAnalyst",
    "build_metadata",
]
