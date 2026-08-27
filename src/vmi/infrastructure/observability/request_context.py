"""A request id on every response, and on every log line it caused.

When a report looks wrong the first question is "which run was that", and the
answer has to be in the response the caller already has rather than in a log
nobody kept.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ...logging_utils import get_logger

log = get_logger("api")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Stamp `X-Request-ID`, time the call, log the outcome."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Duration-MS"] = f"{duration_ms:.1f}"
        log.info(
            "%s %s -> %d in %.1fms [%s]",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response
