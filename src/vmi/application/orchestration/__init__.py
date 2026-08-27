"""Sequencing. One class, because there is exactly one order that makes sense."""

from .pipeline import VisionPipeline, config_digest

__all__ = ["VisionPipeline", "config_digest"]
