"""Logging lives in `vmi.logging_utils`; this package holds the request-scoped
plumbing the API needs on top of it.
"""

from .request_context import RequestIdMiddleware

__all__ = ["RequestIdMiddleware"]
